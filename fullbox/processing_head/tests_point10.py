from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from audit.models import OrderAuditEntry
from employees.models import Employee
from processing_app.closed_discrepancy_review import (
    REVIEW_STATUS_CORRECTION_REQUIRED,
    closed_discrepancy_correction_task_route,
    closed_discrepancy_correction_task_title,
)
from processing_app.stages import PROCESSING_STAGE_DONE
from sku.models import Agency
from todo.models import Task


User = get_user_model()


def _login(client, *, username, role):
    user = User.objects.create_user(username=username, password="pwd")
    employee = Employee.objects.create(
        user=user,
        full_name=f"Сотрудник {username}",
        role=role,
        is_active=True,
    )
    client.force_login(user)
    session = client.session
    session["employee_id"] = employee.pk
    session["employee_role"] = role
    session.save()
    return user, employee


@override_settings(ALLOWED_HOSTS=["testserver"])
class ProcessingClosedDiscrepancyCorrectionTaskTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.head_user, self.head_employee = _login(
            self.client,
            username="point10_processing_head",
            role="processing_head",
        )
        self.worker_user = User.objects.create_user(
            username="point10_worker",
            password="pwd",
        )
        self.worker = Employee.objects.create(
            user=self.worker_user,
            full_name="Обработчик Пункт 10",
            role="processing_worker",
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="ООО Пункт 10")
        self.order_id = "POINT10-MISMATCH"
        self.original_entry = OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            description="Закрытая заявка для пункта 10",
            payload={
                "status": "done",
                "status_label": "Заявка завершена",
                "processing_stage": PROCESSING_STAGE_DONE,
                "processing_stage_label": "Заявка закрыта",
                "discrepancy_status": "approved",
                "cards": [
                    {
                        "id": "card-a",
                        "article": "SKU-A",
                        "rows": [{"size": "42", "qty": "10"}],
                    }
                ],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {
                        "card_id": "card-a",
                        "article": "SKU-A",
                        "size": "42",
                        "processed": "9",
                    }
                ],
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BOX-POINT10",
                        "items": [
                            {"sku": "SKU-A", "size": "42", "qty": 10}
                        ],
                    }
                ],
                "act_pallets": [],
            },
        )

    @property
    def report_url(self):
        return "/processing-head/reports/closed-discrepancies"

    @property
    def review_url(self):
        return (
            "/processing-head/reports/closed-discrepancies/review/"
            f"{self.order_id}"
        )

    @property
    def task_url(self):
        return (
            "/processing-head/reports/closed-discrepancies/"
            f"correction-task/{self.order_id}"
        )

    def _mark_correction_required(self):
        response = self.client.post(
            self.review_url,
            {
                "status": REVIEW_STATUS_CORRECTION_REQUIRED,
                "comment": "Нужно исправить документальное расхождение.",
                "next": self.report_url,
            },
        )
        self.assertEqual(response.status_code, 302)

    def _task_payload(self, *, assignee=None, due_at=None, comment=None):
        due_at = due_at or timezone.localtime() + timedelta(days=1)
        return {
            "assignee_id": (assignee or self.worker).pk,
            "due_at": due_at.strftime("%Y-%m-%dT%H:%M"),
            "comment": comment or "Проверить акт и исправить расхождение.",
            "next": self.report_url,
        }

    def test_correction_form_appears_only_after_required_review(self):
        initial = self.client.get(self.report_url)
        self.assertEqual(initial.status_code, 200)
        self.assertNotContains(initial, self.task_url)

        self._mark_correction_required()
        reviewed = self.client.get(self.report_url)

        self.assertContains(reviewed, self.task_url)
        self.assertContains(reviewed, "Выберите исполнителя")
        self.assertContains(reviewed, self.worker.full_name)
        self.assertContains(reviewed, "Создать задачу")

    def test_post_creates_internal_high_priority_task_without_quantity_changes(self):
        self._mark_correction_required()

        response = self.client.post(
            self.task_url,
            self._task_payload(),
        )

        self.assertRedirects(
            response,
            self.report_url,
            fetch_redirect_response=False,
        )
        task = Task.objects.get(
            title=closed_discrepancy_correction_task_title(self.order_id)
        )
        self.assertEqual(
            task.route,
            closed_discrepancy_correction_task_route(self.order_id),
        )
        self.assertEqual(task.assigned_to, self.worker)
        self.assertEqual(task.observer, self.head_employee)
        self.assertEqual(task.created_by, self.head_user)
        self.assertEqual(task.status, "in_progress")
        self.assertEqual(task.priority, "high")
        self.assertIn(
            "Проверить акт и исправить расхождение.",
            task.description,
        )

        task_event = OrderAuditEntry.objects.get(
            order_id=self.order_id,
            payload__has_key="processing_discrepancy_correction_task",
        )
        self.assertTrue(task_event.payload["internal_only"])
        task_payload = task_event.payload[
            "processing_discrepancy_correction_task"
        ]
        self.assertEqual(task_payload["task_id"], task.pk)
        self.assertEqual(task_payload["task_state"], "created")
        self.assertEqual(
            task_payload["quantity_snapshot"],
            {"declared_qty": 10, "processed_qty": 9, "boxed_qty": 10},
        )

        self.original_entry.refresh_from_db()
        self.assertEqual(
            self.original_entry.payload["processing_results"][0]["processed"],
            "9",
        )
        self.assertEqual(
            self.original_entry.payload["act_boxes"][0]["items"][0]["qty"],
            10,
        )

    def test_exact_repeat_is_idempotent(self):
        self._mark_correction_required()
        payload = self._task_payload()

        self.client.post(self.task_url, payload)
        self.client.post(self.task_url, payload)

        self.assertEqual(
            Task.objects.filter(
                title=closed_discrepancy_correction_task_title(self.order_id),
                route=closed_discrepancy_correction_task_route(self.order_id),
            ).count(),
            1,
        )
        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                payload__has_key="processing_discrepancy_correction_task",
            ).count(),
            1,
        )

    def test_changed_assignment_updates_same_task_and_audit(self):
        second_worker_user = User.objects.create_user(
            username="point10_worker_two",
            password="pwd",
        )
        second_worker = Employee.objects.create(
            user=second_worker_user,
            full_name="Второй обработчик",
            role="processing_worker",
            is_active=True,
        )
        self._mark_correction_required()
        self.client.post(self.task_url, self._task_payload())
        original_task = Task.objects.get(
            title=closed_discrepancy_correction_task_title(self.order_id)
        )

        response = self.client.post(
            self.task_url,
            self._task_payload(
                assignee=second_worker,
                due_at=timezone.localtime() + timedelta(days=2),
                comment="Перепроверить короба и приложить акт.",
            ),
            follow=True,
        )

        self.assertContains(response, f"Задача №{original_task.pk} обновлена.")
        original_task.refresh_from_db()
        self.assertEqual(original_task.assigned_to, second_worker)
        self.assertIn(
            "Перепроверить короба и приложить акт.",
            original_task.description,
        )
        self.assertEqual(
            Task.objects.filter(
                title=closed_discrepancy_correction_task_title(self.order_id)
            ).count(),
            1,
        )
        task_events = OrderAuditEntry.objects.filter(
            order_id=self.order_id,
            payload__has_key="processing_discrepancy_correction_task",
        ).order_by("created_at", "id")
        self.assertEqual(task_events.count(), 2)
        self.assertEqual(
            task_events.last().payload[
                "processing_discrepancy_correction_task"
            ]["task_state"],
            "updated",
        )

    def test_invalid_assignee_and_missing_review_are_rejected(self):
        manager_user = User.objects.create_user(
            username="point10_invalid_manager",
            password="pwd",
        )
        manager = Employee.objects.create(
            user=manager_user,
            full_name="Менеджер Пункт 10",
            role="manager",
            is_active=True,
        )

        missing_review = self.client.post(
            self.task_url,
            self._task_payload(),
            follow=True,
        )
        self.assertContains(
            missing_review,
            "Сначала зафиксируйте решение",
        )

        self._mark_correction_required()
        invalid_assignee = self.client.post(
            self.task_url,
            self._task_payload(assignee=manager),
            follow=True,
        )
        self.assertContains(
            invalid_assignee,
            "Исполнитель должен быть активным сотрудником обработки.",
        )
        self.assertFalse(
            Task.objects.filter(
                title=closed_discrepancy_correction_task_title(self.order_id)
            ).exists()
        )

    def test_non_processing_head_cannot_create_correction_task(self):
        self._mark_correction_required()
        manager_client = Client()
        _login(
            manager_client,
            username="point10_manager_access",
            role="manager",
        )

        response = manager_client.post(
            self.task_url,
            self._task_payload(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            Task.objects.filter(
                title=closed_discrepancy_correction_task_title(self.order_id)
            ).exists()
        )
