from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from sklad.models import WarehouseLocation
from sku.models import Agency, SKU

from .exceptions import FbsPickingError
from .models import (
    FbsBox,
    FbsIntegrationProfile,
    FbsOrder,
    FbsOrderItem,
    FbsOrderStockAllocation,
    FbsPallet,
    FbsPickBatch,
    FbsPickingCart,
    FbsStockBalance,
    FbsStorageCell,
    FbsWavePolicy,
)
from .services.picking import (
    FbsQueuedPickRegroupResult,
    create_pick_batches,
    regroup_queued_pick_batches,
)


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_STATUS_PULL_ENABLED=False,
    FBS_OUTBOX_ENABLED=False,
)
class FbsPickBatchGroupingTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(username="wave-regroup")
        self.agency = Agency.objects.create(agn_name="Wave regroup client")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            name="Wave regroup cabinet",
            external_warehouse_id="wave-regroup-cabinet",
            is_active=True,
        )
        self.sku = SKU.objects.create(
            agency=self.agency, sku_code="REGROUP-SKU", name="Grouping item"
        )
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            location_code="REGROUP-PR",
            is_active=True,
            is_storage=True,
            is_pickable=True,
        )
        self.cell = FbsStorageCell.objects.create(
            cell_code="REGROUP-PR", location=location
        )
        pallet = FbsPallet.objects.create(
            agency=self.agency,
            cell=self.cell,
            pallet_code="REGROUP-PALLET",
            status=FbsPallet.STATUS_ACTIVE,
        )
        self.box_a = FbsBox.objects.create(
            agency=self.agency,
            pallet=pallet,
            box_code="REGROUP-BOX-A",
            status=FbsBox.STATUS_ACTIVE,
        )
        self.box_b = FbsBox.objects.create(
            agency=self.agency,
            pallet=pallet,
            box_code="REGROUP-BOX-B",
            status=FbsBox.STATUS_ACTIVE,
        )
        self.balances = {
            box.id: FbsStockBalance.objects.create(
                agency=self.agency,
                box=box,
                sku_ref=self.sku,
                identity_key=f"regroup-{box.id}",
                sku_code=self.sku.sku_code,
                name=self.sku.name,
                barcode="4600000011111",
                qty=100,
                available_qty=90,
                reserved_qty=0,
            )
            for box in (self.box_a, self.box_b)
        }
        self.counter = 0
        self.started_at = timezone.now() - timedelta(hours=2)

    def order(self, box, *, profile=None, qty=1):
        self.counter += 1
        profile = profile or self.profile
        order = FbsOrder.objects.create(
            profile=profile,
            external_order_id=f"REGROUP-{self.counter}",
            internal_status=FbsOrder.STATUS_RESERVED,
            marketplace_status="awaiting_packaging",
            ordered_at=self.started_at + timedelta(minutes=self.counter),
        )
        item = FbsOrderItem.objects.create(
            order=order,
            external_line_id=str(self.counter),
            external_sku=self.sku.sku_code,
            sku=self.sku,
            barcode="4600000011111",
            product_name=self.sku.name,
            quantity=qty,
        )
        balance = self.balances[box.id]
        balance.available_qty -= qty
        balance.reserved_qty += qty
        balance.save(update_fields=["available_qty", "reserved_qty", "updated_at"])
        FbsOrderStockAllocation.objects.create(
            order_item=item,
            balance=balance,
            qty_reserved=qty,
            status=FbsOrderStockAllocation.STATUS_RESERVED,
            reserved_by=self.actor,
        )
        return order

    def wave(self, *orders):
        return create_pick_batches(
            order_ids=[order.id for order in orders],
            max_orders_per_batch=100,
            max_units_per_batch=100,
            created_by=self.actor,
        )[0]

    def active_box_ids(self, batch):
        return set(
            FbsOrderStockAllocation.objects.filter(
                pick_task__batch=batch,
                pick_task__status="queued",
            ).values_list("balance__box_id", flat=True)
        )

    def stock_state(self):
        return list(
            FbsStockBalance.objects.order_by("id").values_list(
                "id", "qty", "available_qty", "reserved_qty"
            )
        )

    def allocation_state(self):
        return list(
            FbsOrderStockAllocation.objects.order_by("id").values_list(
                "id", "status", "qty_reserved", "qty_picked", "pick_task_id"
            )
        )

    def test_orders_from_same_box_are_joined_across_queued_waves(self):
        a1 = self.order(self.box_a)
        b1 = self.order(self.box_b)
        a2 = self.order(self.box_a)
        b2 = self.order(self.box_b)
        first = self.wave(a1, b1)
        second = self.wave(a2, b2)
        stock_before = self.stock_state()
        allocations_before = self.allocation_state()

        result = regroup_queued_pick_batches(
            profile_id=self.profile.id,
            max_orders_per_batch=2,
            max_units_per_batch=2,
        )

        self.assertEqual(result.batch_count, 2)
        self.assertEqual(result.moved_task_count, 2)
        self.assertEqual(result.emptied_batch_count, 0)
        self.assertEqual(
            {frozenset(self.active_box_ids(batch)) for batch in (first, second)},
            {frozenset({self.box_a.id}), frozenset({self.box_b.id})},
        )
        self.assertEqual(self.stock_state(), stock_before)
        self.assertEqual(self.allocation_state(), allocations_before)

    def test_exact_box_overlap_beats_older_order_in_same_cell(self):
        a1 = self.order(self.box_a)
        older_b = self.order(self.box_b)
        newer_a = self.order(self.box_a)
        batches = create_pick_batches(
            order_ids=[a1.id, older_b.id, newer_a.id],
            max_orders_per_batch=2,
            max_units_per_batch=2,
        )

        first_order_ids = set(batches[0].tasks.values_list("order_id", flat=True))
        self.assertEqual(first_order_ids, {a1.id, newer_a.id})

    def test_excess_empty_wave_is_canceled_without_recreating_numbers(self):
        first = self.wave(self.order(self.box_a))
        second = self.wave(self.order(self.box_a))
        original_ids = {first.id, second.id}

        result = regroup_queued_pick_batches(
            profile_id=self.profile.id,
            max_orders_per_batch=100,
            max_units_per_batch=100,
        )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(result.emptied_batch_count, 1)
        self.assertEqual(first.status, FbsPickBatch.STATUS_QUEUED)
        self.assertEqual(first.tasks.filter(status="queued").count(), 2)
        self.assertEqual(first.planned_qty, 2)
        self.assertEqual(second.status, FbsPickBatch.STATUS_CANCELED)
        self.assertEqual(second.planned_qty, 0)
        self.assertIsNotNone(second.canceled_at)
        self.assertEqual(
            set(FbsPickBatch.objects.values_list("id", flat=True)), original_ids
        )

    def test_assigned_cart_and_partly_picked_waves_are_unchanged(self):
        assigned = self.wave(self.order(self.box_a))
        assigned.assigned_to = self.actor
        assigned.save(update_fields=["assigned_to", "updated_at"])

        cart = self.wave(self.order(self.box_b))
        cart.cart = FbsPickingCart.objects.create(
            barcode="FBS-CART-990001", name="Touched cart"
        )
        cart.save(update_fields=["cart", "updated_at"])

        partly_picked = self.wave(self.order(self.box_a))
        partly_picked.picked_qty = 1
        partly_picked.save(update_fields=["picked_qty", "updated_at"])

        eligible_one = self.wave(self.order(self.box_a))
        eligible_two = self.wave(self.order(self.box_a))
        touched_task_batches = {
            task.id: task.batch_id
            for batch in (assigned, cart, partly_picked)
            for task in batch.tasks.all()
        }

        result = regroup_queued_pick_batches(
            profile_id=self.profile.id,
            max_orders_per_batch=100,
            max_units_per_batch=100,
        )

        self.assertEqual(result.skipped_batch_count, 3)
        self.assertEqual(result.emptied_batch_count, 1)
        self.assertEqual(
            {
                task.id: task.batch_id
                for batch in (assigned, cart, partly_picked)
                for task in batch.tasks.all()
            },
            touched_task_batches,
        )
        for batch in (assigned, cart, partly_picked):
            batch.refresh_from_db()
            self.assertEqual(batch.status, FbsPickBatch.STATUS_QUEUED)
            self.assertEqual(batch.planned_qty, 1)
        eligible_one.refresh_from_db()
        eligible_two.refresh_from_db()
        self.assertEqual(eligible_one.tasks.filter(status="queued").count(), 2)
        self.assertEqual(eligible_two.status, FbsPickBatch.STATUS_CANCELED)

    def test_cabinet_wave_limits_are_enforced(self):
        FbsWavePolicy.objects.create(
            profile=self.profile,
            max_orders_per_wave=2,
            max_units_per_wave=2,
        )
        batches = [self.wave(self.order(self.box_a)) for _ in range(3)]

        result = regroup_queued_pick_batches(
            profile_id=self.profile.id,
            max_orders_per_batch=100,
            max_units_per_batch=100,
        )

        active = FbsPickBatch.objects.filter(
            pk__in=[batch.id for batch in batches],
            status=FbsPickBatch.STATUS_QUEUED,
        )
        self.assertEqual(active.count(), 2)
        self.assertEqual(result.emptied_batch_count, 1)
        self.assertTrue(all(batch.tasks.filter(status="queued").count() <= 2 for batch in active))
        self.assertTrue(all(batch.planned_qty <= 2 for batch in active))

    def test_another_cabinet_is_not_changed(self):
        other_profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            name="Other cabinet",
            external_warehouse_id="other-wave-cabinet",
            is_active=True,
        )
        own = self.wave(self.order(self.box_a))
        other_order = self.order(self.box_b, profile=other_profile)
        other = self.wave(other_order)
        other_state = list(other.tasks.values_list("id", "batch_id", "sort_order"))

        result = regroup_queued_pick_batches(
            profile_id=self.profile.id,
            max_orders_per_batch=100,
            max_units_per_batch=100,
        )

        self.assertEqual(result.batch_count, 1)
        self.assertEqual(list(other.tasks.values_list("id", "batch_id", "sort_order")), other_state)
        own.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(own.status, FbsPickBatch.STATUS_QUEUED)
        self.assertEqual(other.status, FbsPickBatch.STATUS_QUEUED)

    def test_command_regroups_even_when_there_are_no_new_orders(self):
        self.wave(self.order(self.box_a))
        output = StringIO()
        regrouped = FbsQueuedPickRegroupResult(1, 0, 0)
        with patch(
            "fbs.management.commands.launch_fbs_wave.regroup_queued_pick_batches",
            return_value=regrouped,
        ) as regroup:
            call_command(
                "launch_fbs_wave",
                "--apply",
                "--ignore-window",
                stdout=output,
            )

        regroup.assert_called_once_with(
            profile_id=self.profile.id,
            max_orders_per_batch=50,
            max_units_per_batch=50,
        )
        self.assertIn(
            f"FBS_WAVE_REGROUP profile={self.profile.id} batches=1 moved_tasks=0 emptied=0",
            output.getvalue(),
        )
        self.assertIn("FBS_WAVE_SKIP reason=no_orders", output.getvalue())

    def test_command_logs_regroup_failure_and_keeps_tick_alive(self):
        self.wave(self.order(self.box_a))
        output = StringIO()
        with patch(
            "fbs.management.commands.launch_fbs_wave.regroup_queued_pick_batches",
            side_effect=FbsPickingError("regroup failed"),
        ):
            call_command(
                "launch_fbs_wave",
                "--apply",
                "--ignore-window",
                stdout=output,
            )

        self.assertIn(
            f"FBS_WAVE_REGROUP_FAILED profile={self.profile.id} reason=regroup failed",
            output.getvalue(),
        )
        self.assertIn("FBS_WAVE_SKIP reason=no_orders", output.getvalue())
