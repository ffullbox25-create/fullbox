from datetime import timedelta
from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace

from agent.models import DeviceAgent
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import OperationalError, close_old_connections, transaction
from django.test import (
    TestCase,
    TransactionTestCase,
    override_settings,
    skipUnlessDBFeature,
)
from django.utils import timezone
from processing_app.models import ProcessingPrintJob

from sklad.models import WarehouseLocation
from fbs.controller_session import CONTROLLER_WORKSTATION_SESSION_KEY
from fbs.exceptions import FbsHandoverError, FbsPickingError
from fbs.models import (
    FbsBox,
    FbsControllerCheckTote,
    FbsControllerPickTote,
    FbsControllerSession,
    FbsControllerToteOrder,
    FbsHandoverBatch,
    FbsHandoverBox,
    FbsHandoverOrderAssignment,
    FbsIntegrationProfile,
    FbsMarketplaceMetadataTransfer,
    FbsOrder,
    FbsOrderItem,
    FbsOrderLabel,
    FbsOrderStockAllocation,
    FbsOrderTraceability,
    FbsPallet,
    FbsPickBatch,
    FbsPickRestockRequest,
    FbsPickTask,
    FbsPickingCart,
    FbsStockBalance,
    FbsStorageCell,
    FbsToteZone,
    FbsWorkstation,
)
from fbs.controller_views import (
    _controller_metadata_state,
    _current_check_tote_required_transfers,
    _get_or_select_controller_workstation,
)
from fbs.services.picking import (
    _controller_workstation_loads,
    claim_pick_batch_verification,
    handover_pick_batch_for_verification,
)
from fbs.services.printing import recover_stale_fbs_order_label_print_job
from fbs.services.controller_shift import start_controller_shift
from fbs.services.handover import _required_metadata_confirmed
from fbs.services.totes import (
    _is_database_deadlock,
    _is_database_lock_unavailable,
    _is_database_statement_timeout,
    _prefetch_wb_labels_for_pick_batch,
    _ready_transport_box,
    attach_pick_tote_to_available_check_tote,
    check_tote_readiness,
)
from sku.models import Agency, SKU


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
)
class FbsControllerSingleWaveTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.controller = users.objects.create_user(username="single_wave_controller")
        self.picker = users.objects.create_user(username="single_wave_picker")
        self.other_picker = users.objects.create_user(username="single_wave_picker_2")
        self.agency = Agency.objects.create(agn_name="Single wave test client")
        self.workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-990001",
            name="Single wave workstation",
            printer_name="TEST-PRINTER",
        )

    def test_workstation_defaults_to_seven_tares(self):
        self.assertEqual(self.workstation.max_parallel_waves, 7)

    def test_postgresql_deadlock_is_recognized_for_bounded_prefetch_retry(self):
        self.assertTrue(_is_database_deadlock(OperationalError("deadlock detected")))
        self.assertFalse(_is_database_deadlock(OperationalError("connection lost")))

    def test_postgresql_busy_lock_and_statement_timeout_are_recognized(self):
        self.assertTrue(
            _is_database_lock_unavailable(
                OperationalError("could not obtain lock on row in relation")
            )
        )
        self.assertTrue(
            _is_database_statement_timeout(
                OperationalError("canceling statement due to statement timeout")
            )
        )
        self.assertFalse(_is_database_lock_unavailable(OperationalError("offline")))

    @override_settings(FBS_DESKTOP_PRINT_LEASE_SECONDS=60)
    def test_stale_order_label_print_is_requeued_once_without_duplicate(self):
        profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="Stale print recovery",
            external_account_id="stale-print-recovery",
            is_active=True,
        )
        order = FbsOrder.objects.create(
            profile=profile,
            external_order_id="STUCK-PRINT-ORDER",
            internal_status=FbsOrder.STATUS_PICKED,
        )
        label = FbsOrderLabel.objects.create(
            order=order,
            marketplace=profile.marketplace,
            external_label_id="STUCK-PRINT-LABEL",
            barcode="STUCK-PRINT-BARCODE",
            status=FbsOrderLabel.STATUS_READY,
        )
        job = ProcessingPrintJob.objects.create(
            order_id=f"FBS-ORDER-{order.id}",
            card_id=f"fbs:order-label:{label.id}:test",
            barcode=label.barcode,
            printer_name="TEST-PRINTER",
            agent="desktop:test",
            label_png_base64="retained-payload",
            status=ProcessingPrintJob.STATUS_PRINTING,
        )
        ProcessingPrintJob.objects.filter(pk=job.pk).update(
            updated_at=timezone.now() - timedelta(seconds=61)
        )

        recovered = recover_stale_fbs_order_label_print_job(label_id=label.id)

        self.assertEqual(recovered.id, job.id)
        job.refresh_from_db()
        self.assertEqual(job.status, ProcessingPrintJob.STATUS_PENDING)
        self.assertEqual(job.label_png_base64, "retained-payload")
        self.assertEqual(
            ProcessingPrintJob.objects.filter(card_id=job.card_id).count(),
            1,
        )

        ProcessingPrintJob.objects.filter(pk=job.pk).update(
            status=ProcessingPrintJob.STATUS_PRINTING,
            updated_at=timezone.now() - timedelta(seconds=61),
        )
        self.assertIsNone(
            recover_stale_fbs_order_label_print_job(label_id=label.id)
        )

    def test_workstation_rejects_capacity_above_seven(self):
        self.workstation.max_parallel_waves = 8

        with self.assertRaisesMessage(
            ValidationError,
            "На рабочем месте разрешено от одной до семи тележек.",
        ):
            self.workstation.full_clean()

    def test_single_configured_workstation_still_requires_scan(self):
        agent = DeviceAgent.objects.create(agent_id="controller-binding-agent")
        self.workstation.device_agent = agent
        self.workstation.save(update_fields=["device_agent", "updated_at"])
        request = SimpleNamespace(session={})

        selected = _get_or_select_controller_workstation(request)

        self.assertIsNone(selected)
        self.assertNotIn(CONTROLLER_WORKSTATION_SESSION_KEY, request.session)

    def test_scanned_workstation_is_loaded_from_controller_session(self):
        request = SimpleNamespace(
            session={CONTROLLER_WORKSTATION_SESSION_KEY: self.workstation.id}
        )

        selected = _get_or_select_controller_workstation(request)

        self.assertEqual(selected.id, self.workstation.id)

    def test_previous_operator_session_is_cleared_after_desk_takeover(self):
        next_controller = get_user_model().objects.create_user(
            username="single_wave_controller_next"
        )
        start_controller_shift(
            workstation_id=self.workstation.id,
            controller=next_controller,
        )
        request = SimpleNamespace(
            user=self.controller,
            session={CONTROLLER_WORKSTATION_SESSION_KEY: self.workstation.id},
        )

        selected = _get_or_select_controller_workstation(request)

        self.assertIsNone(selected)
        self.assertNotIn(CONTROLLER_WORKSTATION_SESSION_KEY, request.session)

    def test_fullbox_desktop_transfers_bound_workstation_to_new_controller(self):
        next_controller = get_user_model().objects.create_user(
            username="single_wave_controller_next_desktop"
        )
        start_controller_shift(
            workstation_id=self.workstation.id,
            controller=self.controller,
        )
        request = SimpleNamespace(
            user=next_controller,
            session={CONTROLLER_WORKSTATION_SESSION_KEY: self.workstation.id},
            META={"HTTP_USER_AGENT": "FullboxDesktop/1.0.18"},
        )

        selected = _get_or_select_controller_workstation(request)

        self.assertEqual(selected.id, self.workstation.id)
        self.workstation.refresh_from_db()
        self.assertEqual(self.workstation.shift_controller_id, next_controller.id)
        self.assertEqual(
            request.session[CONTROLLER_WORKSTATION_SESSION_KEY],
            self.workstation.id,
        )

    def _batch(self, suffix, *, picker, at_workstation=False, controller=None):
        cart = FbsPickingCart.objects.create(
            barcode=f"FBS-CART-SINGLE-{suffix}",
            name=f"Single wave cart {suffix}",
        )
        return FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            assigned_to=picker,
            workstation=self.workstation if at_workstation else None,
            cart=cart,
            verification_assigned_to=controller,
            picking_completed_at=timezone.now() if at_workstation else None,
            verification_started_at=timezone.now() if controller else None,
        )

    def test_controller_cannot_claim_fourth_active_wave(self):
        for number in range(1, 4):
            self._batch(
                f"ACTIVE-{number}",
                picker=self.picker,
                at_workstation=True,
                controller=self.controller,
            )
        fourth = self._batch("FOURTH", picker=self.other_picker, at_workstation=True)

        with self.assertRaisesMessage(FbsPickingError, "уже открыты три проверки волн"):
            claim_pick_batch_verification(
                batch_id=fourth.id,
                assigned_to=self.controller,
            )

    def test_workstation_accepts_seventh_tare_within_capacity(self):
        self.workstation.max_parallel_waves = 7
        self.workstation.save(update_fields=["max_parallel_waves"])
        for number in range(1, 7):
            self._batch(
                f"ACTIVE-{number}",
                picker=self.picker,
                at_workstation=True,
            )
        seventh = self._batch("SEVENTH", picker=self.other_picker)

        handed_over = handover_pick_batch_for_verification(
            batch_id=seventh.id,
            workstation_scan=self.workstation.barcode,
            performed_by=self.other_picker,
        )

        self.assertEqual(handed_over.workstation_id, self.workstation.id)
        self.assertIsNotNone(handed_over.picking_completed_at)

    def test_cartless_split_continuations_do_not_consume_tare_capacity(self):
        self.workstation.max_parallel_waves = 7
        self.workstation.save(update_fields=["max_parallel_waves"])
        for number in range(1, 7):
            self._batch(
                f"ACTIVE-PHYSICAL-{number}",
                picker=self.picker,
                at_workstation=True,
            )
        for number in range(1, 6):
            FbsPickBatch.objects.create(
                agency=self.agency,
                status=FbsPickBatch.STATUS_VERIFICATION,
                planned_qty=1,
                picked_qty=1,
                assigned_to=self.picker,
                workstation=self.workstation,
                cart=None,
                picking_completed_at=timezone.now(),
            )
        seventh = self._batch("SEVENTH-AFTER-SPLIT", picker=self.other_picker)

        handed_over = handover_pick_batch_for_verification(
            batch_id=seventh.id,
            workstation_scan=self.workstation.barcode,
            performed_by=self.other_picker,
        )

        self.assertEqual(handed_over.workstation_id, self.workstation.id)
        self.assertEqual(
            _controller_workstation_loads(workstation_ids=[self.workstation.id]),
            {self.workstation.id: 7},
        )

    def test_workstation_rejects_tare_above_capacity(self):
        self.workstation.max_parallel_waves = 7
        self.workstation.save(update_fields=["max_parallel_waves"])
        for number in range(1, 8):
            self._batch(
                f"ACTIVE-{number}",
                picker=self.picker,
                at_workstation=True,
            )
        overflow = self._batch("OVERFLOW", picker=self.other_picker)

        with self.assertRaisesMessage(
            FbsPickingError,
            "На рабочем месте уже 7 единиц тары",
        ):
            handover_pick_batch_for_verification(
                batch_id=overflow.id,
                workstation_scan=self.workstation.barcode,
                performed_by=self.other_picker,
            )

    def test_active_restock_does_not_hold_controller_slot(self):
        returning = self._batch("RETURN", picker=self.picker, at_workstation=True)
        FbsPickRestockRequest.objects.create(
            batch=returning,
            status=FbsPickRestockRequest.STATUS_QUEUED,
            reason="Return is waiting for physical scan",
            planned_qty=1,
            created_by=self.controller,
        )
        incoming = self._batch("INCOMING", picker=self.other_picker)

        handed_over = handover_pick_batch_for_verification(
            batch_id=incoming.id,
            workstation_scan=self.workstation.barcode,
            performed_by=self.other_picker,
        )

        self.assertEqual(handed_over.workstation_id, self.workstation.id)
        self.assertIsNotNone(handed_over.picking_completed_at)

    def test_auto_route_skips_check_tote_linked_to_incompatible_handover(self):
        profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="Strict check tote routing",
            external_account_id="strict-check-tote",
            external_warehouse_id="1876669",
            is_active=True,
        )
        order = FbsOrder.objects.create(
            profile=profile,
            external_order_id="5617361585",
            internal_status=FbsOrder.STATUS_PICKED,
            marketplace_status="new",
            raw_payload={
                "officeId": 3105447,
                "deliveryType": "fbs",
            },
        )
        FbsOrderItem.objects.create(
            order=order,
            external_line_id="5617361585",
            external_sku="709826344",
            barcode="4660406800206",
            product_name="Strict route item",
            quantity=1,
            requirements={"cargo_type": 1, "is_b2b": False},
        )
        unknown_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-990090",
            name="Strict route unknown tote",
        )
        pick_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-990091",
            name="Strict route pick tote",
        )
        free_zone = FbsToteZone.objects.create(
            barcode="FBS-TEST-FREE-STRICT",
            name="Strict route free zone",
            kind=FbsToteZone.KIND_FREE,
        )
        session = FbsControllerSession.objects.create(
            workstation=self.workstation,
            controller=self.controller,
            unknown_tote=unknown_tote,
            canceled_tote=unknown_tote,
            free_zone=free_zone,
        )
        stale_handover = FbsHandoverBatch.objects.create(
            profile=profile,
            compatibility_key=(
                "warehouse:1876669:cargo:1:b2b:0:office:3105447:"
                "delivery:fbs:destination:warehouse_sc:invalid-kiz-route:"
                "rewave:source-39:order-5036"
            ),
            marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
            status=FbsHandoverBatch.STATUS_OPEN,
        )
        stale_check_tote = FbsControllerCheckTote.objects.create(
            session=session,
            agency=self.agency,
            profile=profile,
            handover_batch=stale_handover,
            item_qty=1,
            opened_by=self.controller,
        )
        batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            assigned_to=self.picker,
            workstation=self.workstation,
            cart=pick_tote,
            picking_completed_at=timezone.now(),
        )
        FbsPickTask.objects.create(
            batch=batch,
            order=order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
        )

        context = attach_pick_tote_to_available_check_tote(
            session_id=session.id,
            pick_tote_scan=pick_tote.barcode,
            performed_by=self.controller,
        )

        self.assertNotEqual(context.check_tote_id, stale_check_tote.id)
        self.assertIsNone(context.check_tote.handover_batch_id)
        stale_check_tote.refresh_from_db()
        self.assertEqual(stale_check_tote.item_qty, 1)

    def test_rewave_warehouse_route_creates_one_internal_transport_box(self):
        profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="Rewave internal transport box",
            external_account_id="rewave-internal-box",
            external_warehouse_id="2126960",
            is_active=True,
        )
        service_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-990092",
            name="Rewave internal box service tote",
        )
        free_zone = FbsToteZone.objects.create(
            barcode="FBS-TEST-FREE-REWAVE-BOX",
            name="Rewave internal box free zone",
            kind=FbsToteZone.KIND_FREE,
        )
        session = FbsControllerSession.objects.create(
            workstation=self.workstation,
            controller=self.controller,
            unknown_tote=service_tote,
            canceled_tote=service_tote,
            free_zone=free_zone,
        )
        handover = FbsHandoverBatch.objects.create(
            profile=profile,
            compatibility_key=(
                "warehouse:2126960:cargo:1:b2b:0:office:242:delivery:fbs:"
                "destination:warehouse_sc:invalid-kiz-route:rewave:"
                "source-45:order-5149"
            ),
            external_supply_id="WB-GI-271150815",
            marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
            status=FbsHandoverBatch.STATUS_OPEN,
        )
        check_tote = FbsControllerCheckTote.objects.create(
            session=session,
            agency=self.agency,
            profile=profile,
            handover_batch=handover,
            item_qty=1,
            opened_by=self.controller,
        )

        first_box = _ready_transport_box(check_tote)
        second_box = _ready_transport_box(check_tote)

        self.assertEqual(first_box.id, second_box.id)
        self.assertEqual(first_box.status, FbsHandoverBox.STATUS_OPEN)
        self.assertTrue(
            first_box.qr_code.startswith(f"FBS-WB-GI-BOX-{handover.id}-")
        )
        self.assertEqual(handover.boxes.count(), 1)

    def test_pickup_point_route_still_requires_marketplace_transport_box(self):
        profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="Pickup point marketplace box",
            external_account_id="pickup-marketplace-box",
            external_warehouse_id="2126960",
            is_active=True,
        )
        service_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-990094",
            name="Pickup marketplace box service tote",
        )
        free_zone = FbsToteZone.objects.create(
            barcode="FBS-TEST-FREE-PICKUP-BOX",
            name="Pickup marketplace box free zone",
            kind=FbsToteZone.KIND_FREE,
        )
        session = FbsControllerSession.objects.create(
            workstation=self.workstation,
            controller=self.controller,
            unknown_tote=service_tote,
            canceled_tote=service_tote,
            free_zone=free_zone,
        )
        handover = FbsHandoverBatch.objects.create(
            profile=profile,
            compatibility_key=(
                "warehouse:2126960:cargo:1:b2b:0:office:242:delivery:fbs:"
                "destination:pickup_point:workstation:4"
            ),
            external_supply_id="WB-GI-PICKUP-BOX",
            marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
            status=FbsHandoverBatch.STATUS_OPEN,
        )
        check_tote = FbsControllerCheckTote.objects.create(
            session=session,
            agency=self.agency,
            profile=profile,
            handover_batch=handover,
            item_qty=1,
            opened_by=self.controller,
        )

        with self.assertRaisesMessage(
            FbsHandoverError,
            "Транспортный короб marketplace еще не готов",
        ):
            _ready_transport_box(check_tote)

        self.assertEqual(handover.boxes.count(), 0)

    def test_old_failed_kiz_does_not_block_confirmed_current_repick(self):
        profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="Current repick metadata",
            external_account_id="current-repick-metadata",
            external_warehouse_id="1876669",
            is_active=True,
        )
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="RE-PICK-KIZ",
            name="Repicked marked product",
        )
        order = FbsOrder.objects.create(
            profile=profile,
            external_order_id="5609263388",
            internal_status=FbsOrder.STATUS_READY_FOR_HANDOVER,
            marketplace_status="confirm",
        )
        item = FbsOrderItem.objects.create(
            order=order,
            external_line_id="5609263388",
            external_sku=sku.sku_code,
            barcode="4660406800183",
            sku=sku,
            product_name=sku.name,
            quantity=1,
        )
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=99,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="FBS-RE-PICK-KIZ",
            is_storage=True,
            is_pickable=True,
        )
        cell = FbsStorageCell.objects.create(
            cell_code=location.location_code,
            location=location,
        )
        pallet = FbsPallet.objects.create(
            agency=self.agency,
            cell=cell,
            pallet_code="RE-PICK-KIZ-PALLET",
            status=FbsPallet.STATUS_ACTIVE,
        )
        box = FbsBox.objects.create(
            agency=self.agency,
            pallet=pallet,
            box_code="RE-PICK-KIZ-BOX",
            status=FbsBox.STATUS_ACTIVE,
        )
        balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=box,
            sku_ref=sku,
            identity_key="repick-kiz-balance",
            sku_code=sku.sku_code,
            name=sku.name,
            barcode=item.barcode,
            qty=2,
            available_qty=0,
            reserved_qty=0,
        )
        old_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
        )
        old_task = FbsPickTask.objects.create(
            batch=old_batch,
            order=order,
            status=FbsPickTask.STATUS_EXCEPTION,
            planned_qty=1,
            picked_qty=1,
        )
        current_cart = FbsPickingCart.objects.create(
            barcode="FBS-CART-990092",
            name="Current repick tote",
        )
        current_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            assigned_to=self.picker,
            workstation=self.workstation,
            cart=current_cart,
            picking_completed_at=timezone.now(),
        )
        current_task = FbsPickTask.objects.create(
            batch=current_batch,
            order=order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
        )
        old_allocation = FbsOrderStockAllocation.objects.create(
            order_item=item,
            balance=balance,
            pick_task=old_task,
            picked_by=self.picker,
            qty_reserved=1,
            qty_picked=1,
            status=FbsOrderStockAllocation.STATUS_PICKED,
        )
        current_allocation = FbsOrderStockAllocation.objects.create(
            order_item=item,
            balance=balance,
            pick_task=current_task,
            picked_by=self.picker,
            qty_reserved=1,
            qty_picked=1,
            status=FbsOrderStockAllocation.STATUS_PICKED,
        )
        old_trace = FbsOrderTraceability.objects.create(
            allocation=old_allocation,
            marking_code="OLD-REJECTED-KIZ",
            qty=1,
            status=FbsOrderTraceability.STATUS_PICKED,
        )
        current_trace = FbsOrderTraceability.objects.create(
            allocation=current_allocation,
            marking_code="CURRENT-CONFIRMED-KIZ",
            qty=1,
            status=FbsOrderTraceability.STATUS_PICKED,
        )
        FbsMarketplaceMetadataTransfer.objects.create(
            order_item=item,
            traceability=old_trace,
            metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
            value=old_trace.marking_code,
            is_required=True,
            status=FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
            idempotency_key="c" * 64,
        )
        FbsMarketplaceMetadataTransfer.objects.create(
            order_item=item,
            traceability=current_trace,
            metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
            value=current_trace.marking_code,
            is_required=True,
            status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED,
            idempotency_key="d" * 64,
        )
        free_zone = FbsToteZone.objects.create(
            barcode="FBS-TEST-FREE-RE-PICK",
            name="Repick free zone",
            kind=FbsToteZone.KIND_FREE,
        )
        unknown_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-990093",
            name="Repick unknown tote",
        )
        session = FbsControllerSession.objects.create(
            workstation=self.workstation,
            controller=self.controller,
            unknown_tote=unknown_tote,
            canceled_tote=unknown_tote,
            free_zone=free_zone,
        )
        handover = FbsHandoverBatch.objects.create(
            profile=profile,
            marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
            status=FbsHandoverBatch.STATUS_OPEN,
        )
        FbsHandoverOrderAssignment.objects.create(
            batch=handover,
            order=order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
            assigned_by=self.controller,
            confirmed_at=timezone.now(),
        )
        check_tote = FbsControllerCheckTote.objects.create(
            session=session,
            agency=self.agency,
            profile=profile,
            handover_batch=handover,
            status=FbsControllerCheckTote.STATUS_WAITING_KIZ,
            item_qty=1,
            labeled_qty=1,
            opened_by=self.controller,
        )
        pick_context = FbsControllerPickTote.objects.create(
            session=session,
            check_tote=check_tote,
            pick_batch=current_batch,
            tote=current_cart,
            status=FbsControllerPickTote.STATUS_CLOSED,
            planned_qty=1,
            processed_qty=1,
            empty_confirmed_at=timezone.now(),
            closed_at=timezone.now(),
        )
        label = FbsOrderLabel.objects.create(
            order=order,
            marketplace=profile.marketplace,
            external_label_id="5729998-4825",
            barcode="CURRENT-ORDER-LABEL",
            status=FbsOrderLabel.STATUS_APPLIED,
            applied_by=self.controller,
            applied_at=timezone.now(),
        )
        tote_order = FbsControllerToteOrder.objects.create(
            check_tote=check_tote,
            pick_tote=pick_context,
            order=order,
            label=label,
            status=FbsControllerToteOrder.STATUS_LABELED,
            units=1,
            label_confirmed_by=self.controller,
        )

        readiness = check_tote_readiness(check_tote)

        self.assertTrue(readiness.ready)
        self.assertEqual(readiness.status, FbsControllerCheckTote.STATUS_READY)
        self.assertEqual(readiness.blocked_order_ids, frozenset())
        required_transfers = _current_check_tote_required_transfers(tote_order)
        self.assertEqual(len(required_transfers), 1)
        self.assertEqual(required_transfers[0].traceability_id, current_trace.id)
        self.assertEqual(
            required_transfers[0].status,
            FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED,
        )
        self.assertFalse(_required_metadata_confirmed(order))
        self.assertTrue(
            _required_metadata_confirmed(
                order,
                handover_batch=handover,
            )
        )


