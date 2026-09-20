from django.test import SimpleTestCase, TestCase
from django.contrib.auth import get_user_model

from audit.models import OrderAuditEntry
from client_cabinet.client_receiving_view import (
    format_client_datetime,
    localize_place_type,
    resolve_client_receiving_stage,
)
from client_cabinet.lk_requests import build_request_detail
from sku.models import Agency


class ReceivingClientViewHelpersTests(SimpleTestCase):
    def test_format_client_datetime_russian(self):
        self.assertEqual(
            format_client_datetime("2026-07-16T11:20:00+03:00"),
            "16 июля 2026, 11:20",
        )

    def test_localize_place_type(self):
        self.assertEqual(localize_place_type("box"), "Короба")
        self.assertEqual(localize_place_type("pallet"), "Палеты")

    def test_pending_stage(self):
        key, label = resolve_client_receiving_stage(
            status_value="sent_unconfirmed",
            status_label="Ждет подтверждения",
            bucket="manager",
            has_fact=False,
        )
        self.assertEqual(key, "pending")
        self.assertEqual(label, "Ожидает подтверждения менеджером")


class ReceivingDetailApiViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="rcv-view-client", password="pass")
        self.agency = Agency.objects.create(agn_name='ООО "Е-ГРУПП"', portal_user=self.user)
        self.client.force_login(self.user)

    def test_receiving_detail_has_client_view_without_fact_columns(self):
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="receiving",
            order_id="119",
            action="create",
            description="Создана заявка",
            payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
                "eta_at": "2026-07-16T11:20:00+03:00",
                "place_type": "box",
                "expected_boxes": 12,
                "comment": "",
                "items": [
                    {"sku_code": "61-1", "name": "61-1", "barcode": "2945577760374", "qty": 1},
                    {"sku_code": "61-2", "name": "61-2", "barcode": "2945577761722", "qty": 1},
                ],
            },
        )
        detail = build_request_detail(agency=self.agency, order_type="receiving", order_id="119")
        self.assertIsNotNone(detail)
        rcv = detail["receiving_view"]
        self.assertTrue(rcv["enabled"])
        self.assertEqual(rcv["status_label"], "Ожидает подтверждения менеджером")
        self.assertEqual(detail["status_label"], "Ожидает подтверждения менеджером")
        self.assertEqual(detail["status_pill"], "Ожидает подтверждения менеджером")
        self.assertFalse(rcv["show_fact_columns"])
        col_keys = [c["key"] for c in detail["columns"]]
        self.assertIn("qty_planned", col_keys)
        self.assertNotIn("qty_actual", col_keys)
        self.assertNotIn("qty_actual_display", col_keys)
        values = {card["key"]: card["value"] for card in rcv["summary"]}
        self.assertEqual(values["eta"], "16 июля 2026, 11:20")
        self.assertEqual(values["place_type"], "Короба")
        self.assertEqual(values["sku_count"], "2 SKU")
        self.assertEqual(detail["title"], "Заявка на приёмку")
        self.assertIn("менеджеру FullBox", rcv["status_hint"])
