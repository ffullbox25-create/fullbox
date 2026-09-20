from datetime import datetime, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from employees.models import Employee
from processing_app.models import ProcessingWorkEvent

from .employee_report import processing_metric_points


class WarehouseEmployeeProcessingReportTests(TestCase):
    def setUp(self):
        self.day = timezone.localdate()
        self.filters = {
            "date_from": self.day.isoformat(),
            "date_to": self.day.isoformat(),
            "employee": "",
            "role": "",
        }

    def test_processing_metrics_are_grouped_for_employee_without_user(self):
        employee = Employee.objects.create(
            full_name="Обработчик без логина",
            role="processing_worker",
            is_active=True,
        )
        ProcessingWorkEvent.objects.create(
            event_key="report-operation",
            operation_type=ProcessingWorkEvent.TYPE_OPERATION_COMPLETED,
            order_id="25",
            employee=employee,
            employee_name=employee.full_name,
            units=11,
            occurred_at=timezone.now(),
        )
        ProcessingWorkEvent.objects.create(
            event_key="report-box",
            operation_type=ProcessingWorkEvent.TYPE_BOX_FORMED,
            order_id="25",
            employee=employee,
            employee_name=employee.full_name,
            units=11,
            boxes=1,
            occurred_at=timezone.now(),
        )

        current_timezone = timezone.get_current_timezone()
        period_start = timezone.make_aware(
            datetime.combine(self.day, datetime.min.time()),
            current_timezone,
        )
        with patch(
            "head_manager.employee_report._period",
            return_value=(
                self.day,
                self.day,
                period_start,
                period_start + timedelta(days=1),
            ),
        ):
            points = processing_metric_points(
                {**self.filters, "employee": employee.full_name}
            )
        metrics = {metric: value for _day, actor, metric, value in points if actor == employee}

        self.assertEqual(metrics["processing_operations"], 1)
        self.assertEqual(metrics["processing_units"], 11)
        self.assertEqual(metrics["processing_boxes"], 1)
        self.assertEqual(metrics["processing_boxed_units"], 11)
