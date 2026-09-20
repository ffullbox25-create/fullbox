"""Forward-only shipment isolation; real orders and marketplace APIs are never used."""
from unittest.mock import patch

from django.db import transaction

from fbs.exceptions import FbsHandoverError, FbsPickingError
from fbs.models import (
    FbsControllerCheckTote, FbsControllerPickTote, FbsHandoverBatch,
    FbsHandoverBox, FbsHandoverOrderAssignment, FbsIntegrationProfile,
    FbsControllerToteOrder, FbsOrder, FbsOrderLabel, FbsPickTask,
    FbsMarketplaceCommand,
)
from fbs.integrations.contracts import WB_READ_HANDOVER_SUPPLIES
from fbs.integrations.http import MarketplaceHttpResponse
from fbs.services.handover import (
    _prefetch_wb_order_handover_assignment, add_order_to_handover_box,
    create_handover_batch, ensure_order_handover_assignment,
    ensure_wb_order_handover_assignment, tote_handover_compatibility_key,
)
from fbs.services.marketplace import (
    _recovery_compatibility_key, _recover_missing_wb_handover_supply,
    _schedule_controller_wb_box_if_ready,
)
from fbs.services.shipment_policy import is_tote_shipment, shipment_pick_batch_id
from fbs.services.totes import (
    _locked_active_check_tote_orders,
    active_check_tote_orders,
)
from fbs.test_tote_existing_shipment import ExistingShipmentToteTests


