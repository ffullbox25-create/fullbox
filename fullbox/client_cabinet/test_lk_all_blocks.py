"""End-to-end coverage for every client LK block (shell + APIs + agency scope)."""

from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.utils import timezone

from audit.models import OrderAuditEntry
from client_cabinet.models import ClientFinanceDocument, ClientServiceCharge
from client_cabinet.other_requests import create_other_request
from marking.models import MarkingCode
from shipping.models import ShippingOrder, ShippingOrderItem
from sklad.test_utils import create_warehouse_snapshot_row
from sku.models import Agency, Market, MarketCredential, SKU


User = get_user_model()

LK_PAGES = (
    "/",
    "/requests",
    "/request",
    "/stock",
    "/nomenclature",
    "/marking",
    "/marketplaces",
    "/finance",
    "/billing",
    "/notifications",
    "/chat",
    "/other-requests",
)

LK_NAV_ROUTES = (
    "/",
    "/requests",
    "/stock",
    "/nomenclature",
    "/marking",
    "/marketplaces",
    "/finance",
    "/notifications",
    "/chat",
)


class ClientLkAllBlocksBase(TestCase):
    """Shared portal client + foreign agency fixtures for block suites."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="lk_blocks_client", password="pwd")
        cls.foreign_user = User.objects.create_user(username="lk_blocks_foreign", password="pwd")
        cls.staff = User.objects.create_user(username="lk_blocks_staff", password="pwd", is_staff=True)
        cls.agency = Agency.objects.create(
            agn_name="ЛК Все блоки",
            portal_user=cls.user,
            short_name="Блоки",
            contract_numb="Д-BLOCKS",
            contract_link="https://example.com/contract-blocks.pdf",
            sign_oferta=True,
        )
        cls.foreign_agency = Agency.objects.create(
            agn_name="Чужой ЛК",
            portal_user=cls.foreign_user,
            short_name="Чужой",
        )
        cls.wb = Market.objects.create(id=9401, name="WB")
        cls.ozon = Market.objects.create(id=9402, name="OZON")
        cls.sku = SKU.objects.create(
            agency=cls.agency,
            sku_code="BLK-SKU-1",
            name="Товар блока ЛК",
            brand="BrandBlocks",
            size="M",
            market=cls.wb,
        )
        SKU.objects.create(
            agency=cls.foreign_agency,
            sku_code="FOREIGN-SKU",
            name="Чужой товар",
            brand="Other",
        )
        create_warehouse_snapshot_row(
            agency=cls.agency,
            order_id="BLK-RCV-1",
            sku="BLK-SKU-1",
            name="Товар блока ЛК",
            size="M",
            qty=12,
            available_qty=10,
            processing_reserved_qty=2,
            box_code="BOX-BLK-1",
            pallet_code="PAL-BLK-1",
        )
        create_warehouse_snapshot_row(
            agency=cls.foreign_agency,
            order_id="FOREIGN-RCV",
            sku="FOREIGN-SKU",
            name="Чужой товар",
            qty=99,
            available_qty=99,
            box_code="BOX-FOREIGN",
            pallet_code="PAL-FOREIGN",
        )
        MarkingCode.objects.create(
            agency=cls.agency,
            sku=cls.sku,
            sku_code=cls.sku.sku_code,
            code="BLK-MARK-FREE-1",
            source="import",
        )
        MarkingCode.objects.create(
            agency=cls.agency,
            sku=cls.sku,
            sku_code=cls.sku.sku_code,
            code="BLK-MARK-USED-1",
            source="import",
            used_at=timezone.now(),
            used_by=cls.user,
            printed_at=timezone.now(),
        )
        MarketCredential.objects.create(
            id=94011,
            agency=cls.agency,
            market=cls.wb,
            market_key="wb-token-blocks",
        )
        OrderAuditEntry.objects.create(
            order_id="BLK-RCV-1",
            order_type="receiving",
            action="status",
            agency=cls.agency,
            user=cls.user,
            description="Приёмка активна",
            payload={"status": "submitted", "status_label": "Ждет подтверждения", "items": [
                {"sku_code": "BLK-SKU-1", "name": "Товар блока ЛК", "size": "M", "qty": 10},
            ]},
        )
        OrderAuditEntry.objects.create(
            order_id="FOREIGN-RCV",
            order_type="receiving",
            action="status",
            agency=cls.foreign_agency,
            user=cls.foreign_user,
            description="Чужая приёмка",
            payload={"status": "submitted", "status_label": "Ждет подтверждения"},
        )
        shipping = ShippingOrder.objects.create(
            number="SO-BLK-1",
            agency=cls.agency,
            status=ShippingOrder.STATUS_SUBMITTED,
            created_by=cls.user,
        )
        ShippingOrderItem.objects.create(
            order=shipping,
            sku_code="BLK-SKU-1",
            name="Товар блока ЛК",
            size="M",
            qty_requested=4,
        )
        OrderAuditEntry.objects.create(
            order_id=shipping.number,
            order_type="shipping",
            action="status",
            agency=cls.agency,
            user=cls.user,
            description="Отгрузка создана",
            payload={"status": "submitted", "shipping_state": "submitted"},
        )
        ClientFinanceDocument.objects.create(
            agency=cls.agency,
            doc_kind=ClientFinanceDocument.KIND_INVOICE,
            title="Счёт блоки",
            number="INV-BLK-1",
            amount="2500.00",
            period=timezone.localdate().strftime("%Y-%m"),
            issued_at=timezone.localdate(),
            status=ClientFinanceDocument.STATUS_ISSUED,
        )
        ClientServiceCharge.objects.create(
            agency=cls.agency,
            service_type=ClientServiceCharge.TYPE_RECEIVING,
            description="Приёмка блоков",
            amount="700.00",
            period=timezone.localdate().strftime("%Y-%m"),
            charged_at=timezone.localdate(),
            status=ClientServiceCharge.STATUS_OPEN,
            order_type="receiving",
            order_id="BLK-RCV-1",
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def q(self, path: str) -> str:
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}client={self.agency.id}"

    def get_json(self, path: str):
        response = self.client.get(self.q(path))
        self.assertEqual(response.status_code, 200, msg=path)
        payload = response.json()
        self.assertTrue(payload.get("ok"), msg=payload)
        return payload["data"]


class ClientLkShellAndAccessTests(ClientLkAllBlocksBase):
    def test_lk_shell_contains_all_blocks(self):
        response = self.client.get(self.q("/client/dashboard/lk/"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        for page in LK_PAGES:
            self.assertIn(f'data-page="{page}"', content, msg=page)
        for route in LK_NAV_ROUTES:
            self.assertIn(f'data-route="{route}"', content, msg=route)
        self.assertIn("bricolage-manrope.css", content)
        self.assertIn("lk-theme.css", content)
        self.assertIn(f"/client/{self.agency.id}/receiving/new/", content)
        self.assertIn(f"/client/{self.agency.id}/shipping/new/", content)
        self.assertIn(f"/client/{self.agency.id}/packing/new/", content)
        self.assertIn("#/other-requests", content)
        self.assertIn("orders-list-data", content)
        self.assertIn("__lkEnsureRequests", content)
        self.assertIn("__lkShowRequestDetail", content)

    def test_anonymous_user_cannot_open_lk_apis(self):
        anon = Client()
        page = anon.get(self.q("/client/dashboard/lk/"))
        self.assertIn(page.status_code, {302, 403})
        dash = anon.get(self.q("/client/api/v1/dashboard/"))
        self.assertIn(dash.status_code, {302, 403})

    def test_portal_user_cannot_see_foreign_agency_data(self):
        foreign_client = Client()
        foreign_client.force_login(self.foreign_user)
        # Own dashboard only.
        own = foreign_client.get(f"/client/api/v1/dashboard/?client={self.foreign_agency.id}")
        self.assertEqual(own.status_code, 200)
        rows = own.json()["data"]["requests"]
        self.assertTrue(any(r["order_id"] == "FOREIGN-RCV" for r in rows))
        self.assertFalse(any(r["order_id"] in {"BLK-RCV-1", "SO-BLK-1"} for r in rows))
        # Attempt to open our request as foreign portal user → not found.
        detail = foreign_client.get(
            f"/client/api/v1/requests/receiving/BLK-RCV-1/?client={self.foreign_agency.id}"
        )
        self.assertEqual(detail.status_code, 404)
        stock = foreign_client.get(f"/client/api/v1/stock-journal/?client={self.foreign_agency.id}")
        self.assertEqual(stock.status_code, 200)
        stock_rows = stock.json()["data"]["rows"]
        self.assertFalse(any("BLK-SKU-1" in str(row) for row in stock_rows))


class ClientLkHomeBlockTests(ClientLkAllBlocksBase):
    def test_home_dashboard_payload_and_me(self):
        data = self.get_json("/client/api/v1/dashboard/")
        self.assertEqual(data["client"]["id"], self.agency.id)
        self.assertIn("kpi", data)
        self.assertIn("stock", data)
        self.assertIn("requests", data)
        self.assertIn("marketplaces", data)
        self.assertIn("documents", data)
        self.assertIn("notifications", data)
        self.assertIn("marking", data)
        self.assertIn("billing", data)
        self.assertIn("action_urls", data)
        self.assertGreaterEqual(data["kpi"]["stock_units"], 10)
        self.assertGreaterEqual(data["kpi"]["active_requests"], 1)
        self.assertEqual(data["marking"]["total"], 2)
        self.assertEqual(data["marking"]["free"], 1)
        urls = data["action_urls"]
        self.assertEqual(urls["receiving_new"], f"/client/{self.agency.id}/receiving/new/")
        self.assertEqual(urls["shipping_new"], f"/client/{self.agency.id}/shipping/new/")
        self.assertEqual(urls["processing_new"], f"/client/{self.agency.id}/packing/new/")

        me = self.get_json("/client/api/v1/me/")
        self.assertEqual(me["user"], self.user.username)
        self.assertEqual(me["client"]["id"], self.agency.id)


class ClientLkRequestsBlockTests(ClientLkAllBlocksBase):
    def test_requests_list_and_detail_bindings(self):
        data = self.get_json("/client/api/v1/dashboard/")
        requests = data["requests"]
        receiving = next(r for r in requests if r["order_id"] == "BLK-RCV-1")
        shipping = next(r for r in requests if r["order_id"] == "SO-BLK-1")
        self.assertEqual(receiving["type"], "receiving")
        self.assertEqual(receiving["detail_url"], "#/request/receiving/BLK-RCV-1")
        self.assertEqual(shipping["type"], "shipping")
        self.assertEqual(shipping["detail_url"], "#/request/shipping/SO-BLK-1")
        self.assertIn(f"/shipping/", shipping["wms_url"])
        self.assertIn(f"client={self.agency.id}", shipping["wms_url"])

        rcv_detail = self.get_json("/client/api/v1/requests/receiving/BLK-RCV-1/")
        self.assertEqual(rcv_detail["type"], "receiving")
        self.assertGreaterEqual(len(rcv_detail["lines"]), 1)
        self.assertEqual(rcv_detail["lines"][0]["sku_code"], "BLK-SKU-1")
        self.assertFalse(rcv_detail["actions"]["open_wms"])

        ship_detail = self.get_json("/client/api/v1/requests/shipping/SO-BLK-1/")
        self.assertEqual(ship_detail["type"], "shipping")
        self.assertEqual(ship_detail["lines"][0]["qty_requested"], 4)

        export = self.client.get(self.q("/client/api/v1/requests/shipping/SO-BLK-1/export/"))
        self.assertEqual(export.status_code, 200)
        self.assertIn(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            export["Content-Type"],
        )

        page = self.client.get(self.q("/client/dashboard/lk/"))
        orders_list = page.context["orders_list"]
        self.assertTrue(any(row["order_id"] == "BLK-RCV-1" for row in orders_list))
        self.assertTrue(all(str(row["detail_url"]).startswith("#/request/") for row in orders_list))

    def test_create_redirects_for_request_types(self):
        for path, expected in (
            (f"/client/{self.agency.id}/receiving/new/", f"/orders/receiving/?client={self.agency.id}"),
            (f"/client/{self.agency.id}/packing/new/", f"/orders/processing/?client={self.agency.id}"),
            (f"/client/{self.agency.id}/shipping/new/", f"/shipping/new/?client={self.agency.id}"),
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302, msg=path)
            self.assertEqual(response["Location"], expected)

        forbidden = Client()
        forbidden.force_login(self.foreign_user)
        denied = forbidden.get(f"/client/{self.agency.id}/shipping/new/")
        self.assertEqual(denied.status_code, 403)

    def test_processing_client_form_has_params_then_stock(self):
        response = self.client.get(f"/orders/processing/?client={self.agency.id}")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("lk-theme.css", content)
        self.assertIn("Новая заявка на обработку", content)
        self.assertIn("Выбор услуг", content)
        self.assertIn("Сборка набора", content)
        self.assertIn('name="set_build"', content)
        self.assertIn('name="set_qty"', content)
        self.assertIn("Выбор товара", content)
        self.assertIn("Итого по заявке", content)
        self.assertIn("Сменить артикул", content)
        self.assertIn("article-change-toggle", content)
        self.assertIn("data-target-article", content)
        self.assertIn("Артикул Б", content)
        self.assertIn("wizard-next-btn", content)
        self.assertIn("Дальше", content)
        self.assertIn("review-services", content)
        self.assertIn("review-products", content)
        self.assertIn("Комментарий", content)
        self.assertIn("Прикрепить файлы", content)
        self.assertIn("Отправить заявку", content)
        self.assertIn("Сохранить черновик", content)
        self.assertIn("client-processing-stock-table", content)
        services_at = content.find("Выбор услуг")
        stock_at = content.find("Выбор товара")
        review_at = content.find("Итого по заявке")
        self.assertLess(services_at, stock_at)
        self.assertLess(stock_at, review_at)


class ClientLkOtherRequestsBlockTests(ClientLkAllBlocksBase):
    def test_other_requests_create_list_detail_and_cancel(self):
        create = self.client.post(
            self.q("/client/api/v1/other-requests/"),
            data=json.dumps(
                {
                    "category": "measure_size",
                    "description": "Замер для блока ЛК",
                    "save_as_draft": True,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(create.status_code, 200)
        body = create.json()
        self.assertTrue(body["ok"])
        order_id = body["data"]["order_id"]

        dash = self.get_json("/client/api/v1/dashboard/")
        other_rows = [r for r in dash["requests"] if r["type"] == "other"]
        self.assertTrue(any(r["order_id"] == str(order_id) for r in other_rows))
        row = next(r for r in other_rows if r["order_id"] == str(order_id))
        self.assertIn("#/other-requests", row["detail_url"])
        self.assertTrue(row.get("is_draft"))

        detail = self.get_json(f"/client/api/v1/requests/other/{order_id}/")
        self.assertEqual(detail["type"], "other")
        self.assertTrue(detail["actions"]["can_cancel"])
        self.assertTrue(detail["actions"]["can_comment"])
        self.assertTrue(detail["actions"]["can_continue_draft"])
        self.assertIn("#/other-requests", detail["actions"]["continue_url"])

        comment = self.client.post(
            self.q(f"/client/api/v1/requests/other/{order_id}/"),
            data=json.dumps({"action": "comment", "text": "уточнение по блоку"}),
            content_type="application/json",
        )
        self.assertEqual(comment.status_code, 200)
        self.assertTrue(comment.json()["ok"])

        cancel = self.client.post(
            self.q(f"/client/api/v1/requests/other/{order_id}/"),
            data=json.dumps({"action": "cancel", "text": "не нужно"}),
            content_type="application/json",
        )
        self.assertEqual(cancel.status_code, 200)
        self.assertEqual(cancel.json()["data"]["status"], "cancelled")


class ClientLkStockBlockTests(ClientLkAllBlocksBase):
    def test_stock_journal_and_export(self):
        data = self.get_json("/client/api/v1/stock-journal/")
        rows = data["rows"]
        self.assertTrue(rows)
        self.assertTrue(any(row.get("sku") == "BLK-SKU-1" or row.get("sku_code") == "BLK-SKU-1" for row in rows))
        self.assertFalse(any("FOREIGN-SKU" in str(row) for row in rows))

        export = self.client.post(
            self.q("/client/api/v1/stock-journal/export/"),
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(export.status_code, 200)
        self.assertIn("spreadsheetml", export["Content-Type"])


class ClientLkNomenclatureBlockTests(ClientLkAllBlocksBase):
    def test_nomenclature_list_search_and_scope(self):
        data = self.get_json("/client/api/v1/nomenclature/")
        rows = data["rows"]
        self.assertTrue(any(row["sku_code"] == "BLK-SKU-1" for row in rows))
        self.assertFalse(any(row["sku_code"] == "FOREIGN-SKU" for row in rows))

        filtered = self.get_json("/client/api/v1/nomenclature/?search=BLK-SKU")
        self.assertEqual(len(filtered["rows"]), 1)
        self.assertEqual(filtered["rows"][0]["sku_code"], "BLK-SKU-1")
        self.assertIn("BrandBlocks", filtered["filters"]["brand_options"])

        page = self.client.get(self.q("/client/dashboard/lk/"))
        self.assertContains(page, 'data-page="/nomenclature"')
        self.assertContains(page, f"/client/{self.agency.id}/sku/")


class ClientLkMarkingBlockTests(ClientLkAllBlocksBase):
    def test_marking_stats_api_and_page(self):
        data = self.get_json("/client/api/v1/marking/")
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["free"], 1)
        self.assertEqual(data["used"], 1)
        page = self.client.get(self.q("/client/dashboard/lk/"))
        self.assertContains(page, 'data-page="/marking"')
        self.assertContains(page, "/client/marking/")


class ClientLkMarketplacesBlockTests(ClientLkAllBlocksBase):
    def test_marketplaces_payload_and_page_markers(self):
        data = self.get_json("/client/api/v1/marketplaces/?check_auth=0")
        markets = data.get("marketplaces") or []
        self.assertTrue(isinstance(markets, list) and markets)
        blob = json.dumps(markets, ensure_ascii=False).lower()
        self.assertTrue("wb" in blob or "wildberries" in blob)
        configured = [item for item in markets if item.get("configured") or item.get("connected")]
        self.assertTrue(configured, msg=markets)
        page = self.client.get(self.q("/client/dashboard/lk/"))
        self.assertContains(page, 'data-page="/marketplaces"')
        self.assertContains(page, "marketplaces")


class ClientLkFinanceBillingBlockTests(ClientLkAllBlocksBase):
    def test_finance_documents_block(self):
        data = self.get_json("/client/api/v1/finance/")
        docs = data["documents"]
        self.assertTrue(
            any(
                d["doc_kind"] == "invoice"
                and (d.get("subtitle") == "INV-BLK-1" or "INV-BLK-1" in str(d.get("title") or ""))
                for d in docs
            ),
            msg=docs,
        )
        self.assertTrue(any(d["id"] == f"contract-{self.agency.id}" for d in docs))
        self.assertIn("invoice", data["counts"])
        self.assertGreaterEqual(data["counts"]["all"], 1)

    def test_billing_block(self):
        data = self.get_json("/client/api/v1/billing/")
        self.assertTrue(data["billing_available"])
        billing = data["billing"]
        self.assertIsNotNone(billing)
        # Amount from ClientServiceCharge should appear somewhere in payload.
        blob = json.dumps(billing, ensure_ascii=False)
        self.assertTrue("700" in blob or "700.00" in blob or "700,00" in blob)
        page = self.client.get(self.q("/client/dashboard/lk/"))
        self.assertContains(page, 'data-page="/finance"')
        self.assertContains(page, 'data-page="/billing"')


class ClientLkNotificationsChatBlockTests(ClientLkAllBlocksBase):
    def test_notifications_list_and_mark_read(self):
        from client_cabinet.models import ClientNotification
        from client_cabinet.other_requests import take_other_in_work

        created = create_other_request(
            agency=self.agency,
            user=self.user,
            category="courier_box",
            description="короб для блока",
        )
        take_other_in_work(order_id=created["order_id"], user=self.staff)
        self.assertTrue(ClientNotification.objects.filter(agency=self.agency, is_read=False).exists())

        data = self.get_json("/client/api/v1/notifications/")
        self.assertGreaterEqual(data["unread_count"], 1)
        self.assertTrue(data["notifications"])

        mark = self.client.post(
            self.q("/client/api/v1/notifications/"),
            data=json.dumps({"action": "mark_read", "all": True}),
            content_type="application/json",
        )
        self.assertEqual(mark.status_code, 200)
        self.assertEqual(mark.json()["data"]["unread_count"], 0)

    def test_chat_messages_and_attachment(self):
        create = self.client.post(
            self.q("/client/api/v1/chat/messages/"),
            data=json.dumps({"text": "Сообщение из блока чата"}),
            content_type="application/json",
        )
        self.assertEqual(create.status_code, 200)
        self.assertTrue(create.json()["ok"])

        staff_client = Client()
        staff_client.force_login(self.staff)
        reply = staff_client.post(
            f"/client/api/v1/chat/messages/?client={self.agency.id}",
            data=json.dumps({"text": "Ответ менеджера"}),
            content_type="application/json",
        )
        self.assertEqual(reply.status_code, 200)

        opened = self.get_json("/client/api/v1/chat/messages/")
        self.assertGreaterEqual(len(opened["messages"]), 2)
        self.assertEqual(opened["unread_count"], 0)

        upload = SimpleUploadedFile("block-chat.jpg", b"img-bytes", content_type="image/jpeg")
        with_file = self.client.post(
            self.q("/client/api/v1/chat/messages/"),
            data={"text": "файл", "attachments": upload},
        )
        self.assertEqual(with_file.status_code, 200)
        attachments = with_file.json()["data"]["message"]["attachments"]
        self.assertEqual(len(attachments), 1)
        file_url = attachments[0]["url"]
        download = self.client.get(file_url if "?" in file_url else f"{file_url}?client={self.agency.id}")
        self.assertEqual(download.status_code, 200)


class ClientLkDashboardAggregationTests(ClientLkAllBlocksBase):
    def test_dashboard_aggregates_all_blocks_for_current_client_only(self):
        data = self.get_json("/client/api/v1/dashboard/")
        request_ids = {row["order_id"] for row in data["requests"]}
        self.assertIn("BLK-RCV-1", request_ids)
        self.assertIn("SO-BLK-1", request_ids)
        self.assertNotIn("FOREIGN-RCV", request_ids)

        stock_blob = json.dumps(data["stock"], ensure_ascii=False)
        self.assertIn("BLK-SKU-1", stock_blob)
        self.assertNotIn("FOREIGN-SKU", stock_blob)

        docs = data["documents"] or []
        self.assertTrue(
            any(
                d.get("subtitle") == "INV-BLK-1" or "INV-BLK-1" in str(d.get("title") or "")
                for d in docs
            ),
            msg=docs,
        )
        self.assertEqual(data["client"]["id"], self.agency.id)
        self.assertGreaterEqual(data["orders_panel_total"], 2)
