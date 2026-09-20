from django.contrib.auth import get_user_model
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase, override_settings

from fbs.exceptions import FbsInventoryError, FbsMovementError, FbsPickingError
from fbs.models import (
    FbsBox,
    FbsIntegrationProfile,
    FbsOrder,
    FbsOrderItem,
    FbsOrderStockAllocation,
    FbsPallet,
    FbsPickBatch,
    FbsPickingCart,
    FbsPickTask,
    FbsStockBalance,
    FbsStorageCell,
    FbsWorkstation,
)
from fbs.services.free_relocation import (
    cancel_fbs_free_relocation,
    complete_fbs_free_relocation,
    inspect_fbs_pallet,
    place_fbs_pallet_box_group,
    start_fbs_free_relocation,
)
from fbs.services.inventory import balance_is_locked
from fbs.services.physical_locations import fbs_box_physical_location_code
from fbs.services.storage import create_fbs_box
from fbs.services.picking import (
    claim_next_pick_batch,
    claim_pick_batch,
    reserve_order_stock,
)
from fbs.operator_views import _decorate_order_availability
from fbs.tsd_views import _picker_fbs_location_from_scan
from reachtruck_free.box_relocation import (
    cancel_fbs_box_relocation,
    complete_fbs_box_relocation,
    complete_general_box_relocation,
    inspect_fbs_box,
    start_fbs_box_relocation,
    start_general_box_relocation,
)
from reachtruck_free.services import (
    complete_free_move,
    inspect_scan,
    moving_context,
    parse_destination_scan,
    start_free_move,
)
from sklad.location_occupancy import FBS_STORAGE_CONTEXT_TYPE
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseOperationTask,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sklad.location_occupancy import os_location_occupancy_message
from sklad.services.warehouse_transitions import WarehouseStateCode
from sku.models import Agency, SKU, SKUBarcode


