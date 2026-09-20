from unittest.mock import patch

from django.db import OperationalError
from django.test import SimpleTestCase, TestCase, override_settings

from .exceptions import FbsIntegrationError
from .services.marketplace import (
    _is_database_deadlock,
    _is_database_lock_contention,
    _run_marketplace_queue_schedulers,
    retry_wb_marking_code,
)
from .services.picking import prepare_pick_queue


def _postgres_operational_error(sqlstate: str) -> OperationalError:
    driver_error = RuntimeError("postgres failure")
    driver_error.sqlstate = sqlstate
    error = OperationalError("database operation failed")
    error.__cause__ = driver_error
    return error


class FbsMarketplaceLockContentionTests(SimpleTestCase):
    def test_postgres_lock_errors_are_classified_by_sqlstate(self):
        self.assertTrue(_is_database_deadlock(_postgres_operational_error("40P01")))
        self.assertTrue(
            _is_database_lock_contention(_postgres_operational_error("55P03"))
        )
        self.assertFalse(
            _is_database_lock_contention(_postgres_operational_error("08006"))
        )

    @patch("fbs.services.marketplace.time.sleep")
    @patch("fbs.services.marketplace._run_marketplace_queue_schedulers_once")
    def test_marketplace_scheduler_replays_a_rolled_back_deadlock(
        self,
        run_once,
        sleep,
    ):
        run_once.side_effect = [
            _postgres_operational_error("40P01"),
            None,
        ]

        _run_marketplace_queue_schedulers(10)

        self.assertEqual(run_once.call_count, 2)
        sleep.assert_called_once()

    @patch("fbs.services.marketplace._retry_wb_marking_code_once")
    def test_busy_handover_returns_a_fast_operator_error(self, retry_once):
        retry_once.side_effect = _postgres_operational_error("55P03")

        with self.assertRaisesMessage(
            FbsIntegrationError,
            "уже выполняется другая операция",
        ):
            retry_wb_marking_code(
                batch_id=63,
                order_item_id=1,
                marking_scan="test-kiz",
            )


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
)
class FbsWaveLockContentionTests(TestCase):
    @patch("fbs.services.picking.time.sleep")
    @patch("fbs.services.picking._reserve_and_create_pick_queue")
    @patch(
        "fbs.services.picking.feature_enabled",
        side_effect=lambda name: name in {"module", "warehouse_writes"},
    )
    def test_wave_preparation_replays_a_rolled_back_deadlock(
        self,
        _feature_enabled,
        reserve_and_create,
        sleep,
    ):
        reserve_and_create.side_effect = [
            _postgres_operational_error("40P01"),
            (0, 0, 0, ()),
        ]

        result = prepare_pick_queue(limit=1)

        self.assertEqual(reserve_and_create.call_count, 2)
        sleep.assert_called_once()
        self.assertEqual(result.tasks_created, 0)
