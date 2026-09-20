"""Regression: a second preassigned cart must not duplicate a shipment flow."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from sku.models import Agency
from employees.models import Employee
from fbs.exceptions import FbsHandoverError, FbsPickingError
from fbs.models import (
    FbsControllerCheckTote, FbsControllerPickTote, FbsControllerSession,
    FbsControllerToteOrder, FbsHandoverBatch, FbsHandoverBox, FbsHandoverOrder,
    FbsHandoverOrderAssignment, FbsIntegrationProfile, FbsOrder, FbsOrderItem,
    FbsOrderLabel, FbsPickBatch, FbsPickTask, FbsPickingCart, FbsToteBinding,
    FbsToteMovement, FbsToteZone, FbsWorkstation,
)
from fbs.services.totes import (
    attach_pick_tote_to_available_check_tote, check_tote_readiness,
    close_controller_check_tote,
)


@override_settings(FBS_MODULE_ENABLED=True, FBS_WAREHOUSE_WRITES_ENABLED=True)
class ExistingShipmentToteTests(TestCase):
    def setUp(self):
        self.controller = get_user_model().objects.create_user(username="route-controller")
        Employee.objects.create(user=self.controller, full_name="Route controller", role="fbs_controller", is_active=True)
        self.picker = get_user_model().objects.create_user(username="route-picker")
        self.agency = Agency.objects.create(agn_name="Route regression client")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency, marketplace=FbsIntegrationProfile.MARKETPLACE_WB, name="Route WB",
            external_account_id="route-wb", external_warehouse_id="route-wh",
        )
        self.desk = FbsWorkstation.objects.create(barcode="FBS-WS-ROUTE", name="Route desk")
        self.service_tote = self.cart("service")
        self.zone = FbsToteZone.objects.create(
            barcode="FBS-ZONE-ROUTE", name="Free route totes", kind=FbsToteZone.KIND_FREE,
        )
        self.session = FbsControllerSession.objects.create(
            controller=self.controller, workstation=self.desk,
            unknown_tote=self.service_tote, problem_tote=self.service_tote,
            canceled_tote=self.service_tote, free_zone=self.zone,
        )
        self.shipment = FbsHandoverBatch.objects.create(
            profile=self.profile, status=FbsHandoverBatch.STATUS_OPEN,
            marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
            external_supply_id="WB-GI-ROUTE", compatibility_key="route:destination:warehouse_sc",
        )
        self.flow = FbsControllerCheckTote.objects.create(
            session=self.session, agency=self.agency, profile=self.profile,
            handover_batch=self.shipment, status=FbsControllerCheckTote.STATUS_COMPOSITION,
            item_qty=15, labeled_qty=15, composition_qty=15, opened_by=self.controller,
        )
        old_batch = self.wave("old", 15)
        self.old_context = FbsControllerPickTote.objects.create(
            session=self.session, check_tote=self.flow, pick_batch=old_batch,
            tote=old_batch.cart, status=FbsControllerPickTote.STATUS_CLOSED,
            planned_qty=15, processed_qty=15,
        )
        self.box = FbsHandoverBox.objects.create(batch=self.shipment, qr_code="ROUTE-BOX")
        for task in old_batch.tasks.select_related("order"):
            order = task.order
            order.internal_status = FbsOrder.STATUS_READY_FOR_HANDOVER
            order.save(update_fields=["internal_status"])
            label = FbsOrderLabel.objects.create(
                order=order, marketplace=self.profile.marketplace,
                barcode=f"LABEL-{order.id}", status=FbsOrderLabel.STATUS_APPLIED,
            )
            FbsControllerToteOrder.objects.create(
                check_tote=self.flow, pick_tote=self.old_context, order=order,
                label=label, status=FbsControllerToteOrder.STATUS_PACKED,
                transport_box=self.box, label_confirmed_by=self.controller,
                composition_checked_by=self.controller, composition_checked_at=timezone.now(),
            )
            FbsHandoverOrder.objects.create(
                box=self.box, order=order, verified_label=label,
                verified_by=self.controller, verified_at=timezone.now(),
            )
        self.incoming = self.wave("incoming", 5)
        self.before_rows = list(self.flow.orders.order_by("id").values())
        self.before_links = list(self.box.orders.order_by("id").values())
        for name in ("_prefetch_wb_labels_for_pick_batch", "_prefetch_ozon_order_barcodes_for_pick_batch"):
            guard = patch(f"fbs.services.totes.{name}")
            guard.start()
            self.addCleanup(guard.stop)

    def cart(self, name):
        return FbsPickingCart.objects.create(barcode=f"FBS-CART-ROUTE-{name.upper()}", name=name)

    def wave(self, name, qty, *, shipment=True):
        wave = FbsPickBatch.objects.create(
            agency=self.agency, status=FbsPickBatch.STATUS_VERIFICATION,
            cart=self.cart(name), workstation=self.desk, assigned_to=self.picker,
            planned_qty=qty, picked_qty=qty, picking_completed_at=timezone.now(),
        )
        for n in range(qty):
            order = FbsOrder.objects.create(
                profile=self.profile, external_order_id=f"{name}-{n}",
                internal_status=FbsOrder.STATUS_PICKED,
            )
            FbsOrderItem.objects.create(order=order, external_line_id="1", external_sku="sku", quantity=1)
            FbsPickTask.objects.create(
                batch=wave, order=order, status=FbsPickTask.STATUS_PICKED,
                planned_qty=1, picked_qty=1,
            )
            if shipment:
                FbsHandoverOrderAssignment.objects.create(
                    batch=self.shipment, order=order, status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
                )
        return wave

    def attach(self, wave=None, actor=None):
        wave = wave or self.incoming
        return attach_pick_tote_to_available_check_tote(
            session_id=self.session.id, pick_tote_scan=wave.cart.barcode,
            performed_by=actor or self.controller,
        )

    def assert_preserved(self):
        self.assertEqual(list(self.flow.orders.order_by("id").values()), self.before_rows)
        self.assertEqual(list(self.box.orders.order_by("id").values()), self.before_links)

    def assert_rejected(self, message):
        counts = (FbsControllerCheckTote.objects.count(), FbsControllerPickTote.objects.count(), FbsToteMovement.objects.count())
        with self.assertRaisesMessage(FbsPickingError, message):
            self.attach()
        self.assertEqual(counts, (FbsControllerCheckTote.objects.count(), FbsControllerPickTote.objects.count(), FbsToteMovement.objects.count()))
        self.assert_preserved()

    def test_existing_15_plus_5_preserves_checks_and_blocks_premature_close(self):
        context = self.attach()
        self.assertEqual(context.check_tote_id, self.flow.id)
        self.flow.refresh_from_db()
        self.assertEqual((self.flow.item_qty, self.flow.labeled_qty, self.flow.composition_qty), (20, 15, 15))
        self.assertEqual(self.flow.status, FbsControllerCheckTote.STATUS_OPEN)
        self.assertEqual(FbsControllerCheckTote.objects.count(), 1)
        self.assertFalse(check_tote_readiness(self.flow).ready)
        self.assert_preserved()
        binding = FbsToteBinding.objects.get(tote=self.incoming.cart)
        self.assertEqual(binding.state, FbsToteBinding.STATE_AT_CONTROL)
        self.assertEqual(binding.pick_batch_id, self.incoming.id)
        self.assertEqual(FbsToteMovement.objects.filter(tote=self.incoming.cart).count(), 1)
        self.incoming.refresh_from_db()
        self.assertEqual(self.incoming.verification_assigned_to_id, self.controller.id)

    def test_repeat_scan_does_not_duplicate_context_quantity_or_audit(self):
        first = self.attach()
        second = self.attach()
        self.assertEqual(first.id, second.id)
        self.flow.refresh_from_db()
        self.assertEqual(self.flow.item_qty, 20)
        self.assertEqual(FbsControllerCheckTote.objects.count(), 1)
        self.assertEqual(FbsToteMovement.objects.filter(tote=self.incoming.cart).count(), 1)
        self.assert_preserved()

    def test_new_unassigned_cart_keeps_separate_flow(self):
        other = self.wave("independent", 1, shipment=False)
        context = self.attach(other)
        self.assertNotEqual(context.check_tote_id, self.flow.id)
        self.assertIsNone(context.check_tote.handover_batch_id)
        self.flow.refresh_from_db()
        self.assertEqual(self.flow.item_qty, 15)
        self.assert_preserved()

    def test_existing_flow_can_be_resumed_at_three_flow_limit(self):
        for _ in range(2):
            FbsControllerCheckTote.objects.create(session=self.session, profile=self.profile, opened_by=self.controller)
        self.assertEqual(self.attach().check_tote_id, self.flow.id)
        self.assertEqual(self.session.check_totes.count(), 3)

    def test_closed_flow_is_rejected_without_creating_duplicate(self):
        self.flow.status = FbsControllerCheckTote.STATUS_CLOSED
        self.flow.save(update_fields=["status"])
        self.assert_rejected("закрыт")

    def test_closed_marketplace_shipment_is_rejected(self):
        self.shipment.marketplace_state = FbsHandoverBatch.MARKETPLACE_COMPLETE
        self.shipment.save(update_fields=["marketplace_state"])
        self.assert_rejected("закрыт")

    def test_wrong_controller_is_rejected(self):
        with self.assertRaisesMessage(FbsPickingError, "другим контролером"):
            self.attach(actor=self.picker)
        self.assert_preserved()

    def test_wrong_workstation_is_rejected(self):
        other = FbsWorkstation.objects.create(barcode="OTHER-DESK", name="Other")
        self.incoming.workstation = other
        self.incoming.save(update_fields=["workstation"])
        self.assert_rejected("другое рабочее место")

    def test_audit_failure_rolls_back_new_context_and_preserves_scans(self):
        with patch("fbs.services.totes.FbsToteMovement.objects.create", side_effect=RuntimeError("audit failed")):
            with self.assertRaisesMessage(RuntimeError, "audit failed"):
                self.attach()
        self.flow.refresh_from_db()
        self.assertEqual(self.flow.item_qty, 15)
        self.assertFalse(FbsControllerPickTote.objects.filter(pick_batch=self.incoming).exists())
        self.assertFalse(FbsToteBinding.objects.filter(tote=self.incoming.cart).exists())
        self.assert_preserved()

    def test_partly_assigned_cart_cannot_extend_checked_shipment(self):
        FbsHandoverOrderAssignment.objects.filter(order=self.incoming.tasks.first().order).delete()
        self.assert_rejected("не закреплены")

    def test_other_session_owns_existing_flow(self):
        other_desk = FbsWorkstation.objects.create(barcode="OTHER-OWNER", name="Other owner")
        other_session = FbsControllerSession.objects.create(
            workstation=other_desk, controller=self.picker, unknown_tote=self.cart("other-service"), free_zone=self.zone,
        )
        self.flow.session = other_session
        self.flow.save(update_fields=["session"])
        self.assert_rejected("другой смене")

    def test_mixed_shipment_cart_is_rejected(self):
        other = FbsHandoverBatch.objects.create(
            profile=self.profile, external_supply_id="WB-GI-OTHER",
            status=FbsHandoverBatch.STATUS_OPEN, marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
        )
        FbsHandoverOrderAssignment.objects.filter(order=self.incoming.tasks.first().order).update(batch=other)
        self.assert_rejected("разных отгрузок")

    def test_wrong_profile_flow_is_rejected(self):
        other = FbsIntegrationProfile.objects.create(
            agency=self.agency, marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="Other account", external_account_id="other-account",
        )
        self.flow.profile = other
        self.flow.save(update_fields=["profile"])
        self.assert_rejected("другому кабинету")

    def test_new_shipment_without_flow_can_be_attached_and_repeated(self):
        other = FbsHandoverBatch.objects.create(
            profile=self.profile, external_supply_id="WB-GI-FIRST-FLOW",
            status=FbsHandoverBatch.STATUS_OPEN, marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
        )
        FbsHandoverOrderAssignment.objects.filter(order_id__in=self.incoming.tasks.values("order_id")).update(batch=other)
        context = self.attach()
        self.assertEqual(context.check_tote.handover_batch_id, other.id)
        self.assertEqual(self.attach().id, context.id)
        self.assertEqual(FbsControllerCheckTote.objects.count(), 2)
        self.assert_preserved()

    def test_new_cart_still_obeys_three_flow_limit(self):
        for _ in range(2):
            FbsControllerCheckTote.objects.create(
                session=self.session, profile=self.profile, opened_by=self.controller,
                status=FbsControllerCheckTote.STATUS_COMPOSITION,
            )
        other = self.wave("no-room", 1, shipment=False)
        with self.assertRaisesMessage(FbsPickingError, "открыты 3 потока"):
            self.attach(other)
        self.assertEqual(self.session.check_totes.count(), 3)

    def test_close_stays_blocked_while_incoming_tote_is_processing(self):
        self.attach()
        with self.assertRaisesMessage(FbsHandoverError, "обработка тары"):
            close_controller_check_tote(check_tote_id=self.flow.id, performed_by=self.controller)
        self.flow.refresh_from_db()
        self.shipment.refresh_from_db()
        self.assertIsNone(self.flow.closed_at)
        self.assertEqual(self.shipment.status, FbsHandoverBatch.STATUS_OPEN)
        self.assert_preserved()

    def test_controller_http_post_redirects_to_incoming_wave(self):
        from fbs.controller_views import controller_home
        request = RequestFactory().post(reverse("fbs:controller_home"), {
            "action": "scan_pick_tote", "pick_tote_scan": self.incoming.cart.barcode,
        })
        request.user = self.controller
        request.session = {}
        with patch("fbs.controller_views._get_or_select_controller_workstation", return_value=self.desk):
            response = controller_home(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("fbs:tsd_pick_verification", kwargs={"batch_id": self.incoming.id}))
        self.assertEqual(FbsControllerPickTote.objects.get(pick_batch=self.incoming).check_tote_id, self.flow.id)
        self.assert_preserved()

    def test_controller_http_post_without_role_never_attaches(self):
        from fbs.controller_views import controller_home
        request = RequestFactory().post(reverse("fbs:controller_home"), {
            "action": "scan_pick_tote", "pick_tote_scan": self.incoming.cart.barcode,
        })
        request.user = self.picker
        request.session = {}
        response = controller_home(request)
        self.assertIn(response.status_code, (302, 403))
        self.assertFalse(FbsControllerPickTote.objects.filter(pick_batch=self.incoming).exists())