class FbsControllerValidationStatusTests(TestCase):
    def test_marketplace_conflict_is_shown_as_invalid_in_russian(self):
        transfer = SimpleNamespace(
            status=FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
            get_status_display=lambda: "Конфликт",
        )

        state = _controller_metadata_state(
            required=True,
            scanned=True,
            transfer=transfer,
        )

        self.assertEqual(state, {"state": "problem", "label": "Невалиден"})

    def test_unsupported_validation_has_a_clear_russian_label(self):
        transfer = SimpleNamespace(
            status=FbsMarketplaceMetadataTransfer.STATUS_UNSUPPORTED,
            get_status_display=lambda: "Не поддерживается API",
        )

        state = _controller_metadata_state(
            required=True,
            scanned=True,
            transfer=transfer,
        )

        self.assertEqual(
            state,
            {"state": "problem", "label": "Проверка недоступна"},
        )


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
)
class FbsControllerWbPrefetchLockTests(TransactionTestCase):
    def setUp(self):
        users = get_user_model()
        self.controller = users.objects.create_user(username="prefetch-lock-controller")
        self.picker = users.objects.create_user(username="prefetch-lock-picker")
        self.agency = Agency.objects.create(agn_name="Prefetch lock client")
        self.workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-PREFETCH-LOCK",
            name="Prefetch lock workstation",
        )
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="Prefetch lock profile",
            external_account_id="prefetch-lock-profile",
            is_active=True,
        )
        self.order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="PREFETCH-LOCK-ORDER",
            internal_status=FbsOrder.STATUS_PICKED,
        )
        self.batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            assigned_to=self.picker,
            workstation=self.workstation,
            picking_completed_at=timezone.now(),
        )
        FbsPickTask.objects.create(
            batch=self.batch,
            order=self.order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
        )

    @skipUnlessDBFeature("has_select_for_update_nowait")
    def test_prefetch_returns_immediately_when_order_row_is_locked(self):
        locked = Event()
        release = Event()

        def hold_order_lock():
            close_old_connections()
            try:
                with transaction.atomic():
                    FbsOrder.objects.select_for_update().get(pk=self.order.pk)
                    locked.set()
                    release.wait(timeout=5)
            finally:
                close_old_connections()

        thread = Thread(target=hold_order_lock, daemon=True)
        thread.start()
        self.assertTrue(locked.wait(timeout=5), "Order lock was not acquired")
        started_at = monotonic()
        try:
            _prefetch_wb_labels_for_pick_batch(
                batch_id=self.batch.id,
                check_tote_id=0,
                workstation_id=self.workstation.id,
                requested_by=self.controller,
            )
        finally:
            elapsed = monotonic() - started_at
            release.set()
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertLess(elapsed, 1.0)
        self.assertFalse(FbsOrderLabel.objects.filter(order=self.order).exists())
