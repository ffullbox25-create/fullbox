from collections import Counter
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from audit.models import OrderAuditEntry
from sku.models import Agency, SKU
from sklad.models import WarehouseLocation
from employees.models import Employee
from fbs.exceptions import FbsPickingError
from fbs.models import (
    FbsBox, FbsIntegrationProfile, FbsOrder, FbsOrderItem,
    FbsOrderStockAllocation, FbsPickBatch, FbsPickTask, FbsStockBalance, FbsPallet, FbsStorageCell,
)
from fbs.services.picking import (
    _location_grouping_priority, _ordered_pick_batch_chunks, create_pick_batches,
    order_by_pick_priority, pick_wave_destination_choices, prepare_pick_queue,
    FbsQueueStockConfirmationRequired, FbsQueueStockShortage,
)


@override_settings(FBS_MODULE_ENABLED=True, FBS_WAREHOUSE_WRITES_ENABLED=True,
                   FBS_STATUS_PULL_ENABLED=False, FBS_OUTBOX_ENABLED=False)
class ManualWaveChoiceTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(username="wave-choice-manager")
        Employee.objects.create(user=self.actor, full_name="Test wave manager", role="head_manager", is_active=True)
        self.agency = Agency.objects.create(agn_name="Wave choice test client")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency, marketplace="ozon", name="Test cabinet",
            external_warehouse_id="wave-choice-test",
        )
        self.sku = SKU.objects.create(agency=self.agency, sku_code="CHOICE", name="Test item")
        location = WarehouseLocation.objects.create(location_code="WAVE-CHOICE-LOCATION", zone_code="OS")
        cell = FbsStorageCell.objects.create(cell_code="WAVE-CHOICE-CELL", location=location)
        pallet = FbsPallet.objects.create(agency=self.agency, cell=cell, pallet_code="WAVE-CHOICE-PALLET")
        self.box = FbsBox.objects.create(agency=self.agency, pallet=pallet, box_code="WAVE-CHOICE-BOX")
        self.balance = FbsStockBalance.objects.create(
            agency=self.agency, box=self.box, sku_ref=self.sku, identity_key="choice",
            qty=1000, available_qty=500, reserved_qty=500,
        )
        self.counter = 0
        self.factory = RequestFactory()

    def order(self, qty=1, profile=None):
        self.counter += 1
        order = FbsOrder.objects.create(
            profile=profile or self.profile, external_order_id=f"CHOICE-{self.counter}",
            internal_status="reserved", marketplace_status="awaiting_packaging",
        )
        item = FbsOrderItem.objects.create(
            order=order, external_line_id=str(self.counter), sku=self.sku,
            quantity=qty, barcode="4600000000010", product_name="Test item",
        )
        FbsOrderStockAllocation.objects.create(
            order_item=item, balance=self.balance, qty_reserved=qty, status="reserved",
        )
        return order

    def wave(self, count=1, qty=1):
        orders = [self.order(qty) for _ in range(count)]
        return create_pick_batches(order_ids=[o.id for o in orders],
                                   max_orders_per_batch=100, max_units_per_batch=100)[0]

    def create(self, orders, **kwargs):
        return create_pick_batches(
            order_ids=[o.id for o in orders], max_orders_per_batch=100,
            max_units_per_batch=100, created_by=self.actor, **kwargs,
        )

    def test_new_wave_does_not_touch_existing_wave(self):
        old = self.wave()
        before = list(old.tasks.values_list("id", "sort_order", "planned_qty"))
        new = self.create([self.order()], reuse_queued_batches=False)[0]
        self.assertNotEqual(old.id, new.id)
        self.assertEqual(list(old.tasks.values_list("id", "sort_order", "planned_qty")), before)

    def test_selected_wave_only_not_oldest(self):
        oldest = self.wave()
        selected = self.wave()
        order = self.order(3)
        result = self.create([order], target_batch_id=selected.id)
        self.assertEqual([b.id for b in result], [selected.id])
        self.assertEqual(oldest.tasks.count(), 1)
        selected.refresh_from_db()
        self.assertEqual(selected.planned_qty, 4)
        order.refresh_from_db()
        self.assertEqual(order.internal_status, "queued_for_pick")
        self.assertTrue(OrderAuditEntry.objects.filter(
            order_type="fbs_order", order_id=str(order.id), user=self.actor,
        ).exists())
        self.balance.refresh_from_db()
        self.assertEqual((self.balance.qty, self.balance.available_qty, self.balance.reserved_qty), (1000, 500, 500))

    def test_original_50_plus_33_auto_reuse_regression(self):
        old = self.wave(count=50)
        orders = [self.order() for _ in range(33)]
        result = self.create(orders, reuse_queued_batches=True)
        self.assertEqual([b.id for b in result], [old.id])
        old.refresh_from_db()
        self.assertEqual((old.tasks.count(), old.planned_qty), (83, 83))

    def test_partial_auto_reuse_creates_remainder(self):
        old = self.wave(count=1, qty=98)
        result = self.create([self.order() for _ in range(3)], reuse_queued_batches=True)
        self.assertEqual(len(result), 2)
        old.refresh_from_db()
        self.assertEqual(old.planned_qty, 100)
        self.assertEqual(result[1].planned_qty, 1)

    def test_auto_queue_full_can_still_complete_reuse(self):
        old = self.wave()
        result = self.create([self.order()], reuse_queued_batches=True, max_new_batches=0)
        self.assertEqual([b.id for b in result], [old.id])
        self.assertEqual(FbsPickBatch.objects.count(), 1)

    def test_selected_overflow_does_not_create_another_wave_or_partially_append(self):
        old = self.wave(qty=99)
        order = self.order(2)
        audit_before = OrderAuditEntry.objects.count()
        with self.assertRaisesMessage(FbsPickingError, "недостаточно места"):
            self.create([order], target_batch_id=old.id)
        old.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(old.planned_qty, 99)
        self.assertEqual(order.internal_status, "reserved")
        self.assertEqual(FbsPickBatch.objects.count(), 1)
        self.assertFalse(FbsPickTask.objects.filter(order=order).exists())
        self.assertEqual(OrderAuditEntry.objects.count(), audit_before)

    def test_choices_require_capacity_for_entire_selection(self):
        old = self.wave(qty=99)
        one = self.order()
        two = self.order(2)
        self.assertEqual([row["id"] for row in pick_wave_destination_choices(order_ids=[one.id])], [old.id])
        self.assertEqual(pick_wave_destination_choices(order_ids=[two.id]), ())

    def test_claimed_after_preview_is_rejected(self):
        old = self.wave()
        order = self.order()
        self.assertTrue(pick_wave_destination_choices(order_ids=[order.id]))
        old.assigned_to = self.actor
        old.save(update_fields=["assigned_to"])
        self.assertEqual(pick_wave_destination_choices(order_ids=[order.id]), ())
        with self.assertRaisesMessage(FbsPickingError, "уже взята в работу"):
            self.create([order], target_batch_id=old.id)
        self.assertFalse(FbsPickTask.objects.filter(order=order).exists())

    def test_capacity_changed_after_preview_rejects_entire_addition(self):
        old = self.wave(qty=98)
        order = self.order(2)
        self.assertTrue(pick_wave_destination_choices(order_ids=[order.id]))
        self.create([self.order()], target_batch_id=old.id)
        with self.assertRaisesMessage(FbsPickingError, "недостаточно места"):
            self.create([order], target_batch_id=old.id)
        old.refresh_from_db()
        self.assertEqual(old.planned_qty, 99)
        self.assertFalse(FbsPickTask.objects.filter(order=order).exists())

    def test_audit_failure_rolls_back_tasks_allocations_status_and_batch(self):
        old = self.wave()
        order = self.order(2)
        with patch("fbs.services.picking.log_order_bulk_transition", side_effect=RuntimeError("audit failed")):
            with self.assertRaisesMessage(RuntimeError, "audit failed"):
                self.create([order], target_batch_id=old.id)
        order.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual(order.internal_status, "reserved")
        self.assertEqual(old.planned_qty, 1)
        self.assertFalse(FbsPickTask.objects.filter(order=order).exists())
        self.assertFalse(FbsOrderStockAllocation.objects.filter(order_item__order=order, pick_task__isnull=False).exists())

    def test_another_client_and_missing_wave_are_rejected(self):
        old = self.wave()
        other = Agency.objects.create(agn_name="Unrelated client")
        old.agency = other
        old.save(update_fields=["agency"])
        order = self.order()
        for target in [old.id, 999999]:
            with self.subTest(target=target), self.assertRaisesMessage(FbsPickingError, "недоступна"):
                self.create([order], target_batch_id=target)
        self.assertFalse(FbsPickTask.objects.filter(order=order).exists())

    def test_new_large_selection_splits_without_touching_old_wave(self):
        old = self.wave(qty=20)
        result = self.create([self.order(60), self.order(60)], reuse_queued_batches=False)
        self.assertEqual(len(result), 2)
        self.assertNotIn(old.id, [b.id for b in result])
        old.refresh_from_db()
        self.assertEqual(old.planned_qty, 20)

    def test_started_task_excludes_wave_even_if_batch_status_is_stale(self):
        old = self.wave()
        old.tasks.update(status="in_progress")
        order = self.order()
        self.assertEqual(pick_wave_destination_choices(order_ids=[order.id]), ())
        with self.assertRaisesMessage(FbsPickingError, "взятые в работу"):
            self.create([order], target_batch_id=old.id)

    def test_another_cabinet_is_not_accepted(self):
        old = self.wave()
        other = FbsIntegrationProfile.objects.create(
            agency=self.agency, marketplace="wb", name="Second cabinet", external_warehouse_id="other",
        )
        order = self.order(profile=other)
        order.marketplace_status = "new"
        order.marketplace_substatus = "waiting"
        order.save()
        self.assertEqual(pick_wave_destination_choices(order_ids=[order.id]), ())
        with self.assertRaises(FbsPickingError):
            self.create([order], target_batch_id=old.id)

    def test_repeat_confirm_does_not_duplicate_tasks(self):
        old = self.wave()
        order = self.order()
        options = dict(order_ids=[order.id], reuse_queued_batches=False, target_batch_id=old.id)
        first = prepare_pick_queue(**options)
        second = prepare_pick_queue(**options)
        self.assertEqual((first.tasks_created, second.tasks_created), (1, 0))
        self.assertEqual(FbsPickTask.objects.filter(order=order).count(), 1)

    def test_empty_profiles_preserve_other_clients_and_age_order(self):
        older = self.order()
        newer = self.order()
        allocations = {o.id: [SimpleNamespace(qty_reserved=1)] for o in (older, newer)}
        chunks = _ordered_pick_batch_chunks(
            orders_by_profile={1: [], 2: [newer], 3: [older]}, allocations_by_order=allocations,
            max_orders_per_batch=50, max_units_per_batch=50,
        )
        self.assertEqual([chunk[0].id for _, _, chunk in chunks], [older.id, newer.id])

    def test_database_priority_uses_deadline_then_order_age(self):
        older_without_deadline = self.order()
        overdue_newer = self.order()
        now = timezone.now()
        FbsOrder.objects.filter(pk=older_without_deadline.pk).update(
            ordered_at=now - timedelta(days=2),
            cutoff_at=None,
        )
        FbsOrder.objects.filter(pk=overdue_newer.pk).update(
            ordered_at=now - timedelta(days=1),
            cutoff_at=now - timedelta(days=3),
        )

        ordered_ids = list(
            order_by_pick_priority(
                FbsOrder.objects.filter(
                    pk__in=(older_without_deadline.pk, overdue_newer.pk)
                )
            ).values_list("id", flat=True)
        )

        self.assertEqual(ordered_ids, [overdue_newer.id, older_without_deadline.id])

    def test_location_overlap_cannot_overtake_an_older_order(self):
        older = self.order()
        newer = self.order()
        now = timezone.now()
        FbsOrder.objects.filter(pk=older.pk).update(
            ordered_at=now - timedelta(days=2)
        )
        FbsOrder.objects.filter(pk=newer.pk).update(
            ordered_at=now - timedelta(hours=1)
        )
        older.refresh_from_db()
        newer.refresh_from_db()
        old_location = (("location", 1), ("box", 1))
        current_location = (("location", 2), ("box", 2))
        locations = {
            older.id: Counter({old_location: 1}),
            newer.id: Counter({current_location: 1}),
        }

        old_priority = _location_grouping_priority(
            older,
            locations_by_order=locations,
            current_location_counts=Counter({current_location: 1}),
        )
        new_priority = _location_grouping_priority(
            newer,
            locations_by_order=locations,
            current_location_counts=Counter({current_location: 1}),
        )

        self.assertLess(old_priority, new_priority)

    def test_picker_task_order_keeps_oldest_before_shorter_route(self):
        older = self.order()
        newer = self.order()
        now = timezone.now()
        FbsOrder.objects.filter(pk=older.pk).update(
            ordered_at=now - timedelta(days=2)
        )
        FbsOrder.objects.filter(pk=newer.pk).update(
            ordered_at=now - timedelta(hours=1)
        )
        route_location = WarehouseLocation.objects.create(
            location_code="WAVE-CHOICE-EARLY-ROUTE",
            zone_code="OS",
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
        )
        route_cell = FbsStorageCell.objects.create(
            cell_code="WAVE-CHOICE-EARLY-CELL",
            location=route_location,
        )
        route_pallet = FbsPallet.objects.create(
            agency=self.agency,
            cell=route_cell,
            pallet_code="WAVE-CHOICE-EARLY-PALLET",
        )
        route_box = FbsBox.objects.create(
            agency=self.agency,
            pallet=route_pallet,
            box_code="WAVE-CHOICE-EARLY-BOX",
        )
        route_balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=route_box,
            sku_ref=self.sku,
            identity_key="choice-early-route",
            qty=10,
            available_qty=10,
        )
        FbsOrderStockAllocation.objects.filter(
            order_item__order=newer
        ).update(balance=route_balance)

        batch = self.create([newer, older], reuse_queued_batches=False)[0]

        self.assertEqual(
            list(batch.tasks.order_by("sort_order").values_list("order_id", flat=True)),
            [older.id, newer.id],
        )

    def post(self, order, **data):
        from fbs.operator_views import operator_prepare_wave
        request = self.factory.post("/fbs/operator/waves/prepare/", {
            "return_to": "queue", "agency": self.agency.id, "queue_limit": 100,
            "order_ids": [order.id], **data,
        })
        request.user = self.actor
        request.session = {}
        view = operator_prepare_wave
        while hasattr(view, "__wrapped__"):
            view = view.__wrapped__
        with patch("fbs.operator_views._orders_context", return_value={"orders_mode": "queue"}), \
             patch("fbs.operator_views.render", side_effect=lambda req, tpl, ctx, status=200: SimpleNamespace(status_code=status, context=ctx)):
            return view(request)

    def test_initial_click_only_previews_and_never_prepares(self):
        old = self.wave()
        order = self.order()
        with patch("fbs.operator_views.prepare_pick_queue") as prepare:
            response = self.post(order)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["wave_launch_choice"]["selected_order_ids"], [order.id])
        self.assertEqual(response.context["wave_launch_choice"]["destinations"][0]["id"], old.id)
        prepare.assert_not_called()
        self.assertFalse(FbsPickTask.objects.filter(order=order).exists())

    def test_manual_selection_cannot_skip_an_older_available_order(self):
        older = self.order()
        newer = self.order()
        now = timezone.now()
        FbsOrder.objects.filter(pk=older.pk).update(
            ordered_at=now - timedelta(days=2)
        )
        FbsOrder.objects.filter(pk=newer.pk).update(
            ordered_at=now - timedelta(hours=1)
        )

        with patch("fbs.operator_views.prepare_pick_queue") as prepare:
            response = self.post(newer, wave_action="new")

        self.assertEqual(response.status_code, 409)
        prepare.assert_not_called()

    def test_new_confirmation_disables_reuse(self):
        order = self.order()
        with patch("fbs.operator_views.prepare_pick_queue", return_value=SimpleNamespace(
            tasks_created=1, batches=[SimpleNamespace(id=123)], rejected_orders=(),
            awaiting_stock_orders=0, validation_failed_orders=0,
        )) as prepare:
            response = self.post(order, wave_action="new")
        self.assertEqual(response.status_code, 302)
        self.assertIs(prepare.call_args.kwargs["reuse_queued_batches"], False)
        self.assertIsNone(prepare.call_args.kwargs["target_batch_id"])

    def test_existing_confirmation_forwards_exact_target(self):
        order = self.order()
        with patch("fbs.operator_views.prepare_pick_queue", return_value=SimpleNamespace(
            tasks_created=1, batches=[SimpleNamespace(id=123)], rejected_orders=(),
            awaiting_stock_orders=0, validation_failed_orders=0,
        )) as prepare:
            response = self.post(order, wave_action="existing", target_batch_id="123")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(prepare.call_args.kwargs["target_batch_id"], 123)
        self.assertIs(prepare.call_args.kwargs["reuse_queued_batches"], False)

    def test_existing_without_target_does_not_prepare(self):
        order = self.order()
        for value in ["", "bad", "0", "-1", "9" * 50]:
            with self.subTest(value=value), patch("fbs.operator_views.prepare_pick_queue") as prepare:
                response = self.post(order, wave_action="existing", target_batch_id=value)
                self.assertEqual(response.status_code, 409)
                prepare.assert_not_called()

    def test_stock_confirmation_preserves_destination(self):
        order = self.order()
        shortage = FbsQueueStockShortage(order_id=order.id, external_order_id=order.external_order_id,
                                       missing_qty=1, reasons=("Нет товара",))
        with patch("fbs.operator_views.prepare_pick_queue", side_effect=FbsQueueStockConfirmationRequired((shortage,))):
            response = self.post(order, wave_action="existing", target_batch_id="123")
        context = response.context["wave_stock_confirmation"]
        self.assertEqual(context["wave_action"], "existing")
        self.assertEqual(context["target_batch_id"], "123")

    def test_unknown_action_does_not_prepare(self):
        order = self.order()
        with patch("fbs.operator_views.prepare_pick_queue") as prepare:
            response = self.post(order, wave_action="auto")
        self.assertEqual(response.status_code, 409)
        prepare.assert_not_called()

    def test_real_http_preview_and_confirm_route(self):
        old = self.wave()
        order = self.order()
        self.client.force_login(self.actor)
        payload = {"return_to": "queue", "agency": self.agency.id,
                   "queue_limit": 100, "order_ids": [order.id]}
        with patch("fbs.operator_views.prepare_pick_queue") as prepare:
            response = self.client.post(reverse("fbs:operator_prepare_wave"), payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Куда направить заказы?")
        self.assertContains(response, reverse("fbs:operator_confirm_wave"))
        prepare.assert_not_called()
        response = self.client.post(reverse("fbs:operator_confirm_wave"), {
            **payload, "wave_action": "existing", "target_batch_id": old.id,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(FbsPickTask.objects.get(order=order).batch_id, old.id)

    def test_anonymous_http_confirmation_cannot_launch(self):
        order = self.order()
        with patch("fbs.operator_views.prepare_pick_queue") as prepare:
            response = self.client.post(reverse("fbs:operator_confirm_wave"), {
                "agency": self.agency.id, "order_ids": [order.id], "wave_action": "new",
            })
        self.assertIn(response.status_code, (302, 403))
        prepare.assert_not_called()
