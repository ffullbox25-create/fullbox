from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase


class RecordStorageBillingCommandTests(SimpleTestCase):
    @patch("billing.management.commands.record_storage_billing.capture_daily_storage_usage")
    @patch("billing.management.commands.record_storage_billing.StorageBillingService.run_daily_for_all_clients")
    def test_runs_general_and_fbs_storage_separately(self, run_general, capture_fbs):
        run_general.return_value = {
            "day": date(2026, 9, 14),
            "clients": 2,
            "total_pallets": 3,
            "zero_days": 0,
        }
        capture_fbs.return_value = SimpleNamespace(
            usage_date=date(2026, 9, 14),
            agencies=4,
            rows=12,
            incomplete_dimension_rows=1,
        )

        call_command("record_storage_billing", date="2026-09-14")

        run_general.assert_called_once_with(day=date(2026, 9, 14), client_id=None)
        capture_fbs.assert_called_once_with(usage_date=date(2026, 9, 14), agency=None)
