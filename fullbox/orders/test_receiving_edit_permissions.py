from types import SimpleNamespace

from django.test import SimpleTestCase

from orders.web_ui import (
    _can_client_edit_receiving,
    _can_manager_edit_receiving,
    _is_awaiting_manager_receiving,
    _is_receiving_at_or_after_warehouse,
)
from processing_app.stages import processing_client_can_edit, processing_is_locked_for_edit


def _entry(status: str, *, agency_id: int = 1, status_label: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        agency_id=agency_id,
        action="status",
        user=None,
        agency=None,
        payload={
            "status": status,
            "status_label": status_label,
            "submit_action": status,
        },
    )


class ReceivingEditPermissionHelpersTests(SimpleTestCase):
    def test_client_can_edit_only_draft(self):
        agency = SimpleNamespace(id=7)
        draft = [_entry("draft", agency_id=7)]
        awaiting = [_entry("sent_unconfirmed", agency_id=7, status_label="Ждет подтверждения")]
        self.assertTrue(_can_client_edit_receiving(draft, agency))
        self.assertFalse(_can_client_edit_receiving(awaiting, agency))
        self.assertTrue(_is_awaiting_manager_receiving(awaiting))

    def test_client_cannot_edit_after_warehouse(self):
        agency = SimpleNamespace(id=7)
        warehouse = [_entry("warehouse", agency_id=7, status_label="В ожидании поставки товара")]
        self.assertTrue(_is_receiving_at_or_after_warehouse(warehouse))
        self.assertFalse(_can_client_edit_receiving(warehouse, agency))
        self.assertFalse(_can_manager_edit_receiving(warehouse))

    def test_manager_can_edit_before_warehouse(self):
        awaiting = [_entry("sent_unconfirmed", agency_id=7)]
        self.assertTrue(_can_manager_edit_receiving(awaiting))


class ProcessingClientEditHelpersTests(SimpleTestCase):
    def test_client_can_edit_only_draft(self):
        self.assertFalse(
            processing_client_can_edit(
                {"processing_stage": "awaiting_approval", "status": "sent_unconfirmed"}
            )
        )
        self.assertTrue(processing_client_can_edit({"processing_stage": "draft", "status": "draft"}))

    def test_client_cannot_edit_after_manager_approved(self):
        payload = {"processing_stage": "manager_approved", "status": "approved"}
        self.assertTrue(processing_is_locked_for_edit(payload))
        self.assertFalse(processing_client_can_edit(payload))
