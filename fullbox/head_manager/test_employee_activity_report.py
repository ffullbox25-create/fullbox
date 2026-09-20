from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.utils import timezone

from employees.models import Employee

from .employee_activity_report import _blank_rollup, _merge_rows, _short_text
from .web_ui import _head_manager_employee_activity_filters


class EmployeeActivityAggregationTests(TestCase):
    def test_scan_rows_are_counted_as_actions_and_daily_scans(self):
        employee = Employee.objects.create(
            full_name="Тестовый кладовщик",
            role="storekeeper",
            is_active=True,
        )
        rollups = {employee.pk: _blank_rollup(employee)}
        occurred_at = timezone.now()

        _merge_rows(
            rollups,
            [
                {
                    "day": timezone.localdate(occurred_at),
                    "actor_id": employee.pk,
                    "actions": 5,
                    "scan_success": 4,
                    "scan_errors": 1,
                    "volume": 0,
                    "first_action": occurred_at - timedelta(minutes=10),
                    "last_action": occurred_at,
                }
            ],
            actor_key="actor_id",
            contour="fbs_scan",
            scans=True,
        )

        result = rollups[employee.pk]
        self.assertEqual(result["actions"], 5)
        self.assertEqual(result["scans"], 5)
        self.assertEqual(result["scan_success"], 4)
        self.assertEqual(result["scan_errors"], 1)
        self.assertEqual(result["daily"][timezone.localdate(occurred_at)]["scans"], 5)

    def test_raw_long_value_is_shortened(self):
        raw_value = "X" * 300

        result = _short_text(raw_value, limit=20)

        self.assertEqual(len(result), 20)
        self.assertTrue(result.endswith("…"))


class EmployeeActivityViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="employee_activity_head",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Начальник склада",
            role="head_manager",
            user=self.user,
            is_active=True,
        )
        self.employee = Employee.objects.create(
            full_name="Иванов Иван",
            role="storekeeper",
            is_active=True,
        )
        self.client.force_login(self.user)

    def test_period_is_limited_to_92_days(self):
        request = RequestFactory().get(
            "/head-manager/reports/employees/activity/",
            {"date_from": "2026-01-01", "date_to": "2026-08-31"},
        )

        filters = _head_manager_employee_activity_filters(request)

        self.assertTrue(filters["period_limited"])
        self.assertEqual(filters["date_from"], "2026-06-01")
        self.assertEqual(filters["date_to"], "2026-08-31")

    @patch("head_manager.web_ui._head_manager_employee_activity_directory_rows")
    def test_directory_route_renders_employee_link(self, rows_mock):
        rows_mock.return_value = (
            [
                {
                    "employee": self.employee.full_name,
                    "role": "Кладовщик",
                    "login": "Нет учётной записи",
                    "is_active": True,
                    "status_label": "Активен",
                    "active_days": 0,
                    "first_action_label": "—",
                    "last_action_label": "—",
                    "contours_label": "—",
                    "actions": 0,
                    "scans": 0,
                    "scan_success": 0,
                    "scan_errors": 0,
                    "detail_url": f"/head-manager/reports/employees/{self.employee.pk}/",
                }
            ],
            {
                "employees": 1,
                "with_activity": 0,
                "without_activity": 1,
                "actions": 0,
                "scans": 0,
                "scan_errors": 0,
            },
        )

        response = self.client.get("/head-manager/reports/employees/activity/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.employee.full_name)
        self.assertContains(
            response,
            f'/head-manager/reports/employees/{self.employee.pk}/',
        )

    @patch("head_manager.web_ui._head_manager_employee_activity_detail")
    def test_employee_detail_route_renders_daily_and_timeline(self, detail_mock):
        now = timezone.now()
        detail_mock.return_value = {
            "active_days": 1,
            "actions": 3,
            "scans": 2,
            "scan_success": 1,
            "scan_errors": 1,
            "first_action_label": "09:00",
            "last_action_label": "10:00",
            "daily": [
                {
                    "date": timezone.localdate(now).strftime("%d.%m.%Y"),
                    "first_action_label": "09:00",
                    "last_action_label": "10:00",
                    "activity_span_label": "1 ч",
                    "contours_label": "FBS-сканирование",
                    "actions": 3,
                    "scans": 2,
                    "scan_success": 1,
                    "scan_errors": 1,
                    "volume": 1,
                }
            ],
            "timeline": [
                {
                    "date": timezone.localdate(now).strftime("%d.%m.%Y"),
                    "time": "09:15:00",
                    "contour_label": "FBS-сканирование",
                    "action": "Проверка товара",
                    "result": "Ошибка",
                    "result_tone": "error",
                    "amount": "1 скан",
                    "object": "Волна #1",
                    "details": "Штрихкод не совпал",
                }
            ],
            "timeline_truncated": False,
        }

        response = self.client.get(
            f"/head-manager/reports/employees/{self.employee.pk}/"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Проверка товара")
        self.assertContains(response, "Штрихкод не совпал")

    def test_storekeeper_cannot_open_management_report(self):
        warehouse_user = get_user_model().objects.create_user(
            username="employee_activity_storekeeper",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Обычный кладовщик",
            role="storekeeper",
            user=warehouse_user,
            is_active=True,
        )
        self.client.force_login(warehouse_user)

        response = self.client.get("/head-manager/reports/employees/activity/")

        self.assertEqual(response.status_code, 403)
