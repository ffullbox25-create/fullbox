from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import Sum
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

from billing.models import BillingService
from employees.models import Employee
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sku.models import Agency, SKU, SKUBarcode
from sklad.topology import os_location_code

from .client_portal import create_client_movement_request
from .exceptions import FbsInventoryError, FbsReplenishmentError
from .models import (
    FbsBox,
    FbsClientMovementRequest,
    FbsIntegrationProfile,
    FbsInventorySession,
    FbsPallet,
    FbsRackCell,
    FbsRackCellBinding,
    FbsReplenishmentAllocation,
    FbsReplenishmentLine,
    FbsReplenishmentPlan,
    FbsReplenishmentPreparedBox,
    FbsReplenishmentPreparedBoxItem,
    FbsStockBalance,
    FbsStockExportState,
    FbsStockMovement,
    FbsStorageCell,
    FbsStorageLock,
)
from .operator_views import _movement_final_box_label
from .services.client_movements import (
    accept_client_movement_request,
    approve_client_movement_by_manager,
    confirm_client_movement_by_warehouse,
    eligible_source_boxes,
    reject_client_movement_request,
    sync_client_movement_request_status,
)
from .services.replenishment import (
    cancel_replenishment_plan,
    claim_replenishment_plan,
    complete_staged_item_plan_placement,
    complete_replenishment_allocation,
    pack_staged_item_plan,
    reopen_completed_item_plan_for_placement,
    record_fbs_movement_marking_scan,
    stage_replenishment_allocation,
)
from .services.stock_sync import refresh_profile_stock_export_states
from .services.picking import pick_requires_box_scan
from .services.racks import _get_or_create_binding, configure_fbs_rack
from reachtruck.services.missing_box_reports import report_move_task_missing_box
from .staging import FBS_STAGING_CONTEXT_TYPE, movement_staging_container_code
from sklad.location_occupancy import FBS_STORAGE_CONTEXT_TYPE, shared_os_occupied_keys
from sklad.services.warehouse_transitions import WarehouseTransitionError
from sklad.services.warehouse_write_path import WarehouseWritePathService


_reachtruck_models = import_module("reach" + "truck.models")
_reachtruck_commands = import_module("reach" + "truck.services.task_commands")
_reachtruck_ui = import_module("reach" + "truck.services.ui_flows")
MoveRequest = _reachtruck_models.MoveRequest
MoveTask = _reachtruck_models.MoveTask
build_mobile_execution_snapshot = _reachtruck_commands.build_mobile_execution_snapshot
build_mobile_request_execution_snapshot = (
    _reachtruck_commands.build_mobile_request_execution_snapshot
)
complete_move_task = _reachtruck_commands.complete_move_task
scan_move_request_step = _reachtruck_commands.scan_move_request_step
scan_move_task_step = _reachtruck_commands.scan_move_task_step
take_move_request = _reachtruck_commands.take_move_request
take_move_task = _reachtruck_commands.take_move_task
collect_moves = _reachtruck_ui.collect_moves


class FbsMovementLabelFormattingTests(SimpleTestCase):
    def test_final_box_label_uses_short_legal_form_and_prominent_sequence(self):
        request_row = SimpleNamespace(
            agency='ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "КЕЙЗИ"',
            number="FBS-MOV-000048",
        )
        label = _movement_final_box_label(
            request_row,
            box_code="FBS-BOX-2951-2F8AF4FBE8",
            sequence_no=3,
        )
        html = render_to_string(
            "fbs/movement_labels.html",
            {
                "movement_request": request_row,
                "labels": [label],
                "back_url": "/fbs/operator/movements/48/",
            },
        )

        self.assertEqual(label["title"], "Короб №3")
        self.assertEqual(label["subtitle"], "ООО · FBS-MOV-000048")
        self.assertIn("Короб №3", html)
        self.assertIn("FBS-BOX-2951-2F8AF4FBE8", html)
        self.assertNotIn("ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ", html)

    def test_final_box_label_recognizes_individual_entrepreneur(self):
        request_row = SimpleNamespace(
            agency="Индивидуальный предприниматель Талеев Д. Т.",
            number="FBS-MOV-000049",
        )

        label = _movement_final_box_label(
            request_row,
            box_code="FBS-BOX-123",
            sequence_no=1,
        )

        self.assertEqual(label["subtitle"], "ИП · FBS-MOV-000049")


