from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from sku.models import Agency, SKU, SKUBarcode
from sklad.models import WarehouseLocation

from fbs.integrations.http import MarketplaceHttpResponse
from fbs.models import (
    FbsBox,
    FbsControllerCheckTote,
    FbsControllerPickTote,
    FbsControllerSession,
    FbsControllerToteOrder,
    FbsHandoverBatch,
    FbsHandoverBox,
    FbsHandoverOrder,
    FbsHandoverOrderAssignment,
    FbsIntegrationProfile,
    FbsMarketplaceCommand,
    FbsMarketplaceEvent,
    FbsOrder,
    FbsOrderLabel,
    FbsOrderStockAllocation,
    FbsPallet,
    FbsPickBatch,
    FbsPickingCart,
    FbsPickRestockRequest,
    FbsPickTask,
    FbsStockBalance,
    FbsStorageCell,
    FbsToteZone,
    FbsWorkstation,
)
from fbs.integrations.contracts import WB_READ_HANDOVER_ORDER_IDS
from fbs.exceptions import FbsPickingError
from fbs.services import pull_profile_orders, pull_profile_statuses
from fbs.services.pick_restock import create_order_pick_restock_request
from fbs.services.sync import _payload_hash


class StubTransport:
    def __init__(self, *payloads):
        self.payloads = list(payloads)

    def send(self, profile, spec):
        return MarketplaceHttpResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            content=b"",
            json_payload=self.payloads.pop(0),
        )


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_ORDER_PULL_ENABLED=True,
    FBS_STATUS_PULL_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_OUTBOX_ENABLED=True,
    FBS_ZONE_CODE="FBS",
)
class FbsDuplicateStatusReconciliationTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="FBS duplicate status client")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="FBS-RECONCILE-SKU",
            name="FBS reconciliation product",
        )
        self.barcode = "4600000000777"
        SKUBarcode.objects.create(
            sku=self.sku,
            value=self.barcode,
            is_primary=True,
        )
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=77,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="FBS-RECONCILE-1",
            is_storage=True,
            is_pickable=True,
        )
        cell = FbsStorageCell.objects.create(
            cell_code="FBS-RECONCILE-1",
            location=location,
        )
        pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-RECONCILE-PALLET",
            cell=cell,
            status=FbsPallet.STATUS_ACTIVE,
        )
        box = FbsBox.objects.create(
            agency=self.agency,
            pallet=pallet,
            box_code="FBS-RECONCILE-BOX",
            status=FbsBox.STATUS_ACTIVE,
        )
        self.balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=box,
            sku_ref=self.sku,
            identity_key="7" * 64,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode=self.barcode,
            qty=1,
            available_qty=1,
        )
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB reconciliation",
            external_account_id="wb-reconciliation",
            external_warehouse_id="101",
            stock_mode=FbsIntegrationProfile.STOCK_MODE_DISABLED,
            is_active=True,
            order_pull_enabled=True,
            status_pull_enabled=True,
            stock_push_enabled=False,
        )

    def orders_payload(self, *, count=1):
        return {
            "orders": [
                {
                    "id": 7700 + index,
                    "warehouseId": 101,
                    "nmId": 8800 + index,
                    "chrtId": 9900 + index,
                    "skus": [self.barcode],
                    "article": self.sku.sku_code,
                    "createdAt": "2026-08-16T00:00:00Z",
                    "requiredMeta": [],
                    "optionalMeta": [],
                }
                for index in range(count)
            ]
        }

    @staticmethod
    def status_payload(order_id, *, status="new", substatus="waiting"):
        return {
            "orders": [
                {
                    "id": order_id,
                    "supplierStatus": status,
                    "wbStatus": substatus,
                }
            ]
        }

    def test_duplicate_status_reserves_waiting_order_after_stock_appears(self):
        pull_profile_orders(
            profile_id=self.profile.id,
            transport=StubTransport(self.orders_payload(count=2)),
        )
        waiting = FbsOrder.objects.get(external_order_id="7701")
        self.assertEqual(waiting.internal_status, FbsOrder.STATUS_AWAITING_STOCK)

        status_payload = self.status_payload(7701)
        pull_profile_statuses(
            profile_id=self.profile.id,
            transport=StubTransport(status_payload),
        )
        self.balance.refresh_from_db()
        self.balance.qty += 1
        self.balance.available_qty += 1
        self.balance.save(update_fields=["qty", "available_qty", "updated_at"])

        pull_profile_statuses(
            profile_id=self.profile.id,
            transport=StubTransport(status_payload),
        )

        waiting.refresh_from_db()
        self.balance.refresh_from_db()
        self.assertEqual(waiting.internal_status, FbsOrder.STATUS_RESERVED)
        self.assertEqual(
            (
                self.balance.qty,
                self.balance.available_qty,
                self.balance.reserved_qty,
            ),
            (2, 0, 2),
        )

    def test_duplicate_terminal_status_releases_legacy_reservation(self):
        pull_profile_orders(
            profile_id=self.profile.id,
            transport=StubTransport(self.orders_payload()),
        )
        order = FbsOrder.objects.get(external_order_id="7700")
        raw_status = {
            "id": 7700,
            "supplierStatus": "complete",
            "wbStatus": "sorted",
        }
        FbsMarketplaceEvent.objects.create(
            profile=self.profile,
            event_type="wb_status",
            external_id="7700",
            payload_hash=_payload_hash(raw_status),
            payload=raw_status,
            status=FbsMarketplaceEvent.STATUS_PROCESSED,
        )
        order.marketplace_status = "complete"
        order.marketplace_substatus = "sorted"
        order.save(
            update_fields=["marketplace_status", "marketplace_substatus", "updated_at"]
        )

        pull_profile_statuses(
            profile_id=self.profile.id,
            transport=StubTransport({"orders": [raw_status]}),
        )

        order.refresh_from_db()
        self.balance.refresh_from_db()
        allocation = FbsOrderStockAllocation.objects.get(order_item__order=order)
        self.assertEqual(order.internal_status, FbsOrder.STATUS_HANDED_OVER)
        self.assertEqual(allocation.status, FbsOrderStockAllocation.STATUS_RELEASED)
        self.assertEqual((self.balance.available_qty, self.balance.reserved_qty), (1, 0))

    def test_wb_cancel_after_physical_pick_queues_one_safe_return(self):
        self.profile.outbox_enabled = True
        self.profile.save(update_fields=["outbox_enabled", "updated_at"])
        pull_profile_orders(
            profile_id=self.profile.id,
            transport=StubTransport(self.orders_payload()),
        )
        order = FbsOrder.objects.get(external_order_id="7700")
        allocation = FbsOrderStockAllocation.objects.get(order_item__order=order)
        workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-CANCEL-RECONCILE",
            name="Cancellation reconciliation desk",
        )
        pick_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            workstation=workstation,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            picking_completed_at=timezone.now(),
        )
        task = FbsPickTask.objects.create(
            batch=pick_batch,
            order=order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
        )
        allocation.pick_task = task
        allocation.status = FbsOrderStockAllocation.STATUS_PICKED
        allocation.qty_picked = 1
        allocation.save(
            update_fields=["pick_task", "status", "qty_picked", "updated_at"]
        )
        self.balance.qty = 0
        self.balance.available_qty = 0
        self.balance.reserved_qty = 0
        self.balance.save(
            update_fields=["qty", "available_qty", "reserved_qty", "updated_at"]
        )
        order.internal_status = FbsOrder.STATUS_READY_FOR_HANDOVER
        order.save(update_fields=["internal_status", "updated_at"])
        handover_batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-GI-CANCEL-RECONCILE",
            status=FbsHandoverBatch.STATUS_OPEN,
            marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
        )
        assignment = FbsHandoverOrderAssignment.objects.create(
            batch=handover_batch,
            order=order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
            confirmed_at=timezone.now(),
        )
        handover_box = FbsHandoverBox.objects.create(
            batch=handover_batch,
            qr_code="FBS-CANCEL-RECONCILE-BOX",
        )
        handover_link = FbsHandoverOrder.objects.create(
            box=handover_box,
            order=order,
        )
        canceled_status = self.status_payload(
            7700,
            status="complete",
            substatus="canceled_by_client",
        )

        pull_profile_statuses(
            profile_id=self.profile.id,
            transport=StubTransport(canceled_status),
        )
        pull_profile_statuses(
            profile_id=self.profile.id,
            transport=StubTransport(canceled_status),
        )

        order.refresh_from_db()
        handover_link.refresh_from_db()
        request = FbsPickRestockRequest.objects.get(order=order)
        self.assertEqual(FbsPickRestockRequest.objects.filter(order=order).count(), 1)
        self.assertEqual(request.batch_id, pick_batch.id)
        self.assertEqual(request.handover_assignment_id, assignment.id)
        self.assertEqual(
            request.status,
            FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
        )
        self.assertIsNone(request.source_tote_id)
        self.assertEqual(order.internal_status, FbsOrder.STATUS_EXCEPTION)
        self.assertEqual(
            handover_link.status,
            FbsHandoverOrder.STATUS_RETURN_PENDING,
        )
        self.assertEqual(
            FbsMarketplaceCommand.objects.filter(
                order=order,
                command_type=WB_READ_HANDOVER_ORDER_IDS,
            ).count(),
            1,
        )

    def test_wb_cancel_after_supply_delivery_does_not_queue_warehouse_return(self):
        self.profile.outbox_enabled = True
        self.profile.save(update_fields=["outbox_enabled", "updated_at"])
        pull_profile_orders(
            profile_id=self.profile.id,
            transport=StubTransport(self.orders_payload()),
        )
        order = FbsOrder.objects.get(external_order_id="7700")
        allocation = FbsOrderStockAllocation.objects.get(order_item__order=order)
        workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-LATE-CANCEL-RECONCILE",
            name="Late cancellation reconciliation desk",
        )
        pick_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            workstation=workstation,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            picking_completed_at=timezone.now(),
        )
        task = FbsPickTask.objects.create(
            batch=pick_batch,
            order=order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
        )
        allocation.pick_task = task
        allocation.status = FbsOrderStockAllocation.STATUS_PICKED
        allocation.qty_picked = 1
        allocation.save(
            update_fields=["pick_task", "status", "qty_picked", "updated_at"]
        )
        self.balance.qty = 0
        self.balance.available_qty = 0
        self.balance.reserved_qty = 0
        self.balance.save(
            update_fields=["qty", "available_qty", "reserved_qty", "updated_at"]
        )
        order.internal_status = FbsOrder.STATUS_READY_FOR_HANDOVER
        order.save(update_fields=["internal_status", "updated_at"])
        handover_batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-GI-LATE-CANCEL-RECONCILE",
            status=FbsHandoverBatch.STATUS_READY,
            marketplace_state=FbsHandoverBatch.MARKETPLACE_COMPLETE,
        )
        FbsHandoverOrderAssignment.objects.create(
            batch=handover_batch,
            order=order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
            confirmed_at=timezone.now(),
        )
        handover_box = FbsHandoverBox.objects.create(
            batch=handover_batch,
            qr_code="FBS-LATE-CANCEL-RECONCILE-BOX",
        )
        handover_link = FbsHandoverOrder.objects.create(
            box=handover_box,
            order=order,
        )
        canceled_status = self.status_payload(
            7700,
            status="complete",
            substatus="canceled_by_client",
        )

        pull_profile_statuses(
            profile_id=self.profile.id,
            transport=StubTransport(canceled_status),
        )

        order.refresh_from_db()
        handover_link.refresh_from_db()
        self.assertFalse(FbsPickRestockRequest.objects.filter(order=order).exists())
        self.assertEqual(order.internal_status, FbsOrder.STATUS_READY_FOR_HANDOVER)
        self.assertEqual(handover_link.status, FbsHandoverOrder.STATUS_ACTIVE)
        self.assertFalse(
            FbsMarketplaceCommand.objects.filter(
                order=order,
                command_type=WB_READ_HANDOVER_ORDER_IDS,
            ).exists()
        )
        actor = get_user_model().objects.create_user(username="late-cancel-operator")
        with self.assertRaisesRegex(FbsPickingError, "уже передана в WB"):
            create_order_pick_restock_request(
                batch_id=pick_batch.id,
                handover_batch_id=handover_batch.id,
                order_id=order.id,
                reason_code=FbsPickRestockRequest.REASON_CLIENT_CANCELED,
                reason="WB подтвердил позднюю отмену после передачи.",
                confirm_seller_cancel=False,
                created_by=actor,
            )

    def test_wb_cancel_from_completed_wave_queues_return_from_check_tote(self):
        self.profile.outbox_enabled = True
        self.profile.save(update_fields=["outbox_enabled", "updated_at"])
        pull_profile_orders(
            profile_id=self.profile.id,
            transport=StubTransport(self.orders_payload()),
        )
        order = FbsOrder.objects.get(external_order_id="7700")
        allocation = FbsOrderStockAllocation.objects.get(order_item__order=order)
        controller = get_user_model().objects.create_user(
            username="cancelled-completed-wave-controller"
        )
        workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-CANCEL-COMPLETED",
            name="Completed wave cancellation desk",
        )
        pick_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            workstation=workstation,
            status=FbsPickBatch.STATUS_DONE,
            planned_qty=1,
            picked_qty=1,
            picking_completed_at=timezone.now(),
            completed_at=timezone.now(),
        )
        task = FbsPickTask.objects.create(
            batch=pick_batch,
            order=order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
        )
        allocation.pick_task = task
        allocation.status = FbsOrderStockAllocation.STATUS_PICKED
        allocation.qty_picked = 1
        allocation.save(
            update_fields=["pick_task", "status", "qty_picked", "updated_at"]
        )
        self.balance.qty = 0
        self.balance.available_qty = 0
        self.balance.reserved_qty = 0
        self.balance.save(
            update_fields=["qty", "available_qty", "reserved_qty", "updated_at"]
        )
        order.internal_status = FbsOrder.STATUS_READY_FOR_HANDOVER
        order.save(update_fields=["internal_status", "updated_at"])
        handover_batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-GI-CANCEL-COMPLETED",
            status=FbsHandoverBatch.STATUS_OPEN,
            marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
        )
        assignment = FbsHandoverOrderAssignment.objects.create(
            batch=handover_batch,
            order=order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
            confirmed_at=timezone.now(),
        )
        unknown_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-CANCEL-COMPLETED-UNKNOWN",
            name="Completed cancellation unknown tote",
        )
        pick_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-CANCEL-COMPLETED-PICK",
            name="Completed cancellation pick tote",
        )
        free_zone = FbsToteZone.objects.create(
            barcode="FBS-ZONE-CANCEL-COMPLETED-FREE",
            name="Completed cancellation free zone",
            kind=FbsToteZone.KIND_FREE,
        )
        session = FbsControllerSession.objects.create(
            workstation=workstation,
            controller=controller,
            unknown_tote=unknown_tote,
            free_zone=free_zone,
        )
        check_tote = FbsControllerCheckTote.objects.create(
            session=session,
            agency=self.agency,
            profile=self.profile,
            handover_batch=handover_batch,
            status=FbsControllerCheckTote.STATUS_OPEN,
            item_qty=1,
            labeled_qty=1,
            opened_by=controller,
        )
        pick_context = FbsControllerPickTote.objects.create(
            session=session,
            check_tote=check_tote,
            pick_batch=pick_batch,
            tote=pick_tote,
            status=FbsControllerPickTote.STATUS_CLOSED,
            planned_qty=1,
            processed_qty=1,
            closed_at=timezone.now(),
        )
        label = FbsOrderLabel.objects.create(
            order=order,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            barcode="WB-CANCEL-COMPLETED-LABEL",
            status=FbsOrderLabel.STATUS_APPLIED,
            applied_by=controller,
            applied_at=timezone.now(),
        )
        tote_order = FbsControllerToteOrder.objects.create(
            check_tote=check_tote,
            pick_tote=pick_context,
            order=order,
            label=label,
            status=FbsControllerToteOrder.STATUS_LABELED,
            units=1,
            label_confirmed_by=controller,
        )
        canceled_status = self.status_payload(
            7700,
            status="complete",
            substatus="canceled_by_client",
        )

        pull_profile_statuses(
            profile_id=self.profile.id,
            transport=StubTransport(canceled_status),
        )
        pull_profile_statuses(
            profile_id=self.profile.id,
            transport=StubTransport(canceled_status),
        )

        order.refresh_from_db()
        tote_order.refresh_from_db()
        request = FbsPickRestockRequest.objects.get(order=order)
        self.assertEqual(FbsPickRestockRequest.objects.filter(order=order).count(), 1)
        self.assertEqual(request.batch_id, pick_batch.id)
        self.assertEqual(request.handover_assignment_id, assignment.id)
        self.assertEqual(
            request.status,
            FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
        )
        self.assertIsNone(request.source_tote_id)
        self.assertEqual(order.internal_status, FbsOrder.STATUS_EXCEPTION)
        self.assertEqual(tote_order.status, FbsControllerToteOrder.STATUS_LABELED)
        self.assertEqual(
            FbsMarketplaceCommand.objects.filter(
                order=order,
                command_type=WB_READ_HANDOVER_ORDER_IDS,
            ).count(),
            1,
        )