@override_settings(FBS_MODULE_ENABLED=True, FBS_WAREHOUSE_WRITES_ENABLED=True)
class FbsFreeRelocationTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Тестовый клиент FBS")
        self.user = get_user_model().objects.create_user(
            username="fbs-free-driver",
            password="test-password",
        )
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-FREE-1",
            name="Тестовый товар",
        )
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB relocation",
            external_account_id="wb-relocation",
            external_warehouse_id="wb-relocation-warehouse",
        )
        self.workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-RELOCATION",
            name="Стол перемещения",
            max_parallel_waves=2,
        )
        self.cart = FbsPickingCart.objects.create(
            barcode="FBS-CART-RELOCATION",
            name="Тележка перемещения",
        )
        self.source_location = self._location(row=1, tier=4, code="A-1/4-1")
        self.destination_location = self._location(row=2, tier=1, code="A-2/1-1")
        self.receiving_buffer = self._buffer_location(
            zone="PR",
            kind=WarehouseLocation.ZONE_KIND_RECEIVING,
        )
        self.shipping_buffer = self._buffer_location(
            zone="OTG",
            kind=WarehouseLocation.ZONE_KIND_SHIPPING,
        )
        self.receiving_place = self._exact_buffer_location(
            zone="PR",
            kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            code="PR-1-01",
        )
        self.shipping_place = self._exact_buffer_location(
            zone="OTG",
            kind=WarehouseLocation.ZONE_KIND_SHIPPING,
            code="OTG-1-1",
        )
        self.source_cell = FbsStorageCell.objects.create(
            cell_code="FBS@A-1/4-1",
            location=self.source_location,
            client_cluster=self.agency.id,
        )
        self.destination_cell = FbsStorageCell.objects.create(
            cell_code="FBS@A-2/1-1",
            location=self.destination_location,
        )
        self.pallet_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="FBS-PALLET-FREE-1",
            current_location=self.source_location,
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type=FBS_STORAGE_CONTEXT_TYPE,
            source_context_id="FBS-PALLET-FREE-1",
        )
        self.pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-PALLET-FREE-1",
            cell=self.source_cell,
            warehouse_container=self.pallet_container,
            status=FbsPallet.STATUS_ACTIVE,
        )
        self.box_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="FBS-BOX-FREE-1",
            parent_container=self.pallet_container,
            current_location=self.source_location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        self.box = FbsBox.objects.create(
            agency=self.agency,
            pallet=self.pallet,
            box_code="FBS-BOX-FREE-1",
            source_container=self.box_container,
            status=FbsBox.STATUS_ACTIVE,
        )
        self.balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.box,
            sku_ref=self.sku,
            identity_key="FBS-FREE-SKU-1",
            sku_code="SKU-FREE-1",
            name="Тестовый товар",
            barcode="4600000000001",
            qty=10,
            available_qty=10,
            reserved_qty=0,
        )

    def _reserved_allocation(
        self,
        *,
        suffix: str,
        task_status: str | None = None,
        batch_status: str = FbsPickBatch.STATUS_QUEUED,
    ) -> tuple[FbsOrderStockAllocation, FbsPickBatch | None]:
        order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id=f"MOVE-ORDER-{suffix}",
            internal_status=(
                FbsOrder.STATUS_QUEUED_FOR_PICK
                if task_status is not None
                else FbsOrder.STATUS_RESERVED
            ),
        )
        item = FbsOrderItem.objects.create(
            order=order,
            external_line_id=f"MOVE-LINE-{suffix}",
            external_sku=self.sku.sku_code,
            sku=self.sku,
            barcode=self.balance.barcode,
            product_name=self.sku.name,
            quantity=1,
        )
        batch = None
        task = None
        if task_status is not None:
            batch = FbsPickBatch.objects.create(
                agency=self.agency,
                status=batch_status,
                planned_qty=1,
                picked_qty=0,
            )
            task = FbsPickTask.objects.create(
                batch=batch,
                order=order,
                status=task_status,
                planned_qty=1,
                picked_qty=0,
            )
        allocation = FbsOrderStockAllocation.objects.create(
            order_item=item,
            balance=self.balance,
            pick_task=task,
            qty_reserved=1,
            status=(
                FbsOrderStockAllocation.STATUS_PICKING
                if task_status == FbsPickTask.STATUS_IN_PROGRESS
                else FbsOrderStockAllocation.STATUS_RESERVED
            ),
        )
        self.balance.available_qty = 9
        self.balance.reserved_qty = 1
        self.balance.save(
            update_fields=["available_qty", "reserved_qty", "updated_at"]
        )
        return allocation, batch

    def _location(self, *, row: int, tier: int, code: str) -> WarehouseLocation:
        return WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=row,
            section_no=2,
            tier_no=tier,
            cell_no=1,
            location_code=code,
            display_name=code,
            is_active=True,
            is_storage=True,
        )

    def _request(self):
        request = RequestFactory().post("/reachtruck-free/")
        SessionMiddleware(lambda _request: None).process_request(request)
        request.user = self.user
        return request

    def _buffer_location(self, *, zone: str, kind: str) -> WarehouseLocation:
        return WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code=zone,
            zone_kind=kind,
            row_no=0,
            section_no=0,
            tier_no=0,
            cell_no=0,
            location_code=zone,
            display_name=f"Зона {zone}",
            is_active=True,
            is_storage=False,
        )

    def _exact_buffer_location(
        self,
        *,
        zone: str,
        kind: str,
        code: str,
    ) -> WarehouseLocation:
        return WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code=zone,
            zone_kind=kind,
            location_code=code,
            display_name=code,
            capacity_containers=20,
            is_topology_visible=False,
            is_fbs_visible=True,
            is_shipping=zone == "OTG",
            is_active=True,
        )

    def _place_physical_pallet(self, location: WarehouseLocation) -> None:
        WarehouseContainer.objects.filter(
            id__in=[self.pallet_container.id, self.box_container.id]
        ).update(current_location=location)

    def test_low_level_move_locks_balance_and_moves_all_physical_containers(self):
        operation = start_fbs_free_relocation(
            pallet_id=self.pallet.id,
            performed_by=self.user,
            expected_location_id=self.source_location.id,
        )

        self.assertEqual(operation.context_type, "fbs_free_relocation")
        self.assertTrue(balance_is_locked(self.balance.id))
        self.pallet.refresh_from_db()
        self.pallet_container.refresh_from_db()
        self.box_container.refresh_from_db()
        self.assertEqual(self.pallet.cell.location.zone_kind, WarehouseLocation.ZONE_KIND_VIRTUAL)
        self.assertIsNone(self.pallet_container.current_location_id)
        self.assertIsNone(self.box_container.current_location_id)
        self.assertEqual(os_location_occupancy_message(self.source_location), "")

        operation = complete_fbs_free_relocation(
            operation_id=operation.id,
            destination_row_no=2,
            destination_section_no=2,
            destination_tier_no=1,
            destination_cell_no=1,
            performed_by=self.user,
        )

        self.pallet.refresh_from_db()
        self.pallet_container.refresh_from_db()
        self.box_container.refresh_from_db()
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(self.pallet.cell_id, self.destination_cell.id)
        self.assertEqual(self.pallet_container.current_location_id, self.destination_location.id)
        self.assertEqual(self.box_container.current_location_id, self.destination_location.id)
        self.assertEqual(self.box_container.parent_container_id, self.pallet_container.id)
        self.assertFalse(balance_is_locked(self.balance.id))

    def test_reserved_fbs_pallet_is_not_available_for_free_move(self):
        self.balance.available_qty = 9
        self.balance.reserved_qty = 1
        self.balance.save(update_fields=["available_qty", "reserved_qty", "updated_at"])

        info = inspect_fbs_pallet(self.pallet.pallet_code)

        self.assertTrue(info["found"])
        self.assertFalse(info["can_move"])
        self.assertTrue(any("резерв" in value for value in info["blockers"]))
        with self.assertRaises(FbsMovementError):
            start_fbs_free_relocation(
                pallet_id=self.pallet.id,
                performed_by=self.user,
            )

    def test_order_reserve_without_started_wave_moves_with_fbs_pallet(self):
        allocation, batch = self._reserved_allocation(suffix="NO-WAVE")

        info = inspect_fbs_pallet(self.pallet.pallet_code)

        self.assertTrue(info["can_move"], info["blockers"])
        operation = start_fbs_free_relocation(
            pallet_id=self.pallet.id,
            performed_by=self.user,
        )
        operation = complete_fbs_free_relocation(
            operation_id=operation.id,
            destination_row_no=2,
            destination_section_no=2,
            destination_tier_no=1,
            destination_cell_no=1,
            performed_by=self.user,
        )

        allocation.refresh_from_db()
        self.balance.refresh_from_db()
        self.assertIsNone(batch)
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(allocation.status, FbsOrderStockAllocation.STATUS_RESERVED)
        self.assertEqual(allocation.balance_id, self.balance.id)
        self.assertEqual(self.balance.reserved_qty, 1)
        self.assertEqual(
            fbs_box_physical_location_code(allocation.balance.box),
            "A-2/1-1",
        )

    def test_untouched_wave_waits_for_move_then_claims_from_new_location(self):
        allocation, batch = self._reserved_allocation(
            suffix="QUEUED-WAVE",
            task_status=FbsPickTask.STATUS_QUEUED,
        )
        operation = start_fbs_free_relocation(
            pallet_id=self.pallet.id,
            performed_by=self.user,
        )

        with self.assertRaisesMessage(FbsPickingError, "сейчас перемещается"):
            claim_pick_batch(
                batch_id=batch.id,
                assigned_to=self.user,
                workstation_scan=self.workstation.barcode,
                cart_scan=self.cart.barcode,
            )

        complete_fbs_free_relocation(
            operation_id=operation.id,
            destination_row_no=2,
            destination_section_no=2,
            destination_tier_no=1,
            destination_cell_no=1,
            performed_by=self.user,
        )
        batch = claim_pick_batch(
            batch_id=batch.id,
            assigned_to=self.user,
            workstation_scan=self.workstation.barcode,
            cart_scan=self.cart.barcode,
        )

        allocation.refresh_from_db()
        self.assertEqual(batch.status, FbsPickBatch.STATUS_IN_PROGRESS)
        self.assertEqual(allocation.status, FbsOrderStockAllocation.STATUS_PICKING)
        self.assertEqual(
            fbs_box_physical_location_code(allocation.balance.box),
            "A-2/1-1",
        )

    def test_next_wave_skips_untouched_wave_while_its_stock_is_moving(self):
        allocation, moving_batch = self._reserved_allocation(
            suffix="MOVING-FIRST-WAVE",
            task_status=FbsPickTask.STATUS_QUEUED,
        )
        operation = start_fbs_free_relocation(
            pallet_id=self.pallet.id,
            performed_by=self.user,
        )
        free_order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="MOVE-ORDER-FREE-NEXT-WAVE",
            internal_status=FbsOrder.STATUS_QUEUED_FOR_PICK,
        )
        free_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_QUEUED,
            planned_qty=1,
        )
        free_task = FbsPickTask.objects.create(
            batch=free_batch,
            order=free_order,
            status=FbsPickTask.STATUS_QUEUED,
            planned_qty=1,
        )

        claimed = claim_next_pick_batch(
            assigned_to=self.user,
            workstation_scan=self.workstation.barcode,
            cart_scan=self.cart.barcode,
        )

        moving_batch.refresh_from_db()
        free_batch.refresh_from_db()
        free_task.refresh_from_db()
        allocation.refresh_from_db()
        operation.refresh_from_db()
        self.assertEqual(claimed.id, free_batch.id)
        self.assertEqual(moving_batch.status, FbsPickBatch.STATUS_QUEUED)
        self.assertEqual(allocation.status, FbsOrderStockAllocation.STATUS_RESERVED)
        self.assertEqual(free_batch.status, FbsPickBatch.STATUS_IN_PROGRESS)
        self.assertEqual(free_task.status, FbsPickTask.STATUS_IN_PROGRESS)
        self.assertEqual(operation.status, WarehouseOperation.STATUS_IN_PROGRESS)

    def test_started_wave_remains_a_hard_relocation_blocker(self):
        self._reserved_allocation(
            suffix="STARTED-WAVE",
            task_status=FbsPickTask.STATUS_IN_PROGRESS,
            batch_status=FbsPickBatch.STATUS_IN_PROGRESS,
        )

        info = inspect_fbs_pallet(self.pallet.pallet_code)

        self.assertFalse(info["can_move"])
        self.assertTrue(any("в работу" in value for value in info["blockers"]))
        with self.assertRaisesMessage(FbsMovementError, "в работу"):
            start_fbs_free_relocation(
                pallet_id=self.pallet.id,
                performed_by=self.user,
            )

    def test_reserved_box_moves_to_exact_place_and_keeps_allocation(self):
        allocation, _batch = self._reserved_allocation(suffix="BOX-NO-WAVE")
        target_pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="FBS-TARGET-PALLET-EXACT",
            current_location=self.shipping_place,
            status=WarehouseContainer.STATUS_ACTIVE,
        )

        info = inspect_fbs_box(self.box.box_code)
        self.assertTrue(info["can_move"], info["blockers"])
        operation = start_fbs_box_relocation(
            box_id=self.box.id,
            performed_by=self.user,
        )
        operation = complete_fbs_box_relocation(
            operation_id=operation.id,
            destination_scan=target_pallet.container_code,
            performed_by=self.user,
        )

        allocation.refresh_from_db()
        self.balance.refresh_from_db()
        self.box_container.refresh_from_db()
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(allocation.status, FbsOrderStockAllocation.STATUS_RESERVED)
        self.assertEqual(self.balance.reserved_qty, 1)
        self.assertEqual(self.box_container.current_location_id, self.shipping_place.id)

    def test_picker_moves_whole_box_directly_to_exact_otg_place(self):
        allocation, _batch = self._reserved_allocation(suffix="BOX-DIRECT-OTG")
        operation = start_fbs_box_relocation(
            box_id=self.box.id,
            performed_by=self.user,
            performed_by_role="picker",
        )

        operation = complete_fbs_box_relocation(
            operation_id=operation.id,
            destination_scan=self.shipping_place.location_code,
            performed_by=self.user,
            performed_by_role="picker",
        )

        allocation.refresh_from_db()
        self.balance.refresh_from_db()
        self.box.refresh_from_db()
        self.box_container.refresh_from_db()
        operation.refresh_from_db()
        event = WarehouseEvent.objects.get(
            operation=operation,
            event_type="fbs_free_box_relocation_completed",
        )
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(operation.requested_by_role, "picker")
        self.assertEqual(operation.assigned_executor_role, "picker")
        self.assertEqual(event.performed_by_role, "picker")
        self.assertEqual(allocation.status, FbsOrderStockAllocation.STATUS_RESERVED)
        self.assertEqual(allocation.balance_id, self.balance.id)
        self.assertEqual(self.balance.reserved_qty, 1)
        self.assertEqual(self.balance.qty, 10)
        self.assertEqual(self.box.pallet_id, self.pallet.id)
        self.assertEqual(self.box_container.current_location_id, self.shipping_place.id)
        self.assertIsNone(self.box_container.parent_container_id)

    def test_picker_moves_whole_box_directly_to_exact_pr_place(self):
        operation = start_fbs_box_relocation(
            box_id=self.box.id,
            performed_by=self.user,
            performed_by_role="picker",
        )

        operation = complete_fbs_box_relocation(
            operation_id=operation.id,
            destination_scan=self.receiving_place.location_code,
            performed_by=self.user,
            performed_by_role="picker",
        )

        self.balance.refresh_from_db()
        self.box_container.refresh_from_db()
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(operation.destination_location_id, self.receiving_place.id)
        self.assertEqual(self.balance.qty, 10)
        self.assertEqual(self.balance.reserved_qty, 0)
        self.assertEqual(self.box_container.current_location_id, self.receiving_place.id)
        self.assertIsNone(self.box_container.parent_container_id)

    def test_archived_source_pallet_does_not_block_reservation_from_pr_box(self):
        SKUBarcode.objects.create(
            sku=self.sku,
            value=self.balance.barcode,
            is_primary=True,
        )
        operation = start_fbs_box_relocation(
            box_id=self.box.id,
            performed_by=self.user,
            performed_by_role="picker",
        )
        complete_fbs_box_relocation(
            operation_id=operation.id,
            destination_scan=self.receiving_place.location_code,
            performed_by=self.user,
            performed_by_role="picker",
        )

        self.pallet.refresh_from_db()
        self.pallet_container.refresh_from_db()
        self.box_container.refresh_from_db()
        self.assertEqual(self.pallet.status, FbsPallet.STATUS_ARCHIVED)
        self.assertEqual(self.pallet_container.status, WarehouseContainer.STATUS_ARCHIVED)
        self.assertEqual(self.box_container.status, WarehouseContainer.STATUS_ACTIVE)
        self.assertEqual(self.box_container.current_location_id, self.receiving_place.id)

        order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="MOVE-ORDER-PR-ARCHIVED-PALLET",
            internal_status=FbsOrder.STATUS_RECEIVED,
        )
        FbsOrderItem.objects.create(
            order=order,
            external_line_id="MOVE-LINE-PR-ARCHIVED-PALLET",
            external_sku=self.sku.sku_code,
            sku=self.sku,
            barcode=self.balance.barcode,
            product_name=self.sku.name,
            quantity=1,
        )

        result = reserve_order_stock(order_id=order.id, reserved_by=self.user)

        self.assertTrue(result.reserved)
        self.assertEqual(result.reserved_qty, 1)
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.available_qty, 9)
        self.assertEqual(self.balance.reserved_qty, 1)

    def test_case_insensitive_barcode_is_available_and_reservable(self):
        catalog_barcode = "OZN2446436869"
        SKUBarcode.objects.create(
            sku=self.sku,
            value=catalog_barcode,
            is_primary=True,
        )
        self.balance.barcode = catalog_barcode.lower()
        self.balance.save(update_fields=["barcode", "updated_at"])
        order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="CASE-INSENSITIVE-BARCODE",
            internal_status=FbsOrder.STATUS_RECEIVED,
        )
        FbsOrderItem.objects.create(
            order=order,
            external_line_id="CASE-INSENSITIVE-LINE",
            external_sku=self.sku.sku_code,
            sku=self.sku,
            barcode=catalog_barcode,
            product_name=self.sku.name,
            quantity=1,
        )

        _decorate_order_availability([order])

        self.assertEqual(order.availability_state, "fbs_available")
        self.assertEqual(order.availability_rows[0]["fbs_available_qty"], 10)
        result = reserve_order_stock(order_id=order.id, reserved_by=self.user)
        self.assertTrue(result.reserved)
        self.assertEqual(result.reserved_qty, 1)
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.available_qty, 9)
        self.assertEqual(self.balance.reserved_qty, 1)

    def test_case_insensitive_ambiguous_catalog_barcode_is_rejected(self):
        catalog_barcode = "OZN2446436869"
        SKUBarcode.objects.create(
            sku=self.sku,
            value=catalog_barcode,
            is_primary=True,
        )
        other_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-FREE-AMBIGUOUS",
            name="Другой товар с неоднозначным ШК",
        )
        SKUBarcode.objects.create(
            sku=other_sku,
            value=catalog_barcode.lower(),
            is_primary=True,
        )
        order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="CASE-INSENSITIVE-AMBIGUOUS",
            internal_status=FbsOrder.STATUS_RECEIVED,
        )
        FbsOrderItem.objects.create(
            order=order,
            external_line_id="CASE-INSENSITIVE-AMBIGUOUS-LINE",
            external_sku=self.sku.sku_code,
            sku=self.sku,
            barcode=catalog_barcode,
            product_name=self.sku.name,
            quantity=1,
        )

        result = reserve_order_stock(order_id=order.id, reserved_by=self.user)

        self.assertFalse(result.reserved)
        self.assertEqual(result.status, FbsOrder.STATUS_VALIDATION_FAILED)
        self.assertTrue(
            any(
                "без учета регистра привязан к нескольким SKU клиента" in error
                for error in result.errors
            ),
            result.errors,
        )

    def test_picker_moves_whole_box_to_empty_os_place_without_pallet(self):
        operation = start_fbs_box_relocation(
            box_id=self.box.id,
            performed_by=self.user,
            performed_by_role="picker",
        )

        operation = complete_fbs_box_relocation(
            operation_id=operation.id,
            destination_scan="A-2/1-1",
            performed_by=self.user,
            performed_by_role="picker",
        )

        self.balance.refresh_from_db()
        self.box.refresh_from_db()
        self.box_container.refresh_from_db()
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(operation.destination_location_id, self.destination_location.id)
        self.assertEqual(self.balance.qty, 10)
        self.assertEqual(self.balance.available_qty, 10)
        self.assertEqual(self.balance.reserved_qty, 0)
        self.assertEqual(self.box.pallet_id, self.pallet.id)
        self.assertEqual(self.box_container.current_location_id, self.destination_location.id)
        self.assertIsNone(self.box_container.parent_container_id)
        info = inspect_fbs_box(self.box.box_code)
        self.assertTrue(info["can_move"], info["blockers"])

    def test_picker_places_box_with_same_client_loose_boxes_in_os_place(self):
        target_pallet_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="FBS-LOOSE-TARGET-PALLET",
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type=FBS_STORAGE_CONTEXT_TYPE,
            source_context_id="FBS-LOOSE-TARGET-PALLET",
        )
        target_pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-LOOSE-TARGET-PALLET",
            cell=self.destination_cell,
            warehouse_container=target_pallet_container,
            max_boxes=3,
            status=FbsPallet.STATUS_ACTIVE,
        )
        for index in (1, 2):
            existing_container = WarehouseContainer.objects.create(
                agency=self.agency,
                container_type=WarehouseContainer.TYPE_BOX,
                container_code=f"FBS-LOOSE-TARGET-BOX-{index}",
                current_location=self.destination_location,
                status=WarehouseContainer.STATUS_ACTIVE,
            )
            FbsBox.objects.create(
                agency=self.agency,
                pallet=target_pallet,
                box_code=existing_container.container_code,
                source_container=existing_container,
                status=FbsBox.STATUS_ACTIVE,
            )
        operation = start_fbs_box_relocation(
            box_id=self.box.id,
            performed_by=self.user,
            performed_by_role="picker",
        )

        operation = complete_fbs_box_relocation(
            operation_id=operation.id,
            destination_scan="A-2/1-1",
            performed_by=self.user,
            performed_by_role="picker",
        )

        self.box.refresh_from_db()
        self.box_container.refresh_from_db()
        task = operation.tasks.get()
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(self.box.pallet_id, self.pallet.id)
        self.assertEqual(
            self.box_container.current_location_id,
            self.destination_location.id,
        )
        self.assertIsNone(self.box_container.parent_container_id)
        self.assertTrue(task.payload["destination_shared_loose_boxes"])
        self.assertEqual(task.payload["destination_existing_loose_box_count"], 2)

    def test_picker_os_place_rejects_loose_box_of_another_client(self):
        other_agency = Agency.objects.create(agn_name="Другой клиент отдельного короба")
        other_pallet_container = WarehouseContainer.objects.create(
            agency=other_agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="OTHER-LOOSE-PALLET",
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        other_pallet = FbsPallet.objects.create(
            agency=other_agency,
            pallet_code="OTHER-LOOSE-PALLET",
            cell=self.destination_cell,
            warehouse_container=other_pallet_container,
            status=FbsPallet.STATUS_ACTIVE,
        )
        other_box_container = WarehouseContainer.objects.create(
            agency=other_agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="OTHER-LOOSE-BOX",
            current_location=self.destination_location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        FbsBox.objects.create(
            agency=other_agency,
            pallet=other_pallet,
            box_code=other_box_container.container_code,
            source_container=other_box_container,
            status=FbsBox.STATUS_ACTIVE,
        )
        operation = start_fbs_box_relocation(
            box_id=self.box.id,
            performed_by=self.user,
            performed_by_role="picker",
        )

        with self.assertRaisesMessage(FbsMovementError, "другого клиента"):
            complete_fbs_box_relocation(
                operation_id=operation.id,
                destination_scan="A-2/1-1",
                performed_by=self.user,
                performed_by_role="picker",
            )

    def test_picker_os_place_rejects_non_fbs_container_beside_loose_box(self):
        existing_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="FBS-LOOSE-TARGET-BOX",
            current_location=self.destination_location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        existing_box = FbsBox.objects.create(
            agency=self.agency,
            pallet=self.pallet,
            box_code=existing_container.container_code,
            source_container=existing_container,
            status=FbsBox.STATUS_ACTIVE,
        )
        WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="GENERAL-WAREHOUSE-BOX",
            current_location=self.destination_location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        operation = start_fbs_box_relocation(
            box_id=self.box.id,
            performed_by=self.user,
            performed_by_role="picker",
        )

        with self.assertRaisesMessage(FbsMovementError, "GENERAL-WAREHOUSE-BOX"):
            complete_fbs_box_relocation(
                operation_id=operation.id,
                destination_scan="A-2/1-1",
                performed_by=self.user,
                performed_by_role="picker",
            )

        existing_box.delete()

    def test_picker_places_all_boxes_from_physical_pallet_in_one_transaction(self):
        source_physical_pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="SOURCE-PHYSICAL-PALLET-GROUP",
            current_location=self.receiving_buffer,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        self.pallet.max_boxes = 3
        self.pallet.save(update_fields=["max_boxes", "updated_at"])
        self.pallet_container.current_location = None
        self.pallet_container.save(update_fields=["current_location", "updated_at"])
        self.box_container.parent_container = source_physical_pallet
        self.box_container.current_location = self.receiving_buffer
        self.box_container.save(
            update_fields=["parent_container", "current_location", "updated_at"]
        )
        second_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="FBS-BOX-FREE-GROUP-2",
            parent_container=source_physical_pallet,
            current_location=self.receiving_buffer,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        second_box = FbsBox.objects.create(
            agency=self.agency,
            pallet=self.pallet,
            box_code=second_container.container_code,
            source_container=second_container,
            status=FbsBox.STATUS_ACTIVE,
        )
        second_balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=second_box,
            sku_ref=self.sku,
            identity_key="FBS-FREE-SKU-GROUP-2",
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode="4600000000002",
            qty=7,
            available_qty=7,
        )
        placed_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="FBS-BOX-FREE-GROUP-ALREADY",
            current_location=self.destination_location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        placed_box = FbsBox.objects.create(
            agency=self.agency,
            pallet=self.pallet,
            box_code=placed_container.container_code,
            source_container=placed_container,
            status=FbsBox.STATUS_ACTIVE,
        )
        FbsStockBalance.objects.create(
            agency=self.agency,
            box=placed_box,
            sku_ref=self.sku,
            identity_key="FBS-FREE-SKU-GROUP-ALREADY",
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode="4600000000003",
            qty=5,
            available_qty=5,
        )
        allocation, batch = self._reserved_allocation(
            suffix="GROUP-QUEUED-WAVE",
            task_status=FbsPickTask.STATUS_QUEUED,
        )

        operation = place_fbs_pallet_box_group(
            source_scan=source_physical_pallet.container_code,
            destination_location_id=self.destination_location.id,
            performed_by=self.user,
            performed_by_role="picker",
        )

        self.pallet.refresh_from_db()
        self.pallet_container.refresh_from_db()
        self.box_container.refresh_from_db()
        second_container.refresh_from_db()
        placed_container.refresh_from_db()
        allocation.refresh_from_db()
        self.balance.refresh_from_db()
        task = operation.tasks.get()
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(operation.planned_qty, 17)
        self.assertEqual(operation.done_qty, 17)
        self.assertEqual(self.pallet.cell_id, self.destination_cell.id)
        self.assertEqual(
            self.pallet_container.current_location_id,
            self.destination_location.id,
        )
        for container in (
            self.box_container,
            second_container,
            placed_container,
        ):
            self.assertEqual(container.current_location_id, self.destination_location.id)
            self.assertEqual(container.parent_container_id, self.pallet_container.id)
        self.assertFalse(
            source_physical_pallet.child_containers.filter(
                status=WarehouseContainer.STATUS_ACTIVE
            ).exists()
        )
        self.assertEqual(task.payload["scan_kind"], "physical_pallet")
        self.assertEqual(task.payload["box_count"], 3)
        self.assertEqual(task.payload["moved_box_count"], 2)
        self.assertEqual(task.payload["already_at_destination_count"], 1)
        self.assertEqual(task.payload["total_pallet_qty"], 22)
        self.assertEqual(allocation.status, FbsOrderStockAllocation.STATUS_RESERVED)
        self.assertEqual(allocation.balance_id, self.balance.id)
        self.assertEqual(self.balance.reserved_qty, 1)
        self.assertEqual(second_balance.qty, 7)
        self.assertEqual(batch.status, FbsPickBatch.STATUS_QUEUED)
        self.assertEqual(
            fbs_box_physical_location_code(allocation.balance.box),
            "A-2/1-1",
        )
        event = WarehouseEvent.objects.get(
            operation=operation,
            event_type="fbs_pallet_boxes_group_relocation_completed",
        )
        self.assertEqual(event.qty, 17)
        self.assertEqual(event.performed_by_role, "picker")

    def test_group_putaway_rejects_other_container_on_source_pallet_atomically(self):
        WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="FOREIGN-SOURCE-CONTAINER",
            parent_container=self.pallet_container,
            current_location=self.source_location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )

        with self.assertRaisesMessage(FbsMovementError, "другие контейнеры"):
            place_fbs_pallet_box_group(
                source_scan=self.box.box_code,
                destination_location_id=self.destination_location.id,
                performed_by=self.user,
            )

        self.pallet.refresh_from_db()
        self.pallet_container.refresh_from_db()
        self.box_container.refresh_from_db()
        self.assertEqual(self.pallet.cell_id, self.source_cell.id)
        self.assertEqual(self.pallet_container.current_location_id, self.source_location.id)
        self.assertEqual(self.box_container.current_location_id, self.source_location.id)
        self.assertFalse(
            WarehouseOperation.objects.filter(
                context_type="fbs_free_relocation",
                context_id=str(self.pallet.id),
                comment__startswith="Групповое размещение",
            ).exists()
        )

    def test_group_putaway_rejects_started_wave(self):
        self._reserved_allocation(
            suffix="GROUP-STARTED-WAVE",
            task_status=FbsPickTask.STATUS_IN_PROGRESS,
            batch_status=FbsPickBatch.STATUS_IN_PROGRESS,
        )

        with self.assertRaisesMessage(FbsMovementError, "в работу"):
            place_fbs_pallet_box_group(
                source_scan=self.box.box_code,
                destination_location_id=self.destination_location.id,
                performed_by=self.user,
            )

        self.pallet.refresh_from_db()
        self.box_container.refresh_from_db()
        self.assertEqual(self.pallet.cell_id, self.source_cell.id)
        self.assertEqual(self.box_container.current_location_id, self.source_location.id)

    def test_picker_os_place_scan_auto_binds_same_client_pallet(self):
        self.destination_location.location_code = "OS-2-2-1-1"
        self.destination_location.save(update_fields=["location_code", "updated_at"])
        target_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="FBS-TARGET-PALLET-OS",
            current_location=self.destination_location,
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type=FBS_STORAGE_CONTEXT_TYPE,
            source_context_id="FBS-TARGET-PALLET-OS",
        )
        target_pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-TARGET-PALLET-OS",
            cell=self.destination_cell,
            warehouse_container=target_container,
            status=FbsPallet.STATUS_ACTIVE,
        )
        operation = start_fbs_box_relocation(
            box_id=self.box.id,
            performed_by=self.user,
            performed_by_role="picker",
        )

        operation = complete_fbs_box_relocation(
            operation_id=operation.id,
            destination_scan="A-2/1-1",
            performed_by=self.user,
            performed_by_role="picker",
        )

        self.balance.refresh_from_db()
        self.box.refresh_from_db()
        self.box_container.refresh_from_db()
        task = operation.tasks.get()
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(self.balance.qty, 10)
        self.assertEqual(self.balance.available_qty, 10)
        self.assertEqual(self.balance.reserved_qty, 0)
        self.assertEqual(self.box.pallet_id, target_pallet.id)
        self.assertEqual(self.box_container.parent_container_id, target_container.id)
        self.assertEqual(self.box_container.current_location_id, self.destination_location.id)
        self.assertTrue(task.payload["destination_pallet_auto_resolved"])
        self.assertEqual(
            task.payload["destination_pallet_code"],
            target_container.container_code,
        )
        info = inspect_fbs_box(self.box.box_code)
        self.assertTrue(info["can_move"], info["blockers"])

    def test_picker_os_place_rejects_pallet_of_another_client(self):
        other_agency = Agency.objects.create(agn_name="Другой клиент короба")
        target_container = WarehouseContainer.objects.create(
            agency=other_agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="OTHER-TARGET-PALLET-OS",
            current_location=self.destination_location,
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type=FBS_STORAGE_CONTEXT_TYPE,
            source_context_id="OTHER-TARGET-PALLET-OS",
        )
        FbsPallet.objects.create(
            agency=other_agency,
            pallet_code="OTHER-TARGET-PALLET-OS",
            cell=self.destination_cell,
            warehouse_container=target_container,
            status=FbsPallet.STATUS_ACTIVE,
        )
        operation = start_fbs_box_relocation(
            box_id=self.box.id,
            performed_by=self.user,
            performed_by_role="picker",
        )

        with self.assertRaisesMessage(FbsMovementError, "другого клиента"):
            complete_fbs_box_relocation(
                operation_id=operation.id,
                destination_scan="A-2/1-1",
                performed_by=self.user,
                performed_by_role="picker",
            )

        cancel_fbs_box_relocation(
            operation_id=operation.id,
            performed_by=self.user,
        )

    def test_picker_os_place_rejects_full_same_client_pallet(self):
        target_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="FBS-FULL-TARGET-PALLET-OS",
            current_location=self.destination_location,
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type=FBS_STORAGE_CONTEXT_TYPE,
            source_context_id="FBS-FULL-TARGET-PALLET-OS",
        )
        target_pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-FULL-TARGET-PALLET-OS",
            cell=self.destination_cell,
            warehouse_container=target_container,
            max_boxes=1,
            status=FbsPallet.STATUS_ACTIVE,
        )
        existing_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="FBS-EXISTING-TARGET-BOX-OS",
            parent_container=target_container,
            current_location=self.destination_location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        FbsBox.objects.create(
            agency=self.agency,
            pallet=target_pallet,
            box_code="FBS-EXISTING-TARGET-BOX-OS",
            source_container=existing_container,
            status=FbsBox.STATUS_ACTIVE,
        )
        operation = start_fbs_box_relocation(
            box_id=self.box.id,
            performed_by=self.user,
            performed_by_role="picker",
        )

        with self.assertRaisesMessage(FbsMovementError, "нет свободного места"):
            complete_fbs_box_relocation(
                operation_id=operation.id,
                destination_scan="A-2/1-1",
                performed_by=self.user,
                performed_by_role="picker",
            )

        cancel_fbs_box_relocation(
            operation_id=operation.id,
            performed_by=self.user,
        )

    def test_picker_whole_box_rejects_generic_otg_zone(self):
        operation = start_fbs_box_relocation(
            box_id=self.box.id,
            performed_by=self.user,
            performed_by_role="picker",
        )

        with self.assertRaisesMessage(
            FbsMovementError,
            "Общая зона OTG запрещена",
        ):
            complete_fbs_box_relocation(
                operation_id=operation.id,
                destination_scan=self.shipping_buffer.location_code,
                performed_by=self.user,
                performed_by_role="picker",
            )

        cancel_fbs_box_relocation(
            operation_id=operation.id,
            performed_by=self.user,
        )

    def test_reserved_box_rejects_target_pallet_in_generic_zone(self):
        self._reserved_allocation(suffix="BOX-GENERIC")
        target_pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="FBS-TARGET-PALLET-GENERIC",
            current_location=self.shipping_buffer,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        operation = start_fbs_box_relocation(
            box_id=self.box.id,
            performed_by=self.user,
        )

        with self.assertRaisesMessage(FbsMovementError, "Общая зона OTG запрещена"):
            complete_fbs_box_relocation(
                operation_id=operation.id,
                destination_scan=target_pallet.container_code,
                performed_by=self.user,
            )

        cancel_fbs_box_relocation(
            operation_id=operation.id,
            performed_by=self.user,
        )

    def test_occupied_destination_is_rejected_and_cancel_releases_lock(self):
        other_agency = Agency.objects.create(agn_name="Другой клиент")
        other_container = WarehouseContainer.objects.create(
            agency=other_agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="OTHER-FBS-PALLET",
            current_location=self.destination_location,
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type=FBS_STORAGE_CONTEXT_TYPE,
            source_context_id="OTHER-FBS-PALLET",
        )
        FbsPallet.objects.create(
            agency=other_agency,
            pallet_code="OTHER-FBS-PALLET",
            cell=self.destination_cell,
            warehouse_container=other_container,
            status=FbsPallet.STATUS_ACTIVE,
        )
        operation = start_fbs_free_relocation(
            pallet_id=self.pallet.id,
            performed_by=self.user,
        )

        with self.assertRaises(FbsMovementError):
            complete_fbs_free_relocation(
                operation_id=operation.id,
                destination_row_no=2,
                destination_section_no=2,
                destination_tier_no=1,
                destination_cell_no=1,
                performed_by=self.user,
            )
        self.pallet.refresh_from_db()
        self.assertEqual(
            self.pallet.cell.location.zone_kind,
            WarehouseLocation.ZONE_KIND_VIRTUAL,
        )
        self.assertTrue(balance_is_locked(self.balance.id))

        operation = cancel_fbs_free_relocation(
            operation_id=operation.id,
            performed_by=self.user,
        )
        self.assertEqual(operation.status, WarehouseOperation.STATUS_CANCELED)
        self.assertFalse(balance_is_locked(self.balance.id))
        self.pallet.refresh_from_db()
        self.pallet_container.refresh_from_db()
        self.box_container.refresh_from_db()
        self.assertEqual(self.pallet.cell_id, self.source_cell.id)
        self.assertEqual(self.pallet_container.current_location_id, self.source_location.id)
        self.assertEqual(self.box_container.current_location_id, self.source_location.id)

    def test_empty_fbs_pallet_can_be_picked_up_and_releases_source_place(self):
        self.balance.qty = 0
        self.balance.available_qty = 0
        self.balance.save(update_fields=["qty", "available_qty", "updated_at"])

        operation = start_fbs_free_relocation(
            pallet_id=self.pallet.id,
            performed_by=self.user,
            expected_location_id=self.source_location.id,
        )

        self.assertEqual(operation.planned_qty, 0)
        self.pallet_container.refresh_from_db()
        self.box_container.refresh_from_db()
        self.assertIsNone(self.pallet_container.current_location_id)
        self.assertIsNone(self.box_container.current_location_id)
        self.assertEqual(os_location_occupancy_message(self.source_location), "")

    def test_cancel_does_not_return_pallet_when_released_source_was_reoccupied(self):
        operation = start_fbs_free_relocation(
            pallet_id=self.pallet.id,
            performed_by=self.user,
            expected_location_id=self.source_location.id,
        )
        replacement_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="FBS-PALLET-REPLACEMENT",
            current_location=self.source_location,
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type=FBS_STORAGE_CONTEXT_TYPE,
            source_context_id="FBS-PALLET-REPLACEMENT",
        )
        FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-PALLET-REPLACEMENT",
            cell=self.source_cell,
            warehouse_container=replacement_container,
            status=FbsPallet.STATUS_ACTIVE,
        )

        with self.assertRaisesMessage(
            FbsMovementError,
            "Исходное место уже занято после снятия паллеты",
        ):
            cancel_fbs_free_relocation(
                operation_id=operation.id,
                performed_by=self.user,
            )

        operation.refresh_from_db()
        self.pallet.refresh_from_db()
        self.pallet_container.refresh_from_db()
        self.assertEqual(operation.status, WarehouseOperation.STATUS_IN_PROGRESS)
        self.assertEqual(
            self.pallet.cell.location.zone_kind,
            WarehouseLocation.ZONE_KIND_VIRTUAL,
        )
        self.assertIsNone(self.pallet_container.current_location_id)
        self.assertTrue(balance_is_locked(self.balance.id))

    def test_unfinished_putaway_destination_is_not_physical_occupancy(self):
        operation = WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PUTAWAY,
            destination_location=self.destination_location,
            destination_zone_code="OS",
            status=WarehouseOperation.STATUS_IN_PROGRESS,
        )
        WarehouseOperationTask.objects.create(
            operation=operation,
            task_type=WarehouseOperationTask.TYPE_PALLET_MOVE,
            to_location=self.destination_location,
            to_zone_code="OS",
            status=WarehouseOperationTask.STATUS_IN_PROGRESS,
        )

        self.assertEqual(os_location_occupancy_message(self.destination_location), "")

    def test_new_fbs_box_cannot_be_attached_while_pallet_is_moving(self):
        operation = start_fbs_free_relocation(
            pallet_id=self.pallet.id,
            performed_by=self.user,
        )
        extra_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="FBS-BOX-FREE-2",
            current_location=self.source_location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )

        with self.assertRaises(FbsInventoryError):
            create_fbs_box(
                agency=self.agency,
                pallet=self.pallet,
                box_code="FBS-BOX-FREE-2",
                source_container=extra_container,
            )

        extra_container.delete()
        cancel_fbs_free_relocation(
            operation_id=operation.id,
            performed_by=self.user,
        )

    def test_existing_reachtruck_free_screen_detects_and_completes_fbs_move(self):
        info = inspect_scan(self.pallet.pallet_code)
        self.assertEqual(info["stock_contour"], "fbs")
        self.assertTrue(info["can_move"])

        request = self._request()
        result = start_free_move(
            request,
            pallet_code=self.pallet.pallet_code,
            role="reachtruck_driver",
        )
        self.assertEqual(result.operation.context_type, "fbs_free_relocation")
        self.assertEqual(moving_context(request)["move_contour_label"], "FBS")

        result = complete_free_move(request, scan_value="A-2/1-1")
        self.assertEqual(result.operation.status, WarehouseOperation.STATUS_DONE)
        self.pallet.refresh_from_db()
        self.assertEqual(self.pallet.cell_id, self.destination_cell.id)

    def test_fbs_buffer_move_from_pr_to_exact_otg_does_not_create_reserve(self):
        self._place_physical_pallet(self.receiving_buffer)
        reserve_count = WarehouseReserve.objects.count()
        info = inspect_fbs_pallet(self.pallet.pallet_code)

        self.assertTrue(info["can_move"])
        self.assertEqual(info["location_zone_code"], "PR")
        operation = start_fbs_free_relocation(
            pallet_id=self.pallet.id,
            performed_by=self.user,
            expected_location_id=self.receiving_buffer.id,
        )
        self.assertEqual(operation.source_zone_code, "PR")

        operation = complete_fbs_free_relocation(
            operation_id=operation.id,
            destination_zone_code="OTG",
            destination_row_no=0,
            destination_section_no=0,
            destination_tier_no=0,
            destination_cell_no=0,
            destination_location_id=self.shipping_place.id,
            performed_by=self.user,
        )

        self.pallet.refresh_from_db()
        self.pallet_container.refresh_from_db()
        self.box_container.refresh_from_db()
        self.balance.refresh_from_db()
        self.assertEqual(operation.destination_zone_code, "OTG")
        self.assertEqual(self.pallet.cell_id, self.source_cell.id)
        self.assertEqual(
            self.pallet_container.current_location_id,
            self.shipping_place.id,
        )
        self.assertEqual(self.box_container.current_location_id, self.shipping_place.id)
        self.assertEqual(self.balance.reserved_qty, 0)
        self.assertEqual(WarehouseReserve.objects.count(), reserve_count)

    def test_picker_accepts_loc_prefixed_exact_otg_destination(self):
        destination = _picker_fbs_location_from_scan(
            f"LOC:MSK:{self.shipping_place.location_code}"
        )

        self.assertEqual(destination.id, self.shipping_place.id)

    def test_exact_otg_rejects_other_client_pallet_until_mixing_enabled(self):
        other_agency = Agency.objects.create(agn_name="Другой клиент FBS")
        other_cell = FbsStorageCell.objects.create(
            cell_code="FBS@OTHER-OTG",
            location=self.shipping_place,
            client_cluster=other_agency.id,
        )
        other_container = WarehouseContainer.objects.create(
            agency=other_agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="FBS-PALLET-OTHER-OTG",
            current_location=self.shipping_place,
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type=FBS_STORAGE_CONTEXT_TYPE,
            source_context_id="FBS-PALLET-OTHER-OTG",
        )
        FbsPallet.objects.create(
            agency=other_agency,
            pallet_code="FBS-PALLET-OTHER-OTG",
            cell=other_cell,
            warehouse_container=other_container,
            status=FbsPallet.STATUS_ACTIVE,
        )
        operation = start_fbs_free_relocation(
            pallet_id=self.pallet.id,
            performed_by=self.user,
        )

        with self.assertRaisesMessage(FbsMovementError, "другого клиента"):
            complete_fbs_free_relocation(
                operation_id=operation.id,
                destination_zone_code="OTG",
                destination_row_no=0,
                destination_section_no=0,
                destination_tier_no=0,
                destination_cell_no=0,
                destination_location_id=self.shipping_place.id,
                performed_by=self.user,
            )

        self.shipping_place.allow_mixed_client_pallets = True
        self.shipping_place.save(update_fields=["allow_mixed_client_pallets"])
        operation = complete_fbs_free_relocation(
            operation_id=operation.id,
            destination_zone_code="OTG",
            destination_row_no=0,
            destination_section_no=0,
            destination_tier_no=0,
            destination_cell_no=0,
            destination_location_id=self.shipping_place.id,
            performed_by=self.user,
        )

        self.assertEqual(operation.destination_location_id, self.shipping_place.id)

    def test_exact_otg_honors_configured_container_capacity(self):
        self.shipping_place.capacity_containers = 1
        self.shipping_place.save(update_fields=["capacity_containers"])
        existing_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="FBS-PALLET-SAME-CLIENT-OTG",
            current_location=self.shipping_place,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        existing_cell = FbsStorageCell.objects.create(
            cell_code="FBS@SAME-CLIENT-OTG",
            location=self.shipping_place,
            client_cluster=self.agency.id,
        )
        FbsPallet.objects.create(
            agency=self.agency,
            pallet_code=existing_container.container_code,
            cell=existing_cell,
            warehouse_container=existing_container,
            status=FbsPallet.STATUS_ACTIVE,
        )
        operation = start_fbs_free_relocation(
            pallet_id=self.pallet.id,
            performed_by=self.user,
        )

        with self.assertRaisesMessage(FbsMovementError, "свободно 0"):
            complete_fbs_free_relocation(
                operation_id=operation.id,
                destination_zone_code="OTG",
                destination_row_no=0,
                destination_section_no=0,
                destination_tier_no=0,
                destination_cell_no=0,
                destination_location_id=self.shipping_place.id,
                performed_by=self.user,
            )

    def test_fbs_buffer_move_from_otg_to_exact_pr_is_allowed(self):
        self._place_physical_pallet(self.shipping_buffer)
        operation = start_fbs_free_relocation(
            pallet_id=self.pallet.id,
            performed_by=self.user,
            expected_location_id=self.shipping_buffer.id,
        )

        operation = complete_fbs_free_relocation(
            operation_id=operation.id,
            destination_zone_code="PR",
            destination_row_no=0,
            destination_section_no=0,
            destination_tier_no=0,
            destination_cell_no=0,
            destination_location_id=self.receiving_place.id,
            performed_by=self.user,
        )

        self.pallet_container.refresh_from_db()
        self.box_container.refresh_from_db()
        self.assertEqual(operation.source_zone_code, "OTG")
        self.assertEqual(operation.destination_zone_code, "PR")
        self.assertEqual(
            self.pallet_container.current_location_id,
            self.receiving_place.id,
        )
        self.assertEqual(self.box_container.current_location_id, self.receiving_place.id)

    def test_generic_pr_and_otg_destinations_are_rejected_by_write_path(self):
        self._place_physical_pallet(self.receiving_buffer)
        operation = start_fbs_free_relocation(
            pallet_id=self.pallet.id,
            performed_by=self.user,
            expected_location_id=self.receiving_buffer.id,
        )

        with self.assertRaisesMessage(FbsMovementError, "конкретное место"):
            complete_fbs_free_relocation(
                operation_id=operation.id,
                destination_zone_code="OTG",
                destination_row_no=0,
                destination_section_no=0,
                destination_tier_no=0,
                destination_cell_no=0,
                performed_by=self.user,
            )

    def test_fbs_pallet_moves_from_os_to_exact_otg(self):
        operation = start_fbs_free_relocation(
            pallet_id=self.pallet.id,
            performed_by=self.user,
        )

        operation = complete_fbs_free_relocation(
            operation_id=operation.id,
            destination_zone_code="OTG",
            destination_row_no=0,
            destination_section_no=0,
            destination_tier_no=0,
            destination_cell_no=0,
            destination_location_id=self.shipping_place.id,
            performed_by=self.user,
        )

        self.pallet_container.refresh_from_db()
        self.assertEqual(operation.destination_zone_code, "OTG")
        self.assertEqual(self.pallet_container.current_location_id, self.shipping_place.id)

    def test_generic_pr_source_can_be_evacuated_to_os(self):
        self._place_physical_pallet(self.receiving_buffer)
        operation = start_fbs_free_relocation(
            pallet_id=self.pallet.id,
            performed_by=self.user,
            expected_location_id=self.receiving_buffer.id,
        )

        operation = complete_fbs_free_relocation(
            operation_id=operation.id,
            destination_zone_code="OS",
            destination_row_no=2,
            destination_section_no=2,
            destination_tier_no=1,
            destination_cell_no=1,
            performed_by=self.user,
        )

        self.pallet.refresh_from_db()
        self.pallet_container.refresh_from_db()
        self.assertEqual(operation.destination_zone_code, "OS")
        self.assertEqual(self.pallet.cell_id, self.destination_cell.id)
        self.assertEqual(self.pallet_container.current_location_id, self.destination_location.id)

    def test_generic_pr_source_can_be_bound_to_exact_pr_place(self):
        self._place_physical_pallet(self.receiving_buffer)
        operation = start_fbs_free_relocation(
            pallet_id=self.pallet.id,
            performed_by=self.user,
            expected_location_id=self.receiving_buffer.id,
        )

        operation = complete_fbs_free_relocation(
            operation_id=operation.id,
            destination_zone_code="PR",
            destination_row_no=0,
            destination_section_no=0,
            destination_tier_no=0,
            destination_cell_no=0,
            destination_location_id=self.receiving_place.id,
            performed_by=self.user,
        )

        self.pallet_container.refresh_from_db()
        self.assertEqual(operation.destination_zone_code, "PR")
        self.assertEqual(self.pallet_container.current_location_id, self.receiving_place.id)

    def test_reachtruck_screen_completes_pr_to_exact_otg_place(self):
        self._place_physical_pallet(self.receiving_buffer)
        request = self._request()
        start_result = start_free_move(
            request,
            pallet_code=self.pallet.pallet_code,
            role="reachtruck_driver",
        )
        self.assertIn("OTG", start_result.message)

        result = complete_free_move(request, scan_value="OTG-1-1")

        self.assertEqual(result.operation.destination_zone_code, "OTG")
        self.assertIn("OTG-1-1", result.message)

    def test_reachtruck_screen_evacuates_generic_pr_to_os(self):
        self._place_physical_pallet(self.receiving_buffer)
        request = self._request()
        start_free_move(
            request,
            pallet_code=self.pallet.pallet_code,
            role="reachtruck_driver",
        )

        result = complete_free_move(request, scan_value="A-2/1-1")

        self.assertEqual(result.operation.destination_zone_code, "OS")
        self.assertIn("точное место OS", result.message)

    def test_destination_parser_rejects_generic_fbs_buffer_zones(self):
        receiving, receiving_error = parse_destination_scan("PR")
        shipping, shipping_error = parse_destination_scan("]C1OTG")

        self.assertIsNone(receiving)
        self.assertIn("Общая зона PR запрещена", receiving_error)
        self.assertIsNone(shipping)
        self.assertIn("Общая зона OTG запрещена", shipping_error)

    def _general_box_stock(
        self,
        *,
        box_code: str,
        location: WarehouseLocation,
        agency=None,
        parent=None,
        qty: int = 7,
    ):
        agency = agency or self.agency
        container = WarehouseContainer.objects.create(
            agency=agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code=box_code,
            parent_container=parent,
            current_location=location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=agency,
            stock_unit_type="item",
            source_context_type="receiving",
            source_context_id=f"TEST-{box_code}",
            sku_ref=self.sku if agency == self.agency else None,
            sku_code="GENERAL-SKU-1",
            name="Товар общего склада",
            barcode="4600000000999",
            qty=qty,
            available_qty=qty,
            processing_reserved_qty=0,
            shipping_reserved_qty=0,
            other_reserved_qty=0,
            container=container,
            container_code=box_code,
            parent_container=parent,
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code=WarehouseStateCode.STORED.value,
        )
        return container, snapshot

    def test_general_box_without_pallet_moves_directly_to_os_without_quantity_change(self):
        source = self._location(row=7, tier=1, code="G-7/2-1")
        container, snapshot = self._general_box_stock(
            box_code="GENERAL-BOX-1",
            location=source,
        )
        quantities_before = (
            snapshot.qty,
            snapshot.available_qty,
            snapshot.processing_reserved_qty,
            snapshot.shipping_reserved_qty,
            snapshot.other_reserved_qty,
        )
        request = self._request()

        started = start_free_move(
            request,
            pallet_code=container.container_code,
            role="reachtruck_driver",
        )
        self.assertEqual(started.operation.context_type, "reachtruck_free_box")
        self.assertIn("OS", moving_context(request)["destination_prompt"])

        completed = complete_free_move(request, scan_value="A-2/1-1")

        snapshot.refresh_from_db()
        container.refresh_from_db()
        self.assertEqual(completed.operation.destination_location_id, self.destination_location.id)
        self.assertIsNone(snapshot.parent_container_id)
        self.assertIsNone(container.parent_container_id)
        self.assertEqual(snapshot.location_id, self.destination_location.id)
        self.assertEqual(container.current_location_id, self.destination_location.id)
        self.assertEqual(
            (
                snapshot.qty,
                snapshot.available_qty,
                snapshot.processing_reserved_qty,
                snapshot.shipping_reserved_qty,
                snapshot.other_reserved_qty,
            ),
            quantities_before,
        )
        self.assertIn("Количество товара и резервы не изменены", completed.message)
        task_payload = completed.operation.tasks.get().payload
        self.assertTrue(task_payload["destination_exact_place"])
        self.assertEqual(task_payload["destination_pallet_id"], 0)

    def test_general_box_without_pallet_moves_directly_to_exact_pr_and_otg(self):
        for suffix, destination in (
            ("PR", self.receiving_place),
            ("OTG", self.shipping_place),
        ):
            with self.subTest(zone=suffix):
                source = self._location(
                    row=7 if suffix == "PR" else 8,
                    tier=1,
                    code=f"G-{suffix}/2-1",
                )
                container, snapshot = self._general_box_stock(
                    box_code=f"GENERAL-BOX-{suffix}",
                    location=source,
                )
                quantities_before = (snapshot.qty, snapshot.available_qty)
                operation = start_general_box_relocation(
                    box_code=container.container_code,
                    performed_by=self.user,
                    expected_location_id=source.id,
                )

                operation = complete_general_box_relocation(
                    operation_id=operation.id,
                    destination_scan=destination.location_code,
                    performed_by=self.user,
                )

                snapshot.refresh_from_db()
                container.refresh_from_db()
                self.assertEqual(operation.destination_location_id, destination.id)
                self.assertIsNone(snapshot.parent_container_id)
                self.assertIsNone(container.parent_container_id)
                self.assertEqual(snapshot.location_id, destination.id)
                self.assertEqual(container.current_location_id, destination.id)
                self.assertEqual((snapshot.qty, snapshot.available_qty), quantities_before)

    def test_general_loose_boxes_of_one_client_can_share_os_within_capacity(self):
        source = self._location(row=7, tier=1, code="G-7/2-1")
        self.destination_location.capacity_containers = 2
        self.destination_location.save(update_fields=["capacity_containers"])
        existing_container, _existing_snapshot = self._general_box_stock(
            box_code="GENERAL-BOX-EXISTING",
            location=self.destination_location,
        )
        moving_container, moving_snapshot = self._general_box_stock(
            box_code="GENERAL-BOX-MOVING",
            location=source,
        )
        operation = start_general_box_relocation(
            box_code=moving_container.container_code,
            performed_by=self.user,
        )

        operation = complete_general_box_relocation(
            operation_id=operation.id,
            destination_scan=self.destination_location.location_code,
            performed_by=self.user,
        )

        moving_snapshot.refresh_from_db()
        moving_container.refresh_from_db()
        existing_container.refresh_from_db()
        self.assertEqual(moving_snapshot.location_id, self.destination_location.id)
        self.assertEqual(moving_container.current_location_id, self.destination_location.id)
        self.assertEqual(existing_container.current_location_id, self.destination_location.id)
        self.assertTrue(operation.tasks.get().payload["destination_shared_loose_boxes"])

    def test_general_box_can_still_move_to_scanned_pallet_without_quantity_change(self):
        source = self._location(row=7, tier=1, code="G-7/2-1")
        moving_container, moving_snapshot = self._general_box_stock(
            box_code="GENERAL-BOX-TO-PALLET",
            location=source,
        )
        destination_pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="GENERAL-PALLET-DESTINATION",
            current_location=self.destination_location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        quantities_before = (moving_snapshot.qty, moving_snapshot.available_qty)
        operation = start_general_box_relocation(
            box_code=moving_container.container_code,
            performed_by=self.user,
        )

        operation = complete_general_box_relocation(
            operation_id=operation.id,
            destination_scan=destination_pallet.container_code,
            performed_by=self.user,
        )

        moving_snapshot.refresh_from_db()
        moving_container.refresh_from_db()
        self.assertEqual(moving_snapshot.parent_container_id, destination_pallet.id)
        self.assertEqual(moving_container.parent_container_id, destination_pallet.id)
        self.assertEqual(moving_snapshot.location_id, self.destination_location.id)
        self.assertEqual((moving_snapshot.qty, moving_snapshot.available_qty), quantities_before)
        self.assertEqual(operation.tasks.get().payload["destination_type"], "pallet")

    def test_general_box_rejects_generic_pr_zone_as_destination(self):
        source = self._location(row=7, tier=1, code="G-7/2-1")
        container, _snapshot = self._general_box_stock(
            box_code="GENERAL-BOX-GENERIC-PR",
            location=source,
        )
        operation = start_general_box_relocation(
            box_code=container.container_code,
            performed_by=self.user,
        )

        with self.assertRaisesMessage(ValueError, "конкретное место"):
            complete_general_box_relocation(
                operation_id=operation.id,
                destination_scan="PR",
                performed_by=self.user,
            )
