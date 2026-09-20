from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from audit.models import OrderAuditEntry
from orders.services import ReceivingWorkflowService
from orders.web_ui import _current_status_entry
from sku.models import Agency


class ReceivingAutosaveGuardTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="receiving_autosave_guard",
            password="pwd",
        )
        self.agency = Agency.objects.create(agn_name="Клиент защиты автосохранения")

    def test_late_autosave_does_not_roll_sent_order_back_to_draft(self):
        order_id = "PR-AUTOSAVE-GUARD"
        sent_payload = {
            "status": "sent_unconfirmed",
            "status_label": "Ждет подтверждения",
            "submit_action": "send",
            "items": [],
        }
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Отправлено менеджеру",
            payload=sent_payload,
        )
        entry_count = OrderAuditEntry.objects.filter(
            order_id=order_id,
            order_type="receiving",
        ).count()

        result = ReceivingWorkflowService.submit_receiving_order(
            order_id=order_id,
            agency=self.agency,
            payload={
                "status": "draft",
                "status_label": "Черновик",
                "submit_action": "draft",
                "items": [],
            },
            submit_action="draft",
            user=self.user,
            existing_order_id=order_id,
            old_payload=sent_payload,
            is_autosave=True,
        )

        self.assertEqual(result.status_value, "sent_unconfirmed")
        self.assertEqual(result.status_label, "Ждет подтверждения")
        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
            ).count(),
            entry_count,
        )

    def test_status_resolver_ignores_historical_late_draft(self):
        order_id = "PR-AUTOSAVE-HISTORY"
        sent_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Отправлено менеджеру",
            payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
                "submit_action": "send",
            },
        )
        draft_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Позднее автосохранение",
            payload={
                "status": "draft",
                "status_label": "Черновик",
                "submit_action": "draft",
            },
        )

        self.assertEqual(
            _current_status_entry([sent_entry, draft_entry]),
            sent_entry,
        )


class ReceivingFlowClientAutosaveGuardTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.template_source = (
            Path(__file__).resolve().parent
            / "templates"
            / "orders"
            / "receiving_flow.html"
        ).read_text(encoding="utf-8")

    def test_draft_fetch_survives_label_navigation(self):
        self.assertIn("keepalive: requestKeepalive,", self.template_source)
        self.assertIn("return byteLength < 56 * 1024;", self.template_source)

    def test_page_leave_does_not_race_an_active_draft_save(self):
        self.assertIn(
            "if (draftSaveInFlight && draftSaveInFlightKeepalive) {",
            self.template_source,
        )

    def test_box_label_waits_for_confirmed_draft_save(self):
        self.assertIn(
            "saveDraftAfterContainerClose().then((saved) => {",
            self.template_source,
        )
        self.assertIn(
            "if (saved && closedBoxCode && typeof options.afterClose === 'function')",
            self.template_source,
        )

    def test_stale_draft_is_shown_and_reloaded_immediately(self):
        self.assertIn("let draftConflictReloading = false;", self.template_source)
        self.assertIn("window.location.reload();", self.template_source)
        self.assertIn("Повторите только последний скан", self.template_source)
