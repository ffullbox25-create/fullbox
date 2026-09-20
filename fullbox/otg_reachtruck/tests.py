from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models
from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.utils import timezone

from employees.models import Employee
from reachtruck.models import BoxClaim, MoveRequest, MoveRequestItem, MoveTask
from reachtruck.services.move_requests import sync_task_status_by_legacy_order_id
from reachtruck.services.task_commands import build_mobile_execution_snapshot
from shipping.box_splits import encode_partial_box_split
from shipping.discrepancy import shipping_discrepancy_snapshot
from shipping.models import ShippingOrder, ShippingOrderItem
from shipping.services import create_pick_tasks, shipping_pick_readiness
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseOperationTask,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sku.models import Agency, SKU, SKUBarcode
from todo.models import Task

from .models import OtgDeliveryDemand, OtgDeliveryRequest, OtgPalletPlan
from .execution import (
    MoveTaskCommandResult,
    _append_otg_remaining_box_hint,
    _canonicalize_otg_unit_scan,
    _select_otg_request_task,
    _otg_task_temporarily_blocked_by_foreign_claim,
    build_otg_mobile_execution_snapshot,
    build_otg_mobile_request_execution_snapshot,
    complete_otg_move_task,
    confirm_otg_move_task_unit_quantity,
    report_otg_no_stock,
    report_otg_unit_shortage,
    release_stale_otg_move_request_task,
    take_otg_move_request,
    take_selected_otg_move_request_task,
)
from .reserve_swap import prepare_otg_box_scan
from .services import (
    _cancel_preemptible_storage_routes_for_otg,
    _claim_free_otg_boxes_for_order,
    _merge_pick_task_payloads,
    _payload_for_plan,
    _validate_otg_move_payload_quantities,
    build_box_demands,
    create_otg_shipping_pick_request,
    create_otg_shipping_discrepancy_pick,
    create_otg_shipping_supplemental_pick,
    get_otg_confirmed_shortage,
    get_otg_discrepancy_pick_preview,
    get_otg_supplemental_pick_preview,
    preview_otg_shipping_pick_coverage,
    rebind_unavailable_shipping_source_boxes,
    retry_waiting_otg_shipping_requests,
    sync_completed_otg_delivery_request,
    _retry_waiting_otg_request,
    ensure_waiting_otg_shipping_pick_request,
)
from .views import _build_route_groups


class OtgReachtruckPlannerTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="otg_manager", password="pwd")
        self.agency = Agency.objects.create(agn_name="OTG Client")
        self.order = ShippingOrder.objects.create(
            number="SO-OTG-001",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
        )

    def test_otg_unit_scan_uses_canonical_task_barcode_case(self):
        payload = {
            "requested_barcodes": ["ozn5630635792"],
            "requested_barcode_qty": {"ozn5630635792": 46},
        }

        self.assertEqual(
            _canonicalize_otg_unit_scan(payload, "OZN5630635792"),
            "ozn5630635792",
        )

    def test_otg_unit_scan_does_not_replace_a_different_barcode(self):
        payload = {
            "requested_rows": [
                {"barcode_qty": {"ozn5630635792": 46}},
            ],
        }

        self.assertEqual(
            _canonicalize_otg_unit_scan(payload, "OZM5630635792"),
            "OZM5630635792",
        )

    def test_otg_unit_scan_fails_closed_for_case_ambiguous_task_barcodes(self):
        payload = {
            "requested_barcodes": ["ozn5630635792", "OZN5630635792"],
        }

        self.assertEqual(
            _canonicalize_otg_unit_scan(payload, "Ozn5630635792"),
            "Ozn5630635792",
        )

    def test_otg_unit_scan_accepts_physical_barcode_for_same_client_sku(self):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="091124_grayboardbear",
            name="Мольберт детский",
        )
        SKUBarcode.objects.create(
            sku=sku,
            value="4621320255015",
            is_primary=True,
        )
        payload = {
            "requested_sku": "091124_grayboardbear",
            "requested_barcodes": ["ozn1769869289"],
            "requested_barcode_qty": {"ozn1769869289": 14},
        }

        self.assertEqual(
            _canonicalize_otg_unit_scan(
                payload,
                "4621320255015",
                agency_id=self.agency.id,
            ),
            "ozn1769869289",
        )

    def test_otg_unit_scan_rejects_physical_barcode_for_different_sku(self):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="OTHER-SKU",
            name="Другой товар",
        )
        SKUBarcode.objects.create(
            sku=sku,
            value="4621320255016",
            is_primary=True,
        )
        payload = {
            "requested_sku": "091124_grayboardbear",
            "requested_barcodes": ["ozn1769869289"],
            "requested_barcode_qty": {"ozn1769869289": 14},
        }

        self.assertEqual(
            _canonicalize_otg_unit_scan(
                payload,
                "4621320255016",
                agency_id=self.agency.id,
            ),
            "4621320255016",
        )

    def test_otg_unit_scan_rejects_physical_barcode_for_different_client(self):
        another_agency = Agency.objects.create(agn_name="Another OTG Client")
        sku = SKU.objects.create(
            agency=another_agency,
            sku_code="091124_grayboardbear",
            name="Мольберт другого клиента",
        )
        SKUBarcode.objects.create(
            sku=sku,
            value="4621320255017",
            is_primary=True,
        )
        payload = {
            "requested_sku": "091124_grayboardbear",
            "requested_barcodes": ["ozn1769869289"],
            "requested_barcode_qty": {"ozn1769869289": 14},
        }

        self.assertEqual(
            _canonicalize_otg_unit_scan(
                payload,
                "4621320255017",
                agency_id=self.agency.id,
            ),
            "4621320255017",
        )

    def _create_box(
        self,
        *,
        pallet_code: str,
        box_code: str,
        sku: str = "SKU-OTG",
        name: str = "OTG Item",
        size: str = "42",
        barcode: str = "460000000001",
        goods_type: str = "Ready",
        qty: int = 10,
        row: int = 1,
        section: int = 1,
        tier: int = 1,
        cell: int = 1,
        zone: str = "OS",
        warehouse_state_code: str = "stored",
    ) -> WarehouseStockSnapshot:
        location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code=zone,
            row_no=row,
            section_no=section,
            tier_no=tier,
            cell_no=cell,
        )
        pallet, _ = WarehouseContainer.objects.get_or_create(
            agency=self.agency,
            container_code=pallet_code,
            defaults={
                "container_type": WarehouseContainer.TYPE_PALLET,
                "current_location": location,
            },
        )
        if pallet.current_location_id != location.id:
            pallet.current_location = location
            pallet.save(update_fields=["current_location", "updated_at"])
        box, _ = WarehouseContainer.objects.get_or_create(
            agency=self.agency,
            container_code=box_code,
            defaults={
                "container_type": WarehouseContainer.TYPE_BOX,
                "parent_container": pallet,
                "current_location": location,
            },
        )
        changed_fields = []
        if box.parent_container_id != pallet.id:
            box.parent_container = pallet
            changed_fields.append("parent_container")
        if box.current_location_id != location.id:
            box.current_location = location
            changed_fields.append("current_location")
        if changed_fields:
            box.save(update_fields=[*changed_fields, "updated_at"])
        return WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="R-OTG",
            sku_code=sku,
            name=name,
            size=size,
            barcode=barcode,
            goods_type=goods_type,
            qty=qty,
            available_qty=qty,
            container=box,
            container_code=box.container_code,
            parent_container=pallet,
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code=warehouse_state_code,
        )

    def _create_otg_destination(self) -> WarehouseLocation:
        return WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OTG",
            zone_kind=WarehouseLocation.ZONE_KIND_SHIPPING,
            location_code="OTG-GATE-01",
            display_name="OTG · Ворота 01",
            capacity_containers=100,
            is_topology_visible=False,
            is_active=True,
            is_pickable=True,
            is_shipping=True,
        )

    def _create_pallet_with_boxes(self, pallet_code: str, *, boxes: int = 10) -> None:
        for index in range(1, boxes + 1):
            self._create_box(
                pallet_code=pallet_code,
                box_code=f"{pallet_code}-BX-{index:02d}",
                row=int(pallet_code.rsplit("-", 1)[-1]),
            )

    def _create_marked_shipping_source(
        self,
        *,
        marking_code: str,
        pallet_code: str = "PAL-MARKED",
        box_code: str = "BOX-MARKED",
    ) -> WarehouseStockSnapshot:
        self.order.delivery_type = ShippingOrder.DELIVERY_MARKETPLACE
        self.order.save(update_fields=["delivery_type", "updated_at"])
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-MARKED",
            name="Marked item",
            size="M",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=1,
        )
        source = self._create_box(
            pallet_code=pallet_code,
            box_code=box_code,
            sku="SKU-MARKED",
            name="Marked item",
            size="M",
            barcode="460000000001",
            goods_type="Ready",
            qty=1,
        )
        source.marking_code = marking_code
        source.save(update_fields=["marking_code", "updated_at"])
        return source

    def test_partial_marking_scan_returns_matching_source_snapshot(self):
        marking_code = "0104610469110859215SOURCE1\x1d91EE11\x1d92SIGNATURE"
        source = self._create_marked_shipping_source(marking_code=marking_code)

        result = WarehouseWritePathService.validate_partial_shipping_marking_scan(
            agency=self.agency,
            order_id=self.order.number,
            source_box_code="BOX-MARKED",
            source_pallet_code="PAL-MARKED",
            barcode="460000000001",
            marking_code=marking_code,
        )

        self.assertEqual(result["snapshot_id"], source.id)
        self.assertEqual(result["marking_code"], marking_code)

    def test_partial_marking_scan_rejects_product_barcode_prefix(self):
        marking_code = "0104610469110859215SOURCE2\x1d91EE11\x1d92SIGNATURE"
        self._create_marked_shipping_source(marking_code=marking_code)

        with self.assertRaisesMessage(ValueError, "Перед Data Matrix считан линейный штрихкод"):
            WarehouseWritePathService.validate_partial_shipping_marking_scan(
                agency=self.agency,
                order_id=self.order.number,
                source_box_code="BOX-MARKED",
                source_pallet_code="PAL-MARKED",
                barcode="460000000001",
                marking_code=f"460000000001{marking_code}",
            )

    def test_partial_marking_scan_rejects_previously_shipped_unit(self):
        self._create_marked_shipping_source(
            marking_code="0104610469110859215SOURCE3\x1d91EE11\x1d92SIGNATURE",
        )
        shipped_code = "0104610469110859215SHIPPED\x1d91EE11\x1d92SIGNATURE"
        shipped = self._create_box(
            pallet_code="PAL-SHIPPED",
            box_code="BOX-SHIPPED",
            sku="SKU-MARKED",
            name="Marked item",
            size="M",
            barcode="460000000001",
            goods_type="Ready",
            qty=1,
        )
        shipped.marking_code = shipped_code
        shipped.available_qty = 0
        shipped.warehouse_state_code = "shipped"
        shipped.is_archived = True
        shipped.save(
            update_fields=[
                "marking_code",
                "available_qty",
                "warehouse_state_code",
                "is_archived",
                "updated_at",
            ]
        )

        with self.assertRaisesMessage(ValueError, "уже был отгружен"):
            WarehouseWritePathService.validate_partial_shipping_marking_scan(
                agency=self.agency,
                order_id=self.order.number,
                source_box_code="BOX-MARKED",
                source_pallet_code="PAL-MARKED",
                barcode="460000000001",
                marking_code=shipped_code,
            )

    def test_partial_marking_scan_rejects_unit_from_another_active_box(self):
        self._create_marked_shipping_source(
            marking_code="0104610469110859215SOURCE4\x1d91EE11\x1d92SIGNATURE",
        )
        other_code = "0104610469110859215OTHER01\x1d91EE11\x1d92SIGNATURE"
        other = self._create_box(
            pallet_code="PAL-OTHER",
            box_code="BOX-OTHER",
            sku="SKU-MARKED",
            name="Marked item",
            size="M",
            barcode="460000000001",
            goods_type="Ready",
            qty=1,
        )
        other.marking_code = other_code
        other.save(update_fields=["marking_code", "updated_at"])

        with self.assertRaisesMessage(ValueError, "другой активной складской единицей"):
            WarehouseWritePathService.validate_partial_shipping_marking_scan(
                agency=self.agency,
                order_id=self.order.number,
                source_box_code="BOX-MARKED",
                source_pallet_code="PAL-MARKED",
                barcode="460000000001",
                marking_code=other_code,
            )

    def test_partial_marking_scan_rejects_unknown_unit_for_fully_marked_box(self):
        self._create_marked_shipping_source(
            marking_code="0104610469110859215SOURCE5\x1d91EE11\x1d92SIGNATURE",
        )

        with self.assertRaisesMessage(ValueError, "не относится к активному товару в коробе"):
            WarehouseWritePathService.validate_partial_shipping_marking_scan(
                agency=self.agency,
                order_id=self.order.number,
                source_box_code="BOX-MARKED",
                source_pallet_code="PAL-MARKED",
                barcode="460000000001",
                marking_code="0104610469110859215UNKNOWN\x1d91EE11\x1d92SIGNATURE",
            )

    def test_partial_marking_scan_keeps_legacy_unmarked_source_fallback(self):
        self._create_marked_shipping_source(
            marking_code="0104610469110859215CATALOG\x1d91EE11\x1d92SIGNATURE",
            pallet_code="PAL-CATALOG",
            box_code="BOX-CATALOG",
        )
        self._create_box(
            pallet_code="PAL-LEGACY",
            box_code="BOX-LEGACY",
            sku="SKU-MARKED",
            name="Marked item",
            size="M",
            barcode="460000000001",
            goods_type="Ready",
            qty=1,
        )

        result = WarehouseWritePathService.validate_partial_shipping_marking_scan(
            agency=self.agency,
            order_id=self.order.number,
            source_box_code="BOX-LEGACY",
            source_pallet_code="PAL-LEGACY",
            barcode="460000000001",
            marking_code="0104610469110859215LEGACY1\x1d91EE11\x1d92SIGNATURE",
        )

        self.assertEqual(result["snapshot_id"], 0)

    @patch("otg_reachtruck.execution._adopt_active_otg_task")
    @patch("otg_reachtruck.execution.shared_execution.confirm_move_task_unit_quantity")
    def test_manual_unit_quantity_uses_shared_protected_command(
        self,
        confirm_quantity_mock,
        adopt_task_mock,
    ):
        expected_result = object()
        confirm_quantity_mock.return_value = expected_result

        result = confirm_otg_move_task_unit_quantity(
            legacy_order_id="OTG-MANUAL-QTY-1",
            unit_quantity="7",
            user=self.user,
            employee_id=17,
            employee_name="Водитель OTG",
        )

        self.assertIs(result, expected_result)
        adopt_task_mock.assert_called_once_with(
            "OTG-MANUAL-QTY-1",
            user=self.user,
            employee_id=17,
            employee_name="Водитель OTG",
        )
        confirm_quantity_mock.assert_called_once_with(
            legacy_order_id="OTG-MANUAL-QTY-1",
            unit_quantity="7",
            user=self.user,
            employee_id=17,
            employee_name="Водитель OTG",
        )

    def test_unit_shortage_moves_actual_qty_and_sends_missing_qty_to_verification(self):
        driver = Employee.objects.create(
            full_name="Водитель OTG",
            role="reachtruck_driver",
            user=self.user,
        )
        Employee.objects.create(
            full_name="Начальник склада",
            role="head_manager",
        )
        shipping_item = ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-SHORT",
            name="Штучный товар",
            size="M",
            barcode="460000000009",
            goods_type="Ready",
            qty_requested=9,
            comment="Коробов: 1; кратность: 9",
        )
        source = self._create_box(
            pallet_code="PAL-SHORT",
            box_code="BOX-SHORT",
            sku="SKU-SHORT",
            name="Штучный товар",
            size="M",
            barcode="460000000009",
            goods_type="Ready",
            qty=9,
            row=4,
            section=3,
            tier=1,
            cell=2,
        )
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id=str(self.order.pk),
            agency=self.agency,
            requested_by=self.user,
            requested_by_role="storekeeper",
            requested_by_name="Кладовщик",
            destination_zone="OTG",
            status=MoveRequest.STATUS_IN_PROGRESS,
        )
        payload = {
            "otg_scan_fact_mode": "scan_facts_v1",
            "shipping_order_id": self.order.number,
            "shipping_order_pk": self.order.pk,
            "pallet_code": "PAL-SHORT",
            "from_location": {
                "zone": "OS",
                "row": 4,
                "section": 3,
                "tier": 1,
                "cell": 2,
            },
            "from_label": "OS · Линия F · Стеллаж 4 · Этаж 3 · Ячейка 2",
            "to_location": {"zone": "OTG", "row": "", "section": "", "tier": "", "cell": ""},
            "to_label": "OTG · Зона отгрузки",
            "move_mode": MoveTask.MODE_BOX_PARTIAL,
            "pick_mode": "partial",
            "ship_as_loose_units": True,
            "requested_sku": "SKU-SHORT",
            "requested_goods_type": "Ready",
            "requested_barcodes": ["460000000009"],
            "requested_barcode_qty": {"460000000009": 9},
            "requested_qty": 9,
            "available_qty": 9,
            "requested_box_selection": "fixed",
            "requested_box_count": 1,
            "requested_box": "BOX-SHORT",
            "requested_boxes": ["BOX-SHORT"],
            "planned_box_codes": ["BOX-SHORT"],
            "selected_box_codes": ["BOX-SHORT"],
            "reserved_box_codes": [],
            "requested_rows": [
                {
                    "box_code": "BOX-SHORT",
                    "qty": 9,
                    "barcode_qty": {"460000000009": 9},
                }
            ],
            "partial_pick_patterns": [
                {
                    "source_box_qty": 9,
                    "requested_box_count": 1,
                    "source_barcode_qty": {"460000000009": 9},
                    "barcode_qty": {"460000000009": 9},
                    "pick_qty": 9,
                    "requested_article": "SKU-SHORT",
                    "requested_goods_type": "Ready",
                    "requested_barcodes": ["460000000009"],
                }
            ],
            "requested_box_patterns": [
                {
                    "box_qty": 9,
                    "barcode_qty": {"460000000009": 9},
                    "requested_article": "SKU-SHORT",
                    "requested_goods_type": "Ready",
                    "requested_barcodes": ["460000000009"],
                    "requested_box_count": 1,
                }
            ],
            "request_items": [
                {"shipping_item_id": shipping_item.id, "requested_qty": 9}
            ],
            "route_plan": {"boxes_to_pick": 1, "qty_to_pick": 9},
            "mobile_execution": {
                "source_confirmed": True,
                "pallet_confirmed": True,
                "destination_confirmed": False,
                "boxes_scanned": ["BOX-SHORT"],
                "units_scanned": {"BOX-SHORT": {"460000000009": 8}},
                "last_scan": "460000000009",
            },
            "mobile_placement_source": {
                "order_id": "R-OTG",
                "order_type": "receiving",
                "source_kind": "warehouse_stock",
                "agency_id": self.agency.id,
            },
            "mobile_placement_payload": {
                "act": "placement",
                "act_state": "closed",
                "act_items_removed": True,
                "act_pallets": [
                    {
                        "code": "PAL-SHORT",
                        "boxes": ["BOX-SHORT"],
                        "items": [],
                        "location": {
                            "zone": "OS",
                            "row": 4,
                            "section": 3,
                            "tier": 1,
                            "cell": 2,
                        },
                    }
                ],
                "act_boxes": [
                    {
                        "code": "BOX-SHORT",
                        "items": [
                            {
                                "sku": "SKU-SHORT",
                                "sku_code": "SKU-SHORT",
                                "name": "Штучный товар",
                                "size": "M",
                                "barcode": "460000000009",
                                "goods_type": "Ready",
                                "qty": 9,
                            }
                        ],
                    }
                ],
            },
            "assigned_to_id": driver.id,
            "assigned_to_name": driver.full_name,
            "status": MoveTask.STATUS_IN_PROGRESS,
        }
        task = MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-SHORT",
            from_zone="OS",
            from_row=4,
            from_section=3,
            from_tier=1,
            from_cell=2,
            to_zone="OTG",
            move_mode=MoveTask.MODE_BOX_PARTIAL,
            qty_planned=9,
            status=MoveTask.STATUS_IN_PROGRESS,
            assigned_to=self.user,
            assigned_to_name=driver.full_name,
            legacy_order_id="OTG-UNIT-SHORT-1",
            started_at=timezone.now(),
            payload=payload,
        )

        before = build_otg_mobile_execution_snapshot(task.legacy_order_id)
        self.assertEqual(before["current_step"], "units")
        self.assertEqual(before["unit_shortage_entry"]["missing_qty"], 1)

        result = report_otg_unit_shortage(
            legacy_order_id=task.legacy_order_id,
            user=self.user,
            employee_id=driver.id,
            employee_name=driver.full_name,
        )

        self.assertTrue(result.ok, result.error)
        source.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual((source.qty, source.available_qty), (8, 8))
        self.assertEqual(task.qty_planned, 9)
        self.assertEqual(task.payload["requested_qty"], 8)
        self.assertEqual(task.payload["requested_rows"][0]["qty"], 8)
        self.assertEqual(task.payload["partial_pick_patterns"][0]["pick_qty"], 8)
        self.assertEqual(task.payload["unit_shortage_reports"][0]["missing_qty"], 1)
        event = WarehouseEvent.objects.get(
            event_type="stock_corrected",
            source_document_type="reachtruck_move",
            source_document_id=str(task.id),
        )
        self.assertEqual(event.qty, 1)
        operation = WarehouseOperation.objects.get(
            context_type="otg_unit_shortage_check",
            context_id=str(task.id),
        )
        self.assertEqual(operation.status, WarehouseOperation.STATUS_BLOCKED)
        self.assertEqual(operation.planned_qty, 1)
        verification = Task.objects.get(title="СРОЧНО: штучная недостача при отборе OTG")
        self.assertEqual(verification.priority, "urgent")
        self.assertIn("BOX-SHORT", verification.description)

        after = build_otg_mobile_execution_snapshot(task.legacy_order_id)
        self.assertEqual(after["current_step"], "destination")
        self.assertTrue(after["all_boxes_complete"])
        self.assertEqual(after["unit_shortage_notice"]["missing_qty"], 1)

        repeated = report_otg_unit_shortage(
            legacy_order_id=task.legacy_order_id,
            user=self.user,
            employee_id=driver.id,
            employee_name=driver.full_name,
        )
        self.assertTrue(repeated.ok)
        self.assertEqual(
            WarehouseEvent.objects.filter(
                event_type="stock_corrected",
                source_document_type="reachtruck_move",
                source_document_id=str(task.id),
            ).count(),
            1,
        )
        self.assertEqual(
            Task.objects.filter(title="СРОЧНО: штучная недостача при отборе OTG").count(),
            1,
        )

        task.refresh_from_db()
        completed_payload = dict(task.payload or {})
        completed_execution = dict(completed_payload.get("mobile_execution") or {})
        completed_execution["destination_confirmed"] = True
        completed_execution["last_scan"] = "OTG"
        completed_payload["mobile_execution"] = completed_execution
        task.payload = completed_payload
        task.save(update_fields=["payload", "updated_at"])
        completed = complete_otg_move_task(
            legacy_order_id=task.legacy_order_id,
            user=self.user,
            employee_id=driver.id,
            employee_name=driver.full_name,
            require_scan_confirmation=True,
        )
        self.assertTrue(completed.ok, completed.error)
        source.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(task.status, MoveTask.STATUS_DONE)
        self.assertEqual(task.qty_done, 8)
        self.assertEqual(source.qty, 0)
        self.assertTrue(source.is_archived)
        self.assertEqual(
            WarehouseStockSnapshot.objects.filter(
                agency=self.agency,
                source_context_type="shipping",
                source_context_id=self.order.number,
                barcode="460000000009",
                zone_code="OTG",
                is_archived=False,
            ).aggregate(total=models.Sum("qty"))["total"],
            8,
        )

    def test_unit_shortage_write_path_rejects_processing_reserved_stock(self):
        source = self._create_box(
            pallet_code="PAL-SHORT-BLOCKED",
            box_code="BOX-SHORT-BLOCKED",
            sku="SKU-SHORT",
            barcode="460000000019",
            qty=9,
        )
        source.processing_reserved_qty = 1
        source.available_qty = 8
        source.save(
            update_fields=["processing_reserved_qty", "available_qty", "updated_at"]
        )

        with self.assertRaisesMessage(ValueError, "резерв обработки"):
            WarehouseWritePathService.report_otg_unit_shortage(
                agency=self.agency,
                order_id=self.order.number,
                move_task_id="blocked-processing-reserve",
                source_pallet_code="PAL-SHORT-BLOCKED",
                source_box_code="BOX-SHORT-BLOCKED",
                barcode="460000000019",
                expected_qty=9,
                actual_qty=8,
                performed_by=self.user,
            )

        source.refresh_from_db()
        self.assertEqual(
            (source.qty, source.available_qty, source.processing_reserved_qty),
            (9, 8, 1),
        )
        self.assertFalse(
            WarehouseOperation.objects.filter(
                context_type="otg_unit_shortage_check",
                context_id="blocked-processing-reserve",
            ).exists()
        )
        self.assertFalse(
            WarehouseEvent.objects.filter(
                source_document_type="reachtruck_move",
                source_document_id="blocked-processing-reserve",
            ).exists()
        )

    @staticmethod
    def _strict_scan_payload(box_code: str, qty: int) -> dict:
        return {
            "otg_scan_fact_mode": "scan_facts_v1",
            "picked_boxes": [box_code],
            "picked_qty": qty,
            "mobile_execution": {
                "boxes_scanned": [box_code],
                "destination_confirmed": True,
            },
        }

    def test_confirmed_supplement_closes_internal_otg_requests_without_changing_order(self):
        source_move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id=str(self.order.pk),
            agency=self.agency,
            destination_zone="OTG",
            status=MoveRequest.STATUS_DONE,
        )
        MoveTask.objects.create(
            request=source_move_request,
            pallet_code="PAL-SOURCE",
            from_zone="OS",
            to_zone="OTG",
            qty_planned=10,
            qty_done=10,
            status=MoveTask.STATUS_DONE,
            payload=self._strict_scan_payload("BOX-SOURCE", 10),
        )
        source_request = OtgDeliveryRequest.objects.create(
            shipping_order=self.order,
            agency=self.agency,
            move_request=source_move_request,
            status=OtgDeliveryRequest.STATUS_PARTIAL,
            requested_boxes=2,
            planned_boxes=1,
            shortage_boxes=1,
        )
        supplemental_move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id=str(self.order.pk),
            agency=self.agency,
            destination_zone="OTG",
            status=MoveRequest.STATUS_PLANNED,
        )
        supplemental_task = MoveTask.objects.create(
            request=supplemental_move_request,
            pallet_code="PAL-SUPPLEMENT",
            from_zone="OS",
            to_zone="OTG",
            qty_planned=10,
            qty_done=0,
            status=MoveTask.STATUS_IN_PROGRESS,
            legacy_order_id="STRICT-SUPPLEMENT-1",
            payload=self._strict_scan_payload("BOX-SUPPLEMENT", 10),
        )
        supplemental_request = OtgDeliveryRequest.objects.create(
            shipping_order=self.order,
            agency=self.agency,
            move_request=supplemental_move_request,
            status=OtgDeliveryRequest.STATUS_DISPATCHED,
            requested_boxes=1,
            planned_boxes=1,
            shortage_boxes=0,
            payload={
                "request_reason": "shipping_supplement_pick",
                "supplemental_pick": {"source_request_id": source_request.pk},
            },
        )
        order_before = (self.order.status, self.order.expected_boxes)

        sync_task_status_by_legacy_order_id(
            supplemental_task.legacy_order_id,
            status=MoveTask.STATUS_DONE,
            qty_done=10,
        )

        source_request.refresh_from_db()
        supplemental_request.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(source_request.status, OtgDeliveryRequest.STATUS_DONE)
        self.assertEqual(supplemental_request.status, OtgDeliveryRequest.STATUS_DONE)
        self.assertEqual(source_request.shortage_boxes, 1)
        self.assertEqual((self.order.status, self.order.expected_boxes), order_before)

    def test_internal_otg_request_stays_open_without_destination_scan_fact(self):
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id=str(self.order.pk),
            agency=self.agency,
            destination_zone="OTG",
            status=MoveRequest.STATUS_DONE,
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-NO-FACT",
            from_zone="OS",
            to_zone="OTG",
            qty_planned=10,
            qty_done=10,
            status=MoveTask.STATUS_DONE,
            payload={
                "otg_scan_fact_mode": "scan_facts_v1",
                "picked_boxes": ["BOX-NO-FACT"],
                "picked_qty": 10,
                "mobile_execution": {"destination_confirmed": False},
            },
        )
        otg_request = OtgDeliveryRequest.objects.create(
            shipping_order=self.order,
            agency=self.agency,
            move_request=move_request,
            status=OtgDeliveryRequest.STATUS_DISPATCHED,
            requested_boxes=1,
            planned_boxes=1,
        )

        self.assertEqual(sync_completed_otg_delivery_request(move_request), [])
        otg_request.refresh_from_db()
        self.assertEqual(otg_request.status, OtgDeliveryRequest.STATUS_DISPATCHED)

    def test_internal_otg_request_closes_with_nested_partial_unit_scan_facts(self):
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id=str(self.order.pk),
            agency=self.agency,
            destination_zone="OTG",
            status=MoveRequest.STATUS_DONE,
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-PARTIAL-FACT",
            from_zone="OS",
            to_zone="OTG",
            qty_planned=50,
            qty_done=50,
            status=MoveTask.STATUS_DONE,
            payload={
                "otg_scan_fact_mode": "scan_facts_v1",
                "picked_qty": 50,
                "mobile_execution": {
                    "boxes_scanned": ["BOX-PARTIAL-FACT"],
                    "units_scanned": {
                        "BOX-PARTIAL-FACT": {"2050756815777": 50},
                    },
                    "destination_confirmed": True,
                },
            },
        )
        otg_request = OtgDeliveryRequest.objects.create(
            shipping_order=self.order,
            agency=self.agency,
            move_request=move_request,
            status=OtgDeliveryRequest.STATUS_DISPATCHED,
            requested_boxes=1,
            planned_boxes=1,
        )

        self.assertEqual(sync_completed_otg_delivery_request(move_request), [otg_request.id])
        otg_request.refresh_from_db()
        self.assertEqual(otg_request.status, OtgDeliveryRequest.STATUS_DONE)

    def _claim_queue_payload(
        self,
        *,
        pallet_code: str,
        status: str = MoveTask.STATUS_CREATED,
        employee_id: int | None = None,
        box_qty: int = 10,
    ) -> dict:
        payload = {
            "status": status,
            "status_label": "В работе" if status == MoveTask.STATUS_IN_PROGRESS else "Создано",
            "pallet_code": pallet_code,
            "move_mode": MoveTask.MODE_BOX_FULL,
            "requested_box_selection": "pattern_matching",
            "requested_box_count": 1,
            "requested_qty": box_qty,
            "requested_barcodes": ["460000000001"],
            "requested_sku": "SKU-OTG",
            "requested_goods_type": "Ready",
            "requested_box_patterns": [
                {
                    "requested_box_count": 1,
                    "box_qty": box_qty,
                    "barcode_qty": {"460000000001": box_qty},
                    "requested_barcodes": ["460000000001"],
                    "requested_article": "SKU-OTG",
                    "requested_goods_type": "Ready",
                }
            ],
            "to_location": {"zone": "OTG"},
            "destination_code": "OTG",
            "otg_delivery_request_id": 1,
            "otg_scan_fact_mode": "scan_facts_v1",
            "mobile_execution": {},
        }
        if employee_id is not None:
            payload["assigned_to_id"] = employee_id
            payload["assigned_to_name"] = f"Driver {employee_id}"
        return payload

    def _create_claim_queue_task(
        self,
        *,
        move_request: MoveRequest,
        legacy_order_id: str,
        pallet_code: str,
        status: str = MoveTask.STATUS_CREATED,
        employee_id: int | None = None,
        box_qty: int = 10,
    ) -> MoveTask:
        return MoveTask.objects.create(
            request=move_request,
            pallet_code=pallet_code,
            from_zone="OS",
            to_zone="OTG",
            move_mode=MoveTask.MODE_BOX_FULL,
            qty_planned=box_qty,
            status=status,
            assigned_to=self.user if status == MoveTask.STATUS_IN_PROGRESS else None,
            assigned_to_name=f"Driver {employee_id}" if employee_id is not None else "",
            legacy_order_id=legacy_order_id,
            payload=self._claim_queue_payload(
                pallet_code=pallet_code,
                status=status,
                employee_id=employee_id,
                box_qty=box_qty,
            ),
        )

    def _create_claim_queue_request(self, context_id: str = "OTG-CLAIM-QUEUE") -> MoveRequest:
        return MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id=context_id,
            agency=self.agency,
            requested_by=self.user,
            requested_by_name="OTG Manager",
            requested_by_role="manager",
            destination_zone="OTG",
        )

    @override_settings(USE_OTG_REACHTRUCK_PLANNER=True)
    def test_shipping_pick_readiness_requires_full_coverage_without_stock_writes(self):
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=70,
            comment="Коробов: 7; кратность: 10",
        )
        self._create_pallet_with_boxes("PAL-01", boxes=6)
        before = list(
            WarehouseStockSnapshot.objects.order_by("id").values_list(
                "id",
                "qty",
                "available_qty",
                "warehouse_state_code",
            )
        )

        preview = preview_otg_shipping_pick_coverage(self.order)
        readiness = shipping_pick_readiness(self.order)

        self.assertFalse(preview["can_cover"])
        self.assertEqual(preview["shortage_boxes"], 1)
        self.assertEqual(preview["shortage_qty"], 10)
        self.assertFalse(readiness["can_pick"])
        self.assertIn("10 шт.", readiness["reason"])
        with self.assertRaisesMessage(ValidationError, "Нельзя создать полный подбор"):
            create_pick_tasks(self.order, self.user)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, ShippingOrder.STATUS_STOREKEEPER_ACCEPTED)
        self.assertFalse(OtgDeliveryRequest.objects.exists())
        self.assertFalse(MoveRequest.objects.exists())
        self.assertFalse(MoveTask.objects.exists())
        self.assertEqual(
            list(
                WarehouseStockSnapshot.objects.order_by("id").values_list(
                    "id",
                    "qty",
                    "available_qty",
                    "warehouse_state_code",
                )
            ),
            before,
        )

    @override_settings(USE_OTG_REACHTRUCK_PLANNER=True)
    def test_waiting_request_is_not_confirmed_shortage_and_can_retry(self):
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=10,
            comment="Коробов: 1; кратность: 10",
        )
        waiting_request = OtgDeliveryRequest.objects.create(
            shipping_order=self.order,
            agency=self.agency,
            requested_by=self.user,
            requested_by_name="Storekeeper",
            requested_by_role="storekeeper",
            status=OtgDeliveryRequest.STATUS_BLOCKED,
            requested_boxes=1,
            planned_boxes=0,
            shortage_boxes=1,
            payload={
                "waiting_for_stock": True,
                "auto_retry": True,
                "waiting_reason": "pallet_or_reserve_temporarily_unavailable",
            },
        )

        self.assertEqual(get_otg_confirmed_shortage(self.order)["shortage_boxes"], 0)
        readiness = shipping_pick_readiness(self.order)
        self.assertFalse(readiness["can_pick"])
        self.assertIn("10 шт.", readiness["reason"])
        self.assertFalse(get_otg_supplemental_pick_preview(self.order)["can_create"])

        self._create_pallet_with_boxes("PAL-01", boxes=1)
        self.assertTrue(shipping_pick_readiness(self.order)["can_pick"])

        with patch("shipping.services.create_pick_tasks", return_value=["MOVE-1"]) as create_tasks:
            self.assertEqual(_retry_waiting_otg_request(waiting_request.id), ["MOVE-1"])

        create_tasks.assert_called_once()

    @override_settings(USE_OTG_REACHTRUCK_PLANNER=True)
    def test_existing_waiting_request_refreshes_visible_reason(self):
        waiting_request = OtgDeliveryRequest.objects.create(
            shipping_order=self.order,
            agency=self.agency,
            requested_by=self.user,
            status=OtgDeliveryRequest.STATUS_BLOCKED,
            planning_error="Старая причина",
            payload={
                "waiting_for_stock": True,
                "auto_retry": True,
                "waiting_message": "Старая причина",
            },
        )

        refreshed = ensure_waiting_otg_shipping_pick_request(
            order=self.order,
            user=self.user,
            waiting_message="Нельзя создать полный подбор: не хватает 40 шт.",
        )

        waiting_request.refresh_from_db()
        self.assertEqual(refreshed.id, waiting_request.id)
        self.assertEqual(
            waiting_request.planning_error,
            "Нельзя создать полный подбор: не хватает 40 шт.",
        )
        self.assertEqual(
            waiting_request.payload["waiting_message"],
            "Нельзя создать полный подбор: не хватает 40 шт.",
        )

    @override_settings(USE_OTG_REACHTRUCK_PLANNER=True)
    def test_retry_waiting_requests_prefers_manual_urgent_order(self):
        storekeeper = Employee.objects.create(
            full_name="Priority Storekeeper",
            role="storekeeper",
            user=self.user,
            is_active=True,
        )
        normal_order = ShippingOrder.objects.create(
            number="SO-OTG-NORMAL",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
            planned_ship_date=timezone.localdate(),
        )
        urgent_order = ShippingOrder.objects.create(
            number="SO-OTG-URGENT",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
            planned_ship_date=timezone.localdate() + timedelta(days=1),
        )
        normal_request = OtgDeliveryRequest.objects.create(
            shipping_order=normal_order,
            agency=self.agency,
            status=OtgDeliveryRequest.STATUS_BLOCKED,
            payload={"waiting_for_stock": True, "auto_retry": True},
        )
        urgent_request = OtgDeliveryRequest.objects.create(
            shipping_order=urgent_order,
            agency=self.agency,
            status=OtgDeliveryRequest.STATUS_BLOCKED,
            payload={"waiting_for_stock": True, "auto_retry": True},
        )
        Task.objects.create(
            title=f"Заявка на отгрузку №{urgent_order.number}",
            route=f"/shipping/{urgent_order.pk}/",
            assigned_to=storekeeper,
            status="in_progress",
            priority="urgent",
        )

        retried_ids = []

        def retry_one(request_id):
            retried_ids.append(request_id)
            return ["MOVE-URGENT"]

        with patch(
            "otg_reachtruck.services._retry_waiting_otg_request",
            side_effect=retry_one,
        ):
            started = retry_waiting_otg_shipping_requests(
                agency_id=self.agency.id,
                limit=1,
            )

        self.assertEqual(retried_ids, [urgent_request.id])
        self.assertNotEqual(normal_request.id, urgent_request.id)
        self.assertEqual(started, [urgent_order.number])

    @override_settings(USE_OTG_REACHTRUCK_PLANNER=True)
    def test_non_waiting_blocked_request_remains_confirmed_shortage(self):
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=10,
            comment="Коробов: 1; кратность: 10",
        )
        OtgDeliveryRequest.objects.create(
            shipping_order=self.order,
            agency=self.agency,
            status=OtgDeliveryRequest.STATUS_BLOCKED,
            requested_boxes=1,
            planned_boxes=0,
            shortage_boxes=1,
            payload={"partial_plan_blocked": True},
        )

        self.assertEqual(get_otg_confirmed_shortage(self.order)["shortage_boxes"], 1)
        readiness = shipping_pick_readiness(self.order)
        self.assertFalse(readiness["can_pick"])
        self.assertIn("Создать добор", readiness["reason"])

    def test_request_snapshot_skips_foreign_claim_and_returns_task_after_release(self):
        first_box = self._create_box(
            pallet_code="PAL-01",
            box_code="PAL-01-BX-01",
            row=1,
        )
        self._create_box(
            pallet_code="PAL-02",
            box_code="PAL-02-BX-01",
            row=2,
        )
        move_request = self._create_claim_queue_request()
        first_task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-Q-1",
            pallet_code="PAL-01",
            status=MoveTask.STATUS_IN_PROGRESS,
            employee_id=17,
        )
        second_task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-Q-2",
            pallet_code="PAL-02",
            employee_id=91,
        )
        conflict_task = self._create_claim_queue_task(
            move_request=self._create_claim_queue_request("OTG-CONFLICT"),
            legacy_order_id="OTG-C-1",
            pallet_code="PAL-C",
            status=MoveTask.STATUS_IN_PROGRESS,
            employee_id=22,
        )
        claim = BoxClaim.objects.create(
            agency=self.agency,
            move_task=conflict_task,
            box_code=first_box.container_code,
            pallet_code="PAL-01",
            claimed_by=self.user,
        )
        base_snapshot = {
            "can_take": False,
            "can_scan": False,
            "taken_by_other": True,
        }
        task_snapshot = {
            "current_step": "pallet",
            "prompt": "Сканируйте паллету",
            "pallet_code": "PAL-02",
            "destination_code": "OTG",
            "destination_label": "OTG",
        }

        with patch(
            "otg_reachtruck.execution.shared_execution.build_mobile_request_execution_snapshot",
            return_value=base_snapshot.copy(),
        ), patch(
            "otg_reachtruck.execution.shared_execution.build_mobile_execution_snapshot",
            return_value=task_snapshot,
        ):
            snapshot = build_otg_mobile_request_execution_snapshot(
                [first_task.legacy_order_id, second_task.legacy_order_id],
                employee_id=17,
            )

        self.assertEqual(snapshot["active_order_id"], second_task.legacy_order_id)
        self.assertTrue(snapshot["can_take"])
        self.assertFalse(snapshot["can_scan"])
        self.assertEqual(snapshot["temporarily_skipped_order_ids"], [first_task.legacy_order_id])
        first_task.refresh_from_db()
        self.assertEqual(first_task.status, MoveTask.STATUS_IN_PROGRESS)

        claim.status = BoxClaim.STATUS_CANCELLED
        claim.save(update_fields=["status", "updated_at"])
        task_snapshot["pallet_code"] = "PAL-01"
        with patch(
            "otg_reachtruck.execution.shared_execution.build_mobile_request_execution_snapshot",
            return_value=base_snapshot.copy(),
        ), patch(
            "otg_reachtruck.execution.shared_execution.build_mobile_execution_snapshot",
            return_value=task_snapshot,
        ):
            released_snapshot = build_otg_mobile_request_execution_snapshot(
                [first_task.legacy_order_id, second_task.legacy_order_id],
                employee_id=17,
            )

        self.assertEqual(released_snapshot["active_order_id"], first_task.legacy_order_id)
        self.assertFalse(released_snapshot["can_take"])
        self.assertTrue(released_snapshot["can_scan"])
        self.assertEqual(released_snapshot["temporarily_skipped_order_ids"], [])

    def test_request_snapshot_fails_closed_on_duplicate_legacy_order_id(self):
        first_task = self._create_claim_queue_task(
            move_request=self._create_claim_queue_request("OTG-FIRST"),
            legacy_order_id="4627",
            pallet_code="EGR-PALLET",
        )
        other_agency = Agency.objects.create(agn_name="Other OTG Client")
        other_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id="OTG-SECOND",
            agency=other_agency,
            requested_by=self.user,
            destination_zone="OTG",
        )
        self._create_claim_queue_task(
            move_request=other_request,
            legacy_order_id=first_task.legacy_order_id,
            pallet_code="KEZ-PALLET",
        )

        with patch(
            "otg_reachtruck.execution.shared_execution.build_mobile_request_execution_snapshot"
        ) as shared_snapshot:
            snapshot = build_otg_mobile_request_execution_snapshot(
                [first_task.legacy_order_id],
                employee_id=17,
            )

        self.assertEqual(snapshot, {})
        shared_snapshot.assert_not_called()

    def test_own_task_claim_does_not_temporarily_block_task(self):
        source_box = self._create_box(
            pallet_code="PAL-OWN",
            box_code="PAL-OWN-BX-01",
            row=3,
        )
        task = self._create_claim_queue_task(
            move_request=self._create_claim_queue_request("OTG-OWN"),
            legacy_order_id="OTG-OWN-1",
            pallet_code="PAL-OWN",
            status=MoveTask.STATUS_IN_PROGRESS,
            employee_id=17,
        )
        BoxClaim.objects.create(
            agency=self.agency,
            move_task=task,
            box_code=source_box.container_code,
            pallet_code="PAL-OWN",
            claimed_by=self.user,
        )

        self.assertFalse(_otg_task_temporarily_blocked_by_foreign_claim(task))

    def test_request_selection_stops_after_available_active_task(self):
        move_request = self._create_claim_queue_request("OTG-ACTIVE-FIRST")
        queued_task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-ACTIVE-FIRST-Q",
            pallet_code="PAL-ACTIVE-FIRST-Q",
            employee_id=17,
        )
        active_task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-ACTIVE-FIRST-A",
            pallet_code="PAL-ACTIVE-FIRST-A",
            status=MoveTask.STATUS_IN_PROGRESS,
            employee_id=17,
        )

        with patch(
            "otg_reachtruck.execution._otg_task_temporarily_blocked_by_foreign_claim",
            return_value=False,
        ) as blocked_check:
            selected_task, temporarily_skipped = _select_otg_request_task(
                [queued_task.legacy_order_id, active_task.legacy_order_id],
                employee_id=17,
            )

        self.assertEqual(selected_task.id, active_task.id)
        self.assertEqual(temporarily_skipped, [])
        self.assertEqual(blocked_check.call_count, 1)
        self.assertEqual(blocked_check.call_args.args[0].id, active_task.id)

    def test_request_selection_reuses_pallet_stock_within_request(self):
        pallet_code = "PAL-CACHE"
        box_code = "PAL-CACHE-BX-01"
        move_request = self._create_claim_queue_request("OTG-CACHE")
        first_task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-CACHE-1",
            pallet_code=pallet_code,
            employee_id=17,
        )
        second_task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-CACHE-2",
            pallet_code=pallet_code,
            employee_id=17,
        )
        conflict_task = self._create_claim_queue_task(
            move_request=self._create_claim_queue_request("OTG-CACHE-CONFLICT"),
            legacy_order_id="OTG-CACHE-C",
            pallet_code="PAL-CACHE-CONFLICT",
            status=MoveTask.STATUS_IN_PROGRESS,
            employee_id=22,
        )
        BoxClaim.objects.create(
            agency=self.agency,
            move_task=conflict_task,
            box_code=box_code,
            pallet_code=pallet_code,
            claimed_by=self.user,
        )
        stock_boxes = [
            {
                "code": box_code,
                "qty": 10,
                "barcode_qty": {"460000000001": 10},
                "items": [],
                "marked_units": [],
            }
        ]

        with patch(
            "sklad.services.stock_operations.OperationalStockService.get_pallet_boxes",
            return_value=stock_boxes,
        ) as get_pallet_boxes:
            selected_task, temporarily_skipped = _select_otg_request_task(
                [first_task.legacy_order_id, second_task.legacy_order_id],
                employee_id=17,
            )

        self.assertIsNone(selected_task)
        self.assertEqual(
            temporarily_skipped,
            [first_task.legacy_order_id, second_task.legacy_order_id],
        )
        get_pallet_boxes.assert_called_once_with(pallet_code, agency_id=self.agency.id)

    def test_request_selection_stops_after_first_available_queued_task(self):
        move_request = self._create_claim_queue_request("OTG-QUEUED-FIRST")
        first_task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-QUEUED-FIRST-1",
            pallet_code="PAL-QUEUED-FIRST-1",
            employee_id=17,
        )
        second_task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-QUEUED-FIRST-2",
            pallet_code="PAL-QUEUED-FIRST-2",
            employee_id=17,
        )
        third_task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-QUEUED-FIRST-3",
            pallet_code="PAL-QUEUED-FIRST-3",
            employee_id=17,
        )

        with patch(
            "otg_reachtruck.execution._otg_task_temporarily_blocked_by_foreign_claim",
            side_effect=[True, False],
        ) as blocked_check:
            selected_task, temporarily_skipped = _select_otg_request_task(
                [
                    first_task.legacy_order_id,
                    second_task.legacy_order_id,
                    third_task.legacy_order_id,
                ],
                employee_id=17,
            )

        self.assertEqual(selected_task.id, second_task.id)
        self.assertEqual(temporarily_skipped, [first_task.legacy_order_id])
        self.assertEqual(blocked_check.call_count, 2)

    def test_nonmatching_box_is_not_misclassified_as_claim_blocked(self):
        source_box = self._create_box(
            pallet_code="PAL-MISMATCH",
            box_code="PAL-MISMATCH-BX-01",
            qty=100,
            row=4,
        )
        task = self._create_claim_queue_task(
            move_request=self._create_claim_queue_request("OTG-MISMATCH"),
            legacy_order_id="OTG-MISMATCH-1",
            pallet_code="PAL-MISMATCH",
            status=MoveTask.STATUS_IN_PROGRESS,
            employee_id=17,
            box_qty=99,
        )
        conflict_task = self._create_claim_queue_task(
            move_request=self._create_claim_queue_request("OTG-MISMATCH-CONFLICT"),
            legacy_order_id="OTG-MISMATCH-C",
            pallet_code="PAL-C",
            status=MoveTask.STATUS_IN_PROGRESS,
            employee_id=22,
        )
        BoxClaim.objects.create(
            agency=self.agency,
            move_task=conflict_task,
            box_code=source_box.container_code,
            pallet_code="PAL-MISMATCH",
            claimed_by=self.user,
        )

        self.assertFalse(_otg_task_temporarily_blocked_by_foreign_claim(task))

    def test_take_request_assigns_only_selected_available_task(self):
        first_box = self._create_box(
            pallet_code="PAL-A",
            box_code="PAL-A-BX-01",
            row=5,
        )
        self._create_box(
            pallet_code="PAL-B",
            box_code="PAL-B-BX-01",
            row=6,
        )
        self._create_box(
            pallet_code="PAL-D",
            box_code="PAL-D-BX-01",
            row=7,
        )
        move_request = self._create_claim_queue_request("OTG-ASSIGN")
        blocked_task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-A-1",
            pallet_code="PAL-A",
            status=MoveTask.STATUS_IN_PROGRESS,
            employee_id=17,
        )
        selected_task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-A-2",
            pallet_code="PAL-B",
            employee_id=91,
        )
        untouched_task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-A-3",
            pallet_code="PAL-D",
            employee_id=92,
        )
        conflict_task = self._create_claim_queue_task(
            move_request=self._create_claim_queue_request("OTG-ASSIGN-CONFLICT"),
            legacy_order_id="OTG-A-C",
            pallet_code="PAL-C",
            status=MoveTask.STATUS_IN_PROGRESS,
            employee_id=22,
        )
        BoxClaim.objects.create(
            agency=self.agency,
            move_task=conflict_task,
            box_code=first_box.container_code,
            pallet_code="PAL-A",
            claimed_by=self.user,
        )

        with patch(
            "otg_reachtruck.execution._verify_or_retarget_otg_source",
            side_effect=lambda task, payload: (payload, False, ""),
        ), patch("otg_reachtruck.execution.sync_task_status_by_legacy_order_id"):
            result = take_otg_move_request(
                [
                    blocked_task.legacy_order_id,
                    selected_task.legacy_order_id,
                    untouched_task.legacy_order_id,
                ],
                user=self.user,
                employee_id=17,
                employee_name="Driver 17",
            )

        self.assertTrue(result.ok, result.error)
        blocked_task.refresh_from_db()
        selected_task.refresh_from_db()
        untouched_task.refresh_from_db()
        self.assertEqual(blocked_task.status, MoveTask.STATUS_IN_PROGRESS)
        self.assertEqual(blocked_task.payload.get("assigned_to_id"), 17)
        self.assertEqual(selected_task.status, MoveTask.STATUS_IN_PROGRESS)
        self.assertEqual(selected_task.payload.get("assigned_to_id"), 17)
        self.assertEqual(untouched_task.status, MoveTask.STATUS_CREATED)
        self.assertEqual(untouched_task.payload.get("assigned_to_id"), 92)

    def test_driver_can_take_selected_pallet_from_request_route(self):
        self._create_box(
            pallet_code="PAL-ROUTE-A",
            box_code="PAL-ROUTE-A-BX-01",
            row=8,
        )
        self._create_box(
            pallet_code="PAL-ROUTE-B",
            box_code="PAL-ROUTE-B-BX-01",
            row=9,
        )
        move_request = self._create_claim_queue_request("OTG-ROUTE-SELECT")
        first_task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-ROUTE-A",
            pallet_code="PAL-ROUTE-A",
        )
        selected_task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-ROUTE-B",
            pallet_code="PAL-ROUTE-B",
        )

        with patch(
            "otg_reachtruck.execution._verify_or_retarget_otg_source",
            side_effect=lambda task, payload: (payload, False, ""),
        ), patch("otg_reachtruck.execution.sync_task_status_by_legacy_order_id"):
            result = take_selected_otg_move_request_task(
                [first_task.legacy_order_id, selected_task.legacy_order_id],
                selected_legacy_order_id=selected_task.legacy_order_id,
                user=self.user,
                employee_id=17,
                employee_name="Driver 17",
            )

        self.assertTrue(result.ok, result.error)
        first_task.refresh_from_db()
        selected_task.refresh_from_db()
        self.assertEqual(first_task.status, MoveTask.STATUS_CREATED)
        self.assertEqual(selected_task.status, MoveTask.STATUS_IN_PROGRESS)
        self.assertEqual(selected_task.payload.get("assigned_to_id"), 17)

    def test_selected_pallet_must_belong_to_request(self):
        move_request = self._create_claim_queue_request("OTG-ROUTE-BOUNDARY")
        request_task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-ROUTE-OWN",
            pallet_code="PAL-ROUTE-OWN",
        )
        foreign_task = self._create_claim_queue_task(
            move_request=self._create_claim_queue_request("OTG-ROUTE-FOREIGN"),
            legacy_order_id="OTG-ROUTE-FOREIGN",
            pallet_code="PAL-ROUTE-FOREIGN",
        )

        result = take_selected_otg_move_request_task(
            [request_task.legacy_order_id],
            selected_legacy_order_id=foreign_task.legacy_order_id,
            user=self.user,
            employee_id=17,
            employee_name="Driver 17",
        )

        self.assertFalse(result.ok)
        self.assertIn("не относится", result.error)
        request_task.refresh_from_db()
        foreign_task.refresh_from_db()
        self.assertEqual(request_task.status, MoveTask.STATUS_CREATED)
        self.assertEqual(foreign_task.status, MoveTask.STATUS_CREATED)

    def test_selected_pallet_cannot_be_taken_from_other_driver(self):
        move_request = self._create_claim_queue_request("OTG-ROUTE-OTHER-DRIVER")
        task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-ROUTE-OTHER-DRIVER",
            pallet_code="PAL-ROUTE-OTHER-DRIVER",
            status=MoveTask.STATUS_IN_PROGRESS,
            employee_id=92,
        )

        result = take_selected_otg_move_request_task(
            [task.legacy_order_id],
            selected_legacy_order_id=task.legacy_order_id,
            user=self.user,
            employee_id=17,
            employee_name="Driver 17",
        )

        self.assertFalse(result.ok)
        self.assertIn("другим водителем", result.error)
        task.refresh_from_db()
        self.assertEqual(task.payload.get("assigned_to_id"), 92)

    def test_manager_can_release_stale_unscanned_otg_assignment(self):
        manager = Employee.objects.create(
            user=self.user,
            full_name="OTG Supervisor",
            role="head_manager",
        )
        move_request = self._create_claim_queue_request("OTG-RELEASE-STALE")
        task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-RELEASE-STALE-1",
            pallet_code="PAL-RELEASE-STALE",
            status=MoveTask.STATUS_IN_PROGRESS,
            employee_id=17,
        )
        payload = dict(task.payload or {})
        payload["mobile_execution"] = {"source_confirmed": True}
        payload["taken_at"] = timezone.now().isoformat()
        task.payload = payload
        task.started_at = timezone.now() - timedelta(hours=1)
        task.save(update_fields=["payload", "started_at", "updated_at"])
        MoveTask.objects.filter(pk=task.pk).update(
            updated_at=timezone.now() - timedelta(minutes=31)
        )

        result = release_stale_otg_move_request_task(
            [task.legacy_order_id],
            selected_legacy_order_id=task.legacy_order_id,
            user=self.user,
            employee_id=manager.id,
            employee_name=manager.full_name,
        )

        self.assertTrue(result.ok)
        task.refresh_from_db()
        move_request.refresh_from_db()
        self.assertEqual(task.status, MoveTask.STATUS_CREATED)
        self.assertIsNone(task.assigned_to_id)
        self.assertEqual(task.assigned_to_name, "")
        self.assertIsNone(task.started_at)
        self.assertNotIn("assigned_to_id", task.payload)
        self.assertNotIn("assigned_to_name", task.payload)
        self.assertFalse(task.payload["mobile_execution"].get("source_confirmed"))
        self.assertEqual(task.payload["status_label"], "Ожидает водителя")
        self.assertEqual(
            task.payload["assignment_release_history"][-1]["previous_assignee"],
            "Driver 17",
        )
        self.assertEqual(move_request.status, MoveRequest.STATUS_PLANNED)

    def test_manager_cannot_release_otg_assignment_after_pallet_scan(self):
        manager = Employee.objects.create(
            user=self.user,
            full_name="OTG Supervisor",
            role="head_manager",
        )
        move_request = self._create_claim_queue_request("OTG-RELEASE-SCANNED")
        task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-RELEASE-SCANNED-1",
            pallet_code="PAL-RELEASE-SCANNED",
            status=MoveTask.STATUS_IN_PROGRESS,
            employee_id=17,
        )
        payload = dict(task.payload or {})
        payload["mobile_execution"] = {"pallet_confirmed": True}
        task.payload = payload
        task.save(update_fields=["payload", "updated_at"])
        MoveTask.objects.filter(pk=task.pk).update(
            updated_at=timezone.now() - timedelta(minutes=31)
        )

        result = release_stale_otg_move_request_task(
            [task.legacy_order_id],
            selected_legacy_order_id=task.legacy_order_id,
            user=self.user,
            employee_id=manager.id,
            employee_name=manager.full_name,
        )

        self.assertFalse(result.ok)
        self.assertIn("уже есть сканы", result.error)
        task.refresh_from_db()
        self.assertEqual(task.status, MoveTask.STATUS_IN_PROGRESS)
        self.assertTrue(task.payload["mobile_execution"]["pallet_confirmed"])

    def test_route_groups_offer_safe_release_only_to_manager_after_timeout(self):
        task = self._create_claim_queue_task(
            move_request=self._create_claim_queue_request("OTG-RELEASE-ROUTE"),
            legacy_order_id="OTG-RELEASE-ROUTE-1",
            pallet_code="PAL-RELEASE-ROUTE",
            status=MoveTask.STATUS_IN_PROGRESS,
            employee_id=17,
        )
        MoveTask.objects.filter(pk=task.pk).update(
            updated_at=timezone.now() - timedelta(minutes=31)
        )
        task.refresh_from_db()

        manager_groups = _build_route_groups(
            [task],
            can_choose=False,
            can_manage_assignments=True,
        )
        driver_groups = _build_route_groups(
            [task],
            can_choose=False,
            can_manage_assignments=False,
        )

        manager_stop = manager_groups[0]["stops"][0]
        driver_stop = driver_groups[0]["stops"][0]
        self.assertTrue(manager_stop["can_release"])
        self.assertFalse(driver_stop["can_release"])
        self.assertEqual(manager_stop["assignee_name"], "Driver 17")
        html = render_to_string(
            "otg_reachtruck/dashboard.html",
            {
                "selected": {"key": "OTG-RELEASE", "label": "RELEASE"},
                "execution": {"can_take": False, "can_scan": False},
                "route_groups": manager_groups,
                "route_stop_count": 1,
                "request_notifications": [],
            },
        )
        self.assertIn("Вернуть в очередь", html)
        self.assertIn("Driver 17", html)

    def test_dashboard_shows_otg_priority_deadline_and_voice_setup(self):
        Employee.objects.create(
            user=self.user,
            full_name="OTG Driver",
            role="reachtruck_driver",
        )
        move_request = self._create_claim_queue_request("OTG-DASHBOARD-META")
        move_request.priority = MoveRequest.PRIORITY_URGENT
        move_request.due_at = timezone.now() - timedelta(hours=1)
        move_request.comment = "Сначала срочная отгрузка"
        move_request.save(update_fields=["priority", "due_at", "comment", "updated_at"])
        task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-DASHBOARD-META-1",
            pallet_code="PAL-DASHBOARD-META",
        )
        payload = dict(task.payload or {})
        payload["shipping_order_id"] = self.order.number
        task.payload = payload
        task.save(update_fields=["payload", "updated_at"])

        self.client.force_login(self.user)
        response = self.client.get("/otg-reachtruck/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Срочно")
        self.assertContains(response, "Просрочено")
        self.assertContains(response, "Сначала срочная отгрузка")
        self.assertContains(response, "Новая заявка на отгрузку")
        self.assertContains(response, "fullbox-logo.png")
        queue_response = self.client.get("/otg-reachtruck/?queue=1")
        self.assertEqual(queue_response.status_code, 200)
        self.assertEqual(
            queue_response.json()["requests"][0]["key"],
            self.order.number,
        )

    def test_route_groups_places_and_combines_pallet_subtasks(self):
        move_request = self._create_claim_queue_request("OTG-ROUTE-GROUPS")
        current_task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-GROUP-CURRENT",
            pallet_code="PAL-GROUP-A",
            status=MoveTask.STATUS_IN_PROGRESS,
            employee_id=17,
        )
        waiting_same_pallet = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-GROUP-WAITING",
            pallet_code="PAL-GROUP-A",
        )
        completed_task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-GROUP-DONE",
            pallet_code="PAL-GROUP-B",
            status=MoveTask.STATUS_DONE,
        )
        waiting_other_row = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="OTG-GROUP-NEXT",
            pallet_code="PAL-GROUP-C",
        )
        for task, source_code, row, tier, cell in (
            (current_task, "A-7/4-1", 7, 4, 1),
            (waiting_same_pallet, "A-7/4-1", 7, 4, 1),
            (completed_task, "A-7/3-2", 7, 3, 2),
            (waiting_other_row, "B-2/1-1", 2, 1, 1),
        ):
            payload = dict(task.payload or {})
            payload["source_code"] = source_code
            payload["from_location"] = {
                "zone": "OS",
                "section": 2 if source_code.startswith("A-") else 3,
                "row": row,
                "tier": tier,
                "cell": cell,
            }
            task.payload = payload
            task.save(update_fields=["payload", "updated_at"])

        route_groups = _build_route_groups(
            [current_task, waiting_same_pallet, completed_task, waiting_other_row],
            can_choose=True,
        )
        stops = [stop for group in route_groups for stop in group["stops"]]
        by_pallet = {stop["pallet_code"]: stop for stop in stops}

        self.assertEqual([group["label"] for group in route_groups], ["Ряд A-7", "Ряд B-2"])
        self.assertEqual(by_pallet["PAL-GROUP-A"]["subtask_count"], 2)
        self.assertEqual(by_pallet["PAL-GROUP-A"]["status_key"], "current")
        self.assertFalse(by_pallet["PAL-GROUP-A"]["can_choose"])
        self.assertEqual(by_pallet["PAL-GROUP-B"]["status_key"], "done")
        self.assertEqual(by_pallet["PAL-GROUP-C"]["status_key"], "waiting")
        self.assertTrue(by_pallet["PAL-GROUP-C"]["can_choose"])

        html = render_to_string(
            "otg_reachtruck/dashboard.html",
            {
                "selected": {"key": "OTG-ROUTE", "label": "ROUTE"},
                "execution": {"can_take": True, "can_scan": False},
                "route_groups": route_groups,
                "route_stop_count": len(stops),
            },
        )
        self.assertIn("Все места заявки", html)
        self.assertIn("PAL-GROUP-A", html)
        self.assertIn("A-7/4-1", html)
        self.assertIn("Текущая", html)
        self.assertIn("Выполнена", html)
        self.assertIn("Взять в работу", html)

    def test_route_groups_tolerate_legacy_location_payload(self):
        task = self._create_claim_queue_task(
            move_request=self._create_claim_queue_request("OTG-ROUTE-LEGACY"),
            legacy_order_id="OTG-GROUP-LEGACY",
            pallet_code="PAL-GROUP-LEGACY",
        )
        payload = dict(task.payload or {})
        payload["from_location"] = None
        payload["source_code"] = ""
        task.payload = payload
        task.from_row = None
        task.from_section = None
        task.from_tier = None
        task.from_cell = None
        task.save(
            update_fields=[
                "payload",
                "from_row",
                "from_section",
                "from_tier",
                "from_cell",
                "updated_at",
            ]
        )

        route_groups = _build_route_groups([task], can_choose=True)

        self.assertEqual(len(route_groups), 1)
        self.assertEqual(route_groups[0]["label"], "Основной склад")
        self.assertEqual(route_groups[0]["stops"][0]["status_label"], "Ожидает")
        self.assertTrue(route_groups[0]["stops"][0]["can_choose"])

    def test_build_box_demands_groups_mixed_box_by_composition(self):
        comment = "Коробов: 3; кратность: 4; короба: MIX-1, MIX-2, MIX-3; микс-короб"
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-MIX-1",
            name="Mix 1",
            size="M",
            barcode="111",
            goods_type="Ready",
            qty_requested=12,
            comment=comment,
        )
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-MIX-2",
            name="Mix 2",
            size="L",
            barcode="222",
            goods_type="Ready",
            qty_requested=24,
            comment=comment.replace("кратность: 4", "кратность: 8"),
        )

        demands = build_box_demands(self.order)

        self.assertEqual(len(demands), 1)
        demand = demands[0]
        self.assertEqual(demand["demand_type"], OtgDeliveryDemand.TYPE_MIXED_BOX)
        self.assertEqual(demand["boxes_required"], 3)
        self.assertEqual(demand["box_qty"], 12)
        self.assertEqual(len(demand["composition"]), 2)

    def test_build_box_demands_merges_mixed_and_partial_rows_for_one_physical_box(self):
        mixed_item = ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-MIX-FULL",
            name="Full part of mixed box",
            size="M",
            barcode="111",
            goods_type="Ready",
            qty_requested=28,
            comment="Коробов: 1; кратность: 28; короба: BOX-MERGED-01; микс-короб",
        )
        partial_comment = encode_partial_box_split(
            {
                "kind": "partial_box_split",
                "group_key": "partial-second-line",
                "source_boxes": 1,
                "source_box_codes": ["BOX-MERGED-01"],
                "source_box_pattern": [
                    {
                        "sku": "SKU-MIX-PART",
                        "name": "Partial part of mixed box",
                        "size": "L",
                        "barcode": "222",
                        "goods_type": "Ready",
                        "qty": 10,
                    }
                ],
                "pick_pattern": [
                    {
                        "sku": "SKU-MIX-PART",
                        "name": "Partial part of mixed box",
                        "size": "L",
                        "barcode": "222",
                        "goods_type": "Ready",
                        "qty": 3,
                    }
                ],
                "item_pick_qty": 3,
                "item_source_qty": 10,
            }
        )
        partial_item = ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-MIX-PART",
            name="Partial part of mixed box",
            size="L",
            barcode="222",
            goods_type="Ready",
            qty_requested=3,
            comment=partial_comment,
        )
        full_stock = self._create_box(
            pallet_code="PAL-MERGED",
            box_code="BOX-MERGED-01",
            sku="SKU-MIX-FULL",
            barcode="111",
            qty=28,
        )
        partial_stock = self._create_box(
            pallet_code="PAL-MERGED",
            box_code="BOX-MERGED-01",
            sku="SKU-MIX-PART",
            barcode="222",
            qty=10,
        )
        before = (
            full_stock.qty,
            full_stock.available_qty,
            partial_stock.qty,
            partial_stock.available_qty,
        )

        demands = build_box_demands(self.order)
        preview = preview_otg_shipping_pick_coverage(self.order)

        self.assertEqual(len(demands), 1)
        demand = demands[0]
        self.assertEqual(demand["demand_type"], OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT)
        self.assertEqual(demand["boxes_required"], 1)
        self.assertEqual(demand["source_box_codes"], ["BOX-MERGED-01"])
        self.assertEqual(
            {row["barcode"]: row["qty_per_box"] for row in demand["composition"]},
            {"111": 28, "222": 10},
        )
        self.assertEqual(
            {row["barcode"]: row["qty_per_box"] for row in demand["pick_composition"]},
            {"111": 28, "222": 3},
        )
        self.assertEqual(
            demand["item_quantities_per_box"],
            {str(mixed_item.id): 28, str(partial_item.id): 3},
        )
        self.assertTrue(preview["can_cover"], preview)
        self.assertEqual((preview["requested_boxes"], preview["planned_boxes"]), (1, 1))
        _move_request, move_ids, shortage_qty = create_otg_shipping_pick_request(
            order=self.order,
            user=self.user,
            allow_partial=False,
        )
        task = MoveTask.objects.get(request__otg_delivery_requests__shipping_order=self.order)
        self.assertEqual(shortage_qty, 0)
        self.assertEqual(len(move_ids), 1)
        self.assertEqual(task.pallet_code, "PAL-MERGED")
        self.assertEqual(task.payload.get("requested_box_count"), 1)
        self.assertEqual(task.payload.get("requested_qty"), 31)
        self.assertEqual(task.payload.get("requested_barcode_qty"), {"111": 28, "222": 3})
        self.assertEqual(task.payload.get("planned_box_codes"), [])
        full_stock.refresh_from_db()
        partial_stock.refresh_from_db()
        self.assertEqual(
            (full_stock.qty, full_stock.available_qty, partial_stock.qty, partial_stock.available_qty),
            before,
        )

    def test_build_box_demands_merges_multiple_partial_rows_for_one_physical_box(self):
        stock_rows = []
        expected_source = {}
        expected_pick = {}
        expected_items = {}
        for index, (source_qty, pick_qty) in enumerate([(10, 1), (20, 2), (30, 3)], start=1):
            barcode = f"333{index}"
            sku = f"SKU-PART-{index}"
            comment = encode_partial_box_split(
                {
                    "kind": "partial_box_split",
                    "group_key": f"partial-line-{index}",
                    "source_boxes": 1,
                    "source_box_codes": ["BOX-PARTIAL-MERGED"],
                    "source_box_pattern": [
                        {
                            "sku": sku,
                            "name": sku,
                            "size": "0",
                            "barcode": barcode,
                            "goods_type": "Ready",
                            "qty": source_qty,
                        }
                    ],
                    "pick_pattern": [
                        {
                            "sku": sku,
                            "name": sku,
                            "size": "0",
                            "barcode": barcode,
                            "goods_type": "Ready",
                            "qty": pick_qty,
                        }
                    ],
                    "item_pick_qty": pick_qty,
                    "item_source_qty": source_qty,
                }
            )
            item = ShippingOrderItem.objects.create(
                order=self.order,
                sku_code=sku,
                name=sku,
                size="0",
                barcode=barcode,
                goods_type="Ready",
                qty_requested=pick_qty,
                comment=comment,
            )
            expected_source[barcode] = source_qty
            expected_pick[barcode] = pick_qty
            expected_items[str(item.id)] = pick_qty
            stock_rows.append(
                self._create_box(
                    pallet_code="PAL-PARTIAL-MERGED",
                    box_code="BOX-PARTIAL-MERGED",
                    sku=sku,
                    barcode=barcode,
                    qty=source_qty,
                )
            )
        before = [(row.id, row.qty, row.available_qty) for row in stock_rows]

        demands = build_box_demands(self.order)
        preview = preview_otg_shipping_pick_coverage(self.order)

        self.assertEqual(len(demands), 1)
        demand = demands[0]
        self.assertEqual(demand["demand_type"], OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT)
        self.assertEqual(demand["boxes_required"], 1)
        self.assertEqual(demand["source_box_codes"], ["BOX-PARTIAL-MERGED"])
        self.assertEqual(
            {row["barcode"]: row["qty_per_box"] for row in demand["composition"]},
            expected_source,
        )
        self.assertEqual(
            {row["barcode"]: row["qty_per_box"] for row in demand["pick_composition"]},
            expected_pick,
        )
        self.assertEqual(demand["item_quantities_per_box"], expected_items)
        self.assertTrue(preview["can_cover"], preview)
        self.assertEqual((preview["requested_boxes"], preview["planned_boxes"]), (1, 1))
        self.assertEqual(
            list(
                WarehouseStockSnapshot.objects.filter(id__in=[row.id for row in stock_rows])
                .order_by("id")
                .values_list("id", "qty", "available_qty")
            ),
            sorted(before),
        )

    def test_discrepancy_piece_pick_merges_shared_box_into_one_valid_demand(self):
        item_a = ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-DISC-A",
            name="Discrepancy A",
            size="0",
            barcode="460000000101",
            goods_type="Ready",
            qty_requested=4,
        )
        item_b = ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-DISC-B",
            name="Discrepancy B",
            size="0",
            barcode="460000000102",
            goods_type="Ready",
            qty_requested=6,
        )
        stock_rows = [
            self._create_box(
                pallet_code="PAL-DISC-MERGED",
                box_code="BOX-DISC-MERGED",
                sku=item_a.sku_code,
                name=item_a.name,
                barcode=item_a.barcode,
                qty=10,
            ),
            self._create_box(
                pallet_code="PAL-DISC-MERGED",
                box_code="BOX-DISC-MERGED",
                sku=item_b.sku_code,
                name=item_b.name,
                barcode=item_b.barcode,
                qty=20,
            ),
        ]
        before = [(row.id, row.qty, row.available_qty) for row in stock_rows]
        shortages = [
            {
                "shipping_item_id": item_a.id,
                "barcode": item_a.barcode,
                "missing_qty": 4,
            },
            {
                "shipping_item_id": item_b.id,
                "barcode": item_b.barcode,
                "missing_qty": 6,
            },
        ]

        preview = get_otg_discrepancy_pick_preview(
            order=self.order,
            shortage_rows=shortages,
            correction_mode="piece",
        )

        self.assertTrue(preview["can_create"], preview)
        self.assertEqual(preview["source_box_count"], 1)
        self.assertEqual(len(preview["demand_payloads"]), 1)
        demand = preview["demand_payloads"][0]
        self.assertTrue(demand["demand_key"])
        self.assertEqual(demand["box_qty"], 30)
        self.assertEqual(demand["boxes_required"], 1)

        _move_request, move_ids, _current_plan = create_otg_shipping_discrepancy_pick(
            order=self.order,
            shortage_rows=shortages,
            correction_mode="piece",
            user=self.user,
            requested_by_name="OTG Manager",
            requested_by_role="manager",
            expected_plan=preview,
        )

        self.assertEqual(len(move_ids), 1)
        task = MoveTask.objects.get(legacy_order_id=move_ids[0])
        self.assertEqual(task.payload.get("requested_box_count"), 1)
        self.assertEqual(task.payload.get("requested_qty"), 10)
        self.assertEqual(
            task.payload.get("requested_barcode_qty"),
            {"460000000101": 4, "460000000102": 6},
        )
        self.assertEqual(
            list(
                WarehouseStockSnapshot.objects.filter(id__in=[row.id for row in stock_rows])
                .order_by("id")
                .values_list("id", "qty", "available_qty")
            ),
            sorted(before),
        )

    def test_partial_split_fixed_box_allows_only_pick_barcode(self):
        request = OtgDeliveryRequest.objects.create(
            shipping_order=self.order,
            agency=self.agency,
            requested_by=self.user,
            requested_by_name="OTG Manager",
            requested_by_role="manager",
        )
        demand = OtgDeliveryDemand.objects.create(
            request=request,
            demand_key="partial-fixed-mixed-box",
            demand_type=OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT,
            boxes_required=1,
            boxes_planned=1,
            box_qty=80,
            composition=[
                {
                    "sku": "N021",
                    "name": "Source item",
                    "barcode": "2046279701017",
                    "goods_type": "gv",
                    "qty_per_box": 40,
                },
                {
                    "sku": "knife020",
                    "name": "Requested item",
                    "barcode": "2054099382668",
                    "goods_type": "gv",
                    "qty_per_box": 40,
                },
            ],
            pick_composition=[
                {
                    "sku": "knife020",
                    "name": "Requested item",
                    "barcode": "2054099382668",
                    "goods_type": "gv",
                    "qty_per_box": 40,
                }
            ],
            source_box_codes=["BOX-MIXED"],
        )
        plan = OtgPalletPlan.objects.create(
            request=request,
            demand=demand,
            plan_type=OtgPalletPlan.TYPE_PARTIAL_BOX_SPLIT,
            pallet_code="PAL-MIXED",
            boxes_planned=1,
            qty_planned=40,
            from_location={"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
            planned_box_codes=["BOX-MIXED"],
        )

        payload = _payload_for_plan(
            order=self.order,
            otg_request=request,
            plan=plan,
            requested_by_name="OTG Manager",
            requested_by_role="manager",
        )

        self.assertEqual(payload["requested_box_selection"], "fixed")
        self.assertEqual(payload["requested_box"], "BOX-MIXED")
        self.assertEqual(payload["requested_barcodes"], ["2054099382668"])
        self.assertEqual(payload["requested_barcode_qty"], {"2054099382668": 40})
        self.assertEqual(
            payload["partial_pick_patterns"][0]["source_barcode_qty"],
            {"2046279701017": 40, "2054099382668": 40},
        )

    def test_build_box_demands_keeps_same_group_from_different_source_boxes_separate(self):
        item_ids = []
        for source_box_code in ["BOX-N016-01", "BOX-N016-02"]:
            comment = encode_partial_box_split(
                {
                    "kind": "partial_box_split",
                    "group_key": "partial:N016:shared-product-row",
                    "source_boxes": 1,
                    "source_box_codes": [source_box_code],
                    "source_box_pattern": [
                        {
                            "sku": "N016",
                            "name": "Нож складной Финка НКВД",
                            "size": "0",
                            "barcode": "2046279645229",
                            "goods_type": "Ready",
                            "qty": 120,
                        }
                    ],
                    "pick_pattern": [
                        {
                            "sku": "N016",
                            "name": "Нож складной Финка НКВД",
                            "size": "0",
                            "barcode": "2046279645229",
                            "goods_type": "Ready",
                            "qty": 60,
                        }
                    ],
                    "item_pick_qty": 60,
                    "item_source_qty": 120,
                }
            )
            item = ShippingOrderItem.objects.create(
                order=self.order,
                sku_code="N016",
                name="Нож складной Финка НКВД",
                size="0",
                barcode="2046279645229",
                goods_type="Ready",
                qty_requested=60,
                comment=comment,
            )
            item_ids.append(item.id)

        demands = build_box_demands(self.order)

        self.assertEqual(len(demands), 2)
        self.assertEqual(sum(demand["boxes_required"] for demand in demands), 2)
        self.assertEqual(
            {tuple(demand["source_box_codes"]) for demand in demands},
            {("BOX-N016-01",), ("BOX-N016-02",)},
        )
        self.assertEqual(
            sum(
                sum(int(row.get("qty_per_box") or 0) for row in demand["pick_composition"])
                for demand in demands
            ),
            120,
        )
        self.assertEqual(
            {
                int(item_id): int(qty)
                for demand in demands
                for item_id, qty in demand["item_quantities_per_box"].items()
            },
            {item_ids[0]: 60, item_ids[1]: 60},
        )

    def test_otg_payload_guard_rejects_item_total_different_from_task_qty(self):
        with self.assertRaisesMessage(
            ValidationError,
            "сумма позиций задания 120 шт. не совпадает с количеством задания 60 шт.",
        ):
            _validate_otg_move_payload_quantities(
                {
                    "shipping_order_id": self.order.number,
                    "move_mode": "box_partial",
                    "requested_qty": 60,
                    "request_items": [
                        {"shipping_item_id": 101, "requested_qty": 60},
                        {"shipping_item_id": 102, "requested_qty": 60},
                    ],
                }
            )

    def test_merge_fixed_pick_payloads_keeps_every_exact_box(self):
        payloads = [
            {
                "shipping_order_id": self.order.number,
                "move_mode": "box_full",
                "requested_box_selection": "fixed",
                "requested_box_count": 1,
                "requested_boxes": [box_code],
                "requested_box": box_code,
                "requested_qty": 20,
                "request_items": [{"shipping_item_id": 101, "requested_qty": 20}],
                "requested_box_patterns": [],
                "otg_box_composition": [],
                "route_plan": {},
            }
            for box_code in ["BOX-01", "BOX-02", "BOX-03"]
        ]

        merged = _merge_pick_task_payloads(payloads)

        self.assertEqual(merged["requested_box_count"], 3)
        self.assertEqual(merged["requested_boxes"], ["BOX-01", "BOX-02", "BOX-03"])
        self.assertEqual(merged["planned_box_codes"], ["BOX-01", "BOX-02", "BOX-03"])
        self.assertEqual(merged["selected_box_codes"], ["BOX-01", "BOX-02", "BOX-03"])
        self.assertEqual(merged["requested_qty"], 60)
        self.assertEqual(merged["request_items"], [{"shipping_item_id": 101, "requested_qty": 60}])

    def test_otg_payload_guard_rejects_incomplete_fixed_box_list(self):
        with self.assertRaisesMessage(
            ValidationError,
            "указано 11 коробов, но передано 1 уникальных кодов",
        ):
            _validate_otg_move_payload_quantities(
                {
                    "shipping_order_id": self.order.number,
                    "move_mode": "box_full",
                    "requested_box_selection": "fixed",
                    "requested_box_count": 11,
                    "requested_boxes": ["BOX-ONLY"],
                    "requested_qty": 483,
                    "request_items": [{"shipping_item_id": 101, "requested_qty": 483}],
                }
            )

    def test_internal_otg_request_stays_open_when_picked_box_fact_is_incomplete(self):
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id=str(self.order.pk),
            agency=self.agency,
            destination_zone="OTG",
            status=MoveRequest.STATUS_DONE,
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-INCOMPLETE",
            from_zone="OS",
            to_zone="OTG",
            move_mode=MoveTask.MODE_BOX_FULL,
            qty_planned=40,
            qty_done=40,
            status=MoveTask.STATUS_DONE,
            payload={
                "otg_scan_fact_mode": "scan_facts_v1",
                "move_mode": "box_full",
                "requested_box_selection": "fixed",
                "requested_box_count": 2,
                "requested_boxes": ["BOX-01", "BOX-02"],
                "picked_boxes": ["BOX-01"],
                "picked_qty": 40,
                "mobile_execution": {
                    "boxes_scanned": ["BOX-01"],
                    "destination_confirmed": True,
                },
            },
        )
        otg_request = OtgDeliveryRequest.objects.create(
            shipping_order=self.order,
            agency=self.agency,
            move_request=move_request,
            status=OtgDeliveryRequest.STATUS_DISPATCHED,
            requested_boxes=2,
            planned_boxes=2,
        )

        self.assertEqual(sync_completed_otg_delivery_request(move_request), [])
        otg_request.refresh_from_db()
        self.assertEqual(otg_request.status, OtgDeliveryRequest.STATUS_DISPATCHED)

    def test_create_otg_pick_request_plans_full_pallets_before_partial_boxes(self):
        item = ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=370,
            comment="Коробов: 37; кратность: 10",
        )
        for pallet_index in range(1, 6):
            self._create_pallet_with_boxes(f"PAL-{pallet_index:02d}", boxes=10)

        _move_request, move_ids, shortage_qty = create_otg_shipping_pick_request(
            order=self.order,
            user=self.user,
            requested_by_name="OTG Manager",
            requested_by_role="manager",
        )

        self.assertEqual(shortage_qty, 0)
        self.assertEqual(len(move_ids), 4)
        self.assertEqual(MoveRequest.objects.count(), 1)
        self.assertEqual(MoveTask.objects.count(), 4)
        otg_request = OtgDeliveryRequest.objects.get()
        self.assertEqual(otg_request.requested_boxes, 37)
        self.assertEqual(otg_request.planned_boxes, 37)
        self.assertEqual(otg_request.status, OtgDeliveryRequest.STATUS_DISPATCHED)
        self.assertEqual(OtgPalletPlan.objects.filter(plan_type=OtgPalletPlan.TYPE_FULL_PALLET).count(), 3)
        self.assertEqual(OtgPalletPlan.objects.filter(plan_type=OtgPalletPlan.TYPE_PICK_BOXES).count(), 1)

        partial_task = MoveTask.objects.exclude(move_mode=MoveTask.MODE_PALLET_FULL).get()
        self.assertEqual(partial_task.pallet_code, "PAL-04")
        self.assertEqual(partial_task.move_mode, MoveTask.MODE_BOX_FULL)
        self.assertEqual(partial_task.payload.get("requested_box_count"), 7)
        self.assertEqual(partial_task.payload.get("requested_box_selection"), "pattern_matching")
        patterns = partial_task.payload.get("requested_box_patterns") or []
        self.assertEqual(patterns[0]["requested_box_count"], 7)
        self.assertEqual(patterns[0]["barcode_qty"], {"460000000001": 10})
        request_item = partial_task.request.items.get(sku_code=item.sku_code)
        self.assertEqual(request_item.qty_planned, 370)

    def test_create_otg_pick_request_batches_same_pallet_boxes_into_one_trip(self):
        first_item = ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-A",
            name="Item A",
            size="A",
            barcode="111",
            goods_type="Ready",
            qty_requested=10,
            comment="Коробов: 1; кратность: 10",
        )
        second_item = ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-B",
            name="Item B",
            size="B",
            barcode="222",
            goods_type="Ready",
            qty_requested=20,
            comment="Коробов: 1; кратность: 20",
        )
        self._create_box(
            pallet_code="PAL-BATCH",
            box_code="BOX-A",
            sku="SKU-A",
            name="Item A",
            size="A",
            barcode="111",
            qty=10,
        )
        self._create_box(
            pallet_code="PAL-BATCH",
            box_code="BOX-B",
            sku="SKU-B",
            name="Item B",
            size="B",
            barcode="222",
            qty=20,
        )
        self._create_box(
            pallet_code="PAL-BATCH",
            box_code="BOX-REMAINDER",
            sku="SKU-C",
            name="Item C",
            size="C",
            barcode="333",
            qty=30,
        )

        _move_request, move_ids, shortage_qty = create_otg_shipping_pick_request(
            order=self.order,
            user=self.user,
            requested_by_name="OTG Manager",
            requested_by_role="manager",
        )

        self.assertEqual(shortage_qty, 0)
        self.assertEqual(len(move_ids), 1)
        task = MoveTask.objects.get()
        self.assertEqual(task.pallet_code, "PAL-BATCH")
        self.assertEqual(task.move_mode, MoveTask.MODE_BOX_FULL)
        self.assertEqual(task.payload.get("requested_box_count"), 2)
        self.assertEqual(task.payload.get("requested_qty"), 30)
        self.assertEqual(task.payload.get("requested_barcode_qty"), {"111": 10, "222": 20})
        self.assertEqual(task.payload.get("route_plan", {}).get("boxes_to_pick"), 2)
        self.assertEqual(task.payload.get("route_plan", {}).get("qty_to_pick"), 30)
        self.assertEqual(
            task.payload.get("request_items"),
            [
                {"shipping_item_id": first_item.id, "requested_qty": 10},
                {"shipping_item_id": second_item.id, "requested_qty": 20},
            ],
        )
        patterns = task.payload.get("requested_box_patterns") or []
        self.assertEqual(len(patterns), 2)
        self.assertEqual(
            {tuple(sorted(pattern.get("barcode_qty", {}).items())) for pattern in patterns},
            {(("111", 10),), (("222", 20),)},
        )
        plans = list(OtgPalletPlan.objects.order_by("id"))
        self.assertEqual(len(plans), 2)
        self.assertEqual({plan.move_task_id for plan in plans}, {task.id})

        payload = dict(task.payload or {})
        payload["mobile_execution"] = {
            "source_confirmed": True,
            "pallet_confirmed": True,
            "destination_confirmed": False,
            "boxes_scanned": ["BOX-A"],
            "units_scanned": {},
        }
        task.payload = payload
        task.save(update_fields=["payload", "updated_at"])
        snapshot = build_mobile_execution_snapshot(task.legacy_order_id)
        self.assertEqual(snapshot["boxes_scanned_count"], 1)
        self.assertFalse(snapshot["all_boxes_complete"])
        self.assertEqual(snapshot["current_step"], "boxes")

        payload = dict(task.payload or {})
        payload["mobile_execution"] = {
            **dict(payload.get("mobile_execution") or {}),
            "boxes_scanned": ["BOX-A", "BOX-B"],
        }
        task.payload = payload
        task.save(update_fields=["payload", "updated_at"])
        snapshot = build_mobile_execution_snapshot(task.legacy_order_id)
        self.assertEqual(snapshot["boxes_scanned_count"], 2)
        self.assertTrue(snapshot["all_boxes_complete"])
        self.assertEqual(snapshot["current_step"], "destination")

    def test_otg_mobile_hint_names_only_the_remaining_box_pattern(self):
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-A",
            name="Товар A",
            size="A",
            barcode="111",
            goods_type="Ready",
            qty_requested=10,
            comment="Коробов: 2; кратность: 5",
        )
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-B",
            name="Товар B",
            size="B",
            barcode="222",
            goods_type="Ready",
            qty_requested=5,
            comment="Коробов: 1; кратность: 5",
        )
        for box_code in ["BOX-A-1", "BOX-A-2", "BOX-A-EXTRA"]:
            self._create_box(
                pallet_code="PAL-HINT",
                box_code=box_code,
                sku="SKU-A",
                name="Товар A",
                size="A",
                barcode="111",
                qty=5,
            )
        self._create_box(
            pallet_code="PAL-HINT",
            box_code="BOX-B-1",
            sku="SKU-B",
            name="Товар B",
            size="B",
            barcode="222",
            qty=5,
        )

        _move_request, move_ids, shortage_qty = create_otg_shipping_pick_request(
            order=self.order,
            user=self.user,
            requested_by_name="OTG Manager",
            requested_by_role="manager",
        )

        self.assertEqual(shortage_qty, 0)
        self.assertEqual(len(move_ids), 1)
        task = MoveTask.objects.get(legacy_order_id=move_ids[0])
        stock_before = list(
            WarehouseStockSnapshot.objects.filter(
                container_code__in=["BOX-A-1", "BOX-A-2", "BOX-A-EXTRA", "BOX-B-1"]
            )
            .order_by("id")
            .values_list("id", "qty", "available_qty", "zone_code", "location_id")
        )
        payload = dict(task.payload or {})
        payload["mobile_execution"] = {
            "source_confirmed": True,
            "pallet_confirmed": True,
            "destination_confirmed": False,
            "boxes_scanned": ["BOX-A-1", "BOX-A-2"],
            "units_scanned": {},
        }
        task.payload = payload
        task.status = MoveTask.STATUS_IN_PROGRESS
        task.save(update_fields=["payload", "status", "updated_at"])

        snapshot = build_otg_mobile_execution_snapshot(task.legacy_order_id)

        self.assertEqual(
            snapshot["remaining_box_summary"],
            "1 кор. x 5 шт (SKU-B; ШК 222; тип ready)",
        )
        self.assertIn(
            "Осталось подобрать: 1 кор. x 5 шт (SKU-B; ШК 222; тип ready).",
            snapshot["prompt"],
        )
        self.assertEqual(snapshot["expected_scan"], snapshot["remaining_box_summary"])

        rejected = _append_otg_remaining_box_hint(
            MoveTaskCommandResult(
                ok=False,
                error=(
                    "Короб BOX-A-EXTRA не подходит под оставшуюся коробочную схему. "
                    "Отсканируйте другой подходящий короб."
                ),
            ),
            task.legacy_order_id,
        )

        self.assertIn(
            "Осталось подобрать: 1 кор. x 5 шт (SKU-B; ШК 222; тип ready).",
            rejected.error,
        )
        task.refresh_from_db()
        self.assertEqual(
            task.payload.get("mobile_execution", {}).get("boxes_scanned"),
            ["BOX-A-1", "BOX-A-2"],
        )
        self.assertEqual(
            list(
                WarehouseStockSnapshot.objects.filter(
                    container_code__in=["BOX-A-1", "BOX-A-2", "BOX-A-EXTRA", "BOX-B-1"]
                )
                .order_by("id")
                .values_list("id", "qty", "available_qty", "zone_code", "location_id")
            ),
            stock_before,
        )

    @override_settings(USE_OTG_REACHTRUCK_PLANNER=True)
    def test_shipping_create_pick_tasks_uses_otg_planner_when_flag_enabled(self):
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=70,
            comment="Коробов: 7; кратность: 10",
        )
        self._create_pallet_with_boxes("PAL-01", boxes=10)

        move_ids = create_pick_tasks(
            self.order,
            self.user,
            requested_by_name="OTG Manager",
            requested_by_role="manager",
        )

        self.assertEqual(len(move_ids), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, ShippingOrder.STATUS_PICKING)
        self.assertEqual(OtgDeliveryRequest.objects.count(), 1)
        task = MoveTask.objects.get()
        self.assertEqual(task.pallet_code, "PAL-01")
        self.assertEqual(task.payload.get("otg_boxes_planned"), 7)
        self.assertEqual(task.payload.get("otg_scan_fact_mode"), "scan_facts_v1")
        self.assertEqual(task.payload.get("selected_box_codes"), [])
        self.assertEqual(task.payload.get("reserved_box_codes"), [])
        self.assertEqual(task.payload.get("route_plan", {}).get("pallet_code"), "PAL-01")

    def test_free_otg_box_is_never_auto_claimed_without_scan(self):
        result = _claim_free_otg_boxes_for_order(
            self.order,
            [{"boxes_required": 1, "composition": []}],
            performed_by=self.user,
        )

        self.assertEqual(result, [])

    def test_scan_fact_task_keeps_route_advisory_when_mobile_snapshot_is_built(self):
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=70,
            comment="Коробов: 7; кратность: 10",
        )
        self._create_pallet_with_boxes("PAL-01", boxes=10)
        create_otg_shipping_pick_request(order=self.order, user=self.user)
        task = MoveTask.objects.get()

        snapshot = build_mobile_execution_snapshot(task.legacy_order_id)

        task.refresh_from_db()
        self.assertTrue(snapshot.get("scan_fact_mode"))
        self.assertEqual(task.payload.get("selected_box_codes"), [])
        self.assertEqual(task.payload.get("reserved_box_codes"), [])
        self.assertNotIn("otg_live_retarget", task.payload)

    def test_new_scan_fact_request_does_not_credit_box_already_in_otg(self):
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=10,
            comment="Коробов: 1; кратность: 10",
        )
        delivered = self._create_box(
            pallet_code="PAL-OTG",
            box_code="PAL-OTG-BX-01",
            zone="OTG",
            warehouse_state_code="in_otg",
        )
        delivered.source_context_type = "shipping"
        delivered.source_context_id = self.order.number
        delivered.save(update_fields=["source_context_type", "source_context_id", "updated_at"])
        before = (delivered.qty, delivered.available_qty, delivered.warehouse_state_code)

        _move_request, move_ids, shortage_qty = create_otg_shipping_pick_request(
            order=self.order,
            user=self.user,
        )

        delivered.refresh_from_db()
        otg_request = OtgDeliveryRequest.objects.get()
        self.assertEqual(move_ids, [])
        self.assertEqual(shortage_qty, 10)
        self.assertEqual(otg_request.requested_boxes, 1)
        self.assertEqual(otg_request.planned_boxes, 0)
        self.assertEqual(otg_request.shortage_boxes, 1)
        self.assertEqual(otg_request.status, OtgDeliveryRequest.STATUS_BLOCKED)
        self.assertEqual(otg_request.payload.get("otg_already_delivered", {}).get("boxes"), [])
        self.assertEqual(OtgPalletPlan.objects.count(), 0)
        self.assertEqual((delivered.qty, delivered.available_qty, delivered.warehouse_state_code), before)

    def test_supplemental_pick_creates_only_fact_shortage_without_mutating_order_or_stock(self):
        item = ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=70,
            comment="Коробов: 7; кратность: 10",
        )
        self.order.expected_boxes = 7
        self.order.status = ShippingOrder.STATUS_PICKING
        self.order.save(update_fields=["expected_boxes", "status", "updated_at"])
        source_request = OtgDeliveryRequest.objects.create(
            shipping_order=self.order,
            agency=self.agency,
            status=OtgDeliveryRequest.STATUS_PARTIAL,
            requested_boxes=7,
            planned_boxes=1,
            shortage_boxes=6,
            planning_error="Shortage: 6 boxes",
        )
        delivered = self._create_box(
            pallet_code="PAL-OTG",
            box_code="PAL-OTG-BX-01",
            zone="OTG",
            warehouse_state_code="in_otg",
        )
        delivered.source_context_type = "shipping"
        delivered.source_context_id = self.order.number
        delivered.save(update_fields=["source_context_type", "source_context_id", "updated_at"])
        for index in range(2, 8):
            self._create_box(pallet_code="PAL-SUP", box_code=f"PAL-SUP-BX-{index:02d}")
        delivered_before = (delivered.qty, delivered.available_qty, delivered.warehouse_state_code)

        preview = get_otg_supplemental_pick_preview(self.order)
        move_request, move_ids, created_preview = create_otg_shipping_supplemental_pick(
            order=self.order,
            user=self.user,
            requested_by_name="Storekeeper",
            requested_by_role="storekeeper",
        )

        self.assertTrue(preview["can_create"])
        self.assertEqual(preview["source_request_id"], source_request.id)
        self.assertEqual(preview["requested_boxes"], 6)
        self.assertEqual(preview["requested_qty"], 60)
        self.assertEqual(created_preview["requested_boxes"], 6)
        self.assertTrue(move_ids)
        self.assertEqual(MoveRequest.objects.filter(pk=move_request.pk).count(), 1)
        supplemental_request = OtgDeliveryRequest.objects.exclude(pk=source_request.pk).get()
        self.assertEqual(supplemental_request.requested_boxes, 6)
        self.assertEqual(supplemental_request.shortage_boxes, 0)
        self.assertEqual(supplemental_request.payload["request_reason"], "shipping_supplement_pick")
        task = MoveTask.objects.filter(request=move_request).get()
        self.assertEqual(task.payload.get("otg_scan_fact_mode"), "scan_facts_v1")
        self.assertEqual(task.payload.get("selected_box_codes"), [])
        self.assertEqual(task.payload.get("reserved_box_codes"), [])
        self.assertEqual(task.payload.get("route_plan", {}).get("boxes_to_pick"), 6)
        self.order.refresh_from_db()
        item.refresh_from_db()
        delivered.refresh_from_db()
        source_request.refresh_from_db()
        self.assertEqual((self.order.status, self.order.expected_boxes), (ShippingOrder.STATUS_PICKING, 7))
        self.assertEqual((item.qty_requested, item.qty_reserved, item.qty_shipped), (70, 0, 0))
        self.assertEqual((delivered.qty, delivered.available_qty, delivered.warehouse_state_code), delivered_before)
        self.assertEqual((source_request.planned_boxes, source_request.shortage_boxes), (1, 6))
        self.assertFalse(get_otg_supplemental_pick_preview(self.order)["can_create"])
        task.status = MoveTask.STATUS_DONE
        task.save(update_fields=["status", "updated_at"])
        self.assertFalse(get_otg_supplemental_pick_preview(self.order)["can_create"])

    def test_supplemental_pick_rolls_back_when_full_shortage_is_not_available(self):
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=70,
            comment="Коробов: 7; кратность: 10",
        )
        self.order.expected_boxes = 7
        self.order.status = ShippingOrder.STATUS_PICKING
        self.order.save(update_fields=["expected_boxes", "status", "updated_at"])
        OtgDeliveryRequest.objects.create(
            shipping_order=self.order,
            agency=self.agency,
            status=OtgDeliveryRequest.STATUS_PARTIAL,
            requested_boxes=7,
            planned_boxes=1,
            shortage_boxes=6,
        )
        delivered = self._create_box(
            pallet_code="PAL-OTG",
            box_code="PAL-OTG-BX-01",
            zone="OTG",
            warehouse_state_code="in_otg",
        )
        delivered.source_context_type = "shipping"
        delivered.source_context_id = self.order.number
        delivered.save(update_fields=["source_context_type", "source_context_id", "updated_at"])
        for index in range(2, 7):
            self._create_box(pallet_code="PAL-SUP", box_code=f"PAL-SUP-BX-{index:02d}")
        delivered_before = (delivered.qty, delivered.available_qty, delivered.warehouse_state_code)

        with self.assertRaisesMessage(ValidationError, "Новое задание не создано"):
            create_otg_shipping_supplemental_pick(order=self.order, user=self.user)

        delivered.refresh_from_db()
        self.assertEqual(OtgDeliveryRequest.objects.count(), 1)
        self.assertEqual(MoveRequest.objects.count(), 0)
        self.assertEqual(MoveTask.objects.count(), 0)
        self.assertEqual((delivered.qty, delivered.available_qty, delivered.warehouse_state_code), delivered_before)

    def test_supplemental_pick_does_not_replace_one_box_with_two_smaller_boxes(self):
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=10,
            comment="Коробов: 1; кратность: 10",
        )
        self.order.expected_boxes = 1
        self.order.status = ShippingOrder.STATUS_PICKING
        self.order.save(update_fields=["expected_boxes", "status", "updated_at"])
        source_request = OtgDeliveryRequest.objects.create(
            shipping_order=self.order,
            agency=self.agency,
            status=OtgDeliveryRequest.STATUS_PARTIAL,
            requested_boxes=1,
            planned_boxes=0,
            shortage_boxes=1,
        )
        first = self._create_box(
            pallet_code="PAL-SMALL",
            box_code="BOX-SMALL-1",
            qty=5,
        )
        second = self._create_box(
            pallet_code="PAL-SMALL",
            box_code="BOX-SMALL-2",
            qty=5,
        )
        before = {
            first.id: (first.qty, first.available_qty, first.warehouse_state_code),
            second.id: (second.qty, second.available_qty, second.warehouse_state_code),
        }

        preview = get_otg_supplemental_pick_preview(self.order)
        with self.assertRaisesMessage(ValidationError, "Новое задание не создано"):
            create_otg_shipping_supplemental_pick(order=self.order, user=self.user)

        self.assertTrue(preview["can_create"])
        self.assertEqual(preview["requested_boxes"], 1)
        self.assertEqual(preview["requested_qty"], 10)
        self.assertEqual(list(OtgDeliveryRequest.objects.all()), [source_request])
        self.assertEqual(MoveRequest.objects.count(), 0)
        self.assertEqual(MoveTask.objects.count(), 0)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(
            (first.qty, first.available_qty, first.warehouse_state_code),
            before[first.id],
        )
        self.assertEqual(
            (second.qty, second.available_qty, second.warehouse_state_code),
            before[second.id],
        )

    def test_piece_discrepancy_pick_prefers_open_box_and_does_not_move_stock(self):
        item = ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=30,
            comment="Коробов: 3; кратность: 10",
        )
        open_snapshot = self._create_box(
            pallet_code="PAL-OPEN",
            box_code="BOX-OPEN",
            qty=6,
            tier=2,
        )
        open_event = WarehouseEvent.objects.create(
            agency=self.agency,
            event_type="movement_completed",
            stock_context_type="shipping",
            stock_context_id="OLD-SHIPPING",
            container=open_snapshot.container,
            from_location=open_snapshot.location,
            to_location=open_snapshot.location,
            from_zone_code="OS",
            to_zone_code="OS",
            qty=4,
            payload={
                "partial_shipping_pick": True,
                "source_box_code": "BOX-OPEN",
            },
            occurred_at=timezone.now(),
        )
        open_snapshot.last_event = open_event
        open_snapshot.save(update_fields=["last_event", "updated_at"])
        closed_snapshot = self._create_box(
            pallet_code="PAL-CLOSED",
            box_code="BOX-CLOSED",
            qty=5,
            tier=1,
        )
        shortage_rows = [
            {
                "barcode": "460000000001",
                "missing_qty": 5,
                "shipping_item_id": item.id,
            }
        ]
        before = {
            snapshot.id: (
                snapshot.qty,
                snapshot.available_qty,
                snapshot.warehouse_state_code,
            )
            for snapshot in (open_snapshot, closed_snapshot)
        }

        preview = get_otg_discrepancy_pick_preview(
            order=self.order,
            shortage_rows=shortage_rows,
            correction_mode="piece",
        )
        move_request, move_ids, created_preview = (
            create_otg_shipping_discrepancy_pick(
                order=self.order,
                shortage_rows=shortage_rows,
                correction_mode="piece",
                user=self.user,
                requested_by_name="Storekeeper",
                requested_by_role="storekeeper",
                expected_plan=preview,
            )
        )

        self.assertTrue(preview["can_create"])
        self.assertEqual(preview["plan_rows"][0]["box_code"], "BOX-OPEN")
        self.assertEqual(preview["plan_rows"][0]["pick_qty"], 5)
        self.assertEqual(created_preview["plan_fingerprint"], preview["plan_fingerprint"])
        self.assertEqual(len(move_ids), 1)
        task = MoveTask.objects.get(request=move_request)
        self.assertEqual(task.pallet_code, "PAL-OPEN")
        self.assertEqual(task.payload.get("otg_scan_fact_mode"), "scan_facts_v1")
        self.assertEqual(task.payload.get("planned_box_codes"), ["BOX-OPEN"])
        self.assertEqual(
            MoveRequestItem.objects.get(request=move_request).qty_requested,
            5,
        )
        for snapshot in (open_snapshot, closed_snapshot):
            snapshot.refresh_from_db()
            self.assertEqual(
                (
                    snapshot.qty,
                    snapshot.available_qty,
                    snapshot.warehouse_state_code,
                ),
                before[snapshot.id],
            )

    def test_piece_discrepancy_pick_promotes_complete_closed_box_to_full_box(self):
        item = ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=30,
            comment="Коробов: 1; кратность: 30; короба: BOX-CLOSED-30",
        )
        source = self._create_box(
            pallet_code="PAL-CLOSED-30",
            box_code="BOX-CLOSED-30",
            qty=30,
        )
        other_source = self._create_box(
            pallet_code="PAL-CLOSED-30",
            box_code="BOX-OTHER-30",
            qty=30,
        )
        before = {
            row.id: (row.qty, row.available_qty, row.warehouse_state_code)
            for row in (source, other_source)
        }
        shortage_rows = [
            {
                "barcode": "460000000001",
                "missing_qty": 30,
                "shipping_item_id": item.id,
            }
        ]

        preview = get_otg_discrepancy_pick_preview(
            order=self.order,
            shortage_rows=shortage_rows,
            correction_mode="piece",
        )

        self.assertTrue(preview["can_create"], preview)
        self.assertEqual(preview["source_box_count"], 1)
        self.assertEqual(preview["plan_rows"][0]["box_code"], "BOX-CLOSED-30")
        self.assertEqual(preview["plan_rows"][0]["pick_qty"], 30)
        self.assertEqual(preview["plan_rows"][0]["effective_pick_mode"], "whole_box")
        self.assertEqual(len(preview["demand_payloads"]), 1)
        demand = preview["demand_payloads"][0]
        self.assertEqual(demand["demand_type"], OtgDeliveryDemand.TYPE_FULL_BOX)
        self.assertEqual(demand["pick_composition"], [])
        self.assertEqual(demand["payload"]["correction_mode"], "piece")
        self.assertEqual(demand["payload"]["effective_pick_mode"], "whole_box")

        move_request, move_ids, created_preview = create_otg_shipping_discrepancy_pick(
            order=self.order,
            shortage_rows=shortage_rows,
            correction_mode="piece",
            user=self.user,
            requested_by_name="Storekeeper",
            requested_by_role="storekeeper",
            expected_plan=preview,
        )

        self.assertEqual(created_preview["plan_fingerprint"], preview["plan_fingerprint"])
        self.assertEqual(len(move_ids), 1)
        task = MoveTask.objects.get(request=move_request)
        self.assertEqual(task.pallet_code, "PAL-CLOSED-30")
        self.assertEqual(task.move_mode, MoveTask.MODE_BOX_FULL)
        self.assertEqual(task.payload.get("move_mode"), MoveTask.MODE_BOX_FULL)
        self.assertFalse(task.payload.get("ship_as_loose_units"))
        self.assertEqual(task.payload.get("planned_box_codes"), ["BOX-CLOSED-30"])
        self.assertEqual(task.payload.get("requested_box_count"), 1)
        self.assertEqual(task.payload.get("requested_qty"), 30)
        for row in (source, other_source):
            row.refresh_from_db()
            self.assertEqual(
                (row.qty, row.available_qty, row.warehouse_state_code),
                before[row.id],
            )

    def test_piece_discrepancy_pick_uses_one_box_when_it_covers_shortage(self):
        item = ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=180,
            comment="Коробов: 1; кратность: 180",
        )
        open_snapshot = self._create_box(
            pallet_code="PAL-OPEN",
            box_code="BOX-OPEN-60",
            qty=60,
            tier=1,
        )
        open_event = WarehouseEvent.objects.create(
            agency=self.agency,
            event_type="movement_completed",
            stock_context_type="shipping",
            stock_context_id="OLD-SHIPPING",
            container=open_snapshot.container,
            from_location=open_snapshot.location,
            to_location=open_snapshot.location,
            from_zone_code="OS",
            to_zone_code="OS",
            qty=60,
            payload={
                "partial_shipping_pick": True,
                "source_box_code": "BOX-OPEN-60",
            },
            occurred_at=timezone.now(),
        )
        open_snapshot.last_event = open_event
        open_snapshot.save(update_fields=["last_event", "updated_at"])
        covering_snapshot = self._create_box(
            pallet_code="PAL-COVERING",
            box_code="BOX-COVERING-240",
            qty=240,
            tier=2,
        )
        shortage_rows = [
            {
                "barcode": "460000000001",
                "missing_qty": 180,
                "shipping_item_id": item.id,
            }
        ]
        before = {
            snapshot.id: (
                snapshot.qty,
                snapshot.available_qty,
                snapshot.warehouse_state_code,
            )
            for snapshot in (open_snapshot, covering_snapshot)
        }

        preview = get_otg_discrepancy_pick_preview(
            order=self.order,
            shortage_rows=shortage_rows,
            correction_mode="piece",
        )
        move_request, move_ids, created_preview = (
            create_otg_shipping_discrepancy_pick(
                order=self.order,
                shortage_rows=shortage_rows,
                correction_mode="piece",
                user=self.user,
                requested_by_name="Storekeeper",
                requested_by_role="storekeeper",
                expected_plan=preview,
            )
        )

        self.assertTrue(preview["can_create"])
        self.assertEqual(preview["source_box_count"], 1)
        self.assertEqual(len(preview["plan_rows"]), 1)
        self.assertEqual(preview["plan_rows"][0]["box_code"], "BOX-COVERING-240")
        self.assertEqual(preview["plan_rows"][0]["pick_qty"], 180)
        self.assertEqual(preview["plan_rows"][0]["effective_pick_mode"], "piece")
        self.assertEqual(
            preview["demand_payloads"][0]["demand_type"],
            OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT,
        )
        self.assertEqual(created_preview["plan_fingerprint"], preview["plan_fingerprint"])
        self.assertEqual(len(move_ids), 1)
        task = MoveTask.objects.get(request=move_request)
        self.assertEqual(task.pallet_code, "PAL-COVERING")
        self.assertEqual(task.move_mode, MoveTask.MODE_BOX_PARTIAL)
        self.assertTrue(task.payload.get("ship_as_loose_units"))
        self.assertEqual(task.payload.get("planned_box_codes"), ["BOX-COVERING-240"])
        self.assertEqual(
            MoveRequestItem.objects.get(request=move_request).qty_requested,
            180,
        )
        for snapshot in (open_snapshot, covering_snapshot):
            snapshot.refresh_from_db()
            self.assertEqual(
                (
                    snapshot.qty,
                    snapshot.available_qty,
                    snapshot.warehouse_state_code,
                ),
                before[snapshot.id],
            )

    def test_no_stock_records_incident_without_changing_stock(self):
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=170,
            comment="Коробов: 17; кратность: 10",
        )
        self._create_pallet_with_boxes("PAL-01", boxes=10)
        self._create_pallet_with_boxes("PAL-02", boxes=10)
        create_otg_shipping_pick_request(order=self.order, user=self.user)
        task = MoveTask.objects.order_by("id").first()
        task.status = MoveTask.STATUS_IN_PROGRESS
        payload = dict(task.payload or {})
        payload["status"] = MoveTask.STATUS_IN_PROGRESS
        payload["assigned_to_id"] = 17
        task.payload = payload
        task.save(update_fields=["status", "payload", "updated_at"])
        stock = WarehouseStockSnapshot.objects.filter(agency=self.agency).order_by("id").first()
        before = (stock.qty, stock.available_qty, stock.warehouse_state_code)

        result = report_otg_no_stock(
            legacy_order_id=task.legacy_order_id,
            user=self.user,
            employee_id=17,
            employee_name="Driver",
        )

        self.assertTrue(result.ok, result.error)
        task.refresh_from_db()
        stock.refresh_from_db()
        self.assertEqual(task.status, MoveTask.STATUS_FAILED)
        self.assertEqual(task.payload.get("no_stock_reports", [])[0]["pallet_code"], "PAL-01")
        self.assertEqual((stock.qty, stock.available_qty, stock.warehouse_state_code), before)
        self.assertTrue(MoveTask.objects.filter(status=MoveTask.STATUS_CREATED).exists())

    @override_settings(USE_OTG_REACHTRUCK_PLANNER=True)
    def test_no_stock_on_dispatched_request_allows_only_missing_box_supplement(self):
        item = ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=20,
            comment="Коробов: 2; кратность: 10",
        )
        self.order.expected_boxes = 2
        self.order.status = ShippingOrder.STATUS_PICKING
        self.order.save(update_fields=["expected_boxes", "status", "updated_at"])
        delivered = self._create_box(
            pallet_code="PAL-01",
            box_code="PAL-01-BX-01",
            row=1,
        )
        missing = self._create_box(
            pallet_code="PAL-02",
            box_code="PAL-02-BX-01",
            row=2,
        )
        create_otg_shipping_pick_request(order=self.order, user=self.user)
        source_request = OtgDeliveryRequest.objects.get()
        tasks = {task.pallet_code: task for task in MoveTask.objects.order_by("id")}
        self.assertEqual(set(tasks), {"PAL-01", "PAL-02"})

        delivered_task = tasks["PAL-01"]
        delivered_payload = dict(delivered_task.payload or {})
        delivered_payload["status"] = MoveTask.STATUS_DONE
        delivered_task.status = MoveTask.STATUS_DONE
        delivered_task.payload = delivered_payload
        delivered_task.save(update_fields=["status", "payload", "updated_at"])
        delivered.zone_code = "OTG"
        delivered.warehouse_state_code = "in_otg"
        delivered.source_context_type = "shipping"
        delivered.source_context_id = self.order.number
        delivered.save(
            update_fields=[
                "zone_code",
                "warehouse_state_code",
                "source_context_type",
                "source_context_id",
                "updated_at",
            ]
        )

        failed_task = tasks["PAL-02"]
        failed_payload = dict(failed_task.payload or {})
        failed_payload["status"] = MoveTask.STATUS_IN_PROGRESS
        failed_payload["assigned_to_id"] = 17
        failed_task.status = MoveTask.STATUS_IN_PROGRESS
        failed_task.payload = failed_payload
        failed_task.save(update_fields=["status", "payload", "updated_at"])
        result = report_otg_no_stock(
            legacy_order_id=failed_task.legacy_order_id,
            user=self.user,
            employee_id=17,
            employee_name="Driver",
        )
        replacement = self._create_box(
            pallet_code="PAL-03",
            box_code="PAL-03-BX-01",
            row=3,
        )
        missing_before = (missing.qty, missing.available_qty, missing.warehouse_state_code)
        replacement_before = (replacement.qty, replacement.available_qty, replacement.warehouse_state_code)

        preview = get_otg_supplemental_pick_preview(self.order)
        readiness = shipping_pick_readiness(self.order)
        move_request, move_ids, created_preview = create_otg_shipping_supplemental_pick(
            order=self.order,
            user=self.user,
            requested_by_name="Storekeeper",
            requested_by_role="storekeeper",
        )

        self.assertTrue(result.ok, result.error)
        self.assertTrue(preview["can_create"])
        self.assertEqual(preview["source_request_id"], source_request.id)
        self.assertEqual(preview["requested_boxes"], 1)
        self.assertFalse(readiness["can_pick"])
        self.assertIn("добор", str(readiness["reason"]).lower())
        self.assertEqual(created_preview["requested_boxes"], 1)
        self.assertEqual(len(move_ids), 1)
        supplemental_request = OtgDeliveryRequest.objects.exclude(pk=source_request.pk).get()
        self.assertEqual(supplemental_request.requested_boxes, 1)
        supplemental_task = MoveTask.objects.filter(request=move_request).get()
        self.assertEqual(supplemental_task.pallet_code, "PAL-03")
        self.assertEqual(supplemental_task.payload.get("route_plan", {}).get("boxes_to_pick"), 1)

        self.order.refresh_from_db()
        item.refresh_from_db()
        source_request.refresh_from_db()
        missing.refresh_from_db()
        replacement.refresh_from_db()
        self.assertEqual(self.order.status, ShippingOrder.STATUS_PICKING)
        self.assertEqual((item.qty_requested, item.qty_reserved, item.qty_shipped), (20, 0, 0))
        self.assertEqual(source_request.status, OtgDeliveryRequest.STATUS_DISPATCHED)
        self.assertEqual(source_request.shortage_boxes, 0)
        self.assertEqual((missing.qty, missing.available_qty, missing.warehouse_state_code), missing_before)
        self.assertEqual(
            (replacement.qty, replacement.available_qty, replacement.warehouse_state_code),
            replacement_before,
        )

    @override_settings(USE_OTG_REACHTRUCK_PLANNER=True)
    def test_no_stock_partial_pick_replans_exact_units_from_another_pallet(self):
        comment = encode_partial_box_split(
            {
                "kind": "partial_box_split",
                "group_key": "supplemental-partial-retry",
                "source_boxes": 1,
                "source_box_codes": ["PAL-MISSING-BX-01"],
                "source_box_pattern": [
                    {
                        "sku": "SKU-OTG",
                        "name": "OTG Item",
                        "size": "42",
                        "barcode": "460000000001",
                        "goods_type": "Ready",
                        "qty": 296,
                    }
                ],
                "pick_pattern": [
                    {
                        "sku": "SKU-OTG",
                        "name": "OTG Item",
                        "size": "42",
                        "barcode": "460000000001",
                        "goods_type": "Ready",
                        "qty": 150,
                    }
                ],
                "item_pick_qty": 150,
                "item_source_qty": 296,
            }
        )
        item = ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=150,
            comment=comment,
        )
        self.order.expected_boxes = 1
        self.order.status = ShippingOrder.STATUS_PICKING
        self.order.save(update_fields=["expected_boxes", "status", "updated_at"])
        missing = self._create_box(
            pallet_code="PAL-MISSING",
            box_code="PAL-MISSING-BX-01",
            qty=296,
            row=2,
        )
        create_otg_shipping_pick_request(order=self.order, user=self.user)
        source_request = OtgDeliveryRequest.objects.get()
        failed_task = MoveTask.objects.get()
        failed_payload = dict(failed_task.payload or {})
        failed_payload.update(
            {
                "status": MoveTask.STATUS_FAILED,
                "blocked_reason": "no_stock_at_planned_pallet",
            }
        )
        failed_task.status = MoveTask.STATUS_FAILED
        failed_task.payload = failed_payload
        failed_task.save(update_fields=["status", "payload", "updated_at"])
        replacement = self._create_box(
            pallet_code="PAL-REPLACEMENT",
            box_code="PAL-REPLACEMENT-BX-01",
            qty=200,
            row=3,
        )
        before = {
            missing.id: (missing.qty, missing.available_qty, missing.warehouse_state_code),
            replacement.id: (
                replacement.qty,
                replacement.available_qty,
                replacement.warehouse_state_code,
            ),
        }
        discrepancy_snapshot = shipping_discrepancy_snapshot(self.order)
        self.order.shipping_discrepancy_status = "pending"
        self.order.shipping_discrepancy_payload = {
            "workflow_version": 2,
            "status": "pending",
            "snapshot": discrepancy_snapshot,
            "missing_text": discrepancy_snapshot.get("missing_text") or "",
            "binding_snapshot": discrepancy_snapshot.get("binding_snapshot") or {},
        }
        self.order.save(
            update_fields=[
                "shipping_discrepancy_status",
                "shipping_discrepancy_payload",
                "updated_at",
            ]
        )

        preview = get_otg_supplemental_pick_preview(self.order)
        move_request, move_ids, created_preview = (
            create_otg_shipping_supplemental_pick(
                order=self.order,
                user=self.user,
                requested_by_name="Storekeeper",
                requested_by_role="storekeeper",
            )
        )

        self.assertTrue(preview["can_create"])
        self.assertTrue(preview["is_partial_retry"])
        self.assertEqual(preview["source_request_id"], source_request.id)
        self.assertEqual(preview["requested_boxes"], 1)
        self.assertEqual(preview["requested_qty"], 150)
        self.assertEqual(preview["requested_qty_by_item_id"], {str(item.id): 150})
        self.assertEqual(preview["excluded_pallet_codes"], ["PAL-MISSING"])
        self.assertEqual(preview["plan_rows"], [])
        self.assertEqual(created_preview["requested_qty"], 150)
        self.assertEqual(len(move_ids), 1)
        supplemental_task = MoveTask.objects.get(request=move_request)
        self.assertEqual(supplemental_task.pallet_code, "PAL-REPLACEMENT")
        self.assertEqual(
            supplemental_task.payload.get("planned_box_codes"),
            ["PAL-REPLACEMENT-BX-01"],
        )
        self.assertEqual(supplemental_task.payload.get("requested_qty"), 150)
        self.assertEqual(
            MoveRequestItem.objects.get(request=move_request).qty_requested,
            150,
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.shipping_discrepancy_status, "pickup_required")
        self.assertEqual(
            self.order.shipping_discrepancy_payload.get("additional_pick_move_ids"),
            move_ids,
        )
        self.assertEqual(
            self.order.shipping_discrepancy_payload.get("decision_source"),
            "confirmed_no_stock_operational_supplement",
        )
        for snapshot in (missing, replacement):
            snapshot.refresh_from_db()
            self.assertEqual(
                (snapshot.qty, snapshot.available_qty, snapshot.warehouse_state_code),
                before[snapshot.id],
            )

    def test_strict_scan_mode_never_prepares_a_reserve_swap(self):
        self._create_otg_destination()
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=10,
            comment="Коробов: 1; кратность: 10",
        )
        self._create_pallet_with_boxes("PAL-01", boxes=1)
        create_otg_shipping_pick_request(order=self.order, user=self.user)
        task = MoveTask.objects.get()
        task.status = MoveTask.STATUS_IN_PROGRESS
        payload = dict(task.payload or {})
        payload["status"] = MoveTask.STATUS_IN_PROGRESS
        task.payload = payload
        task.save(update_fields=["status", "payload", "updated_at"])

        result = prepare_otg_box_scan(
            legacy_order_id=task.legacy_order_id,
            scan_value="PAL-01-BX-01",
            user=self.user,
        )

        task.refresh_from_db()
        self.assertTrue(result.ok, result.error)
        self.assertFalse(result.changed)
        self.assertEqual(task.payload.get("selected_box_codes"), [])
        self.assertEqual(task.payload.get("reserved_box_codes"), [])

    def test_strict_scan_mode_suggests_free_equivalent_for_fbs_reserved_box(self):
        self._create_otg_destination()
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=10,
            comment="Коробов: 1; кратность: 10",
        )
        reserved_snapshot = self._create_box(
            pallet_code="PAL-01",
            box_code="PAL-01-BX-01",
        )
        free_snapshot = self._create_box(
            pallet_code="PAL-01",
            box_code="PAL-01-BX-02",
        )
        create_otg_shipping_pick_request(order=self.order, user=self.user)
        task = MoveTask.objects.get()
        self.assertEqual(task.payload.get("otg_scan_fact_mode"), "scan_facts_v1")

        reserve = WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_FBS_MOVEMENT,
            context_type="fbs_client_movement",
            context_id="777",
            sku_code=reserved_snapshot.sku_code,
            size=reserved_snapshot.size,
            barcode=reserved_snapshot.barcode,
            goods_type=reserved_snapshot.goods_type,
            qty_reserved=reserved_snapshot.qty,
            qty_allocated=reserved_snapshot.qty,
            status=WarehouseReserve.STATUS_ALLOCATED,
            source_document_type="fbs_client_movement",
            source_document_id="777",
            created_by=self.user,
        )
        reserved_snapshot.available_qty = 0
        reserved_snapshot.other_reserved_qty = reserved_snapshot.qty
        reserved_snapshot.save(
            update_fields=["available_qty", "other_reserved_qty", "updated_at"]
        )
        WarehouseEvent.objects.create(
            agency=self.agency,
            event_type="fbs_movement_reserved",
            stock_context_type="fbs_client_movement",
            stock_context_id="777",
            container=reserved_snapshot.container,
            reserve=reserve,
            source_document_type="fbs_client_movement",
            source_document_id="777",
            qty=reserved_snapshot.qty,
            payload={
                "source_snapshot_id": reserved_snapshot.id,
                "container_id": reserved_snapshot.container_id,
                "container_code": reserved_snapshot.container_code,
            },
            performed_by=self.user,
            occurred_at=timezone.now(),
        )

        task.status = MoveTask.STATUS_IN_PROGRESS
        payload = dict(task.payload or {})
        payload["status"] = MoveTask.STATUS_IN_PROGRESS
        execution = dict(payload.get("mobile_execution") or {})
        execution.update(
            {
                "pallet_confirmed": True,
                "source_confirmed": True,
                "destination_confirmed": False,
                "boxes_scanned": [],
            }
        )
        payload["mobile_execution"] = execution
        task.payload = payload
        task.save(update_fields=["status", "payload", "updated_at"])

        result = prepare_otg_box_scan(
            legacy_order_id=task.legacy_order_id,
            scan_value=reserved_snapshot.container_code,
            user=self.user,
        )

        self.assertFalse(result.ok)
        self.assertFalse(result.changed)
        self.assertIn(reserved_snapshot.container_code, result.error)
        self.assertIn("FBS-перемещение #777", result.error)
        self.assertIn(free_snapshot.container_code, result.error)
        self.assertIn("Не берите его", result.error)
        reserved_snapshot.refresh_from_db()
        free_snapshot.refresh_from_db()
        self.assertEqual(
            (reserved_snapshot.qty, reserved_snapshot.available_qty, reserved_snapshot.other_reserved_qty),
            (10, 0, 10),
        )
        self.assertEqual(
            (free_snapshot.qty, free_snapshot.available_qty, free_snapshot.other_reserved_qty),
            (10, 10, 0),
        )

    @override_settings(USE_OTG_REACHTRUCK_PLANNER=True)
    def test_shipping_pick_readiness_names_foreign_otg_task_occupying_source_pallet(self):
        comment = encode_partial_box_split(
            {
                "kind": "partial_box_split",
                "group_key": "busy-source-pallet",
                "source_boxes": 1,
                "source_box_codes": ["PAL-SOURCE-BX-01"],
                "source_box_pattern": [
                    {
                        "sku": "SKU-OTG",
                        "name": "OTG Item",
                        "size": "42",
                        "barcode": "460000000001",
                        "goods_type": "Ready",
                        "qty": 100,
                    }
                ],
                "pick_pattern": [
                    {
                        "sku": "SKU-OTG",
                        "name": "OTG Item",
                        "size": "42",
                        "barcode": "460000000001",
                        "goods_type": "Ready",
                        "qty": 25,
                    }
                ],
                "item_pick_qty": 25,
                "item_source_qty": 100,
            }
        )
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=25,
            comment=comment,
        )
        source = self._create_box(
            pallet_code="PAL-SOURCE",
            box_code="PAL-SOURCE-BX-01",
            qty=100,
            row=8,
        )
        blocking_order = ShippingOrder.objects.create(
            number="OTG-000246",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_PICKING,
        )
        blocking_request = self._create_claim_queue_request(str(blocking_order.pk))
        blocking_request.status = MoveRequest.STATUS_IN_PROGRESS
        blocking_request.comment = "OTG shipping OTG-000246"
        blocking_request.save(update_fields=["status", "comment", "updated_at"])
        self._create_claim_queue_task(
            move_request=blocking_request,
            legacy_order_id="OTG-000246-BUSY",
            pallet_code="PAL-SOURCE",
            status=MoveTask.STATUS_CREATED,
            box_qty=50,
        )
        OtgDeliveryRequest.objects.create(
            shipping_order=blocking_order,
            agency=self.agency,
            move_request=blocking_request,
            status=OtgDeliveryRequest.STATUS_IN_PROGRESS,
            requested_boxes=1,
            planned_boxes=1,
            shortage_boxes=0,
        )
        before = (source.qty, source.available_qty, self.order.status)

        preview = preview_otg_shipping_pick_coverage(self.order)
        readiness = shipping_pick_readiness(self.order)

        source.refresh_from_db()
        self.order.refresh_from_db()
        self.assertFalse(preview["can_cover"])
        self.assertEqual(preview["blocked_by_shipping_orders"], ["246_OTG"])
        self.assertEqual(
            readiness,
            {
                "can_pick": False,
                "reason": "Паллета занята заданием 246_OTG, дождитесь завершения.",
            },
        )
        self.assertEqual((source.qty, source.available_qty, self.order.status), before)
        self.assertFalse(OtgDeliveryRequest.objects.filter(shipping_order=self.order).exists())

    @override_settings(USE_OTG_REACHTRUCK_PLANNER=True)
    @patch("shipping.services.shipping_pick_readiness")
    def test_busy_pallet_creates_one_waiting_request_without_tasks(
        self,
        pick_readiness_mock,
    ):
        pick_readiness_mock.return_value = {
            "can_pick": False,
            "reason": "Паллета занята заданием 246_OTG, дождитесь завершения.",
        }
        self.order.expected_boxes = 3
        self.order.save(update_fields=["expected_boxes", "updated_at"])

        self.assertEqual(
            create_pick_tasks(
                self.order,
                self.user,
                requested_by_name="Storekeeper",
                requested_by_role="storekeeper",
            ),
            [],
        )
        self.assertEqual(create_pick_tasks(self.order, self.user), [])

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, ShippingOrder.STATUS_STOREKEEPER_ACCEPTED)
        waiting_requests = OtgDeliveryRequest.objects.filter(shipping_order=self.order)
        self.assertEqual(waiting_requests.count(), 1)
        waiting_request = waiting_requests.get()
        self.assertEqual(waiting_request.status, OtgDeliveryRequest.STATUS_BLOCKED)
        self.assertEqual(waiting_request.requested_boxes, 3)
        self.assertEqual(waiting_request.planned_boxes, 0)
        self.assertEqual(waiting_request.shortage_boxes, 0)
        self.assertTrue(waiting_request.payload.get("waiting_for_stock"))
        self.assertTrue(waiting_request.payload.get("auto_retry"))
        self.assertEqual(waiting_request.events.count(), 1)
        self.assertFalse(MoveRequest.objects.filter(context_id=str(self.order.pk)).exists())
        self.assertFalse(MoveTask.objects.filter(request__context_id=str(self.order.pk)).exists())

    def test_partial_pick_does_not_replace_source_box_used_by_another_shipping_task(self):
        comment = encode_partial_box_split(
            {
                "kind": "partial_box_split",
                "group_key": "strict-source-active-task",
                "source_boxes": 1,
                "source_box_codes": ["PAL-SOURCE-BX-01"],
                "source_box_pattern": [
                    {
                        "sku": "SKU-OTG",
                        "name": "OTG Item",
                        "size": "42",
                        "barcode": "460000000001",
                        "goods_type": "Ready",
                        "qty": 99,
                    }
                ],
                "pick_pattern": [
                    {
                        "sku": "SKU-OTG",
                        "name": "OTG Item",
                        "size": "42",
                        "barcode": "460000000001",
                        "goods_type": "Ready",
                        "qty": 33,
                    }
                ],
                "item_pick_qty": 33,
                "item_source_qty": 99,
            }
        )
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=33,
            comment=comment,
        )
        source = self._create_box(
            pallet_code="PAL-SOURCE",
            box_code="PAL-SOURCE-BX-01",
            qty=99,
            row=8,
        )
        replacement = self._create_box(
            pallet_code="PAL-REPLACEMENT",
            box_code="PAL-REPLACEMENT-BX-01",
            qty=100,
            row=9,
        )
        blocker = self._create_claim_queue_task(
            move_request=self._create_claim_queue_request("OTG-OTHER"),
            legacy_order_id="OTG-OTHER-1",
            pallet_code="PAL-SOURCE",
        )
        blocker_payload = dict(blocker.payload or {})
        blocker_payload["shipping_order_id"] = "SO-OTHER"
        blocker_payload["planned_box_codes"] = [source.container_code]
        blocker.payload = blocker_payload
        blocker.save(update_fields=["payload", "updated_at"])
        source_before = (source.qty, source.available_qty)
        replacement_before = (replacement.qty, replacement.available_qty)

        _move_request, move_ids, shortage_qty = create_otg_shipping_pick_request(
            order=self.order,
            user=self.user,
        )

        source.refresh_from_db()
        replacement.refresh_from_db()
        otg_request = OtgDeliveryRequest.objects.get(shipping_order=self.order)
        self.assertEqual(move_ids, [])
        self.assertEqual(shortage_qty, 33)
        self.assertEqual(otg_request.status, OtgDeliveryRequest.STATUS_BLOCKED)
        self.assertEqual(otg_request.planned_boxes, 0)
        self.assertEqual(otg_request.shortage_boxes, 1)
        self.assertFalse(OtgPalletPlan.objects.filter(request=otg_request).exists())
        self.assertEqual((source.qty, source.available_qty), source_before)
        self.assertEqual((replacement.qty, replacement.available_qty), replacement_before)

    def test_rebind_reserves_partial_source_before_selecting_regular_boxes(self):
        regular_item = ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=70,
            qty_reserved=70,
            comment="Коробов: 2; кратность: 35; короба: BOX-OLD-01, BOX-OLD-02",
        )
        partial_comment = encode_partial_box_split(
            {
                "kind": "partial_box_split",
                "group_key": "partial-source-must-stay-exclusive",
                "source_boxes": 1,
                "source_box_codes": ["BOX-ANCHOR"],
                "source_box_pattern": [
                    {
                        "sku": "SKU-OTG",
                        "name": "OTG Item",
                        "size": "42",
                        "barcode": "460000000001",
                        "goods_type": "Ready",
                        "qty": 35,
                    }
                ],
                "pick_pattern": [
                    {
                        "sku": "SKU-OTG",
                        "name": "OTG Item",
                        "size": "42",
                        "barcode": "460000000001",
                        "goods_type": "Ready",
                        "qty": 30,
                    }
                ],
                "item_pick_qty": 30,
                "item_source_qty": 35,
            }
        )
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=30,
            qty_reserved=30,
            comment=partial_comment,
        )
        self._create_box(
            pallet_code="PAL-A-ANCHOR",
            box_code="BOX-ANCHOR",
            qty=35,
            row=20,
        )
        self._create_box(
            pallet_code="PAL-B-REGULAR",
            box_code="BOX-REGULAR-01",
            qty=35,
            row=21,
        )
        self._create_box(
            pallet_code="PAL-C-REGULAR",
            box_code="BOX-REGULAR-02",
            qty=35,
            row=22,
        )

        changes = rebind_unavailable_shipping_source_boxes(self.order)

        regular_item.refresh_from_db()
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["item_id"], regular_item.id)
        self.assertEqual(
            changes[0]["new_boxes"],
            ["BOX-REGULAR-01", "BOX-REGULAR-02"],
        )
        self.assertNotIn("BOX-ANCHOR", changes[0]["new_boxes"])
        preview = preview_otg_shipping_pick_coverage(self.order)
        self.assertTrue(preview["can_cover"], preview)
        self.assertEqual(preview["shortage_boxes"], 0)
        self.assertEqual(preview["shortage_qty"], 0)

    def test_rebind_does_not_fall_back_to_stale_comment_after_actual_delivery(self):
        current_item = ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=10,
            qty_reserved=10,
            comment="Коробов: 1; кратность: 10; короба: BOX-ORIGINAL",
        )
        original = self._create_box(
            pallet_code="PAL-ORIGINAL",
            box_code="BOX-ORIGINAL",
            qty=10,
            row=30,
        )
        self._create_box(
            pallet_code="PAL-ACTUAL",
            box_code="BOX-ACTUAL",
            qty=10,
            row=31,
            zone="OTG",
            warehouse_state_code="in_otg",
        )
        other = ShippingOrder.objects.create(
            number="SO-OTG-OLDER",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_PICKING,
        )
        ShippingOrderItem.objects.create(
            order=other,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=10,
            qty_reserved=10,
            comment="Коробов: 1; кратность: 10; короба: BOX-ORIGINAL",
        )
        move_request = self._create_claim_queue_request(str(other.pk))
        move_task = self._create_claim_queue_task(
            move_request=move_request,
            legacy_order_id="SO-OTG-OLDER-TASK",
            pallet_code="PAL-ACTUAL",
            status=MoveTask.STATUS_DONE,
        )
        BoxClaim.objects.create(
            agency=self.agency,
            move_task=move_task,
            box_code="BOX-ACTUAL",
            pallet_code="PAL-ACTUAL",
            claim_kind=BoxClaim.KIND_BOX,
            shipping_order_id=other.number,
            shipping_order_pk=other.pk,
            status=BoxClaim.STATUS_DELIVERED,
            claimed_by=self.user,
            claimed_at=timezone.now(),
            delivered_at=timezone.now(),
            released_at=timezone.now(),
        )

        changes = rebind_unavailable_shipping_source_boxes(self.order)

        current_item.refresh_from_db()
        self.assertEqual(changes, [])
        self.assertIn(original.container_code, current_item.comment)

    def test_partial_pick_uses_identified_source_when_remaining_qty_covers_pick(self):
        comment = encode_partial_box_split(
            {
                "kind": "partial_box_split",
                "group_key": "strict-source-stale-qty",
                "source_boxes": 1,
                "source_box_codes": ["PAL-SOURCE-BX-01"],
                "source_box_pattern": [
                    {
                        "sku": "SKU-OTG",
                        "name": "OTG Item",
                        "size": "42",
                        "barcode": "460000000001",
                        "goods_type": "Ready",
                        "qty": 99,
                    }
                ],
                "pick_pattern": [
                    {
                        "sku": "SKU-OTG",
                        "name": "OTG Item",
                        "size": "42",
                        "barcode": "460000000001",
                        "goods_type": "Ready",
                        "qty": 33,
                    }
                ],
                "item_pick_qty": 33,
                "item_source_qty": 99,
            }
        )
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=33,
            comment=comment,
        )
        source = self._create_box(
            pallet_code="PAL-SOURCE",
            box_code="PAL-SOURCE-BX-01",
            qty=66,
            row=10,
        )
        replacement = self._create_box(
            pallet_code="PAL-REPLACEMENT",
            box_code="PAL-REPLACEMENT-BX-01",
            qty=99,
            row=11,
        )
        source_before = (source.qty, source.available_qty)
        replacement_before = (replacement.qty, replacement.available_qty)

        _move_request, move_ids, shortage_qty = create_otg_shipping_pick_request(
            order=self.order,
            user=self.user,
        )

        source.refresh_from_db()
        replacement.refresh_from_db()
        otg_request = OtgDeliveryRequest.objects.get(shipping_order=self.order)
        self.assertEqual(len(move_ids), 1)
        self.assertEqual(shortage_qty, 0)
        self.assertEqual(otg_request.status, OtgDeliveryRequest.STATUS_DISPATCHED)
        self.assertEqual(otg_request.planned_boxes, 1)
        self.assertEqual(otg_request.shortage_boxes, 0)
        plan = OtgPalletPlan.objects.get(request=otg_request)
        self.assertEqual(plan.pallet_code, "PAL-SOURCE")
        self.assertEqual(plan.planned_box_codes, ["PAL-SOURCE-BX-01"])
        self.assertEqual((source.qty, source.available_qty), source_before)
        self.assertEqual((replacement.qty, replacement.available_qty), replacement_before)

    def test_new_scan_fact_task_plans_partial_pick_from_pr(self):
        comment = encode_partial_box_split(
            {
                "kind": "partial_box_split",
                "group_key": "pr-partial",
                "source_boxes": 1,
                "source_box_codes": ["PAL-PR-BX-01"],
                "source_box_pattern": [
                    {
                        "sku": "SKU-OTG",
                        "name": "OTG Item",
                        "size": "42",
                        "barcode": "460000000001",
                        "goods_type": "Ready",
                        "qty": 10,
                    }
                ],
                "pick_pattern": [
                    {
                        "sku": "SKU-OTG",
                        "name": "OTG Item",
                        "size": "42",
                        "barcode": "460000000001",
                        "goods_type": "Ready",
                        "qty": 3,
                    }
                ],
                "item_pick_qty": 3,
            }
        )
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=3,
            comment=comment,
        )
        self._create_box(
            pallet_code="PAL-PR",
            box_code="PAL-PR-BX-01",
            qty=10,
            zone="PR",
            warehouse_state_code="placed_in_receiving",
        )

        _move_request, move_ids, shortage_qty = create_otg_shipping_pick_request(
            order=self.order,
            user=self.user,
        )

        self.assertEqual(shortage_qty, 0)
        self.assertEqual(len(move_ids), 1)
        task = MoveTask.objects.get()
        self.assertEqual(task.payload.get("otg_scan_fact_mode"), "scan_facts_v1")
        self.assertEqual(task.payload.get("from_location", {}).get("zone"), "PR")
        self.assertTrue(task.payload.get("ship_as_loose_units"))

    def test_otg_pick_supersedes_unstarted_obr_to_storage_route_without_stock_change(self):
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=10,
            comment="Коробов: 1; кратность: 10",
        )
        source = self._create_box(
            pallet_code="PAL-OBR-DIRECT",
            box_code="PAL-OBR-DIRECT-BX-01",
            qty=10,
            zone="OBR",
            warehouse_state_code="placed_after_processing",
        )
        old_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_PROCESSING,
            context_id="40",
            agency=self.agency,
            destination_zone="OS",
            status=MoveRequest.STATUS_PLANNED,
        )
        old_task = MoveTask.objects.create(
            request=old_request,
            pallet_code="PAL-OBR-DIRECT",
            from_zone="OBR",
            to_zone="OS",
            move_mode=MoveTask.MODE_PALLET_FULL,
            qty_planned=10,
            status=MoveTask.STATUS_CREATED,
            legacy_order_id="MOVE-OBR-DIRECT-1",
            payload={
                "status": "created",
                "processing_order_id": "40",
                "from_location": {"zone": "OBR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "move_mode": MoveTask.MODE_PALLET_FULL,
            },
        )
        stock_before = (source.qty, source.available_qty, source.processing_reserved_qty, source.shipping_reserved_qty)

        preview = preview_otg_shipping_pick_coverage(self.order)
        move_request, move_ids, shortage_qty = create_otg_shipping_pick_request(
            order=self.order,
            user=self.user,
            allow_partial=False,
        )

        source.refresh_from_db()
        old_task.refresh_from_db()
        old_request.refresh_from_db()
        self.assertTrue(preview["can_cover"])
        self.assertEqual(shortage_qty, 0)
        self.assertEqual(len(move_ids), 1)
        self.assertEqual(old_task.status, MoveTask.STATUS_CANCELED)
        self.assertEqual(old_request.status, MoveRequest.STATUS_CANCELED)
        self.assertEqual(
            old_task.payload.get("superseded_by_otg_shipping", {}).get("shipping_order_id"),
            self.order.number,
        )
        new_task = MoveTask.objects.get(request=move_request)
        self.assertEqual(new_task.from_zone, "OBR")
        self.assertEqual(new_task.to_zone, "OTG")
        self.assertEqual(
            (source.qty, source.available_qty, source.processing_reserved_qty, source.shipping_reserved_qty),
            stock_before,
        )

    def test_otg_pick_allows_disjoint_unstarted_fbs_box_route_on_same_pallet(self):
        self._create_otg_destination()
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=10,
            comment="Коробов: 1; кратность: 10",
        )
        otg_source = self._create_box(
            pallet_code="PAL-SHARED-BOXES",
            box_code="BOX-FOR-OTG",
            qty=10,
            zone="PR",
            warehouse_state_code="placed_in_receiving",
        )
        fbs_source = self._create_box(
            pallet_code="PAL-SHARED-BOXES",
            box_code="BOX-FOR-FBS",
            qty=10,
            zone="PR",
            warehouse_state_code="placed_in_receiving",
        )
        fbs_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id="fbs-movement:56",
            agency=self.agency,
            destination_zone="OBR",
            status=MoveRequest.STATUS_IN_PROGRESS,
        )
        fbs_task = MoveTask.objects.create(
            request=fbs_request,
            pallet_code="PAL-SHARED-BOXES",
            from_zone="PR",
            to_zone="OBR",
            move_mode=MoveTask.MODE_BOX_FULL,
            qty_planned=10,
            status=MoveTask.STATUS_CREATED,
            legacy_order_id="FBS-MOV-000056",
            payload={
                "status": "created",
                "move_mode": MoveTask.MODE_BOX_FULL,
                "requested_box_selection": "fixed",
                "requested_box_count": 1,
                "requested_boxes": ["BOX-FOR-FBS"],
                "selected_box_codes": ["BOX-FOR-FBS"],
                "fbs_replenishment_bridge_v1": True,
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OBR"},
            },
        )
        fbs_claim = BoxClaim.objects.create(
            agency=self.agency,
            move_task=fbs_task,
            box_code="BOX-FOR-FBS",
            pallet_code="PAL-SHARED-BOXES",
            status=BoxClaim.STATUS_CLAIMED,
        )
        stock_before = sorted(
            [
                (otg_source.id, otg_source.qty, otg_source.available_qty),
                (fbs_source.id, fbs_source.qty, fbs_source.available_qty),
            ]
        )

        preview = preview_otg_shipping_pick_coverage(self.order)
        move_request, move_ids, shortage_qty = create_otg_shipping_pick_request(
            order=self.order,
            user=self.user,
            allow_partial=False,
        )

        fbs_request.refresh_from_db()
        fbs_task.refresh_from_db()
        fbs_claim.refresh_from_db()
        self.assertTrue(preview["can_cover"], preview)
        self.assertEqual(shortage_qty, 0)
        self.assertEqual(len(move_ids), 1)
        self.assertEqual(fbs_request.status, MoveRequest.STATUS_IN_PROGRESS)
        self.assertEqual(fbs_task.status, MoveTask.STATUS_CREATED)
        self.assertEqual(fbs_claim.status, BoxClaim.STATUS_CLAIMED)
        plan = OtgPalletPlan.objects.get(move_task__request=move_request)
        self.assertEqual(plan.pallet_code, "PAL-SHARED-BOXES")
        self.assertEqual(plan.planned_box_codes, ["BOX-FOR-OTG"])
        self.assertEqual(
            list(
                WarehouseStockSnapshot.objects.filter(id__in=[otg_source.id, fbs_source.id])
                .order_by("id")
                .values_list("id", "qty", "available_qty")
            ),
            stock_before,
        )

    def test_otg_route_gate_blocks_same_box_in_unstarted_fbs_route(self):
        fbs_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id="fbs-movement:56",
            agency=self.agency,
            destination_zone="OBR",
            status=MoveRequest.STATUS_IN_PROGRESS,
        )
        fbs_task = MoveTask.objects.create(
            request=fbs_request,
            pallet_code="PAL-SHARED-BOXES",
            from_zone="PR",
            to_zone="OBR",
            move_mode=MoveTask.MODE_BOX_FULL,
            qty_planned=10,
            status=MoveTask.STATUS_CREATED,
            legacy_order_id="FBS-MOV-SAME-BOX",
            payload={
                "move_mode": MoveTask.MODE_BOX_FULL,
                "requested_boxes": ["BOX-SHARED"],
                "fbs_replenishment_bridge_v1": True,
            },
        )

        with self.assertRaisesMessage(ValidationError, "уже взята в работу"):
            _cancel_preemptible_storage_routes_for_otg(
                agency_id=self.agency.id,
                pallet_codes={"PAL-SHARED-BOXES"},
                selected_box_keys_by_pallet={"pal-shared-boxes": {"box-shared"}},
                order=self.order,
            )

        fbs_task.refresh_from_db()
        self.assertEqual(fbs_task.status, MoveTask.STATUS_CREATED)

    def test_otg_route_gate_blocks_started_disjoint_box_route(self):
        fbs_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id="fbs-movement:56",
            agency=self.agency,
            destination_zone="OBR",
            status=MoveRequest.STATUS_IN_PROGRESS,
        )
        fbs_task = MoveTask.objects.create(
            request=fbs_request,
            pallet_code="PAL-SHARED-BOXES",
            from_zone="PR",
            to_zone="OBR",
            move_mode=MoveTask.MODE_BOX_FULL,
            qty_planned=10,
            status=MoveTask.STATUS_IN_PROGRESS,
            assigned_to=self.user,
            assigned_to_name="Driver",
            started_at=timezone.now(),
            legacy_order_id="FBS-MOV-STARTED",
            payload={
                "move_mode": MoveTask.MODE_BOX_FULL,
                "requested_boxes": ["BOX-FOR-FBS"],
                "fbs_replenishment_bridge_v1": True,
                "mobile_execution": {"source_confirmed": True},
            },
        )

        with self.assertRaisesMessage(ValidationError, "уже взята в работу"):
            _cancel_preemptible_storage_routes_for_otg(
                agency_id=self.agency.id,
                pallet_codes={"PAL-SHARED-BOXES"},
                selected_box_keys_by_pallet={"pal-shared-boxes": {"box-for-otg"}},
                order=self.order,
            )

        fbs_task.refresh_from_db()
        self.assertEqual(fbs_task.status, MoveTask.STATUS_IN_PROGRESS)

    def test_otg_pick_cancels_only_unstarted_linked_putaway_operation(self):
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=10,
            comment="Коробов: 1; кратность: 10",
        )
        source = self._create_box(
            pallet_code="PAL-OBR-OP",
            box_code="PAL-OBR-OP-BX-01",
            qty=10,
            zone="OBR",
            warehouse_state_code="placed_after_processing",
        )
        operation = WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PUTAWAY,
            context_type="processing",
            context_id="41",
            source_zone_code="OBR",
            destination_zone_code="OS",
            status=WarehouseOperation.STATUS_PLANNED,
            planned_qty=10,
        )
        warehouse_task = WarehouseOperationTask.objects.create(
            operation=operation,
            task_type=WarehouseOperationTask.TYPE_PALLET_MOVE,
            from_zone_code="OBR",
            to_zone_code="OS",
            qty_planned=10,
            status=WarehouseOperationTask.STATUS_CREATED,
            payload={"snapshot_ids": [source.id]},
        )
        source.active_operation = operation
        source.active_operation_type = operation.operation_type
        source.save(update_fields=["active_operation", "active_operation_type", "updated_at"])
        old_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_PROCESSING,
            context_id="41",
            agency=self.agency,
            destination_zone="OS",
            status=MoveRequest.STATUS_PLANNED,
        )
        old_task = MoveTask.objects.create(
            request=old_request,
            pallet_code="PAL-OBR-OP",
            from_zone="OBR",
            to_zone="OS",
            move_mode=MoveTask.MODE_PALLET_FULL,
            qty_planned=10,
            status=MoveTask.STATUS_CREATED,
            legacy_order_id="MOVE-OBR-OP-1",
            payload={
                "status": "created",
                "processing_order_id": "41",
                "from_location": {"zone": "OBR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "move_mode": MoveTask.MODE_PALLET_FULL,
                "warehouse_operation_id": operation.id,
                "warehouse_operation_task_id": warehouse_task.id,
            },
        )
        stock_before = (source.qty, source.available_qty)

        preview = preview_otg_shipping_pick_coverage(self.order)
        _move_request, move_ids, shortage_qty = create_otg_shipping_pick_request(
            order=self.order,
            user=self.user,
            allow_partial=False,
        )

        source.refresh_from_db()
        old_task.refresh_from_db()
        warehouse_task.refresh_from_db()
        operation.refresh_from_db()
        self.assertTrue(preview["can_cover"])
        self.assertEqual(shortage_qty, 0)
        self.assertEqual(len(move_ids), 1)
        self.assertEqual(old_task.status, MoveTask.STATUS_CANCELED)
        self.assertEqual(warehouse_task.status, WarehouseOperationTask.STATUS_CANCELED)
        self.assertEqual(operation.status, WarehouseOperation.STATUS_CANCELED)
        self.assertIsNone(source.active_operation_id)
        self.assertEqual((source.qty, source.available_qty), stock_before)

    def test_otg_pick_keeps_started_obr_route_blocked(self):
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-OTG",
            name="OTG Item",
            size="42",
            barcode="460000000001",
            goods_type="Ready",
            qty_requested=10,
            comment="Коробов: 1; кратность: 10",
        )
        source = self._create_box(
            pallet_code="PAL-OBR-STARTED",
            box_code="PAL-OBR-STARTED-BX-01",
            qty=10,
            zone="OBR",
            warehouse_state_code="placed_after_processing",
        )
        old_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_PROCESSING,
            context_id="42",
            agency=self.agency,
            destination_zone="OS",
            status=MoveRequest.STATUS_IN_PROGRESS,
        )
        old_task = MoveTask.objects.create(
            request=old_request,
            pallet_code="PAL-OBR-STARTED",
            from_zone="OBR",
            to_zone="OS",
            move_mode=MoveTask.MODE_PALLET_FULL,
            qty_planned=10,
            status=MoveTask.STATUS_IN_PROGRESS,
            assigned_to=self.user,
            assigned_to_name="Driver",
            started_at=timezone.now(),
            legacy_order_id="MOVE-OBR-STARTED-1",
            payload={
                "status": "in_progress",
                "assigned_to_id": self.user.id,
                "from_location": {"zone": "OBR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "mobile_execution": {"source_confirmed": True},
            },
        )
        stock_before = (source.qty, source.available_qty)

        preview = preview_otg_shipping_pick_coverage(self.order)

        source.refresh_from_db()
        old_task.refresh_from_db()
        self.assertFalse(preview["can_cover"])
        self.assertEqual(old_task.status, MoveTask.STATUS_IN_PROGRESS)
        self.assertEqual((source.qty, source.available_qty), stock_before)
        self.assertFalse(OtgDeliveryRequest.objects.filter(shipping_order=self.order).exists())

    def test_partial_pick_from_pr_moves_only_scanned_qty_to_otg(self):
        otg_location = self._create_otg_destination()
        source = self._create_box(
            pallet_code="PAL-PR",
            box_code="PAL-PR-BX-01",
            qty=10,
            zone="PR",
            warehouse_state_code="placed_in_receiving",
        )

        created_ids = WarehouseWritePathService.complete_partial_shipping_pick_to_otg(
            agency=self.agency,
            order_id=str(self.order.pk),
            source_pallet_code="PAL-PR",
            picked_rows=[
                {
                    "box_code": "PAL-PR-BX-01",
                    "picked_qty": 3,
                    "barcode_qty": {"460000000001": 3},
                }
            ],
            destination_location_code=otg_location.location_code,
            performed_by=self.user,
            allow_receiving_source=True,
        )

        source.refresh_from_db()
        otg_snapshot = WarehouseStockSnapshot.objects.get(id=created_ids[0])
        self.assertEqual(source.qty, 7)
        self.assertEqual(source.available_qty, 7)
        self.assertEqual(source.warehouse_state_code, "placed_in_receiving")
        self.assertEqual(otg_snapshot.qty, 3)
        self.assertEqual(otg_snapshot.available_qty, 0)
        self.assertEqual(otg_snapshot.warehouse_state_code, "in_otg")
        source.container.refresh_from_db()
        source.parent_container.refresh_from_db()
        self.assertEqual(source.container.status, WarehouseContainer.STATUS_ACTIVE)
        self.assertEqual(source.parent_container.status, WarehouseContainer.STATUS_ACTIVE)

    def test_partial_pick_archives_empty_source_box_and_pallet(self):
        otg_location = self._create_otg_destination()
        source = self._create_box(
            pallet_code="PAL-PR-EMPTY",
            box_code="PAL-PR-EMPTY-BX-01",
            qty=3,
            zone="PR",
            warehouse_state_code="placed_in_receiving",
        )
        source_box = source.container
        source_pallet = source.parent_container

        created_ids = WarehouseWritePathService.complete_partial_shipping_pick_to_otg(
            agency=self.agency,
            order_id=str(self.order.pk),
            source_pallet_code=source_pallet.container_code,
            picked_rows=[
                {
                    "box_code": source_box.container_code,
                    "picked_qty": 3,
                    "barcode_qty": {"460000000001": 3},
                }
            ],
            destination_location_code=otg_location.location_code,
            performed_by=self.user,
            allow_receiving_source=True,
        )

        source.refresh_from_db()
        source_box.refresh_from_db()
        source_pallet.refresh_from_db()
        self.assertTrue(source.is_archived)
        self.assertEqual(source.qty, 0)
        self.assertEqual(len(created_ids), 1)
        self.assertEqual(source_box.status, WarehouseContainer.STATUS_ARCHIVED)
        self.assertIsNone(source_box.current_location_id)
        self.assertIsNone(source_box.parent_container_id)
        self.assertEqual(source_pallet.status, WarehouseContainer.STATUS_ARCHIVED)
        self.assertIsNone(source_pallet.current_location_id)

    def test_partial_pick_from_pr_rejects_existing_reserve(self):
        source = self._create_box(
            pallet_code="PAL-PR",
            box_code="PAL-PR-BX-01",
            qty=10,
            zone="PR",
            warehouse_state_code="placed_in_receiving",
        )
        source.processing_reserved_qty = 1
        source.available_qty = 9
        source.save(update_fields=["processing_reserved_qty", "available_qty", "updated_at"])

        with self.assertRaises(ValueError):
            WarehouseWritePathService.complete_partial_shipping_pick_to_otg(
                agency=self.agency,
                order_id=str(self.order.pk),
                source_pallet_code="PAL-PR",
                picked_rows=[
                    {
                        "box_code": "PAL-PR-BX-01",
                        "picked_qty": 3,
                        "barcode_qty": {"460000000001": 3},
                    }
                ],
                performed_by=self.user,
                allow_receiving_source=True,
            )

        source.refresh_from_db()
        self.assertEqual(source.qty, 10)
        self.assertEqual(source.warehouse_state_code, "placed_in_receiving")
