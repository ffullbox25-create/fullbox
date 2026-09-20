"""Полный suite ЛК клиента и менеджера: приёмка, обработка, отгрузка, остатки, прочие.

Проверяет согласованность статусов и видимости между:
- клиентским ЛК (`/client/dashboard/lk/`, `/client/api/v1/...`)
- кабинетом менеджера (`/team-manager/...` и вход в ЛК клиента)
"""

from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from audit.models import OrderAuditEntry
from client_cabinet.client_drafts import list_client_draft_order_ids
from client_cabinet.other_requests import create_other_request
from employees.models import Employee
from shipping.models import ShippingOrder
from sklad.services import WarehouseStateCode
from sklad.test_utils import create_warehouse_snapshot_row
from sku.models import Agency, Market
from todo.models import Task


User = get_user_model()


@override_settings(ALLOWED_HOSTS=["*"])
class ClientManagerFullSuiteBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.client_user = User.objects.create_user(username="full_suite_client", password="pwd")
        cls.manager_user = User.objects.create_user(username="full_suite_manager", password="pwd")
        cls.foreign_user = User.objects.create_user(username="full_suite_foreign", password="pwd")

        cls.manager = Employee.objects.create(
            full_name="Менеджер полный suite",
            role="manager",
            user=cls.manager_user,
            is_active=True,
        )
        cls.agency = Agency.objects.create(
            agn_name="Клиент полный suite ЛК",
            portal_user=cls.client_user,
            short_name="Suite",
            mened_user_id=cls.manager_user.id,
        )
        cls.foreign_agency = Agency.objects.create(
            agn_name="Чужой клиент suite",
            portal_user=cls.foreign_user,
            short_name="Чужой",
        )
        cls.market, _ = Market.objects.get_or_create(id=9801, defaults={"name": "Wildberries Suite"})

        create_warehouse_snapshot_row(
            agency=cls.agency,
            order_id="FS-FREE",
            sku="FS-SKU-FREE",
            name="Свободный остаток suite",
            goods_type="gv",
            qty=50,
            available_qty=50,
            box_code="BOX-FS-FREE",
            pallet_code="PAL-FS-FREE",
        )
        create_warehouse_snapshot_row(
            agency=cls.agency,
            order_id="FS-LOCK",
            sku="FS-SKU-LOCK",
            name="Заблокированный остаток suite",
            goods_type="gv",
            qty=15,
            available_qty=0,
            box_code="BOX-FS-LOCK",
            pallet_code="PAL-FS-LOCK",
            warehouse_state_code=WarehouseStateCode.READY_FOR_LOADING.value,
        )
        create_warehouse_snapshot_row(
            agency=cls.foreign_agency,
            order_id="FS-FOREIGN",
            sku="FS-SKU-FOREIGN",
            name="Чужой остаток",
            goods_type="gv",
            qty=99,
            available_qty=99,
            box_code="BOX-FS-FOREIGN",
            pallet_code="PAL-FS-FOREIGN",
        )

    def setUp(self):
        self.client_http = Client()
        self.manager_http = Client()
        self.foreign_http = Client()
        self.client_http.force_login(self.client_user)
        self.manager_http.force_login(self.manager_user)
        self.foreign_http.force_login(self.foreign_user)

    def _cid(self):
        return self.agency.id

    def _find_request(self, rows, order_id, order_type=None):
        for row in rows or []:
            if str(row.get("order_id")) != str(order_id):
                continue
            if order_type and row.get("type") != order_type:
                continue
            return row
        return None

    def _client_dash(self):
        return self.client_http.get(f"/client/api/v1/dashboard/?client={self._cid()}")

    def _manager_client_dash(self):
        return self.manager_http.get(f"/client/api/v1/dashboard/?client={self._cid()}")


