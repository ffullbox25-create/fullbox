import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import OperationalError, connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from employees.models import Employee
from sklad.models import WarehouseLocation
from sku.models import Agency, SKU, SKUBarcode
from processing_app.models import ProcessingPrintJob

from .models import (
    FbsBox,
    FbsControllerCheckTote,
    FbsControllerPickTote,
    FbsControllerToteOrder,
    FbsIntegrationProfile,
    FbsMarketplaceMetadataTransfer,
    FbsMarketplaceEvent,
    FbsOrder,
    FbsOrderItem,
    FbsOrderLabel,
    FbsOrderStockAllocation,
    FbsOrderTraceability,
    FbsPallet,
    FbsPickBatch,
    FbsPickException,
    FbsPickScanEvent,
    FbsPickingCart,
    FbsPickTask,
    FbsPickVerificationProgress,
    FbsStockBalance,
    FbsStorageCell,
    FbsToteBinding,
    FbsToteMovement,
    FbsWorkstation,
)
from .exceptions import FbsLabelError, FbsPickingError, FbsScanMismatchError
from .desktop_presence import get_desktop_presences
from .services.labels import (
    confirm_order_label_scan,
    ensure_order_label_request,
    prefetch_wb_order_label_request,
    register_marketplace_label,
)
from .services.picking import (
    _restore_wb_kiz_gs_separators,
    _validate_kiz_scan,
    create_pick_batches,
    finalize_wb_order_markings,
    find_pick_verification_allocation,
    merge_queued_pick_batches,
    release_order_reservation,
    reserve_order_stock,
    resolve_verification_item_scan,
    wb_optional_marking_available,
)
from .services.traceability import prepare_order_marketplace_metadata
from .services.marketplace import _schedule_active_wb_verification_label_requests
from .services.printing import queue_fbs_order_label_print
from .services.problems import (
    pick_missing_group_quantity,
    queue_verification_problem_restock,
    report_pick_exception,
    report_pick_missing_quantity,
)
from .services.totes import (
    confirm_pick_tote_empty,
    split_mixed_profile_verification_batch,
    start_controller_session,
)
from .services.sync import (
    _all_mapped_by_external_id,
    _ingest_statuses_batch,
    _prepare_marketplace_events,
    _profile_for_sync,
)
from .tsd_views import (
    _next_incomplete_verification_allocation,
    _next_verification_label,
    _next_verification_allocation,
    _verification_wave_rows,
)


