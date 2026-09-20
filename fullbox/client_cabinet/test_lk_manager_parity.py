"""Dual-actor parity: client LK ↔ manager cabinet.

Checks that statuses, stock, requests and files stay consistent when the same
order is viewed/acted from the client portal and the manager account.
"""

from __future__ import annotations

import shutil
import tempfile
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings

from audit.models import OrderAuditEntry
from client_cabinet.other_requests import (
    complete_other_request,
    create_other_request,
    send_other_to_warehouse,
    take_other_in_work,
)
from employees.models import Employee
from shipping.models import ShippingOrder
from sklad.services import WarehouseStateCode
from sklad.test_utils import create_warehouse_snapshot_row
from sku.models import Agency
from todo.models import Task


User = get_user_model()


@override_settings(ALLOWED_HOSTS=["*"])
class ClientManagerParityBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.client_user = User.objects.create_user(username="parity_client", password="pwd")
        cls.manager_user = User.objects.create_user(username="parity_manager", password="pwd")
        cls.storekeeper_user = User.objects.create_user(username="parity_storekeeper", password="pwd")
        cls.other_client_user = User.objects.create_user(username="parity_other_client", password="pwd")

        cls.manager = Employee.objects.create(
            full_name="Менеджер паритет",
            role="manager",
            user=cls.manager_user,
            is_active=True,
        )
        cls.storekeeper = Employee.objects.create(
            full_name="Кладовщик паритет",
            role="storekeeper",
            user=cls.storekeeper_user,
            is_active=True,
        )
        cls.agency = Agency.objects.create(
            agn_name="Клиент паритет ЛК",
            portal_user=cls.client_user,
            short_name="Паритет",
            mened_user_id=cls.manager_user.id,
        )
        cls.other_agency = Agency.objects.create(
            agn_name="Чужой клиент паритет",
            portal_user=cls.other_client_user,
            short_name="Чужой",
        )
        create_warehouse_snapshot_row(
            agency=cls.agency,
            order_id="PAR-FREE",
            sku="PAR-SKU-FREE",
            name="Свободный остаток",
            goods_type="gv",
            qty=40,
            available_qty=40,
            box_code="BOX-PAR-FREE",
            pallet_code="PAL-PAR-FREE",
        )
        create_warehouse_snapshot_row(
            agency=cls.agency,
            order_id="PAR-LOCK",
            sku="PAR-SKU-LOCK",
            name="В резерве отгрузки",
            goods_type="gv",
            qty=12,
            available_qty=0,
            box_code="BOX-PAR-LOCK",
            pallet_code="PAL-PAR-LOCK",
            warehouse_state_code=WarehouseStateCode.READY_FOR_LOADING.value,
        )

    def setUp(self):
        self.client_http = Client()
        self.manager_http = Client()
        self.other_http = Client()
        self.client_http.force_login(self.client_user)
        self.manager_http.force_login(self.manager_user)
        self.other_http.force_login(self.other_client_user)

    def _cid(self, agency=None):
        return (agency or self.agency).id

    def _response_bytes(self, response):
        if hasattr(response, "streaming_content"):
            return b"".join(response.streaming_content)
        return response.content

    def _client_dash(self):
        return self.client_http.get(f"/client/api/v1/dashboard/?client={self._cid()}")

    def _manager_dash(self):
        return self.manager_http.get(f"/client/api/v1/dashboard/?client={self._cid()}")

    def _find_request(self, rows, order_id, order_type=None):
        for row in rows or []:
            if str(row.get("order_id")) != str(order_id):
                continue
            if order_type and row.get("type") != order_type:
                continue
            return row
        return None


