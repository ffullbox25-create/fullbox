from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from audit.models import OrderAuditEntry
from employees.models import Employee
from processing_app.closed_discrepancy_audit import (
    audit_closed_processing_discrepancies,
)
from processing_app.closed_discrepancy_review import (
    REVIEW_STATUS_CORRECTION_REQUIRED,
    REVIEW_STATUS_EXPLAINED,
)
from processing_app.stages import PROCESSING_STAGE_DONE
from sku.models import Agency


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
    return user


@override_settings(ALLOWED_HOSTS=["testserver"])
class ProcessingClosedDiscrepancyReviewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.head_user = _login(
            self.client,
            username="point9_processing_head",
            role="processing_head",
        )
        self.agency = Agency.objects.create(agn_name="ООО Пункт 9")
        self.order_id = "POINT9-MISMATCH"
        self.original_entry = self._create_closed_order(
            order_id=self.order_id,
            declared=10,
            processed=9,
            boxed=10,
        )

    def _create_closed_order(self, *, order_id, declared, processed, boxed):
        return OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            description="Закрытая заявка для пункта 9",
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
                        "items": [
                            {"sku": "SKU-A", "size": "42", "qty": boxed}
                        ],
                    }
                ],
                "act_pallets": [],
            },
        )

    def test_report_exposes_closed_mismatch_and_review_form(self):
        catalog = self.client.get("/processing-head/reports")
        detail = self.client.get(
            "/processing-head/reports/closed-discrepancies"
        )

        self.assertEqual(catalog.status_code, 200)
        self.assertContains(catalog, "Закрытые заявки с расхождениями")
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, self.order_id)
        self.assertContains(detail, "заявлено ≠ обработано")
        self.assertContains(
            detail,
            f"/processing-head/reports/closed-discrepancies/review/{self.order_id}",
        )
        self.assertContains(detail, "За всё время")
        self.assertContains(detail, ">10<", html=False)
        self.assertContains(detail, ">9<", html=False)

    def test_review_writes_internal_event_without_changing_quantities(self):
        response = self.client.post(
            f"/processing-head/reports/closed-discrepancies/review/{self.order_id}",
            {
                "status": REVIEW_STATUS_EXPLAINED,
                "comment": "Подтверждено актом клиента.",
                "next": "/processing-head/reports/closed-discrepancies",
            },
        )

        self.assertRedirects(
            response,
            "/processing-head/reports/closed-discrepancies",
            fetch_redirect_response=False,
        )
        review_entry = OrderAuditEntry.objects.filter(
            order_id=self.order_id,
            payload__has_key="processing_discrepancy_review",
        ).get()
        self.assertTrue(review_entry.payload["internal_only"])
        self.assertEqual(
            review_entry.payload["processing_discrepancy_review"]["status"],
            REVIEW_STATUS_EXPLAINED,
        )
        self.assertEqual(
            review_entry.payload["processing_discrepancy_review"][
                "quantity_snapshot"
            ],
            {
                "declared_qty": 10,
                "processed_qty": 9,
                "boxed_qty": 10,
            },
        )
        self.original_entry.refresh_from_db()
        self.assertEqual(
            self.original_entry.payload["processing_results"][0]["processed"],
            "9",
        )
        report = audit_closed_processing_discrepancies(
            order_ids=[self.order_id]
        )
        self.assertEqual(report.rows[0].declared_qty, 10)
        self.assertEqual(report.rows[0].processed_qty, 9)
        self.assertEqual(report.rows[0].boxed_qty, 10)
        self.assertEqual(
            report.rows[0].review_status,
            REVIEW_STATUS_EXPLAINED,
        )

    def test_duplicate_review_is_idempotent(self):
        payload = {
            "status": REVIEW_STATUS_CORRECTION_REQUIRED,
            "comment": "Нужна отдельная служебная корректировка.",
            "next": "/processing-head/reports/closed-discrepancies",
        }

        self.client.post(
            f"/processing-head/reports/closed-discrepancies/review/{self.order_id}",
            payload,
        )
        self.client.post(
            f"/processing-head/reports/closed-discrepancies/review/{self.order_id}",
            payload,
        )

        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                payload__has_key="processing_discrepancy_review",
            ).count(),
            1,
        )

    def test_final_decision_requires_comment(self):
        response = self.client.post(
            f"/processing-head/reports/closed-discrepancies/review/{self.order_id}",
            {
                "status": REVIEW_STATUS_CORRECTION_REQUIRED,
                "comment": "",
                "next": "/processing-head/reports/closed-discrepancies",
            },
            follow=True,
        )

        self.assertContains(
            response,
            "Для итогового решения обязательно укажите причину.",
        )
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                payload__has_key="processing_discrepancy_review",
            ).exists()
        )

    def test_non_processing_head_cannot_record_review(self):
        manager_client = Client()
        _login(
            manager_client,
            username="point9_manager",
            role="manager",
        )

        response = manager_client.post(
            f"/processing-head/reports/closed-discrepancies/review/{self.order_id}",
            {
                "status": REVIEW_STATUS_EXPLAINED,
                "comment": "Попытка менеджера.",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                payload__has_key="processing_discrepancy_review",
            ).exists()
        )

    def test_matching_closed_order_cannot_be_reviewed(self):
        matching_order_id = "POINT9-MATCH"
        self._create_closed_order(
            order_id=matching_order_id,
            declared=10,
            processed=10,
            boxed=10,
        )

        response = self.client.post(
            (
                "/processing-head/reports/closed-discrepancies/review/"
                f"{matching_order_id}"
            ),
            {
                "status": REVIEW_STATUS_EXPLAINED,
                "comment": "Расхождения нет.",
                "next": "/processing-head/reports/closed-discrepancies",
            },
            follow=True,
        )

        self.assertContains(
            response,
            "Заявка не закрыта или расхождение количеств отсутствует.",
        )
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=matching_order_id,
                payload__has_key="processing_discrepancy_review",
            ).exists()
        )