class OneToteShipmentTests(ExistingShipmentToteTests):
    # Also inherit the 18 legacy two-cart regressions: rollout must not move
    # their orders, erase their scans, or recreate duplicate controller flows.
    def assign(self, wave, n=0, **kwargs):
        order = wave.tasks.order_by("id")[n].order
        return ensure_order_handover_assignment(
            order_id=order.id, pick_batch_id=wave.id, assigned_by=self.controller,
            **kwargs,
        )

    def ozon(self, *waves):
        profile = FbsIntegrationProfile.objects.create(
            agency=self.agency, marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            name="One tote Ozon", external_account_id="one-tote-ozon",
            external_warehouse_id="one-tote-wh",
        )
        for wave in waves:
            FbsOrder.objects.filter(id__in=wave.tasks.values("order_id")).update(profile=profile)
        return profile

    def test_two_new_wb_carts_do_not_join_each_other_or_old_open_supply(self):
        a = self.wave("new-a", 2, shipment=False)
        b = self.wave("new-b", 1, shipment=False)
        one, two = self.assign(a), self.assign(b)
        self.assertEqual(self.assign(a, 1).batch_id, one.batch_id)
        self.assertEqual(len({one.batch_id, two.batch_id, self.shipment.id}), 3)
        self.assertEqual(shipment_pick_batch_id(one.batch), a.id)
        self.assertEqual(shipment_pick_batch_id(two.batch), b.id)

    def test_ozon_is_also_isolated_without_controller_flow(self):
        a = self.wave("ozon-a", 2, shipment=False)
        b = self.wave("ozon-b", 1, shipment=False)
        self.ozon(a, b)
        one, two = self.assign(a), self.assign(b)
        self.assertNotEqual(one.batch_id, two.batch_id)
        self.assertEqual(self.assign(a, 1).batch_id, one.batch_id)
        self.assertEqual(one.batch.boxes.count(), 1)
        self.assertEqual(one.status, FbsHandoverOrderAssignment.STATUS_CONFIRMED)

    def test_background_then_controller_use_same_supply(self):
        wave = self.wave("before-flow", 2, shipment=False)
        first = self.assign(wave, workstation_id=self.desk.id)
        context = self.attach(wave)
        second = self.assign(wave, 1, check_tote_id=context.check_tote_id)
        self.assertEqual(first.batch_id, second.batch_id)
        context.check_tote.refresh_from_db()
        self.assertEqual(context.check_tote.handover_batch_id, first.batch_id)

    def test_new_totes_have_separate_flows_and_shipments(self):
        a = self.wave("flow-a", 1, shipment=False)
        b = self.wave("flow-b", 1, shipment=False)
        ca, cb = self.attach(a), self.attach(b)
        aa = self.assign(a, check_tote_id=ca.check_tote_id)
        ab = self.assign(b, check_tote_id=cb.check_tote_id)
        self.assertNotEqual(ca.check_tote_id, cb.check_tote_id)
        self.assertNotEqual(aa.batch_id, ab.batch_id)
        self.assertEqual(self.attach(a).id, ca.id)

    def test_workstation_change_does_not_fragment_one_cart(self):
        wave = self.wave("desk-change", 2, shipment=False)
        one = self.assign(wave, workstation_id=self.desk.id)
        two = self.assign(wave, 1, workstation_id=999999)
        self.assertEqual(one.batch_id, two.batch_id)

    def test_reused_physical_cart_has_new_shipment_for_new_wave(self):
        a = self.wave("reuse-a", 1, shipment=False)
        b = self.wave("reuse-b", 1, shipment=False)
        b.cart = a.cart
        b.save(update_fields=["cart"])
        self.assertNotEqual(self.assign(a).batch_id, self.assign(b).batch_id)

    def test_missing_optional_wave_resolved_from_order(self):
        wave = self.wave("infer", 1, shipment=False)
        order = wave.tasks.first().order
        assignment = ensure_wb_order_handover_assignment(order_id=order.id)
        self.assertEqual(shipment_pick_batch_id(assignment.batch), wave.id)
        self.assertEqual(ensure_order_handover_assignment(order_id=order.id).id, assignment.id)

    def test_wrong_explicit_wave_cannot_create_shipment(self):
        a = self.wave("wrong-a", 1, shipment=False)
        b = self.wave("wrong-b", 1, shipment=False)
        before = FbsHandoverBatch.objects.count()
        with self.assertRaisesMessage(FbsHandoverError, "другой волне"):
            ensure_order_handover_assignment(order_id=a.tasks.first().order_id, pick_batch_id=b.id)
        self.assertEqual(FbsHandoverBatch.objects.count(), before)

    def test_missing_wave_does_not_fall_back_to_shared_workstation(self):
        order = FbsOrder.objects.create(profile=self.profile, external_order_id="no-wave", internal_status="picked")
        with self.assertRaisesMessage(FbsHandoverError, "нет волны"):
            ensure_order_handover_assignment(order_id=order.id, workstation_id=self.desk.id)
        self.assertFalse(FbsHandoverOrderAssignment.objects.filter(order=order).exists())

    def test_remaining_legacy_wave_orders_stay_in_original_shipment(self):
        wave = self.wave("part-legacy", 2, shipment=False)
        first_order = wave.tasks.first().order
        FbsHandoverOrderAssignment.objects.create(batch=self.shipment, order=first_order)
        original_key = self.shipment.compatibility_key
        second = self.assign(wave, 1)
        self.assertEqual(second.batch_id, self.shipment.id)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.compatibility_key, original_key)
        self.assertFalse(is_tote_shipment(self.shipment))

    def test_repeat_assignment_does_not_duplicate(self):
        wave = self.wave("repeat", 1, shipment=False)
        first = self.assign(wave)
        self.assertEqual(first.id, self.assign(wave).id)
        self.assertEqual(first.batch.order_assignments.count(), 1)

    def test_nowait_prefetch_and_normal_assignment_agree(self):
        wave = self.wave("nowait", 2, shipment=False)
        first = _prefetch_wb_order_handover_assignment(
            order_id=wave.tasks.first().order_id, pick_batch_id=wave.id,
        )
        self.assertEqual(first.batch_id, self.assign(wave, 1).batch_id)

    def test_partial_strict_prefetch_can_finish_in_existing_flow(self):
        wave = self.wave("partial-strict", 2, shipment=False)
        first = self.assign(wave)
        flow = FbsControllerCheckTote.objects.create(
            session=self.session, profile=self.profile, agency=self.agency,
            handover_batch=first.batch, status=FbsControllerCheckTote.STATUS_OPEN,
            opened_by=self.controller,
        )
        context = self.attach(wave)
        self.assertEqual(context.check_tote_id, flow.id)
        self.assertEqual(self.assign(wave, 1, check_tote_id=flow.id).batch_id, first.batch_id)

    def test_foreign_flow_rejected_without_assignment(self):
        wave = self.wave("foreign-flow", 1, shipment=False)
        with self.assertRaisesMessage(FbsHandoverError, "другая тара"):
            self.assign(wave, check_tote_id=self.flow.id)
        self.assertFalse(FbsHandoverOrderAssignment.objects.filter(order_id=wave.tasks.first().order_id).exists())

    def test_new_owner_guard_rejects_cross_wave_assignment(self):
        a = self.wave("owner-a", 1, shipment=False)
        b = self.wave("owner-b", 1, shipment=False)
        first = self.assign(a)
        FbsHandoverOrderAssignment.objects.create(batch=first.batch, order=b.tasks.first().order)
        with self.assertRaisesMessage(FbsPickingError, "другой тарой"):
            self.attach(b)

    def test_closed_supply_cannot_create_second_shipment_for_same_cart(self):
        wave = self.wave("closed-owner", 2, shipment=False)
        first = self.assign(wave)
        first.batch.status = FbsHandoverBatch.STATUS_READY
        first.batch.save(update_fields=["status"])
        before = FbsHandoverBatch.objects.count()
        with self.assertRaisesMessage(FbsHandoverError, "закрыта"):
            self.assign(wave, 1)
        self.assertEqual(FbsHandoverBatch.objects.count(), before)

    def test_incompatible_wb_destinations_do_not_silently_split_or_merge(self):
        wave = self.wave("destinations", 2, shipment=False)
        self.assign(wave)
        other = wave.tasks.order_by("id")[1].order
        other.raw_payload = {"officeId": "different-office"}
        other.save(update_fields=["raw_payload"])
        before = FbsHandoverBatch.objects.count()
        with self.assertRaisesMessage(FbsHandoverError, "несовместимые"):
            self.assign(wave, 1)
        self.assertEqual(FbsHandoverBatch.objects.count(), before)

    def test_recovery_keeps_owner_key(self):
        wave = self.wave("recovery", 1, shipment=False)
        assignment = self.assign(wave)
        self.assertEqual(
            _recovery_compatibility_key(batch=assignment.batch, assignment=assignment),
            assignment.batch.compatibility_key,
        )

    def test_api_payload_refresh_cannot_erase_local_owner(self):
        wave = self.wave("payload", 1, shipment=False)
        assignment = self.assign(wave)
        assignment.batch.marketplace_payload = {"id": "WB-CHANGED"}
        assignment.batch.save(update_fields=["marketplace_payload"])
        assignment.batch.refresh_from_db()
        self.assertEqual(shipment_pick_batch_id(assignment.batch), wave.id)

    def test_manual_shipment_binds_first_wave_then_rejects_another(self):
        a = self.wave("manual-a", 2, shipment=False)
        b = self.wave("manual-b", 1, shipment=False)
        profile = self.ozon(a, b)
        batch = create_handover_batch(profile=profile, external_supply_id="MANUAL-NEW")
        box = FbsHandoverBox.objects.create(batch=batch, qr_code="MANUAL-BOX")
        labels = []
        for wave in (a, b):
            for task in wave.tasks.all():
                order = task.order
                order.internal_status = FbsOrder.STATUS_READY_FOR_HANDOVER
                order.save(update_fields=["internal_status"])
                labels.append(FbsOrderLabel.objects.create(
                    order=order, marketplace=profile.marketplace,
                    barcode=f"MANUAL-{order.id}", status=FbsOrderLabel.STATUS_APPLIED,
                ))
        for label in labels[:2]:
            add_order_to_handover_box(box_id=box.id, order_label_scan=label.barcode, added_by=self.controller)
        with self.assertRaisesMessage(FbsHandoverError, "другой тарой"):
            add_order_to_handover_box(box_id=box.id, order_label_scan=labels[-1].barcode, added_by=self.controller)
        batch.refresh_from_db()
        self.assertEqual(shipment_pick_batch_id(batch), a.id)
        self.assertEqual(box.orders.count(), 2)

    def test_reopening_manual_legacy_supply_does_not_opt_it_into_new_policy(self):
        before = self.shipment.compatibility_key
        batch = create_handover_batch(profile=self.profile, external_supply_id=self.shipment.external_supply_id)
        self.assertEqual(batch.id, self.shipment.id)
        self.assertEqual(batch.compatibility_key, before)

    def test_canceled_task_does_not_override_current_wave(self):
        wave = self.wave("current", 1, shipment=False)
        old = self.wave("canceled-task", 1, shipment=False)
        FbsPickTask.objects.create(
            batch=old, order=wave.tasks.first().order,
            status=FbsPickTask.STATUS_CANCELED, planned_qty=1,
        )
        self.assertEqual(shipment_pick_batch_id(self.assign(wave).batch), wave.id)

    def test_full_missing_supply_recovery_keeps_all_orders_in_one_owned_replacement(self):
        wave = self.wave("missing-supply", 2, shipment=False)
        first, second = self.assign(wave), self.assign(wave, 1)
        source = first.batch
        source.external_supply_id = "WB-MISSING-ONE-TOTE"
        source.save(update_fields=["external_supply_id"])
        command = FbsMarketplaceCommand.objects.create(
            profile=self.profile, order=first.order, handover_batch=source,
            command_type=WB_READ_HANDOVER_SUPPLIES, http_method="GET",
            endpoint="/test/read-only-supplies", endpoint_version="v3",
            idempotency_key="missing-one-tote", payload_hash="test-only",
            status=FbsMarketplaceCommand.STATUS_SENT,
        )
        response = MarketplaceHttpResponse(status_code=200, headers={}, content=b"", json_payload={})
        with patch("fbs.services.marketplace.schedule_wb_handover_supply") as schedule:
            _recover_missing_wb_handover_supply(command.id, response)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertNotEqual(first.batch_id, source.id)
        self.assertEqual(first.batch_id, second.batch_id)
        self.assertEqual(first.batch.compatibility_key, source.compatibility_key)
        self.assertEqual(self.assign(wave).batch_id, first.batch_id)
        schedule.assert_called_once_with(batch_id=first.batch_id)

    def test_limit_does_not_silently_allocate_second_supply(self):
        wave = self.wave("full-supply", 2, shipment=False)
        first = self.assign(wave)
        manager_type = type(first.batch.order_assignments)
        before = FbsHandoverBatch.objects.count()
        with patch.object(manager_type, "count", return_value=1000):
            with self.assertRaisesMessage(FbsHandoverError, "лимит 1000"):
                self.assign(wave, 1)
        self.assertEqual(FbsHandoverBatch.objects.count(), before)

    def test_delivery_facts_are_idempotent_and_do_not_charge_without_tariff(self):
        from billing.fbs_services import sync_fbs_delivery_to_billing
        from billing.models import BillingService, WarehouseServiceFact, ApplicationCharge

        BillingService.objects.create(code="fbs_delivery", name="Test delivery", unit="рейс")
        a = self.wave("bill-a", 1, shipment=False)
        b = self.wave("bill-b", 1, shipment=False)
        for wave in (a, b):
            batch = self.assign(wave).batch
            batch.status = FbsHandoverBatch.STATUS_ACCEPTED
            batch.save(update_fields=["status"])
            _, first = sync_fbs_delivery_to_billing(batch=batch)
            _, repeated = sync_fbs_delivery_to_billing(batch=batch)
            self.assertEqual(first[0].id, repeated[0].id)
            self.assertEqual(first[0].quantity, 1)
        self.assertEqual(WarehouseServiceFact.objects.filter(service__code="fbs_delivery").count(), 2)
        self.assertFalse(ApplicationCharge.objects.filter(service__code="fbs_delivery").exists())

    def test_second_manual_supply_for_same_wave_is_rejected(self):
        wave = self.wave("duplicate-manual", 1, shipment=False)
        profile = self.ozon(wave)
        self.assign(wave)
        order = wave.tasks.first().order
        order.internal_status = FbsOrder.STATUS_READY_FOR_HANDOVER
        order.save(update_fields=["internal_status"])
        label = FbsOrderLabel.objects.create(
            order=order, marketplace=profile.marketplace, barcode="DUPLICATE-MANUAL",
            status=FbsOrderLabel.STATUS_APPLIED,
        )
        batch = create_handover_batch(profile=profile, external_supply_id="SECOND-MANUAL")
        box = FbsHandoverBox.objects.create(batch=batch, qr_code="SECOND-MANUAL-BOX")
        with self.assertRaisesMessage(FbsHandoverError, "уже есть отгрузка"):
            add_order_to_handover_box(box_id=box.id, order_label_scan=label.barcode, added_by=self.controller)
        self.assertFalse(box.orders.exists())

    def confirmed_supply(self, suffix):
        wave = self.wave(suffix, 1, shipment=False)
        assignment = self.assign(wave)
        assignment.batch.external_supply_id = f"WB-ONE-TOTE-{suffix}"
        assignment.batch.save(update_fields=["external_supply_id"])
        assignment.status = FbsHandoverOrderAssignment.STATUS_CONFIRMED
        assignment.save(update_fields=["status"])
        return assignment.batch

    def test_wave_owned_warehouse_supply_gets_one_internal_box_without_workstation_key(self):
        batch = self.confirmed_supply("warehouse-box")
        self.assertNotIn(":workstation:", batch.compatibility_key)
        _schedule_controller_wb_box_if_ready(batch_id=batch.id)
        _schedule_controller_wb_box_if_ready(batch_id=batch.id)
        self.assertEqual(batch.boxes.count(), 1)
        self.assertTrue(batch.boxes.first().qr_code.startswith("FBS-WB-GI-BOX-"))

    def test_wave_owned_pickup_supply_schedules_marketplace_box(self):
        batch = self.confirmed_supply("pickup-box")
        batch.compatibility_key = batch.compatibility_key.replace(":destination:warehouse_sc", ":destination:pickup_point")
        batch.save(update_fields=["compatibility_key"])
        with patch("fbs.services.marketplace.schedule_wb_handover_boxes") as schedule:
            _schedule_controller_wb_box_if_ready(batch_id=batch.id)
        schedule.assert_called_once_with(batch_id=batch.id, amount=1)
        self.assertFalse(batch.boxes.exists())

    def test_new_policy_keeps_marketplace_confirmation_gate_for_box(self):
        batch = self.confirmed_supply("pending-box")
        batch.order_assignments.update(status=FbsHandoverOrderAssignment.STATUS_PENDING)
        _schedule_controller_wb_box_if_ready(batch_id=batch.id)
        self.assertFalse(batch.boxes.exists())

    def test_active_tote_rows_lock_through_distinct_subquery(self):
        wave = self.wave("trusted-lock", 1, shipment=False)
        pick_tote = self.attach(wave)
        order = wave.tasks.first().order
        self.assign(wave, check_tote_id=pick_tote.check_tote_id)
        label = FbsOrderLabel.objects.create(
            order=order,
            marketplace=self.profile.marketplace,
            barcode="TRUSTED-LOCK-LABEL",
            status=FbsOrderLabel.STATUS_APPLIED,
        )
        tote_order = FbsControllerToteOrder.objects.create(
            check_tote=pick_tote.check_tote,
            pick_tote=pick_tote,
            order=order,
            label=label,
            label_confirmed_by=self.controller,
            primary_order_label_scan_reused=True,
        )

        self.assertEqual(
            list(
                active_check_tote_orders(pick_tote.check_tote).values_list(
                    "id", flat=True
                )
            ),
            [tote_order.id],
        )
        with transaction.atomic():
            locked_ids = list(
                _locked_active_check_tote_orders(pick_tote.check_tote)
                .values_list("id", flat=True)
            )
        self.assertEqual(locked_ids, [tote_order.id])
