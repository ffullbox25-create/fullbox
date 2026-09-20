from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone
from employees.models import Employee

from fbs.exceptions import (
    FbsHandoverError,
    FbsInventoryError,
    FbsMovementError,
    FbsPickingError,
)
from fbs.models import (
    FbsClientStoragePolicy,
    FbsHandoverBatch,
    FbsHandoverBox,
    FbsHandoverOrderAssignment,
    FbsIntegrationProfile,
    FbsInventoryLine,
    FbsInventorySession,
    FbsMarketplaceMetadataTransfer,
    FbsOrder,
    FbsOrderItem,
    FbsOrderLabel,
    FbsOrderStockAllocation,
    FbsPickBatch,
    FbsPickingCart,
    FbsReplenishmentPolicy,
    FbsStockBalance,
    FbsStorageCell,
    FbsStorageDailyUsage,
    FbsWorkstation,
)
from audit.models import OrderAuditEntry
from fbs.services import (
    activate_drained_inventory,
    add_handover_box,
    add_order_to_handover_box,
    approve_inventory,
    approve_compliance_override,
    capture_daily_storage_usage,
    claim_internal_movement,
    claim_pick_batch,
    claim_next_pick_batch,
    close_handover_box,
    complete_pick_allocation,
    create_fbs_box,
    create_fbs_pallet,
    create_handover_batch,
    create_internal_movement,
    create_inventory_session,
    create_pick_batches,
    dispatch_handover_batch,
    finish_inventory_count,
    handover_pick_batch_for_verification,
    handover_manifest,
    record_inventory_scan,
    refresh_handover_acceptance,
    reserve_order_stock,
    scan_handover_box,
    scan_internal_movement,
    validate_pick_equipment,
)
from sku.models import Agency, SKU, SKUBarcode
from sklad.models import WarehouseEvent, WarehouseLocation


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_ZONE_CODE="FBS",
)
class FbsOperationsBase(TestCase):
    def setUp(self):
        users = get_user_model()
        self.picker = users.objects.create_user(username="fbs_ops_picker")
        self.other_picker = users.objects.create_user(username="fbs_ops_picker_2")
        self.manager = users.objects.create_user(username="fbs_ops_manager")
        self.agency = Agency.objects.create(agn_name="FBS operations client")
        self.other_agency = Agency.objects.create(agn_name="Foreign FBS client")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="OPS-SKU-1",
            name="Operations product",
            length_mm=100,
            width_mm=100,
            height_mm=100,
        )
        self.sku_barcode = SKUBarcode.objects.create(
            sku=self.sku,
            value="OPS-BARCODE",
            is_primary=True,
        )
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB operations",
            external_account_id="ops-account",
            external_warehouse_id="ops-warehouse",
        )
        self.cells = []
        self.pallets = []
        self.boxes = []
        for index in range(1, 4):
            location = WarehouseLocation.objects.create(
                warehouse_code="MSK",
                zone_code="FBS",
                zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
                row_no=1,
                section_no=index,
                tier_no=1 if index < 3 else 2,
                cell_no=1,
                location_code=f"FBS-OPS-1-{index}-1-1",
                is_storage=True,
                is_pickable=index < 3,
            )
            cell = FbsStorageCell.objects.create(
                cell_code=location.location_code,
                location=location,
                purpose=(
                    FbsStorageCell.PURPOSE_PICK
                    if index < 3
                    else FbsStorageCell.PURPOSE_RESERVE
                ),
                client_cluster=1,
            )
            pallet = create_fbs_pallet(
                agency=self.agency,
                cell=cell,
                pallet_code=f"OPS-PALLET-{index}",
            )
            box = create_fbs_box(
                agency=self.agency,
                pallet=pallet,
                box_code=f"OPS-BOX-{index}",
            )
            box.width_mm = 500
            box.height_mm = 500
            box.depth_mm = 500
            box.save(update_fields=["width_mm", "height_mm", "depth_mm", "updated_at"])
            self.cells.append(cell)
            self.pallets.append(pallet)
            self.boxes.append(box)
        self.workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-700001",
            name="Operations workstation",
            printer_name="OPS-PRINTER",
            max_parallel_waves=2,
        )
        self.carts = [
            FbsPickingCart.objects.create(
                barcode=f"FBS-CART-70000{index}",
                name=f"Operations cart {index}",
            )
            for index in range(1, 5)
        ]

    def balance(self, *, qty=5, box=None, identity="ops-stock", marking_code=""):
        return FbsStockBalance.objects.create(
            agency=self.agency,
            box=box or self.boxes[0],
            sku_ref=self.sku,
            identity_key=identity,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode="OPS-BARCODE",
            marking_code=marking_code,
            lot_code="OPS-LOT",
            expiry_date=date.today() + timedelta(days=365),
            qty=qty,
            available_qty=qty,
        )

    def order(self, *, quantity=1, suffix="1", status=FbsOrder.STATUS_RECEIVED):
        order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id=f"OPS-ORDER-{suffix}",
            internal_status=status,
            cutoff_at=timezone.now() + timedelta(hours=2),
        )
        FbsOrderItem.objects.create(
            order=order,
            external_line_id=f"OPS-LINE-{suffix}",
            external_sku=self.sku.sku_code,
            barcode=self.sku_barcode.value,
            sku=self.sku,
            product_name=self.sku.name,
            quantity=quantity,
        )
        return order


