from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from sku.models import Agency, SKU
from sklad.models import WarehouseLocation

from .exceptions import FbsLabelError, FbsPickingError
from .models import (
    FbsBox,
    FbsHandoverOrderAssignment,
    FbsIntegrationProfile,
    FbsOrder,
    FbsOrderItem,
    FbsOrderLabel,
    FbsOrderStockAllocation,
    FbsOrderTraceability,
    FbsPallet,
    FbsPickBatch,
    FbsPickTask,
    FbsPickVerificationProgress,
    FbsStockBalance,
    FbsStorageCell,
)
from .services.labels import (
    ensure_order_label_request,
    prefetch_wb_order_label_request,
)
from .services.marketplace import WB_COMPOSITION_SYNC_INTERVAL
from .services.picking import (
    find_pick_verification_allocation,
    refresh_pick_batch_verification,
    verify_pick_allocation_unit,
)
from .services.totes import _prefetch_wb_labels_for_pick_batch
from .tsd_views import _verification_wave_rows


urlpatterns = []


@override_settings(
    ROOT_URLCONF="fbs.test_picking_speed_optimization",
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
)
class FbsPickingSpeedOptimizationTests(TestCase):
    def setUp(self):
        self.controller = get_user_model().objects.create_user(
            username="speed_controller"
        )
        self.agency = Agency.objects.create(agn_name="Speed optimization client")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SPEED-SKU",
            name="Product with two valid barcodes",
        )
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB speed profile",
            external_account_id="wb-speed-profile",
            external_warehouse_id="1876669",
            is_active=True,
        )
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=91,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="FBS-SPEED-1",
            is_storage=True,
            is_pickable=True,
        )
        cell = FbsStorageCell.objects.create(
            cell_code="FBS-SPEED-1",
            location=location,
        )
        pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-SPEED-PALLET",
            cell=cell,
            status=FbsPallet.STATUS_ACTIVE,
        )
        box = FbsBox.objects.create(
            agency=self.agency,
            pallet=pallet,
            box_code="FBS-SPEED-BOX",
            status=FbsBox.STATUS_ACTIVE,
        )
        self.balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=box,
            sku_ref=self.sku,
            identity_key="9" * 64,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode="4600000000001",
            qty=0,
            available_qty=0,
        )
        self.order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="SPEED-ORDER-1",
            internal_status=FbsOrder.STATUS_PICKED,
            marketplace_status="confirm",
        )
        self.item = FbsOrderItem.objects.create(
            order=self.order,
            external_line_id="SPEED-LINE-1",
            external_sku=self.sku.sku_code,
            sku=self.sku,
            barcode="4600000000002",
            product_name=self.sku.name,
            quantity=2,
            requirements={},
        )
        self.batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=2,
            picked_qty=2,
            verification_assigned_to=self.controller,
            picking_completed_at=timezone.now(),
            verification_started_at=timezone.now(),
        )
        self.task = FbsPickTask.objects.create(
            batch=self.batch,
            order=self.order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=2,
            picked_qty=2,
        )
        self.allocation = FbsOrderStockAllocation.objects.create(
            order_item=self.item,
            balance=self.balance,
            pick_task=self.task,
            qty_reserved=2,
            qty_picked=2,
            status=FbsOrderStockAllocation.STATUS_PICKED,
        )
        FbsOrderTraceability.objects.create(
            allocation=self.allocation,
            qty=2,
            status=FbsOrderTraceability.STATUS_PICKED,
        )

    def test_order_item_barcode_is_accepted_when_balance_barcode_differs(self):
        resolved = find_pick_verification_allocation(
            batch_id=self.batch.id,
            item_scan=self.item.barcode,
            performed_by=self.controller,
        )

        progress = verify_pick_allocation_unit(
            allocation_id=resolved.id,
            item_scan=self.item.barcode,
            performed_by=self.controller,
        )

        self.assertEqual(progress.qty_verified, 1)

    def test_wb_label_prefetch_does_not_block_unfinished_order_scans(self):
        label = prefetch_wb_order_label_request(
            order_id=self.order.id,
            requested_by=self.controller,
        )

        self.assertEqual(label.status, FbsOrderLabel.STATUS_REQUESTED)
        with self.assertRaisesMessage(
            FbsLabelError,
            "Повторная проверка товара еще не завершена.",
        ):
            ensure_order_label_request(
                order_id=self.order.id,
                requested_by=self.controller,
            )

        progress = verify_pick_allocation_unit(
            allocation_id=self.allocation.id,
            item_scan=self.item.barcode,
            performed_by=self.controller,
        )

        self.assertEqual(progress.qty_verified, 1)

    def test_pending_label_blocks_next_order_only_after_order_is_verified(self):
        prefetch_wb_order_label_request(
            order_id=self.order.id,
            requested_by=self.controller,
        )
        FbsPickVerificationProgress.objects.create(
            allocation=self.allocation,
            qty_verified=self.allocation.qty_picked,
        )
        next_order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="SPEED-ORDER-2",
            internal_status=FbsOrder.STATUS_PICKED,
            marketplace_status="confirm",
        )
        next_item = FbsOrderItem.objects.create(
            order=next_order,
            external_line_id="SPEED-LINE-2",
            external_sku=self.sku.sku_code,
            sku=self.sku,
            barcode=self.item.barcode,
            product_name=self.sku.name,
            quantity=1,
            requirements={},
        )
        next_task = FbsPickTask.objects.create(
            batch=self.batch,
            order=next_order,
            status=FbsPickTask.STATUS_PICKED,
            sort_order=2,
            planned_qty=1,
            picked_qty=1,
        )
        next_allocation = FbsOrderStockAllocation.objects.create(
            order_item=next_item,
            balance=self.balance,
            pick_task=next_task,
            qty_reserved=1,
            qty_picked=1,
            status=FbsOrderStockAllocation.STATUS_PICKED,
        )
        FbsOrderTraceability.objects.create(
            allocation=next_allocation,
            qty=1,
            status=FbsOrderTraceability.STATUS_PICKED,
        )
        self.batch.planned_qty = 3
        self.batch.picked_qty = 3
        self.batch.save(update_fields=["planned_qty", "picked_qty", "updated_at"])

        with self.assertRaisesMessage(
            FbsPickingError,
            "Сначала подтвердите marketplace-этикетку уже проверенного заказа.",
        ):
            verify_pick_allocation_unit(
                allocation_id=next_allocation.id,
                item_scan=next_item.barcode,
                performed_by=self.controller,
            )

    def test_pick_batch_prefetch_queues_wb_label_and_handover_assignment(self):
        with mock.patch(
            "fbs.services.labels.prefetch_wb_order_label_request"
        ) as label_prefetch, mock.patch(
            "fbs.services.handover.ensure_wb_order_handover_assignment"
        ) as handover_assignment, mock.patch(
            "fbs.services.marketplace.schedule_label_preparation"
        ) as label_schedule:
            _prefetch_wb_labels_for_pick_batch(
                batch_id=self.batch.id,
                check_tote_id=901,
                workstation_id=902,
                requested_by=self.controller,
            )

        label_prefetch.assert_called_once_with(
            order_id=self.order.id,
            requested_by=self.controller,
        )
        handover_assignment.assert_called_once_with(
            order_id=self.order.id,
            assigned_by=self.controller,
            workstation_id=902,
            pick_batch_id=self.batch.id,
            check_tote_id=901,
        )
        label_schedule.assert_called_once_with(order_id=self.order.id)

    def test_order_label_scan_is_not_treated_as_unknown_product(self):
        FbsOrderLabel.objects.create(
            order=self.order,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            external_label_id="5709462-2474",
            barcode="*DUsazQpm",
            status=FbsOrderLabel.STATUS_APPLIED,
        )

        with self.assertRaises(FbsPickingError) as error:
            find_pick_verification_allocation(
                batch_id=self.batch.id,
                item_scan="DUsaZQpm",
                performed_by=self.controller,
            )

        message = str(error.exception)
        self.assertIn("QR-этикетка WB заказа", message)
        self.assertIn(self.order.external_order_id, message)
        self.assertIn(self.balance.barcode, message)
        self.assertNotIn("отсутствует в текущей таре", message)

    def test_unknown_product_scan_keeps_missing_from_tote_error(self):
        with self.assertRaises(FbsPickingError) as error:
            find_pick_verification_allocation(
                batch_id=self.batch.id,
                item_scan="UNKNOWN-PRODUCT",
                performed_by=self.controller,
            )

        self.assertIn("отсутствует в текущей таре", str(error.exception))

    def test_wrong_barcode_message_lists_scanned_and_expected_values(self):
        with self.assertRaises(FbsPickingError) as error:
            verify_pick_allocation_unit(
                allocation_id=self.allocation.id,
                item_scan="WRONG-460",
                performed_by=self.controller,
            )

        message = str(error.exception)
        self.assertIn("WRONG-460", message)
        self.assertIn(self.balance.barcode, message)
        self.assertIn(self.item.barcode, message)

    def test_empty_verification_wave_is_finalized(self):
        self.task.status = FbsPickTask.STATUS_CANCELED
        self.task.save(update_fields=["status", "updated_at"])

        refreshed = refresh_pick_batch_verification(batch_id=self.batch.id)

        self.assertEqual(refreshed.status, FbsPickBatch.STATUS_DONE)
        self.assertIsNotNone(refreshed.completed_at)

    def test_wb_composition_fallback_is_five_minutes(self):
        self.assertEqual(WB_COMPOSITION_SYNC_INTERVAL.total_seconds(), 300)

    def test_wave_row_names_order_missing_from_handover_until_all_scans_complete(self):
        row = _verification_wave_rows(self.batch)[0]

        self.assertFalse(row.is_product_complete)
        self.assertFalse(row.is_verification_complete)
        self.assertEqual(row.state_label, "Остался товар 2 шт.")
        self.assertIn(self.balance.barcode, row.item_barcodes)

        for _ in range(2):
            verify_pick_allocation_unit(
                allocation_id=self.allocation.id,
                item_scan=self.item.barcode,
                performed_by=self.controller,
            )
        FbsOrderLabel.objects.create(
            order=self.order,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            external_label_id="5709462-2475",
            barcode="ORDER-LABEL-2",
            status=FbsOrderLabel.STATUS_APPLIED,
            applied_by=self.controller,
            applied_at=timezone.now(),
        )
        automatic_assignment = FbsHandoverOrderAssignment.objects.get(
            order=self.order,
        )
        handover = automatic_assignment.batch
        automatic_assignment.delete()

        row = _verification_wave_rows(self.batch)[0]
        self.assertTrue(row.is_product_complete)
        self.assertTrue(row.is_label_complete)
        self.assertFalse(row.is_handover_added)
        self.assertFalse(row.is_verification_complete)
        self.assertEqual(row.state_label, "QR отсканирован · отгрузка создается")

        FbsHandoverOrderAssignment.objects.create(
            batch=handover,
            order=self.order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
            assigned_by=self.controller,
            confirmed_at=timezone.now(),
        )

        row = _verification_wave_rows(self.batch)[0]
        self.assertTrue(row.is_verification_complete)
        self.assertEqual(row.target_handover_batch, handover)
        self.assertEqual(
            row.state_label,
            f"В отгрузке #{handover.id} · короб ожидается",
        )
