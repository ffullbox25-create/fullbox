from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from fbs.controller_views import _controller_daily_report
from fbs.models import (
    FbsIntegrationProfile,
    FbsOrder,
    FbsPickBatch,
    FbsPickScanEvent,
    FbsPickTask,
)
from sku.models import Agency


@override_settings(FBS_MODULE_ENABLED=True, FBS_WAREHOUSE_WRITES_ENABLED=True)
class FbsControllerDailyReportTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.controller = users.objects.create_user(username="daily_report_controller")
        self.other_controller = users.objects.create_user(
            username="daily_report_other_controller"
        )
        self.first_agency = Agency.objects.create(
            agn_name="Общество с ограниченной ответственностью Альфа"
        )
        self.second_agency = Agency.objects.create(agn_name="ИП Бета")
        self.first_profile = FbsIntegrationProfile.objects.create(
            agency=self.first_agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="Daily report WB",
            external_warehouse_id="daily-report-wb",
        )
        self.second_profile = FbsIntegrationProfile.objects.create(
            agency=self.second_agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            name="Daily report Ozon",
            external_warehouse_id="daily-report-ozon",
        )

    def _wave(self, *, agency, profile, suffix, start_minute, duration_seconds):
        report_date = timezone.localdate()
        current_timezone = timezone.get_current_timezone()
        day_start = timezone.make_aware(
            datetime.combine(report_date, datetime.min.time()),
            current_timezone,
        )
        started_at = day_start + timedelta(minutes=start_minute)
        completed_at = started_at + timedelta(seconds=duration_seconds)
        batch = FbsPickBatch.objects.create(
            agency=agency,
            status=FbsPickBatch.STATUS_DONE,
            planned_qty=5,
            picked_qty=5,
            verification_assigned_to=self.controller,
            verification_started_at=started_at,
            completed_at=completed_at,
        )
        order = FbsOrder.objects.create(
            profile=profile,
            external_order_id=f"DAILY-REPORT-{suffix}",
        )
        task = FbsPickTask.objects.create(
            batch=batch,
            order=order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=5,
            picked_qty=5,
        )
        return batch, task

    def test_report_summarizes_only_current_controller_and_groups_clients(self):
        first_batch, first_task = self._wave(
            agency=self.first_agency,
            profile=self.first_profile,
            suffix="FIRST",
            start_minute=10,
            duration_seconds=600,
        )
        second_batch, second_task = self._wave(
            agency=self.second_agency,
            profile=self.second_profile,
            suffix="SECOND",
            start_minute=30,
            duration_seconds=300,
        )
        for batch, task in (
            (first_batch, first_task),
            (first_batch, first_task),
            (second_batch, second_task),
        ):
            FbsPickScanEvent.objects.create(
                batch=batch,
                task=task,
                stage=FbsPickScanEvent.STAGE_VERIFY_ITEM,
                result=FbsPickScanEvent.RESULT_SUCCESS,
                created_by=self.controller,
            )
        FbsPickScanEvent.objects.create(
            batch=first_batch,
            task=first_task,
            stage=FbsPickScanEvent.STAGE_VERIFY_MARKING,
            result=FbsPickScanEvent.RESULT_ERROR,
            message="КИЗ не принят",
            created_by=self.controller,
        )
        FbsPickScanEvent.objects.create(
            batch=first_batch,
            task=first_task,
            stage=FbsPickScanEvent.STAGE_VERIFY_ITEM,
            result=FbsPickScanEvent.RESULT_SUCCESS,
            created_by=self.other_controller,
        )

        report = _controller_daily_report(controller=self.controller)

        self.assertEqual(report["units"], 3)
        self.assertEqual(report["orders"], 2)
        self.assertEqual(report["waves"], 2)
        self.assertEqual(report["wave_time"], "0:15:00")
        self.assertEqual(report["average_wave_time"], "0:07:30")
        self.assertEqual(report["longest_wave_time"], "0:10:00")
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["error_reasons"], [("КИЗ не принят", 1)])
        self.assertEqual(
            [(row["units"], row["orders"], row["waves"], row["errors"]) for row in report["clients"]],
            [(2, 1, 1, 1), (1, 1, 1, 0)],
        )

    def test_report_route_is_registered(self):
        self.assertEqual(
            reverse("fbs:controller_daily_report"),
            "/fbs/controller/report/",
        )