class FbsWaveRulesTests(FbsOperationsBase):
    def test_wave_is_single_client_and_limited_by_orders_and_units(self):
        self.balance(qty=120)
        for index in range(60):
            order = self.order(quantity=2, suffix=str(index))
            reserve_order_stock(order_id=order.id)

        batches = create_pick_batches(max_orders_per_batch=100)

        self.assertEqual([batch.planned_qty for batch in batches], [100, 20])
        self.assertEqual([batch.tasks.count() for batch in batches], [50, 10])
        self.assertTrue(all(batch.agency_id == self.agency.id for batch in batches))

    def test_claim_assigns_whole_wave_and_requires_previous_handover(self):
        self.balance(qty=3)
        for index in range(3):
            order = self.order(suffix=str(index))
            reserve_order_stock(order_id=order.id)
            create_pick_batches(max_orders_per_batch=50)
        batches = list(FbsPickBatch.objects.order_by("id"))

        claim_pick_batch(
            batch_id=batches[0].id,
            assigned_to=self.picker,
            workstation_scan=self.workstation.barcode,
            cart_scan=self.carts[0].barcode,
        )
        self.assertFalse(batches[0].tasks.exclude(assigned_to=self.picker).exists())

        with self.assertRaisesMessage(FbsPickingError, "Сначала сдайте текущую волну"):
            claim_pick_batch(
                batch_id=batches[1].id,
                assigned_to=self.picker,
                workstation_scan=self.workstation.barcode,
                cart_scan=self.carts[1].barcode,
            )
        with self.assertRaises(FbsPickingError):
            claim_pick_batch(
                batch_id=batches[0].id,
                assigned_to=self.other_picker,
                workstation_scan=self.workstation.barcode,
                cart_scan=self.carts[3].barcode,
            )

    def test_cart_stays_busy_until_handover_and_workstation_is_chosen_at_end(self):
        balance = self.balance(qty=3)
        for index in range(3):
            order = self.order(suffix=f"cart-{index}")
            reserve_order_stock(order_id=order.id)
            create_pick_batches(max_orders_per_batch=50)
        batches = list(FbsPickBatch.objects.order_by("id"))

        claim_pick_batch(
            batch_id=batches[0].id,
            assigned_to=self.picker,
            workstation_scan=self.workstation.barcode,
            cart_scan=self.carts[0].barcode,
        )
        allocation = FbsOrderStockAllocation.objects.get(pick_task__batch=batches[0])
        complete_pick_allocation(
            allocation_id=allocation.id,
            cell_scan=self.cells[0].cell_code,
            box_scan=self.boxes[0].box_code,
            item_scan=balance.barcode,
            performed_by=self.picker,
        )

        first = FbsPickBatch.objects.get(pk=batches[0].id)
        self.assertEqual(first.status, FbsPickBatch.STATUS_VERIFICATION)
        self.assertIsNone(first.picking_completed_at)
        self.assertIsNone(first.workstation_id)
        with self.assertRaises(FbsPickingError):
            claim_pick_batch(
                batch_id=batches[1].id,
                assigned_to=self.picker,
                workstation_scan=self.workstation.barcode,
                cart_scan=self.carts[0].barcode,
            )
        destination = FbsWorkstation.objects.create(
            barcode="FBS-WS-700002",
            name="Actual handover workstation",
            printer_name="OPS-PRINTER-2",
            max_parallel_waves=2,
        )

        handover_pick_batch_for_verification(
            batch_id=first.id,
            workstation_scan=destination.barcode,
            performed_by=self.picker,
        )
        first.refresh_from_db()
        self.assertEqual(first.workstation_id, destination.id)
        self.assertIsNotNone(first.picking_completed_at)
        second = claim_pick_batch(
            batch_id=batches[1].id,
            assigned_to=self.picker,
            workstation_scan="",
            cart_scan=self.carts[0].barcode,
        )
        self.assertEqual(second.cart_id, self.carts[0].id)
        self.assertIsNone(second.workstation_id)

    def test_claim_next_wave_assigns_oldest_free_wave(self):
        self.balance(qty=3)
        for index in range(3):
            order = self.order(suffix=f"next-{index}")
            reserve_order_stock(order_id=order.id)
            create_pick_batches(max_orders_per_batch=50)
        batches = list(FbsPickBatch.objects.order_by("created_at", "id"))
        claim_pick_batch(
            batch_id=batches[0].id,
            assigned_to=self.other_picker,
            workstation_scan=self.workstation.barcode,
            cart_scan=self.carts[0].barcode,
        )

        claimed = claim_next_pick_batch(
            assigned_to=self.picker,
            workstation_scan=self.workstation.barcode,
            cart_scan=self.carts[1].barcode,
        )

        self.assertEqual(claimed.id, batches[1].id)
        self.assertEqual(claimed.assigned_to_id, self.picker.id)
        self.assertEqual(claimed.cart_id, self.carts[1].id)
        self.assertIsNone(claimed.workstation_id)
        self.assertEqual(
            FbsPickBatch.objects.get(pk=batches[2].id).status,
            FbsPickBatch.STATUS_QUEUED,
        )

    def test_handover_rejects_full_workstation_and_accepts_another_one(self):
        balance = self.balance(qty=2)
        self.workstation.max_parallel_waves = 1
        self.workstation.save(update_fields=["max_parallel_waves", "updated_at"])
        for index in range(2):
            order = self.order(suffix=f"handover-{index}")
            reserve_order_stock(order_id=order.id)
            create_pick_batches(max_orders_per_batch=50)
        batches = list(FbsPickBatch.objects.order_by("id"))
        for batch, picker, cart in zip(
            batches,
            (self.picker, self.other_picker),
            self.carts[:2],
        ):
            claim_pick_batch(
                batch_id=batch.id,
                assigned_to=picker,
                workstation_scan="",
                cart_scan=cart.barcode,
            )
            allocation = FbsOrderStockAllocation.objects.get(pick_task__batch=batch)
            complete_pick_allocation(
                allocation_id=allocation.id,
                cell_scan=self.cells[0].cell_code,
                box_scan=self.boxes[0].box_code,
                item_scan=balance.barcode,
                performed_by=picker,
            )

        handover_pick_batch_for_verification(
            batch_id=batches[0].id,
            workstation_scan=self.workstation.barcode,
            performed_by=self.picker,
        )
        with self.assertRaisesMessage(FbsPickingError, "Рабочее место занято"):
            handover_pick_batch_for_verification(
                batch_id=batches[1].id,
                workstation_scan=self.workstation.barcode,
                performed_by=self.other_picker,
            )
        second_workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-700003",
            name="Free workstation",
            printer_name="OPS-PRINTER-3",
            max_parallel_waves=1,
        )

        handed_over = handover_pick_batch_for_verification(
            batch_id=batches[1].id,
            workstation_scan=second_workstation.barcode,
            performed_by=self.other_picker,
        )

        self.assertEqual(handed_over.workstation_id, second_workstation.id)
        self.assertIsNotNone(handed_over.picking_completed_at)

    def test_equipment_binding_rejects_cart_used_by_other_picker(self):
        self.balance(qty=1)
        order = self.order(suffix="busy-cart")
        reserve_order_stock(order_id=order.id)
        batch = create_pick_batches()[0]
        claim_pick_batch(
            batch_id=batch.id,
            assigned_to=self.other_picker,
            workstation_scan=self.workstation.barcode,
            cart_scan=self.carts[0].barcode,
        )

        with self.assertRaisesMessage(FbsPickingError, "другим сборщиком"):
            validate_pick_equipment(
                assigned_to=self.picker,
                workstation_scan=self.workstation.barcode,
                cart_scan=self.carts[0].barcode,
            )


