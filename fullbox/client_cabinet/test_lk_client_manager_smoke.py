"""Smoke tests: client LK shell/APIs + manager cabinet entry into client LK."""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from audit.models import OrderAuditEntry
from client_cabinet.api_views import _stock_journal_rows
from employees.models import Employee
from shipping.models import ShippingOrder
from sklad.services import WarehouseStateCode
from sklad.test_utils import create_warehouse_snapshot_row
from sku.models import Agency


User = get_user_model()


@override_settings(ALLOWED_HOSTS=["*"])
class ClientLkSmokeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="lk_smoke_client", password="pwd")
        cls.agency = Agency.objects.create(
            agn_name="ЛК Smoke Client",
            portal_user=cls.user,
            short_name="Smoke",
        )
        create_warehouse_snapshot_row(
            agency=cls.agency,
            order_id="SMK-FREE",
            sku="SMK-SKU-1",
            name="Свободный товар",
            goods_type="gv",
            qty=20,
            available_qty=20,
            box_code="BOX-SMK-FREE",
            pallet_code="PAL-SMK-FREE",
        )
        create_warehouse_snapshot_row(
            agency=cls.agency,
            order_id="SMK-LOCK",
            sku="SMK-SKU-2",
            name="В отгрузке",
            goods_type="gv",
            qty=8,
            available_qty=0,
            box_code="BOX-SMK-LOCK",
            pallet_code="PAL-SMK-LOCK",
            warehouse_state_code=WarehouseStateCode.READY_FOR_LOADING.value,
        )
        ShippingOrder.objects.create(
            number="SO-SMK-1",
            agency=cls.agency,
            created_by=cls.user,
            status=ShippingOrder.STATUS_SUBMITTED,
        )
        OrderAuditEntry.objects.create(
            agency=cls.agency,
            order_type="shipping",
            order_id="SO-SMK-1",
            action="status",
            user=cls.user,
            description="Отправлено менеджеру",
            payload={"status": "submitted", "shipping_state": "submitted"},
        )

    def setUp(self):
        self.http = Client()
        self.http.force_login(self.user)

    def test_lk_shell_and_me(self):
        page = self.http.get(f"/client/dashboard/lk/?client={self.agency.id}")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Личный кабинет")

        me = self.http.get(f"/client/api/v1/me/?client={self.agency.id}")
        self.assertEqual(me.status_code, 200)
        body = me.json()
        self.assertTrue(body.get("ok"))
        self.assertEqual(int(body["data"]["client"]["id"]), self.agency.id)

    def test_dashboard_lists_shipping_request(self):
        dash = self.http.get(f"/client/api/v1/dashboard/?client={self.agency.id}")
        self.assertEqual(dash.status_code, 200)
        data = dash.json()["data"]
        rows = data.get("requests") or []
        own = next((r for r in rows if r.get("order_id") == "SO-SMK-1"), None)
        self.assertIsNotNone(own)
        self.assertEqual(own.get("type"), "shipping")
        self.assertEqual(own.get("status_label"), "На согласовании менеджера")
        self.assertEqual(own.get("status"), "waiting")

    def test_stock_journal_shows_only_available_rows(self):
        resp = self.http.get(f"/client/api/v1/stock-journal/?client={self.agency.id}")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["data"]["rows"]
        boxes = {row["box_code"] for row in rows}
        self.assertIn("BOX-SMK-FREE", boxes)
        self.assertNotIn("BOX-SMK-LOCK", boxes)

        helper_rows = _stock_journal_rows(self.agency, hide_consumed=True)
        helper_boxes = {row["box_code"] for row in helper_rows}
        self.assertNotIn("BOX-SMK-LOCK", helper_boxes)

    def test_request_detail_shipping(self):
        detail = self.http.get(
            f"/client/api/v1/requests/shipping/SO-SMK-1/?client={self.agency.id}"
        )
        self.assertEqual(detail.status_code, 200)
        body = detail.json()["data"]
        self.assertEqual(body["type"], "shipping")
        self.assertEqual(body["status_label"], "На согласовании менеджера")


@override_settings(ALLOWED_HOSTS=["*"])
class ManagerLkEntrySmokeTests(TestCase):
    """Manager cabinet can open client LK for selected agency."""

    @classmethod
    def setUpTestData(cls):
        cls.manager_user = User.objects.create_user(username="lk_smoke_manager", password="pwd")
        Employee.objects.create(
            full_name="Менеджер Smoke",
            role="manager",
            user=cls.manager_user,
            is_active=True,
        )
        cls.client_user = User.objects.create_user(username="lk_smoke_portal", password="pwd")
        cls.agency = Agency.objects.create(
            agn_name="Клиент для менеджера smoke",
            portal_user=cls.client_user,
            inn="7701234567",
        )

    def setUp(self):
        self.http = Client()
        self.http.force_login(self.manager_user)

    def test_manager_clients_link_to_client_lk(self):
        page = self.http.get("/team-manager/clients/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, self.agency.agn_name)
        self.assertContains(page, f"/client/dashboard/lk/?client={self.agency.id}")

    def test_manager_opens_client_lk(self):
        lk = self.http.get(f"/client/dashboard/lk/?client={self.agency.id}")
        self.assertEqual(lk.status_code, 200)
        me = self.http.get(f"/client/api/v1/me/?client={self.agency.id}")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(int(me.json()["data"]["client"]["id"]), self.agency.id)