@override_settings(ALLOWED_HOSTS=["*"])
class ClientManagerShellAndAccessTests(ClientManagerFullSuiteBase):
    def test_client_and_manager_open_lk_shell_and_me(self):
        client_page = self.client_http.get(f"/client/dashboard/lk/?client={self._cid()}")
        manager_page = self.manager_http.get(f"/client/dashboard/lk/?client={self._cid()}")
        self.assertEqual(client_page.status_code, 200)
        self.assertEqual(manager_page.status_code, 200)
        self.assertContains(client_page, "Личный кабинет")
        self.assertContains(manager_page, "Личный кабинет")

        client_me = self.client_http.get(f"/client/api/v1/me/?client={self._cid()}")
        manager_me = self.manager_http.get(f"/client/api/v1/me/?client={self._cid()}")
        self.assertEqual(client_me.status_code, 200)
        self.assertEqual(manager_me.status_code, 200)
        self.assertEqual(int(client_me.json()["data"]["client"]["id"]), self._cid())
        self.assertEqual(int(manager_me.json()["data"]["client"]["id"]), self._cid())

        manager_home = self.manager_http.get("/team-manager/")
        self.assertEqual(manager_home.status_code, 200)
        self.assertContains(manager_home, "Заявки")
        self.assertContains(manager_home, 'href="/team-manager/orders/"')
        self.assertContains(manager_home, 'href="/team-manager/inventory/"')

    def test_foreign_client_cannot_read_agency_scope(self):
        dash = self.foreign_http.get(f"/client/api/v1/dashboard/?client={self._cid()}")
        self.assertEqual(dash.status_code, 200)
        rows = dash.json()["data"].get("requests") or []
        self.assertFalse(any(r.get("order_id") in {"FS-RCV-1", "FS-OBR-1", "SO-FS-1"} for r in rows))

        stock = self.foreign_http.get(f"/client/api/v1/stock-journal/?client={self._cid()}")
        self.assertEqual(stock.status_code, 200)
        boxes = {row["box_code"] for row in stock.json()["data"]["rows"]}
        self.assertNotIn("BOX-FS-FREE", boxes)


@override_settings(ALLOWED_HOSTS=["*"])
class ClientManagerReceivingSuiteTests(ClientManagerFullSuiteBase):
    def test_receiving_submitted_visible_in_client_lk_and_manager_journal(self):
        order_id = "FS-RCV-1"
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id=order_id,
            action="create",
            user=self.client_user,
            description="Заявка на приемку",
            payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
                "submit_action": "send",
                "items": [{"sku_code": "FS-SKU-FREE", "name": "Свободный остаток suite", "qty": 10}],
            },
        )

        client_dash = self._client_dash()
        manager_dash = self._manager_client_dash()
        self.assertEqual(client_dash.status_code, 200)
        self.assertEqual(manager_dash.status_code, 200)

        client_row = self._find_request(client_dash.json()["data"].get("requests"), order_id, "receiving")
        manager_row = self._find_request(manager_dash.json()["data"].get("requests"), order_id, "receiving")
        self.assertIsNotNone(client_row)
        self.assertIsNotNone(manager_row)
        self.assertEqual(client_row["status"], manager_row["status"])
        self.assertEqual(client_row["status_label"], manager_row["status_label"])
        self.assertNotIn("черновик", (client_row["status_label"] or "").lower())

        client_detail = self.client_http.get(
            f"/client/api/v1/requests/receiving/{order_id}/?client={self._cid()}"
        )
        manager_detail = self.manager_http.get(
            f"/client/api/v1/requests/receiving/{order_id}/?client={self._cid()}"
        )
        self.assertEqual(client_detail.status_code, 200)
        self.assertEqual(manager_detail.status_code, 200)
        self.assertEqual(client_detail.json()["data"]["type"], "receiving")
        self.assertEqual(manager_detail.json()["data"]["type"], "receiving")

        journal = self.manager_http.get("/team-manager/orders/", {"type": "receiving", "q": order_id})
        self.assertEqual(journal.status_code, 200)
        self.assertContains(journal, order_id)
        self.assertContains(journal, "Ждет подтверждения")
        self.assertNotContains(journal, "<span class=\"badge gray\">Черновик</span>", html=False)

    def test_receiving_client_draft_not_listed_as_active_manager_order(self):
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="draft-fs-rcv",
            action="create",
            user=self.client_user,
            description="Черновик приемки",
            payload={"status": "draft", "status_label": "Черновик", "submit_action": "draft"},
        )
        journal = self.manager_http.get("/team-manager/orders/", {"type": "receiving", "q": "draft-fs-rcv"})
        self.assertEqual(journal.status_code, 200)
        content = journal.content.decode("utf-8")
        self.assertIn("По выбранным фильтрам заявок нет", content)
        self.assertNotIn("Черновик приемки", content)
        self.assertNotIn('class="cell-primary"', content)