class FbsInventorySafetyTests(FbsOperationsBase):
    def test_drain_lock_holds_new_order_but_allows_existing_pick_to_finish(self):
        balance = self.balance(qty=2)
        active_order = self.order(suffix="active")
        reserve_order_stock(order_id=active_order.id)
        batch = create_pick_batches()[0]
        claim_pick_batch(
            batch_id=batch.id,
            assigned_to=self.picker,
            workstation_scan=self.workstation.barcode,
            cart_scan=self.carts[0].barcode,
        )
        session = create_inventory_session(
            scope_type=FbsInventorySession.SCOPE_BOX,
            mode=FbsInventorySession.MODE_DRAIN,
            box=self.boxes[0],
            created_by=self.manager,
        )
        waiting_order = self.order(suffix="waiting")

        waiting = reserve_order_stock(order_id=waiting_order.id)

        waiting_order.refresh_from_db()
        self.assertFalse(waiting.reserved)
        self.assertEqual(waiting_order.hold_reason, "inventory")
        with self.assertRaises(FbsInventoryError):
            activate_drained_inventory(session_id=session.id)

        allocation = FbsOrderStockAllocation.objects.get(order_item__order=active_order)
        complete_pick_allocation(
            allocation_id=allocation.id,
            cell_scan=self.cells[0].cell_code,
            box_scan=self.boxes[0].box_code,
            item_scan=balance.barcode,
            performed_by=self.picker,
        )
        activated = activate_drained_inventory(session_id=session.id)
        self.assertEqual(activated.status, FbsInventorySession.STATUS_COUNTING)

    def test_immediate_lock_blocks_execution(self):
        balance = self.balance(qty=1)
        order = self.order(suffix="blocked-pick")
        reserve_order_stock(order_id=order.id)
        batch = create_pick_batches()[0]
        claim_pick_batch(
            batch_id=batch.id,
            assigned_to=self.picker,
            workstation_scan=self.workstation.barcode,
            cart_scan=self.carts[0].barcode,
        )
        create_inventory_session(
            scope_type=FbsInventorySession.SCOPE_BOX,
            mode=FbsInventorySession.MODE_IMMEDIATE,
            box=self.boxes[0],
            created_by=self.manager,
        )
        allocation = FbsOrderStockAllocation.objects.get(order_item__order=order)

        with self.assertRaises(FbsInventoryError):
            complete_pick_allocation(
                allocation_id=allocation.id,
                cell_scan=self.cells[0].cell_code,
                box_scan=self.boxes[0].box_code,
                item_scan=balance.barcode,
                performed_by=self.picker,
            )

    def test_blind_recount_requires_second_person_and_manager_resolution(self):
        balance = self.balance(qty=5)
        session = create_inventory_session(
            scope_type=FbsInventorySession.SCOPE_BOX,
            mode=FbsInventorySession.MODE_IMMEDIATE,
            box=self.boxes[0],
            created_by=self.manager,
        )
        for _ in range(3):
            record_inventory_scan(
                session_id=session.id,
                scan_code=balance.barcode,
                counted_by=self.picker,
            )
        finish_inventory_count(session_id=session.id, counted_by=self.picker)
        with self.assertRaises(FbsInventoryError):
            record_inventory_scan(
                session_id=session.id,
                scan_code=balance.barcode,
                counted_by=self.picker,
            )
        for _ in range(4):
            record_inventory_scan(
                session_id=session.id,
                scan_code=balance.barcode,
                counted_by=self.other_picker,
            )
        finish_inventory_count(session_id=session.id, counted_by=self.other_picker)
        line = FbsInventoryLine.objects.get(session=session)

        approved = approve_inventory(
            session_id=session.id,
            approved_by=self.manager,
            final_counts={line.id: 4},
        )

        balance.refresh_from_db()
        self.assertEqual(balance.qty, 4)
        self.assertEqual(approved.status, FbsInventorySession.STATUS_DONE)
        self.assertTrue(WarehouseEvent.objects.filter(event_type="fbs_inventory_adjusted").exists())


