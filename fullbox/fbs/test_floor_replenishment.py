from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from reachtruck.models import MoveRequest, MoveTask
from sklad.models import WarehouseLocation
from sku.models import Agency, SKU, SKUBarcode

from .exceptions import FbsMovementError
from .models import (
    FbsBox,
    FbsIntegrationProfile,
    FbsInternalMovement,
    FbsOrder,
    FbsOrderItem,
    FbsOrderStockAllocation,
    FbsOrderTraceability,
    FbsPallet,
    FbsStockBalance,
    FbsStorageCell,
)
from .management.commands.launch_fbs_wave import _ready_agency_waves
from .services.floor_replenishment import (
    analyze_floor_replenishment_needs,
    create_floor_replenishment_movement,
)
from .services.movements import (
    FLOOR_REPLENISHMENT_COMMENT_PREFIX,
    create_internal_movement,
)
from .services.picking import (
    create_pick_batches,
    reserve_order_stock,
)
from .services.reachtruck_bridge import (
    FLOOR_REPLENISHMENT_MARKER,
    build_fbs_mobile_execution_snapshot,
    scan_fbs_move_task,
    take_fbs_move_task,
)
from .tsd_views import _pick_location_label


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_STATUS_PULL_ENABLED=False,
)
class FbsFirstFloorWaveTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="fbs-floor-driver")
        self.agency = Agency.objects.create(agn_name="FBS first floor client")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            name="FBS first floor profile",
            external_warehouse_id="fbs-first-floor-warehouse",
            is_active=True,
        )
        self.first_cell = self._cell(tier=1, cell_no=1, purpose=FbsStorageCell.PURPOSE_PICK)
        self.upper_cell = self._cell(tier=3, cell_no=2, purpose=FbsStorageCell.PURPOSE_RESERVE)
        self.first_pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-FLOOR-PALLET-1",
            cell=self.first_cell,
            max_boxes=10,
            status=FbsPallet.STATUS_ACTIVE,
        )
        self.upper_pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-FLOOR-PALLET-3",
            cell=self.upper_cell,
            max_boxes=10,
            status=FbsPallet.STATUS_ACTIVE,
        )
        self.first_box = FbsBox.objects.create(
            agency=self.agency,
            pallet=self.first_pallet,
            box_code="FBS-FLOOR-BOX-1",
            status=FbsBox.STATUS_ACTIVE,
        )
        self.upper_box = FbsBox.objects.create(
            agency=self.agency,
            pallet=self.upper_pallet,
            box_code="FBS-FLOOR-BOX-3",
            status=FbsBox.STATUS_ACTIVE,
        )
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="FBS-FLOOR-SKU",
            name="Товар первого яруса",
        )
        self.barcode = "4600000009101"
        SKUBarcode.objects.create(sku=self.sku, value=self.barcode, is_primary=True)
        self.first_balance = self._balance(
            box=self.first_box,
            identity="first-floor",
            qty=2,
            available=2,
        )
        self.upper_balance = self._balance(
            box=self.upper_box,
            identity="upper-floor",
            qty=5,
            available=5,
        )

    def _cell(self, *, tier, cell_no, purpose):
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=71,
            section_no=1,
            tier_no=tier,
            cell_no=cell_no,
            location_code=f"FBS-FLOOR-{tier}-{cell_no}",
            is_active=True,
            is_storage=True,
            is_pickable=True,
        )
        return FbsStorageCell.objects.create(
            cell_code=f"FBS-FLOOR-CELL-{tier}-{cell_no}",
            location=location,
            purpose=purpose,
            is_active=True,
        )

    def _balance(self, *, box, identity, qty, available):
        return FbsStockBalance.objects.create(
            agency=self.agency,
            box=box,
            sku_ref=self.sku,
            identity_key=identity,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode=self.barcode,
            qty=qty,
            available_qty=available,
            reserved_qty=qty - available,
        )

    def _order(self, *, external_id, quantity=1, status=FbsOrder.STATUS_RECEIVED):
        order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id=external_id,
            internal_status=status,
        )
        item = FbsOrderItem.objects.create(
            order=order,
            external_line_id=f"{external_id}-LINE",
            external_sku=self.sku.sku_code,
            sku=self.sku,
            barcode=self.barcode,
            product_name=self.sku.name,
            quantity=quantity,
            requirements={},
        )
        return order, item

    def test_reservation_uses_first_tier_only(self):
        order, _ = self._order(external_id="FLOOR-RESERVE-1")

        result = reserve_order_stock(order_id=order.id, reserved_by=self.user)

        self.assertTrue(result.reserved)
        self.assertEqual(result.allocations[0].balance_id, self.first_balance.id)

    def test_upper_only_stock_is_reserved_with_exact_source_address(self):
        self.first_balance.available_qty = 0
        self.first_balance.qty = 0
        self.first_balance.save(update_fields=["available_qty", "qty", "updated_at"])
        order, _ = self._order(external_id="FLOOR-WAIT-1")

        result = reserve_order_stock(order_id=order.id, reserved_by=self.user)

        self.assertTrue(result.reserved)
        self.assertEqual(result.status, FbsOrder.STATUS_RESERVED)
        self.assertEqual(result.allocations[0].balance_id, self.upper_balance.id)
        self.assertIn(
            "Этаж 3",
            _pick_location_label(result.allocations[0].balance.box.pallet.cell),
        )
        self.upper_balance.refresh_from_db()
        self.assertEqual(self.upper_balance.reserved_qty, 1)

    def test_analysis_proposes_whole_box_from_upper_tier(self):
        self.first_balance.available_qty = 0
        self.first_balance.qty = 0
        self.first_balance.save(update_fields=["available_qty", "qty", "updated_at"])
        self._order(external_id="FLOOR-DEMAND-1", quantity=3)

        analysis = analyze_floor_replenishment_needs()

        self.assertEqual(len(analysis.suggestions), 1)
        suggestion = analysis.suggestions[0]
        self.assertEqual(suggestion.source_box_id, self.upper_box.id)
        self.assertEqual(suggestion.target_pallet_id, self.first_pallet.id)
        self.assertEqual(suggestion.source_tier, 3)
        self.assertEqual(suggestion.covered_qty, 5)

    def test_driver_scans_address_box_and_first_tier_destination(self):
        result = create_floor_replenishment_movement(
            source_box_id=self.upper_box.id,
            target_pallet_id=self.first_pallet.id,
            barcode=self.barcode,
            needed_qty=3,
            requested_by=self.user,
            comment="Подать товар к ближайшей волне.",
        )
        task = result.task
        self.assertTrue(task.payload[FLOOR_REPLENISHMENT_MARKER])
        self.assertEqual(task.request.status, MoveRequest.STATUS_PLANNED)

        taken = take_fbs_move_task(
            task=task,
            user=self.user,
            employee_id=101,
            employee_name="Водитель FBS",
        )
        self.assertTrue(taken.ok)
        task.refresh_from_db()
        self.assertEqual(build_fbs_mobile_execution_snapshot(task)["current_step"], "source")

        source_scan = self.upper_cell.warehouse_location_code
        target_scan = self.first_cell.warehouse_location_code
        source_result = scan_fbs_move_task(
            task=task,
            scan_value=source_scan,
            user=self.user,
            employee_id=101,
            employee_name="Водитель FBS",
        )
        self.assertTrue(source_result.ok)
        box_result = scan_fbs_move_task(
            task=task,
            scan_value=self.upper_box.box_code,
            user=self.user,
            employee_id=101,
            employee_name="Водитель FBS",
        )
        self.assertTrue(box_result.ok)
        wrong_target = scan_fbs_move_task(
            task=task,
            scan_value="WRONG-FIRST-FLOOR",
            user=self.user,
            employee_id=101,
            employee_name="Водитель FBS",
        )
        self.assertFalse(wrong_target.ok)
        completed = scan_fbs_move_task(
            task=task,
            scan_value=target_scan,
            user=self.user,
            employee_id=101,
            employee_name="Водитель FBS",
        )
        self.assertTrue(completed.ok)
        self.assertTrue(completed.completed)
        self.upper_box.refresh_from_db()
        task.refresh_from_db()
        result.movement.refresh_from_db()
        task.request.refresh_from_db()
        self.assertEqual(self.upper_box.pallet_id, self.first_pallet.id)
        self.assertEqual(result.movement.status, FbsInternalMovement.STATUS_DONE)
        self.assertEqual(task.status, MoveTask.STATUS_DONE)
        self.assertEqual(task.request.status, MoveRequest.STATUS_DONE)

    def test_low_level_service_rejects_non_first_tier_route(self):
        with self.assertRaises(FbsMovementError):
            create_internal_movement(
                mode=FbsInternalMovement.MODE_BOX,
                source_box=self.first_box,
                target_pallet=self.upper_pallet,
                requested_by=self.user,
                comment=f"{FLOOR_REPLENISHMENT_COMMENT_PREFIX} invalid route",
            )

    def test_open_request_reserves_last_target_box_slot(self):
        self.first_pallet.max_boxes = 2
        self.first_pallet.save(update_fields=["max_boxes", "updated_at"])
        create_floor_replenishment_movement(
            source_box_id=self.upper_box.id,
            target_pallet_id=self.first_pallet.id,
            barcode=self.barcode,
            needed_qty=1,
            requested_by=self.user,
        )
        second_upper_box = FbsBox.objects.create(
            agency=self.agency,
            pallet=self.upper_pallet,
            box_code="FBS-FLOOR-BOX-3-B",
            status=FbsBox.STATUS_ACTIVE,
        )
        self._balance(
            box=second_upper_box,
            identity="upper-floor-second",
            qty=2,
            available=2,
        )

        with self.assertRaises(FbsMovementError):
            create_floor_replenishment_movement(
                source_box_id=second_upper_box.id,
                target_pallet_id=self.first_pallet.id,
                barcode=self.barcode,
                needed_qty=1,
                requested_by=self.user,
            )

    def test_upper_tier_reservation_enters_wave_without_release(self):
        self.first_balance.available_qty = 0
        self.first_balance.qty = 0
        self.first_balance.save(update_fields=["available_qty", "qty", "updated_at"])
        order, item = self._order(
            external_id="FLOOR-LEGACY-1",
            status=FbsOrder.STATUS_RESERVED,
        )
        self.upper_balance.available_qty = 4
        self.upper_balance.reserved_qty = 1
        self.upper_balance.save(
            update_fields=["available_qty", "reserved_qty", "updated_at"]
        )
        allocation = FbsOrderStockAllocation.objects.create(
            order_item=item,
            balance=self.upper_balance,
            qty_reserved=1,
            status=FbsOrderStockAllocation.STATUS_RESERVED,
            reserved_by=self.user,
        )
        FbsOrderTraceability.objects.create(
            allocation=allocation,
            qty=1,
            status=FbsOrderTraceability.STATUS_RESERVED,
        )
        ready_groups = _ready_agency_waves()
        self.assertEqual(len(ready_groups), 1)
        self.assertEqual(ready_groups[0].agency_id, self.agency.id)
        self.assertEqual(ready_groups[0].order_ids, (order.id,))
        self.assertEqual(ready_groups[0].unit_count, 1)

        batches = create_pick_batches(order_ids=[order.id])

        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].planned_qty, 1)
        order.refresh_from_db()
        allocation.refresh_from_db()
        self.upper_balance.refresh_from_db()
        self.assertEqual(order.internal_status, FbsOrder.STATUS_QUEUED_FOR_PICK)
        self.assertIsNotNone(allocation.pick_task_id)
        self.assertEqual(allocation.status, FbsOrderStockAllocation.STATUS_RESERVED)
        self.assertEqual(self.upper_balance.available_qty, 4)
        self.assertEqual(self.upper_balance.reserved_qty, 1)