@override_settings(ALLOWED_HOSTS=["*"])
class ClientManagerProcessingSuiteTests(ClientManagerFullSuiteBase):
    def test_processing_submit_shows_waiting_not_draft_for_client_and_manager(self):
        sent = self.client_http.post(
            f"/orders/processing/?client={self._cid()}",
            data={
                "submit_action": "send",
                "product_name": "Suite обработка",
                "article": "FS-SKU-FREE",
                "stock_article[]": ["FS-SKU-FREE"],
                "stock_size[]": ["42"],
                "stock_barcode[]": ["BAR-FS-1"],
                "stock_qty[]": ["0"],
            },
        )
        self.assertEqual(sent.status_code, 302, getattr(sent, "content", b"")[:400])

        submitted = (
            OrderAuditEntry.objects.filter(agency=self.agency, order_type="processing")
            .exclude(order_id__startswith="draft-")
            .order_by("-created_at", "-id")
            .first()
        )
        self.assertIsNotNone(submitted)
        order_id = str(submitted.order_id)
        self.assertEqual((submitted.payload or {}).get("status"), "sent_unconfirmed")
        self.assertEqual(list_client_draft_order_ids(agency=self.agency, order_type="processing"), [])

        client_dash = self._client_dash()
        manager_dash = self._manager_client_dash()
        client_row = self._find_request(client_dash.json()["data"].get("requests"), order_id, "processing")
        manager_row = self._find_request(manager_dash.json()["data"].get("requests"), order_id, "processing")
        self.assertIsNotNone(client_row)
        self.assertIsNotNone(manager_row)
        self.assertEqual(client_row["status"], manager_row["status"])
        self.assertNotIn("черновик", (client_row.get("status_label") or "").lower())
        self.assertNotIn("черновик", (manager_row.get("status_label") or "").lower())

        client_detail = self.client_http.get(
            f"/client/api/v1/requests/processing/{order_id}/?client={self._cid()}"
        )
        manager_detail = self.manager_http.get(
            f"/client/api/v1/requests/processing/{order_id}/?client={self._cid()}"
        )
        self.assertEqual(client_detail.status_code, 200)
        self.assertEqual(manager_detail.status_code, 200)
        self.assertEqual(client_detail.json()["data"]["type"], "processing")
        self.assertEqual(manager_detail.json()["data"]["status_label"], client_detail.json()["data"]["status_label"])

        journal = self.manager_http.get("/team-manager/orders/", {"type": "processing", "q": order_id})
        self.assertEqual(journal.status_code, 200)
        self.assertContains(journal, f"{order_id}_OBR")
        self.assertContains(journal, "Ждет подтверждения")
        self.assertNotContains(journal, "OBR · черновик")

        self.assertTrue(
            Task.objects.filter(
                route=f"/orders/processing/{order_id}/",
                assigned_to=self.manager,
            )
            .exclude(status="done")
            .exists()
        )

    def test_processing_ghost_draft_hidden_from_manager_journal(self):
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="41",
            action="create",
            user=self.client_user,
            description="Заявка на обработку №41",
            payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
                "submit_action": "submitted",
            },
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="draft-fs-ghost",
            action="create",
            user=self.client_user,
            description="Черновик заявки на обработку",
            payload={"status": "draft", "status_label": "Черновик", "submit_action": "draft"},
        )

        journal = self.manager_http.get("/team-manager/orders/", {"type": "processing"})
        self.assertEqual(journal.status_code, 200)
        self.assertContains(journal, "№41_OBR")
        self.assertContains(journal, "Ждет подтверждения")
        self.assertNotContains(journal, "draft-fs-ghost")
        self.assertNotContains(journal, "Черновик заявки на обработку")


