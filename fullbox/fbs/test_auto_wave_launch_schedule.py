from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, TestCase

from django.db.utils import OperationalError

from fbs.management.commands.launch_fbs_wave import (
    CLIENT_DEADLOCK_RETRY_ATTEMPTS,
    DEFAULT_MAX_QUEUED_BATCHES,
    ReadyAgencyWave,
    TICK_OPENING,
    TICK_REGULAR,
    _create_client_batches,
    _has_queued_capacity,
    _groups_in_fair_order,
    _groups_for_tick,
    _scheduled_slot,
    _scheduled_tick_kind,
)
from fbs.exceptions import FbsPickingError
from fbs.services.picking import _reserve_and_create_pick_queue


MOSCOW = ZoneInfo("Europe/Moscow")


def at(hour, minute, second=0):
    return datetime(2026, 8, 31, hour, minute, second, tzinfo=MOSCOW)


def group(agency_id, units, oldest_hour, *, profile_id=None):
    return ReadyAgencyWave(
        agency_id=agency_id,
        order_ids=(agency_id * 100,),
        unit_count=units,
        oldest_key=(at(oldest_hour, 0), agency_id * 100),
        profile_id=profile_id,
    )


class FbsAutoWaveScheduleTests(SimpleTestCase):
    def test_exact_schedule_slots(self):
        self.assertEqual(_scheduled_tick_kind(at(8, 0)), TICK_OPENING)
        self.assertEqual(_scheduled_tick_kind(at(8, 30)), TICK_REGULAR)
        self.assertEqual(_scheduled_tick_kind(at(9, 0)), TICK_REGULAR)
        self.assertEqual(_scheduled_tick_kind(at(9, 30)), TICK_REGULAR)
        self.assertEqual(_scheduled_tick_kind(at(18, 30)), TICK_REGULAR)
        self.assertEqual(_scheduled_tick_kind(at(19, 30)), TICK_REGULAR)
        self.assertIsNone(_scheduled_tick_kind(at(7, 59)))
        self.assertIsNone(_scheduled_tick_kind(at(8, 50)))
        self.assertIsNone(_scheduled_tick_kind(at(18, 50)))
        self.assertIsNone(_scheduled_tick_kind(at(20, 0)))

    def test_every_half_hour_launches_small_and_regular_clients(self):
        groups = (group(1, 1, 8), group(2, 9, 7), group(3, 27, 6))
        selected = _groups_for_tick(groups, TICK_REGULAR)
        self.assertEqual([row.agency_id for row in selected], [1, 2, 3])

    def test_opening_tick_launches_both_small_and_regular_clients(self):
        groups = (group(1, 4, 8), group(2, 18, 7))
        selected = _groups_for_tick(groups, TICK_OPENING)
        self.assertEqual([row.agency_id for row in selected], [1, 2])

    def test_clients_are_never_merged_for_threshold(self):
        groups = (group(1, 6, 8), group(2, 6, 7))
        self.assertEqual(
            [row.agency_id for row in _groups_for_tick(groups, TICK_REGULAR)],
            [1, 2],
        )


class FbsWaveFairOrderTests(SimpleTestCase):
    def test_never_served_then_longest_waiting_cabinets_go_first(self):
        groups = (
            group(1, 20, 8, profile_id=101),
            group(2, 20, 7, profile_id=102),
            group(3, 20, 6, profile_id=103),
        )

        ordered = _groups_in_fair_order(
            groups,
            latest_wave_at_by_profile={
                101: at(12, 0),
                102: at(10, 0),
            },
        )

        self.assertEqual([row.profile_id for row in ordered], [103, 102, 101])

    def test_equal_service_time_falls_back_to_oldest_ready_order(self):
        groups = (
            group(1, 20, 8, profile_id=101),
            group(2, 20, 6, profile_id=102),
        )
        last_wave_at = at(9, 0)

        ordered = _groups_in_fair_order(
            groups,
            latest_wave_at_by_profile={101: last_wave_at, 102: last_wave_at},
        )

        self.assertEqual([row.profile_id for row in ordered], [102, 101])


class FbsQueuedWaveCapacityTests(SimpleTestCase):
    def test_automatic_launcher_has_no_global_queue_cap_by_default(self):
        self.assertEqual(DEFAULT_MAX_QUEUED_BATCHES, 0)
        self.assertTrue(_has_queued_capacity(12, DEFAULT_MAX_QUEUED_BATCHES))
        self.assertTrue(_has_queued_capacity(1000, DEFAULT_MAX_QUEUED_BATCHES))

    def test_explicit_positive_queue_cap_is_still_honored(self):
        self.assertTrue(_has_queued_capacity(11, 12))
        self.assertFalse(_has_queued_capacity(12, 12))
        self.assertFalse(_has_queued_capacity(13, 12))