@override_settings(
    ROOT_URLCONF="fbs.test_urls",
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_ZONE_CODE="FBS",
    FBS_CLIENT_FULL_PALLET_LOGICAL_POST_ON_ACCEPT=False,
)
class FbsClientMovementExecutionTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.storekeeper = users.objects.create_user(username="movement-storekeeper")
        self.driver = users.objects.create_user(username="movement-driver")
        self.manager = users.objects.create_user(username="movement-manager")
        Employee.objects.create(
            user=self.storekeeper,
            full_name="Кладовщик перемещений",
            role="storekeeper",
        )
        self.driver_employee = Employee.objects.create(
            user=self.driver,
            full_name="Ричтрак перемещений",
            role="reachtruck_driver",
        )
        Employee.objects.create(
            user=self.manager,
            full_name="Менеджер",
            role="manager",
        )
        head_user = users.objects.create_user(username="movement-head-manager")
        Employee.objects.create(
            user=head_user,
            full_name="Начальник склада перемещений",
            role="head_manager",
        )
        self.agency = Agency.objects.create(
            agn_name="Клиент перемещений",
            mened_user_id=self.manager.id,
        )
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="MOV-SKU-1",
            name="Товар для FBS",
        )
        self.barcode = "4600000009001"
        SKUBarcode.objects.create(sku=self.sku, value=self.barcode, is_primary=True)
        self.general_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="STORAGE",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=41,
            section_no=1,
            tier_no=2,
            cell_no=1,
            location_code="MOV-GENERAL-1",
            is_storage=True,
        )
        self.source_pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="MOV-SOURCE-PALLET",
            current_location=self.general_location,
        )
        self.staging_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS-PREP",
            zone_kind=WarehouseLocation.ZONE_KIND_TRANSIT,
            row_no=41,
            section_no=1,
            tier_no=0,
            cell_no=1,
            location_code="MOV-FBS-PREP-1",
        )
        fbs_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=41,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="MOV-FBS-1",
            is_storage=True,
        )
        self.cell = FbsStorageCell.objects.create(
            cell_code="MOV-FBS-1",
            location=fbs_location,
        )
        self.pallet = FbsPallet.objects.create(
            agency=self.agency,
            cell=self.cell,
            pallet_code="MOV-PALLET-1",
            max_boxes=10,
        )
        self.item_target_box = FbsBox.objects.create(
            agency=self.agency,
            pallet=self.pallet,
            box_code="MOV-PICK-BOX-1",
        )
        self.free_os_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=3,
            section_no=2,
            tier_no=1,
            cell_no=1,
            location_code="OS-3-2-1-1",
            display_name="OS test free cell",
            is_storage=True,
        )
        BillingService.objects.get_or_create(
            code="fbs_receiving_goods",
            defaults={"name": "FBS: перемещение товара", "unit": "шт"},
        )
        BillingService.objects.get_or_create(
            code="fbs_movement_box",
            defaults={"name": "FBS: перемещение короба", "unit": "кор."},
        )

    def _source_box(
        self,
        code: str,
        *,
        normalize_gv: bool = True,
    ) -> WarehouseContainer:
        if normalize_gv and not str(code or "").strip().casefold().endswith("-gv"):
            code = f"{code}-gv"
        return WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code=code,
            parent_container=self.source_pallet,
            current_location=self.general_location,
        )

    def _pr_rack_cell(self, code: str) -> FbsRackCell:
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            location_code=code,
            display_name=f"PR rack {code}",
            is_topology_visible=False,
            is_fbs_visible=True,
            is_active=True,
            is_pickable=True,
        )
        rack = configure_fbs_rack(
            location_id=location.id,
            cell_count=1,
            created_by=self.storekeeper,
        )
        return FbsRackCell.objects.select_related(
            "rack__location",
            "storage_cell__location",
        ).get(rack=rack, position=1)

    def _snapshot(
        self,
        *,
        code: str,
        qty: int,
        barcode: str | None = None,
        goods_type: str = "gv",
        normalize_gv: bool = True,
    ):
        box = self._source_box(code, normalize_gv=normalize_gv)
        return WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            stock_unit_type="box",
            source_context_type="receiving",
            source_context_id=f"MOV-{code}",
            sku_ref=self.sku,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode=barcode or self.barcode,
            goods_type=goods_type,
            qty=qty,
            available_qty=qty,
            container=box,
            container_code=box.container_code,
            location=self.general_location,
            zone_code="STORAGE",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            warehouse_state_code="stored",
        )

    def _request(
        self,
        *,
        mode="item",
        qty=4,
        units_per_box=1,
        box_count=None,
    ):
        line = {
            "barcode": self.barcode,
            "qty": qty,
            "units_per_box": units_per_box,
        }
        if box_count is not None:
            line["box_count"] = box_count
        return create_client_movement_request(
            agency=self.agency,
            mode=mode,
            raw_lines=[line],
        )

    def _marking_code(self, serial: str, *, gtin: str = "04600000009001") -> str:
        return f"01{gtin}21{serial}"

    def _approve_and_accept(self, request_row):
        return accept_client_movement_request(
            request_id=request_row.id,
            accepted_by=self.storekeeper,
        )

    def _physical_replacement_plan_pair(self, request_row, *, replacement_qty=5):
        request_line = request_row.lines.get()
        original = FbsReplenishmentPlan.objects.create(
            agency=self.agency,
            client_movement_request=request_row,
            mode=FbsReplenishmentPlan.MODE_BOX,
            status=FbsReplenishmentPlan.STATUS_CANCELED,
            target_cell=self.cell,
            target_pallet=self.pallet,
            target_box=self.item_target_box,
            comment=f"Клиентская заявка {request_row.number}.",
            planned_qty=5,
            moved_qty=0,
        )
        FbsReplenishmentLine.objects.create(
            plan=original,
            client_movement_line=request_line,
            source_container=self._source_box(
                f"MOV-REPLACED-{request_row.id}",
            ),
            target_box=self.item_target_box,
            qty_requested=5,
            qty_planned=5,
            qty_moved=0,
            status=FbsReplenishmentLine.STATUS_CANCELED,
        )
        replacement = FbsReplenishmentPlan.objects.create(
            agency=self.agency,
            client_movement_request=request_row,
            mode=FbsReplenishmentPlan.MODE_BOX,
            status=FbsReplenishmentPlan.STATUS_DONE,
            target_cell=self.cell,
            target_pallet=self.pallet,
            target_box=self.item_target_box,
            comment=(
                f"Клиентская заявка {request_row.number}. "
                f"[replacement for plan {original.id}: TEST physical scan]"
            ),
            planned_qty=replacement_qty,
            moved_qty=replacement_qty,
        )
        FbsReplenishmentLine.objects.create(
            plan=replacement,
            client_movement_line=request_line,
            source_container=self._source_box(
                f"MOV-REPLACEMENT-{request_row.id}",
            ),
            target_box=self.item_target_box,
            qty_requested=replacement_qty,
            qty_planned=replacement_qty,
            qty_moved=replacement_qty,
            status=FbsReplenishmentLine.STATUS_DONE,
        )
        return original, replacement

    def test_box_request_accepts_whole_boxes_with_different_sizes(self):
        self._source_box("MOV-MIXED-SIZE-PALLET-BLOCKER")
        source_40 = self._snapshot(code="MOV-MIXED-SIZE-40", qty=40)
        source_39 = self._snapshot(code="MOV-MIXED-SIZE-39", qty=39)
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[
                {
                    "barcode": self.barcode,
                    "qty": 79,
                    "box_plan": [
                        {"units_per_box": 40, "box_count": 1},
                        {"units_per_box": 39, "box_count": 1},
                    ],
                }
            ],
            idempotency_key="movement-box-mixed-size-whole-boxes",
        )
        request_line = request_row.lines.get()
        self.assertEqual(request_line.units_per_box, 1)
        self.assertEqual(request_line.requested_box_count, 2)
        source_40.refresh_from_db()
        source_39.refresh_from_db()
        self.assertEqual(
            (source_40.available_qty, source_40.other_reserved_qty),
            (0, 40),
        )
        self.assertEqual(
            (source_39.available_qty, source_39.other_reserved_qty),
            (0, 39),
        )
        exact_allocations = WarehouseWritePathService.fbs_movement_reserve_allocations(
            agency=self.agency,
            request_id=request_row.id,
        )
        self.assertEqual(
            {row["container_id"] for row in exact_allocations},
            {source_40.container_id, source_39.container_id},
        )
        approve_client_movement_by_manager(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )

        result = accept_client_movement_request(
            request_id=request_row.id,
            accepted_by=self.storekeeper,
            target_pallet_id=self.pallet.id,
        )

        request_row.refresh_from_db()
        self.assertEqual(
            request_row.status,
            FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED,
        )
        self.assertEqual(sum(plan.planned_qty for plan in result.plans), 79)
        self.assertEqual(
            set(
                FbsReplenishmentLine.objects.filter(
                    plan__client_movement_request=request_row,
                    source_container__isnull=False,
                ).values_list("source_container_id", flat=True)
            ),
            {source_40.container_id, source_39.container_id},
        )

    def test_legacy_quantity_pool_accepts_mixed_size_whole_boxes(self):
        self._source_box("MOV-LEGACY-MIXED-PALLET-BLOCKER")
        source_40 = self._snapshot(code="MOV-LEGACY-MIXED-40", qty=40)
        source_39 = self._snapshot(code="MOV-LEGACY-MIXED-39", qty=39)
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[
                {
                    "barcode": self.barcode,
                    "qty": 79,
                    "box_plan": [
                        {"units_per_box": 40, "box_count": 1},
                        {"units_per_box": 39, "box_count": 1},
                    ],
                }
            ],
        )
        request_line = request_row.lines.get()
        request_row.idempotency_key = "legacy-mixed-size-quantity-pool"
        request_row.save(update_fields=["idempotency_key", "updated_at"])

        from sklad.services.fbs_quantity_reserves import reserve_quantity

        reserve_quantity(
            agency=self.agency,
            request_id=request_row.id,
            allocations=[
                {
                    "snapshot_id": source_40.id,
                    "request_line_id": request_line.id,
                    "qty": 40,
                },
                {
                    "snapshot_id": source_39.id,
                    "request_line_id": request_line.id,
                    "qty": 39,
                },
            ],
            created_by=self.manager,
        )
        approve_client_movement_by_manager(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )

        result = accept_client_movement_request(
            request_id=request_row.id,
            accepted_by=self.storekeeper,
            target_pallet_id=self.pallet.id,
        )

        request_row.refresh_from_db()
        self.assertEqual(
            request_row.status,
            FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED,
        )
        self.assertEqual(sum(plan.planned_qty for plan in result.plans), 79)
        self.assertEqual(
            set(
                FbsReplenishmentLine.objects.filter(
                    plan__client_movement_request=request_row,
                    source_container__isnull=False,
                ).values_list("source_container_id", flat=True)
            ),
            {source_40.container_id, source_39.container_id},
        )

    def test_quantity_pool_preserves_whole_boxes_and_repacks_only_remainder(self):
        source_20_a = self._snapshot(code="MOV-POOL-PARTIAL-20-A", qty=20)
        source_20_b = self._snapshot(code="MOV-POOL-PARTIAL-20-B", qty=20)
        source_partial = self._snapshot(code="MOV-POOL-PARTIAL-30", qty=30)
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[
                {
                    "barcode": self.barcode,
                    "qty": 70,
                    "box_plan": [
                        {"units_per_box": 20, "box_count": 2},
                        {"units_per_box": 30, "box_count": 1},
                    ],
                }
            ],
        )
        request_line = request_row.lines.get()
        request_line.requested_qty = 55
        request_line.requested_box_count = 2
        request_line.save(update_fields=["requested_qty", "requested_box_count"])
        request_row.requested_qty = 55
        request_row.idempotency_key = "legacy-partial-box-quantity-pool"
        request_row.save(update_fields=["requested_qty", "idempotency_key", "updated_at"])

        from sklad.services.fbs_quantity_reserves import reserve_quantity

        reserve_quantity(
            agency=self.agency,
            request_id=request_row.id,
            allocations=[
                {
                    "snapshot_id": source_20_a.id,
                    "request_line_id": request_line.id,
                    "qty": 20,
                },
                {
                    "snapshot_id": source_20_b.id,
                    "request_line_id": request_line.id,
                    "qty": 20,
                },
                {
                    "snapshot_id": source_partial.id,
                    "request_line_id": request_line.id,
                    "qty": 15,
                }
            ],
            created_by=self.manager,
        )
        approve_client_movement_by_manager(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )

        result = accept_client_movement_request(
            request_id=request_row.id,
            accepted_by=self.storekeeper,
            target_pallet_id=self.pallet.id,
        )

        box_plans = [plan for plan in result.plans if plan.mode == FbsReplenishmentPlan.MODE_BOX]
        item_plans = [plan for plan in result.plans if plan.mode == FbsReplenishmentPlan.MODE_ITEM]
        self.assertEqual(sum(plan.planned_qty for plan in box_plans), 50)
        self.assertEqual(len(item_plans), 1)
        self.assertEqual(item_plans[0].planned_qty, 5)
        self.assertEqual(item_plans[0].prepared_boxes.count(), 1)
        self.assertFalse(
            MoveTask.objects.filter(
                payload__fbs_plan_id=item_plans[0].id,
                payload__fbs_movement_marking_scan_v1=True,
            ).exists()
        )

    def test_completed_physical_replacement_unblocks_request_and_confirmation(self):
        self._snapshot(code="MOV-REPLACEMENT-POSITIVE", qty=5)
        request_row = self._request(
            mode=FbsClientMovementRequest.MODE_BOX,
            qty=5,
            units_per_box=5,
            box_count=1,
        )
        request_row.status = FbsClientMovementRequest.STATUS_IN_PROGRESS
        request_row.save(update_fields=["status", "updated_at"])
        original, replacement = self._physical_replacement_plan_pair(request_row)

        synced = sync_client_movement_request_status(
            request_row.id,
            performed_by=self.storekeeper,
        )

        self.assertEqual(synced.status, FbsClientMovementRequest.STATUS_MOVED)
        confirmed = confirm_client_movement_by_warehouse(
            request_id=request_row.id,
            confirmed_by=self.storekeeper,
        )
        self.assertEqual(confirmed.status, FbsClientMovementRequest.STATUS_COMPLETED)
        self.assertEqual(confirmed.actual_moved_qty, 5)
        self.assertEqual(confirmed.actual_moved_box_count, 1)
        original.refresh_from_db()
        replacement.refresh_from_db()
        self.assertEqual(original.status, FbsReplenishmentPlan.STATUS_CANCELED)
        self.assertEqual(replacement.status, FbsReplenishmentPlan.STATUS_DONE)

    def test_physical_replacement_with_wrong_quantity_remains_blocked(self):
        self._snapshot(code="MOV-REPLACEMENT-MISMATCH", qty=5)
        request_row = self._request(
            mode=FbsClientMovementRequest.MODE_BOX,
            qty=5,
            units_per_box=5,
            box_count=1,
        )
        request_row.status = FbsClientMovementRequest.STATUS_IN_PROGRESS
        request_row.save(update_fields=["status", "updated_at"])
        self._physical_replacement_plan_pair(request_row, replacement_qty=4)

        synced = sync_client_movement_request_status(request_row.id)

        self.assertEqual(
            synced.status,
            FbsClientMovementRequest.STATUS_IN_PROGRESS,
        )
        request_row.status = FbsClientMovementRequest.STATUS_MOVED
        request_row.save(update_fields=["status", "updated_at"])
        with self.assertRaisesMessage(
            FbsReplenishmentError,
            "Есть отмененные задания без отметки «короб не найден».",
        ):
            confirm_client_movement_by_warehouse(
                request_id=request_row.id,
                confirmed_by=self.storekeeper,
            )

    def test_item_request_books_fbs_stock_only_after_driver_places_generated_box(self):
        source = self._snapshot(code="MOV-SOURCE-ITEM", qty=10)
        unrelated = self._snapshot(code="MOV-UNRELATED", qty=3)
        request_row = self._request(qty=4)

        result = self._approve_and_accept(request_row)
        repeated = accept_client_movement_request(
            request_id=request_row.id,
            accepted_by=self.storekeeper,
        )

        source.refresh_from_db()
        unrelated.refresh_from_db()
        request_row.refresh_from_db()
        self.assertEqual(len(result.plans), 1)
        self.assertEqual(repeated.plans[0].id, result.plans[0].id)
        self.assertEqual(
            request_row.status,
            FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED,
        )
        self.assertEqual(source.qty, 10)
        self.assertEqual(source.available_qty, 6)
        self.assertEqual(source.other_reserved_qty, 4)
        self.assertEqual(unrelated.qty, 3)
        self.assertFalse(FbsStockBalance.objects.exists())
        self.assertEqual(WarehouseOperation.objects.count(), 1)
        self.assertEqual(WarehouseReserve.objects.count(), 1)
        staging_container = WarehouseContainer.objects.get(
            agency=self.agency,
            source_context_type=FBS_STAGING_CONTEXT_TYPE,
            source_context_id=str(request_row.id),
        )
        self.assertEqual(
            staging_container.container_code,
            movement_staging_container_code(request_row),
        )
        self.assertEqual(staging_container.current_location_id, self.staging_location.id)
        self.assertEqual(staging_container.status, WarehouseContainer.STATUS_ACTIVE)

        plan = result.plans[0]
        self.assertIsNone(plan.target_box_id)
        self.assertEqual(plan.staging_location_id, self.staging_location.id)
        claim_replenishment_plan(plan_id=plan.id, assigned_to=self.driver)

        request_row.refresh_from_db()
        self.assertEqual(request_row.status, FbsClientMovementRequest.STATUS_IN_PROGRESS)
        allocation = FbsReplenishmentAllocation.objects.get(line__plan=plan)
        staged = stage_replenishment_allocation(
            allocation_id=allocation.id,
            source_scan=source.container_code,
            staging_container_scan=movement_staging_container_code(request_row),
            staging_scan=self.staging_location.location_code,
            performed_by=self.driver,
        )
        repeated_staged = stage_replenishment_allocation(
            allocation_id=allocation.id,
            source_scan=source.container_code,
            staging_container_scan=movement_staging_container_code(request_row),
            staging_scan=self.staging_location.location_code,
            performed_by=self.driver,
        )

        source.refresh_from_db()
        unrelated.refresh_from_db()
        request_row.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(staged.id, repeated_staged.id)
        self.assertEqual(staged.qty_staged, 4)
        self.assertEqual(plan.status, FbsReplenishmentPlan.STATUS_AWAITING_PACK)
        self.assertEqual(request_row.status, FbsClientMovementRequest.STATUS_IN_PROGRESS)
        self.assertEqual(source.qty, 6)
        self.assertEqual(source.available_qty, 6)
        self.assertEqual(source.other_reserved_qty, 0)
        self.assertFalse(FbsStockBalance.objects.exists())
        self.assertFalse(FbsStockMovement.objects.exists())
        self.assertTrue(
            WarehouseEvent.objects.filter(event_type="fbs_replenishment_staged").exists()
        )
        staged_event = WarehouseEvent.objects.get(event_type="fbs_replenishment_staged")
        self.assertEqual(
            staged_event.payload["staging_container_code"],
            movement_staging_container_code(request_row),
        )

        packed_box = pack_staged_item_plan(
            plan_id=plan.id,
            packed_by=self.storekeeper,
            staging_container_scan=movement_staging_container_code(request_row),
        )
        repeated_box = pack_staged_item_plan(
            plan_id=plan.id,
            packed_by=self.storekeeper,
            staging_container_scan=movement_staging_container_code(request_row),
        )

        source.refresh_from_db()
        unrelated.refresh_from_db()
        request_row.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(plan.status, FbsReplenishmentPlan.STATUS_IN_PROGRESS)
        self.assertEqual(request_row.status, FbsClientMovementRequest.STATUS_IN_PROGRESS)
        self.assertEqual(repeated_box.id, packed_box.id)
        self.assertTrue(packed_box.box_code.startswith(f"FBS-BOX-{self.agency.id}-"))
        self.assertEqual(packed_box.source_container.current_location_id, self.staging_location.id)
        self.assertFalse(FbsStockBalance.objects.exists())
        self.assertFalse(FbsStockMovement.objects.exists())
        placement_task = MoveTask.objects.get(
            legacy_order_id=f"FBS-RPL-PLACE-P{plan.id}"
        )
        self.assertEqual(placement_task.status, MoveTask.STATUS_CREATED)

        complete_staged_item_plan_placement(
            plan_id=plan.id,
            target_box_scan=packed_box.box_code,
            destination_scan="A-3/1-1",
            performed_by=self.driver,
        )

        request_row.refresh_from_db()
        plan.refresh_from_db()
        balance = FbsStockBalance.objects.get(
            agency=self.agency, box=packed_box, barcode=self.barcode
        )
        self.assertEqual(request_row.status, FbsClientMovementRequest.STATUS_MOVED)
        self.assertIsNone(request_row.completed_at)
        self.assertEqual(source.qty, 6)
        self.assertEqual(source.available_qty, 6)
        self.assertEqual(source.other_reserved_qty, 0)
        self.assertEqual(unrelated.qty, 3)
        self.assertEqual(balance.qty, 4)
        self.assertEqual(balance.available_qty, 0)
        self.assertEqual(FbsStockMovement.objects.count(), 1)
        self.assertEqual(plan.target_cell.location_id, self.free_os_location.id)
        staging_container.refresh_from_db()
        self.assertEqual(staging_container.status, WarehouseContainer.STATUS_ARCHIVED)
        self.assertTrue(
            WarehouseEvent.objects.filter(event_type="fbs_replenishment_completed").exists()
        )
        confirmed = confirm_client_movement_by_warehouse(
            request_id=request_row.id,
            confirmed_by=self.storekeeper,
        )
        self.assertEqual(
            confirmed.status,
            FbsClientMovementRequest.STATUS_COMPLETED,
        )
        self.assertEqual(confirmed.actual_moved_box_count, 1)
        self.assertIsNotNone(confirmed.completed_at)
        self.assertIsNotNone(confirmed.billing_synced_at)
        balance.refresh_from_db()
        self.assertEqual(balance.available_qty, 4)

    def test_item_request_moves_complete_reserved_source_box_without_repacking(self):
        source = self._snapshot(code="MOV-SOURCE-WHOLE", qty=5)
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_ITEM,
            raw_lines=[{"barcode": self.barcode, "qty": 5}],
            requested_box_count=2,
            idempotency_key="movement-item-whole-source-box",
        )
        approve_client_movement_by_manager(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )

        result = accept_client_movement_request(
            request_id=request_row.id,
            accepted_by=self.storekeeper,
            target_pallet_id=self.pallet.id,
            prepared_box_count=2,
        )

        self.assertEqual(len(result.plans), 1)
        plan = result.plans[0]
        self.assertEqual(plan.mode, FbsReplenishmentPlan.MODE_BOX)
        self.assertIsNone(plan.staging_location_id)
        self.assertEqual(plan.prepared_boxes.count(), 0)
        self.assertEqual(plan.target_box.source_container_id, source.container_id)
        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)
        self.assertEqual(task.move_mode, MoveTask.MODE_BOX_FULL)
        self.assertEqual(task.payload["source_box_code"], source.container.container_code)
        self.assertNotIn("fbs_prepared_boxes_v1", task.payload)

    def test_item_request_splits_full_source_box_from_partial_repacking(self):
        full_source = self._snapshot(code="MOV-SOURCE-SPLIT-FULL", qty=2)
        partial_source = self._snapshot(code="MOV-SOURCE-SPLIT-PARTIAL", qty=3)
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_ITEM,
            raw_lines=[{"barcode": self.barcode, "qty": 4}],
            requested_box_count=2,
            idempotency_key="movement-item-split-full-source-box",
        )
        approve_client_movement_by_manager(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )

        result = accept_client_movement_request(
            request_id=request_row.id,
            accepted_by=self.storekeeper,
            target_pallet_id=self.pallet.id,
            prepared_box_count=2,
        )

        self.assertEqual(len(result.plans), 2)
        box_plan = next(
            plan for plan in result.plans if plan.mode == FbsReplenishmentPlan.MODE_BOX
        )
        item_plan = next(
            plan for plan in result.plans if plan.mode == FbsReplenishmentPlan.MODE_ITEM
        )
        self.assertEqual(box_plan.planned_qty, 2)
        self.assertEqual(
            box_plan.target_box.source_container_id,
            full_source.container_id,
        )
        self.assertEqual(item_plan.planned_qty, 2)
        self.assertEqual(item_plan.prepared_boxes.count(), 1)
        full_task = MoveTask.objects.get(payload__fbs_plan_id=box_plan.id)
        partial_task = MoveTask.objects.get(payload__fbs_plan_id=item_plan.id)
        self.assertEqual(full_task.move_mode, MoveTask.MODE_BOX_FULL)
        self.assertNotIn("fbs_prepared_boxes_v1", full_task.payload)
        self.assertTrue(partial_task.payload["fbs_prepared_boxes_v1"])
        full_source.refresh_from_db()
        partial_source.refresh_from_db()
        self.assertEqual(
            (full_source.available_qty, full_source.other_reserved_qty),
            (0, 2),
        )
        self.assertEqual(
            (partial_source.available_qty, partial_source.other_reserved_qty),
            (1, 2),
        )

    def test_box_request_splits_full_source_box_from_partial_repacking(self):
        full_source = self._snapshot(code="MOV-BOX-SPLIT-FULL", qty=2)
        partial_source = self._snapshot(code="MOV-BOX-SPLIT-PARTIAL", qty=2)
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[
                {
                    "barcode": self.barcode,
                    "qty": 4,
                    "units_per_box": 2,
                    "box_count": 2,
                }
            ],
            idempotency_key="movement-box-split-full-source-box",
        )
        approve_client_movement_by_manager(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )
        WarehouseStockSnapshot.objects.filter(pk=partial_source.id).update(
            qty=3,
            available_qty=3,
        )
        partial_source.refresh_from_db()

        result = accept_client_movement_request(
            request_id=request_row.id,
            accepted_by=self.storekeeper,
            target_pallet_id=self.pallet.id,
        )

        self.assertEqual(len(result.plans), 2)
        box_plan = next(
            plan for plan in result.plans if plan.mode == FbsReplenishmentPlan.MODE_BOX
        )
        item_plan = next(
            plan for plan in result.plans if plan.mode == FbsReplenishmentPlan.MODE_ITEM
        )
        self.assertEqual(box_plan.planned_qty, 2)
        self.assertEqual(
            box_plan.target_box.source_container_id,
            full_source.container_id,
        )
        self.assertNotIn(
            FbsReplenishmentPlan.CLIENT_ITEM_WHOLE_BOX_MARKER,
            box_plan.comment,
        )
        self.assertEqual(item_plan.planned_qty, 2)
        self.assertTrue(
            item_plan.comment.startswith(
                FbsReplenishmentPlan.CLIENT_BOX_ITEM_FALLBACK_MARKER
            )
        )
        self.assertEqual(item_plan.prepared_boxes.count(), 1)
        full_task = MoveTask.objects.get(payload__fbs_plan_id=box_plan.id)
        partial_task = MoveTask.objects.get(payload__fbs_plan_id=item_plan.id)
        self.assertEqual(full_task.move_mode, MoveTask.MODE_BOX_FULL)
        self.assertNotIn("fbs_prepared_boxes_v1", full_task.payload)
        self.assertTrue(partial_task.payload["fbs_prepared_boxes_v1"])
        full_source.refresh_from_db()
        partial_source.refresh_from_db()
        self.assertEqual(
            (full_source.available_qty, full_source.other_reserved_qty),
            (0, 2),
        )
        self.assertEqual(
            (partial_source.available_qty, partial_source.other_reserved_qty),
            (1, 2),
        )

    def test_item_request_never_reserves_nonready_stock_with_same_barcode(self):
        nonready = self._snapshot(
            code="MOV-NONREADY-SAME-BARCODE",
            qty=100,
            goods_type="no",
        )
        ready = self._snapshot(code="MOV-READY-SAME-BARCODE", qty=10)
        request_row = self._request(qty=4)

        result = self._approve_and_accept(request_row)

        nonready.refresh_from_db()
        ready.refresh_from_db()
        allocation = FbsReplenishmentAllocation.objects.get(line__plan=result.plans[0])
        self.assertEqual(allocation.source_snapshot_id, ready.id)
        self.assertEqual(
            (nonready.qty, nonready.available_qty, nonready.other_reserved_qty),
            (100, 100, 0),
        )
        self.assertEqual(
            (ready.qty, ready.available_qty, ready.other_reserved_qty),
            (10, 6, 4),
        )

    def test_box_candidates_require_ready_stock_in_storage_state(self):
        nonready = self._snapshot(
            code="MOV-NONREADY-BOX",
            qty=5,
            goods_type="no",
        )
        receiving_ready = self._snapshot(code="MOV-RECEIVING-READY-BOX", qty=5)
        receiving_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            location_code="MOV-RECEIVING",
        )
        receiving_ready.location = receiving_location
        receiving_ready.zone_code = "PR"
        receiving_ready.zone_kind = WarehouseLocation.ZONE_KIND_RECEIVING
        receiving_ready.warehouse_state_code = "placed_in_receiving"
        receiving_ready.save(
            update_fields=[
                "location",
                "zone_code",
                "zone_kind",
                "warehouse_state_code",
                "updated_at",
            ]
        )
        receiving_ready.container.current_location = receiving_location
        receiving_ready.container.save(update_fields=["current_location", "updated_at"])
        ready = self._snapshot(code="MOV-READY-STORED-BOX", qty=5)

        candidates = eligible_source_boxes(
            agency_id=self.agency.id,
            barcodes=(self.barcode,),
            units_per_box=5,
        )

        self.assertEqual(
            [candidate.container.id for candidate in candidates],
            [ready.container_id],
        )
        self.assertEqual(nonready.available_qty, 5)
        self.assertEqual(receiving_ready.available_qty, 5)

    def test_box_candidates_accept_active_gv_and_vp_box_containers(self):
        ready = self._snapshot(code="MOV-READY-GV-BOX", qty=5)
        legacy_vp = self._snapshot(
            code="MOV-READY-LEGACY_VP",
            qty=5,
            normalize_gv=False,
        )
        wrong_suffix = self._snapshot(
            code="MOV-WRONG-TYPE-vz",
            qty=5,
            normalize_gv=False,
        )

        pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="MOV-PALLET-gv",
            current_location=self.general_location,
        )
        pallet_stock = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            stock_unit_type="pallet",
            source_context_type="receiving",
            source_context_id="MOV-PALLET-GV",
            sku_ref=self.sku,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode=self.barcode,
            goods_type="gv",
            qty=5,
            available_qty=5,
            container=pallet,
            container_code=pallet.container_code,
            location=self.general_location,
            zone_code="STORAGE",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            warehouse_state_code="stored",
        )
        loose_stock = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            stock_unit_type="item",
            source_context_type="receiving",
            source_context_id="MOV-LOOSE-GV",
            sku_ref=self.sku,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode=self.barcode,
            goods_type="gv",
            qty=5,
            available_qty=5,
            container=None,
            container_code="",
            location=self.general_location,
            zone_code="STORAGE",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            warehouse_state_code="stored",
        )

        candidates = eligible_source_boxes(
            agency_id=self.agency.id,
            barcodes=(self.barcode,),
            units_per_box=5,
        )

        self.assertEqual(
            [candidate.container.id for candidate in candidates],
            [ready.container_id, legacy_vp.container_id],
        )
        self.assertEqual(wrong_suffix.available_qty, 5)
        self.assertEqual(pallet_stock.available_qty, 5)
        self.assertEqual(loose_stock.available_qty, 5)

    def test_fbs_movement_write_path_rejects_nonready_receiving_or_non_gv_box_stock(self):
        nonready = self._snapshot(
            code="MOV-WRITE-PATH-NONREADY",
            qty=5,
            goods_type="no",
        )
        receiving_ready = self._snapshot(code="MOV-WRITE-PATH-RECEIVING", qty=5)
        receiving_ready.zone_code = "PR"
        receiving_ready.zone_kind = WarehouseLocation.ZONE_KIND_RECEIVING
        receiving_ready.warehouse_state_code = "placed_in_receiving"
        receiving_ready.save(
            update_fields=[
                "zone_code",
                "zone_kind",
                "warehouse_state_code",
                "updated_at",
            ]
        )
        wrong_suffix = self._snapshot(
            code="MOV-WRITE-PATH-vz",
            qty=5,
            normalize_gv=False,
        )
        pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="MOV-WRITE-PATH-PALLET-gv",
            current_location=self.general_location,
        )
        pallet_stock = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            stock_unit_type="pallet",
            source_context_type="receiving",
            source_context_id="MOV-WRITE-PATH-PALLET",
            sku_ref=self.sku,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode=self.barcode,
            goods_type="gv",
            qty=5,
            available_qty=5,
            container=pallet,
            container_code=pallet.container_code,
            location=self.general_location,
            zone_code="STORAGE",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            warehouse_state_code="stored",
        )
        loose_stock = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            stock_unit_type="item",
            source_context_type="receiving",
            source_context_id="MOV-WRITE-PATH-LOOSE",
            sku_ref=self.sku,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode=self.barcode,
            goods_type="gv",
            qty=5,
            available_qty=5,
            container=None,
            container_code="",
            location=self.general_location,
            zone_code="STORAGE",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            warehouse_state_code="stored",
        )

        for request_id, snapshot in enumerate(
            (nonready, receiving_ready, wrong_suffix, pallet_stock, loose_stock),
            start=99001,
        ):
            with self.subTest(snapshot=snapshot.container_code):
                with self.assertRaisesMessage(
                    WarehouseTransitionError,
                    "только готовый товар из активного GV-короба",
                ):
                    WarehouseWritePathService.reserve_for_fbs_movement(
                        agency=self.agency,
                        request_id=request_id,
                        allocations=[
                            {
                                "snapshot_id": snapshot.id,
                                "request_line_id": 1,
                                "qty": 5,
                                "container_id": snapshot.container_id,
                                "container_code": snapshot.container_code,
                                "barcode": snapshot.barcode,
                                "sku_id": snapshot.sku_ref_id,
                            }
                        ],
                        created_by=self.storekeeper,
                    )
                snapshot.refresh_from_db()
                self.assertEqual(
                    (snapshot.available_qty, snapshot.other_reserved_qty),
                    (5, 0),
                )

    def test_item_staging_and_packing_require_the_movement_container_qr(self):
        source = self._snapshot(code="MOV-STAGING-QR", qty=3)
        request_row = self._request(qty=2)
        plan = self._approve_and_accept(request_row).plans[0]
        claim_replenishment_plan(plan_id=plan.id, assigned_to=self.driver)
        allocation = FbsReplenishmentAllocation.objects.get(line__plan=plan)

        with self.assertRaisesMessage(FbsReplenishmentError, "временный короб"):
            stage_replenishment_allocation(
                allocation_id=allocation.id,
                source_scan=source.container_code,
                staging_container_scan="FBS-STAGE-MOV-WRONG",
                staging_scan=self.staging_location.location_code,
                performed_by=self.driver,
            )

        source.refresh_from_db()
        allocation.refresh_from_db()
        self.assertEqual((source.qty, source.other_reserved_qty), (3, 2))
        self.assertEqual(allocation.status, FbsReplenishmentAllocation.STATUS_RESERVED)

        stage_replenishment_allocation(
            allocation_id=allocation.id,
            source_scan=source.container_code,
            staging_container_scan=movement_staging_container_code(request_row),
            staging_scan=self.staging_location.location_code,
            performed_by=self.driver,
        )
        with self.assertRaisesMessage(FbsReplenishmentError, "временный короб"):
            pack_staged_item_plan(
                plan_id=plan.id,
                packed_by=self.storekeeper,
                staging_container_scan="FBS-STAGE-MOV-WRONG",
            )
        self.assertFalse(FbsStockBalance.objects.exists())

    def test_operator_can_print_movement_container_and_staging_zone_qr(self):
        self._snapshot(code="MOV-LABELS", qty=3)
        request_row = self._request(qty=2)
        self._approve_and_accept(request_row)
        self.client.force_login(self.storekeeper)

        detail = self.client.get(f"/fbs/operator/movements/{request_row.id}/")
        self.assertContains(detail, movement_staging_container_code(request_row))
        self.assertContains(detail, "Печатать QR короба")
        container_label = self.client.get(
            f"/fbs/operator/movements/{request_row.id}/labels/?kind=container"
        )
        self.assertContains(container_label, movement_staging_container_code(request_row))
        self.assertContains(container_label, "Временный короб")
        zone_label = self.client.get(
            f"/fbs/operator/movements/{request_row.id}/labels/?kind=zone"
        )
        self.assertContains(zone_label, self.staging_location.location_code)
        self.assertContains(zone_label, "Зона подготовки FBS")

    def test_request_without_fbs_pallet_waits_for_driver_scan_before_os_reservation(self):
        self.pallet.status = FbsPallet.STATUS_ARCHIVED
        self.pallet.save(update_fields=["status", "updated_at"])
        self.item_target_box.status = FbsBox.STATUS_ARCHIVED
        self.item_target_box.save(update_fields=["status", "updated_at"])
        WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="MOV-OCCUPIED-STANDALONE-BOX",
            current_location=self.free_os_location,
        )
        existing_free_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=41,
            section_no=2,
            tier_no=1,
            cell_no=1,
            location_code="OS-41-2-1-1",
            display_name="OS existing free cell outside static topology",
            is_storage=True,
        )
        location_ids_before = set(
            WarehouseLocation.objects.values_list("id", flat=True)
        )
        self._snapshot(code="MOV-SOURCE-SHARED-OS", qty=10)
        request_row = self._request(qty=4)

        result = self._approve_and_accept(request_row)

        target_pallet = result.plans[0].target_pallet
        target_pallet.refresh_from_db()
        location = target_pallet.cell.location
        container = target_pallet.warehouse_container
        self.assertNotEqual(location.id, existing_free_location.id)
        self.assertEqual(location.zone_code, "FBS")
        self.assertEqual(location.zone_kind, WarehouseLocation.ZONE_KIND_VIRTUAL)
        self.assertIsNone(container.current_location_id)
        self.assertEqual(container.source_context_type, FBS_STORAGE_CONTEXT_TYPE)
        self.assertEqual(
            set(WarehouseLocation.objects.values_list("id", flat=True)) - location_ids_before,
            {location.id},
        )
        self.assertNotIn(
            (
                existing_free_location.row_no,
                existing_free_location.section_no,
                existing_free_location.tier_no,
                existing_free_location.cell_no,
            ),
            shared_os_occupied_keys(),
        )
        # Compatibility: old accepted plans may still point at a physical OS cell.
        legacy_cell = FbsStorageCell.objects.create(
            cell_code="FBS@LEGACY-PLAN-RESERVATION",
            location=existing_free_location,
            client_cluster=self.agency.id,
        )
        target_pallet.cell = legacy_cell
        target_pallet.save(update_fields=["cell", "updated_at"])
        container.current_location = existing_free_location
        container.save(update_fields=["current_location", "updated_at"])
        for plan in result.plans:
            plan.target_cell = legacy_cell
            plan.save(update_fields=["target_cell", "updated_at"])
            operation = plan.warehouse_operation
            operation.destination_location = existing_free_location
            operation.destination_zone_code = "OS"
            operation.save(
                update_fields=[
                    "destination_location",
                    "destination_zone_code",
                    "updated_at",
                ]
            )
            operation.tasks.update(
                to_location=existing_free_location,
                to_zone_code="OS",
            )
        self.assertNotIn(
            (
                existing_free_location.row_no,
                existing_free_location.section_no,
                existing_free_location.tier_no,
                existing_free_location.cell_no,
            ),
            shared_os_occupied_keys(),
        )
        from .services.storage import release_unplaced_os_reservation

        self.assertTrue(release_unplaced_os_reservation(pallet=target_pallet))
        target_pallet.refresh_from_db()
        container.refresh_from_db()
        self.assertEqual(target_pallet.cell.location.zone_code, "FBS")
        self.assertEqual(
            target_pallet.cell.location.zone_kind,
            WarehouseLocation.ZONE_KIND_VIRTUAL,
        )
        self.assertIsNone(container.current_location_id)
        for plan in result.plans:
            plan.refresh_from_db()
            operation = plan.warehouse_operation
            operation.refresh_from_db()
            self.assertEqual(plan.target_cell_id, target_pallet.cell_id)
            self.assertEqual(
                operation.destination_location_id,
                target_pallet.cell.location_id,
            )
            self.assertFalse(
                operation.tasks.exclude(
                    to_location_id=target_pallet.cell.location_id,
                    to_zone_code="FBS",
                ).exists()
            )

    def test_box_request_selects_only_exact_full_boxes_and_moves_each_once(self):
        first = self._snapshot(code="MOV-WHOLE-1", qty=5)
        second = self._snapshot(code="MOV-WHOLE-2", qty=5)
        request_row = self._request(mode="box", qty=10, units_per_box=5)

        result = self._approve_and_accept(request_row)

        self.assertEqual(len(result.plans), 2)
        self.assertEqual(
            MoveRequest.objects.filter(context_id=f"fbs-movement:{request_row.id}").count(),
            1,
        )
        self.assertEqual(
            MoveTask.objects.filter(payload__fbs_movement_id=request_row.id).count(),
            2,
        )
        self.assertEqual(
            set(result.request.replenishment_plans.values_list("target_box__box_code", flat=True)),
            {"MOV-WHOLE-1-gv", "MOV-WHOLE-2-gv"},
        )
        self.assertEqual(
            set(
                result.request.replenishment_plans.values_list(
                    "target_box__source_container__container_code", flat=True
                )
            ),
            {"MOV-WHOLE-1-gv", "MOV-WHOLE-2-gv"},
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.qty, first.available_qty, first.other_reserved_qty), (5, 0, 5))
        self.assertEqual((second.qty, second.available_qty, second.other_reserved_qty), (5, 0, 5))
        self.assertFalse(FbsStockBalance.objects.exists())

        for plan in result.plans:
            claim_replenishment_plan(plan_id=plan.id, assigned_to=self.driver)
            for allocation in FbsReplenishmentAllocation.objects.filter(line__plan=plan):
                complete_replenishment_allocation(
                    allocation_id=allocation.id,
                    source_scan=allocation.source_snapshot.container_code,
                    target_box_scan=os_location_code(row=3, section=2, tier=1, cell=1),
                    performed_by=self.driver,
                )

        request_row.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(request_row.status, FbsClientMovementRequest.STATUS_MOVED)
        self.assertEqual(first.qty, 0)
        self.assertEqual(second.qty, 0)
        self.assertEqual(
            sum(FbsStockBalance.objects.values_list("qty", flat=True)),
            10,
        )
        self.assertEqual(FbsStockMovement.objects.count(), 2)
        placed_boxes = list(
            FbsBox.objects.filter(box_code__in=("MOV-WHOLE-1-gv", "MOV-WHOLE-2-gv"))
            .select_related("pallet__cell")
            .order_by("box_code")
        )
        self.assertEqual(
            {box.pallet.cell.location_id for box in placed_boxes},
            {self.free_os_location.id},
        )
        self.assertEqual(len({box.pallet_id for box in placed_boxes}), 1)
        confirmed = confirm_client_movement_by_warehouse(
            request_id=request_row.id,
            confirmed_by=self.storekeeper,
        )
        self.assertEqual(
            confirmed.status,
            FbsClientMovementRequest.STATUS_COMPLETED,
        )

    def test_whole_box_completion_rolls_back_if_stock_would_remain_in_fbo_and_fbs(self):
        source = self._snapshot(code="MOV-SINGLE-CONTOUR", qty=5)
        request_row = self._request(mode="box", qty=5, units_per_box=5)
        result = self._approve_and_accept(request_row)
        plan = result.plans[0]
        claim_replenishment_plan(plan_id=plan.id, assigned_to=self.driver)
        allocation = FbsReplenishmentAllocation.objects.get(line__plan=plan)
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            stock_unit_type="box",
            source_context_type="receiving",
            source_context_id="MOV-LATE-SAME-BOX",
            sku_ref=self.sku,
            sku_code="MOV-LATE-SKU",
            name="Поздний остаток в том же коробе",
            barcode="4600000009999",
            goods_type="gv",
            qty=1,
            available_qty=1,
            container=source.container,
            container_code=source.container_code,
            location=self.general_location,
            zone_code="STORAGE",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            warehouse_state_code="stored",
        )

        with self.assertRaisesRegex(
            FbsReplenishmentError,
            "одновременно получит остаток FBS и основного склада",
        ):
            complete_replenishment_allocation(
                allocation_id=allocation.id,
                source_scan=source.container_code,
                target_box_scan=os_location_code(row=3, section=2, tier=1, cell=1),
                performed_by=self.driver,
            )

        source.refresh_from_db()
        allocation.refresh_from_db()
        self.assertEqual((source.qty, source.other_reserved_qty), (5, 5))
        self.assertEqual(
            allocation.status,
            FbsReplenishmentAllocation.STATUS_RESERVED,
        )
        self.assertFalse(FbsStockBalance.objects.exists())
        self.assertFalse(FbsStockMovement.objects.exists())

    @override_settings(FBS_CLIENT_FULL_PALLET_LOGICAL_POST_ON_ACCEPT=True)
    def test_full_pallet_is_posted_to_fbs_without_driver_or_physical_move(self):
        first = self._snapshot(code="MOV-LOGICAL-PALLET-1", qty=5)
        second = self._snapshot(code="MOV-LOGICAL-PALLET-2", qty=5)
        first_box_id = first.container_id
        second_box_id = second.container_id
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[
                {
                    "barcode": self.barcode,
                    "qty": 10,
                    "units_per_box": 5,
                    "box_count": 2,
                }
            ],
            idempotency_key="movement-logical-full-pallet",
        )
        approve_client_movement_by_manager(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )

        result = self._approve_and_accept(request_row)

        request_row.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        first_box = WarehouseContainer.objects.get(pk=first_box_id)
        second_box = WarehouseContainer.objects.get(pk=second_box_id)
        self.assertEqual(
            request_row.status,
            FbsClientMovementRequest.STATUS_COMPLETED,
            repr(
                {
                    "uses_hard_reserve": request_row.uses_hard_reserve,
                    "idempotency_key": request_row.idempotency_key,
                    "plans": list(
                        request_row.replenishment_plans.order_by("id").values(
                            "id",
                            "mode",
                            "comment",
                            "lines__source_container__parent_container_id",
                        )
                    ),
                }
            ),
        )
        self.assertEqual(request_row.actual_moved_qty, 10)
        self.assertEqual(request_row.actual_moved_box_count, 2)
        self.assertIsNotNone(request_row.billing_synced_at)
        self.assertEqual(
            (first.qty, first.available_qty, first.other_reserved_qty, first.is_archived),
            (0, 0, 0, True),
        )
        self.assertEqual(
            (second.qty, second.available_qty, second.other_reserved_qty, second.is_archived),
            (0, 0, 0, True),
        )
        self.assertEqual(first_box.current_location_id, self.general_location.id)
        self.assertEqual(second_box.current_location_id, self.general_location.id)
        self.assertEqual(first_box.parent_container_id, self.source_pallet.id)
        self.assertEqual(second_box.parent_container_id, self.source_pallet.id)
        self.assertEqual(
            FbsStockBalance.objects.aggregate(total=Sum("qty"))["total"],
            10,
        )
        self.assertEqual(FbsStockMovement.objects.count(), 2)
        self.assertFalse(
            FbsReplenishmentPlan.objects.filter(
                client_movement_request=request_row,
            ).exclude(status=FbsReplenishmentPlan.STATUS_DONE).exists()
        )
        # Маршрут ричтрака больше не строится вовсе: решение о логической проводке
        # принимается до того, как задания создаются. Раньше здесь было два задания,
        # рождённых и погашенных в одной транзакции.
        tasks = MoveTask.objects.filter(payload__fbs_movement_id=request_row.id)
        self.assertFalse(tasks.exists())
        self.assertFalse(
            MoveRequest.objects.filter(
                context_id=f"fbs-movement:{request_row.id}"
            ).exists()
        )
        self.assertEqual(
            WarehouseEvent.objects.filter(
                event_type="fbs_replenishment_completed",
                payload__physical_place_preserved=True,
            ).count(),
            2,
        )

        repeated = accept_client_movement_request(
            request_id=request_row.id,
            accepted_by=self.storekeeper,
        )
        self.assertEqual(repeated.request.status, FbsClientMovementRequest.STATUS_COMPLETED)
        self.assertEqual(FbsStockMovement.objects.count(), 2)
        self.assertEqual(
            FbsStockBalance.objects.aggregate(total=Sum("qty"))["total"],
            10,
        )

    @override_settings(FBS_CLIENT_FULL_PALLET_LOGICAL_POST_ON_ACCEPT=True)
    def test_full_pallet_never_creates_a_driver_route(self):
        """Полная паллета: заданий не появляется даже на мгновение.

        Раньше задания создавались при подтверждении планов и гасились несколькими
        строками ниже, в той же транзакции: 909 таких отмен за 30 дней на проде.
        """
        self._snapshot(code="MOV-DEFER-PALLET-1", qty=5)
        self._snapshot(code="MOV-DEFER-PALLET-2", qty=5)
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[
                {"barcode": self.barcode, "qty": 10, "units_per_box": 5, "box_count": 2}
            ],
            idempotency_key="movement-defer-full-pallet",
        )
        approve_client_movement_by_manager(
            request_id=request_row.id, reviewed_by=self.manager
        )

        self._approve_and_accept(request_row)

        request_row.refresh_from_db()
        self.assertEqual(
            request_row.status, FbsClientMovementRequest.STATUS_COMPLETED
        )
        self.assertFalse(
            MoveTask.objects.filter(payload__fbs_movement_id=request_row.id).exists(),
            "полная паллета не должна порождать ни одного задания ричтрака",
        )
        self.assertFalse(
            MoveRequest.objects.filter(
                context_id=f"fbs-movement:{request_row.id}"
            ).exists(),
            "заявка ричтрака не должна создаваться под логическую проводку",
        )

    @override_settings(FBS_CLIENT_FULL_PALLET_LOGICAL_POST_ON_ACCEPT=True)
    def test_partial_pallet_still_gets_its_driver_route(self):
        """Контроль на «отсрочка сработала слишком широко»."""
        self._snapshot(code="MOV-DEFER-BOX-TAKEN", qty=5)
        self._snapshot(code="MOV-DEFER-BOX-LEFT", qty=5)
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[
                {"barcode": self.barcode, "qty": 5, "units_per_box": 5, "box_count": 1}
            ],
            idempotency_key="movement-defer-partial-box",
        )
        approve_client_movement_by_manager(
            request_id=request_row.id, reviewed_by=self.manager
        )

        self._approve_and_accept(request_row)

        request_row.refresh_from_db()
        tasks = MoveTask.objects.filter(payload__fbs_movement_id=request_row.id)
        self.assertTrue(tasks.exists(), "неполная паллета обязана ехать ричтраком")
        self.assertFalse(
            tasks.exclude(status=MoveTask.STATUS_CREATED).exists(),
            "задания должны быть свежими, а не отменёнными",
        )
        self.assertTrue(
            MoveRequest.objects.filter(
                context_id=f"fbs-movement:{request_row.id}"
            ).exists()
        )

    @override_settings(FBS_CLIENT_FULL_PALLET_LOGICAL_POST_ON_ACCEPT=True)
    def test_request_status_is_recomputed_once_not_once_per_plan(self):
        """Статус заявки ричтрака пересчитывается один раз на приёмку."""
        self._snapshot(code="MOV-DEFER-ONCE-TAKEN", qty=5)
        self._snapshot(code="MOV-DEFER-ONCE-LEFT", qty=5)
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[
                {"barcode": self.barcode, "qty": 5, "units_per_box": 5, "box_count": 1}
            ],
            idempotency_key="movement-defer-sync-once",
        )
        approve_client_movement_by_manager(
            request_id=request_row.id, reviewed_by=self.manager
        )

        from fbs.services import reachtruck_bridge

        with patch.object(
            reachtruck_bridge,
            "sync_client_movement_reachtruck_request",
            wraps=reachtruck_bridge.sync_client_movement_reachtruck_request,
        ) as synced:
            self._approve_and_accept(request_row)

        self.assertEqual(synced.call_count, 1)

    @override_settings(FBS_CLIENT_FULL_PALLET_LOGICAL_POST_ON_ACCEPT=True)
    def test_confirm_plan_without_the_flag_still_builds_the_route(self):
        """Защита четвёртого вызова из tsd_views.py, который параметр не передаёт."""
        from fbs.services.replenishment import confirm_replenishment_plan

        self._snapshot(code="MOV-DEFER-DIRECT-TAKEN", qty=5)
        self._snapshot(code="MOV-DEFER-DIRECT-LEFT", qty=5)
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[
                {"barcode": self.barcode, "qty": 5, "units_per_box": 5, "box_count": 1}
            ],
            idempotency_key="movement-defer-direct-confirm",
        )
        approve_client_movement_by_manager(
            request_id=request_row.id, reviewed_by=self.manager
        )
        self._approve_and_accept(request_row)
        plan = FbsReplenishmentPlan.objects.filter(
            client_movement_request=request_row
        ).exclude(status=FbsReplenishmentPlan.STATUS_DONE).order_by("id").first()
        self.assertIsNotNone(plan)
        MoveTask.objects.filter(payload__fbs_movement_id=request_row.id).delete()

        confirm_replenishment_plan(plan_id=plan.id, confirmed_by=self.storekeeper)

        self.assertTrue(
            MoveTask.objects.filter(payload__fbs_movement_id=request_row.id).exists(),
            "без нового параметра маршрут обязан строиться как раньше",
        )

    @override_settings(FBS_CLIENT_FULL_PALLET_LOGICAL_POST_ON_ACCEPT=True)
    def test_separate_box_movement_still_requires_reachtruck_driver(self):
        selected = self._snapshot(code="MOV-SEPARATE-BOX", qty=5)
        self._snapshot(code="MOV-BOX-LEFT-ON-PALLET", qty=5)
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[
                {
                    "barcode": self.barcode,
                    "qty": 5,
                    "units_per_box": 5,
                    "box_count": 1,
                }
            ],
            idempotency_key="movement-logical-partial-box",
        )
        approve_client_movement_by_manager(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )

        result = self._approve_and_accept(request_row)

        request_row.refresh_from_db()
        selected.refresh_from_db()
        self.assertEqual(
            request_row.status,
            FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED,
        )
        self.assertEqual((selected.qty, selected.available_qty, selected.other_reserved_qty), (5, 0, 5))
        self.assertFalse(FbsStockBalance.objects.exists())
        self.assertEqual(len(result.plans), 1)
        task = MoveTask.objects.get(payload__fbs_movement_id=request_row.id)
        self.assertEqual(task.status, MoveTask.STATUS_CREATED)
        self.assertEqual(task.move_mode, MoveTask.MODE_BOX_FULL)

    def test_large_box_request_batches_lock_and_request_status_checks(self):
        box_count = 12
        for index in range(box_count):
            self._snapshot(code=f"MOV-BATCH-LOCK-{index}", qty=2)
        request_row = self._request(
            mode="box",
            qty=box_count * 2,
            units_per_box=2,
            box_count=box_count,
        )

        from .services.inventory import box_is_locked, validate_boxes_unlocked
        from .services.reachtruck_bridge import _sync_request

        with (
            patch(
                "fbs.services.client_movements.validate_boxes_unlocked",
                wraps=validate_boxes_unlocked,
            ) as validate_batch,
            patch(
                "fbs.services.inventory.box_is_locked",
                wraps=box_is_locked,
            ) as validate_single,
            patch(
                "fbs.services.reachtruck_bridge._sync_request",
                wraps=_sync_request,
            ) as sync_request,
        ):
            result = self._approve_and_accept(request_row)

        self.assertEqual(len(result.plans), box_count)
        self.assertEqual(validate_batch.call_count, 1)
        self.assertEqual(validate_single.call_count, 0)
        self.assertEqual(sync_request.call_count, 1)
        self.assertFalse(
            FbsReplenishmentPlan.objects.filter(
                client_movement_request=request_row,
            ).exclude(status=FbsReplenishmentPlan.STATUS_CONFIRMED).exists()
        )

    def test_batch_box_lock_validation_does_not_bypass_inventory_lock(self):
        session = FbsInventorySession.objects.create(
            scope_type=FbsInventorySession.SCOPE_BOX,
            mode=FbsInventorySession.MODE_IMMEDIATE,
            status=FbsInventorySession.STATUS_COUNTING,
            agency=self.agency,
            box=self.item_target_box,
        )
        FbsStorageLock.objects.create(
            session=session,
            scope_type=FbsInventorySession.SCOPE_BOX,
            agency=self.agency,
            box=self.item_target_box,
            block_new_reservations=True,
            block_execution=True,
        )

        from .services.inventory import validate_boxes_unlocked

        with self.assertRaises(FbsInventoryError):
            validate_boxes_unlocked([self.item_target_box.id])

    def test_box_request_falls_back_to_available_stock_when_exact_box_count_is_missing(self):
        first = self._snapshot(code="MOV-ONLY-EXACT", qty=5)
        wrong = self._snapshot(code="MOV-MIXED-BOX", qty=5)
        other_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="MOV-SKU-OTHER",
            name="Незаявленный товар",
        )
        other_barcode = "4600000009999"
        SKUBarcode.objects.create(sku=other_sku, value=other_barcode, is_primary=True)
        request_row = self._request(mode="box", qty=10, units_per_box=5)
        extra = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            stock_unit_type="box",
            source_context_type="receiving",
            source_context_id="MOV-MIXED-EXTRA",
            sku_ref=other_sku,
            sku_code=other_sku.sku_code,
            name=other_sku.name,
            barcode=other_barcode,
            qty=1,
            available_qty=1,
            container=wrong.container,
            container_code=wrong.container_code,
            location=self.general_location,
            zone_code="STORAGE",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            warehouse_state_code="stored",
        )
        approve_client_movement_by_manager(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )

        result = accept_client_movement_request(
            request_id=request_row.id,
            accepted_by=self.storekeeper,
        )

        request_row.refresh_from_db()
        first.refresh_from_db()
        wrong.refresh_from_db()
        self.assertEqual(
            request_row.status,
            FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED,
        )
        self.assertEqual(len(result.plans), 1)
        plan = result.plans[0]
        self.assertEqual(plan.mode, FbsReplenishmentPlan.MODE_ITEM)
        self.assertIsNone(plan.target_box_id)
        self.assertEqual(plan.planned_qty, 10)
        self.assertEqual(
            FbsReplenishmentPreparedBox.objects.filter(plan=plan).count(),
            2,
        )
        self.assertEqual(
            MoveTask.objects.filter(payload__fbs_movement_id=request_row.id).count(),
            1,
        )
        self.assertEqual((first.qty, first.available_qty, first.other_reserved_qty), (5, 0, 5))
        self.assertEqual((wrong.qty, wrong.available_qty, wrong.other_reserved_qty), (5, 0, 5))
        extra.refresh_from_db()
        self.assertEqual((extra.qty, extra.available_qty, extra.other_reserved_qty), (1, 1, 0))
        self.assertEqual(FbsReplenishmentPlan.objects.count(), 1)
        self.assertEqual(WarehouseOperation.objects.count(), 1)
        self.assertEqual(WarehouseReserve.objects.count(), 2)
        self.assertEqual(FbsBox.objects.count(), 1)

    def test_canceling_linked_plan_releases_stock_and_cancels_request(self):
        source = self._snapshot(code="MOV-CANCEL", qty=7)
        request_row = self._request(qty=4)
        plan = self._approve_and_accept(request_row).plans[0]

        canceled = cancel_replenishment_plan(
            plan_id=plan.id,
            canceled_by=self.storekeeper,
        )

        source.refresh_from_db()
        request_row.refresh_from_db()
        self.assertEqual(canceled.status, FbsReplenishmentPlan.STATUS_CANCELED)
        self.assertEqual(request_row.status, FbsClientMovementRequest.STATUS_CANCELED)
        self.assertEqual((source.qty, source.available_qty, source.other_reserved_qty), (7, 7, 0))
        self.assertFalse(FbsStockBalance.objects.exists())

    def test_reject_leaves_warehouse_untouched(self):
        source = self._snapshot(code="MOV-REJECT", qty=6)
        request_row = self._request(qty=4)
        request_row.status = FbsClientMovementRequest.STATUS_SUBMITTED
        request_row.save(update_fields=["status", "updated_at"])

        rejected = reject_client_movement_request(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )

        source.refresh_from_db()
        self.assertEqual(rejected.status, FbsClientMovementRequest.STATUS_REJECTED)
        self.assertEqual((source.qty, source.available_qty, source.other_reserved_qty), (6, 6, 0))
        self.assertFalse(WarehouseOperation.objects.exists())
        self.assertFalse(WarehouseReserve.objects.exists())

    def test_operator_pages_and_approval_are_desktop_and_role_protected(self):
        self._snapshot(code="MOV-UI", qty=8)
        request_row = self._request(qty=4)
        approve_client_movement_by_manager(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )
        self.client.force_login(self.storekeeper)

        listing = self.client.get("/fbs/operator/movements/")
        detail = self.client.get(f"/fbs/operator/movements/{request_row.id}/")
        approved = self.client.post(
            f"/fbs/operator/movements/{request_row.id}/approve/",
            {"prepared_box_count": "1"},
        )

        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, request_row.number)
        self.assertContains(listing, "tsd-page-desktop")
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Принять и передать ричтраку")
        self.assertEqual(approved.status_code, 302)
        request_row.refresh_from_db()
        self.assertEqual(
            request_row.status,
            FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED,
        )

        self.client.force_login(self.manager)
        self.assertEqual(self.client.get("/fbs/operator/movements/").status_code, 403)

    def test_storekeeper_accepts_client_request_without_manager_approval(self):
        self._snapshot(code="MOV-ROLE-GUARD", qty=2)
        request_row = self._request(qty=2)

        result = accept_client_movement_request(
            request_id=request_row.id,
            accepted_by=self.storekeeper,
        )

        request_row.refresh_from_db()
        self.assertEqual(len(result.plans), 1)
        self.assertEqual(
            request_row.status,
            FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED,
        )
        self.assertIsNone(request_row.reviewed_by_id)
        self.assertIsNone(request_row.reviewed_at)

    def test_storekeeper_can_select_another_item_box_after_reachtruck_staging(self):
        second_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=41,
            section_no=2,
            tier_no=1,
            cell_no=1,
            location_code="MOV-FBS-2",
            is_storage=True,
        )
        second_cell = FbsStorageCell.objects.create(
            cell_code="MOV-FBS-2",
            location=second_location,
        )
        second_pallet = FbsPallet.objects.create(
            agency=self.agency,
            cell=second_cell,
            pallet_code="MOV-PALLET-2",
            max_boxes=10,
        )
        second_box = FbsBox.objects.create(
            agency=self.agency,
            pallet=second_pallet,
            box_code="MOV-PICK-BOX-2",
        )
        source = self._snapshot(code="MOV-MANUAL-TARGET", qty=5)
        request_row = self._request(qty=3)
        plan = self._approve_and_accept(request_row).plans[0]
        claim_replenishment_plan(plan_id=plan.id, assigned_to=self.driver)
        allocation = FbsReplenishmentAllocation.objects.get(line__plan=plan)

        stage_replenishment_allocation(
            allocation_id=allocation.id,
            source_scan=source.container_code,
            staging_container_scan=movement_staging_container_code(request_row),
            staging_scan=self.staging_location.location_code,
            performed_by=self.driver,
        )
        with self.assertRaisesMessage(FbsReplenishmentError, "заранее не выбираются"):
            pack_staged_item_plan(
                plan_id=plan.id,
                staging_container_scan=movement_staging_container_code(request_row),
                target_box_id=second_box.id,
                packed_by=self.storekeeper,
            )

        plan.refresh_from_db()
        self.assertIsNone(plan.target_box_id)
        self.assertEqual(plan.status, FbsReplenishmentPlan.STATUS_AWAITING_PACK)
        self.assertFalse(FbsStockBalance.objects.exists())

    def test_storekeeper_can_create_new_box_for_staged_items(self):
        source = self._snapshot(code="MOV-NEW-BOX", qty=4)
        request_row = self._request(qty=2)
        plan = self._approve_and_accept(request_row).plans[0]
        claim_replenishment_plan(plan_id=plan.id, assigned_to=self.driver)
        allocation = FbsReplenishmentAllocation.objects.get(line__plan=plan)
        stage_replenishment_allocation(
            allocation_id=allocation.id,
            source_scan=source.container_code,
            staging_container_scan=movement_staging_container_code(request_row),
            staging_scan=self.staging_location.location_code,
            performed_by=self.driver,
        )

        target_box = pack_staged_item_plan(
            plan_id=plan.id,
            staging_container_scan=movement_staging_container_code(request_row),
            packed_by=self.storekeeper,
        )

        self.assertTrue(target_box.box_code.startswith(f"FBS-BOX-{self.agency.id}-"))
        self.assertIsNotNone(target_box.source_container_id)
        self.assertEqual(target_box.source_container.current_location_id, self.staging_location.id)
        self.assertFalse(FbsStockBalance.objects.exists())
        self.assertTrue(
            MoveTask.objects.filter(
                legacy_order_id=f"FBS-RPL-PLACE-P{plan.id}",
                status=MoveTask.STATUS_CREATED,
            ).exists()
        )

    def test_driver_cannot_place_fbs_box_in_occupied_os_cell(self):
        source = self._snapshot(code="MOV-OCCUPIED-DESTINATION", qty=4)
        request_row = self._request(qty=2)
        plan = self._approve_and_accept(request_row).plans[0]
        claim_replenishment_plan(plan_id=plan.id, assigned_to=self.driver)
        allocation = FbsReplenishmentAllocation.objects.get(line__plan=plan)
        stage_replenishment_allocation(
            allocation_id=allocation.id,
            source_scan=source.container_code,
            staging_container_scan=movement_staging_container_code(request_row),
            staging_scan=self.staging_location.location_code,
            performed_by=self.driver,
        )
        target_box = pack_staged_item_plan(
            plan_id=plan.id,
            staging_container_scan=movement_staging_container_code(request_row),
            packed_by=self.storekeeper,
        )
        WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="MOV-OCCUPIED-STANDALONE-BOX",
            current_location=self.free_os_location,
        )

        with self.assertRaisesMessage(FbsReplenishmentError, "недоступна"):
            complete_staged_item_plan_placement(
                plan_id=plan.id,
                target_box_scan=target_box.box_code,
                destination_scan="A-3/1-1",
                performed_by=self.driver,
            )

        plan.refresh_from_db()
        allocation.refresh_from_db()
        self.assertEqual(plan.status, FbsReplenishmentPlan.STATUS_IN_PROGRESS)
        self.assertEqual(allocation.status, FbsReplenishmentAllocation.STATUS_STAGED)
        self.assertFalse(FbsStockBalance.objects.exists())

    def test_legacy_completed_box_can_be_requeued_without_duplicate_balance(self):
        source = self._snapshot(code="MOV-LEGACY-PLACEMENT", qty=4)
        request_row = self._request(qty=2)
        plan = self._approve_and_accept(request_row).plans[0]
        claim_replenishment_plan(plan_id=plan.id, assigned_to=self.driver)
        allocation = FbsReplenishmentAllocation.objects.get(line__plan=plan)
        stage_replenishment_allocation(
            allocation_id=allocation.id,
            source_scan=source.container_code,
            staging_container_scan=movement_staging_container_code(request_row),
            staging_scan=self.staging_location.location_code,
            performed_by=self.driver,
        )
        target_box = pack_staged_item_plan(
            plan_id=plan.id,
            staging_container_scan=movement_staging_container_code(request_row),
            packed_by=self.storekeeper,
        )
        complete_staged_item_plan_placement(
            plan_id=plan.id,
            target_box_scan=target_box.box_code,
            destination_scan="A-3/1-1",
            performed_by=self.driver,
        )
        MoveTask.objects.filter(legacy_order_id=f"FBS-RPL-PLACE-P{plan.id}").delete()
        physical_box = target_box.source_container
        WarehouseEvent.objects.filter(container=physical_box).update(container=None)
        target_box.source_container = None
        target_box.save(update_fields=["source_container", "updated_at"])
        physical_box.delete()
        second_free_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=3,
            section_no=2,
            tier_no=1,
            cell_no=2,
            location_code="OS-3-2-1-2",
            is_storage=True,
        )
        movement_count = FbsStockMovement.objects.count()

        reopened = reopen_completed_item_plan_for_placement(
            plan_id=plan.id,
            reopened_by=self.storekeeper,
        )
        self.assertEqual(reopened.status, FbsReplenishmentPlan.STATUS_IN_PROGRESS)
        self.assertTrue(
            MoveTask.objects.filter(
                legacy_order_id=f"FBS-RPL-PLACE-P{plan.id}",
                status=MoveTask.STATUS_CREATED,
            ).exists()
        )
        complete_staged_item_plan_placement(
            plan_id=plan.id,
            target_box_scan=target_box.box_code,
            destination_scan="A-3/1-2",
            performed_by=self.driver,
        )

        plan.refresh_from_db()
        self.assertEqual(plan.status, FbsReplenishmentPlan.STATUS_DONE)
        self.assertEqual(plan.target_cell.location_id, second_free_location.id)
        self.assertEqual(FbsStockBalance.objects.get(box=target_box).qty, 2)
        self.assertEqual(FbsStockMovement.objects.count(), movement_count)

    def test_staged_item_plan_cannot_be_canceled_or_packed_by_manager(self):
        source = self._snapshot(code="MOV-STAGED-GUARDS", qty=3)
        request_row = self._request(qty=2)
        plan = self._approve_and_accept(request_row).plans[0]
        claim_replenishment_plan(plan_id=plan.id, assigned_to=self.driver)
        allocation = FbsReplenishmentAllocation.objects.get(line__plan=plan)
        stage_replenishment_allocation(
            allocation_id=allocation.id,
            source_scan=source.container_code,
            staging_container_scan=movement_staging_container_code(request_row),
            staging_scan=self.staging_location.location_code,
            performed_by=self.driver,
        )

        with self.assertRaisesMessage(FbsReplenishmentError, "уже передан кладовщику"):
            cancel_replenishment_plan(plan_id=plan.id, canceled_by=self.storekeeper)
        with self.assertRaisesMessage(FbsReplenishmentError, "только кладовщик"):
            pack_staged_item_plan(
                plan_id=plan.id,
                staging_container_scan=movement_staging_container_code(request_row),
                packed_by=self.manager,
            )

        plan.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(plan.status, FbsReplenishmentPlan.STATUS_AWAITING_PACK)
        self.assertEqual((source.qty, source.available_qty, source.other_reserved_qty), (1, 1, 0))
        self.assertFalse(FbsStockBalance.objects.exists())

    def test_tsd_staging_and_operator_packing_buttons_follow_two_step_flow(self):
        source = self._snapshot(code="MOV-TSD-STAGE", qty=4)
        request_row = self._request(qty=2)
        plan = self._approve_and_accept(request_row).plans[0]
        claim_replenishment_plan(plan_id=plan.id, assigned_to=self.driver)
        allocation = FbsReplenishmentAllocation.objects.get(line__plan=plan)

        self.client.force_login(self.driver)
        allocation_page = self.client.get(
            f"/fbs/tsd/reachtruck/allocations/{allocation.id}/"
        )
        self.assertContains(allocation_page, "MOV-FBS-PREP-1")
        source_scan = self.client.post(
            f"/fbs/tsd/reachtruck/allocations/{allocation.id}/",
            {"action": "scan_source", "source_scan": source.container_code},
        )
        self.assertEqual(source_scan.status_code, 200)
        self.assertContains(source_scan, movement_staging_container_code(request_row))
        self.assertContains(source_scan, "QR зоны подготовки FBS")
        self.assertContains(source_scan, "Передать кладовщику")
        staged = self.client.post(
            f"/fbs/tsd/reachtruck/allocations/{allocation.id}/",
            {
                "action": "complete",
                "staging_container_scan": movement_staging_container_code(request_row),
                "staging_scan": self.staging_location.location_code,
            },
        )
        self.assertEqual(staged.status_code, 302)

        self.client.force_login(self.storekeeper)
        detail = self.client.get(f"/fbs/operator/movements/{request_row.id}/")
        self.assertContains(detail, "Создать короб по плану")
        self.assertContains(detail, "Создать QR нового короба")
        packed = self.client.post(
            f"/fbs/operator/movements/{request_row.id}/pack/",
            {
                "plan_id": plan.id,
                "staging_container_scan": movement_staging_container_code(request_row),
            },
        )
        self.assertEqual(packed.status_code, 302)
        plan.refresh_from_db()
        self.assertEqual(plan.status, FbsReplenishmentPlan.STATUS_IN_PROGRESS)
        self.assertFalse(FbsStockBalance.objects.exists())
        prepared_detail = self.client.get(
            f"/fbs/operator/movements/{request_row.id}/"
        )
        self.assertContains(prepared_detail, plan.target_box.box_code)
        self.assertContains(prepared_detail, "Печатать QR FBS-короба")
        label = self.client.get(
            f"/fbs/operator/movements/{request_row.id}/labels/?kind=final_box"
        )
        self.assertContains(label, plan.target_box.box_code)
        self.assertContains(label, "Короб FBS")

        placement_task = MoveTask.objects.get(
            legacy_order_id=f"FBS-RPL-PLACE-P{plan.id}"
        )
        take_result = take_move_task(
            legacy_order_id=placement_task.legacy_order_id,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)
        box_scan = scan_move_task_step(
            legacy_order_id=placement_task.legacy_order_id,
            scan_value=plan.target_box.box_code,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(box_scan.ok, box_scan.error)
        self.assertFalse(box_scan.completed)
        destination_scan = scan_move_task_step(
            legacy_order_id=placement_task.legacy_order_id,
            scan_value="A-3/1-1",
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(destination_scan.ok, destination_scan.error)
        self.assertTrue(destination_scan.completed)
        plan.refresh_from_db()
        self.assertEqual(plan.status, FbsReplenishmentPlan.STATUS_DONE)
        self.assertEqual(plan.target_cell.location_id, self.free_os_location.id)
        self.assertTrue(FbsStockBalance.objects.filter(box=plan.target_box, qty=2).exists())

    def test_item_movement_runs_through_existing_reachtruck_scan_commands(self):
        source = self._snapshot(code="MOV-BRIDGE-ITEM", qty=5)
        request_row = self._request(qty=2)
        plan = self._approve_and_accept(request_row).plans[0]
        allocation = FbsReplenishmentAllocation.objects.get(line__plan=plan)
        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)

        active, _done = collect_moves(
            self.driver_employee.id,
            True,
            include_done=False,
            include_plans=False,
        )
        move = next(row for row in active if row["order_id"] == task.legacy_order_id)
        self.assertEqual(move["mobile_category"], "movement")
        self.assertEqual(move["mobile_request_key"], f"fbs:{request_row.number}")

        take_result = take_move_task(
            legacy_order_id=task.legacy_order_id,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok)
        scans = [
            self.source_pallet.container_code,
            source.container_code,
            self.barcode,
            self.barcode,
            movement_staging_container_code(request_row),
            self.staging_location.location_code,
        ]
        for index, scan in enumerate(scans):
            result = scan_move_task_step(
                legacy_order_id=task.legacy_order_id,
                scan_value=scan,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            )
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.completed, index == len(scans) - 1)

        task.refresh_from_db()
        allocation.refresh_from_db()
        source.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(task.status, MoveTask.STATUS_DONE)
        self.assertEqual(allocation.status, FbsReplenishmentAllocation.STATUS_STAGED)
        self.assertEqual(plan.status, FbsReplenishmentPlan.STATUS_AWAITING_PACK)
        self.assertEqual((source.qty, source.available_qty, source.other_reserved_qty), (3, 3, 0))
        self.assertFalse(FbsStockBalance.objects.exists())
        self.assertEqual(
            build_mobile_execution_snapshot(task.legacy_order_id)["current_step"],
            "done",
        )
        self.assertTrue(
            complete_move_task(
                legacy_order_id=task.legacy_order_id,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            ).ok
        )

    def test_item_movement_uses_final_printed_boxes_and_places_only_nonempty_boxes(self):
        source = self._snapshot(code="MOV-DIRECT-BOXES", qty=5)
        request_row = self._request(qty=4)
        approve_client_movement_by_manager(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )
        result = accept_client_movement_request(
            request_id=request_row.id,
            accepted_by=self.storekeeper,
            prepared_box_count=3,
        )
        plan = result.plans[0]
        allocation = FbsReplenishmentAllocation.objects.get(line__plan=plan)
        prepared_boxes = list(plan.prepared_boxes.order_by("sequence_no", "id"))
        self.assertEqual(len(prepared_boxes), 3)
        self.assertFalse(
            WarehouseContainer.objects.filter(
                agency=self.agency,
                source_context_type=FBS_STAGING_CONTEXT_TYPE,
            ).exists()
        )
        self.assertFalse(FbsBox.objects.filter(box_code__in=[box.box_code for box in prepared_boxes]).exists())
        self.assertFalse(FbsStockBalance.objects.exists())
        task = MoveTask.objects.get(payload__fbs_allocation_ids=[allocation.id])
        self.assertTrue(task.payload.get("fbs_prepared_boxes_v1"))
        self.assertEqual(
            task.payload.get("destination_scan_code"),
            self.staging_location.location_code,
        )
        take_result = take_move_task(
            legacy_order_id=task.legacy_order_id,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)
        scans = [
            self.source_pallet.container_code,
            source.container_code,
            self.staging_location.location_code,
            prepared_boxes[0].box_code,
            self.barcode,
            self.barcode,
            prepared_boxes[0].box_code,
            prepared_boxes[1].box_code,
            self.barcode,
            self.barcode,
        ]
        for scan in scans:
            scan_result = scan_move_task_step(
                legacy_order_id=task.legacy_order_id,
                scan_value=scan,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            )
            self.assertTrue(scan_result.ok, scan_result.error)

        task.refresh_from_db()
        allocation.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(task.status, MoveTask.STATUS_DONE)
        self.assertEqual(allocation.status, FbsReplenishmentAllocation.STATUS_STAGED)
        self.assertEqual((source.qty, source.available_qty, source.other_reserved_qty), (1, 1, 0))
        self.assertEqual(FbsReplenishmentPreparedBoxItem.objects.count(), 2)
        self.assertFalse(FbsStockBalance.objects.exists())

        close_task = MoveTask.objects.get(legacy_order_id=f"FBS-RPL-CLOSE-P{plan.id}")
        self.assertTrue(
            take_move_task(
                legacy_order_id=close_task.legacy_order_id,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            ).ok
        )
        close_result = scan_move_task_step(
            legacy_order_id=close_task.legacy_order_id,
            scan_value=prepared_boxes[1].box_code,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(close_result.ok, close_result.error)
        self.assertTrue(close_result.completed)
        prepared_boxes = list(plan.prepared_boxes.order_by("sequence_no", "id"))
        self.assertEqual(
            [box.status for box in prepared_boxes],
            [
                FbsReplenishmentPreparedBox.STATUS_CLOSED,
                FbsReplenishmentPreparedBox.STATUS_CLOSED,
                FbsReplenishmentPreparedBox.STATUS_UNUSED,
            ],
        )
        self.assertEqual(
            prepared_boxes[2].physical_container.status,
            WarehouseContainer.STATUS_ARCHIVED,
        )

        placement_tasks = list(
            MoveTask.objects.filter(legacy_order_id__startswith="FBS-RPL-PLACE-B").order_by("id")
        )
        self.assertEqual(len(placement_tasks), 2)
        for placement_task in placement_tasks:
            self.assertTrue(
                take_move_task(
                    legacy_order_id=placement_task.legacy_order_id,
                    user=self.driver,
                    employee_id=self.driver_employee.id,
                    employee_name=self.driver_employee.full_name,
                ).ok
            )
            box_code = placement_task.payload["source_box_code"]
            self.assertTrue(
                scan_move_task_step(
                    legacy_order_id=placement_task.legacy_order_id,
                    scan_value=box_code,
                    user=self.driver,
                    employee_id=self.driver_employee.id,
                    employee_name=self.driver_employee.full_name,
                ).ok
            )
            placement_result = scan_move_task_step(
                legacy_order_id=placement_task.legacy_order_id,
                scan_value="A-3/1-1",
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            )
            self.assertTrue(placement_result.ok, placement_result.error)
            self.assertTrue(placement_result.completed)

        plan.refresh_from_db()
        request_row.refresh_from_db()
        self.assertEqual(plan.status, FbsReplenishmentPlan.STATUS_DONE)
        self.assertEqual(plan.moved_qty, 4)
        self.assertEqual(request_row.status, FbsClientMovementRequest.STATUS_MOVED)
        self.assertEqual(FbsStockBalance.objects.aggregate(total=Sum("qty"))["total"], 4)
        placed_boxes = list(
            FbsReplenishmentPreparedBox.objects.filter(
                plan=plan,
                status=FbsReplenishmentPreparedBox.STATUS_PLACED,
            ).select_related("fbs_box__pallet__cell")
        )
        self.assertEqual(len(placed_boxes), 2)
        self.assertEqual(
            {box.fbs_box.pallet.cell.location_id for box in placed_boxes},
            {self.free_os_location.id},
        )
        self.assertEqual({box.fbs_box.pallet.agency_id for box in placed_boxes}, {self.agency.id})

    def test_operator_uses_box_count_requested_by_client_for_item_movement(self):
        self._snapshot(code="MOV-CLIENT-BOX-COUNT", qty=5)
        request_row = self._request(qty=4, box_count=2)
        approve_client_movement_by_manager(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )
        self.client.force_login(self.storekeeper)

        detail = self.client.get(f"/fbs/operator/movements/{request_row.id}/")
        accepted = self.client.post(
            f"/fbs/operator/movements/{request_row.id}/approve/",
            {"prepared_box_count": "2"},
        )

        self.assertContains(detail, "Клиент указал: 2")
        self.assertContains(detail, 'value="2"')
        self.assertEqual(accepted.status_code, 302)
        plan = FbsReplenishmentPlan.objects.get(client_movement_request=request_row)
        self.assertEqual(plan.prepared_boxes.count(), 2)
        self.assertEqual(
            plan.prepared_boxes.values("physical_container_id").distinct().count(),
            2,
        )

    def test_request_level_mixed_box_plan_prints_one_permanent_box(self):
        self._snapshot(code="MOV-MIX-FIRST", qty=2)
        second_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-MIX-2",
            name="Второй товар для микса",
        )
        second_barcode = "4600000000002"
        SKUBarcode.objects.create(
            sku=second_sku,
            value=second_barcode,
            is_primary=True,
        )
        source_box = self._source_box("MOV-MIX-SECOND")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            stock_unit_type="box",
            source_context_type="receiving",
            source_context_id="MOV-MIX-SECOND",
            sku_ref=second_sku,
            sku_code=second_sku.sku_code,
            name=second_sku.name,
            barcode=second_barcode,
            goods_type="gv",
            qty=2,
            available_qty=2,
            container=source_box,
            container_code=source_box.container_code,
            location=self.general_location,
            zone_code="STORAGE",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            warehouse_state_code="stored",
        )
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_ITEM,
            raw_lines=[
                {"barcode": self.barcode, "qty": 1},
                {"barcode": second_barcode, "qty": 1},
            ],
            requested_box_count=1,
            requested_mixed_box_count=1,
        )
        approve_client_movement_by_manager(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )
        self.client.force_login(self.storekeeper)

        detail = self.client.get(f"/fbs/operator/movements/{request_row.id}/")
        accepted = self.client.post(
            f"/fbs/operator/movements/{request_row.id}/approve/",
            {"prepared_box_count": "1"},
        )

        self.assertContains(detail, "1 физических коробов, из них микс: 1")
        self.assertEqual(accepted.status_code, 302)
        plan = FbsReplenishmentPlan.objects.get(client_movement_request=request_row)
        self.assertEqual(plan.prepared_boxes.count(), 1)

    def test_item_reachtruck_task_uses_product_barcode_not_marking_code(self):
        source = self._snapshot(code="MOV-BRIDGE-BARCODE", qty=4)
        source.marking_code = "010460000009991721MARKING-CODE"
        source.save(update_fields=["marking_code", "updated_at"])
        request_row = self._request(qty=2)

        plan = self._approve_and_accept(request_row).plans[0]
        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)

        self.assertEqual(task.payload["fbs_unit_scan_code"], self.barcode)
        self.assertNotEqual(task.payload["fbs_unit_scan_code"], source.marking_code)

    def test_new_marked_item_task_requires_product_barcode_then_exact_chz(self):
        self.sku.honest_sign = True
        self.sku.save(update_fields=["honest_sign"])
        source = self._snapshot(code="MOV-BRIDGE-CHZ", qty=2)
        marking_code = self._marking_code("MOVEMENT-UNIT-1")
        source.marking_code = marking_code
        source.save(update_fields=["marking_code", "updated_at"])
        request_row = self._request(qty=1)
        approve_client_movement_by_manager(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )
        plan = accept_client_movement_request(
            request_id=request_row.id,
            accepted_by=self.storekeeper,
            prepared_box_count=1,
        ).plans[0]
        prepared_box = plan.prepared_boxes.get()
        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)
        self.assertTrue(task.payload["fbs_movement_marking_scan_v1"])
        self.assertTrue(
            take_move_task(
                legacy_order_id=task.legacy_order_id,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            ).ok
        )
        for scan in (
            self.source_pallet.container_code,
            source.container.container_code,
            self.staging_location.location_code,
            prepared_box.box_code,
        ):
            result = scan_move_task_step(
                legacy_order_id=task.legacy_order_id,
                scan_value=scan,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            )
            self.assertTrue(result.ok, result.error)

        product_result = scan_move_task_step(
            legacy_order_id=task.legacy_order_id,
            scan_value=self.barcode,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(product_result.ok, product_result.error)
        task.refresh_from_db()
        self.assertEqual(task.payload["mobile_execution"]["units_scanned_qty"], 0)
        self.assertTrue(task.payload["mobile_execution"]["pending_marking_scan"])
        self.assertEqual(
            build_mobile_execution_snapshot(task.legacy_order_id)["current_step"],
            "marking",
        )

        wrong_result = scan_move_task_step(
            legacy_order_id=task.legacy_order_id,
            scan_value=self._marking_code("OTHER-SKU", gtin="04600000009018"),
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertFalse(wrong_result.ok)
        self.assertIn("другому товару", wrong_result.error)

        valid_result = scan_move_task_step(
            legacy_order_id=task.legacy_order_id,
            scan_value=marking_code,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(valid_result.ok, valid_result.error)
        self.assertTrue(valid_result.completed)
        task.refresh_from_db()
        self.assertEqual(task.payload["mobile_execution"]["units_scanned_qty"], 1)
        event = WarehouseEvent.objects.get(
            event_type="fbs_replenishment_marking_scanned"
        )
        self.assertEqual(event.payload["marking_code"], marking_code)
        self.assertEqual(event.payload["product_barcode"], self.barcode)

    def test_movement_marking_service_blocks_duplicate_scan(self):
        self.sku.honest_sign = True
        self.sku.save(update_fields=["honest_sign"])
        source = self._snapshot(code="MOV-BRIDGE-CHZ-DUP", qty=2)
        marking_code = self._marking_code("MOVEMENT-DUPLICATE")
        source.marking_code = marking_code
        source.save(update_fields=["marking_code", "updated_at"])
        request_row = self._request(qty=1)
        approve_client_movement_by_manager(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )
        plan = accept_client_movement_request(
            request_id=request_row.id,
            accepted_by=self.storekeeper,
            prepared_box_count=1,
        ).plans[0]
        allocation = FbsReplenishmentAllocation.objects.get(line__plan=plan)
        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)
        self.assertTrue(
            take_move_task(
                legacy_order_id=task.legacy_order_id,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            ).ok
        )

        record_fbs_movement_marking_scan(
            allocation_id=allocation.id,
            marking_scan=marking_code,
            performed_by=self.driver,
        )
        with self.assertRaisesMessage(FbsReplenishmentError, "ДУБЛЬ ЧЗ"):
            record_fbs_movement_marking_scan(
                allocation_id=allocation.id,
                marking_scan=marking_code,
                performed_by=self.driver,
            )

    def test_whole_box_movement_keeps_box_and_uses_reachtruck_destination_scan(self):
        source = self._snapshot(code="MOV-BRIDGE-BOX", qty=5)
        request_row = self._request(mode="box", qty=5, units_per_box=5)
        plan = self._approve_and_accept(request_row).plans[0]
        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)

        self.assertTrue(task.payload["fbs_dynamic_os_destination_v1"])
        self.assertEqual(task.payload["destination_scan_code"], "")
        self.assertEqual(task.to_zone, "OS")
        self.assertIsNone(task.to_row)
        self.assertIsNone(task.to_section)
        self.assertIsNone(task.to_tier)
        self.assertIsNone(task.to_cell)

        take_move_task(
            legacy_order_id=task.legacy_order_id,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        for scan in (
            self.source_pallet.container_code,
            source.container_code,
            os_location_code(row=3, section=2, tier=1, cell=1),
        ):
            result = scan_move_task_step(
                legacy_order_id=task.legacy_order_id,
                scan_value=scan,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            )
            self.assertTrue(result.ok, result.error)

        task.refresh_from_db()
        source.refresh_from_db()
        plan.refresh_from_db()
        target_box = FbsBox.objects.get(pk=plan.target_box_id)
        self.assertEqual(task.status, MoveTask.STATUS_DONE)
        self.assertEqual(task.to_zone, "OS")
        self.assertEqual((task.to_row, task.to_section, task.to_tier, task.to_cell), (3, 2, 1, 1))
        self.assertEqual(
            task.payload["destination_scan_code"],
            os_location_code(row=3, section=2, tier=1, cell=1),
        )
        self.assertEqual(source.qty, 0)
        self.assertEqual(plan.target_cell.location_id, self.free_os_location.id)
        self.assertEqual(target_box.box_code, source.container_code)
        self.assertEqual(target_box.source_container_id, source.container_id)
        self.assertEqual(target_box.source_container.current_location_id, self.free_os_location.id)
        self.assertEqual(
            target_box.source_container.parent_container_id,
            target_box.pallet.warehouse_container_id,
        )
        self.assertTrue(FbsStockBalance.objects.filter(box=target_box, qty=5).exists())

    @override_settings(FBS_STOCK_PUSH_ENABLED=True)
    def test_stock_stays_unavailable_until_warehouse_confirmation(self):
        profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB movement stock",
            external_account_id="wb-movement-stock",
            external_warehouse_id="901",
            stock_mode=FbsIntegrationProfile.STOCK_MODE_MANAGED,
            is_active=True,
            stock_push_enabled=True,
        )
        export_state = FbsStockExportState.objects.create(
            profile=profile,
            sku_ref=self.sku,
            barcode=self.barcode,
            external_item_id="99001",
        )
        source = self._snapshot(code="MOV-PUBLISH-IN-TRANSIT", qty=5)
        request_row = self._request(mode="box", qty=5, units_per_box=5)

        plan = self._approve_and_accept(request_row).plans[0]

        source.refresh_from_db()
        export_state.refresh_from_db()
        self.assertEqual(
            (source.qty, source.available_qty, source.other_reserved_qty),
            (5, 0, 5),
        )
        self.assertEqual(source.location_id, self.general_location.id)
        self.assertEqual(source.container.current_location_id, self.general_location.id)
        self.assertFalse(FbsStockBalance.objects.exists())
        self.assertFalse(
            WarehouseEvent.objects.filter(
                agency=self.agency,
                event_type="fbs_client_movement_stock_export_activated",
                stock_context_type="fbs_client_movement",
                stock_context_id=str(request_row.id),
            ).exists()
        )
        self.assertEqual(export_state.desired_qty, 0)
        self.assertEqual(export_state.status, FbsStockExportState.STATUS_PENDING)

        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)
        take_move_task(
            legacy_order_id=task.legacy_order_id,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        for scan in (
            self.source_pallet.container_code,
            source.container_code,
            os_location_code(row=3, section=2, tier=1, cell=1),
        ):
            result = scan_move_task_step(
                legacy_order_id=task.legacy_order_id,
                scan_value=scan,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            )
            self.assertTrue(result.ok, result.error)

        source.refresh_from_db()
        refresh_profile_stock_export_states(profile_id=profile.id)
        export_state.refresh_from_db()
        self.assertEqual((source.qty, source.other_reserved_qty), (0, 0))
        self.assertEqual(
            FbsStockBalance.objects.aggregate(total=Sum("qty"))["total"],
            5,
        )
        balance = FbsStockBalance.objects.get(
            agency=self.agency,
            barcode=self.barcode,
        )
        self.assertEqual(balance.available_qty, 0)
        self.assertEqual(export_state.desired_qty, 0)

        confirmed = confirm_client_movement_by_warehouse(
            request_id=request_row.id,
            confirmed_by=self.storekeeper,
        )
        balance.refresh_from_db()
        export_state.refresh_from_db()
        self.assertEqual(confirmed.status, FbsClientMovementRequest.STATUS_COMPLETED)
        self.assertEqual(balance.available_qty, 5)
        self.assertTrue(
            WarehouseEvent.objects.filter(
                agency=self.agency,
                event_type="fbs_client_movement_stock_export_activated",
                stock_context_type="fbs_client_movement",
                stock_context_id=str(request_row.id),
            ).exists()
        )
        self.assertEqual(export_state.desired_qty, 5)
        self.assertEqual(export_state.status, FbsStockExportState.STATUS_PENDING)

    def test_whole_box_with_multiple_allocations_reuses_one_destination_scan(self):
        source_box = self._source_box("MOV-BRIDGE-BOX-MULTI")
        source_rows = []
        for index, qty in enumerate((2, 3), start=1):
            source_rows.append(
                WarehouseStockSnapshot.objects.create(
                    agency=self.agency,
                    stock_unit_type="box",
                    source_context_type="receiving",
                    source_context_id=f"MOV-BRIDGE-BOX-MULTI-{index}",
                    sku_ref=self.sku,
                    sku_code=self.sku.sku_code,
                    name=self.sku.name,
                    barcode=self.barcode,
                    goods_type="gv",
                    qty=qty,
                    available_qty=qty,
                    container=source_box,
                    container_code=source_box.container_code,
                    location=self.general_location,
                    zone_code="STORAGE",
                    zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
                    warehouse_state_code="stored",
                )
            )
        request_row = self._request(mode="box", qty=5, units_per_box=5)
        plan = self._approve_and_accept(request_row).plans[0]
        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)

        self.assertEqual(
            FbsReplenishmentAllocation.objects.filter(line__plan=plan).count(),
            2,
        )
        self.assertTrue(
            take_move_task(
                legacy_order_id=task.legacy_order_id,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            ).ok
        )
        replenishment_service = import_module("fbs.services.replenishment")
        with (
            patch.object(
                replenishment_service,
                "_select_replenishment_destination",
                wraps=replenishment_service._select_replenishment_destination,
            ) as select_destination,
            patch.object(
                replenishment_service,
                "_refresh_completion_status",
                wraps=replenishment_service._refresh_completion_status,
            ) as refresh_completion,
            patch.object(
                replenishment_service,
                "_complete_replenishment_allocation",
                wraps=replenishment_service._complete_replenishment_allocation,
            ) as complete_single_allocation,
        ):
            for scan in (
                self.source_pallet.container_code,
                source_box.container_code,
                os_location_code(row=3, section=2, tier=1, cell=1),
            ):
                result = scan_move_task_step(
                    legacy_order_id=task.legacy_order_id,
                    scan_value=scan,
                    user=self.driver,
                    employee_id=self.driver_employee.id,
                    employee_name=self.driver_employee.full_name,
                )
                self.assertTrue(result.ok, result.error)
        self.assertEqual(select_destination.call_count, 1)
        self.assertEqual(refresh_completion.call_count, 1)
        self.assertEqual(complete_single_allocation.call_count, 0)

        task.refresh_from_db()
        plan.refresh_from_db()
        for source in source_rows:
            source.refresh_from_db()
        self.assertEqual(task.status, MoveTask.STATUS_DONE)
        self.assertEqual(plan.status, FbsReplenishmentPlan.STATUS_DONE)
        self.assertEqual(plan.moved_qty, 5)
        self.assertEqual([source.qty for source in source_rows], [0, 0])
        self.assertEqual(
            FbsReplenishmentAllocation.objects.filter(
                line__plan=plan,
                status=FbsReplenishmentAllocation.STATUS_DONE,
                qty_moved__gt=0,
            ).count(),
            2,
        )
        self.assertEqual(
            FbsStockMovement.objects.filter(allocation__line__plan=plan).count(),
            2,
        )
        self.assertEqual(
            WarehouseEvent.objects.filter(
                event_type="fbs_replenishment_completed",
                stock_context_type="fbs_replenishment",
                stock_context_id=str(plan.id),
            ).count(),
            2,
        )
        self.assertEqual(
            FbsStockBalance.objects.filter(box_id=plan.target_box_id).aggregate(total=Sum("qty"))[
                "total"
            ],
            5,
        )

    def test_whole_box_many_marked_allocations_complete_with_bounded_queries(self):
        source_box = self._source_box("MOV-BRIDGE-BOX-BULK-MARKED")
        for index in range(300):
            WarehouseStockSnapshot.objects.create(
                agency=self.agency,
                stock_unit_type="box",
                source_context_type="receiving",
                source_context_id=f"MOV-BRIDGE-BOX-BULK-MARKED-{index}",
                sku_ref=self.sku,
                sku_code=self.sku.sku_code,
                name=self.sku.name,
                barcode=self.barcode,
                goods_type="gv",
                marking_code=self._marking_code(f"BULK{index:05d}"),
                qty=1,
                available_qty=1,
                container=source_box,
                container_code=source_box.container_code,
                location=self.general_location,
                zone_code="STORAGE",
                zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
                warehouse_state_code="stored",
            )
        request_row = self._request(mode="box", qty=300, units_per_box=300)
        plan = self._approve_and_accept(request_row).plans[0]
        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)
        self.assertEqual(
            FbsReplenishmentAllocation.objects.filter(line__plan=plan).count(),
            300,
        )
        self.assertTrue(
            take_move_task(
                legacy_order_id=task.legacy_order_id,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            ).ok
        )
        for scan in (self.source_pallet.container_code, source_box.container_code):
            result = scan_move_task_step(
                legacy_order_id=task.legacy_order_id,
                scan_value=scan,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            )
            self.assertTrue(result.ok, result.error)

        with CaptureQueriesContext(connection) as queries:
            completed = scan_move_task_step(
                legacy_order_id=task.legacy_order_id,
                scan_value=os_location_code(row=3, section=2, tier=1, cell=1),
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            )

        self.assertTrue(completed.ok, completed.error)
        self.assertTrue(completed.completed)
        self.assertLess(len(queries), 300)
        self.assertEqual(
            FbsStockMovement.objects.filter(allocation__line__plan=plan).count(),
            300,
        )
        self.assertEqual(
            FbsStockBalance.objects.filter(box_id=plan.target_box_id).aggregate(total=Sum("qty"))[
                "total"
            ],
            300,
        )

    def test_whole_box_missing_report_skips_box_even_when_identical_box_is_available(self):
        missing_source = self._snapshot(code="MOV-BRIDGE-MISSING", qty=5)
        replacement_source = self._snapshot(code="MOV-BRIDGE-REPLACEMENT", qty=5)
        request_row = self._request(mode="box", qty=5, units_per_box=5)
        plan = self._approve_and_accept(request_row).plans[0]
        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)
        take_result = take_move_task(
            legacy_order_id=task.legacy_order_id,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)
        pallet_scan = scan_move_task_step(
            legacy_order_id=task.legacy_order_id,
            scan_value=self.source_pallet.container_code,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(pallet_scan.ok, pallet_scan.error)

        result = report_move_task_missing_box(
            legacy_order_id=task.legacy_order_id,
            box_code=missing_source.container_code,
            mobile_category="movement",
            mobile_request_key=f"fbs:{request_row.number}",
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )

        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.partial_completed)
        self.assertEqual(result.replacement_box_code, "")
        task.refresh_from_db()
        missing_source.refresh_from_db()
        replacement_source.refresh_from_db()
        allocation = FbsReplenishmentAllocation.objects.get(line__plan=plan)
        plan.refresh_from_db()
        request_row.refresh_from_db()
        self.assertEqual(allocation.source_snapshot_id, missing_source.id)
        self.assertEqual(allocation.status, FbsReplenishmentAllocation.STATUS_CANCELED)
        self.assertEqual(task.status, MoveTask.STATUS_CANCELED)
        self.assertIn("missing_box_skipped_v1", task.payload)
        self.assertEqual(plan.status, FbsReplenishmentPlan.STATUS_CANCELED)
        self.assertEqual(request_row.status, FbsClientMovementRequest.STATUS_MOVED)
        self.assertEqual((missing_source.qty, missing_source.available_qty), (5, 0))
        self.assertEqual((replacement_source.qty, replacement_source.available_qty), (5, 5))
        self.assertEqual(missing_source.other_reserved_qty, 5)
        self.assertEqual(replacement_source.other_reserved_qty, 0)

    def test_existing_request_assignment_drift_is_repaired_for_current_driver(self):
        source = self._snapshot(code="MOV-ASSIGNMENT-REPAIR", qty=5)
        request_row = self._request(mode="box", qty=5, units_per_box=5)
        plan = self._approve_and_accept(request_row).plans[0]
        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)

        payload = dict(task.payload or {})
        payload.update(
            {
                "status": MoveTask.STATUS_IN_PROGRESS,
                "status_label": "В работе",
                "assigned_to_id": self.driver_employee.id,
                "assigned_employee_id": self.driver_employee.id,
                "assigned_to_name": self.driver_employee.full_name,
            }
        )
        task.status = MoveTask.STATUS_IN_PROGRESS
        task.assigned_to = self.driver
        task.assigned_to_name = self.driver_employee.full_name
        task.payload = payload
        task.save(
            update_fields=[
                "status",
                "assigned_to",
                "assigned_to_name",
                "payload",
                "updated_at",
            ]
        )
        plan.refresh_from_db()
        self.assertIsNone(plan.assigned_to_id)
        before_stock = (source.qty, source.available_qty, source.other_reserved_qty)

        result = scan_move_request_step(
            legacy_order_ids=[task.legacy_order_id],
            scan_value=self.source_pallet.container_code,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )

        self.assertTrue(result.ok, result.error)
        plan.refresh_from_db()
        task.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(plan.assigned_to_id, self.driver.id)
        self.assertFalse(
            plan.warehouse_operation.tasks.exclude(assigned_to=self.driver).exists()
        )
        self.assertEqual(task.assigned_to_id, self.driver.id)
        self.assertTrue(task.payload["mobile_execution"]["pallet_confirmed"])
        self.assertEqual(
            (source.qty, source.available_qty, source.other_reserved_qty),
            before_stock,
        )

    def test_whole_box_request_take_batches_status_sync(self):
        self._snapshot(code="MOV-BATCH-TAKE-BOX-1", qty=2)
        self._snapshot(code="MOV-BATCH-TAKE-BOX-2", qty=2)
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[
                {
                    "barcode": self.barcode,
                    "qty": 4,
                    "units_per_box": 2,
                    "box_count": 2,
                }
            ],
        )
        result = self._approve_and_accept(request_row)
        tasks = list(
            MoveTask.objects.filter(
                payload__fbs_movement_id=request_row.id,
                payload__fbs_allocation_ids__isnull=False,
            ).order_by("id")
        )
        task_ids = [task.legacy_order_id for task in tasks]

        with patch(
            "fbs.services.client_movements.sync_client_movement_request_status",
            wraps=sync_client_movement_request_status,
        ) as sync_status:
            take_result = take_move_request(
                legacy_order_ids=task_ids,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            )

        self.assertTrue(take_result.ok, take_result.error)
        self.assertEqual(len(result.plans), 2)
        self.assertEqual(sync_status.call_count, 1)
        self.assertFalse(
            FbsReplenishmentPlan.objects.filter(
                id__in=[plan.id for plan in result.plans]
            ).exclude(assigned_to=self.driver).exists()
        )
        self.assertFalse(
            MoveTask.objects.filter(id__in=[task.id for task in tasks])
            .exclude(assigned_to=self.driver, status=MoveTask.STATUS_IN_PROGRESS)
            .exists()
        )

    def test_whole_box_request_collects_all_boxes_before_one_fbs_trip(self):
        second_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="MOV-SKU-2",
            name="Второй товар для FBS",
        )
        second_barcode = "4600000009002"
        SKUBarcode.objects.create(sku=second_sku, value=second_barcode, is_primary=True)
        first_source = self._snapshot(code="MOV-BATCH-BOX-1", qty=2)
        second_box = self._source_box("MOV-BATCH-BOX-2")
        second_source = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            stock_unit_type="box",
            source_context_type="receiving",
            source_context_id="MOV-BATCH-BOX-2",
            sku_ref=second_sku,
            sku_code=second_sku.sku_code,
            name=second_sku.name,
            barcode=second_barcode,
            goods_type="gv",
            qty=2,
            available_qty=2,
            container=second_box,
            container_code=second_box.container_code,
            location=self.general_location,
            zone_code="STORAGE",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            warehouse_state_code="stored",
        )
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[
                {"barcode": self.barcode, "qty": 2, "units_per_box": 2, "box_count": 1},
                {"barcode": second_barcode, "qty": 2, "units_per_box": 2, "box_count": 1},
            ],
        )
        result = self._approve_and_accept(request_row)
        tasks = list(
            MoveTask.objects.filter(
                payload__fbs_movement_id=request_row.id,
                payload__fbs_allocation_ids__isnull=False,
            ).order_by("id")
        )
        self.assertEqual(len(result.plans), 2)
        self.assertEqual(len(tasks), 2)
        task_ids = [task.legacy_order_id for task in tasks]

        self.client.force_login(self.driver)
        request_page = self.client.get(
            "/reachtruck/",
            {
                "mobile_category": "movement",
                "mobile_request": f"fbs:{request_row.number}",
            },
        )
        self.assertEqual(request_page.status_code, 200)
        self.assertContains(request_page, "Взять заявку в работу")

        take_result = take_move_request(
            legacy_order_ids=task_ids,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)
        for task in tasks:
            task.refresh_from_db()
            self.assertEqual(task.assigned_to_id, self.driver.id)
            self.assertTrue(task.payload["fbs_box_collection_batch_v1"])
        for plan in result.plans:
            plan.refresh_from_db()
            self.assertEqual(plan.assigned_to_id, self.driver.id)
            self.assertFalse(
                plan.warehouse_operation.tasks.exclude(assigned_to=self.driver).exists()
            )
        collection_page = self.client.get(
            "/reachtruck/",
            {
                "mobile_category": "movement",
                "mobile_request": f"fbs:{request_row.number}",
            },
        )
        self.assertContains(collection_page, "Собрано коробов 0 из 2")
        self.assertContains(collection_page, "Осталось коробов")

        other_driver = get_user_model().objects.create_user(username="movement-driver-2")
        other_employee = Employee.objects.create(
            user=other_driver,
            full_name="Второй ричтрак",
            role="reachtruck_driver",
        )
        blocked_take = take_move_request(
            legacy_order_ids=task_ids,
            user=other_driver,
            employee_id=other_employee.id,
            employee_name=other_employee.full_name,
        )
        self.assertFalse(blocked_take.ok)
        self.assertIn("другим водителем", blocked_take.error)

        snapshot = build_mobile_request_execution_snapshot(
            task_ids,
            employee_id=self.driver_employee.id,
        )
        self.assertTrue(snapshot["fbs_box_collection_batch"])
        self.assertEqual(snapshot["collected_count"], 0)
        self.assertEqual(snapshot["current_step"], "pallet")

        pallet_scan = scan_move_request_step(
            legacy_order_ids=task_ids,
            scan_value=self.source_pallet.container_code,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(pallet_scan.ok, pallet_scan.error)
        for task in tasks:
            task.refresh_from_db()
            self.assertTrue(task.payload["mobile_execution"]["pallet_confirmed"])

        snapshot = build_mobile_request_execution_snapshot(
            task_ids,
            employee_id=self.driver_employee.id,
        )
        first_box_code = str(snapshot["expected_scan"])
        first_box_scan = scan_move_request_step(
            legacy_order_ids=task_ids,
            scan_value=first_box_code,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(first_box_scan.ok, first_box_scan.error)
        snapshot = build_mobile_request_execution_snapshot(
            task_ids,
            employee_id=self.driver_employee.id,
        )
        self.assertEqual(snapshot["collected_count"], 1)
        self.assertEqual(snapshot["current_step"], "boxes")

        early_destination = scan_move_request_step(
            legacy_order_ids=task_ids,
            scan_value=os_location_code(row=3, section=2, tier=1, cell=1),
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertFalse(early_destination.ok)
        self.assertIn("Ожидается QR короба", early_destination.error)
        first_source.refresh_from_db()
        second_source.refresh_from_db()
        self.assertEqual((first_source.qty, second_source.qty), (2, 2))

        second_box_scan = scan_move_request_step(
            legacy_order_ids=task_ids,
            scan_value=str(snapshot["expected_scan"]),
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(second_box_scan.ok, second_box_scan.error)
        self.assertIn("Все 2 короба собраны", second_box_scan.message)
        snapshot = build_mobile_request_execution_snapshot(
            task_ids,
            employee_id=self.driver_employee.id,
        )
        self.assertTrue(snapshot["all_collected"])
        self.assertEqual(snapshot["current_step"], "destination")
        first_source.refresh_from_db()
        second_source.refresh_from_db()
        self.assertEqual((first_source.qty, second_source.qty), (2, 2))

        first_placement = scan_move_request_step(
            legacy_order_ids=task_ids,
            scan_value=os_location_code(row=3, section=2, tier=1, cell=1),
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(first_placement.ok, first_placement.error)
        self.assertFalse(first_placement.completed)
        snapshot = build_mobile_request_execution_snapshot(
            task_ids,
            employee_id=self.driver_employee.id,
        )
        self.assertEqual(snapshot["placed_count"], 1)
        self.assertEqual(snapshot["current_step"], "destination")

        second_placement = scan_move_request_step(
            legacy_order_ids=task_ids,
            scan_value=os_location_code(row=3, section=2, tier=1, cell=1),
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(second_placement.ok, second_placement.error)
        self.assertTrue(second_placement.completed)
        first_source.refresh_from_db()
        second_source.refresh_from_db()
        self.assertEqual((first_source.qty, second_source.qty), (0, 0))
        self.assertEqual(
            FbsStockBalance.objects.aggregate(total=Sum("qty"))["total"],
            4,
        )

    def test_hard_reserved_full_source_pallet_merges_into_selected_fbs_cell_without_capacity_limit(self):
        first_source = self._snapshot(code="MOV-FULL-PALLET-1", qty=2)
        second_source = self._snapshot(code="MOV-FULL-PALLET-2", qty=2)
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[
                {
                    "barcode": self.barcode,
                    "qty": 4,
                    "units_per_box": 2,
                    "box_count": 2,
                }
            ],
            idempotency_key="movement-full-source-pallet",
        )
        approve_client_movement_by_manager(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )

        result = self._approve_and_accept(request_row)
        planned_pallet = result.plans[0].target_pallet
        self.assertEqual(planned_pallet.cell.location.zone_code, "FBS")
        self.assertEqual(
            planned_pallet.cell.location.zone_kind,
            WarehouseLocation.ZONE_KIND_VIRTUAL,
        )
        self.assertIsNone(planned_pallet.warehouse_container.current_location_id)
        legacy_cell = FbsStorageCell.objects.create(
            cell_code="FBS@LEGACY-OS-FREE",
            location=self.free_os_location,
            client_cluster=self.agency.id,
        )
        legacy_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code=f"FBS-PAL-{self.agency.id}-LEGACYEMPTY",
            current_location=self.free_os_location,
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type=FBS_STORAGE_CONTEXT_TYPE,
            source_context_id=f"FBS-PAL-{self.agency.id}-LEGACYEMPTY",
        )
        legacy_pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code=f"FBS-PAL-{self.agency.id}-LEGACYEMPTY",
            cell=legacy_cell,
            warehouse_container=legacy_container,
            max_boxes=10,
            status=FbsPallet.STATUS_PLANNED,
        )
        blocked_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=4,
            section_no=2,
            tier_no=4,
            cell_no=1,
            location_code="OS-4-2-4-1",
            is_active=True,
            is_storage=True,
        )
        blocked_cell = FbsStorageCell.objects.create(
            cell_code="FBS@A-4/4-1",
            location=blocked_location,
            client_cluster=self.agency.id,
        )
        blocked_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="FBS-BLOCKER-PALLET-001",
            current_location=blocked_location,
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type=FBS_STORAGE_CONTEXT_TYPE,
            source_context_id="FBS-BLOCKER-PALLET-001",
        )
        blocked_pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-BLOCKER-PALLET-001",
            cell=blocked_cell,
            warehouse_container=blocked_container,
            max_boxes=1,
            status=FbsPallet.STATUS_ACTIVE,
        )
        FbsBox.objects.create(
            agency=self.agency,
            pallet=blocked_pallet,
            box_code="FBS-BLOCKER-BOX-001",
            status=FbsBox.STATUS_ACTIVE,
        )
        tasks = list(
            MoveTask.objects.filter(
                payload__fbs_movement_id=request_row.id,
                payload__fbs_allocation_ids__isnull=False,
            ).order_by("id")
        )
        self.assertEqual(len(result.plans), 2)
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(task.move_mode == MoveTask.MODE_PALLET_FULL for task in tasks))
        self.assertTrue(all(task.payload["fbs_full_source_pallet_v1"] for task in tasks))
        self.assertTrue(all(task.payload["mobile_execution"]["box_confirmed"] for task in tasks))
        task_ids = [task.legacy_order_id for task in tasks]

        take_result = take_move_request(
            legacy_order_ids=task_ids,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)
        before = build_mobile_request_execution_snapshot(
            task_ids,
            employee_id=self.driver_employee.id,
        )
        self.assertEqual(before["current_step"], "pallet")
        self.assertEqual(before["expected_scan"], self.source_pallet.container_code)
        self.assertEqual(before["collected_count"], 0)

        pallet_scan = scan_move_request_step(
            legacy_order_ids=task_ids,
            scan_value=self.source_pallet.container_code,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(pallet_scan.ok, pallet_scan.error)
        after_pallet = build_mobile_request_execution_snapshot(
            task_ids,
            employee_id=self.driver_employee.id,
        )
        self.assertEqual(after_pallet["current_step"], "destination")
        self.assertEqual(after_pallet["collected_count"], 2)
        destination_scan = os_location_code(row=3, section=2, tier=1, cell=1)
        self.assertEqual(after_pallet["expected_scan"], "QR выбранной ячейки OS")
        self.assertEqual(after_pallet["active_destination_code"], "")
        self.assertTrue(after_pallet["destination_guide"]["advisory"])
        self.assertIn("только рекомендация", after_pallet["prompt"])
        self.assertIn(
            destination_scan,
            {
                row["code"]
                for row in after_pallet["destination_guide"]["free_places"]
            },
        )

        invalid_destination = scan_move_request_step(
            legacy_order_ids=task_ids,
            scan_value="OS-99-99-99-99",
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertFalse(invalid_destination.ok)
        self.assertIn(
            "Водитель может выбрать любую другую действующую ячейку OS",
            invalid_destination.error,
        )

        placement = scan_move_request_step(
            legacy_order_ids=task_ids,
            scan_value=os_location_code(row=4, section=2, tier=4, cell=1),
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(placement.ok, placement.error)
        self.assertTrue(placement.completed)
        first_source.refresh_from_db()
        second_source.refresh_from_db()
        self.source_pallet.refresh_from_db()
        planned_pallet.refresh_from_db()
        blocked_pallet.refresh_from_db()
        self.assertEqual((first_source.qty, second_source.qty), (0, 0))
        self.assertEqual(self.source_pallet.status, WarehouseContainer.STATUS_ARCHIVED)
        self.assertIsNone(self.source_pallet.current_location_id)
        self.assertEqual(planned_pallet.status, FbsPallet.STATUS_ARCHIVED)
        self.assertEqual(self.source_pallet.source_context_type, FBS_STORAGE_CONTEXT_TYPE)
        self.assertEqual(
            set(
                WarehouseContainer.objects.filter(
                    id__in=(first_source.container_id, second_source.container_id)
                ).values_list("parent_container_id", flat=True)
            ),
            {blocked_container.id},
        )
        self.assertEqual(blocked_pallet.cell.location_id, blocked_location.id)
        self.assertEqual(
            FbsBox.objects.filter(
                pallet=blocked_pallet,
                status__in=(FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE),
            ).count(),
            3,
        )
        self.assertEqual(
            FbsStockBalance.objects.aggregate(total=Sum("qty"))["total"],
            4,
        )

    def test_hard_reserved_multiple_source_pallets_allocate_os_in_one_batch(self):
        source_pallets = []
        for index in range(3):
            source_pallet = WarehouseContainer.objects.create(
                agency=self.agency,
                container_type=WarehouseContainer.TYPE_PALLET,
                container_code=f"MOV-SOURCE-PALLET-BATCH-{index}",
                current_location=self.general_location,
            )
            source_box = WarehouseContainer.objects.create(
                agency=self.agency,
                container_type=WarehouseContainer.TYPE_BOX,
                container_code=f"MOV-SOURCE-BOX-BATCH-{index}-gv",
                parent_container=source_pallet,
                current_location=self.general_location,
            )
            WarehouseStockSnapshot.objects.create(
                agency=self.agency,
                stock_unit_type="box",
                source_context_type="receiving",
                source_context_id=f"MOV-BATCH-{index}",
                sku_ref=self.sku,
                sku_code=self.sku.sku_code,
                name=self.sku.name,
                barcode=self.barcode,
                goods_type="gv",
                qty=2,
                available_qty=2,
                container=source_box,
                container_code=source_box.container_code,
                location=self.general_location,
                zone_code="STORAGE",
                zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
                warehouse_state_code="stored",
            )
            source_pallets.append(source_pallet)
            if index:
                WarehouseLocation.objects.create(
                    warehouse_code="MSK",
                    zone_code="OS",
                    zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
                    row_no=3,
                    section_no=2,
                    tier_no=1,
                    cell_no=index + 1,
                    location_code=f"OS-3-2-1-{index + 1}",
                    is_storage=True,
                )

        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[
                {
                    "barcode": self.barcode,
                    "qty": 6,
                    "units_per_box": 2,
                    "box_count": 3,
                }
            ],
            idempotency_key="movement-full-source-pallet-batch",
        )
        approve_client_movement_by_manager(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )

        from .services.storage import allocate_shared_os_fbs_pallet_batch

        with patch(
            "fbs.services.client_movements.allocate_shared_os_fbs_pallet_batch",
            wraps=allocate_shared_os_fbs_pallet_batch,
        ) as allocate_batch:
            result = self._approve_and_accept(request_row)

        self.assertEqual(allocate_batch.call_count, 1)
        self.assertEqual(len(result.plans), 3)
        tasks = list(
            MoveTask.objects.filter(
                payload__fbs_movement_id=request_row.id,
                payload__fbs_allocation_ids__isnull=False,
            ).order_by("id")
        )
        self.assertEqual(len(tasks), 3)
        self.assertEqual(
            {task.payload["fbs_source_pallet_id"] for task in tasks},
            {source_pallet.id for source_pallet in source_pallets},
        )
        self.assertTrue(all(task.move_mode == MoveTask.MODE_PALLET_FULL for task in tasks))
        self.assertEqual(
            len({plan.target_pallet_id for plan in result.plans}),
            3,
        )
        target_pallets = FbsPallet.objects.filter(
            id__in={plan.target_pallet_id for plan in result.plans}
        ).select_related("cell__location", "warehouse_container")
        self.assertEqual(
            {pallet.cell.location.zone_code for pallet in target_pallets},
            {"FBS"},
        )
        self.assertTrue(
            all(
                pallet.cell.location.zone_kind == WarehouseLocation.ZONE_KIND_VIRTUAL
                and pallet.warehouse_container.current_location_id is None
                for pallet in target_pallets
            )
        )

    def test_hard_reserved_partial_source_pallet_still_requires_box_scan(self):
        selected = self._snapshot(code="MOV-PARTIAL-PALLET-SELECTED", qty=2)
        untouched = self._snapshot(code="MOV-PARTIAL-PALLET-UNTOUCHED", qty=2)
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[
                {
                    "barcode": self.barcode,
                    "qty": 2,
                    "units_per_box": 2,
                    "box_count": 1,
                }
            ],
            idempotency_key="movement-partial-source-pallet",
        )
        approve_client_movement_by_manager(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )

        result = self._approve_and_accept(request_row)
        task = MoveTask.objects.get(payload__fbs_movement_id=request_row.id)
        self.assertEqual(len(result.plans), 1)
        self.assertEqual(task.move_mode, MoveTask.MODE_BOX_FULL)
        self.assertFalse(task.payload.get("fbs_full_source_pallet_v1"))
        self.assertFalse(task.payload["mobile_execution"]["box_confirmed"])
        selected.refresh_from_db()
        untouched.refresh_from_db()
        self.assertEqual(selected.other_reserved_qty, 2)
        self.assertEqual(untouched.available_qty, 2)

    def test_full_source_pallet_guard_rejects_new_unselected_box(self):
        source = self._snapshot(code="MOV-FULL-GUARD-SELECTED", qty=2)
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[
                {
                    "barcode": self.barcode,
                    "qty": 2,
                    "units_per_box": 2,
                    "box_count": 1,
                }
            ],
            idempotency_key="movement-full-pallet-guard",
        )
        approve_client_movement_by_manager(
            request_id=request_row.id,
            reviewed_by=self.manager,
        )
        self._approve_and_accept(request_row)
        task = MoveTask.objects.get(payload__fbs_movement_id=request_row.id)
        task_ids = [task.legacy_order_id]
        take_result = take_move_request(
            legacy_order_ids=task_ids,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)
        pallet_scan = scan_move_request_step(
            legacy_order_ids=task_ids,
            scan_value=self.source_pallet.container_code,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(pallet_scan.ok, pallet_scan.error)
        added = self._snapshot(code="MOV-FULL-GUARD-ADDED", qty=1)

        placement = scan_move_request_step(
            legacy_order_ids=task_ids,
            scan_value=os_location_code(row=3, section=2, tier=1, cell=1),
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )

        self.assertFalse(placement.ok)
        self.assertIn("Состав паллеты изменился", placement.error)
        source.refresh_from_db()
        added.refresh_from_db()
        self.source_pallet.refresh_from_db()
        self.assertEqual(source.qty, 2)
        self.assertEqual(added.qty, 1)
        self.assertEqual(self.source_pallet.current_location_id, self.general_location.id)

    def test_active_legacy_otg_task_protects_box_from_fbs_movement(self):
        source = self._snapshot(code="MOV-OTG-PROTECTED", qty=2)
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id="legacy-otg-protection",
            agency=self.agency,
            requested_by=self.manager,
            requested_by_name="Менеджер",
            requested_by_role="manager",
            destination_zone="OTG",
            status=MoveRequest.STATUS_CREATED,
        )
        MoveTask.objects.create(
            request=move_request,
            legacy_order_id="legacy-otg-protected-box",
            pallet_code=self.source_pallet.container_code,
            to_zone="OTG",
            move_mode=MoveTask.MODE_BOX_FULL,
            qty_planned=2,
            status=MoveTask.STATUS_CREATED,
            payload={
                "shipping_order_id": "OTG-TEST-PROTECTION",
                "pallet_code": self.source_pallet.container_code,
                "requested_box": source.container.container_code,
                "requested_boxes": [source.container.container_code],
                "planned_box_codes": [source.container.container_code],
            },
        )

        with self.assertRaisesMessage(
            WarehouseTransitionError,
            "Остаток уже закреплён за активной заявкой на отгрузку",
        ):
            WarehouseWritePathService.assert_not_reserved_for_shipping(
                agency=self.agency,
                snapshots=[source],
            )

    def test_whole_box_request_skips_first_missing_box_and_continues(self):
        second_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="MOV-SKU-MISSING-BATCH",
            name="Отсутствующий товар FBS",
        )
        second_barcode = "4600000009012"
        SKUBarcode.objects.create(sku=second_sku, value=second_barcode, is_primary=True)
        first_source = self._snapshot(code="MOV-PARTIAL-BOX-1", qty=2)
        second_box = self._source_box("MOV-PARTIAL-BOX-2")
        second_source = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            stock_unit_type="box",
            source_context_type="receiving",
            source_context_id="MOV-PARTIAL-BOX-2",
            sku_ref=second_sku,
            sku_code=second_sku.sku_code,
            name=second_sku.name,
            barcode=second_barcode,
            goods_type="gv",
            qty=2,
            available_qty=2,
            container=second_box,
            container_code=second_box.container_code,
            location=self.general_location,
            zone_code="STORAGE",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            warehouse_state_code="stored",
        )
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[
                {"barcode": self.barcode, "qty": 2, "units_per_box": 2, "box_count": 1},
                {"barcode": second_barcode, "qty": 2, "units_per_box": 2, "box_count": 1},
            ],
        )
        self._approve_and_accept(request_row)
        tasks = list(
            MoveTask.objects.filter(
                payload__fbs_movement_id=request_row.id,
                payload__fbs_allocation_ids__isnull=False,
            ).order_by("id")
        )
        task_ids = [task.legacy_order_id for task in tasks]
        take_result = take_move_request(
            legacy_order_ids=task_ids,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)
        pallet_scan = scan_move_request_step(
            legacy_order_ids=task_ids,
            scan_value=self.source_pallet.container_code,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(pallet_scan.ok, pallet_scan.error)
        snapshot = build_mobile_request_execution_snapshot(
            task_ids, employee_id=self.driver_employee.id
        )
        missing_task = MoveTask.objects.get(legacy_order_id=snapshot["active_order_id"])
        missing_code = str(snapshot["expected_scan"])

        report = report_move_task_missing_box(
            legacy_order_id=missing_task.legacy_order_id,
            box_code=missing_code,
            mobile_category="movement",
            mobile_request_key=f"fbs:{request_row.number}",
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )

        self.assertTrue(report.ok, report.error)
        self.assertTrue(report.partial_completed)
        missing_task.refresh_from_db()
        self.assertEqual(missing_task.status, MoveTask.STATUS_CANCELED)
        self.assertIn("missing_box_skipped_v1", missing_task.payload)
        snapshot = build_mobile_request_execution_snapshot(
            task_ids, employee_id=self.driver_employee.id
        )
        self.assertEqual(snapshot["skipped_count"], 1)
        self.assertFalse(snapshot["all_collected"])
        for _ in range(3):
            if snapshot["all_collected"]:
                break
            scan_result = scan_move_request_step(
                legacy_order_ids=task_ids,
                scan_value=str(snapshot["expected_scan"]),
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            )
            self.assertTrue(scan_result.ok, scan_result.error)
            snapshot = build_mobile_request_execution_snapshot(
                task_ids, employee_id=self.driver_employee.id
            )
        self.assertTrue(snapshot["all_collected"])
        self.assertEqual(snapshot["collected_count"], 1)
        self.assertEqual(snapshot["current_step"], "destination")
        first_source.refresh_from_db()
        self.assertEqual((first_source.qty, first_source.available_qty), (2, 0))
        self.assertEqual(first_source.other_reserved_qty, 2)
        skipped_allocation = FbsReplenishmentAllocation.objects.get(
            line__plan_id=missing_task.payload["fbs_plan_id"]
        )
        skipped_allocation.warehouse_reserve.refresh_from_db()
        self.assertEqual(
            skipped_allocation.warehouse_reserve.status,
            WarehouseReserve.STATUS_CANCELED,
        )
        self.assertTrue(
            WarehouseReserve.objects.filter(
                context_type="missing_box_check",
                qty_allocated=2,
                status=WarehouseReserve.STATUS_ALLOCATED,
            ).exists()
        )

        completed = scan_move_request_step(
            legacy_order_ids=task_ids,
            scan_value=os_location_code(row=3, section=2, tier=1, cell=1),
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(completed.ok, completed.error)
        self.assertTrue(completed.completed)
        first_source.refresh_from_db()
        second_source.refresh_from_db()
        request_row.refresh_from_db()
        self.assertEqual(first_source.qty, 2)
        self.assertEqual(second_source.qty, 0)
        self.assertEqual(request_row.status, FbsClientMovementRequest.STATUS_MOVED)
        move_request = MoveRequest.objects.get(
            context_id=f"fbs-movement:{request_row.id}"
        )
        self.assertEqual(move_request.status, MoveRequest.STATUS_DONE)

    def test_whole_box_request_with_all_boxes_missing_requires_fact_comment(self):
        source = self._snapshot(code="MOV-ALL-MISSING", qty=5)
        request_row = self._request(
            mode=FbsClientMovementRequest.MODE_BOX,
            qty=5,
            units_per_box=5,
            box_count=1,
        )
        plan = self._approve_and_accept(request_row).plans[0]
        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)
        take_result = take_move_task(
            legacy_order_id=task.legacy_order_id,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)
        pallet_scan = scan_move_task_step(
            legacy_order_id=task.legacy_order_id,
            scan_value=self.source_pallet.container_code,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(pallet_scan.ok, pallet_scan.error)

        report = report_move_task_missing_box(
            legacy_order_id=task.legacy_order_id,
            box_code=source.container_code,
            mobile_category="movement",
            mobile_request_key=f"fbs:{request_row.number}",
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )

        self.assertTrue(report.ok, report.error)
        self.assertTrue(report.partial_completed)
        task.refresh_from_db()
        plan.refresh_from_db()
        request_row.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(task.status, MoveTask.STATUS_CANCELED)
        self.assertEqual(plan.status, FbsReplenishmentPlan.STATUS_CANCELED)
        self.assertEqual(request_row.status, FbsClientMovementRequest.STATUS_MOVED)
        self.assertEqual((source.qty, source.available_qty, source.other_reserved_qty), (5, 0, 5))
        with self.assertRaisesMessage(
            FbsReplenishmentError,
            "При расхождении количества укажите комментарий кладовщика.",
        ):
            confirm_client_movement_by_warehouse(
                request_id=request_row.id,
                confirmed_by=self.storekeeper,
            )

        confirmed = confirm_client_movement_by_warehouse(
            request_id=request_row.id,
            confirmed_by=self.storekeeper,
            comment="Расхождение: короб отсутствует на месте.",
        )

        source.refresh_from_db()
        self.assertEqual(confirmed.status, FbsClientMovementRequest.STATUS_COMPLETED)
        self.assertEqual(confirmed.actual_moved_qty, 0)
        self.assertEqual(confirmed.actual_moved_box_count, 0)
        self.assertEqual(
            confirmed.clarification_reason,
            "Расхождение: короб отсутствует на месте.",
        )
        self.assertEqual((source.qty, source.available_qty, source.other_reserved_qty), (5, 0, 5))
        self.assertIsNotNone(confirmed.billing_synced_at)

    def test_whole_box_client_movement_can_finish_in_exact_pr_rack_cell(self):
        source = self._snapshot(code="MOV-BOX-TO-PR", qty=5)
        rack_cell = self._pr_rack_cell("MOV-PR-RACK")
        request_row = self._request(
            mode=FbsClientMovementRequest.MODE_BOX,
            qty=5,
            units_per_box=5,
            box_count=1,
        )
        plan = self._approve_and_accept(request_row).plans[0]
        source_fbs_box_id = plan.target_box_id
        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)

        take_result = take_move_task(
            legacy_order_id=task.legacy_order_id,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)
        for scan in (
            self.source_pallet.container_code,
            source.container.container_code,
            rack_cell.cell_code,
        ):
            result = scan_move_task_step(
                legacy_order_id=task.legacy_order_id,
                scan_value=scan,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            )
            self.assertTrue(result.ok, result.error)

        plan.refresh_from_db()
        task.refresh_from_db()
        source.refresh_from_db()
        binding = FbsRackCellBinding.objects.select_related(
            "box__source_container",
            "pallet__warehouse_container",
        ).get(rack_cell=rack_cell, agency=self.agency)
        self.assertEqual(plan.target_cell_id, rack_cell.storage_cell_id)
        self.assertEqual(plan.target_pallet_id, binding.pallet_id)
        self.assertEqual(plan.target_box_id, binding.box_id)
        self.assertEqual(binding.box_id, source_fbs_box_id)
        self.assertEqual(binding.box.source_container_id, source.container_id)
        self.assertEqual(
            binding.box.source_container.current_location_id,
            rack_cell.storage_cell.location_id,
        )
        self.assertEqual(binding.box.source_container.source_context_type, "fbs_rack_cell")
        self.assertFalse(pick_requires_box_scan(binding.box))
        self.assertEqual(task.to_zone, "PR")
        self.assertEqual(task.status, MoveTask.STATUS_DONE)
        self.assertEqual((source.qty, source.available_qty, source.other_reserved_qty), (0, 0, 0))
        self.assertEqual(
            FbsStockBalance.objects.filter(box=binding.box).aggregate(total=Sum("qty"))["total"],
            5,
        )

    def test_whole_box_client_movement_merges_into_existing_same_client_pr_binding(self):
        source = self._snapshot(code="MOV-BOX-MERGE-PR", qty=5)
        rack_cell = self._pr_rack_cell("MOV-PR-MERGE")
        existing = _get_or_create_binding(
            rack_cell=rack_cell,
            agency=self.agency,
            actor=self.storekeeper,
        )
        request_row = self._request(
            mode=FbsClientMovementRequest.MODE_BOX,
            qty=5,
            units_per_box=5,
            box_count=1,
        )
        plan = self._approve_and_accept(request_row).plans[0]
        source_fbs_box_id = plan.target_box_id
        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)
        self.assertTrue(
            take_move_task(
                legacy_order_id=task.legacy_order_id,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            ).ok
        )
        for scan in (
            self.source_pallet.container_code,
            source.container.container_code,
            rack_cell.cell_code,
        ):
            result = scan_move_task_step(
                legacy_order_id=task.legacy_order_id,
                scan_value=scan,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            )
            self.assertTrue(result.ok, result.error)

        plan.refresh_from_db()
        source.container.refresh_from_db()
        source_box = FbsBox.objects.get(pk=source_fbs_box_id)
        self.assertEqual(plan.target_box_id, existing.box_id)
        self.assertEqual(source_box.status, FbsBox.STATUS_ARCHIVED)
        self.assertEqual(source.container.status, WarehouseContainer.STATUS_ARCHIVED)
        self.assertFalse(pick_requires_box_scan(existing.box))
        self.assertEqual(
            FbsStockBalance.objects.filter(box=existing.box).aggregate(total=Sum("qty"))["total"],
            5,
        )

    def test_whole_box_client_movement_rejects_pr_cell_used_by_another_client(self):
        source = self._snapshot(code="MOV-BOX-FOREIGN-PR", qty=5)
        rack_cell = self._pr_rack_cell("MOV-PR-FOREIGN")
        other_agency = Agency.objects.create(agn_name="Другой клиент PR")
        _get_or_create_binding(
            rack_cell=rack_cell,
            agency=other_agency,
            actor=self.storekeeper,
        )
        request_row = self._request(
            mode=FbsClientMovementRequest.MODE_BOX,
            qty=5,
            units_per_box=5,
            box_count=1,
        )
        plan = self._approve_and_accept(request_row).plans[0]
        allocation = FbsReplenishmentAllocation.objects.get(line__plan=plan)
        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)
        self.assertTrue(
            take_move_task(
                legacy_order_id=task.legacy_order_id,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            ).ok
        )
        for scan in (self.source_pallet.container_code, source.container.container_code):
            result = scan_move_task_step(
                legacy_order_id=task.legacy_order_id,
                scan_value=scan,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            )
            self.assertTrue(result.ok, result.error)

        rejected = scan_move_task_step(
            legacy_order_id=task.legacy_order_id,
            scan_value=rack_cell.cell_code,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )

        self.assertFalse(rejected.ok)
        self.assertIn("другого клиента", rejected.error)
        task.refresh_from_db()
        allocation.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(task.status, MoveTask.STATUS_IN_PROGRESS)
        self.assertEqual(allocation.status, FbsReplenishmentAllocation.STATUS_RESERVED)
        self.assertEqual((source.qty, source.available_qty, source.other_reserved_qty), (5, 0, 5))
        self.assertFalse(FbsStockMovement.objects.filter(allocation=allocation).exists())

    def test_whole_box_driver_rejects_occupied_cell_and_can_scan_another_free_cell(self):
        source = self._snapshot(code="MOV-BRIDGE-BOX-OCCUPIED", qty=5)
        request_row = self._request(mode="box", qty=5, units_per_box=5)
        plan = self._approve_and_accept(request_row).plans[0]
        allocation = FbsReplenishmentAllocation.objects.get(line__plan=plan)
        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)
        other_agency = Agency.objects.create(agn_name="Другой клиент")
        WarehouseContainer.objects.create(
            agency=other_agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="OTHER-CLIENT-OCCUPIED-PALLET",
            current_location=self.free_os_location,
        )
        second_free_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=4,
            section_no=2,
            tier_no=1,
            cell_no=1,
            location_code="OS-4-2-1-1",
            display_name="OS second free cell",
            is_storage=True,
        )

        take_move_task(
            legacy_order_id=task.legacy_order_id,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        for scan in (self.source_pallet.container_code, source.container_code):
            result = scan_move_task_step(
                legacy_order_id=task.legacy_order_id,
                scan_value=scan,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            )
            self.assertTrue(result.ok, result.error)

        preassigned_result = scan_move_task_step(
            legacy_order_id=task.legacy_order_id,
            scan_value=self.cell.cell_code,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertFalse(preassigned_result.ok)
        self.assertIn("действующей топологии", preassigned_result.error)

        occupied_result = scan_move_task_step(
            legacy_order_id=task.legacy_order_id,
            scan_value=os_location_code(row=3, section=2, tier=1, cell=1),
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertFalse(occupied_result.ok)
        self.assertIn("уже занято", occupied_result.error)
        task.refresh_from_db()
        allocation.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(task.status, MoveTask.STATUS_IN_PROGRESS)
        self.assertEqual(allocation.status, FbsReplenishmentAllocation.STATUS_RESERVED)
        self.assertEqual(source.qty, 5)
        self.assertFalse(FbsStockMovement.objects.filter(allocation=allocation).exists())

        completed = scan_move_task_step(
            legacy_order_id=task.legacy_order_id,
            scan_value=os_location_code(row=4, section=2, tier=1, cell=1),
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(completed.ok, completed.error)
        self.assertTrue(completed.completed)
        plan.refresh_from_db()
        self.assertEqual(plan.target_cell.location_id, second_free_location.id)

    def test_whole_box_driver_gets_clear_error_for_regular_box_in_fbs_cell(self):
        source = self._snapshot(code="MOV-BRIDGE-BOX-REGULAR-OCCUPIED", qty=5)
        request_row = self._request(mode="box", qty=5, units_per_box=5)
        plan = self._approve_and_accept(request_row).plans[0]
        allocation = FbsReplenishmentAllocation.objects.get(line__plan=plan)
        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)
        other_agency = Agency.objects.create(agn_name="Другой клиент с обычным коробом")
        FbsStorageCell.objects.create(
            cell_code="FBS@OS-3-2-1-1",
            location=self.free_os_location,
            purpose=FbsStorageCell.PURPOSE_FLEX,
        )
        WarehouseContainer.objects.create(
            agency=other_agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="OTHER-CLIENT-OCCUPIED-BOX",
            current_location=self.free_os_location,
        )

        self.assertTrue(
            take_move_task(
                legacy_order_id=task.legacy_order_id,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            ).ok
        )
        for scan in (self.source_pallet.container_code, source.container_code):
            result = scan_move_task_step(
                legacy_order_id=task.legacy_order_id,
                scan_value=scan,
                user=self.driver,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            )
            self.assertTrue(result.ok, result.error)

        rejected = scan_move_task_step(
            legacy_order_id=task.legacy_order_id,
            scan_value=os_location_code(row=3, section=2, tier=1, cell=1),
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )

        self.assertFalse(rejected.ok)
        self.assertIn("OTHER-CLIENT-OCCUPIED-BOX", rejected.error)
        self.assertNotIn("Internal Server Error", rejected.error)
        task.refresh_from_db()
        allocation.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(task.status, MoveTask.STATUS_IN_PROGRESS)
        self.assertEqual(allocation.status, FbsReplenishmentAllocation.STATUS_RESERVED)
        self.assertEqual((source.qty, source.other_reserved_qty), (5, 5))
        self.assertFalse(FbsStockMovement.objects.filter(allocation=allocation).exists())

    def test_failed_final_scan_does_not_close_or_partially_move_fbs_task(self):
        source = self._snapshot(code="MOV-BRIDGE-ROLLBACK", qty=5)
        request_row = self._request(qty=2)
        plan = self._approve_and_accept(request_row).plans[0]
        allocation = FbsReplenishmentAllocation.objects.get(line__plan=plan)
        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)
        take_move_task(
            legacy_order_id=task.legacy_order_id,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        for scan in (
            self.source_pallet.container_code,
            source.container_code,
            self.barcode,
            self.barcode,
            movement_staging_container_code(request_row),
        ):
            self.assertTrue(
                scan_move_task_step(
                    legacy_order_id=task.legacy_order_id,
                    scan_value=scan,
                    user=self.driver,
                    employee_id=self.driver_employee.id,
                    employee_name=self.driver_employee.full_name,
                ).ok
            )
        WarehouseStockSnapshot.objects.filter(pk=source.pk).update(other_reserved_qty=1)

        result = scan_move_task_step(
            legacy_order_id=task.legacy_order_id,
            scan_value=self.staging_location.location_code,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )

        task.refresh_from_db()
        allocation.refresh_from_db()
        self.assertFalse(result.ok)
        self.assertEqual(task.status, MoveTask.STATUS_IN_PROGRESS)
        self.assertFalse((task.payload or {}).get("mobile_execution", {}).get("destination_confirmed"))
        self.assertEqual(allocation.status, FbsReplenishmentAllocation.STATUS_RESERVED)
        self.assertFalse(FbsStockBalance.objects.exists())

    def test_driver_fbs_routes_open_the_existing_reachtruck_screen(self):
        self._snapshot(code="MOV-BRIDGE-UI", qty=3)
        request_row = self._request(qty=2)
        plan = self._approve_and_accept(request_row).plans[0]
        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)
        self.client.force_login(self.driver)

        self.assertRedirects(
            self.client.get("/fbs/tsd/"),
            "/reachtruck/",
            fetch_redirect_response=False,
        )
        self.assertRedirects(
            self.client.get("/fbs/tsd/reachtruck/"),
            "/reachtruck/",
            fetch_redirect_response=False,
        )
        page = self.client.get(
            "/reachtruck/",
            {
                "mobile_category": "movement",
                "mobile_request": f"fbs:{request_row.number}",
                "mobile_task": task.legacy_order_id,
            },
        )
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, request_row.number)
        self.assertContains(page, self.source_pallet.container_code)
        self.assertContains(page, "Взять в работу")

    def test_fbs_reachtruck_screen_uses_physical_os_address_for_new_and_existing_tasks(self):
        self.general_location.zone_code = "OS"
        self.general_location.row_no = 2
        self.general_location.section_no = 7
        self.general_location.tier_no = 1
        self.general_location.cell_no = 1
        self.general_location.location_code = "OS-2-7-1-1"
        self.general_location.save(
            update_fields=[
                "zone_code",
                "row_no",
                "section_no",
                "tier_no",
                "cell_no",
                "location_code",
                "updated_at",
            ]
        )
        self._snapshot(code="MOV-BRIDGE-OS-ADDRESS", qty=3)
        request_row = self._request(qty=2)
        plan = self._approve_and_accept(request_row).plans[0]
        task = MoveTask.objects.get(payload__fbs_plan_id=plan.id)

        self.assertEqual(task.payload["source_location_scan_code"], "F-2/1-1")
        self.assertEqual(
            task.payload["from_label"],
            "OS · Линия F · Стеллаж 2 · Этаж 1 · Ячейка 1",
        )

        legacy_payload = dict(task.payload or {})
        legacy_payload["source_location_scan_code"] = "OS-2-7-1-1"
        legacy_payload["from_label"] = "OS · Ряд 2 · Секция 7 · Ярус 1 · Ячейка 1"
        task.payload = legacy_payload
        task.save(update_fields=["payload", "updated_at"])

        screen = build_mobile_execution_snapshot(task.legacy_order_id)
        self.assertEqual(screen["source_code"], "F-2/1-1")
        self.assertEqual(
            screen["source_label"],
            "OS · Линия F · Стеллаж 2 · Этаж 1 · Ячейка 1",
        )

        take_move_task(
            legacy_order_id=task.legacy_order_id,
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.client.force_login(self.driver)
        page = self.client.get(
            "/reachtruck/",
            {
                "mobile_category": "movement",
                "mobile_request": f"fbs:{request_row.number}",
                "mobile_task": task.legacy_order_id,
            },
        )
        self.assertContains(page, "Где находится товар")
        self.assertContains(
            page,
            '<div class="scan-priority-code topology">OS · Линия F · Стеллаж 2 · Этаж 1 · Ячейка 1</div>',
            html=True,
        )
        self.assertContains(
            page,
            '<div class="scan-topology-code">QR ячейки: F-2/1-1</div>',
            html=True,
        )
        self.assertContains(
            page,
            '<a class="mobile-exit-btn" href="/logout/">Выйти</a>',
            html=True,
        )

    def test_ordinary_reachtruck_task_does_not_enter_fbs_bridge(self):
        request_row = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id="ordinary-regression",
            agency=self.agency,
            destination_zone="STORAGE",
        )
        task = MoveTask.objects.create(
            request=request_row,
            pallet_code="ORDINARY-PALLET",
            from_zone="STORAGE",
            to_zone="STORAGE",
            payload={
                "pallet_code": "ORDINARY-PALLET",
                "from_location": {"zone": "STORAGE", "row": 41, "section": 1, "tier": 2, "cell": 1},
                "to_location": {"zone": "STORAGE", "row": 41, "section": 2, "tier": 1, "cell": 1},
            },
            legacy_order_id="ORDINARY-REGRESSION",
        )

        with patch(
            "fbs.services.reachtruck_bridge.build_fbs_mobile_execution_snapshot"
        ) as fbs_snapshot:
            build_mobile_execution_snapshot(task.legacy_order_id)

        fbs_snapshot.assert_not_called()
