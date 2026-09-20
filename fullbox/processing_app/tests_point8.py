import json
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from audit.models import OrderAuditEntry
from sku.models import Agency

from .closed_discrepancy_audit import audit_closed_processing_discrepancies
from .stages import PROCESSING_STAGE_DONE, PROCESSING_STAGE_QUALITY_CONTROL


class ClosedProcessingDiscrepancyAuditTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Клиент аудита обработки")

    def _create_order(
        self,
        order_id,
        *,
        declared,
        processed,
        boxed,
        stage=PROCESSING_STAGE_DONE,
        discrepancy_status="",
    ):
        return OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            description="Тестовая история обработки",
            payload={
                "status": "done" if stage == PROCESSING_STAGE_DONE else "processing_in_work",
                "status_label": "Заявка завершена" if stage == PROCESSING_STAGE_DONE else "На проверке",
                "processing_stage": stage,
                "processing_stage_label": "Заявка закрыта" if stage == PROCESSING_STAGE_DONE else "Проверка",
                "discrepancy_status": discrepancy_status,
                "cards": [
                    {
                        "id": "card-a",
                        "article": "SKU-A",
                        "rows": [{"size": "42", "qty": str(declared)}],
                    }
                ],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {
                        "card_id": "card-a",
                        "article": "SKU-A",
                        "size": "42",
                        "processed": str(processed),
                    }
                ],
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": f"BOX-{order_id}",
                        "items": [{"sku": "SKU-A", "size": "42", "qty": boxed}],
                    }
                ],
                "act_pallets": [
                    {
                        "code": f"PAL-{order_id}",
                        "boxes": [f"BOX-{order_id}"],
                        "items": [],
                    }
                ],
            },
        )

    def test_reports_only_closed_quantity_mismatches_without_writes(self):
        matching_entry = self._create_order(
            "POINT8-MATCH",
            declared=10,
            processed=10,
            boxed=10,
        )
        mismatch_entry = self._create_order(
            "POINT8-MISMATCH",
            declared=10,
            processed=9,
            boxed=10,
            discrepancy_status="approved",
        )
        active_entry = self._create_order(
            "POINT8-ACTIVE",
            declared=10,
            processed=9,
            boxed=10,
            stage=PROCESSING_STAGE_QUALITY_CONTROL,
        )
        entry_count = OrderAuditEntry.objects.count()

        report = audit_closed_processing_discrepancies()

        self.assertEqual(report.scanned_count, 3)
        self.assertEqual(report.closed_count, 2)
        self.assertEqual(report.matching_count, 1)
        self.assertEqual(report.discrepancy_count, 1)
        self.assertFalse(report.sample_truncated)
        row = report.rows[0]
        self.assertEqual(row.order_id, "POINT8-MISMATCH")
        self.assertEqual(row.declared_qty, 10)
        self.assertEqual(row.processed_qty, 9)
        self.assertEqual(row.boxed_qty, 10)
        self.assertEqual(
            row.differences,
            ("declared_vs_processed", "processed_vs_boxed"),
        )
        self.assertEqual(row.discrepancy_status, "approved")
        self.assertEqual(OrderAuditEntry.objects.count(), entry_count)
        matching_entry.refresh_from_db()
        mismatch_entry.refresh_from_db()
        active_entry.refresh_from_db()
        self.assertEqual(matching_entry.payload["processing_stage"], PROCESSING_STAGE_DONE)
        self.assertEqual(mismatch_entry.payload["discrepancy_status"], "approved")
        self.assertEqual(active_entry.payload["processing_stage"], PROCESSING_STAGE_QUALITY_CONTROL)

    def test_sample_limit_does_not_change_total_discrepancy_count(self):
        self._create_order("POINT8-LIMIT-1", declared=5, processed=4, boxed=4)
        self._create_order("POINT8-LIMIT-2", declared=7, processed=7, boxed=6)

        report = audit_closed_processing_discrepancies(sample_limit=1)

        self.assertEqual(report.discrepancy_count, 2)
        self.assertEqual(len(report.rows), 1)
        self.assertTrue(report.sample_truncated)

    def test_order_filter_limits_read_only_audit(self):
        self._create_order("POINT8-FILTER-1", declared=5, processed=4, boxed=4)
        self._create_order("POINT8-FILTER-2", declared=7, processed=6, boxed=6)

        report = audit_closed_processing_discrepancies(
            order_ids=["POINT8-FILTER-2"],
        )

        self.assertEqual(report.scanned_count, 1)
        self.assertEqual(report.discrepancy_count, 1)
        self.assertEqual(report.rows[0].order_id, "POINT8-FILTER-2")

    def test_management_command_outputs_machine_readable_read_only_report(self):
        self._create_order(
            "POINT8-COMMAND",
            declared=11,
            processed=10,
            boxed=11,
            discrepancy_status="approved",
        )
        output = StringIO()

        call_command(
            "audit_closed_processing",
            "--format=json",
            stdout=output,
        )

        payload = json.loads(output.getvalue())
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["closed_count"], 1)
        self.assertEqual(payload["discrepancy_count"], 1)
        self.assertEqual(payload["rows"][0]["order_id"], "POINT8-COMMAND")