class FbsInternalMovementTests(FbsOperationsBase):
    def test_whole_box_movement_changes_address_without_changing_stock(self):
        balance = self.balance(qty=5)
        movement = create_internal_movement(
            mode="box",
            source_box=self.boxes[0],
            target_pallet=self.pallets[1],
            requested_by=self.manager,
        )
        claim_internal_movement(movement_id=movement.id, assigned_to=self.picker)

        scan_internal_movement(
            movement_id=movement.id,
            source_box_scan=self.boxes[0].box_code,
            target_scan=self.pallets[1].pallet_code,
            performed_by=self.picker,
        )

        self.boxes[0].refresh_from_db()
        balance.refresh_from_db()
        self.assertEqual(self.boxes[0].pallet_id, self.pallets[1].id)
        self.assertEqual((balance.qty, balance.available_qty), (5, 5))

    def test_item_consolidation_preserves_identity_and_total(self):
        source = self.balance(qty=5, identity="consolidated")
        movement = create_internal_movement(
            mode="item",
            source_box=self.boxes[0],
            target_pallet=self.pallets[1],
            target_box=self.boxes[1],
            source_balance=source,
            qty=2,
            requested_by=self.manager,
        )
        claim_internal_movement(movement_id=movement.id, assigned_to=self.picker)
        for _ in range(2):
            movement = scan_internal_movement(
                movement_id=movement.id,
                source_box_scan=self.boxes[0].box_code,
                target_scan=self.boxes[1].box_code,
                item_scan=source.barcode,
                performed_by=self.picker,
            )

        source.refresh_from_db()
        target = FbsStockBalance.objects.get(box=self.boxes[1], identity_key=source.identity_key)
        self.assertEqual((source.qty, target.qty, source.qty + target.qty), (3, 2, 5))
        self.assertEqual((target.lot_code, target.expiry_date), (source.lot_code, source.expiry_date))
        self.assertEqual(movement.status, movement.STATUS_DONE)

    def test_cross_client_movement_is_rejected(self):
        foreign_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            row_no=9,
            section_no=9,
            tier_no=1,
            cell_no=1,
            location_code="FBS-FOREIGN-9-9-1-1",
            is_storage=True,
        )
        foreign_cell = FbsStorageCell.objects.create(
            cell_code=foreign_location.location_code,
            location=foreign_location,
        )
        foreign_pallet = create_fbs_pallet(
            agency=self.other_agency,
            cell=foreign_cell,
            pallet_code="FOREIGN-PALLET",
        )
        self.balance(qty=1)
        with self.assertRaises(FbsMovementError):
            create_internal_movement(
                mode="box",
                source_box=self.boxes[0],
                target_pallet=foreign_pallet,
            )