@override_settings(ALLOWED_HOSTS=["*"])
class ClientManagerShippingSuiteTests(ClientManagerFullSuiteBase):
    def test_shipping_submitted_parity_client_manager_and_journal(self):
        ShippingOrder.objects.create(
            number="SO-FS-1",
            agency=self.agency,
            created_by=self.client_user,
            status=ShippingOrder.STATUS_SUBMITTED,
            marketplace=self.market,
            destination_warehouse="Склад suite",
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="shipping",
            order_id="SO-FS-1",
            action="status",
            user=self.client_user,
            description="Отправлено менеджеру",
            payload={
                "status": "submitted",
                "shipping_state": "submitted",
                "status_label": "На согласовании менеджера",
            },
        )

        client_dash = self._client_dash()
        manager_dash = self._manager_client_dash()
        client_row = self._find_request(client_dash.json()["data"].get("requests"), "SO-FS-1", "shipping")
        manager_row = self._find_request(manager_dash.json()["data"].get("requests"), "SO-FS-1", "shipping")
        self.assertIsNotNone(client_row)
        self.assertIsNotNone(manager_row)
        self.assertEqual(client_row["status"], "waiting")
        self.assertEqual(manager_row["status_label"], client_row["status_label"])

        client_detail = self.client_http.get(
            f"/client/api/v1/requests/shipping/SO-FS-1/?client={self._cid()}"
        )
        manager_detail = self.manager_http.get(
            f"/client/api/v1/requests/shipping/SO-FS-1/?client={self._cid()}"
        )
        self.assertEqual(client_detail.status_code, 200)
        self.assertEqual(manager_detail.status_code, 200)
        self.assertEqual(client_detail.json()["data"]["type"], "shipping")
        self.assertEqual(
            client_detail.json()["data"]["status_label"],
            manager_detail.json()["data"]["status_label"],
        )

        journal = self.manager_http.get("/team-manager/orders/", {"type": "shipping", "q": "SO-FS-1"})
        self.assertEqual(journal.status_code, 200)
        self.assertContains(journal, "SO-FS-1")
        self.assertContains(journal, "На согласовании менеджера")
        self.assertContains(journal, f"/client/dashboard/lk/?client={self._cid()}")

    def test_shipping_create_form_opens_for_client_and_manager(self):
        client_form = self.client_http.get(f"/shipping/new/?client={self._cid()}")
        manager_form = self.manager_http.get(f"/shipping/new/?client={self._cid()}")
        self.assertEqual(client_form.status_code, 200)
        self.assertEqual(manager_form.status_code, 200)