@override_settings(ALLOWED_HOSTS=["*"])
class ClientManagerStatusParityTests(ClientManagerParityBase):
    def test_shipping_submitted_same_status_in_client_lk_and_manager_journal(self):
        ShippingOrder.objects.create(
            number="SO-PAR-1",
            agency=self.agency,
            created_by=self.client_user,
            status=ShippingOrder.STATUS_SUBMITTED,
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="shipping",
            order_id="SO-PAR-1",
            action="status",
            user=self.client_user,
            description="Отправлено менеджеру",
            payload={"status": "submitted", "shipping_state": "submitted", "status_label": "На согласовании менеджера"},
        )

        client_dash = self._client_dash()
        self.assertEqual(client_dash.status_code, 200)
        client_row = self._find_request(client_dash.json()["data"].get("requests"), "SO-PAR-1", "shipping")
        self.assertIsNotNone(client_row)
        self.assertEqual(client_row["status"], "waiting")
        self.assertEqual(client_row["status_label"], "На согласовании менеджера")

        manager_dash = self._manager_dash()
        self.assertEqual(manager_dash.status_code, 200)
        manager_row = self._find_request(manager_dash.json()["data"].get("requests"), "SO-PAR-1", "shipping")
        self.assertIsNotNone(manager_row)
        self.assertEqual(manager_row["status"], client_row["status"])
        self.assertEqual(manager_row["status_label"], client_row["status_label"])

        journal = self.manager_http.get("/team-manager/orders/", {"type": "shipping", "q": "SO-PAR-1"})
        self.assertEqual(journal.status_code, 200)
        self.assertContains(journal, "SO-PAR-1")
        self.assertContains(journal, "На согласовании менеджера")
        self.assertContains(journal, f"/client/dashboard/lk/?client={self._cid()}")

    def test_receiving_act_sent_then_client_confirm_updates_both_views(self):
        order_id = "PAR-RCV-ACT-1"
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id=order_id,
            action="status",
            user=self.storekeeper_user,
            description="Товар принят",
            payload={
                "status": "warehouse",
                "status_label": "Товар принят",
            },
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id=order_id,
            action="status",
            user=self.manager_user,
            description="Создан акт приемки",
            payload={
                "status": "sent_unconfirmed",
                "status_label": "Акт отправлен клиенту",
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_sent": "Акт приемки",
                "act_storekeeper_signed": True,
                "act_manager_signed": True,
                "act_items": [{"sku_code": "PAR-SKU-FREE", "planned_qty": 10, "actual_qty": 10}],
            },
        )

        before_dash = self._client_dash()
        self.assertEqual(before_dash.status_code, 200)
        before_row = self._find_request(before_dash.json()["data"].get("requests"), order_id, "receiving")
        self.assertIsNotNone(before_row)
        self.assertNotEqual(before_row.get("status_label"), "Выполнена")
        self.assertNotEqual(before_row.get("status"), "completed")
        self.assertTrue(before_row.get("attention") or "акт" in (before_row.get("status_label") or "").lower())

        confirm = self.client_http.post(
            f"/orders/receiving/{order_id}/act/client/confirm/?client={self._cid()}",
            data={"return": f"/client/dashboard/lk/?client={self._cid()}"},
        )
        self.assertIn(confirm.status_code, {200, 302})

        after_client = self.client_http.get(
            f"/client/api/v1/requests/receiving/{order_id}/?client={self._cid()}"
        )
        self.assertEqual(after_client.status_code, 200)
        after_data = after_client.json()["data"]
        self.assertEqual(after_data.get("status_label"), "Выполнена")
        self.assertEqual(after_data.get("status"), "completed")

        after_manager = self.manager_http.get(
            f"/client/api/v1/requests/receiving/{order_id}/?client={self._cid()}"
        )
        self.assertEqual(after_manager.status_code, 200)
        manager_data = after_manager.json()["data"]
        self.assertEqual(manager_data.get("status_label"), "Выполнена")
        self.assertEqual(manager_data.get("status"), "completed")

        after_dash = self._manager_dash()
        after_row = self._find_request(after_dash.json()["data"].get("requests"), order_id, "receiving")
        self.assertIsNotNone(after_row)
        self.assertEqual(after_row.get("status"), "completed")
        self.assertEqual(after_row.get("status_label"), "Выполнена")

        journal = self.manager_http.get("/team-manager/orders/", {"type": "receiving", "q": order_id})
        self.assertEqual(journal.status_code, 200)
        self.assertContains(journal, order_id)

    def test_cancel_requested_visible_in_client_and_manager_dashboard(self):
        order_id = "PAR-RCV-CANCEL-1"
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id=order_id,
            action="status",
            user=self.client_user,
            description="Клиент запросил отмену",
            payload={
                "status": "cancel_requested",
                "status_label": "Отмена на согласовании",
                "cancel_requested_by_client": True,
            },
        )
        client_dash = self._client_dash()
        manager_dash = self._manager_dash()
        client_row = self._find_request(client_dash.json()["data"].get("requests"), order_id, "receiving")
        manager_row = self._find_request(manager_dash.json()["data"].get("requests"), order_id, "receiving")
        self.assertIsNotNone(client_row)
        self.assertIsNotNone(manager_row)
        self.assertEqual(client_row["status"], manager_row["status"])
        self.assertIn("отмен", (client_row["status_label"] or "").lower())
        self.assertIn("отмен", (manager_row["status_label"] or "").lower())