class FbsHandoverControlTests(FbsOperationsBase):
    def ready_order(self):
        order = self.order(suffix="handover", status=FbsOrder.STATUS_READY_FOR_HANDOVER)
        label = FbsOrderLabel.objects.create(
            order=order,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            external_label_id="WB-LABEL-OPS",
            barcode="WB-ORDER-QR-OPS",
            status=FbsOrderLabel.STATUS_APPLIED,
        )
        item = order.items.get()
        transfer = FbsMarketplaceMetadataTransfer.objects.create(
            order_item=item,
            metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
            value="OPS-KIZ",
            is_required=True,
            status=FbsMarketplaceMetadataTransfer.STATUS_FAILED,
            idempotency_key="f" * 64,
        )
        return order, label, transfer

    def test_box_requires_marketplace_validation_and_turns_green_after_sorted(self):
        order, label, transfer = self.ready_order()
        batch = create_handover_batch(
            profile=self.profile,
            external_supply_id="WB-SUPPLY-1",
            created_by=self.manager,
        )
        box = add_handover_box(batch_id=batch.id, qr_code="WB-BOX-QR-1")
        FbsHandoverOrderAssignment.objects.create(
            batch=batch,
            order=order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
            assigned_by=self.manager,
            confirmed_at=timezone.now(),
        )
        with self.assertRaises(FbsHandoverError):
            add_order_to_handover_box(
                box_id=box.id,
                order_label_scan=label.barcode,
                added_by=self.picker,
            )
        transfer.status = FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED
        transfer.save(update_fields=["status", "updated_at"])
        add_order_to_handover_box(
            box_id=box.id,
            order_label_scan=label.barcode,
            added_by=self.picker,
        )
        close_handover_box(box_id=box.id)
        scan_handover_box(
            batch_id=batch.id,
            box_qr_scan=box.qr_code,
            scanned_by=self.manager,
        )
        batch.marketplace_state = FbsHandoverBatch.MARKETPLACE_COMPLETE
        batch.supply_qr_code = "WB-SUPPLY-QR-1"
        batch.supply_label_file.save(
            "wb-supply-1.png",
            ContentFile(b"\x89PNG\r\n\x1a\nOPS"),
            save=False,
        )
        batch.save(
            update_fields=[
                "marketplace_state",
                "supply_qr_code",
                "supply_label_file",
                "updated_at",
            ]
        )
        dispatch_handover_batch(batch_id=batch.id, dispatched_by=self.manager)

        batch.refresh_from_db()
        box.refresh_from_db()
        self.assertEqual(batch.status, FbsHandoverBatch.STATUS_DISPATCHED)
        self.assertEqual(box.status, FbsHandoverBox.STATUS_DISPATCHED)
        self.assertEqual(handover_manifest(batch_id=batch.id)[0].order_count, 1)
        dispatch_audit = OrderAuditEntry.objects.get(
            order_type="fbs_order",
            order_id=str(order.id),
            payload__source="dispatch_handover_batch",
        )
        self.assertEqual(
            dispatch_audit.payload["changes"]["internal_status"],
            {
                "from": FbsOrder.STATUS_READY_FOR_HANDOVER,
                "to": FbsOrder.STATUS_HANDED_OVER,
            },
        )

        # WB keeps the primary status as ``complete`` and reports supply
        # acceptance separately as ``wbStatus=sorted``.
        order.marketplace_status = "complete"
        order.marketplace_substatus = "sorted"
        order.save(
            update_fields=[
                "marketplace_status",
                "marketplace_substatus",
                "updated_at",
            ]
        )
        refresh_handover_acceptance(batch_id=batch.id)
        batch.refresh_from_db()
        box.refresh_from_db()
        self.assertEqual(batch.status, FbsHandoverBatch.STATUS_ACCEPTED)
        self.assertEqual(box.status, FbsHandoverBox.STATUS_ACCEPTED)

    def test_head_manager_can_audit_override_failed_required_metadata(self):
        order, label, transfer = self.ready_order()
        Employee.objects.create(
            user=self.manager,
            full_name="Начальник склада FBS",
            role="head_manager",
        )
        batch = create_handover_batch(profile=self.profile, created_by=self.manager)
        box = add_handover_box(batch_id=batch.id, qr_code="WB-BOX-OVERRIDE")
        FbsHandoverOrderAssignment.objects.create(
            batch=batch,
            order=order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
            assigned_by=self.manager,
            confirmed_at=timezone.now(),
        )

        approve_compliance_override(
            order_id=order.id,
            metadata_type=transfer.metadata_type,
            reason="Письменное согласование клиента и маркетплейса",
            approved_by=self.manager,
        )
        link = add_order_to_handover_box(
            box_id=box.id,
            order_label_scan=label.barcode,
            added_by=self.picker,
        )

        self.assertEqual(link.order_id, order.id)

    def test_head_manager_cannot_override_marketplace_waiting_state(self):
        order, _label, transfer = self.ready_order()
        Employee.objects.create(
            user=self.manager,
            full_name="Начальник склада FBS",
            role="head_manager",
        )
        transfer.status = FbsMarketplaceMetadataTransfer.STATUS_SENT
        transfer.save(update_fields=["status", "updated_at"])

        with self.assertRaises(FbsHandoverError):
            approve_compliance_override(
                order_id=order.id,
                metadata_type=transfer.metadata_type,
                reason="Нельзя обходить ожидание",
                approved_by=self.manager,
            )