@override_settings(ALLOWED_HOSTS=["*"])
class ClientManagerStockSuiteTests(ClientManagerFullSuiteBase):
    def test_stock_available_only_same_for_client_and_manager(self):
        client_stock = self.client_http.get(f"/client/api/v1/stock-journal/?client={self._cid()}")
        manager_stock = self.manager_http.get(f"/client/api/v1/stock-journal/?client={self._cid()}")
        self.assertEqual(client_stock.status_code, 200)
        self.assertEqual(manager_stock.status_code, 200)

        client_boxes = {row["box_code"] for row in client_stock.json()["data"]["rows"]}
        manager_boxes = {row["box_code"] for row in manager_stock.json()["data"]["rows"]}
        self.assertEqual(client_boxes, manager_boxes)
        self.assertIn("BOX-FS-FREE", client_boxes)
        self.assertNotIn("BOX-FS-LOCK", client_boxes)
        self.assertNotIn("BOX-FS-FOREIGN", client_boxes)

        dash = self._client_dash()
        kpi = dash.json()["data"].get("kpi") or {}
        self.assertGreaterEqual(int(kpi.get("stock_units") or 0), 50)

        inventory = self.manager_http.get("/team-manager/inventory/")
        self.assertEqual(inventory.status_code, 200)

    def test_stock_export_available_for_client_and_manager(self):
        client_export = self.client_http.post(
            f"/client/api/v1/stock-journal/export/?client={self._cid()}",
            data=json.dumps({}),
            content_type="application/json",
        )
        manager_export = self.manager_http.post(
            f"/client/api/v1/stock-journal/export/?client={self._cid()}",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(client_export.status_code, 200)
        self.assertEqual(manager_export.status_code, 200)
        self.assertIn(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            client_export["Content-Type"],
        )


@override_settings(ALLOWED_HOSTS=["*"])
class ClientManagerOtherRequestsSuiteTests(ClientManagerFullSuiteBase):
    def test_other_request_create_visible_in_client_lk_and_manager_journal(self):
        created = create_other_request(
            agency=self.agency,
            user=self.client_user,
            category="custom",
            description="Прочая заявка suite",
            save_as_draft=False,
        )
        order_id = str(created["order_id"])
        self.assertEqual(created["status"], "submitted")

        client_dash = self._client_dash()
        manager_dash = self._manager_client_dash()
        client_row = self._find_request(client_dash.json()["data"].get("requests"), order_id, "other")
        manager_row = self._find_request(manager_dash.json()["data"].get("requests"), order_id, "other")
        self.assertIsNotNone(client_row)
        self.assertIsNotNone(manager_row)
        self.assertEqual(client_row["status"], manager_row["status"])

        client_detail = self.client_http.get(
            f"/client/api/v1/requests/other/{order_id}/?client={self._cid()}"
        )
        manager_detail = self.manager_http.get(
            f"/client/api/v1/requests/other/{order_id}/?client={self._cid()}"
        )
        self.assertEqual(client_detail.status_code, 200)
        self.assertEqual(manager_detail.status_code, 200)
        self.assertEqual(client_detail.json()["data"]["type"], "other")
        self.assertEqual(manager_detail.json()["data"]["type"], "other")

        journal = self.manager_http.get("/team-manager/orders/", {"type": "other", "q": order_id})
        self.assertEqual(journal.status_code, 200)
        self.assertContains(journal, order_id)

        self.assertTrue(
            Task.objects.filter(route=f"/orders/other/{order_id}/", assigned_to=self.manager)
            .exclude(status="done")
            .exists()
        )

    def test_other_request_draft_stays_client_side_not_manager_active_order(self):
        draft = create_other_request(
            agency=self.agency,
            user=self.client_user,
            category="custom",
            description="Черновик прочей заявки suite",
            save_as_draft=True,
        )
        order_id = str(draft["order_id"])
        journal = self.manager_http.get("/team-manager/orders/", {"type": "other", "q": order_id})
        self.assertEqual(journal.status_code, 200)
        content = journal.content.decode("utf-8")
        self.assertIn("По выбранным фильтрам заявок нет", content)
        self.assertNotIn("Черновик прочей заявки suite", content)
        self.assertNotIn('class="cell-primary"', content)


@override_settings(ALLOWED_HOSTS=["*"])
class ClientManagerCrossTypeJournalSuiteTests(ClientManagerFullSuiteBase):
    def test_manager_orders_journal_lists_all_main_types(self):
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="FS-ALL-RCV",
            action="status",
            user=self.client_user,
            description="Приемка suite",
            payload={"status": "sent_unconfirmed", "status_label": "Ждет подтверждения"},
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="52",
            action="status",
            user=self.client_user,
            description="Обработка suite",
            payload={"status": "sent_unconfirmed", "status_label": "Ждет подтверждения"},
        )
        ShippingOrder.objects.create(
            number="SO-FS-ALL",
            agency=self.agency,
            created_by=self.client_user,
            status=ShippingOrder.STATUS_SUBMITTED,
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="shipping",
            order_id="SO-FS-ALL",
            action="status",
            user=self.client_user,
            description="Отгрузка suite",
            payload={
                "status": "submitted",
                "shipping_state": "submitted",
                "status_label": "На согласовании менеджера",
            },
        )
        other = create_other_request(
            agency=self.agency,
            user=self.client_user,
            category="custom",
            description="Прочая suite all",
            save_as_draft=False,
        )

        page = self.manager_http.get("/team-manager/orders/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "FS-ALL-RCV")
        self.assertContains(page, "№52_OBR")
        self.assertContains(page, "SO-FS-ALL")
        self.assertContains(page, other["order_id"])
        self.assertContains(page, "Приёмка")
        self.assertContains(page, "Обработка")
        self.assertContains(page, "Отгрузки")
        self.assertContains(page, "Другие")

        client_dash = self._client_dash()
        requests = client_dash.json()["data"].get("requests") or []
        types = {row.get("type") for row in requests}
        self.assertTrue({"receiving", "processing", "shipping", "other"}.issubset(types))