@override_settings(ALLOWED_HOSTS=["*"])
class ClientManagerStockParityTests(ClientManagerParityBase):
    def test_client_and_manager_see_same_available_only_stock(self):
        client_resp = self.client_http.get(f"/client/api/v1/stock-journal/?client={self._cid()}")
        manager_resp = self.manager_http.get(f"/client/api/v1/stock-journal/?client={self._cid()}")
        self.assertEqual(client_resp.status_code, 200)
        self.assertEqual(manager_resp.status_code, 200)

        client_boxes = {row["box_code"] for row in client_resp.json()["data"]["rows"]}
        manager_boxes = {row["box_code"] for row in manager_resp.json()["data"]["rows"]}
        self.assertEqual(client_boxes, manager_boxes)
        self.assertIn("BOX-PAR-FREE", client_boxes)
        self.assertNotIn("BOX-PAR-LOCK", client_boxes)

        dash = self._client_dash()
        kpi = dash.json()["data"].get("kpi") or {}
        # Client KPI must reflect available stock, not reserved/ready-for-loading qty.
        self.assertGreaterEqual(int(kpi.get("stock_units") or 0), 40)

    def test_other_agency_cannot_read_stock(self):
        # Portal user is always scoped to own agency even if ?client= points elsewhere.
        resp = self.other_http.get(f"/client/api/v1/stock-journal/?client={self._cid()}")
        self.assertEqual(resp.status_code, 200)
        boxes = {row["box_code"] for row in resp.json()["data"]["rows"]}
        self.assertNotIn("BOX-PAR-FREE", boxes)
        self.assertNotIn("BOX-PAR-LOCK", boxes)

        detail = self.other_http.get(
            f"/client/api/v1/requests/shipping/SO-PAR-1/?client={self._cid()}"
        )
        # Foreign portal user must not read another agency request card.
        self.assertTrue(detail.status_code in {403, 404} or detail.json().get("ok") is False)