class FbsWbKizGsSeparatorTests(SimpleTestCase):
    def test_restores_unique_short_serial_layout(self):
        raw = "010466040680002221SER12391ABCD92" + "X" * 44

        restored = _restore_wb_kiz_gs_separators(raw)

        self.assertEqual(restored.replace("\x1d", ""), raw)
        self.assertEqual(restored.count("\x1d"), 2)
        self.assertEqual(restored[24:32], "\x1d91ABCD\x1d")

    def test_restores_unique_twenty_character_serial_layout(self):
        raw = "010466040680002221" + "S" * 20 + "91ABCD92" + "X" * 44

        restored = _restore_wb_kiz_gs_separators(raw)

        self.assertEqual(restored.replace("\x1d", ""), raw)
        self.assertEqual(restored.count("\x1d"), 2)
        self.assertEqual(restored[38:46], "\x1d91ABCD\x1d")

    def test_prefers_exact_stored_physical_code(self):
        raw = "010466040680002221SERIAL93OTHER"
        reference = "010466040680002221SERIAL\x1d93OTHER"

        self.assertEqual(
            _restore_wb_kiz_gs_separators(raw, reference_markings=(reference,)),
            reference,
        )

    def test_does_not_guess_when_layout_is_ambiguous(self):
        raw = "010466040680002221A91AAAA92B91BBBB92" + "X" * 20

        self.assertEqual(_restore_wb_kiz_gs_separators(raw), raw)

    def test_keeps_existing_separators_unchanged(self):
        raw = "010466040680002221SER123\x1d91ABCD\x1d92" + "X" * 44

        self.assertEqual(_restore_wb_kiz_gs_separators(raw), raw)


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_ORDER_PULL_ENABLED=True,
    FBS_STATUS_PULL_ENABLED=True,
)
class FbsSyncBatchingTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Sync batching client")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="Sync batching profile",
            external_warehouse_id="sync-batching-warehouse",
            is_active=True,
            order_pull_enabled=True,
            status_pull_enabled=True,
        )

    def test_loaded_profile_is_reused_without_database_query(self):
        with self.assertNumQueries(0):
            profile = _profile_for_sync(
                self.profile.id,
                stream="orders",
                loaded_profile=self.profile,
            )
        self.assertIs(profile, self.profile)

    def test_marketplace_events_are_prepared_in_bounded_batches(self):
        payloads = [
            (f"ORDER-{index}", {"id": index, "status": "new"})
            for index in range(75)
        ]
        with CaptureQueriesContext(connection) as captured:
            events = _prepare_marketplace_events(
                profile=self.profile,
                event_type="wb_status",
                payloads=payloads,
            )
        self.assertEqual(len(events), 75)
        self.assertEqual(FbsMarketplaceEvent.objects.count(), 75)
        self.assertLessEqual(len(captured), 8)

        with CaptureQueriesContext(connection) as captured_duplicates:
            duplicate_events = _prepare_marketplace_events(
                profile=self.profile,
                event_type="wb_status",
                payloads=payloads,
            )
        self.assertEqual(set(duplicate_events), set(events))
        self.assertEqual(FbsMarketplaceEvent.objects.count(), 75)
        self.assertLessEqual(len(captured_duplicates), 2)

    def test_unmapped_sku_state_is_loaded_for_whole_batch(self):
        orders = [
            FbsOrder(
                profile=self.profile,
                external_order_id=f"ORDER-{index}",
            )
            for index in range(40)
        ]
        FbsOrder.objects.bulk_create(orders)
        FbsOrderItem.objects.bulk_create(
            [
                FbsOrderItem(
                    order=order,
                    external_line_id=f"LINE-{order.id}",
                    external_sku=f"SKU-{order.id}",
                    product_name=f"Product {order.id}",
                    quantity=1,
                )
                for order in orders
            ]
        )
        with CaptureQueriesContext(connection) as captured:
            mapped_by_external_id = _all_mapped_by_external_id(
                profile=self.profile,
                external_order_ids=[
                    *(order.external_order_id for order in orders),
                    "ORDER-NOT-IMPORTED-YET",
                ],
            )
        self.assertEqual(len(mapped_by_external_id), 41)
        self.assertFalse(
            any(
                mapped_by_external_id[order.external_order_id]
                for order in orders
            )
        )
        self.assertTrue(mapped_by_external_id["ORDER-NOT-IMPORTED-YET"])
        self.assertEqual(len(captured), 2)

    def test_status_batch_reuses_loaded_profile_for_locked_orders(self):
        orders = [
            FbsOrder(
                profile=self.profile,
                external_order_id=f"STATUS-{index}",
                marketplace_status="new",
            )
            for index in range(40)
        ]
        FbsOrder.objects.bulk_create(orders)
        statuses = [
            {
                "external_order_id": order.external_order_id,
                "marketplace_status": "new",
                "marketplace_substatus": "waiting",
                "raw_payload": {
                    "id": order.external_order_id,
                    "supplierStatus": "new",
                    "wbStatus": "waiting",
                },
            }
            for order in orders
        ]

        with CaptureQueriesContext(connection) as captured:
            states = _ingest_statuses_batch(self.profile, statuses)

        profile_selects = [
            query["sql"]
            for query in captured.captured_queries
            if 'FROM "fbs_integration_profile"' in query["sql"]
        ]
        self.assertEqual(len(states), 40)
        self.assertEqual(profile_selects, [])


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
)
class FbsMissingOrderWaveReleaseTests(TestCase):
    def setUp(self):
        self.picker = get_user_model().objects.create_user(username="task5a-picker")
        self.agency = Agency.objects.create(agn_name="Task 5a client")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            name="Task 5a profile",
            external_warehouse_id="task5a-warehouse",
        )
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=95,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="FBS-TASK5A-1",
            is_storage=True,
            is_pickable=True,
        )
        cell = FbsStorageCell.objects.create(
            cell_code="FBS-TASK5A-1",
            location=location,
        )
        pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-TASK5A-PALLET",
            cell=cell,
            status=FbsPallet.STATUS_ACTIVE,
        )
        box = FbsBox.objects.create(
            agency=self.agency,
            pallet=pallet,
            box_code="FBS-TASK5A-BOX",
            status=FbsBox.STATUS_ACTIVE,
        )
        self.source_box = box
        self.good_sku = self._sku("TASK5A-GOOD", "4600000000501")
        self.missing_sku = self._sku("TASK5A-MISSING", "4600000000502")
        good_balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=box,
            sku_ref=self.good_sku,
            identity_key="task5a-good",
            sku_code=self.good_sku.sku_code,
            name=self.good_sku.name,
            barcode="4600000000501",
            qty=0,
            available_qty=0,
            reserved_qty=0,
        )
        self.missing_balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=box,
            sku_ref=self.missing_sku,
            identity_key="task5a-missing",
            sku_code=self.missing_sku.sku_code,
            name=self.missing_sku.name,
            barcode="4600000000502",
            qty=3,
            available_qty=2,
            reserved_qty=1,
        )
        alternate_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=95,
            section_no=1,
            tier_no=1,
            cell_no=2,
            location_code="FBS-TASK5A-2",
            is_storage=True,
            is_pickable=True,
        )
        alternate_cell = FbsStorageCell.objects.create(
            cell_code="FBS-TASK5A-2",
            location=alternate_location,
        )
        alternate_pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-TASK5A-PALLET-2",
            cell=alternate_cell,
            status=FbsPallet.STATUS_ACTIVE,
        )
        self.alternate_box = FbsBox.objects.create(
            agency=self.agency,
            pallet=alternate_pallet,
            box_code="FBS-TASK5A-BOX-2",
            status=FbsBox.STATUS_ACTIVE,
        )
        self.alternate_balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.alternate_box,
            sku_ref=self.missing_sku,
            identity_key="task5a-missing-alternate",
            sku_code=self.missing_sku.sku_code,
            name=self.missing_sku.name,
            barcode="4600000000502",
            qty=10,
            available_qty=10,
            reserved_qty=0,
        )
        self.good_order, good_item = self._order(
            external_id="TASK5A-GOOD-ORDER",
            sku=self.good_sku,
            barcode="4600000000501",
            status=FbsOrder.STATUS_PICKED,
        )
        self.missing_order, missing_item = self._order(
            external_id="TASK5A-MISSING-ORDER",
            sku=self.missing_sku,
            barcode="4600000000502",
            status=FbsOrder.STATUS_PICKING,
        )
        self.batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_IN_PROGRESS,
            planned_qty=2,
            picked_qty=1,
            assigned_to=self.picker,
        )
        self.good_task = FbsPickTask.objects.create(
            batch=self.batch,
            order=self.good_order,
            assigned_to=self.picker,
            status=FbsPickTask.STATUS_PICKED,
            sort_order=1,
            planned_qty=1,
            picked_qty=1,
        )
        self.missing_task = FbsPickTask.objects.create(
            batch=self.batch,
            order=self.missing_order,
            assigned_to=self.picker,
            status=FbsPickTask.STATUS_IN_PROGRESS,
            sort_order=2,
            planned_qty=1,
            picked_qty=0,
        )
        good_allocation = FbsOrderStockAllocation.objects.create(
            order_item=good_item,
            balance=good_balance,
            pick_task=self.good_task,
            qty_reserved=1,
            qty_picked=1,
            status=FbsOrderStockAllocation.STATUS_PICKED,
        )
        self.missing_allocation = FbsOrderStockAllocation.objects.create(
            order_item=missing_item,
            balance=self.missing_balance,
            pick_task=self.missing_task,
            qty_reserved=1,
            qty_picked=0,
            status=FbsOrderStockAllocation.STATUS_PICKING,
        )
        FbsOrderTraceability.objects.create(
            allocation=good_allocation,
            qty=1,
            status=FbsOrderTraceability.STATUS_PICKED,
        )
        FbsOrderTraceability.objects.create(
            allocation=self.missing_allocation,
            qty=1,
            status=FbsOrderTraceability.STATUS_RESERVED,
        )

    def _sku(self, sku_code, barcode):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code=sku_code,
            name=sku_code,
        )
        SKUBarcode.objects.create(sku=sku, value=barcode, is_primary=True)
        return sku

    def _order(self, *, external_id, sku, barcode, status):
        order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id=external_id,
            internal_status=status,
        )
        item = FbsOrderItem.objects.create(
            order=order,
            external_line_id=f"{external_id}-LINE",
            external_sku=sku.sku_code,
            sku=sku,
            barcode=barcode,
            product_name=sku.name,
            quantity=1,
            requirements={},
        )
        return order, item

    def test_active_wb_wave_background_scheduler_requests_missing_label(self):
        self.profile.marketplace = FbsIntegrationProfile.MARKETPLACE_WB
        self.profile.is_active = True
        self.profile.outbox_enabled = True
        self.profile.save(
            update_fields=[
                "marketplace",
                "is_active",
                "outbox_enabled",
                "updated_at",
            ]
        )
        self.batch.status = FbsPickBatch.STATUS_VERIFICATION
        self.batch.verification_assigned_to = self.picker
        self.batch.picking_completed_at = timezone.now()
        self.batch.save(
            update_fields=[
                "status",
                "verification_assigned_to",
                "picking_completed_at",
                "updated_at",
            ]
        )

        _schedule_active_wb_verification_label_requests(limit=50)

        label = FbsOrderLabel.objects.get(order=self.good_order)
        self.assertEqual(label.status, FbsOrderLabel.STATUS_REQUESTED)
        self.assertEqual(label.requested_by, self.picker)
        self.assertFalse(
            FbsOrderLabel.objects.filter(order=self.missing_order).exists()
        )

    def _report_missing(self):
        return report_pick_exception(
            allocation_id=self.missing_allocation.id,
            exception_type=FbsPickException.TYPE_NOT_FOUND,
            reason="Нет на месте",
            reported_by=self.picker,
        )

    def _make_missing_allocation_partially_picked(self):
        self.missing_allocation.order_item.quantity = 3
        self.missing_allocation.order_item.save(update_fields=["quantity", "updated_at"])
        self.missing_allocation.qty_reserved = 3
        self.missing_allocation.qty_picked = 2
        self.missing_allocation.save(
            update_fields=["qty_reserved", "qty_picked", "updated_at"]
        )
        trace = self.missing_allocation.traceability
        trace.qty = 3
        trace.save(update_fields=["qty", "updated_at"])
        self.missing_task.planned_qty = 3
        self.missing_task.picked_qty = 2
        self.missing_task.save(
            update_fields=["planned_qty", "picked_qty", "updated_at"]
        )
        self.batch.planned_qty = 4
        self.batch.picked_qty = 3
        self.batch.save(update_fields=["planned_qty", "picked_qty", "updated_at"])

    def _add_same_missing_orders(self, count):
        rows = []
        for index in range(count):
            order, item = self._order(
                external_id=f"TASK5A-GROUP-{index}",
                sku=self.missing_sku,
                barcode="4600000000502",
                status=FbsOrder.STATUS_PICKING,
            )
            task = FbsPickTask.objects.create(
                batch=self.batch,
                order=order,
                assigned_to=self.picker,
                status=FbsPickTask.STATUS_IN_PROGRESS,
                sort_order=3 + index,
                planned_qty=1,
                picked_qty=0,
            )
            allocation = FbsOrderStockAllocation.objects.create(
                order_item=item,
                balance=self.missing_balance,
                pick_task=task,
                qty_reserved=1,
                qty_picked=0,
                status=FbsOrderStockAllocation.STATUS_PICKING,
            )
            FbsOrderTraceability.objects.create(
                allocation=allocation,
                qty=1,
                status=FbsOrderTraceability.STATUS_RESERVED,
            )
            rows.append((order, task, allocation))
        self.missing_balance.qty = int(self.missing_balance.qty or 0) + count
        self.missing_balance.reserved_qty = (
            int(self.missing_balance.reserved_qty or 0) + count
        )
        self.missing_balance.save(
            update_fields=["qty", "reserved_qty", "updated_at"]
        )
        self.batch.planned_qty = int(self.batch.planned_qty or 0) + count
        self.batch.save(update_fields=["planned_qty", "updated_at"])
        return rows

    def _prepare_controller_problem_session(self):
        workstation = FbsWorkstation.objects.create(
            barcode="TASK5A-CONTROLLER-WS",
            name="Task 5a controller workstation",
        )
        unknown_tote = FbsPickingCart.objects.create(
            barcode="TASK5A-UNKNOWN-TOTE",
            name="Task 5a unknown tote",
        )
        problem_tote = FbsPickingCart.objects.create(
            barcode="TASK5A-PROBLEM-TOTE",
            name="Task 5a problem tote",
        )
        canceled_tote = FbsPickingCart.objects.create(
            barcode="TASK5A-CANCELED-TOTE",
            name="Task 5a canceled tote",
        )
        pick_tote = FbsPickingCart.objects.create(
            barcode="TASK5A-PICK-TOTE",
            name="Task 5a pick tote",
        )
        session = start_controller_session(
            workstation_id=workstation.id,
            controller=self.picker,
            unknown_tote_scan=unknown_tote.barcode,
            problem_tote_scan=problem_tote.barcode,
            canceled_tote_scan=canceled_tote.barcode,
        )
        controller_check_tote = FbsControllerCheckTote.objects.create(
            session=session,
            tote=None,
            agency=self.agency,
            profile=self.profile,
            opened_by=self.picker,
        )
        self.batch.workstation = workstation
        self.batch.cart = pick_tote
        self.batch.verification_assigned_to = self.picker
        self.batch.picking_completed_at = timezone.now()
        self.batch.save(
            update_fields=[
                "workstation",
                "cart",
                "verification_assigned_to",
                "picking_completed_at",
                "updated_at",
            ]
        )
        FbsControllerPickTote.objects.create(
            session=session,
            check_tote=controller_check_tote,
            pick_batch=self.batch,
            tote=pick_tote,
            planned_qty=int(self.batch.planned_qty or 0),
        )
        return session, problem_tote

    def _mixed_profile_verification_wave(self):
        wb_profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="Task 5a second profile",
            external_warehouse_id="task5a-second-warehouse",
        )
        workstation = FbsWorkstation.objects.create(
            barcode="TASK5A-SPLIT-WS",
            name="Task 5a split workstation",
        )
        cart = FbsPickingCart.objects.create(
            barcode="TASK5A-SPLIT-CART",
            name="Task 5a split cart",
        )
        batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=2,
            picked_qty=2,
            assigned_to=self.picker,
            workstation=workstation,
            cart=cart,
            created_by=self.picker,
            started_at=timezone.now(),
            picking_completed_at=timezone.now(),
        )
        rows = []
        for index, (profile, task_status) in enumerate(
            (
                (self.profile, FbsPickTask.STATUS_PICKED),
                (wb_profile, FbsPickTask.STATUS_PICKED),
                (self.profile, FbsPickTask.STATUS_CANCELED),
            ),
            start=1,
        ):
            order = FbsOrder.objects.create(
                profile=profile,
                external_order_id=f"TASK5A-SPLIT-{index}",
                internal_status=(
                    FbsOrder.STATUS_PICKED
                    if task_status == FbsPickTask.STATUS_PICKED
                    else FbsOrder.STATUS_CANCELLED
                ),
            )
            item = FbsOrderItem.objects.create(
                order=order,
                external_line_id=f"TASK5A-SPLIT-{index}-LINE",
                external_sku=self.good_sku.sku_code,
                sku=self.good_sku,
                barcode="4600000000501",
                product_name=self.good_sku.name,
                quantity=1,
                requirements={},
            )
            task = FbsPickTask.objects.create(
                batch=batch,
                order=order,
                assigned_to=self.picker,
                status=task_status,
                sort_order=index,
                planned_qty=1,
                picked_qty=(1 if task_status == FbsPickTask.STATUS_PICKED else 0),
            )
            if task_status == FbsPickTask.STATUS_PICKED:
                allocation = FbsOrderStockAllocation.objects.create(
                    order_item=item,
                    balance=self.good_task.allocations.get().balance,
                    pick_task=task,
                    qty_reserved=1,
                    qty_picked=1,
                    status=FbsOrderStockAllocation.STATUS_PICKED,
                )
                FbsOrderTraceability.objects.create(
                    allocation=allocation,
                    qty=1,
                    status=FbsOrderTraceability.STATUS_PICKED,
                )
            rows.append((order, task))
        return batch, wb_profile, workstation, cart, rows

    def test_create_pick_batches_separates_profiles_of_same_client(self):
        wb_profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="Task 5a queue WB profile",
            external_warehouse_id="task5a-queue-wb",
        )
        orders = []
        for index, profile in enumerate((self.profile, wb_profile), start=1):
            order = FbsOrder.objects.create(
                profile=profile,
                external_order_id=f"TASK5A-QUEUE-PROFILE-{index}",
                internal_status=FbsOrder.STATUS_RESERVED,
            )
            item = FbsOrderItem.objects.create(
                order=order,
                external_line_id=f"TASK5A-QUEUE-PROFILE-{index}-LINE",
                external_sku=self.good_sku.sku_code,
                sku=self.good_sku,
                barcode="4600000000501",
                product_name=self.good_sku.name,
                quantity=1,
                requirements={},
            )
            allocation = FbsOrderStockAllocation.objects.create(
                order_item=item,
                balance=self.good_task.allocations.get().balance,
                qty_reserved=1,
                status=FbsOrderStockAllocation.STATUS_RESERVED,
            )
            FbsOrderTraceability.objects.create(
                allocation=allocation,
                qty=1,
                status=FbsOrderTraceability.STATUS_RESERVED,
            )
            orders.append(order)

        batches = create_pick_batches(
            order_ids=[order.id for order in orders],
            created_by=self.picker,
        )

        self.assertEqual(len(batches), 2)
        self.assertEqual(
            {
                tuple(batch.tasks.values_list("order__profile_id", flat=True))
                for batch in batches
            },
            {(self.profile.id,), (wb_profile.id,)},
        )

    def test_merge_queued_pick_batches_rejects_different_profiles(self):
        wb_profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="Task 5a merge WB profile",
            external_warehouse_id="task5a-merge-wb",
        )
        batches = []
        for index, profile in enumerate((self.profile, wb_profile), start=1):
            order = FbsOrder.objects.create(
                profile=profile,
                external_order_id=f"TASK5A-MERGE-PROFILE-{index}",
                internal_status=FbsOrder.STATUS_QUEUED_FOR_PICK,
            )
            batch = FbsPickBatch.objects.create(
                agency=self.agency,
                planned_qty=1,
            )
            FbsPickTask.objects.create(
                batch=batch,
                order=order,
                status=FbsPickTask.STATUS_QUEUED,
                sort_order=1,
                planned_qty=1,
            )
            batches.append(batch)

        with self.assertRaisesMessage(
            FbsPickingError,
            "Объединять можно только волны одного кабинета клиента.",
        ):
            merge_queued_pick_batches(
                primary_batch_id=batches[0].id,
                merged_batch_ids=[batches[1].id],
            )

    def test_split_mixed_verification_wave_preserves_stock_and_reservations(self):
        batch, wb_profile, _workstation, cart, _rows = (
            self._mixed_profile_verification_wave()
        )
        balance = self.good_task.allocations.get().balance
        stock_before = (balance.qty, balance.available_qty, balance.reserved_qty)
        allocation_ids = set(
            FbsOrderStockAllocation.objects.filter(pick_task__batch=batch)
            .values_list("id", flat=True)
        )

        batches = split_mixed_profile_verification_batch(
            batch_id=batch.id,
            performed_by=self.picker,
        )

        balance.refresh_from_db()
        self.assertEqual(len(batches), 2)
        self.assertEqual(
            [split_batch.planned_qty for split_batch in batches],
            [1, 1],
        )
        self.assertEqual(
            {
                frozenset(
                    split_batch.tasks.values_list(
                        "order__profile_id", flat=True
                    )
                )
                for split_batch in batches
            },
            {frozenset((self.profile.id,)), frozenset((wb_profile.id,))},
        )
        self.assertEqual(batches[0].cart_id, cart.id)
        self.assertIsNone(batches[1].cart_id)
        self.assertEqual(
            set(
                FbsOrderStockAllocation.objects.filter(
                    pick_task__batch__in=batches
                ).values_list("id", flat=True)
            ),
            allocation_ids,
        )
        self.assertEqual(
            (balance.qty, balance.available_qty, balance.reserved_qty),
            stock_before,
        )
        self.assertEqual(
            FbsToteMovement.objects.filter(
                details__operation="mixed_profile_wave_split"
            ).count(),
            2,
        )

    def test_split_wave_passes_same_cart_to_next_profile_after_first_part(self):
        batch, _wb_profile, workstation, cart, _rows = (
            self._mixed_profile_verification_wave()
        )
        first_batch, continuation = split_mixed_profile_verification_batch(
            batch_id=batch.id,
            performed_by=self.picker,
        )
        unknown_tote = FbsPickingCart.objects.create(
            barcode="TASK5A-SPLIT-UNKNOWN",
            name="Task 5a split unknown",
        )
        session = start_controller_session(
            workstation_id=workstation.id,
            controller=self.picker,
            unknown_tote_scan=unknown_tote.barcode,
        )
        check_tote = FbsControllerCheckTote.objects.create(
            session=session,
            tote=None,
            agency=self.agency,
            profile=self.profile,
            opened_by=self.picker,
        )
        pick_context = FbsControllerPickTote.objects.create(
            session=session,
            check_tote=check_tote,
            pick_batch=first_batch,
            tote=cart,
            planned_qty=first_batch.planned_qty,
        )
        for task in first_batch.tasks.filter(status=FbsPickTask.STATUS_PICKED):
            label = FbsOrderLabel.objects.create(
                order=task.order,
                marketplace=task.order.profile.marketplace,
                external_label_id=f"TASK5A-SPLIT-LABEL-{task.id}",
                barcode=f"TASK5A-SPLIT-LABEL-QR-{task.id}",
                status=FbsOrderLabel.STATUS_APPLIED,
            )
            FbsControllerToteOrder.objects.create(
                check_tote=check_tote,
                pick_tote=pick_context,
                order=task.order,
                label=label,
                units=1,
                label_confirmed_by=self.picker,
            )

        closed = confirm_pick_tote_empty(
            pick_batch_id=first_batch.id,
            performed_by=self.picker,
        )

        first_batch.refresh_from_db()
        continuation.refresh_from_db()
        binding = FbsToteBinding.objects.get(tote=cart)
        self.assertEqual(closed.status, FbsControllerPickTote.STATUS_CLOSED)
        self.assertIsNone(closed.empty_confirmed_at)
        self.assertIsNotNone(first_batch.cart_released_at)
        self.assertEqual(continuation.cart_id, cart.id)
        self.assertEqual(binding.state, FbsToteBinding.STATE_AT_CONTROL)
        self.assertEqual(binding.pick_batch_id, continuation.id)

    def test_missing_order_leaves_wave_and_other_order_remains_picked(self):
        self._report_missing()

        self.batch.refresh_from_db()
        self.good_task.refresh_from_db()
        self.missing_task.refresh_from_db()
        self.missing_order.refresh_from_db()
        self.assertEqual(self.batch.status, FbsPickBatch.STATUS_VERIFICATION)
        self.assertEqual(self.good_task.status, FbsPickTask.STATUS_PICKED)
        self.assertEqual(self.missing_task.status, FbsPickTask.STATUS_CANCELED)
        self.assertEqual(
            self.missing_order.internal_status,
            FbsOrder.STATUS_AWAITING_STOCK,
        )

    def test_missing_source_becomes_unavailable_instead_of_reentering_reserve(self):
        self._report_missing()

        self.missing_balance.refresh_from_db()
        self.assertEqual(self.missing_balance.available_qty, 0)
        self.assertEqual(self.missing_balance.reserved_qty, 0)

    def test_picker_can_remove_seven_same_sku_units_with_one_confirmation(self):
        extra_rows = self._add_same_missing_orders(6)

        self.assertEqual(
            pick_missing_group_quantity(
                allocation_id=self.missing_allocation.id,
                assigned_to=self.picker,
            ),
            7,
        )
        result = report_pick_missing_quantity(
            allocation_id=self.missing_allocation.id,
            missing_qty=7,
            reason="Короб пуст",
            reported_by=self.picker,
        )

        self.batch.refresh_from_db()
        self.missing_balance.refresh_from_db()
        self.missing_task.refresh_from_db()
        self.assertEqual(result.released_qty, 7)
        self.assertEqual(result.affected_order_count, 7)
        self.assertTrue(result.source_quarantined)
        self.assertEqual(len(result.issues), 7)
        self.assertEqual(self.missing_balance.available_qty, 0)
        self.assertEqual(self.missing_balance.reserved_qty, 0)
        self.assertEqual(self.missing_task.status, FbsPickTask.STATUS_CANCELED)
        self.assertTrue(
            all(
                task.status == FbsPickTask.STATUS_CANCELED
                for _, task, _ in [
                    (order, FbsPickTask.objects.get(pk=task.pk), allocation)
                    for order, task, allocation in extra_rows
                ]
            )
        )
        self.assertEqual(self.batch.planned_qty, 1)
        self.assertEqual(self.batch.picked_qty, 1)
        self.assertEqual(self.batch.status, FbsPickBatch.STATUS_VERIFICATION)

    def test_picker_quantity_releases_only_requested_group_units(self):
        extra_rows = self._add_same_missing_orders(6)

        result = report_pick_missing_quantity(
            allocation_id=self.missing_allocation.id,
            missing_qty=3,
            reason="Не найдено три",
            reported_by=self.picker,
        )

        self.batch.refresh_from_db()
        self.missing_balance.refresh_from_db()
        self.assertEqual(result.released_qty, 3)
        self.assertEqual(result.affected_order_count, 3)
        self.assertFalse(result.source_quarantined)
        self.assertEqual(len(result.issues), 3)
        self.assertEqual(self.missing_balance.available_qty, 2)
        self.assertEqual(self.missing_balance.reserved_qty, 4)
        self.assertEqual(self.batch.planned_qty, 5)
        self.assertEqual(self.batch.picked_qty, 1)
        self.assertEqual(self.batch.status, FbsPickBatch.STATUS_IN_PROGRESS)
        remaining_task_ids = [task.id for _, task, _ in extra_rows[2:]]
        self.assertEqual(
            FbsPickTask.objects.filter(
                id__in=remaining_task_ids,
                status=FbsPickTask.STATUS_IN_PROGRESS,
            ).count(),
            4,
        )

    def test_picker_quantity_is_validated_against_current_source_group(self):
        self._add_same_missing_orders(2)

        with self.assertRaisesMessage(FbsPickingError, "от 1 до 3"):
            report_pick_missing_quantity(
                allocation_id=self.missing_allocation.id,
                missing_qty=4,
                reason="",
                reported_by=self.picker,
            )

    def test_picker_quantity_can_split_unpicked_part_of_one_allocation(self):
        self.missing_allocation.order_item.quantity = 4
        self.missing_allocation.order_item.save(
            update_fields=["quantity", "updated_at"]
        )
        self.missing_allocation.qty_reserved = 4
        self.missing_allocation.qty_picked = 2
        self.missing_allocation.save(
            update_fields=["qty_reserved", "qty_picked", "updated_at"]
        )
        trace = self.missing_allocation.traceability
        trace.qty = 4
        trace.save(update_fields=["qty", "updated_at"])
        self.missing_balance.available_qty = 1
        self.missing_balance.reserved_qty = 2
        self.missing_balance.save(
            update_fields=["available_qty", "reserved_qty", "updated_at"]
        )
        self.missing_task.planned_qty = 4
        self.missing_task.picked_qty = 2
        self.missing_task.save(
            update_fields=["planned_qty", "picked_qty", "updated_at"]
        )
        self.batch.planned_qty = 5
        self.batch.picked_qty = 3
        self.batch.save(update_fields=["planned_qty", "picked_qty", "updated_at"])

        result = report_pick_missing_quantity(
            allocation_id=self.missing_allocation.id,
            missing_qty=1,
            reason="Одна единица отсутствует",
            reported_by=self.picker,
        )

        self.missing_allocation.refresh_from_db()
        self.missing_balance.refresh_from_db()
        self.missing_task.refresh_from_db()
        self.batch.refresh_from_db()
        shortage = FbsOrderStockAllocation.objects.get(
            pick_task=self.missing_task,
            status=FbsOrderStockAllocation.STATUS_RELEASED,
        )
        self.assertEqual(result.released_qty, 1)
        self.assertFalse(result.source_quarantined)
        self.assertEqual(shortage.qty_reserved, 1)
        self.assertEqual(self.missing_allocation.qty_reserved, 3)
        self.assertEqual(self.missing_allocation.qty_picked, 2)
        self.assertEqual(self.missing_allocation.status, FbsOrderStockAllocation.STATUS_PICKING)
        self.assertEqual(self.missing_balance.available_qty, 1)
        self.assertEqual(self.missing_balance.reserved_qty, 1)
        self.assertEqual(self.missing_task.planned_qty, 3)
        self.assertEqual(self.batch.planned_qty, 4)

    def test_picker_quantity_can_split_never_started_allocation(self):
        self.missing_allocation.order_item.quantity = 4
        self.missing_allocation.order_item.save(
            update_fields=["quantity", "updated_at"]
        )
        self.missing_allocation.qty_reserved = 4
        self.missing_allocation.save(update_fields=["qty_reserved", "updated_at"])
        trace = self.missing_allocation.traceability
        trace.qty = 4
        trace.save(update_fields=["qty", "updated_at"])
        self.missing_balance.available_qty = 1
        self.missing_balance.reserved_qty = 4
        self.missing_balance.qty = 5
        self.missing_balance.save(
            update_fields=["qty", "available_qty", "reserved_qty", "updated_at"]
        )
        self.missing_task.planned_qty = 4
        self.missing_task.save(update_fields=["planned_qty", "updated_at"])
        self.batch.planned_qty = 5
        self.batch.save(update_fields=["planned_qty", "updated_at"])

        result = report_pick_missing_quantity(
            allocation_id=self.missing_allocation.id,
            missing_qty=1,
            reason="Одной единицы нет",
            reported_by=self.picker,
        )

        self.missing_allocation.refresh_from_db()
        self.missing_task.refresh_from_db()
        self.missing_order.refresh_from_db()
        self.missing_balance.refresh_from_db()
        self.batch.refresh_from_db()
        shortage = FbsOrderStockAllocation.objects.get(
            pick_task=self.missing_task,
            status=FbsOrderStockAllocation.STATUS_RELEASED,
        )
        self.assertEqual(result.released_qty, 1)
        self.assertEqual(shortage.qty_reserved, 1)
        self.assertEqual(self.missing_allocation.qty_reserved, 3)
        self.assertEqual(self.missing_allocation.qty_picked, 0)
        self.assertEqual(self.missing_task.planned_qty, 3)
        self.assertEqual(self.missing_task.status, FbsPickTask.STATUS_IN_PROGRESS)
        self.assertEqual(self.missing_order.internal_status, FbsOrder.STATUS_PICKING)
        self.assertEqual(self.missing_balance.available_qty, 1)
        self.assertEqual(self.missing_balance.reserved_qty, 3)
        self.assertEqual(self.batch.planned_qty, 4)
        self.assertEqual(self.batch.status, FbsPickBatch.STATUS_IN_PROGRESS)

    def test_planned_quantity_matches_picked_after_order_release(self):
        self._report_missing()

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.planned_qty, 1)
        self.assertEqual(self.batch.picked_qty, 1)

    def test_repeated_report_does_not_release_reservation_twice(self):
        first_issue = self._report_missing()
        self.missing_balance.refresh_from_db()
        available_after_first = self.missing_balance.available_qty

        second_issue = self._report_missing()

        self.missing_balance.refresh_from_db()
        self.assertEqual(second_issue.id, first_issue.id)
        self.assertEqual(self.missing_balance.available_qty, available_after_first)
        self.assertEqual(
            FbsPickException.objects.filter(allocation=self.missing_allocation).count(),
            1,
        )

    def test_released_order_can_be_reserved_for_next_wave(self):
        self._report_missing()

        result = reserve_order_stock(
            order_id=self.missing_order.id,
            reserved_by=self.picker,
        )

        self.assertTrue(result.reserved)
        self.assertEqual(result.status, FbsOrder.STATUS_RESERVED)
        self.assertEqual(result.reserved_qty, 1)
        self.assertEqual(result.allocations[0].balance_id, self.alternate_balance.id)

    def test_partial_allocation_releases_only_shortage_and_reaches_verification(self):
        self._make_missing_allocation_partially_picked()
        available_before = self.missing_balance.available_qty

        issue = self._report_missing()

        self.missing_allocation.refresh_from_db()
        self.missing_balance.refresh_from_db()
        self.missing_task.refresh_from_db()
        self.missing_order.refresh_from_db()
        self.batch.refresh_from_db()
        shortage = FbsOrderStockAllocation.objects.get(
            pick_task=self.missing_task,
            status=FbsOrderStockAllocation.STATUS_RELEASED,
        )
        self.assertEqual(issue.allocation_id, self.missing_allocation.id)
        self.assertEqual(
            self.missing_allocation.status,
            FbsOrderStockAllocation.STATUS_PICKED,
        )
        self.assertEqual(self.missing_allocation.qty_reserved, 2)
        self.assertEqual(self.missing_allocation.qty_picked, 2)
        self.assertEqual(shortage.qty_reserved, 1)
        self.assertEqual(shortage.qty_picked, 0)
        self.assertEqual(
            shortage.traceability.status,
            FbsOrderTraceability.STATUS_RELEASED,
        )
        self.assertEqual(self.missing_balance.available_qty, 0)
        self.assertEqual(self.missing_balance.reserved_qty, 0)
        self.assertEqual(self.missing_task.planned_qty, 2)
        self.assertEqual(self.missing_task.picked_qty, 2)
        self.assertEqual(self.missing_task.status, FbsPickTask.STATUS_PICKED)
        self.assertEqual(self.missing_order.internal_status, FbsOrder.STATUS_PICKED)
        self.assertEqual(self.batch.planned_qty, 3)
        self.assertEqual(self.batch.picked_qty, 3)
        self.assertEqual(self.batch.status, FbsPickBatch.STATUS_VERIFICATION)

    def test_partial_shortage_keeps_wave_running_when_another_task_is_open(self):
        self._make_missing_allocation_partially_picked()
        pending_order, pending_item = self._order(
            external_id="TASK5A-PENDING-ORDER",
            sku=self.good_sku,
            barcode="4600000000501",
            status=FbsOrder.STATUS_PICKING,
        )
        pending_task = FbsPickTask.objects.create(
            batch=self.batch,
            order=pending_order,
            assigned_to=self.picker,
            status=FbsPickTask.STATUS_IN_PROGRESS,
            sort_order=3,
            planned_qty=1,
            picked_qty=0,
        )
        pending_allocation = FbsOrderStockAllocation.objects.create(
            order_item=pending_item,
            balance=self.good_task.allocations.get().balance,
            pick_task=pending_task,
            qty_reserved=1,
            qty_picked=0,
            status=FbsOrderStockAllocation.STATUS_PICKING,
        )
        FbsOrderTraceability.objects.create(
            allocation=pending_allocation,
            qty=1,
            status=FbsOrderTraceability.STATUS_RESERVED,
        )
        self.batch.planned_qty = 5
        self.batch.save(update_fields=["planned_qty", "updated_at"])

        self._report_missing()

        self.batch.refresh_from_db()
        pending_task.refresh_from_db()
        self.assertEqual(self.batch.status, FbsPickBatch.STATUS_IN_PROGRESS)
        self.assertEqual(self.batch.planned_qty, 4)
        self.assertEqual(self.batch.picked_qty, 3)
        self.assertEqual(pending_task.status, FbsPickTask.STATUS_IN_PROGRESS)

    def test_multiline_partial_order_does_not_corrupt_wave_totals(self):
        picked_item = FbsOrderItem.objects.create(
            order=self.missing_order,
            external_line_id="TASK5A-MISSING-ORDER-PICKED",
            external_sku=self.good_sku.sku_code,
            sku=self.good_sku,
            barcode="4600000000501",
            product_name=self.good_sku.name,
            quantity=1,
            requirements={},
        )
        picked_allocation = FbsOrderStockAllocation.objects.create(
            order_item=picked_item,
            balance=self.good_task.allocations.get().balance,
            pick_task=self.missing_task,
            qty_reserved=1,
            qty_picked=1,
            status=FbsOrderStockAllocation.STATUS_PICKED,
        )
        FbsOrderTraceability.objects.create(
            allocation=picked_allocation,
            qty=1,
            status=FbsOrderTraceability.STATUS_PICKED,
        )
        self.missing_task.planned_qty = 2
        self.missing_task.picked_qty = 1
        self.missing_task.save(
            update_fields=["planned_qty", "picked_qty", "updated_at"]
        )
        self.batch.planned_qty = 3
        self.batch.picked_qty = 2
        self.batch.save(update_fields=["planned_qty", "picked_qty", "updated_at"])

        self._report_missing()

        self.missing_task.refresh_from_db()
        self.batch.refresh_from_db()
        self.assertEqual(self.missing_task.status, FbsPickTask.STATUS_PICKED)
        self.assertEqual(self.missing_task.planned_qty, 1)
        self.assertEqual(self.missing_task.picked_qty, 1)
        self.assertEqual(self.batch.planned_qty, 2)
        self.assertEqual(self.batch.picked_qty, 2)
        self.assertEqual(self.batch.status, FbsPickBatch.STATUS_VERIFICATION)

    def test_partial_shortage_repeat_is_idempotent(self):
        self._make_missing_allocation_partially_picked()

        first_issue = self._report_missing()
        self.missing_balance.refresh_from_db()
        available_after_first = self.missing_balance.available_qty
        second_issue = self._report_missing()

        self.missing_balance.refresh_from_db()
        self.assertEqual(second_issue.id, first_issue.id)
        self.assertEqual(self.missing_balance.available_qty, available_after_first)
        self.assertEqual(
            FbsPickException.objects.filter(allocation=self.missing_allocation).count(),
            1,
        )
        self.assertEqual(
            FbsOrderStockAllocation.objects.filter(
                pick_task=self.missing_task,
                status=FbsOrderStockAllocation.STATUS_RELEASED,
            ).count(),
            1,
        )

    def test_full_release_subtracts_canceled_task_picked_quantity(self):
        picked_item = FbsOrderItem.objects.create(
            order=self.missing_order,
            external_line_id="TASK5A-CANCELED-PICKED",
            external_sku=self.good_sku.sku_code,
            sku=self.good_sku,
            barcode="4600000000501",
            product_name=self.good_sku.name,
            quantity=1,
            requirements={},
        )
        picked_allocation = FbsOrderStockAllocation.objects.create(
            order_item=picked_item,
            balance=self.good_task.allocations.get().balance,
            pick_task=self.missing_task,
            qty_reserved=1,
            qty_picked=1,
            status=FbsOrderStockAllocation.STATUS_PICKED,
        )
        FbsOrderTraceability.objects.create(
            allocation=picked_allocation,
            qty=1,
            status=FbsOrderTraceability.STATUS_PICKED,
        )
        self.missing_task.planned_qty = 2
        self.missing_task.picked_qty = 1
        self.missing_task.save(
            update_fields=["planned_qty", "picked_qty", "updated_at"]
        )
        self.batch.planned_qty = 3
        self.batch.picked_qty = 2
        self.batch.save(update_fields=["planned_qty", "picked_qty", "updated_at"])

        release_order_reservation(
            order_id=self.missing_order.id,
            released_by=self.picker,
            cancel_order=False,
        )

        self.missing_task.refresh_from_db()
        self.batch.refresh_from_db()
        self.assertEqual(self.missing_task.status, FbsPickTask.STATUS_CANCELED)
        self.assertEqual(self.batch.planned_qty, 1)
        self.assertEqual(self.batch.picked_qty, 1)
        self.assertEqual(self.batch.status, FbsPickBatch.STATUS_VERIFICATION)

    def test_wave_with_picked_and_exception_tasks_is_not_canceled(self):
        exception_order, _ = self._order(
            external_id="TASK5A-EXCEPTION-ORDER",
            sku=self.good_sku,
            barcode="4600000000501",
            status=FbsOrder.STATUS_EXCEPTION,
        )
        FbsPickTask.objects.create(
            batch=self.batch,
            order=exception_order,
            assigned_to=self.picker,
            status=FbsPickTask.STATUS_EXCEPTION,
            sort_order=3,
            planned_qty=1,
            picked_qty=0,
        )
        self.batch.planned_qty = 3
        self.batch.save(update_fields=["planned_qty", "updated_at"])

        release_order_reservation(
            order_id=self.missing_order.id,
            released_by=self.picker,
            cancel_order=False,
        )

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, FbsPickBatch.STATUS_IN_PROGRESS)

    def test_incomplete_order_cannot_be_confirmed_ready_for_handover(self):
        self._make_missing_allocation_partially_picked()
        self._report_missing()
        controller = get_user_model().objects.create_superuser(
            username="task5a-controller",
            email="task5a-controller@example.test",
            password="test-only-password",
        )
        self.batch.verification_assigned_to = controller
        self.batch.picking_completed_at = timezone.now()
        self.batch.save(
            update_fields=[
                "verification_assigned_to",
                "picking_completed_at",
                "updated_at",
            ]
        )
        label = FbsOrderLabel.objects.create(
            order=self.missing_order,
            marketplace=self.profile.marketplace,
            external_label_id="TASK5A-INCOMPLETE-LABEL",
            barcode="TASK5A-INCOMPLETE-QR",
            status=FbsOrderLabel.STATUS_READY,
        )

        with self.assertRaisesMessage(
            FbsLabelError,
            "В заказе не хватает позиций. Отправьте его в проблемные — "
            "обычная передача запрещена.",
        ):
            confirm_order_label_scan(
                label_id=label.id,
                label_scan=label.barcode,
                performed_by=controller,
            )

        label.refresh_from_db()
        self.missing_order.refresh_from_db()
        self.assertEqual(label.status, FbsOrderLabel.STATUS_READY)
        self.assertEqual(self.missing_order.internal_status, FbsOrder.STATUS_PICKED)

    def test_completed_repick_is_not_blocked_by_historical_shortage(self):
        historical_issue = self._report_missing()
        self.batch.status = FbsPickBatch.STATUS_DONE
        self.batch.completed_at = timezone.now()
        self.batch.save(update_fields=["status", "completed_at", "updated_at"])
        controller = get_user_model().objects.create_superuser(
            username="task5a-repick-controller",
            email="task5a-repick-controller@example.test",
            password="test-only-password",
        )
        repick_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            assigned_to=self.picker,
            verification_assigned_to=controller,
            picking_completed_at=timezone.now(),
        )
        repick_task = FbsPickTask.objects.create(
            batch=repick_batch,
            order=self.missing_order,
            assigned_to=self.picker,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
            claimed_at=timezone.now(),
            completed_at=timezone.now(),
        )
        repick_allocation = FbsOrderStockAllocation.objects.create(
            order_item=self.missing_allocation.order_item,
            balance=self.alternate_balance,
            pick_task=repick_task,
            qty_reserved=1,
            qty_picked=1,
            status=FbsOrderStockAllocation.STATUS_PICKED,
        )
        FbsPickVerificationProgress.objects.create(
            allocation=repick_allocation,
            qty_verified=1,
        )
        self.missing_order.internal_status = FbsOrder.STATUS_PICKED
        self.missing_order.problem_reason = ""
        self.missing_order.save(
            update_fields=["internal_status", "problem_reason", "updated_at"]
        )
        label = FbsOrderLabel.objects.create(
            order=self.missing_order,
            marketplace=self.profile.marketplace,
            external_label_id="TASK5A-REPICK-LABEL",
            barcode="TASK5A-REPICK-QR",
            status=FbsOrderLabel.STATUS_READY,
        )

        confirmed = confirm_order_label_scan(
            label_id=label.id,
            label_scan=label.barcode,
            performed_by=controller,
        )

        confirmed.refresh_from_db()
        historical_issue.refresh_from_db()
        self.assertEqual(confirmed.status, FbsOrderLabel.STATUS_APPLIED)
        self.assertEqual(historical_issue.status, FbsPickException.STATUS_OPEN)

    def test_incomplete_order_remains_visible_after_all_picked_units_verified(self):
        self._make_missing_allocation_partially_picked()
        self._report_missing()
        for allocation in FbsOrderStockAllocation.objects.filter(
            pick_task__batch=self.batch,
            status=FbsOrderStockAllocation.STATUS_PICKED,
        ):
            FbsPickVerificationProgress.objects.create(
                allocation=allocation,
                qty_verified=int(allocation.qty_picked or 0),
            )

        self.assertIsNone(_next_verification_allocation(self.batch))
        fallback = _next_incomplete_verification_allocation(self.batch)
        rows = _verification_wave_rows(self.batch)
        incomplete_row = next(
            row for row in rows if row.order.id == self.missing_order.id
        )
        self.assertEqual(fallback.id, self.missing_allocation.id)
        self.assertTrue(incomplete_row.is_incomplete)
        self.assertEqual(incomplete_row.state, "problem")
        self.assertIn("Недокомплект", incomplete_row.problem_reason)
        self.assertIn("4600000000502", incomplete_row.problem_reason)
        self.assertIn("1 шт.", incomplete_row.problem_reason)

    def test_incomplete_order_uses_existing_issue_when_sent_to_problem_tote(self):
        self._make_missing_allocation_partially_picked()
        original_issue = self._report_missing()
        _, problem_tote = self._prepare_controller_problem_session()

        request = queue_verification_problem_restock(
            allocation_id=self.missing_allocation.id,
            exception_type=FbsPickException.TYPE_NOT_FOUND,
            reason="Недокомплект подтвержден контролером",
            service_tote_scan=problem_tote.barcode,
            reported_by=self.picker,
        )

        original_issue.refresh_from_db()
        self.missing_task.refresh_from_db()
        self.missing_order.refresh_from_db()
        self.batch.refresh_from_db()
        self.assertEqual(request.source_tote_id, problem_tote.id)
        self.assertEqual(request.planned_qty, 2)
        self.assertEqual(request.lines.count(), 1)
        self.assertEqual(request.lines.get().allocation_id, self.missing_allocation.id)
        self.assertEqual(
            FbsPickException.objects.filter(task=self.missing_task).count(),
            1,
        )
        self.assertIn("Контролер подтвердил сканом", original_issue.reason)
        self.assertEqual(self.missing_task.status, FbsPickTask.STATUS_EXCEPTION)
        self.assertEqual(self.missing_order.internal_status, FbsOrder.STATUS_EXCEPTION)
        self.assertEqual(self.batch.status, FbsPickBatch.STATUS_VERIFICATION)
        remaining_rows = _verification_wave_rows(self.batch)
        self.assertEqual(
            [row.order.id for row in remaining_rows],
            [self.good_order.id],
        )

    def _prepare_marking_controller_scan(self):
        Employee.objects.create(
            user=self.picker,
            full_name="Контролер КИЗ",
            role="fbs_controller",
        )
        good_allocation = self.good_task.allocations.select_related(
            "balance",
            "order_item",
        ).get()
        good_allocation.order_item.requirements = {"required_meta": ["sgtin"]}
        good_allocation.order_item.save(update_fields=["requirements", "updated_at"])
        self.batch.status = FbsPickBatch.STATUS_VERIFICATION
        self.batch.verification_assigned_to = self.picker
        self.batch.picking_completed_at = timezone.now()
        self.batch.save(
            update_fields=[
                "status",
                "verification_assigned_to",
                "picking_completed_at",
                "updated_at",
            ]
        )
        return good_allocation

    def test_kiz_in_product_field_resolves_exact_gtin(self):
        allocation = self._prepare_marking_controller_scan()
        marking_scan = "010460000000050121ABC123"

        found = find_pick_verification_allocation(
            batch_id=self.batch.id,
            item_scan=marking_scan,
            performed_by=self.picker,
        )
        product_barcode, inferred_marking = resolve_verification_item_scan(
            found,
            marking_scan,
        )

        self.assertEqual(found.id, allocation.id)
        self.assertEqual(product_barcode, allocation.balance.barcode)
        self.assertEqual(inferred_marking, marking_scan)

    def test_kiz_in_product_field_is_accepted_as_one_combined_scan(self):
        allocation = self._prepare_marking_controller_scan()
        marking_scan = "010460000000050121ABC123"
        self.client.force_login(self.picker)

        response = self.client.post(
            reverse("fbs:tsd_pick_verification", kwargs={"batch_id": self.batch.id}),
            {
                "action": "select_item",
                "item_scan": marking_scan,
            },
        )

        self.assertEqual(response.status_code, 302)
        progress = FbsPickVerificationProgress.objects.get(allocation=allocation)
        allocation.traceability.refresh_from_db()
        self.assertEqual(progress.qty_verified, 1)
        self.assertEqual(allocation.traceability.marking_code, marking_scan)

    def test_barcode_in_kiz_field_is_retryable_and_has_no_problem_tote_modal(self):
        allocation = self._prepare_marking_controller_scan()
        self.client.force_login(self.picker)

        response = self.client.post(
            reverse("fbs:tsd_pick_verification", kwargs={"batch_id": self.batch.id}),
            {
                "action": "verify",
                "allocation_id": allocation.id,
                "item_scan": allocation.balance.barcode,
                "marking_scan": allocation.balance.barcode,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertContains(
            response,
            "Отсканирован штрихкод товара вместо КИЗа",
            status_code=400,
        )
        self.assertNotContains(response, "Сканирование остановлено", status_code=400)
        self.assertNotContains(response, "Поместите весь заказ", status_code=400)
        self.assertFalse(FbsPickVerificationProgress.objects.filter(
            allocation=allocation,
        ).exists())

    def test_product_barcode_is_a_retryable_kiz_mismatch(self):
        with self.assertRaises(FbsScanMismatchError):
            _validate_kiz_scan(
                "4600000000501",
                product_barcodes=("4600000000501",),
            )

    def test_wb_kiz_is_accepted_without_client_stock_or_gtin_matching(self):
        self.profile.marketplace = FbsIntegrationProfile.MARKETPLACE_WB
        self.profile.save(update_fields=["marketplace", "updated_at"])
        allocation = self._prepare_marking_controller_scan()
        scanned_marking = "010461046911083521WRONGPRODUCT"
        self.client.force_login(self.picker)

        response = self.client.post(
            reverse("fbs:tsd_pick_verification", kwargs={"batch_id": self.batch.id}),
            {
                "action": "verify",
                "allocation_id": allocation.id,
                "item_scan": allocation.balance.barcode,
                "marking_scan": scanned_marking,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            FbsStockBalance.objects.filter(marking_code=scanned_marking).exists()
        )
        self.assertTrue(
            FbsPickVerificationProgress.objects.filter(allocation=allocation).exists()
        )
        self.assertTrue(
            FbsPickScanEvent.objects.filter(
                allocation=allocation,
                stage=FbsPickScanEvent.STAGE_VERIFY_MARKING,
                result=FbsPickScanEvent.RESULT_SUCCESS,
                scan_value=scanned_marking,
            ).exists()
        )

    def _prepare_optional_wb_marking_controller_scan(self):
        self.profile.marketplace = FbsIntegrationProfile.MARKETPLACE_WB
        self.profile.marking_push_enabled = True
        self.profile.save(
            update_fields=["marketplace", "marking_push_enabled", "updated_at"]
        )
        allocation = self._prepare_marking_controller_scan()
        allocation.order_item.requirements = {
            "optional_meta": ["sgtin"],
            "required_meta": [],
            "wb_meta": {
                "sgtin": {
                    "decision": "optional",
                    "available": True,
                    "has_value": False,
                }
            },
        }
        allocation.order_item.save(update_fields=["requirements", "updated_at"])
        allocation.order_item.sku.honest_sign = False
        allocation.order_item.sku.save(update_fields=["honest_sign"])
        allocation.balance.marking_code = ""
        allocation.balance.save(update_fields=["marking_code", "updated_at"])
        allocation.traceability.marking_code = ""
        allocation.traceability.save(update_fields=["marking_code", "updated_at"])
        return allocation

    def test_wb_optional_sgtin_offers_kiz_scan_without_sku_checkbox(self):
        allocation = self._prepare_optional_wb_marking_controller_scan()
        self.client.force_login(self.picker)

        response = self.client.post(
            reverse("fbs:tsd_pick_verification", kwargs={"batch_id": self.batch.id}),
            {
                "action": "select_item",
                "item_scan": allocation.balance.barcode,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(wb_optional_marking_available(allocation.order_item))
        self.assertContains(response, "Если Data Matrix есть на товаре")
        self.assertContains(response, "Подтвердить без ЧЗ")
        self.assertFalse(
            FbsPickVerificationProgress.objects.filter(allocation=allocation).exists()
        )

    def test_wb_optional_kiz_is_accepted_without_gtin_matching_and_finalized(self):
        allocation = self._prepare_optional_wb_marking_controller_scan()
        scanned_marking = "010461046911083521OPTIONALSCAN"
        stock_qty = allocation.balance.qty
        stock_reserved_qty = allocation.balance.reserved_qty
        self.client.force_login(self.picker)

        response = self.client.post(
            reverse("fbs:tsd_pick_verification", kwargs={"batch_id": self.batch.id}),
            {
                "action": "verify",
                "allocation_id": allocation.id,
                "item_scan": allocation.balance.barcode,
                "marking_scan": scanned_marking,
            },
        )

        self.assertEqual(response.status_code, 302)
        allocation.balance.refresh_from_db()
        allocation.traceability.refresh_from_db()
        self.assertEqual(allocation.balance.qty, stock_qty)
        self.assertEqual(allocation.balance.reserved_qty, stock_reserved_qty)
        self.assertEqual(allocation.traceability.marking_code, "")
        self.assertTrue(
            FbsPickScanEvent.objects.filter(
                allocation=allocation,
                stage=FbsPickScanEvent.STAGE_VERIFY_MARKING,
                result=FbsPickScanEvent.RESULT_SUCCESS,
                scan_value=scanned_marking,
            ).exists()
        )

        finalized = finalize_wb_order_markings(
            order_id=self.good_order.id,
            performed_by=self.picker,
        )
        prepare_order_marketplace_metadata(
            order_id=self.good_order.id,
            pick_task_id=self.good_task.id,
        )

        allocation.traceability.refresh_from_db()
        self.assertEqual(finalized, (scanned_marking,))
        self.assertEqual(allocation.traceability.marking_code, scanned_marking)
        self.assertTrue(
            FbsMarketplaceMetadataTransfer.objects.filter(
                traceability=allocation.traceability,
                metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
                value=scanned_marking,
                is_required=False,
            ).exists()
        )

    def test_wb_optional_kiz_can_be_skipped_for_unmarked_product(self):
        allocation = self._prepare_optional_wb_marking_controller_scan()
        self.client.force_login(self.picker)

        response = self.client.post(
            reverse("fbs:tsd_pick_verification", kwargs={"batch_id": self.batch.id}),
            {
                "action": "verify",
                "allocation_id": allocation.id,
                "item_scan": allocation.balance.barcode,
                "marking_scan": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            FbsPickVerificationProgress.objects.filter(
                allocation=allocation,
                qty_verified=1,
            ).exists()
        )
        self.assertFalse(
            FbsPickScanEvent.objects.filter(
                allocation=allocation,
                stage=FbsPickScanEvent.STAGE_VERIFY_MARKING,
            ).exists()
        )

    def test_duplicate_wb_kiz_is_retryable_without_problem_tote_modal(self):
        self.profile.marketplace = FbsIntegrationProfile.MARKETPLACE_WB
        self.profile.save(update_fields=["marketplace", "updated_at"])
        allocation = self._prepare_marking_controller_scan()
        scanned_marking = "010461046911083521DUPLICATE"
        FbsOrderTraceability.objects.filter(
            allocation=self.missing_allocation
        ).update(
            marking_code=scanned_marking,
            qty=1,
            status=FbsOrderTraceability.STATUS_RESERVED,
        )
        self.client.force_login(self.picker)

        response = self.client.post(
            reverse("fbs:tsd_pick_verification", kwargs={"batch_id": self.batch.id}),
            {
                "action": "verify",
                "allocation_id": allocation.id,
                "item_scan": allocation.balance.barcode,
                "marking_scan": scanned_marking,
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertNotContains(response, "Сканирование остановлено", status_code=409)
        self.assertNotContains(response, "Поместите весь заказ", status_code=409)
        self.assertFalse(
            FbsPickVerificationProgress.objects.filter(allocation=allocation).exists()
        )

    def test_wb_prefetched_label_is_ready_but_cannot_be_applied_before_verification(self):
        self.profile.marketplace = FbsIntegrationProfile.MARKETPLACE_WB
        self.profile.save(update_fields=["marketplace", "updated_at"])
        self._prepare_marking_controller_scan()

        prefetched = prefetch_wb_order_label_request(
            order_id=self.good_order.id,
            requested_by=self.picker,
        )
        label = register_marketplace_label(
            order_id=self.good_order.id,
            external_label_id="PREFETCHED-WB-LABEL",
            barcode="PREFETCHED-WB-BARCODE",
            label_format=FbsOrderLabel.FORMAT_PNG,
            file_url="https://example.test/prefetched-wb-label.png",
        )

        self.assertEqual(label.id, prefetched.id)
        self.assertEqual(label.status, FbsOrderLabel.STATUS_READY)
        with self.assertRaisesMessage(
            FbsLabelError,
            "Повторная проверка товара еще не завершена.",
        ):
            confirm_order_label_scan(
                label_id=label.id,
                label_scan=label.barcode,
                performed_by=self.picker,
            )
        label.refresh_from_db()
        self.good_order.refresh_from_db()
        self.assertEqual(label.status, FbsOrderLabel.STATUS_READY)
        self.assertEqual(self.good_order.internal_status, FbsOrder.STATUS_PICKED)

    def test_applied_label_is_reused_and_duplicate_request_is_canceled(self):
        self.profile.marketplace = FbsIntegrationProfile.MARKETPLACE_WB
        self.profile.save(update_fields=["marketplace", "updated_at"])
        allocation = self._prepare_marking_controller_scan()
        FbsPickVerificationProgress.objects.create(
            allocation=allocation,
            qty_verified=int(allocation.qty_picked or 0),
            verified_by=self.picker,
        )
        applied = FbsOrderLabel.objects.create(
            order=self.good_order,
            marketplace=self.profile.marketplace,
            external_label_id="ALREADY-APPLIED-LABEL",
            barcode="ALREADY-APPLIED-BARCODE",
            status=FbsOrderLabel.STATUS_APPLIED,
            applied_by=self.picker,
            applied_at=timezone.now(),
        )
        duplicate = FbsOrderLabel.objects.create(
            order=self.good_order,
            marketplace=self.profile.marketplace,
            status=FbsOrderLabel.STATUS_REQUESTED,
        )

        selected = ensure_order_label_request(
            order_id=self.good_order.id,
            requested_by=self.picker,
        )

        duplicate.refresh_from_db()
        self.assertEqual(selected.id, applied.id)
        self.assertEqual(duplicate.status, FbsOrderLabel.STATUS_CANCELED)
        self.assertIn("уже была подтверждена", duplicate.error)

    def test_applied_label_must_be_scanned_into_current_rewave_tote(self):
        self.profile.marketplace = FbsIntegrationProfile.MARKETPLACE_WB
        self.profile.save(update_fields=["marketplace", "updated_at"])
        allocation = self._prepare_marking_controller_scan()
        FbsPickVerificationProgress.objects.create(
            allocation=allocation,
            qty_verified=int(allocation.qty_picked or 0),
            verified_by=self.picker,
        )
        allocation.traceability.marking_code = "010460000000050121REUSED"
        allocation.traceability.save(
            update_fields=["marking_code", "updated_at"],
        )
        applied = FbsOrderLabel.objects.create(
            order=self.good_order,
            marketplace=self.profile.marketplace,
            external_label_id="VISIBLE-APPLIED-LABEL",
            barcode="VISIBLE-APPLIED-BARCODE",
            status=FbsOrderLabel.STATUS_APPLIED,
            applied_by=self.picker,
            applied_at=timezone.now(),
        )
        FbsOrderLabel.objects.create(
            order=self.good_order,
            marketplace=self.profile.marketplace,
            status=FbsOrderLabel.STATUS_CANCELED,
            error="Duplicate request",
        )
        self._prepare_controller_problem_session()

        self.assertEqual(_next_verification_label(self.batch).id, applied.id)
        row = next(
            item
            for item in _verification_wave_rows(self.batch)
            if item.order.id == self.good_order.id
        )
        self.assertEqual(row.label.id, applied.id)
        self.assertFalse(row.is_label_complete)
        self.assertEqual(row.state_label, "QR готов, требуется скан")

        self.client.force_login(self.picker)
        verification_url = reverse(
            "fbs:tsd_pick_verification",
            kwargs={"batch_id": self.batch.id},
        )
        response = self.client.get(verification_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="label-scan"')
        self.assertContains(response, "Скан этикетки заказа")
        self.assertNotContains(response, "Этикетка не готова: Применена")

        response = self.client.post(
            verification_url,
            {
                "action": "confirm_label",
                "label_scan": applied.barcode,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.good_order.refresh_from_db()
        self.assertEqual(
            self.good_order.internal_status,
            FbsOrder.STATUS_READY_FOR_HANDOVER,
        )
        current_tote_order = FbsControllerToteOrder.objects.get(
            pick_tote__pick_batch=self.batch,
            order=self.good_order,
        )
        self.assertEqual(current_tote_order.label_id, applied.id)
        self.assertEqual(current_tote_order.label_confirmed_by_id, self.picker.id)

        self.assertIsNone(_next_verification_label(self.batch))
        completed_row = next(
            item
            for item in _verification_wave_rows(self.batch)
            if item.order.id == self.good_order.id
        )
        self.assertTrue(completed_row.is_label_complete)

    def test_rewave_automatic_print_is_deduplicated_inside_verification_scope(self):
        Employee.objects.create(
            user=self.picker,
            full_name="Контролер повторной волны",
            role="fbs_controller",
        )
        workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-REWAVE-PRINT",
            name="Rewave print workstation",
        )
        self.batch.workstation = workstation
        self.batch.verification_started_at = timezone.now()
        self.batch.save(
            update_fields=["workstation", "verification_started_at", "updated_at"]
        )
        label = FbsOrderLabel.objects.create(
            order=self.good_order,
            marketplace=self.profile.marketplace,
            external_label_id="REWAVE-PRINT-LABEL",
            barcode="REWAVE-PRINT-BARCODE",
            status=FbsOrderLabel.STATUS_APPLIED,
            file="order-labels/test/rewave-print.png",
            applied_by=self.picker,
            applied_at=timezone.now(),
        )
        print_scope = (
            f"verification:{self.batch.id}:"
            f"{self.batch.verification_started_at.isoformat()}"
        )

        with patch(
            "fbs.services.printing._read_order_label_image",
            return_value=(b"\x89PNG\r\n\x1a\ntest", 58, 40),
        ):
            first_job = queue_fbs_order_label_print(
                label_id=label.id,
                requested_by=self.picker,
                force=True,
                print_scope=print_scope,
                desktop_agent_id="desktop:rewave-test",
                desktop_printer_name="Xprinter XP-365B",
            )
            first_job.status = ProcessingPrintJob.STATUS_PRINTED
            first_job.save(update_fields=["status", "updated_at"])
            repeated_automatic_job = queue_fbs_order_label_print(
                label_id=label.id,
                requested_by=self.picker,
                force=True,
                print_scope=print_scope,
                desktop_agent_id="desktop:rewave-test",
                desktop_printer_name="Xprinter XP-365B",
            )

        self.assertEqual(repeated_automatic_job.id, first_job.id)
        self.assertEqual(
            ProcessingPrintJob.objects.filter(
                card_id__startswith=f"fbs:order-label:{label.id}:"
            ).count(),
            1,
        )

    def test_wb_reservation_keeps_quantity_without_binding_kiz(self):
        self.profile.marketplace = FbsIntegrationProfile.MARKETPLACE_WB
        self.profile.save(update_fields=["marketplace", "updated_at"])
        marking_code = "010460000000050121RESERVEONLY"
        FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.source_box,
            sku_ref=self.good_sku,
            identity_key="task5a-good-reserve-only",
            sku_code=self.good_sku.sku_code,
            name=self.good_sku.name,
            barcode="4600000000501",
            marking_code=marking_code,
            qty=1,
            available_qty=1,
            reserved_qty=0,
        )
        order, _ = self._order(
            external_id="TASK5A-RESERVE-ONLY",
            sku=self.good_sku,
            barcode="4600000000501",
            status=FbsOrder.STATUS_RECEIVED,
        )

        result = reserve_order_stock(order_id=order.id, reserved_by=self.picker)

        self.assertTrue(result.reserved)
        allocation = result.allocations[0]
        allocation.refresh_from_db()
        allocation.traceability.refresh_from_db()
        self.assertEqual(allocation.balance.marking_code, marking_code)
        self.assertEqual(allocation.balance.reserved_qty, 1)
        self.assertEqual(allocation.traceability.marking_code, "")

    def test_wb_kiz_binds_after_label_without_mutating_stock_reservations(self):
        expected_marking = "010460000000050121EXPECTED"
        scanned_marking = "010460000000050121SCANNED"
        self.profile.marketplace = FbsIntegrationProfile.MARKETPLACE_WB
        self.profile.save(update_fields=["marketplace", "updated_at"])
        self._prepare_controller_problem_session()
        allocation = self._prepare_marking_controller_scan()
        allocation.balance.marking_code = expected_marking
        allocation.balance.save(update_fields=["marking_code", "updated_at"])
        technical_balance_id = allocation.balance_id
        allocation.traceability.marking_code = ""
        allocation.traceability.save(update_fields=["marking_code", "updated_at"])
        scanned_balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.source_box,
            sku_ref=self.good_sku,
            identity_key="task5a-good-queued-marking",
            sku_code=self.good_sku.sku_code,
            name=self.good_sku.name,
            barcode=allocation.balance.barcode,
            marking_code=scanned_marking,
            qty=1,
            available_qty=0,
            reserved_qty=1,
        )
        future_order, future_item = self._order(
            external_id="TASK5A-FUTURE-ORDER",
            sku=self.good_sku,
            barcode=allocation.balance.barcode,
            status=FbsOrder.STATUS_QUEUED_FOR_PICK,
        )
        future_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_QUEUED,
            planned_qty=1,
            picked_qty=0,
        )
        future_task = FbsPickTask.objects.create(
            batch=future_batch,
            order=future_order,
            status=FbsPickTask.STATUS_QUEUED,
            planned_qty=1,
            picked_qty=0,
        )
        future_allocation = FbsOrderStockAllocation.objects.create(
            order_item=future_item,
            balance=scanned_balance,
            pick_task=future_task,
            qty_reserved=1,
            qty_picked=0,
            status=FbsOrderStockAllocation.STATUS_RESERVED,
        )
        FbsOrderTraceability.objects.create(
            allocation=future_allocation,
            marking_code="",
            qty=1,
            status=FbsOrderTraceability.STATUS_RESERVED,
        )
        self.client.force_login(self.picker)

        response = self.client.post(
            reverse("fbs:tsd_pick_verification", kwargs={"batch_id": self.batch.id}),
            {
                "action": "verify",
                "allocation_id": allocation.id,
                "item_scan": allocation.balance.barcode,
                "marking_scan": scanned_marking,
            },
        )

        self.assertEqual(response.status_code, 302)
        allocation.refresh_from_db()
        allocation.traceability.refresh_from_db()
        self.assertEqual(allocation.traceability.marking_code, "")
        self.assertTrue(
            FbsPickScanEvent.objects.filter(
                allocation=allocation,
                stage=FbsPickScanEvent.STAGE_VERIFY_MARKING,
                result=FbsPickScanEvent.RESULT_SUCCESS,
                scan_value=scanned_marking,
            ).exists()
        )
        future_allocation.refresh_from_db()
        scanned_balance.refresh_from_db()
        self.assertEqual(future_allocation.balance_id, scanned_balance.id)
        self.assertEqual(future_allocation.status, FbsOrderStockAllocation.STATUS_RESERVED)
        self.assertEqual(scanned_balance.reserved_qty, 1)

        label = FbsOrderLabel.objects.get(order=self.good_order)
        label.external_label_id = "TASK5A-FINAL-LABEL"
        label.barcode = "TASK5A-FINAL-ORDER-BARCODE"
        label.status = FbsOrderLabel.STATUS_READY
        label.save(
            update_fields=[
                "external_label_id",
                "barcode",
                "status",
                "updated_at",
            ]
        )

        with self.assertRaisesMessage(
            FbsLabelError,
            "Скан не совпадает с этикеткой этого заказа.",
        ):
            confirm_order_label_scan(
                label_id=label.id,
                label_scan="WRONG-ORDER-BARCODE",
                performed_by=self.picker,
            )
        allocation.traceability.refresh_from_db()
        self.assertEqual(allocation.traceability.marking_code, "")

        confirm_order_label_scan(
            label_id=label.id,
            label_scan=label.barcode,
            performed_by=self.picker,
        )

        allocation.refresh_from_db()
        allocation.traceability.refresh_from_db()
        future_allocation.refresh_from_db()
        future_allocation.traceability.refresh_from_db()
        label.refresh_from_db()
        self.good_order.refresh_from_db()
        self.assertEqual(allocation.traceability.marking_code, scanned_marking)
        self.assertEqual(future_allocation.traceability.marking_code, "")
        self.assertEqual(allocation.balance_id, technical_balance_id)
        self.assertEqual(future_allocation.balance_id, scanned_balance.id)
        scanned_balance.refresh_from_db()
        self.assertEqual(scanned_balance.reserved_qty, 1)
        self.assertEqual(label.status, FbsOrderLabel.STATUS_APPLIED)
        self.assertEqual(self.good_order.internal_status, FbsOrder.STATUS_READY_FOR_HANDOVER)
        self.assertTrue(
            FbsMarketplaceMetadataTransfer.objects.filter(
                traceability=allocation.traceability,
                metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
                value=scanned_marking,
            ).exists()
        )

    def test_recent_error_shows_order_reason_and_clear_problem_action(self):
        allocation = self._prepare_marking_controller_scan()
        FbsPickScanEvent.objects.create(
            batch=self.batch,
            task=allocation.pick_task,
            allocation=allocation,
            stage=FbsPickScanEvent.STAGE_VERIFY_MARKING,
            result=FbsPickScanEvent.RESULT_ERROR,
            scan_value="TEST-WRONG-KIZ",
            expected_value="TEST-EXPECTED-KIZ",
            message="Тестовая причина ошибки ЧЗ.",
            created_by=self.picker,
        )
        self.client.force_login(self.picker)

        response = self.client.post(
            reverse("fbs:tsd_pick_verification", kwargs={"batch_id": self.batch.id}),
            {
                "action": "select_item",
                "item_scan": allocation.balance.barcode,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "заказ TASK5A-GOOD-ORDER")
        self.assertContains(response, "Причина ошибки: Тестовая причина ошибки ЧЗ.")
        self.assertContains(response, "Указать проблему заказа")
        self.assertContains(response, "Это ручное действие")


class FbsClientProfileAutomaticCommandEnablementTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="FBS automatic commands client")

    @staticmethod
    def _selection():
        from .services.client_profiles import SelectedMarketplaceWarehouse

        return SelectedMarketplaceWarehouse(
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            warehouse_id="auto-command-warehouse",
            name="WB auto commands",
        )

    def test_new_selected_profile_enables_outbox_and_marking_commands(self):
        from .services.client_profiles import configure_client_fbs_profiles

        result = configure_client_fbs_profiles(
            agency=self.agency,
            enabled=True,
            selections=(self._selection(),),
        )

        profile = FbsIntegrationProfile.objects.get(agency=self.agency)
        self.assertEqual(result.created, 1)
        self.assertTrue(profile.is_active)
        self.assertTrue(profile.order_pull_enabled)
        self.assertTrue(profile.outbox_enabled)
        self.assertTrue(profile.marking_push_enabled)

    def test_resaving_selected_legacy_profile_enables_all_commands(self):
        from .services.client_profiles import configure_client_fbs_profiles

        profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="Legacy WB profile",
            external_warehouse_id="auto-command-warehouse",
            is_active=True,
            order_pull_enabled=True,
            outbox_enabled=False,
            marking_push_enabled=False,
        )

        result = configure_client_fbs_profiles(
            agency=self.agency,
            enabled=True,
            selections=(self._selection(),),
        )

        profile.refresh_from_db()
        self.assertEqual(result.updated, 1)
        self.assertTrue(profile.outbox_enabled)
        self.assertTrue(profile.marking_push_enabled)


class FbsDesktopPrintNoAutomaticRetryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="desktop-print-no-duplicate",
            password="test-password",
        )
        self.client.force_login(self.user)

    def _job(self, *, status: str, card_id: str | None = None) -> ProcessingPrintJob:
        return ProcessingPrintJob.objects.create(
            status=status,
            order_id="FBS-ORDER-1",
            card_id=card_id or f"fbs:order-label:1:test:{status}",
            barcode="ORDER-LABEL",
            printer_name="Xprinter XP-365B",
            label_png_base64="cG5n",
            requested_by=self.user.username,
            agent="desktop:test-workstation",
        )

    def _complete(self, *, job, status):
        return self.client.post(
            "/fbs/desktop-print/complete/",
            data=json.dumps(
                {
                    "workstation_id": "test-workstation",
                    "job_ids": [job.id],
                    "status": status,
                }
            ),
            content_type="application/json",
            HTTP_X_FULLBOX_DESKTOP="1",
        )

    def _next(self, **extra):
        return self.client.get(
            "/fbs/desktop-print/next/",
            {"workstation_id": "test-workstation"},
            HTTP_X_FULLBOX_DESKTOP="1",
            **extra,
        )

    def test_desktop_presence_publishes_current_printers_and_preserves_them_on_poll(self):
        response = self.client.post(
            "/fbs/desktop-print/presence/",
            data=json.dumps(
                {
                    "workstation_id": "test-workstation",
                    "printers": ["Xprinter XP-365B", "Xprinter XP-365B (копия 3)"],
                    "printer_details": [
                        {"name": "Xprinter XP-365B (копия 3)", "is_default": True}
                    ],
                    "preferred_printer": "Xprinter XP-365B (копия 3)",
                }
            ),
            content_type="application/json",
            HTTP_X_FULLBOX_DESKTOP="1",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self._next()
        presence = get_desktop_presences(["test-workstation"])["test-workstation"]
        self.assertEqual(
            presence["printers"],
            ["Xprinter XP-365B", "Xprinter XP-365B (копия 3)"],
        )
        self.assertEqual(
            presence["preferred_printer"], "Xprinter XP-365B (копия 3)"
        )

    def test_stale_printing_job_is_not_automatically_reprinted(self):
        job = self._job(status=ProcessingPrintJob.STATUS_PRINTING)
        ProcessingPrintJob.objects.filter(pk=job.pk).update(
            updated_at=timezone.now() - timedelta(hours=1),
        )

        response = self._next()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["has_job"])
        self.assertEqual(response.json()["retry_after_ms"], 3000)
        self.assertEqual(response.headers["Retry-After"], "3")
        job.refresh_from_db()
        self.assertEqual(job.status, ProcessingPrintJob.STATUS_PRINTING)

    def test_logged_out_desktop_poll_returns_json_reauthentication_without_redirect(self):
        self.client.logout()

        response = self._next()

        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.json()["reauth_required"])
        self.assertEqual(response.json()["retry_after_ms"], 60000)
        self.assertEqual(response.headers["Retry-After"], "60")
        self.assertNotIn("Location", response.headers)

    @override_settings(AUTHENTICATED_IDLE_TIMEOUT_SECONDS=60)
    def test_expired_desktop_poll_returns_json_without_login_page_redirect(self):
        session = self.client.session
        session["_fullbox_last_activity_at"] = int(timezone.now().timestamp()) - 61
        session.save()

        response = self._next()

        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.json()["reauth_required"])
        self.assertEqual(response.headers["Retry-After"], "60")
        self.assertNotIn("Location", response.headers)

    def test_pending_job_is_still_delivered_to_desktop(self):
        job = self._job(status=ProcessingPrintJob.STATUS_PENDING)

        response = self._next()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["has_job"])
        self.assertEqual(response.json()["job"]["id"], job.id)
        job.refresh_from_db()
        self.assertEqual(job.status, ProcessingPrintJob.STATUS_PRINTING)

    def test_pending_job_accepts_individual_desktop_credential(self):
        from agent.auth import secret_digest
        from agent.models import DeviceAgent

        device = DeviceAgent.objects.create(
            agent_id="desktop-auth-0123456789abcdef0123456789abcdef",
            name="test-workstation",
            token_digest=secret_digest("desktop-secret"),
        )
        job = self._job(status=ProcessingPrintJob.STATUS_PENDING)

        response = self._next(
            HTTP_X_FULLBOX_DESKTOP_ID=device.agent_id,
            HTTP_X_FULLBOX_DESKTOP_TOKEN="desktop-secret",
            HTTP_X_FULLBOX_DESKTOP_VERSION="1.0.24",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["has_job"])
        self.assertEqual(response.json()["desktop_auth"], "device")
        self.assertEqual(response.json()["job"]["id"], job.id)

    def test_invalid_individual_desktop_credential_does_not_use_legacy_fallback(self):
        from agent.auth import secret_digest
        from agent.models import DeviceAgent

        device = DeviceAgent.objects.create(
            agent_id="desktop-auth-fedcba9876543210fedcba9876543210",
            name="test-workstation",
            token_digest=secret_digest("desktop-secret"),
        )

        response = self._next(
            HTTP_X_FULLBOX_DESKTOP_ID=device.agent_id,
            HTTP_X_FULLBOX_DESKTOP_TOKEN="wrong-secret",
            HTTP_X_FULLBOX_DESKTOP_VERSION="1.0.24",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error_code"], "invalid_desktop_credentials")

    def test_supply_label_batch_is_capped_below_request_timeout(self):
        jobs = [
            self._job(
                status=ProcessingPrintJob.STATUS_PENDING,
                card_id=f"fbs:handover-supply:{batch_id}",
            )
            for batch_id in range(1, 11)
        ]

        response = self._next()

        self.assertEqual(response.status_code, 200)
        claimed_ids = response.json()["job"]["job_ids"]
        self.assertEqual(claimed_ids, [job.id for job in jobs[:4]])
        self.assertEqual(
            ProcessingPrintJob.objects.filter(
                id__in=claimed_ids,
                status=ProcessingPrintJob.STATUS_PRINTING,
            ).count(),
            4,
        )
        self.assertEqual(
            ProcessingPrintJob.objects.filter(
                id__in=[job.id for job in jobs[4:]],
                status=ProcessingPrintJob.STATUS_PENDING,
            ).count(),
            6,
        )

    def test_hidden_label_renderer_cannot_claim_pending_job(self):
        job = self._job(status=ProcessingPrintJob.STATUS_PENDING)

        response = self._next(
            HTTP_REFERER="https://lk.fullbox.ru/labels/settings/?renderer=1",
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["has_job"])
        job.refresh_from_db()
        self.assertEqual(job.status, ProcessingPrintJob.STATUS_PENDING)

    def test_regular_labels_settings_page_can_still_claim_pending_job(self):
        job = self._job(status=ProcessingPrintJob.STATUS_PENDING)

        response = self._next(
            HTTP_REFERER="https://lk.fullbox.ru/labels/settings/",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["has_job"])
        self.assertEqual(response.json()["job"]["id"], job.id)
        job.refresh_from_db()
        self.assertEqual(job.status, ProcessingPrintJob.STATUS_PRINTING)

    @patch("fbs.services.handover.dispatch_handover_batch")
    def test_printed_supply_label_dispatches_matching_handover(self, dispatch):
        job = self._job(
            status=ProcessingPrintJob.STATUS_PRINTING,
            card_id="fbs:handover-supply:75",
        )

        response = self._complete(
            job=job,
            status=ProcessingPrintJob.STATUS_PRINTED,
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("handover_transition_errors", response.json())
        job.refresh_from_db()
        self.assertEqual(job.status, ProcessingPrintJob.STATUS_PRINTED)
        dispatch.assert_called_once_with(
            batch_id=75,
            dispatched_by=self.user,
            supply_label_print_job_id=job.id,
        )

    @patch("fbs.services.handover.dispatch_handover_batch")
    def test_failed_supply_label_print_does_not_dispatch_handover(self, dispatch):
        job = self._job(
            status=ProcessingPrintJob.STATUS_PRINTING,
            card_id="fbs:handover-supply:75",
        )

        response = self._complete(
            job=job,
            status=ProcessingPrintJob.STATUS_FAILED,
        )

        self.assertEqual(response.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.status, ProcessingPrintJob.STATUS_FAILED)
        dispatch.assert_not_called()

    @patch("fbs.services.handover.dispatch_handover_batch")
    def test_repeated_print_confirmation_reconciles_handover(self, dispatch):
        job = self._job(
            status=ProcessingPrintJob.STATUS_PRINTED,
            card_id="fbs:handover-supply:75:reprint:abcdef123456",
        )

        response = self._complete(
            job=job,
            status=ProcessingPrintJob.STATUS_PRINTED,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["updated"], 0)
        dispatch.assert_called_once_with(
            batch_id=75,
            dispatched_by=self.user,
            supply_label_print_job_id=job.id,
        )

    @patch("fbs.services.handover.dispatch_handover_batch")
    def test_bulk_confirmation_keeps_prior_job_when_later_transition_fails(self, dispatch):
        first = self._job(
            status=ProcessingPrintJob.STATUS_PRINTING,
            card_id="fbs:handover-supply:75",
        )
        second = self._job(
            status=ProcessingPrintJob.STATUS_PRINTING,
            card_id="fbs:handover-supply:76",
        )
        dispatch.side_effect = [None, RuntimeError("late transition failed")]

        with self.assertRaisesMessage(RuntimeError, "late transition failed"):
            self.client.post(
                "/fbs/desktop-print/complete/",
                data=json.dumps(
                    {
                        "workstation_id": "test-workstation",
                        "job_ids": [first.id, second.id],
                        "status": ProcessingPrintJob.STATUS_PRINTED,
                    }
                ),
                content_type="application/json",
                HTTP_X_FULLBOX_DESKTOP="1",
            )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, ProcessingPrintJob.STATUS_PRINTED)
        self.assertEqual(second.status, ProcessingPrintJob.STATUS_PRINTING)


class FbsMarketplaceDeadlockRetryTests(SimpleTestCase):
    class _DeadlockCause(Exception):
        pgcode = "40P01"

    @patch("fbs.services.marketplace.time.sleep")
    @patch("fbs.services.marketplace._apply_success")
    def test_confirmation_retries_fully_rolled_back_deadlock(self, apply_success, sleep):
        from .services.marketplace import _apply_success_with_deadlock_retry

        deadlock = OperationalError("deadlock detected")
        deadlock.__cause__ = self._DeadlockCause()
        confirmed = object()
        response = object()
        apply_success.side_effect = [deadlock, confirmed]

        result = _apply_success_with_deadlock_retry(123, response)

        self.assertIs(result, confirmed)
        self.assertEqual(apply_success.call_count, 2)
        apply_success.assert_called_with(123, response)
        sleep.assert_called_once_with(0.05)

    @patch("fbs.services.marketplace.time.sleep")
    @patch("fbs.services.marketplace._apply_success")
    def test_confirmation_does_not_retry_other_database_error(self, apply_success, sleep):
        from .services.marketplace import _apply_success_with_deadlock_retry

        error = OperationalError("connection failed")
        apply_success.side_effect = error

        with self.assertRaises(OperationalError) as captured:
            _apply_success_with_deadlock_retry(123, object())

        self.assertIs(captured.exception, error)
        self.assertEqual(apply_success.call_count, 1)
        sleep.assert_not_called()


class FbsOperatorWarehouseColumnTemplateTests(SimpleTestCase):
    def test_orders_table_shows_marketplace_warehouse_as_separate_column(self):
        templates_dir = Path(__file__).resolve().parents[1] / "templates" / "fbs"
        orders_template = (templates_dir / "operator_orders.html").read_text()
        row_template = (templates_dir / "operator_order_row.html").read_text()

        self.assertIn("<th>Склад WB</th>", orders_template)
        self.assertIn("склад {{ profile.external_warehouse_id", orders_template)
        self.assertIn('class="warehouse-column"', row_template)
        self.assertIn("order.profile.external_warehouse_id", row_template)
        self.assertIn("Профиль включен", row_template)
        self.assertIn("Профиль выключен", row_template)


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
)
class FbsOperatorOrderStickerSearchTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="sticker-search")
        Employee.objects.create(
            user=self.user,
            full_name="FBS sticker search storekeeper",
            role="storekeeper",
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Sticker search client")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="Sticker search profile",
            external_warehouse_id="sticker-search-warehouse",
        )
        self.order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="5708459089",
            internal_status=FbsOrder.STATUS_HANDED_OVER,
            marketplace_status="complete",
        )
        FbsOrderLabel.objects.create(
            order=self.order,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            external_label_id="5774143-4777",
            barcode="*DXGoX5l3",
            status=FbsOrderLabel.STATUS_APPLIED,
        )
        self.other_order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="UNRELATED-ORDER",
            internal_status=FbsOrder.STATUS_RESERVED,
        )

    def test_visible_sticker_number_finds_archived_order(self):
        response = self.client.get(
            reverse("fbs:operator_orders"),
            {"q": "5774143-4777"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "5708459089")
        self.assertNotContains(response, "UNRELATED-ORDER")
        self.assertContains(response, "Поиск по текущим и архивным заказам")

    def test_scanner_barcode_finds_order(self):
        response = self.client.get(
            reverse("fbs:operator_orders"),
            {"q": "*DXGoX5l3"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "5708459089")

    def test_search_field_is_ready_for_scanner_input(self):
        response = self.client.get(reverse("fbs:operator_orders"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-order-scanner-search")
        self.assertContains(response, "Сканируйте стикер или введите значение")


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
)
class FbsOperatorFilteredWaveTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="dev")
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Filtered wave client")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            name="Filtered wave profile",
            external_warehouse_id="filtered-wave-warehouse",
        )

    def _order(self, external_id: str, ordered_at):
        return FbsOrder.objects.create(
            profile=self.profile,
            external_order_id=external_id,
            internal_status=FbsOrder.STATUS_RESERVED,
            ordered_at=ordered_at,
        )

    def test_filtered_wave_selection_uses_oldest_marketplace_orders(self):
        from .operator_views import _prioritized_queue_order_ids

        now = timezone.now()
        newest = self._order("FILTERED-NEWEST", now - timedelta(hours=1))
        oldest = self._order("FILTERED-OLDEST", now - timedelta(days=2))
        middle = self._order("FILTERED-MIDDLE", now - timedelta(days=1))

        selected = _prioritized_queue_order_ids(
            FbsOrder.objects.filter(pk__in=(newest.id, oldest.id, middle.id)),
            limit=2,
        )

        self.assertEqual(selected, [oldest.id, middle.id])

    def test_filtered_wave_detects_more_than_one_partner(self):
        from .operator_views import _filtered_wave_agency_ids

        other_agency = Agency.objects.create(agn_name="Other filtered wave client")
        other_profile = FbsIntegrationProfile.objects.create(
            agency=other_agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            name="Other filtered wave profile",
            external_warehouse_id="other-filtered-wave-warehouse",
        )
        self._order("FILTERED-FIRST-AGENCY", timezone.now())
        FbsOrder.objects.create(
            profile=other_profile,
            external_order_id="FILTERED-SECOND-AGENCY",
            internal_status=FbsOrder.STATUS_RESERVED,
            ordered_at=timezone.now(),
        )

        agency_ids = _filtered_wave_agency_ids(FbsOrder.objects.all())

        self.assertEqual(set(agency_ids), {self.agency.id, other_agency.id})

    @patch("fbs.operator_views.prepare_pick_queue")
    def test_launch_without_checkboxes_uses_filtered_oldest_orders(self, prepare):
        from types import SimpleNamespace

        now = timezone.now()
        newest = self._order("FILTERED-VIEW-NEWEST", now - timedelta(hours=1))
        oldest = self._order("FILTERED-VIEW-OLDEST", now - timedelta(days=2))
        middle = self._order("FILTERED-VIEW-MIDDLE", now - timedelta(days=1))
        prepare.return_value = SimpleNamespace(
            batches=(),
            tasks_created=0,
            awaiting_stock_orders=0,
            validation_failed_orders=0,
            rejected_orders=(),
        )

        response = self.client.post(
            reverse("fbs:operator_prepare_wave"),
            {
                "marketplace": FbsIntegrationProfile.MARKETPLACE_OZON,
                "queue_limit": "1",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            prepare.call_args.kwargs["order_ids"],
            [oldest.id],
        )
        self.assertNotIn(middle.id, prepare.call_args.kwargs["order_ids"])
        self.assertNotIn(newest.id, prepare.call_args.kwargs["order_ids"])
        self.assertTrue(prepare.call_args.kwargs["single_agency_only"])

    @patch("fbs.operator_views.prepare_pick_queue")
    def test_launch_without_checkboxes_rejects_multiple_partners(self, prepare):
        other_agency = Agency.objects.create(agn_name="Second filtered wave client")
        other_profile = FbsIntegrationProfile.objects.create(
            agency=other_agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            name="Second filtered wave profile",
            external_warehouse_id="second-filtered-wave-warehouse",
        )
        self._order("FILTERED-FIRST-PARTNER", timezone.now())
        FbsOrder.objects.create(
            profile=other_profile,
            external_order_id="FILTERED-SECOND-PARTNER",
            internal_status=FbsOrder.STATUS_RESERVED,
            ordered_at=timezone.now(),
        )

        response = self.client.post(
            reverse("fbs:operator_prepare_wave"),
            {
                "marketplace": FbsIntegrationProfile.MARKETPLACE_OZON,
                "queue_limit": "2",
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertContains(
            response,
            "По выбранному фильтру найдено несколько партнеров",
            status_code=409,
        )
        prepare.assert_not_called()

    def test_launch_form_allows_filtered_mode_without_manual_checkboxes(self):
        templates_dir = Path(__file__).resolve().parents[1] / "templates" / "fbs"
        orders_template = (templates_dir / "operator_orders.html").read_text()
        launch_form = orders_template.split("{% if wave_stock_confirmation %}", 1)[0]

        self.assertNotIn('name="submit_mode"', launch_form)
        self.assertNotIn("data-wave-submit", launch_form)
        self.assertIn("Без галочек система возьмет самые старые заказы", launch_form)
