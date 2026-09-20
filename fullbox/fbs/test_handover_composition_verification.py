import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from agent.models import AgentEvent, DeviceAgent
from employees.models import Employee
from processing_app.models import ProcessingPrintJob
from sku.models import Agency

from .controller_session import CONTROLLER_WORKSTATION_SESSION_KEY
from .controller_views import (
    _composition_error_sticker_context,
    controller_check_tote,
)
from .exceptions import FbsHandoverError, FbsIntegrationError, FbsPickingError
from .models import (
    FbsControllerCheckTote,
    FbsControllerPickTote,
    FbsControllerSession,
    FbsControllerToteOrder,
    FbsHandoverBatch,
    FbsHandoverBox,
    FbsHandoverOrder,
    FbsHandoverOrderAssignment,
    FbsIntegrationProfile,
    FbsMarketplaceMetadataTransfer,
    FbsOrder,
    FbsOrderItem,
    FbsOrderLabel,
    FbsPickBatch,
    FbsPickTask,
    FbsPickingCart,
    FbsPickRestockRequest,
    FbsToteBinding,
    FbsToteMovement,
    FbsToteZone,
    FbsWorkstation,
)
from .services import (
    close_handover_box,
    dispatch_handover_batch,
    request_wb_handover_delivery,
    scan_handover_box,
    verify_handover_order_label,
)
from .services.marketplace import schedule_wb_handover_delivery
from .services.handover import (
    approve_handover_verification_override,
    handover_composition_readiness,
    handover_verification_override_status,
    refresh_handover_acceptance,
)
from .services.totes import (
    COMPOSITION_ALREADY_PACKED_MESSAGE,
    COMPOSITION_ITEM_METADATA_PENDING_MESSAGE,
    COMPOSITION_PRODUCT_BARCODE_MESSAGE,
    CompositionProblemToteRoutingRequired,
    check_tote_readiness,
    auto_finalize_trusted_wb_check_tote,
    close_controller_check_tote,
    confirm_check_tote_composition_item,
    confirm_composition_item_in_problem_tote,
    confirm_order_label_to_check_tote,
    confirm_pick_tote_empty,
    mark_pick_tote_awaiting_empty,
)
from .tsd_views import _handover_agent_scan_context, _handover_detail_summary


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_OUTBOX_ENABLED=True,
)
class HandoverCompositionVerificationTests(TestCase):
    def setUp(self):
        self.controller = get_user_model().objects.create_user(
            username="fbs_composition_controller",
            password="pwd",
        )
        Employee.objects.create(
            user=self.controller,
            full_name="FBS composition controller",
            role="fbs_controller",
        )
        self.agency = Agency.objects.create(agn_name="FBS composition client")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB composition test",
            external_warehouse_id="1931120",
            outbox_enabled=True,
        )
        self.batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-GI-COMPOSITION-1",
            compatibility_key="test:destination:pickup_point",
            marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
        )
        self.box = FbsHandoverBox.objects.create(
            batch=self.batch,
            qr_code="WB-BOX-COMPOSITION-1",
        )
        self.order, self.label, self.link = self._linked_order(
            batch=self.batch,
            box=self.box,
            external_order_id="WB-ORDER-1",
            barcode="WB-LABEL-1",
        )

    def _linked_order(self, *, batch, box, external_order_id, barcode):
        order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id=external_order_id,
            internal_status=FbsOrder.STATUS_READY_FOR_HANDOVER,
            marketplace_status="confirm",
        )
        label = FbsOrderLabel.objects.create(
            order=order,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            external_label_id=f"label-{external_order_id}",
            barcode=barcode,
            status=FbsOrderLabel.STATUS_APPLIED,
        )
        FbsHandoverOrderAssignment.objects.create(
            batch=batch,
            order=order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )
        link = FbsHandoverOrder.objects.create(
            box=box,
            order=order,
            added_by=self.controller,
        )
        return order, label, link

    def _ready_batch_for_supply_label_dispatch(self):
        self.link.verified_label = self.label
        self.link.verified_by = self.controller
        self.link.verified_at = timezone.now()
        self.link.save(
            update_fields=["verified_label", "verified_by", "verified_at"]
        )
        self.box.status = FbsHandoverBox.STATUS_SCANNED
        self.box.save(update_fields=["status", "updated_at"])
        self.batch.status = FbsHandoverBatch.STATUS_READY
        self.batch.marketplace_state = FbsHandoverBatch.MARKETPLACE_COMPLETE
        self.batch.supply_qr_code = "WB-SUPPLY-QR-PRINTED"
        self.batch.supply_label_file.name = "handover-labels/wb-supply-printed.png"
        self.batch.save(
            update_fields=[
                "status",
                "marketplace_state",
                "supply_qr_code",
                "supply_label_file",
                "updated_at",
            ]
        )

    def _supply_label_print_job(self, *, status, batch_id=None, card_id=None):
        target_batch_id = self.batch.id if batch_id is None else batch_id
        return ProcessingPrintJob.objects.create(
            status=status,
            order_id=f"FBS-HANDOVER-{target_batch_id}",
            card_id=card_id or f"fbs:handover-supply:{target_batch_id}",
            barcode="WB-SUPPLY-QR-PRINTED",
            requested_by=self.controller.username,
            agent="desktop:test-workstation",
        )

    def _fast_composition_scan(self, *, check_tote, label_scan):
        request = RequestFactory().post(
            f"/fbs/controller/check-totes/{check_tote.id}/",
            data={"action": "scan_composition", "label_scan": label_scan},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        request.user = self.controller
        view = controller_check_tote
        while hasattr(view, "__wrapped__"):
            view = view.__wrapped__
        with mock.patch(
            "employees.access.get_request_role",
            return_value="fbs_controller",
        ), mock.patch(
            "fbs.controller_views.get_request_role",
            return_value="fbs_controller",
        ):
            response = view(request, check_tote_id=check_tote.id)
        return response, json.loads(response.content.decode("utf-8"))

    def _employee_user(self, *, username, role, access_roles=None):
        user = get_user_model().objects.create_user(
            username=username,
            password="pwd",
        )
        Employee.objects.create(
            user=user,
            full_name=username,
            role=role,
            access_roles=access_roles or [],
        )
        return user

    def _controller_tote_order(self, *, status, composition_qty=0):
        workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-990101",
            name="FBS single scan workstation",
        )
        unknown_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-990101",
            name="FBS unknown tote",
        )
        pick_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-990102",
            name="FBS pick tote",
        )
        free_zone = FbsToteZone.objects.create(
            barcode="FBS-TOTE-ZONE-990101",
            name="FBS free tote zone",
            kind=FbsToteZone.KIND_FREE,
        )
        session = FbsControllerSession.objects.create(
            workstation=workstation,
            controller=self.controller,
            unknown_tote=unknown_tote,
            free_zone=free_zone,
        )
        check_tote = FbsControllerCheckTote.objects.create(
            session=session,
            agency=self.agency,
            profile=self.profile,
            handover_batch=self.batch,
            status=FbsControllerCheckTote.STATUS_COMPOSITION,
            item_qty=1,
            labeled_qty=1,
            composition_qty=composition_qty,
            opened_by=self.controller,
        )
        pick_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            assigned_to=self.controller,
            verification_assigned_to=self.controller,
        )
        pick_context = FbsControllerPickTote.objects.create(
            session=session,
            check_tote=check_tote,
            pick_batch=pick_batch,
            tote=pick_tote,
            status=FbsControllerPickTote.STATUS_CLOSED,
            planned_qty=1,
            processed_qty=1,
        )
        tote_order = FbsControllerToteOrder.objects.create(
            check_tote=check_tote,
            pick_tote=pick_context,
            order=self.order,
            label=self.label,
            transport_box=(
                self.box if status == FbsControllerToteOrder.STATUS_PACKED else None
            ),
            status=status,
            units=1,
            label_confirmed_by=self.controller,
            composition_checked_by=(
                self.controller
                if status == FbsControllerToteOrder.STATUS_PACKED
                else None
            ),
        )
        return check_tote, tote_order

    def _additional_controller_tote_order(self, *, session, suffix):
        batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id=f"WB-GI-COMPOSITION-{suffix}",
            compatibility_key=f"test:destination:{suffix}",
            marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
        )
        box = FbsHandoverBox.objects.create(
            batch=batch,
            qr_code=f"WB-BOX-COMPOSITION-{suffix}",
            external_box_id=f"WB-BOX-EXT-COMPOSITION-{suffix}",
        )
        box.label_file.name = f"handover-box-labels/wb-box-{suffix}.pdf"
        box.save(update_fields=["label_file", "updated_at"])
        order, label, _ = self._linked_order(
            batch=batch,
            box=box,
            external_order_id=f"WB-ORDER-{suffix}",
            barcode=f"WB-LABEL-{suffix}",
        )
        check_tote = FbsControllerCheckTote.objects.create(
            session=session,
            agency=self.agency,
            profile=self.profile,
            handover_batch=batch,
            status=FbsControllerCheckTote.STATUS_READY,
            item_qty=1,
            labeled_qty=1,
            opened_by=self.controller,
        )
        pick_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            assigned_to=self.controller,
            verification_assigned_to=self.controller,
        )
        pick_context = FbsControllerPickTote.objects.create(
            session=session,
            check_tote=check_tote,
            pick_batch=pick_batch,
            tote=FbsPickingCart.objects.create(
                barcode=f"FBS-CART-COMPOSITION-{suffix}",
                name=f"FBS pick tote {suffix}",
            ),
            status=FbsControllerPickTote.STATUS_CLOSED,
            planned_qty=1,
            processed_qty=1,
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
        return check_tote, tote_order

    def _processing_pick_tote_order(self):
        check_tote, tote_order = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )
        pick_context = check_tote.pick_totes.get()
        pick_context.status = FbsControllerPickTote.STATUS_PROCESSING
        pick_context.empty_confirmed_at = None
        pick_context.closed_at = None
        pick_context.save(
            update_fields=[
                "status",
                "empty_confirmed_at",
                "closed_at",
                "updated_at",
            ]
        )
        FbsPickTask.objects.create(
            batch=pick_context.pick_batch,
            order=self.order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
        )
        return check_tote, tote_order, pick_context

    def _bind_problem_tote(self, session, *, suffix):
        problem_tote = FbsPickingCart.objects.create(
            barcode=f"FBS-CART-COMPOSITION-PROBLEM-{suffix}",
            name=f"FBS composition problem tote {suffix}",
        )
        session.problem_tote = problem_tote
        session.save(update_fields=["problem_tote"])
        return problem_tote

    def _add_unresolved_marking(self, *, order, suffix):
        order_item = FbsOrderItem.objects.create(
            order=order,
            external_line_id=f"composition-marking-{suffix}",
            external_sku=f"composition-marking-sku-{suffix}",
            barcode=f"4600000{suffix}",
            quantity=1,
        )
        return FbsMarketplaceMetadataTransfer.objects.create(
            order_item=order_item,
            metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
            value=f"MARKING-{suffix}",
            is_required=True,
            status=FbsMarketplaceMetadataTransfer.STATUS_PREPARED,
            idempotency_key=f"composition-marking-{suffix}",
        )

    def test_controller_single_scan_adds_and_verifies_wb_order(self):
        self.box.external_box_id = "WB-BOX-EXT-COMPOSITION-1"
        self.box.label_file.name = "handover-box-labels/wb-box.pdf"
        self.box.save(
            update_fields=["external_box_id", "label_file", "updated_at"]
        )
        check_tote, tote_order = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )

        result = confirm_check_tote_composition_item(
            check_tote_id=check_tote.id,
            label_scan=self.label.barcode,
            performed_by=self.controller,
        )

        result.refresh_from_db()
        check_tote.refresh_from_db()
        self.link.refresh_from_db()
        self.assertEqual(result.id, tote_order.id)
        self.assertEqual(result.status, FbsControllerToteOrder.STATUS_PACKED)
        self.assertEqual(result.transport_box_id, self.box.id)
        self.assertEqual(check_tote.composition_qty, 1)
        self.assertEqual(self.link.verified_label_id, self.label.id)
        self.assertEqual(self.link.verified_by_id, self.controller.id)
        self.assertIsNotNone(self.link.verified_at)

    def test_background_primary_control_still_requires_ready_transport_box(self):
        check_tote, tote_order = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )
        tote_order.primary_order_label_scan_reused = True
        tote_order.save(
            update_fields=["primary_order_label_scan_reused", "updated_at"]
        )

        with self.assertRaisesMessage(FbsHandoverError, "Транспортный короб marketplace еще не готов"):
            auto_finalize_trusted_wb_check_tote(check_tote_id=check_tote.id)

        tote_order.refresh_from_db()
        check_tote.refresh_from_db()
        self.link.refresh_from_db()
        self.assertEqual(
            tote_order.status,
            FbsControllerToteOrder.STATUS_LABELED,
        )
        self.assertIsNone(tote_order.composition_checked_at)
        self.assertIsNone(tote_order.composition_checked_by_id)
        self.assertEqual(check_tote.composition_qty, 0)
        self.assertNotEqual(
            check_tote.status,
            FbsControllerCheckTote.STATUS_CLOSED,
        )
        self.assertIsNone(self.link.verified_at)

    def test_physical_wb_order_label_scan_counts_after_primary_scan(self):
        self.box.external_box_id = "WB-BOX-EXT-PHYSICAL-SCAN"
        self.box.label_file.name = "handover-box-labels/wb-physical-scan.pdf"
        self.box.save(
            update_fields=["external_box_id", "label_file", "updated_at"]
        )
        check_tote, tote_order = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )
        tote_order.primary_order_label_scan_reused = True
        tote_order.save(
            update_fields=["primary_order_label_scan_reused", "updated_at"]
        )

        packed = confirm_check_tote_composition_item(
            check_tote_id=check_tote.id,
            label_scan=self.label.barcode,
            performed_by=self.controller,
        )

        packed.refresh_from_db()
        check_tote.refresh_from_db()
        self.assertEqual(packed.status, FbsControllerToteOrder.STATUS_PACKED)
        self.assertEqual(packed.composition_checked_by_id, self.controller.id)
        self.assertIsNotNone(packed.composition_checked_at)
        self.assertEqual(check_tote.composition_qty, 1)

    def test_another_controller_can_open_verify_and_close_check_tote(self):
        self.box.external_box_id = "WB-BOX-EXT-ANOTHER-CONTROLLER"
        self.box.label_file.name = "handover-box-labels/another-controller.pdf"
        self.box.save(
            update_fields=["external_box_id", "label_file", "updated_at"]
        )
        check_tote, tote_order = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )
        other_controller = self._employee_user(
            username="fbs_other_composition_controller",
            role="fbs_controller",
        )
        request = RequestFactory().get(
            f"/fbs/controller/check-totes/{check_tote.id}/"
        )
        request.user = other_controller
        request.session = {}

        response = controller_check_tote(request, check_tote_id=check_tote.id)
        self.assertEqual(response.status_code, 200)

        result = confirm_check_tote_composition_item(
            check_tote_id=check_tote.id,
            label_scan=self.label.barcode,
            performed_by=other_controller,
        )
        closed = close_controller_check_tote(
            check_tote_id=check_tote.id,
            performed_by=other_controller,
        )

        result.refresh_from_db()
        self.link.refresh_from_db()
        closed.refresh_from_db()
        self.assertEqual(result.id, tote_order.id)
        self.assertEqual(result.composition_checked_by_id, other_controller.id)
        self.assertEqual(self.link.verified_by_id, other_controller.id)
        self.assertEqual(closed.status, FbsControllerCheckTote.STATUS_CLOSED)
        self.assertEqual(closed.closed_by_id, other_controller.id)
        self.assertEqual(closed.session.controller_id, self.controller.id)

    def test_check_tote_service_rejects_employee_without_controller_access(self):
        check_tote, _ = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )
        storekeeper = self._employee_user(
            username="fbs_non_controller_storekeeper",
            role="storekeeper",
        )

        with self.assertRaisesMessage(
            FbsHandoverError,
            "Для проверки отгрузки нужна роль контролера.",
        ):
            confirm_check_tote_composition_item(
                check_tote_id=check_tote.id,
                label_scan=self.label.barcode,
                performed_by=storekeeper,
            )

        check_tote.refresh_from_db()
        self.assertEqual(check_tote.composition_qty, 0)

    def test_unresolved_marking_blocks_only_the_scanned_order(self):
        check_tote, tote_order = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )
        self._add_unresolved_marking(order=self.order, suffix="BLOCKED-ORDER")

        readiness = check_tote_readiness(check_tote)
        self.assertFalse(readiness.ready)
        self.assertTrue(readiness.composition_ready)
        self.assertIn(self.order.id, readiness.blocked_order_ids)
        self.assertFalse(readiness.tote_reasons)

        with self.assertRaisesMessage(
            FbsHandoverError,
            COMPOSITION_ITEM_METADATA_PENDING_MESSAGE,
        ):
            confirm_check_tote_composition_item(
                check_tote_id=check_tote.id,
                label_scan=self.label.barcode,
                performed_by=self.controller,
            )

        tote_order.refresh_from_db()
        check_tote.refresh_from_db()
        self.assertEqual(tote_order.status, FbsControllerToteOrder.STATUS_LABELED)
        self.assertIsNone(tote_order.transport_box_id)
        self.assertEqual(check_tote.composition_qty, 0)

    def test_ready_order_packs_while_another_order_waits_for_marking(self):
        self.box.external_box_id = "WB-BOX-EXT-PER-ITEM-MARKING"
        self.box.label_file.name = "handover-box-labels/wb-per-item-marking.pdf"
        self.box.save(
            update_fields=["external_box_id", "label_file", "updated_at"]
        )
        check_tote, blocked_tote_order = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )
        self._add_unresolved_marking(order=self.order, suffix="MIXED-BLOCKED")
        healthy_order, healthy_label, _ = self._linked_order(
            batch=self.batch,
            box=self.box,
            external_order_id="WB-ORDER-MIXED-HEALTHY",
            barcode="WB-LABEL-MIXED-HEALTHY",
        )
        pick_context = check_tote.pick_totes.get()
        healthy_tote_order = FbsControllerToteOrder.objects.create(
            check_tote=check_tote,
            pick_tote=pick_context,
            order=healthy_order,
            label=healthy_label,
            status=FbsControllerToteOrder.STATUS_LABELED,
            units=1,
            label_confirmed_by=self.controller,
        )
        check_tote.item_qty = 2
        check_tote.labeled_qty = 2
        check_tote.save(update_fields=["item_qty", "labeled_qty", "updated_at"])

        packed = confirm_check_tote_composition_item(
            check_tote_id=check_tote.id,
            label_scan=healthy_label.barcode,
            performed_by=self.controller,
        )

        blocked_tote_order.refresh_from_db()
        packed.refresh_from_db()
        check_tote.refresh_from_db()
        self.assertEqual(packed.id, healthy_tote_order.id)
        self.assertEqual(packed.status, FbsControllerToteOrder.STATUS_PACKED)
        self.assertEqual(
            blocked_tote_order.status,
            FbsControllerToteOrder.STATUS_LABELED,
        )
        self.assertIsNone(blocked_tote_order.transport_box_id)
        self.assertEqual(check_tote.composition_qty, 1)

    def test_close_still_rejects_unresolved_marking(self):
        check_tote, _ = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )
        self._add_unresolved_marking(order=self.order, suffix="CLOSE-BLOCKED")

        with self.assertRaisesMessage(
            FbsHandoverError,
            "Ожидают подтверждения КИЗ или срока: 1.",
        ):
            close_controller_check_tote(
                check_tote_id=check_tote.id,
                performed_by=self.controller,
            )

        check_tote.refresh_from_db()
        self.assertNotEqual(check_tote.status, FbsControllerCheckTote.STATUS_CLOSED)

    def test_composition_scan_routes_to_another_tote_at_same_table(self):
        check_tote, _ = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )
        other_check_tote, other_tote_order = self._additional_controller_tote_order(
            session=check_tote.session,
            suffix="ROUTE-SAME-TABLE",
        )
        physical_check_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-COMPOSITION-ROUTE-SAME",
            name="FBS same table check tote",
        )
        other_check_tote.tote = physical_check_tote
        other_check_tote.save(update_fields=["tote", "updated_at"])

        with self.assertRaisesMessage(
            FbsHandoverError,
            f"Положите товар в тару проверки {physical_check_tote.barcode}",
        ):
            confirm_check_tote_composition_item(
                check_tote_id=check_tote.id,
                label_scan=other_tote_order.label.barcode,
                performed_by=self.controller,
            )

    def test_composition_scan_routes_to_active_shipment_at_another_table(self):
        check_tote, _ = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )
        other_controller = get_user_model().objects.create_user(
            username="fbs_composition_other_table_controller",
        )
        other_workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-COMPOSITION-OTHER-TABLE",
            name="12",
        )
        other_session = FbsControllerSession.objects.create(
            workstation=other_workstation,
            controller=other_controller,
            unknown_tote=FbsPickingCart.objects.create(
                barcode="FBS-CART-COMPOSITION-OTHER-UNKNOWN",
                name="FBS other table unknown tote",
            ),
            free_zone=check_tote.session.free_zone,
        )
        _, other_tote_order = self._additional_controller_tote_order(
            session=other_session,
            suffix="ROUTE-OTHER-TABLE",
        )

        with self.assertRaisesMessage(
            FbsHandoverError,
            (
                "Данный товар относится к отгрузке стола номер 12. "
                "Передайте данный товар сотруднику, работающему за тем столом"
            ),
        ):
            confirm_check_tote_composition_item(
                check_tote_id=check_tote.id,
                label_scan=other_tote_order.label.barcode,
                performed_by=self.controller,
            )

    def test_composition_scan_requests_problem_tote_when_route_is_not_found(self):
        check_tote, _ = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )
        problem_tote = self._bind_problem_tote(
            check_tote.session,
            suffix="ROUTE-NOT-FOUND",
        )
        scan_value = "WB-LABEL-ROUTE-NOT-FOUND"

        with self.assertRaises(CompositionProblemToteRoutingRequired) as raised:
            confirm_check_tote_composition_item(
                check_tote_id=check_tote.id,
                label_scan=scan_value,
                performed_by=self.controller,
            )

        self.assertEqual(
            str(raised.exception),
            (
                "На данном столе нет подходящей тары для этого товара. "
                f"Положите товар в проблемную тару ({problem_tote.barcode})"
            ),
        )
        self.assertEqual(raised.exception.label_scan, scan_value)
        self.assertEqual(
            raised.exception.problem_tote_barcode,
            problem_tote.barcode,
        )

    def test_product_barcode_requires_marketplace_barcode(self):
        check_tote, _ = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )
        product_barcode = "4600000000999"
        FbsOrderItem.objects.create(
            order=self.order,
            external_line_id="composition-product-barcode",
            external_sku="composition-product-sku",
            barcode=product_barcode,
            quantity=1,
        )

        with self.assertRaisesMessage(
            FbsHandoverError,
            COMPOSITION_PRODUCT_BARCODE_MESSAGE,
        ):
            confirm_check_tote_composition_item(
                check_tote_id=check_tote.id,
                label_scan=product_barcode,
                performed_by=self.controller,
            )

    def test_problem_tote_confirmation_records_manual_rebind_with_reason(self):
        check_tote, _ = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )
        problem_tote = self._bind_problem_tote(
            check_tote.session,
            suffix="MANUAL-REBIND",
        )
        scan_value = "WB-LABEL-MANUAL-REBIND"

        movement = confirm_composition_item_in_problem_tote(
            check_tote_id=check_tote.id,
            label_scan=scan_value,
            problem_tote_scan=problem_tote.barcode,
            performed_by=self.controller,
        )
        repeated = confirm_composition_item_in_problem_tote(
            check_tote_id=check_tote.id,
            label_scan=scan_value,
            problem_tote_scan=problem_tote.barcode,
            performed_by=self.controller,
        )

        movement.refresh_from_db()
        self.assertEqual(repeated.id, movement.id)
        self.assertEqual(movement.tote_id, problem_tote.id)
        self.assertEqual(movement.target_kind, "problem_tote")
        self.assertEqual(movement.target_code, problem_tote.barcode)
        self.assertEqual(movement.source_kind, "check_tote")
        self.assertEqual(movement.performed_by_id, self.controller.id)
        self.assertIsNotNone(movement.created_at)
        self.assertTrue(movement.details["manual_decision"])
        self.assertEqual(
            movement.details["reason_code"],
            "marketplace_label_not_found",
        )
        self.assertTrue(movement.details["reason"])
        self.assertEqual(
            FbsToteMovement.objects.filter(
                tote=problem_tote,
                details__label_scan=scan_value,
            ).count(),
            1,
        )

    def test_second_check_tote_can_start_composition_before_first_is_closed(self):
        self.box.external_box_id = "WB-BOX-EXT-COMPOSITION-FIRST"
        self.box.label_file.name = "handover-box-labels/wb-box-first.pdf"
        self.box.save(
            update_fields=["external_box_id", "label_file", "updated_at"]
        )
        first_check_tote, first_tote_order = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )
        first_check_tote.status = FbsControllerCheckTote.STATUS_READY
        first_check_tote.save(update_fields=["status", "updated_at"])
        second_check_tote, second_tote_order = self._additional_controller_tote_order(
            session=first_check_tote.session,
            suffix="SECOND",
        )

        first_packed = confirm_check_tote_composition_item(
            check_tote_id=first_check_tote.id,
            label_scan=first_tote_order.label.barcode,
            performed_by=self.controller,
        )
        first_check_tote.refresh_from_db()
        self.assertEqual(
            first_check_tote.status,
            FbsControllerCheckTote.STATUS_COMPOSITION,
        )

        packed = confirm_check_tote_composition_item(
            check_tote_id=second_check_tote.id,
            label_scan=second_tote_order.label.barcode,
            performed_by=self.controller,
        )

        second_check_tote.refresh_from_db()
        self.assertEqual(packed.id, second_tote_order.id)
        self.assertEqual(packed.status, FbsControllerToteOrder.STATUS_PACKED)
        self.assertEqual(
            second_check_tote.status,
            FbsControllerCheckTote.STATUS_COMPOSITION,
        )
        self.assertEqual(
            packed.transport_box.batch_id,
            second_check_tote.handover_batch_id,
        )
        self.assertNotEqual(
            packed.transport_box_id,
            first_packed.transport_box_id,
        )

    def test_processed_order_packs_while_pick_tote_awaits_empty_confirmation(self):
        self.box.external_box_id = "WB-BOX-EXT-PARTIAL-COMPOSITION"
        self.box.label_file.name = "handover-box-labels/wb-partial-composition.pdf"
        self.box.save(
            update_fields=["external_box_id", "label_file", "updated_at"]
        )
        check_tote, tote_order = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )
        pick_context = check_tote.pick_totes.get()
        pick_context.status = FbsControllerPickTote.STATUS_AWAITING_EMPTY
        pick_context.save(update_fields=["status", "updated_at"])
        readiness = check_tote_readiness(check_tote)
        self.assertTrue(readiness.composition_ready)
        self.assertTrue(readiness.tote_reasons)
        self.assertFalse(readiness.blocked_order_ids)

        packed = confirm_check_tote_composition_item(
            check_tote_id=check_tote.id,
            label_scan=self.label.barcode,
            performed_by=self.controller,
        )

        tote_order.refresh_from_db()
        check_tote.refresh_from_db()
        self.assertEqual(packed.id, tote_order.id)
        self.assertEqual(tote_order.status, FbsControllerToteOrder.STATUS_PACKED)
        self.assertEqual(tote_order.transport_box_id, self.box.id)
        self.assertEqual(check_tote.status, FbsControllerCheckTote.STATUS_COMPOSITION)

        with self.assertRaisesMessage(
            FbsHandoverError,
            "Не подтверждена пустота тары подбора.",
        ):
            close_controller_check_tote(
                check_tote_id=check_tote.id,
                performed_by=self.controller,
            )

        check_tote.refresh_from_db()
        self.assertNotEqual(check_tote.status, FbsControllerCheckTote.STATUS_CLOSED)

    def test_empty_confirmation_unlocks_composition_and_print_dispatch(self):
        check_tote, tote_order, pick_context = self._processing_pick_tote_order()
        self.box.external_box_id = "WB-BOX-EXT-EMPTY-CONFIRMATION"
        self.box.label_file.name = "handover-box-labels/wb-empty-confirmation.pdf"
        self.box.save(
            update_fields=["external_box_id", "label_file", "updated_at"]
        )

        self.assertTrue(
            mark_pick_tote_awaiting_empty(
                pick_batch_id=pick_context.pick_batch_id,
            )
        )
        pick_context.refresh_from_db()
        self.assertEqual(
            pick_context.status,
            FbsControllerPickTote.STATUS_AWAITING_EMPTY,
        )

        confirmed = confirm_pick_tote_empty(
            pick_batch_id=pick_context.pick_batch_id,
            performed_by=self.controller,
        )
        confirmed.refresh_from_db()
        confirmed.pick_batch.refresh_from_db()
        binding = FbsToteBinding.objects.get(tote=confirmed.tote)
        self.assertEqual(confirmed.status, FbsControllerPickTote.STATUS_CLOSED)
        self.assertIsNotNone(confirmed.empty_confirmed_at)
        self.assertIsNotNone(confirmed.closed_at)
        self.assertIsNotNone(confirmed.pick_batch.cart_released_at)
        self.assertEqual(binding.state, FbsToteBinding.STATE_FREE)
        self.assertEqual(binding.zone_id, confirmed.session.free_zone_id)

        packed = confirm_check_tote_composition_item(
            check_tote_id=check_tote.id,
            label_scan=self.label.barcode,
            performed_by=self.controller,
        )
        self.assertEqual(packed.id, tote_order.id)
        self.assertEqual(packed.status, FbsControllerToteOrder.STATUS_PACKED)
        close_controller_check_tote(
            check_tote_id=check_tote.id,
            performed_by=self.controller,
        )
        self.batch.marketplace_state = FbsHandoverBatch.MARKETPLACE_COMPLETE
        self.batch.supply_qr_code = "WB-SUPPLY-QR-EMPTY-CONFIRMATION"
        self.batch.supply_label_file.name = (
            "handover-labels/wb-empty-confirmation.pdf"
        )
        self.batch.save(
            update_fields=[
                "marketplace_state",
                "supply_qr_code",
                "supply_label_file",
                "updated_at",
            ]
        )

        print_job = self._supply_label_print_job(
            status=ProcessingPrintJob.STATUS_PRINTED,
        )
        dispatched = dispatch_handover_batch(
            batch_id=self.batch.id,
            dispatched_by=self.controller,
            supply_label_print_job_id=print_job.id,
        )
        self.assertEqual(dispatched.status, FbsHandoverBatch.STATUS_DISPATCHED)

    def test_wb_ready_batch_cannot_dispatch_before_supply_label_print(self):
        self._ready_batch_for_supply_label_dispatch()

        with self.assertRaisesMessage(
            FbsHandoverError,
            "Сначала распечатайте ШК поставки.",
        ):
            dispatch_handover_batch(
                batch_id=self.batch.id,
                dispatched_by=self.controller,
            )

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, FbsHandoverBatch.STATUS_READY)
        self.assertIsNone(self.batch.dispatched_at)

    def test_printed_supply_label_dispatches_ready_batch(self):
        self._ready_batch_for_supply_label_dispatch()
        print_job = self._supply_label_print_job(
            status=ProcessingPrintJob.STATUS_PRINTED,
        )

        dispatched = dispatch_handover_batch(
            batch_id=self.batch.id,
            dispatched_by=self.controller,
            supply_label_print_job_id=print_job.id,
        )

        self.box.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(dispatched.status, FbsHandoverBatch.STATUS_DISPATCHED)
        self.assertEqual(dispatched.dispatched_by, self.controller)
        self.assertIsNotNone(dispatched.dispatched_at)
        self.assertEqual(self.box.status, FbsHandoverBox.STATUS_DISPATCHED)
        self.assertEqual(self.order.internal_status, FbsOrder.STATUS_HANDED_OVER)

    def test_failed_supply_label_print_cannot_dispatch_batch(self):
        self._ready_batch_for_supply_label_dispatch()
        print_job = self._supply_label_print_job(
            status=ProcessingPrintJob.STATUS_FAILED,
        )

        with self.assertRaisesMessage(
            FbsHandoverError,
            "Принтер еще не подтвердил печать ШК поставки.",
        ):
            dispatch_handover_batch(
                batch_id=self.batch.id,
                dispatched_by=self.controller,
                supply_label_print_job_id=print_job.id,
            )

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, FbsHandoverBatch.STATUS_READY)

    def test_supply_label_print_for_another_batch_cannot_dispatch(self):
        self._ready_batch_for_supply_label_dispatch()
        print_job = self._supply_label_print_job(
            status=ProcessingPrintJob.STATUS_PRINTED,
            card_id="fbs:handover-supply:999999",
        )

        with self.assertRaisesMessage(
            FbsHandoverError,
            "Задание печати относится к другой поставке.",
        ):
            dispatch_handover_batch(
                batch_id=self.batch.id,
                dispatched_by=self.controller,
                supply_label_print_job_id=print_job.id,
            )

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, FbsHandoverBatch.STATUS_READY)

    def test_empty_confirmation_rejects_another_controller(self):
        _, _, pick_context = self._processing_pick_tote_order()
        self.assertTrue(
            mark_pick_tote_awaiting_empty(
                pick_batch_id=pick_context.pick_batch_id,
            )
        )
        other_controller = get_user_model().objects.create_user(
            username="fbs_other_empty_confirmation_controller",
        )

        with self.assertRaisesMessage(
            FbsPickingError,
            "Тару подбора обрабатывает другой контролер.",
        ):
            confirm_pick_tote_empty(
                pick_batch_id=pick_context.pick_batch_id,
                performed_by=other_controller,
            )

        pick_context.refresh_from_db()
        pick_context.pick_batch.refresh_from_db()
        self.assertEqual(
            pick_context.status,
            FbsControllerPickTote.STATUS_AWAITING_EMPTY,
        )
        self.assertIsNone(pick_context.empty_confirmed_at)
        self.assertIsNone(pick_context.closed_at)
        self.assertIsNone(pick_context.pick_batch.cart_released_at)

    def test_empty_confirmation_rejects_unprocessed_orders(self):
        _, _, pick_context = self._processing_pick_tote_order()
        second_order, _, _ = self._linked_order(
            batch=self.batch,
            box=self.box,
            external_order_id="WB-ORDER-EMPTY-CONFIRMATION-MISSING",
            barcode="WB-LABEL-EMPTY-CONFIRMATION-MISSING",
        )
        FbsPickTask.objects.create(
            batch=pick_context.pick_batch,
            order=second_order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
        )

        self.assertFalse(
            mark_pick_tote_awaiting_empty(
                pick_batch_id=pick_context.pick_batch_id,
            )
        )
        with self.assertRaisesMessage(
            FbsPickingError,
            "В таре числится необработанных заказов: 1.",
        ):
            confirm_pick_tote_empty(
                pick_batch_id=pick_context.pick_batch_id,
                performed_by=self.controller,
            )

        pick_context.refresh_from_db()
        pick_context.pick_batch.refresh_from_db()
        self.assertEqual(
            pick_context.status,
            FbsControllerPickTote.STATUS_PROCESSING,
        )
        self.assertIsNone(pick_context.empty_confirmed_at)
        self.assertIsNone(pick_context.closed_at)
        self.assertIsNone(pick_context.pick_batch.cart_released_at)

    def test_empty_confirmation_is_idempotent_after_close(self):
        _, _, pick_context = self._processing_pick_tote_order()
        self.assertTrue(
            mark_pick_tote_awaiting_empty(
                pick_batch_id=pick_context.pick_batch_id,
            )
        )
        first = confirm_pick_tote_empty(
            pick_batch_id=pick_context.pick_batch_id,
            performed_by=self.controller,
        )

        with mock.patch("fbs.services.totes._move_tote") as move_tote:
            second = confirm_pick_tote_empty(
                pick_batch_id=pick_context.pick_batch_id,
                performed_by=self.controller,
            )

        move_tote.assert_not_called()
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.status, FbsControllerPickTote.STATUS_CLOSED)
        self.assertEqual(second.empty_confirmed_at, first.empty_confirmed_at)
        self.assertEqual(second.closed_at, first.closed_at)

    def test_order_label_scan_only_adds_order_to_logical_check_tote(self):
        self.link.delete()
        self.box.external_box_id = "WB-BOX-EXT-DIRECT-SCAN"
        self.box.label_file.name = "handover-box-labels/wb-direct-scan.pdf"
        self.box.save(
            update_fields=["external_box_id", "label_file", "updated_at"]
        )
        self.order.internal_status = FbsOrder.STATUS_PICKED
        self.order.save(update_fields=["internal_status", "updated_at"])
        self.label.status = FbsOrderLabel.STATUS_READY
        self.label.save(update_fields=["status", "updated_at"])

        workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-990201",
            name="FBS direct scan workstation",
        )
        unknown_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-990201",
            name="FBS direct scan unknown tote",
        )
        pick_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-990202",
            name="FBS direct scan pick tote",
        )
        free_zone = FbsToteZone.objects.create(
            barcode="FBS-TOTE-ZONE-990201",
            name="FBS direct scan free tote zone",
            kind=FbsToteZone.KIND_FREE,
        )
        session = FbsControllerSession.objects.create(
            workstation=workstation,
            controller=self.controller,
            unknown_tote=unknown_tote,
            free_zone=free_zone,
        )
        check_tote = FbsControllerCheckTote.objects.create(
            session=session,
            agency=self.agency,
            profile=self.profile,
            handover_batch=self.batch,
            status=FbsControllerCheckTote.STATUS_OPEN,
            item_qty=1,
            opened_by=self.controller,
        )
        pick_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            assigned_to=self.controller,
            verification_assigned_to=self.controller,
        )
        FbsPickTask.objects.create(
            batch=pick_batch,
            order=self.order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
        )
        pick_context = FbsControllerPickTote.objects.create(
            session=session,
            check_tote=check_tote,
            pick_batch=pick_batch,
            tote=pick_tote,
            status=FbsControllerPickTote.STATUS_PROCESSING,
            planned_qty=1,
        )

        def confirm_label(**kwargs):
            label = FbsOrderLabel.objects.get(pk=kwargs["label_id"])
            label.status = FbsOrderLabel.STATUS_APPLIED
            label.save(update_fields=["status", "updated_at"])
            FbsOrder.objects.filter(pk=label.order_id).update(
                internal_status=FbsOrder.STATUS_READY_FOR_HANDOVER
            )
            return label

        with mock.patch(
            "fbs.services.labels.confirm_order_label_scan",
            side_effect=confirm_label,
        ):
            result = confirm_order_label_to_check_tote(
                label_id=self.label.id,
                label_scan=self.label.barcode,
                pick_batch_id=pick_batch.id,
                performed_by=self.controller,
            )

        result.refresh_from_db()
        check_tote.refresh_from_db()
        pick_context.refresh_from_db()
        self.assertEqual(result.status, FbsControllerToteOrder.STATUS_LABELED)
        self.assertIsNone(result.transport_box_id)
        self.assertEqual(check_tote.labeled_qty, 1)
        self.assertEqual(check_tote.composition_qty, 0)
        self.assertEqual(pick_context.processed_qty, 1)
        self.assertFalse(FbsHandoverOrder.objects.filter(order=self.order).exists())

    def test_packed_controller_order_rescan_repairs_wb_verification_once(self):
        check_tote, tote_order = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_PACKED,
            composition_qty=1,
        )

        first = confirm_check_tote_composition_item(
            check_tote_id=check_tote.id,
            label_scan=self.label.barcode,
            performed_by=self.controller,
        )
        second = confirm_check_tote_composition_item(
            check_tote_id=check_tote.id,
            label_scan=self.label.barcode,
            performed_by=self.controller,
        )

        check_tote.refresh_from_db()
        self.link.refresh_from_db()
        self.assertEqual(first.id, tote_order.id)
        self.assertEqual(second.id, tote_order.id)
        self.assertEqual(
            first.composition_scan_message,
            COMPOSITION_ALREADY_PACKED_MESSAGE,
        )
        self.assertEqual(
            second.composition_scan_message,
            COMPOSITION_ALREADY_PACKED_MESSAGE,
        )
        self.assertEqual(check_tote.composition_qty, 1)
        self.assertEqual(self.link.verified_label_id, self.label.id)
        self.assertIsNotNone(self.link.verified_at)

    def test_fast_composition_scan_returns_compact_success_payload(self):
        self.box.external_box_id = "WB-BOX-EXT-FAST-SCAN"
        self.box.label_file.name = "handover-box-labels/wb-fast-scan.pdf"
        self.box.save(
            update_fields=["external_box_id", "label_file", "updated_at"]
        )
        check_tote, tote_order = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )

        response, payload = self._fast_composition_scan(
            check_tote=check_tote,
            label_scan=self.label.barcode,
        )

        tote_order.refresh_from_db()
        check_tote.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["scan_state"], "accepted")
        self.assertEqual(payload["sticker_number"], self.label.external_label_id)
        self.assertEqual(payload["order_number"], self.order.external_order_id)
        self.assertEqual(payload["box_code"], self.box.qr_code)
        self.assertEqual(payload["scanned_count"], 1)
        self.assertEqual(payload["total_count"], 1)
        self.assertTrue(payload["all_scanned"])
        self.assertEqual(tote_order.status, FbsControllerToteOrder.STATUS_PACKED)
        self.assertEqual(check_tote.composition_qty, 1)

    def test_controller_error_uses_sticker_as_primary_identifier(self):
        check_tote, _ = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )

        context = _composition_error_sticker_context(
            check_tote,
            (
                f"Заказ {self.order.external_order_id}: "
                "не завершена проверка на рабочем столе."
            ),
        )

        self.assertEqual(context["sticker_number"], self.label.external_label_id)
        self.assertEqual(context["order_number"], self.order.external_order_id)
        self.assertIn(f"Стикер {self.label.external_label_id}", context["error"])
        self.assertIn(f"заказ {self.order.external_order_id}", context["error"])

    def test_fast_composition_scan_returns_nonblocking_duplicate_without_increment(self):
        check_tote, tote_order = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_PACKED,
            composition_qty=1,
        )

        response, payload = self._fast_composition_scan(
            check_tote=check_tote,
            label_scan=self.label.barcode,
        )

        tote_order.refresh_from_db()
        check_tote.refresh_from_db()
        self.assertEqual(response.status_code, 409)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["scan_state"], "duplicate")
        self.assertEqual(payload["title"], "ПОВТОР ТОВАРА")
        self.assertFalse(payload["blocking"])
        self.assertEqual(payload["instruction"], "")
        self.assertEqual(tote_order.status, FbsControllerToteOrder.STATUS_PACKED)
        self.assertEqual(check_tote.composition_qty, 1)

    def test_fast_composition_scan_blocks_order_from_another_check_tote(self):
        check_tote, tote_order = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )
        other_check_tote, other_tote_order = self._additional_controller_tote_order(
            session=check_tote.session,
            suffix="FAST-FOREIGN",
        )

        response, payload = self._fast_composition_scan(
            check_tote=check_tote,
            label_scan=other_tote_order.label.barcode,
        )

        tote_order.refresh_from_db()
        other_tote_order.refresh_from_db()
        check_tote.refresh_from_db()
        other_check_tote.refresh_from_db()
        self.assertEqual(response.status_code, 409)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["scan_state"], "foreign_order")
        self.assertTrue(payload["blocking"])
        self.assertEqual(tote_order.status, FbsControllerToteOrder.STATUS_LABELED)
        self.assertEqual(
            other_tote_order.status,
            FbsControllerToteOrder.STATUS_LABELED,
        )
        self.assertEqual(check_tote.composition_qty, 0)
        self.assertEqual(other_check_tote.composition_qty, 0)

    def test_controller_tote_close_requires_actual_wb_label_scan(self):
        check_tote, _ = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_PACKED,
            composition_qty=1,
        )

        with self.assertRaisesMessage(
            FbsHandoverError,
            "Не отсканировано WB-этикеток: 1.",
        ):
            close_controller_check_tote(
                check_tote_id=check_tote.id,
                performed_by=self.controller,
            )

    def test_exact_wb_label_is_persisted_as_composition_verification(self):
        with CaptureQueriesContext(connection) as queries:
            result = verify_handover_order_label(
                batch_id=self.batch.id,
                order_label_scan=self.label.barcode,
                verified_by=self.controller,
            )

        result.refresh_from_db()
        self.assertEqual(result.id, self.link.id)
        self.assertEqual(result.verified_label_id, self.label.id)
        self.assertEqual(result.verified_by_id, self.controller.id)
        self.assertIsNotNone(result.verified_at)
        handover_lock_queries = [
            query["sql"]
            for query in queries.captured_queries
            if 'FROM "fbs_handover_order"' in query["sql"]
        ]
        self.assertTrue(handover_lock_queries)
        self.assertFalse(
            any(
                'LEFT OUTER JOIN "fbs_order_label"' in sql
                for sql in handover_lock_queries
            )
        )

    def test_handover_composition_modal_receives_controller_agent_scans(self):
        agent = DeviceAgent.objects.create(agent_id="fbs-composition-agent")
        workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-990099",
            name="FBS composition workstation",
            device_agent=agent,
        )
        event = AgentEvent.objects.create(
            agent_id=agent.agent_id,
            event_type=AgentEvent.EVENT_SCAN,
            payload={"value": "PREVIOUS-SCAN", "source": "com"},
        )
        request = RequestFactory().get("/fbs/tsd/storekeeper/handover/1/")
        request.user = self.controller
        request.session = {CONTROLLER_WORKSTATION_SESSION_KEY: workstation.id}

        with self.assertNumQueries(2), mock.patch(
            "fbs.tsd_views.get_request_role", return_value="fbs_controller"
        ), mock.patch(
            "fbs.tsd_views.reverse",
            return_value="/fbs/controller/scan-events/",
        ):
            context = _handover_agent_scan_context(request)

        self.assertEqual(
            context,
            {
                "agent_scan_poll_url": "/fbs/controller/scan-events/",
                "agent_scan_event_id": event.id,
            },
        )

    def test_repeated_scan_is_idempotent(self):
        first = verify_handover_order_label(
            batch_id=self.batch.id,
            order_label_scan=self.label.barcode,
            verified_by=self.controller,
        )
        second = verify_handover_order_label(
            batch_id=self.batch.id,
            order_label_scan=self.label.barcode,
            verified_by=self.controller,
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(FbsHandoverOrder.objects.filter(order=self.order).count(), 1)

    def test_reconciliation_is_available_before_full_dispatch_readiness(self):
        context = _handover_detail_summary(self.batch)

        self.assertFalse(context["handover_composition_ready"])
        self.assertTrue(context["handover_reconciliation_available"])
        self.assertEqual(context["handover_reconciliation_total_count"], 1)
        self.assertEqual(context["handover_reconciliation_missing_count"], 1)
        self.assertFalse(context["handover_scan_complete"])

    def test_label_from_another_handover_is_rejected(self):
        other_batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-GI-COMPOSITION-2",
            compatibility_key="other:destination:pickup_point",
            marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
        )
        other_box = FbsHandoverBox.objects.create(
            batch=other_batch,
            qr_code="WB-BOX-COMPOSITION-2",
        )
        _, other_label, _ = self._linked_order(
            batch=other_batch,
            box=other_box,
            external_order_id="WB-ORDER-2",
            barcode="WB-LABEL-2",
        )

        with self.assertRaisesMessage(
            FbsHandoverError,
            "другой поставке WB-GI-COMPOSITION-2",
        ):
            verify_handover_order_label(
                batch_id=self.batch.id,
                order_label_scan=other_label.barcode,
                verified_by=self.controller,
            )
        self.link.refresh_from_db()
        self.assertIsNone(self.link.verified_at)

    def test_remote_supply_conflict_blocks_composition_verification(self):
        assignment = FbsHandoverOrderAssignment.objects.get(order=self.order)
        assignment.status = FbsHandoverOrderAssignment.STATUS_ERROR
        assignment.error = (
            "Не ОК: заказ уехал в другую поставку WB «Поставка из кабинета WB» "
            "(WB-GI-OTHER-1). В текущей поставке подтверждать его нельзя."
        )
        assignment.save(update_fields=["status", "error", "updated_at"])

        with self.assertRaisesMessage(FbsHandoverError, "WB-GI-OTHER-1"):
            verify_handover_order_label(
                batch_id=self.batch.id,
                order_label_scan=self.label.barcode,
                verified_by=self.controller,
            )

        self.link.refresh_from_db()
        self.assertIsNone(self.link.verified_at)

    def test_box_cannot_close_or_scan_before_composition_verification(self):
        with self.assertRaisesMessage(FbsHandoverError, "Контрольный скан"):
            close_handover_box(box_id=self.box.id)

        self.box.status = FbsHandoverBox.STATUS_CLOSED
        self.box.save(update_fields=["status", "updated_at"])
        with self.assertRaisesMessage(FbsHandoverError, "Контрольный скан"):
            scan_handover_box(
                batch_id=self.batch.id,
                box_qr_scan=self.box.qr_code,
                scanned_by=self.controller,
            )

    def test_verified_order_allows_box_close_and_scan(self):
        verify_handover_order_label(
            batch_id=self.batch.id,
            order_label_scan=self.label.barcode,
            verified_by=self.controller,
        )

        close_handover_box(box_id=self.box.id)
        scan_handover_box(
            batch_id=self.batch.id,
            box_qr_scan=self.box.qr_code,
            scanned_by=self.controller,
        )

        self.box.refresh_from_db()
        self.batch.refresh_from_db()
        self.assertEqual(self.box.status, FbsHandoverBox.STATUS_SCANNED)
        self.assertEqual(self.batch.status, FbsHandoverBatch.STATUS_READY)

    def test_delivered_verified_order_allows_box_close(self):
        verify_handover_order_label(
            batch_id=self.batch.id,
            order_label_scan=self.label.barcode,
            verified_by=self.controller,
        )
        self.order.internal_status = FbsOrder.STATUS_DELIVERED
        self.order.marketplace_status = "complete"
        self.order.marketplace_substatus = "sold"
        self.order.save(
            update_fields=[
                "internal_status",
                "marketplace_status",
                "marketplace_substatus",
                "updated_at",
            ]
        )

        close_handover_box(box_id=self.box.id)

        self.box.refresh_from_db()
        self.assertEqual(self.box.status, FbsHandoverBox.STATUS_CLOSED)
        self.order.refresh_from_db()
        self.assertEqual(self.order.internal_status, FbsOrder.STATUS_DELIVERED)

    def test_unready_box_error_names_order_status_and_action(self):
        verify_handover_order_label(
            batch_id=self.batch.id,
            order_label_scan=self.label.barcode,
            verified_by=self.controller,
        )
        self.order.internal_status = FbsOrder.STATUS_PICKED
        self.order.save(update_fields=["internal_status", "updated_at"])

        with self.assertRaisesMessage(
            FbsHandoverError,
            "Заказ WB-ORDER-1 не готов к передаче: статус «Отобран»",
        ) as raised:
            close_handover_box(box_id=self.box.id)

        self.assertIn("исключите его и выполните возврат товара", str(raised.exception))

    def test_wb_downstream_statuses_confirm_handover_acceptance(self):
        for marketplace_substatus in ("ready_for_pickup", "sold"):
            with self.subTest(marketplace_substatus=marketplace_substatus):
                self.batch.status = FbsHandoverBatch.STATUS_DISPATCHED
                self.batch.accepted_at = None
                self.batch.save(update_fields=["status", "accepted_at", "updated_at"])
                self.box.status = FbsHandoverBox.STATUS_DISPATCHED
                self.box.accepted_at = None
                self.box.save(update_fields=["status", "accepted_at", "updated_at"])
                self.order.internal_status = FbsOrder.STATUS_DELIVERED
                self.order.marketplace_status = "complete"
                self.order.marketplace_substatus = marketplace_substatus
                self.order.save(
                    update_fields=[
                        "internal_status",
                        "marketplace_status",
                        "marketplace_substatus",
                        "updated_at",
                    ]
                )

                refresh_handover_acceptance(batch_id=self.batch.id)

                self.batch.refresh_from_db()
                self.box.refresh_from_db()
                self.assertEqual(self.batch.status, FbsHandoverBatch.STATUS_ACCEPTED)
                self.assertEqual(self.box.status, FbsHandoverBox.STATUS_ACCEPTED)

    def test_wb_delivery_is_blocked_without_composition_verification(self):
        self.box.status = FbsHandoverBox.STATUS_SCANNED
        self.box.save(update_fields=["status", "updated_at"])
        self.batch.status = FbsHandoverBatch.STATUS_READY
        self.batch.save(update_fields=["status", "updated_at"])

        with self.assertRaisesMessage(FbsHandoverError, "Контрольный скан"):
            request_wb_handover_delivery(
                batch_id=self.batch.id,
                requested_by=self.controller,
            )

        with self.assertRaisesMessage(FbsIntegrationError, "контрольный скан"):
            schedule_wb_handover_delivery(
                batch_id=self.batch.id,
                requested_by=self.controller,
            )

    def test_staged_problem_order_keeps_final_wb_delivery_gate(self):
        self.link.verified_label = self.label
        self.link.verified_by = self.controller
        self.link.verified_at = timezone.now()
        self.link.save(
            update_fields=["verified_label", "verified_by", "verified_at"]
        )
        problem_order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="WB-ORDER-STAGED-RETURN",
            internal_status=FbsOrder.STATUS_EXCEPTION,
            marketplace_status="cancel",
        )
        problem_assignment = FbsHandoverOrderAssignment.objects.create(
            batch=self.batch,
            order=problem_order,
            status=FbsHandoverOrderAssignment.STATUS_CANCELED,
        )
        pick_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            picking_completed_at=timezone.now(),
        )
        request = FbsPickRestockRequest.objects.create(
            batch=pick_batch,
            order=problem_order,
            handover_assignment=problem_assignment,
            source_tote=FbsPickingCart.objects.create(
                barcode="FBS-CART-STAGED-WB-RETURN",
                name="Staged WB return",
            ),
            status=FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
            reason_code=FbsPickRestockRequest.REASON_MARKETPLACE,
            reason="Controller staged order while WB readback continues",
            marketplace_action=FbsPickRestockRequest.MARKETPLACE_ACTION_VERIFY_CANCEL,
            planned_qty=1,
            created_by=self.controller,
        )

        waiting = handover_composition_readiness(self.batch)

        self.assertFalse(waiting.ready)
        self.assertIn(
            "Не завершен возврат проблемных заказов: 1.",
            waiting.reasons,
        )

        request.status = FbsPickRestockRequest.STATUS_QUEUED
        request.marketplace_confirmed_at = timezone.now()
        request.save(
            update_fields=["status", "marketplace_confirmed_at", "updated_at"]
        )

        confirmed = handover_composition_readiness(self.batch)
        self.assertTrue(confirmed.ready)

    def test_active_pick_tote_blocks_delivery_and_dispatch_after_all_assigned_orders(self):
        check_tote, _ = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_PACKED,
            composition_qty=1,
        )
        pick_context = check_tote.pick_totes.get()
        pick_context.status = FbsControllerPickTote.STATUS_PROCESSING
        pick_context.planned_qty = 2
        pick_context.processed_qty = 1
        pick_context.save(
            update_fields=["status", "planned_qty", "processed_qty", "updated_at"]
        )
        self.link.verified_label = self.label
        self.link.verified_by = self.controller
        self.link.verified_at = self.batch.created_at
        self.link.save(
            update_fields=["verified_label", "verified_by", "verified_at"]
        )
        self.box.status = FbsHandoverBox.STATUS_CLOSED
        self.box.save(update_fields=["status", "updated_at"])
        with self.assertRaisesMessage(
            FbsHandoverError,
            "Не завершены тары подбора контролера: 1",
        ):
            scan_handover_box(
                batch_id=self.batch.id,
                box_qr_scan=self.box.qr_code,
                scanned_by=self.controller,
            )
        self.box.refresh_from_db()
        self.batch.refresh_from_db()
        self.assertEqual(self.box.status, FbsHandoverBox.STATUS_CLOSED)
        self.assertEqual(self.batch.status, FbsHandoverBatch.STATUS_OPEN)

        self.box.status = FbsHandoverBox.STATUS_SCANNED
        self.box.save(update_fields=["status", "updated_at"])
        self.batch.status = FbsHandoverBatch.STATUS_READY
        self.batch.marketplace_state = FbsHandoverBatch.MARKETPLACE_COMPLETE
        self.batch.supply_qr_code = "WB-SUPPLY-QR"
        self.batch.supply_label_file.name = "handover-labels/wb-supply.pdf"
        self.batch.save(
            update_fields=[
                "status",
                "marketplace_state",
                "supply_qr_code",
                "supply_label_file",
                "updated_at",
            ]
        )

        for operation in (
            lambda: request_wb_handover_delivery(
                batch_id=self.batch.id,
                requested_by=self.controller,
            ),
            lambda: dispatch_handover_batch(
                batch_id=self.batch.id,
                dispatched_by=self.controller,
            ),
        ):
            with self.assertRaisesMessage(
                FbsHandoverError,
                "Не завершены тары подбора контролера: 1",
            ):
                operation()

    @mock.patch("fbs.services.marketplace.schedule_wb_handover_delivery")
    def test_verified_wb_delivery_confirms_box_without_second_box_scan(
        self,
        schedule_delivery,
    ):
        self.box.label_file.name = "handover-box-labels/wb-box.pdf"
        self.box.save(update_fields=["label_file", "updated_at"])
        workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-990100",
            name="FBS delivery workstation",
            active_handover_box=self.box,
        )
        verify_handover_order_label(
            batch_id=self.batch.id,
            order_label_scan=self.label.barcode,
            verified_by=self.controller,
        )
        schedule_delivery.return_value = mock.sentinel.command

        result = request_wb_handover_delivery(
            batch_id=self.batch.id,
            requested_by=self.controller,
        )

        self.assertIs(result, mock.sentinel.command)
        self.box.refresh_from_db()
        self.batch.refresh_from_db()
        workstation.refresh_from_db()
        self.assertEqual(self.box.status, FbsHandoverBox.STATUS_SCANNED)
        self.assertEqual(self.box.scanned_by_id, self.controller.id)
        self.assertIsNotNone(self.box.scanned_at)
        self.assertEqual(self.batch.status, FbsHandoverBatch.STATUS_READY)
        self.assertIsNone(workstation.active_handover_box_id)
        schedule_delivery.assert_called_once_with(
            batch_id=self.batch.id,
            requested_by=self.controller,
        )

    @mock.patch("fbs.services.marketplace.schedule_wb_handover_delivery")
    def test_empty_wb_box_still_blocks_delivery(self, schedule_delivery):
        verify_handover_order_label(
            batch_id=self.batch.id,
            order_label_scan=self.label.barcode,
            verified_by=self.controller,
        )
        FbsHandoverBox.objects.create(
            batch=self.batch,
            qr_code="WB-BOX-COMPOSITION-EMPTY",
            label_file="handover-box-labels/wb-empty.pdf",
        )

        with self.assertRaisesMessage(FbsHandoverError, "Пустой короб"):
            request_wb_handover_delivery(
                batch_id=self.batch.id,
                requested_by=self.controller,
            )

        schedule_delivery.assert_not_called()

    def test_ozon_box_flow_is_not_changed(self):
        ozon_profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            name="Ozon composition control",
            external_warehouse_id="ozon-warehouse",
        )
        ozon_batch = FbsHandoverBatch.objects.create(
            profile=ozon_profile,
            marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
        )
        ozon_box = FbsHandoverBox.objects.create(
            batch=ozon_batch,
            qr_code="OZON-BOX-COMPOSITION-1",
        )
        ozon_order = FbsOrder.objects.create(
            profile=ozon_profile,
            external_order_id="OZON-ORDER-1",
            internal_status=FbsOrder.STATUS_READY_FOR_HANDOVER,
        )
        FbsHandoverOrder.objects.create(
            box=ozon_box,
            order=ozon_order,
            added_by=self.controller,
        )

        close_handover_box(box_id=ozon_box.id)
        scan_handover_box(
            batch_id=ozon_batch.id,
            box_qr_scan=ozon_box.qr_code,
            scanned_by=self.controller,
        )

        ozon_box.refresh_from_db()
        self.assertEqual(ozon_box.status, FbsHandoverBox.STATUS_SCANNED)

    def _handover_override_employee(self, *, username, access_roles):
        user = get_user_model().objects.create_user(
            username=username,
            password="pwd",
        )
        Employee.objects.create(
            user=user,
            full_name=username,
            role="manager",
            access_roles=access_roles,
        )
        self.batch.compatibility_key = "test:destination:warehouse_sc"
        self.batch.save(update_fields=["compatibility_key", "updated_at"])
        return user

    def test_delegated_head_manager_can_approve_handover_override(self):
        delegated_head = self._handover_override_employee(
            username="delegated_handover_head",
            access_roles=["head_manager"],
        )

        override = approve_handover_verification_override(
            batch_id=self.batch.id,
            reason="Служебное разрешение начальника склада",
            approved_by=delegated_head,
        )

        self.assertEqual(override.approved_by, delegated_head)
        self.assertEqual(
            override.reason,
            "Служебное разрешение начальника склада",
        )

    def test_head_manager_can_materialize_exact_primary_scan_composition(self):
        delegated_head = self._handover_override_employee(
            username="delegated_primary_scan_head",
            access_roles=["head_manager"],
        )
        self.link.delete()
        check_tote, tote_order = self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )

        override = approve_handover_verification_override(
            batch_id=self.batch.id,
            reason="Подтвержден первичный скан всей поставки",
            approved_by=delegated_head,
        )

        link = FbsHandoverOrder.objects.get(order=self.order)
        self.assertEqual(link.box, self.box)
        self.assertEqual(link.added_by, delegated_head)
        self.assertIsNone(link.verified_at)
        self.assertIsNone(link.verified_label_id)
        self.assertEqual(override.approved_by, delegated_head)
        check_tote.refresh_from_db()
        tote_order.refresh_from_db()
        self.assertEqual(
            check_tote.status,
            FbsControllerCheckTote.STATUS_COMPOSITION,
        )
        self.assertEqual(
            tote_order.status,
            FbsControllerToteOrder.STATUS_LABELED,
        )
        summary = _handover_detail_summary(self.batch)
        self.assertEqual(summary["handover_unboxed_order_count"], 0)
        self.assertEqual(summary["handover_missing_order_count"], 0)
        self.assertEqual(
            summary["handover_next_action"].code,
            "deliver_marketplace",
        )

    def test_primary_scan_composition_requires_exact_active_order_set(self):
        self._handover_override_employee(
            username="delegated_primary_scan_mismatch_head",
            access_roles=["head_manager"],
        )
        self.link.delete()
        self._controller_tote_order(
            status=FbsControllerToteOrder.STATUS_LABELED,
        )
        _, _, extra_link = self._linked_order(
            batch=self.batch,
            box=self.box,
            external_order_id="WB-ORDER-NOT-SCANNED",
            barcode="WB-LABEL-NOT-SCANNED",
        )
        extra_link.delete()

        active_override, blocker = handover_verification_override_status(self.batch)

        self.assertIsNone(active_override)
        self.assertIn("точный состав активных заказов", blocker)
        self.assertFalse(FbsHandoverOrder.objects.filter(box=self.box).exists())

    def test_manager_without_delegated_role_cannot_approve_handover_override(self):
        manager = self._handover_override_employee(
            username="ordinary_handover_manager",
            access_roles=[],
        )

        with self.assertRaisesMessage(
            FbsHandoverError,
            "доступна только начальнику склада",
        ):
            approve_handover_verification_override(
                batch_id=self.batch.id,
                reason="Недопустимая попытка",
                approved_by=manager,
            )

    def test_handover_override_status_returns_exact_blocker(self):
        assignment = FbsHandoverOrderAssignment.objects.get(order=self.order)
        assignment.status = FbsHandoverOrderAssignment.STATUS_PENDING
        assignment.save(update_fields=["status", "updated_at"])
        self.batch.compatibility_key = "test:destination:warehouse_sc"
        self.batch.save(update_fields=["compatibility_key", "updated_at"])

        active_override, blocker = handover_verification_override_status(self.batch)

        self.assertIsNone(active_override)
        self.assertIn("не подтвержден в поставке WB", blocker)

    def test_delegated_head_manager_sees_handover_override_action(self):
        delegated_head = self._handover_override_employee(
            username="delegated_handover_head_ui",
            access_roles=["head_manager"],
        )
        self.client.force_login(delegated_head)

        response = self.client.get(
            f"/fbs/tsd/storekeeper/handover/{self.batch.id}/"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Разрешить передачу без повторной проверки")
        self.assertContains(response, 'name="action" value="approve_verification_override"')