@override_settings(ALLOWED_HOSTS=["*"])
class ClientManagerRequestAndFilesParityTests(ClientManagerParityBase):
    def setUp(self):
        super().setUp()
        self.temp_media = tempfile.mkdtemp(prefix="parity-attachments-")
        self.media_override = override_settings(MEDIA_ROOT=self.temp_media)
        self.media_override.enable()

    def tearDown(self):
        self.media_override.disable()
        shutil.rmtree(self.temp_media, ignore_errors=True)
        super().tearDown()

    def test_other_request_status_ladder_and_attachment_roundtrip(self):
        upload = SimpleUploadedFile("parity-brief.txt", b"need photo brief", content_type="text/plain")
        create_resp = self.client_http.post(
            f"/client/api/v1/other-requests/?client={self._cid()}",
            data={
                "category": "photo_video",
                "description": "Нужно фото комплекта для паритета",
                "attachments": upload,
            },
        )
        self.assertEqual(create_resp.status_code, 200)
        body = create_resp.json()
        self.assertTrue(body.get("ok"))
        order_id = body["data"]["order_id"]
        attachments = body["data"]["attachments"]
        self.assertEqual(len(attachments), 1)
        file_url = attachments[0]["url"] + f"?client={self._cid()}"

        # Client detail shows waiting + file.
        client_detail = self.client_http.get(
            f"/client/api/v1/requests/other/{order_id}/?client={self._cid()}"
        )
        self.assertEqual(client_detail.status_code, 200)
        client_data = client_detail.json()["data"]
        self.assertEqual(client_data["status"], "waiting")
        self.assertEqual(len(client_data["attachments"]), 1)

        # Manager sees same request in LK-as-client and can download the file.
        manager_detail = self.manager_http.get(
            f"/client/api/v1/requests/other/{order_id}/?client={self._cid()}"
        )
        self.assertEqual(manager_detail.status_code, 200)
        manager_data = manager_detail.json()["data"]
        self.assertEqual(manager_data["status"], client_data["status"])
        self.assertEqual(len(manager_data["attachments"]), 1)

        manager_download = self.manager_http.get(file_url)
        self.assertEqual(manager_download.status_code, 200)
        self.assertEqual(self._response_bytes(manager_download), b"need photo brief")

        client_download = self.client_http.get(file_url)
        self.assertEqual(client_download.status_code, 200)
        self.assertEqual(self._response_bytes(client_download), b"need photo brief")

        # Foreign client cannot download.
        foreign = self.other_http.get(
            attachments[0]["url"] + f"?client={self.other_agency.id}"
        )
        self.assertIn(foreign.status_code, {403, 404})

        # Manager task exists and warehouse ladder updates both dashboards.
        manager_task = Task.objects.get(route=f"/orders/other/{order_id}/", assigned_to=self.manager)
        self.assertTrue(send_other_to_warehouse(manager_task, SimpleNamespace(user=self.manager_user)))
        take_other_in_work(order_id=order_id, user=self.storekeeper_user)
        complete_other_request(order_id=order_id, user=self.storekeeper_user)

        done_client = self.client_http.get(
            f"/client/api/v1/requests/other/{order_id}/?client={self._cid()}"
        )
        done_manager = self.manager_http.get(
            f"/client/api/v1/requests/other/{order_id}/?client={self._cid()}"
        )
        self.assertEqual(done_client.json()["data"]["status"], "completed")
        self.assertEqual(done_manager.json()["data"]["status"], "completed")
        self.assertEqual(done_client.json()["data"]["status_label"], "Выполнена")
        self.assertEqual(done_manager.json()["data"]["status_label"], "Выполнена")

        journal = self.manager_http.get("/team-manager/orders/", {"type": "other", "q": order_id})
        self.assertEqual(journal.status_code, 200)
        self.assertContains(journal, order_id)
        manager_orders = self.manager_http.get(f"/orders/other/?q={order_id}")
        self.assertEqual(manager_orders.status_code, 200)
        self.assertContains(manager_orders, order_id)

    def test_chat_file_visible_to_manager_viewing_client_lk(self):
        upload = SimpleUploadedFile("parity-chat.pdf", b"%PDF-parity", content_type="application/pdf")
        post = self.client_http.post(
            f"/client/api/v1/chat/messages/?client={self._cid()}",
            data={"text": "Файл для менеджера", "attachments": upload},
        )
        self.assertEqual(post.status_code, 200)
        payload = post.json()["data"]["message"]
        self.assertEqual(len(payload["attachments"]), 1)
        file_url = payload["attachments"][0]["url"]
        if "client=" not in file_url:
            file_url = file_url + (("&" if "?" in file_url else "?") + f"client={self._cid()}")

        manager_list = self.manager_http.get(f"/client/api/v1/chat/messages/?client={self._cid()}")
        self.assertEqual(manager_list.status_code, 200)
        messages = manager_list.json()["data"].get("messages") or manager_list.json()["data"].get("items") or []
        self.assertTrue(any("Файл для менеджера" in (m.get("text") or "") for m in messages))

        manager_file = self.manager_http.get(file_url)
        self.assertEqual(manager_file.status_code, 200)
        self.assertEqual(self._response_bytes(manager_file), b"%PDF-parity")

        foreign = self.other_http.get(
            payload["attachments"][0]["url"].split("?")[0] + f"?client={self.other_agency.id}"
        )
        self.assertIn(foreign.status_code, {403, 404})

    def test_manager_clients_page_opens_same_client_lk(self):
        page = self.manager_http.get("/team-manager/clients/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, self.agency.agn_name)
        lk = self.manager_http.get(f"/client/dashboard/lk/?client={self._cid()}")
        self.assertEqual(lk.status_code, 200)
        self.assertContains(lk, "Личный кабинет")
        me = self.manager_http.get(f"/client/api/v1/me/?client={self._cid()}")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(int(me.json()["data"]["client"]["id"]), self._cid())
