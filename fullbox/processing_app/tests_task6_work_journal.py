from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from audit.models import OrderAuditEntry
from employees.models import Employee
from sku.models import Agency

from .models import ProcessingWorkEvent
from .work_journal import (
    record_processing_assignment_completion,
    record_processing_box_completions,
)


class ProcessingWorkJournalTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Task 6 journal agency")

    def test_assignment_completion_is_linked_to_employee_and_idempotent(self):
        employee = Employee.objects.create(
            full_name="Упаковщица без логина",
            role="packer",
            is_active=True,
        )
        assignment = {
            "id": "assignment-1",
            "assignee_id": employee.pk,
            "assignee_name": employee.full_name,
            "operation_key": "labeling",
            "operation_label": "Маркировка",
            "planned_qty": 12,
            "actual_qty": 12,
            "status": "completed",
        }

        first, first_created = record_processing_assignment_completion(
            order_id="25",
            agency=self.agency,
            assignment=assignment,
            occurred_at=timezone.now(),
        )
        second, second_created = record_processing_assignment_completion(
            order_id="25",
            agency=self.agency,
            assignment=assignment,
            occurred_at=timezone.now(),
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.employee, employee)
        self.assertEqual(first.units, 12)
        self.assertEqual(ProcessingWorkEvent.objects.count(), 1)

    def test_box_completion_records_owner_and_does_not_duplicate(self):
        user = get_user_model().objects.create_user(username="box-worker")
        employee = Employee.objects.create(
            full_name="Формировщик коробов",
            role="processing_worker",
            user=user,
            is_active=True,
        )
        boxes = [
            {
                "code": "BOX-001",
                "owner_user_id": user.pk,
                "owner_user_label": employee.full_name,
                "items": [{"qty": 7}, {"qty": 5}],
            }
        ]

        created_first = record_processing_box_completions(
            order_id="28",
            agency=self.agency,
            boxes=boxes,
            occurred_at=timezone.now(),
        )
        created_second = record_processing_box_completions(
            order_id="28",
            agency=self.agency,
            boxes=boxes,
            occurred_at=timezone.now(),
        )

        event = ProcessingWorkEvent.objects.get()
        self.assertEqual(created_first, 1)
        self.assertEqual(created_second, 0)
        self.assertEqual(event.employee, employee)
        self.assertEqual(event.boxes, 1)
        self.assertEqual(event.units, 12)

    def test_backfill_is_dry_run_by_default_and_idempotent_on_apply(self):
        user = get_user_model().objects.create_user(username="backfill-worker")
        employee = Employee.objects.create(
            full_name="Исторический обработчик",
            role="processing_worker",
            user=user,
            is_active=True,
        )
        occurred_at = timezone.now()
        OrderAuditEntry.objects.create(
            order_id="historic-25",
            order_type="processing",
            action="update",
            agency=self.agency,
            user=user,
            payload={
                "processing_work_assignments": [
                    {
                        "id": "historic-assignment",
                        "assignee_id": employee.pk,
                        "assignee_name": employee.full_name,
                        "operation_key": "labeling",
                        "operation_label": "Маркировка",
                        "actual_qty": 4,
                        "status": "completed",
                        "completed_at": occurred_at.isoformat(),
                    }
                ],
                "act_boxes": [
                    {
                        "code": "HISTORIC-BOX",
                        "owner_user_id": user.pk,
                        "owner_user_label": employee.full_name,
                        "items": [{"qty": 4}],
                    },
                    {
                        "code": "OWNERLESS-BOX",
                        "items": [{"qty": 2}],
                    },
                ],
                "flow_closed_at": occurred_at.isoformat(),
            },
        )

        dry_output = StringIO()
        call_command("backfill_processing_work_events", stdout=dry_output)
        self.assertEqual(ProcessingWorkEvent.objects.count(), 0)
        self.assertIn("ownerless_boxes_skipped=1", dry_output.getvalue())

        call_command("backfill_processing_work_events", "--apply", stdout=StringIO())
        call_command("backfill_processing_work_events", "--apply", stdout=StringIO())

        self.assertEqual(ProcessingWorkEvent.objects.count(), 2)
        self.assertEqual(
            ProcessingWorkEvent.objects.filter(employee=employee).count(),
            2,
        )
