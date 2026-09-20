"""Unit and HTTP tests for LK home progress and live endpoints."""

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase

from client_cabinet.home_progress import enrich_request_row, request_progress
from client_cabinet.lk_status_map import (
    is_terminal_cancelled_status,
    resolve_lk_request_status,
)
from client_cabinet.web_ui import _lk_request_filter_status
from sku.models import Agency, Market, SKU
from sklad.test_utils import create_warehouse_snapshot_row

User = get_user_model()


class HomeProgressTests(SimpleTestCase):
    def test_deleted_as_error_is_final_cancelled_status(self):
        self.assertTrue(is_terminal_cancelled_status("Удалена как ошибочная"))
        resolved = resolve_lk_request_status(
            bucket="manager",
            status_label="Удалена как ошибочная",
            attention=True,
            payload={"status": "cancelled"},
        )
        self.assertEqual(resolved.filter_status, "cancelled")
        self.assertEqual(resolved.cancel_policy, "none")
        self.assertEqual(
            _lk_request_filter_status(
                bucket="manager",
                status_label="Удалена как ошибочная",
                attention=True,
            ),
            "cancelled",
        )

    def test_cancel_request_remains_waiting(self):
        resolved = resolve_lk_request_status(
            bucket="manager",
            status_label="Отмена на согласовании менеджера",
            attention=False,
            payload={"status": "cancel_requested"},
        )
        self.assertEqual(resolved.filter_status, "waiting")

    def test_receiving_in_work_progress(self):
        result = request_progress(
            order_type="receiving",
            bucket="warehouse",
            status="processing",
            status_label="В работе",
        )
        self.assertEqual(result["percent"], 65)
        self.assertEqual(result["stage_label"], "В работе")

    def test_draft_progress(self):
        result = request_progress(
            order_type="shipping",
            bucket="client",
            status="waiting",
            status_label="Черновик",
            is_draft=True,
        )
        self.assertEqual(result["percent"], 10)

    def test_done_progress(self):
        result = request_progress(
            order_type="processing",
            bucket="done",
            status="completed",
            status_label="Выполнена",
        )
        self.assertEqual(result["percent"], 100)

    def test_enrich_request_row(self):
        row = enrich_request_row(
            {
                "type": "shipping",
                "bucket": "warehouse",
                "status": "processing",
                "status_label": "Готова к отгрузке",
            }
        )
        self.assertEqual(row["progress_percent"], 80)
        self.assertIn("progress_stage", row)


class HomeLiveApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="lk_home_live", password="pwd")
        self.agency = Agency.objects.create(agn_name="ЛК live API", portal_user=self.user)
        self.http = Client()
        self.http.force_login(self.user)
        Market.objects.create(id=9501, name="WB")
        SKU.objects.create(agency=self.agency, sku_code="SKU-LIVE", name="Live")
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="RCV-LIVE",
            sku="SKU-LIVE",
            name="Live",
            qty=12,
            available_qty=12,
            box_code="BOX-LIVE-1",
            pallet_code="PAL-LIVE-1",
        )

    def test_live_endpoint_returns_available_stock(self):
        response = self.http.get(f"/client/api/v1/dashboard/live/?client={self.agency.id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["status"], "online")
        self.assertEqual(data["stock"]["units"], 12)
        self.assertIn("marketplace_deliveries", data)
        self.assertIn("wildberries", data["marketplace_deliveries"])

    def test_live_history_endpoint(self):
        response = self.http.get(
            f"/client/api/v1/dashboard/live-history/?limit=5&client={self.agency.id}"
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("results", payload["data"])
        self.assertLessEqual(len(payload["data"]["results"]), 5)

    def test_live_rejects_other_agency(self):
        other = Agency.objects.create(agn_name="Чужой")
        create_warehouse_snapshot_row(
            agency=other,
            order_type="receiving",
            order_id="RCV-OTHER",
            sku="SKU-OTHER",
            name="Other",
            qty=999,
            available_qty=999,
            box_code="BOX-OTHER-1",
            pallet_code="PAL-OTHER-1",
        )
        response = self.http.get(f"/client/api/v1/dashboard/live/?client={other.id}")
        # Portal client cannot read another agency: either denied, or scoped to own agency.
        if response.status_code == 200:
            data = response.json()["data"]
            self.assertEqual(data["stock"]["units"], 12)
            self.assertNotEqual(data["stock"]["units"], 999)
        else:
            self.assertIn(response.status_code, {403, 400})
