import json
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from audit.models import OrderAuditEntry
from sku.models import Agency
from todo.models import Task

from .order_audit import audit_processing_orders


class ProcessingOrderAuditTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Клиент аудита живых заявок")

    def _create_order(self, order_id, payload):
        return OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            description="Тестовая история обработки",
            payload=payload,
        )

    def test_audit_reports_missing_stage_and_open_tasks_without_writes(self):
        self._create_order(
            "POINT11-ACTIVE",
            {
                "status": "processing_in_work",
                "status_label": "",
                "cards": [],
                "processed_cards": [],
            },
        )
        Task.objects.create(
            title="Задача на обработку",
            route="/orders/processing/POINT11-ACTIVE/flow/",
            status="backlog",
        )
        entry_count = OrderAuditEntry.objects.count()

        report = audit_processing_orders(order_ids=["POINT11-ACTIVE"])

        self.assertTrue(report.read_only)
        self.assertEqual(report.scanned_count, 1)
        self.assertEqual(len(report.rows), 1)
        row = report.rows[0]
        self.assertEqual(row.order_id, "POINT11-ACTIVE")
        self.assertEqual(row.stage, "")
        self.assertEqual(row.open_tasks_count, 1)
        self.assertTrue(row.has_issues)
        self.assertEqual(OrderAuditEntry.objects.count(), entry_count)

    def test_management_command_outputs_read_only_json(self):
        self._create_order(
            "POINT11-COMMAND",
            {
                "status": "processing_in_work",
                "processing_stage": "unboxing_opened",
                "processing_stage_label": "открыта раскоробовка",
                "cards": [],
                "processed_cards": [],
            },
        )
        output = StringIO()

        call_command(
            "audit_processing_orders",
            "--order-id=POINT11-COMMAND",
            "--format=json",
            stdout=output,
        )

        payload = json.loads(output.getvalue())
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["scanned_count"], 1)
        self.assertEqual(payload["rows"][0]["order_id"], "POINT11-COMMAND")
        self.assertEqual(payload["rows"][0]["stage"], "unboxing_opened")