class FbsStorageBillingTests(FbsOperationsBase):
    def test_liter_billing_is_per_sku_and_idempotent(self):
        balance = self.balance(qty=5)
        FbsClientStoragePolicy.objects.create(
            agency=self.agency,
            billing_mode=FbsClientStoragePolicy.BILLING_LITERS,
        )

        first = capture_daily_storage_usage(agency=self.agency)
        second = capture_daily_storage_usage(agency=self.agency)

        usage = FbsStorageDailyUsage.objects.get(agency=self.agency)
        self.assertEqual(first.rows, 1)
        self.assertEqual(second.rows, 1)
        self.assertEqual(usage.quantity, 5)
        self.assertEqual(usage.volume_liters, Decimal("5.000"))
        self.assertTrue(usage.dimensions_complete)

        balance.qty = 0
        balance.available_qty = 0
        balance.save(update_fields=["qty", "available_qty", "updated_at"])
        empty = capture_daily_storage_usage(agency=self.agency)

        self.assertEqual(empty.rows, 0)
        self.assertFalse(FbsStorageDailyUsage.objects.filter(agency=self.agency).exists())

    def test_replenishment_policy_validates_client_and_thresholds(self):
        policy = FbsReplenishmentPolicy(
            agency=self.agency,
            sku=self.sku,
            minimum_qty=5,
            target_qty=20,
        )
        policy.full_clean()
        policy.save()
        self.assertEqual(policy.target_qty, 20)