class FbsReserveOnlyQueueTests(TestCase):
    @patch("fbs.services.picking.create_pick_batches")
    @patch("fbs.services.picking.reserve_order_stock")
    def test_reserve_only_mode_does_not_create_batch(
        self,
        reserve_order_stock_mock,
        create_pick_batches_mock,
    ):
        reserve_order_stock_mock.return_value = SimpleNamespace(
            reserved=True,
            status="reserved",
        )

        result = _reserve_and_create_pick_queue(
            queueable_order_ids=[101],
            current_orders={},
            stock_shortage_policy="mark",
            max_orders_per_batch=50,
            max_units_per_batch=100,
            create_batches=False,
        )

        self.assertEqual(result, (1, 0, 0, ()))
        create_pick_batches_mock.assert_not_called()

    @patch("fbs.services.picking.create_pick_batches")
    @patch("fbs.services.picking.reserve_order_stock")
    def test_normal_mode_still_creates_batches(
        self,
        reserve_order_stock_mock,
        create_pick_batches_mock,
    ):
        reserve_order_stock_mock.return_value = SimpleNamespace(
            reserved=True,
            status="reserved",
        )
        expected_batches = (SimpleNamespace(id=501),)
        create_pick_batches_mock.return_value = expected_batches

        result = _reserve_and_create_pick_queue(
            queueable_order_ids=[101],
            current_orders={},
            stock_shortage_policy="mark",
            max_orders_per_batch=50,
            max_units_per_batch=100,
            create_batches=True,
        )

        self.assertEqual(result, (1, 0, 0, expected_batches))
        create_pick_batches_mock.assert_called_once_with(
            order_ids=[101],
            max_orders_per_batch=50,
            max_units_per_batch=100,
            created_by=None,
        )


class FbsWaveSlotToleranceTests(SimpleTestCase):
    """systemd будит юнит с точностью до минуты; тик не должен теряться."""

    def test_late_start_still_counts_as_the_slot(self):
        self.assertEqual(_scheduled_tick_kind(at(9, 1, 7)), TICK_REGULAR)
        self.assertEqual(_scheduled_tick_kind(at(8, 1, 30)), TICK_OPENING)
        self.assertEqual(_scheduled_tick_kind(at(19, 31, 59)), TICK_REGULAR)

    def test_slot_is_normalized_to_the_grid(self):
        self.assertEqual(_scheduled_slot(at(9, 1, 7)).strftime("%H:%M"), "09:00")
        self.assertEqual(_scheduled_slot(at(10, 31, 5)).strftime("%H:%M"), "10:30")
        self.assertEqual(_scheduled_slot(at(10, 0, 30)).strftime("%H:%M"), "10:00")

    def test_too_late_or_too_early_is_not_a_slot(self):
        self.assertIsNone(_scheduled_tick_kind(at(9, 3)))
        self.assertIsNone(_scheduled_tick_kind(at(9, 20)))
        self.assertIsNone(_scheduled_tick_kind(at(7, 59)))
        self.assertIsNone(_scheduled_tick_kind(at(8, 20)))

    def test_day_edges(self):
        self.assertEqual(_scheduled_slot(at(8, 2)).strftime("%H:%M"), "08:00")
        self.assertIsNone(_scheduled_slot(at(7, 59)))
        self.assertEqual(_scheduled_slot(at(19, 32)).strftime("%H:%M"), "19:30")
        self.assertIsNone(_scheduled_slot(at(19, 33)))
        self.assertIsNone(_scheduled_slot(at(20, 0)))


def _deadlock_error():
    error = OperationalError("deadlock detected")
    error.sqlstate = "40P01"
    return error


class FbsClientBatchIsolationTests(SimpleTestCase):
    """Сбой по одному клиенту не должен стоить всего тика."""

    def test_deadlock_is_replayed_for_the_same_client(self):
        calls = []

        def side_effect(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise _deadlock_error()
            return (SimpleNamespace(id=11),)

        with patch(
            "fbs.management.commands.launch_fbs_wave.create_pick_batches",
            side_effect=side_effect,
        ):
            batches = _create_client_batches(
                group(1, 12, 8),
                max_orders_per_batch=50,
                max_units_per_batch=50,
                max_new_batches=6,
            )

        self.assertEqual([batch.id for batch in batches], [11])
        self.assertEqual(len(calls), 2)

    def test_non_deadlock_error_is_not_replayed(self):
        with patch(
            "fbs.management.commands.launch_fbs_wave.create_pick_batches",
            side_effect=OperationalError("connection refused"),
        ) as mock:
            with self.assertRaises(OperationalError):
                _create_client_batches(
                    group(1, 12, 8),
                    max_orders_per_batch=50,
                    max_units_per_batch=50,
                    max_new_batches=6,
                )
        self.assertEqual(mock.call_count, 1)

    def test_persistent_deadlock_gives_up_after_the_retries(self):
        with patch(
            "fbs.management.commands.launch_fbs_wave.create_pick_batches",
            side_effect=_deadlock_error(),
        ) as mock:
            with self.assertRaises(OperationalError):
                _create_client_batches(
                    group(1, 12, 8),
                    max_orders_per_batch=50,
                    max_units_per_batch=50,
                    max_new_batches=6,
                )
        self.assertEqual(mock.call_count, CLIENT_DEADLOCK_RETRY_ATTEMPTS)

    def test_blocked_order_error_reaches_the_caller_as_client_failure(self):
        with patch(
            "fbs.management.commands.launch_fbs_wave.create_pick_batches",
            side_effect=FbsPickingError("Заказ отменен маркетплейсом."),
        ):
            with self.assertRaises(FbsPickingError):
                _create_client_batches(
                    group(1, 12, 8),
                    max_orders_per_batch=50,
                    max_units_per_batch=50,
                    max_new_batches=6,
                )
