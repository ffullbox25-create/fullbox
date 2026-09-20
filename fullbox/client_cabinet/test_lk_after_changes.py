"""Regression tests for client LK after recent marketplace / other-requests / UI changes."""

from __future__ import annotations

import json
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from openpyxl import load_workbook

from audit.models import OrderAuditEntry
from client_cabinet.api_views import (
    OTHER_REQUEST_CATEGORIES,
    api_dashboard,
    api_dashboard_live,
    api_marketplaces,
    api_other_request_create,
    api_stock_journal,
    invalidate_dashboard_lite_cache,
    invalidate_request_payloads_cache,
)
from client_cabinet.lk_status_map import resolve_lk_entry_status, resolve_lk_request_status
from client_cabinet.marketplace_lk import build_mp_stocks_payload, build_marketplaces_payload
from client_cabinet.other_requests import (
    STATUS_LABELS,
    accept_other_by_warehouse,
    complete_other_request,
    create_other_request,
    extract_other_order_id,
    normalize_category,
    send_other_to_warehouse,
    take_other_in_work,
)
from client_cabinet.services import build_client_cabinet_url, build_dashboard_context
from client_cabinet.views import (
    _lk_request_filter_status,
    _lk_request_type_badge,
    _lk_request_type_key,
    _order_bucket,
    _order_status_label,
    dashboard_lk,
)
from employees.models import Employee
from market_sync.http import friendly_network_error, marketplace_session
from fullbox.order_numbers import format_order_number
from sklad.models import WarehouseReserve
from sklad.test_utils import create_warehouse_snapshot_row
from sku.models import Agency, Market, MarketCredential, SKU
from todo.models import Task
from todo.services import (
    TaskDetailState,
    complete_other_from_task,
    handle_task_detail_post,
    send_other_to_warehouse as todo_send_other,
)


User = get_user_model()


class ClientLkHttpHelpersTests(SimpleTestCase):
    def test_marketplace_session_ignores_env_proxy(self):
        session = marketplace_session()
        self.assertFalse(session.trust_env)

    def test_friendly_network_error_for_proxy(self):
        exc = Exception(
            "HTTPSConnectionPool(host='content-api.wildberries.ru', port=443): "
            "Max retries exceeded (Caused by ProxyError('Unable to connect to proxy', "
            "OSError('Tunnel connection failed: 403 Forbidden')))"
        )
        message = friendly_network_error(exc, "WB")
        self.assertIn("прокси", message.lower())
        self.assertNotIn("HTTPSConnectionPool", message)

    def test_friendly_network_error_for_timeout(self):
        message = friendly_network_error(Exception("Read timed out"), "Ozon")
        self.assertIn("не ответил", message.lower())

    def test_other_category_aliases_and_order_number(self):
        self.assertEqual(normalize_category("photo_report"), "photo_video")
        self.assertEqual(normalize_category("other"), "custom")
        self.assertEqual(normalize_category("measure_size"), "measure_size")
        self.assertEqual(format_order_number("other", "15"), "15_OTH")
        self.assertEqual(extract_other_order_id("/orders/other/42/"), "42")
        self.assertIsNone(extract_other_order_id("/orders/receiving/42/"))

    def test_other_type_badge_is_dr(self):
        self.assertEqual(_lk_request_type_key("other"), "other")
        self.assertEqual(_lk_request_type_badge("other"), "ПРЧ")

    def test_dashboard_home_uses_lite_dashboard_api(self):
        template_path = Path(__file__).resolve().parent / "templates/client_cabinet/dashboard_lk_react.html"
        template_text = template_path.read_text(encoding="utf-8")
        self.assertIn('/client/api/v1/dashboard/?client=', template_text)
        self.assertIn('&lite=1', template_text)
        self.assertIn('id="home-active-requests"', template_text)
        self.assertIn("renderHomeActiveRequests", template_text)

    def test_lk_status_map_resolves_display_statuses_and_cancel_policy(self):
        draft = resolve_lk_request_status(bucket="client", status_label="Черновик", payload={"status": "draft"})
        waiting = resolve_lk_request_status(
            bucket="manager",
            status_label="Ждет подтверждения",
            payload={"status": "sent_unconfirmed"},
        )
        warehouse = resolve_lk_request_status(bucket="warehouse", status_label="В работе склада")
        done = resolve_lk_request_status(bucket="done", status_label="Выполнена", payload={"status": "done"})
        cancelled = resolve_lk_request_status(bucket="done", status_label="ОТМЕНЕНА", payload={"status": "canceled"})
        cancel_pending = resolve_lk_request_status(
            bucket="manager",
            status_label="Отмена на согласовании менеджера",
            payload={"status": "cancel_requested"},
        )

        self.assertEqual(draft.filter_status, "waiting")
        self.assertEqual(draft.cancel_policy, "direct")
        self.assertEqual(waiting.manager_label, "На проверке менеджера")
        self.assertEqual(waiting.cancel_policy, "direct")
        self.assertEqual(warehouse.filter_status, "processing")
        self.assertEqual(warehouse.cancel_policy, "manager_approval")
        self.assertEqual(done.filter_status, "completed")
        self.assertEqual(done.cancel_policy, "none")
        self.assertEqual(cancelled.filter_status, "cancelled")
        self.assertEqual(cancelled.cancel_policy, "none")
        self.assertEqual(cancel_pending.filter_status, "waiting")
        self.assertEqual(cancel_pending.cancel_policy, "none")

        awaiting_sign = resolve_lk_request_status(
            bucket="manager",
            status_label="Ожидает подписи менеджера",
            payload={"status": "act_sent", "act_storekeeper_signed": True},
        )
        self.assertEqual(awaiting_sign.cancel_policy, "none")
        self.assertEqual(awaiting_sign.filter_status, "waiting")

    def test_lk_status_map_act_sent_is_not_completed_until_client_confirms(self):
        awaiting_manager = resolve_lk_entry_status(
            SimpleNamespace(
                order_type="receiving",
                order_id="ACT-MGR-1",
                agency=None,
                agency_id=None,
                payload={
                    "status": "warehouse",
                    "status_label": "Принято складом, акт приемки отправлен менеджеру",
                    "act_storekeeper_signed": True,
                },
            ),
            audience="default",
        )
        self.assertEqual(awaiting_manager.bucket, "manager")
        self.assertEqual(awaiting_manager.next_step, "Подписать акт")
        self.assertNotEqual(awaiting_manager.status_label, "Выполнена")

        sent_to_client = resolve_lk_entry_status(
            SimpleNamespace(
                order_type="receiving",
                order_id="ACT-CL-1",
                agency=None,
                agency_id=None,
                payload={"act_sent": "Акт приемки", "status": "done"},
            ),
            audience="client",
        )
        self.assertEqual(sent_to_client.bucket, "client")
        self.assertIn("акт", sent_to_client.status_label.lower())
        self.assertNotEqual(sent_to_client.status_label, "Выполнена")
        self.assertEqual(sent_to_client.next_step, "Подтвердить акт")

        warehouse_done_label = resolve_lk_entry_status(
            SimpleNamespace(
                order_type="receiving",
                order_id="ACT-CL-2",
                agency=None,
                agency_id=None,
                payload={
                    "act_sent": "Акт приемки с расхождениями",
                    "status": "done",
                    "status_label": "Завершена",
                    "act_viewed": True,
                },
            ),
            audience="client",
        )
        self.assertEqual(warehouse_done_label.bucket, "client")
        self.assertEqual(warehouse_done_label.status_label, "Акт отправлен клиенту")
        self.assertNotEqual(warehouse_done_label.status_label, "Выполнена")

        confirmed = resolve_lk_entry_status(
            SimpleNamespace(
                order_type="receiving",
                order_id="ACT-OK-1",
                agency=None,
                agency_id=None,
                payload={"act_sent": "Акт приемки", "act_client_response": "confirmed"},
            ),
            audience="client",
        )
        self.assertEqual(confirmed.bucket, "done")
        self.assertEqual(confirmed.status_label, "Выполнена")


class ClientLkPageAndUrlsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="lk_page_client", password="pwd")
        self.agency = Agency.objects.create(agn_name="ИП Тест ЛК", portal_user=self.user, short_name="Тест ЛК")
        self.factory = RequestFactory()

    def test_cabinet_urls_point_to_lk(self):
        self.assertEqual(build_client_cabinet_url(self.agency.id), f"/client/dashboard/lk/?client={self.agency.id}")
        self.assertTrue(build_client_cabinet_url(self.agency.id).startswith("/client/dashboard/lk/"))

    def test_dashboard_lk_contains_other_requests_and_marketplaces(self):
        request = self.factory.get(f"/client/dashboard/lk/?client={self.agency.id}")
        request.user = self.user
        response = dashboard_lk(request)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn('data-page="/other-requests"', content)
        self.assertIn("Прочие заявки", content)
        self.assertIn("Измерить размер", content)
        self.assertIn("/client/api/v1/other-requests/", content)
        self.assertIn('data-page="/marketplaces"', content)
        self.assertIn("Ваши площадки и поставки", content)
        self.assertIn('data-page="/knowledge"', content)
        self.assertIn("#/knowledge", content)
        self.assertIn("База знаний", content)
        self.assertIn("База знаний WMS", content)
        self.assertIn("Инструкция по работе с системой", content)
        self.assertIn('id="kb-tpl-receiving"', content)
        self.assertIn('id="kb-tpl-processing"', content)
        self.assertIn('id="kb-tpl-shipping"', content)
        self.assertIn('id="kb-tpl-returns"', content)
        self.assertIn('id="kb-tpl-other"', content)
        self.assertIn("Если подключено чтение Ozon API", content)
        self.assertIn("заявка в кабинете Ozon не изменяется", content)
        self.assertIn("конкретные дни и ближайший вывоз возвратов", content)
        self.assertIn("В работе на складе", content)
        self.assertIn("PDF, Excel, JPG или PNG", content)
        self.assertIn("Дополнительные работы и материалы могут тарифицироваться отдельно", content)
        self.assertIn('href="#/knowledge/receiving" target="_blank"', content)
        self.assertIn('href="#/knowledge/processing" target="_blank"', content)
        self.assertIn('href="#/knowledge/shipping" target="_blank"', content)
        self.assertIn('href="#/knowledge/returns" target="_blank"', content)
        self.assertIn('href="#/knowledge/other" target="_blank"', content)
        self.assertIn("indexOf('#/knowledge/')", content)
        self.assertIn("window.addEventListener('hashchange', hideGuideForKnowledge)", content)
        self.assertIn("Старт работы", content)
        self.assertIn("Чек-листы", content)
        self.assertIn("Как создать API-токен Wildberries для Fullbox", content)
        self.assertIn("/static/docs/api-wildberries-fbs.pdf", content)
        self.assertIn("Скачать актуальную инструкцию по API WB", content)
        self.assertIn("Фуллбокс Обухово МО", content)
        self.assertIn("«Контент»</b> и <b>«Маркетплейс»", content)
        self.assertIn("«Чтение и запись»", content)
        self.assertIn("Токен «Только чтение» и токен тестового контура", content)
        self.assertIn("403 Forbidden — scope is not allowed for this resource", content)
        self.assertNotIn("/static/docs/wb-nomenclature-token.pdf", content)
        self.assertIn("Как создать API FBS Ozon", content)
        self.assertIn("Client ID", content)
        self.assertIn("Posting FBS", content)
        self.assertIn("/static/docs/api-ozon-fbs.pdf", content)
        self.assertIn("Скачать PDF по API Ozon FBS", content)
        self.assertIn("Как сгенерировать Seller API Ozon для номенклатуры", content)
        self.assertIn("Admin read only", content)
        self.assertIn("180 дней", content)
        self.assertIn("/static/docs/ozon-nomenclature-seller-api.pdf", content)
        self.assertIn("Скачать PDF по Seller API Ozon", content)
        self.assertIn('data-page="/requests"', content)
        self.assertIn('data-page="/stock"', content)
        self.assertIn("Профиль компании", content)
        self.assertIn(f"/client/{self.agency.id}/edit/", content)
        self.assertIn("#/other-requests", content)
        self.assertIn("req-stage", content)
        self.assertIn("managerStageLabel", content)
        self.assertIn("cancelHint", content)
        self.assertIn("cancel_policy", content)
        self.assertIn("data-request-list-cancel", content)
        self.assertIn("postListCancel", content)
        self.assertIn("manager_status_label", content)
        self.assertIn("Можно отменить", content)
        self.assertIn("Отмена через менеджера", content)
        self.assertIn("В работе склада", content)

    def test_dashboard_context_includes_other_order_type(self):
        OrderAuditEntry.objects.create(
            order_id="99",
            order_type="other",
            action="submit",
            agency=self.agency,
            user=self.user,
            description="Прочая заявка",
            payload={
                "status": "submitted",
                "status_label": "Ждет подтверждения",
                "category": "measure_size",
                "category_label": "Измерить размер",
                "order_title": "Прочая заявка · Измерить размер",
                "order_display_title": "Прочая заявка · Измерить размер",
            },
        )
        request = self.factory.get(f"/client/dashboard/lk/?client={self.agency.id}")
        request.user = self.user
        context = build_dashboard_context(
            request=request,
            selected_client=self.agency,
            client_view=True,
            run_inventory_check=False,
        )
        all_orders = [
            order
            for column in context["orders_panel_columns"]
            for order in column["orders"]
        ]
        other = next(item for item in all_orders if item["order_type"] == "other" and item["order_id"] == "99")
        self.assertIn("#/request/other/99", other["detail_url"])
        self.assertIn(f"client={self.agency.id}", other["detail_url"])
        self.assertIn("Ждет подтверждения", other["status_label"])


class ClientLkApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="lk_api_client", password="pwd")
        self.agency = Agency.objects.create(agn_name="Клиент API ЛК", portal_user=self.user)
        self.factory = RequestFactory()
        self.manager = Employee.objects.create(full_name="Менеджер ЛК", role="manager", is_active=True)
        self.storekeeper = Employee.objects.create(full_name="Кладовщик ЛК", role="storekeeper", is_active=True)
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_id="pr-lk-1",
            sku="SKU-LK-1",
            name="Товар ЛК",
            barcode="210000000001",
            goods_type="gv",
            qty=4,
            available_qty=2,
            box_code="BOX-LK-1",
            pallet_code="PAL-LK-1",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
        )

    def _auth_get(self, path):
        request = self.factory.get(path)
        request.user = self.user
        return request

    def _auth_post(self, path, payload):
        request = self.factory.post(
            path,
            data=json.dumps(payload),
            content_type="application/json",
        )
        request.user = self.user
        return request

    def test_api_dashboard_exposes_lk_payload(self):
        response = api_dashboard(self._auth_get(f"/client/api/v1/dashboard/?client={self.agency.id}"))
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["client"]["id"], self.agency.id)
        self.assertIn("requests", data)
        self.assertIn("stock", data)
        self.assertIn("marketplaces", data)
        self.assertNotIn("mp_stocks", data)  # stocks live under /api/v1/marketplaces/
        self.assertEqual(data["other_request_categories"], OTHER_REQUEST_CATEGORIES)
        self.assertIn("other_new", data["action_urls"])
        self.assertIn("#/other-requests", data["action_urls"]["other_new"])
        self.assertEqual(data["action_urls"]["other_journal"], f"/orders/other/?client={self.agency.id}")

    def test_api_dashboard_lite_returns_home_payload_without_heavy_blocks(self):
        OrderAuditEntry.objects.create(
            order_id="PR-LITE-ACTIVE",
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Активная заявка для главной",
            payload={"status": "submitted", "status_label": "Ждет подтверждения"},
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_id="pr-lk-lite-hidden-processing",
            sku="SKU-LK-LITE-HIDDEN",
            name="Товар в обработке lite",
            barcode="210000000098",
            goods_type="gv",
            qty=7,
            available_qty=7,
            box_code="BOX-LK-LITE-HIDDEN",
            pallet_code="PAL-LK-LITE-HIDDEN",
            warehouse_state_code="in_processing_zone",
        )
        invalidate_dashboard_lite_cache(self.agency.id)
        response = api_dashboard(self._auth_get(f"/client/api/v1/dashboard/?client={self.agency.id}&lite=1"))
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertTrue(payload["ok"])
        data = payload["data"]

        self.assertEqual(data["client"]["id"], self.agency.id)
        self.assertEqual(data["stock"]["summary"]["total_units"], 2)
        self.assertEqual(data["live_dashboard"]["stock"]["units"], 2)
        self.assertEqual(data["stock"]["all_products"], [])
        self.assertIn("live_dashboard", data)
        self.assertTrue(any(row["order_id"] == "PR-LITE-ACTIVE" for row in data["active_requests_home"]))
        self.assertIn("kpi", data)
        self.assertNotIn("requests", data)
        self.assertNotIn("documents", data)
        self.assertNotIn("marketplaces", data)
        self.assertNotIn("notifications", data)

    def test_api_stock_journal_returns_rows(self):
        response = api_stock_journal(
            self._auth_get(f"/client/api/v1/stock-journal/?client={self.agency.id}&hide_consumed=1")
        )
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertTrue(payload["ok"])
        rows = payload["data"]["rows"]
        self.assertTrue(any(row["box_code"] == "BOX-LK-1" for row in rows))

    def test_api_dashboard_stock_summary_matches_stock_journal(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_id="pr-lk-hidden-processing",
            sku="SKU-LK-HIDDEN",
            name="Товар в обработке",
            barcode="210000000099",
            goods_type="gv",
            qty=5,
            available_qty=5,
            box_code="BOX-LK-HIDDEN",
            pallet_code="PAL-LK-HIDDEN",
            warehouse_state_code="in_processing_zone",
        )
        dashboard = api_dashboard(self._auth_get(f"/client/api/v1/dashboard/?client={self.agency.id}"))
        journal = api_stock_journal(
            self._auth_get(f"/client/api/v1/stock-journal/?client={self.agency.id}&hide_consumed=1")
        )

        dashboard_payload = json.loads(dashboard.content.decode("utf-8"))
        journal_payload = json.loads(journal.content.decode("utf-8"))
        self.assertTrue(dashboard_payload["ok"])
        self.assertTrue(journal_payload["ok"])

        stock_summary = dashboard_payload["data"]["stock"]["summary"]
        journal_summary = journal_payload["data"]["summary"]
        # Клиентский KPI = только available (без резерва/выгрузки).
        self.assertEqual(stock_summary["total_units"], 2)
        self.assertEqual(stock_summary["available"], 2)
        self.assertEqual(stock_summary["reserved"], 0)
        self.assertFalse(
            any(row["sku"] == "SKU-LK-HIDDEN" for row in dashboard_payload["data"]["stock"]["all_products"])
        )
        self.assertFalse(
            any(row["sku"] == "SKU-LK-HIDDEN" for row in journal_payload["data"]["rows"])
        )
        self.assertEqual(journal_summary["total_qty"], 2)
        self.assertEqual(journal_summary["available_qty"], stock_summary["total_units"])
        self.assertEqual(journal_summary["available_qty"], stock_summary["available"])
        self.assertEqual(dashboard_payload["data"]["kpi"]["stock_units"], stock_summary["total_units"])

    def test_api_dashboard_live_stock_uses_client_visible_stock(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_id="pr-lk-live-hidden-processing",
            sku="SKU-LK-LIVE-HIDDEN",
            name="Товар в обработке live",
            barcode="210000000097",
            goods_type="gv",
            qty=9,
            available_qty=9,
            box_code="BOX-LK-LIVE-HIDDEN",
            pallet_code="PAL-LK-LIVE-HIDDEN",
            warehouse_state_code="in_processing_zone",
        )
        invalidate_dashboard_lite_cache(self.agency.id)

        response = api_dashboard_live(self._auth_get(f"/client/api/v1/dashboard/live/?client={self.agency.id}"))
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["stock"]["units"], 2)

    def test_api_dashboard_available_stock_excludes_active_reserves(self):
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="reserve-lk-1",
            sku_code="SKU-LK-1",
            size="",
            barcode="210000000001",
            goods_type="gv",
            qty_reserved=1,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        dashboard = api_dashboard(self._auth_get(f"/client/api/v1/dashboard/?client={self.agency.id}"))
        journal = api_stock_journal(
            self._auth_get(f"/client/api/v1/stock-journal/?client={self.agency.id}&hide_consumed=1")
        )

        dashboard_payload = json.loads(dashboard.content.decode("utf-8"))
        journal_payload = json.loads(journal.content.decode("utf-8"))
        stock_summary = dashboard_payload["data"]["stock"]["summary"]
        product = next(row for row in dashboard_payload["data"]["stock"]["all_products"] if row["sku"] == "SKU-LK-1")

        self.assertEqual(stock_summary["total_units"], 1)
        self.assertEqual(stock_summary["available"], 1)
        self.assertEqual(stock_summary["reserved"], 0)
        self.assertEqual(product["qty"], 1)
        self.assertEqual(product["available_qty"], 1)
        self.assertEqual(product["reserved_qty"], 0)
        self.assertEqual(journal_payload["data"]["summary"]["available_qty"], stock_summary["available"])

    def test_api_dashboard_request_statuses_are_grouped_for_client_lk(self):
        OrderAuditEntry.objects.create(
            order_id="PR-STATUS-1",
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Приемка со склада",
            payload={"status": "warehouse", "status_label": "На складе"},
        )
        OrderAuditEntry.objects.create(
            order_id="OBR-STATUS-1",
            order_type="processing",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Обработка ожидает менеджера",
            payload={"status": "sent_unconfirmed", "status_label": "Ждет подтверждения"},
        )

        response = api_dashboard(self._auth_get(f"/client/api/v1/dashboard/?client={self.agency.id}"))
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode("utf-8"))
        rows = {row["order_id"]: row for row in payload["data"]["requests"]}

        self.assertEqual(rows["PR-STATUS-1"]["bucket"], "warehouse")
        self.assertEqual(rows["PR-STATUS-1"]["status"], "processing")
        self.assertEqual(rows["PR-STATUS-1"]["status_label"], "В работе")
        self.assertEqual(rows["PR-STATUS-1"]["cancel_policy"], "manager_approval")
        self.assertEqual(rows["PR-STATUS-1"]["manager_status_label"], "В работе склада")
        self.assertEqual(rows["OBR-STATUS-1"]["bucket"], "manager")
        self.assertEqual(rows["OBR-STATUS-1"]["status"], "waiting")
        self.assertEqual(rows["OBR-STATUS-1"]["status_label"], "Ждет подтверждения")
        self.assertEqual(rows["OBR-STATUS-1"]["cancel_policy"], "direct")
        self.assertEqual(rows["OBR-STATUS-1"]["manager_status_label"], "На проверке менеджера")
        self.assertEqual(payload["data"]["kpi"]["in_processing"], 1)
        self.assertEqual(payload["data"]["kpi"]["waiting"], 1)

    def test_smoke_lk_command_checks_client_and_manager_lk(self):
        manager_user = User.objects.create_user(username="lk_smoke_manager", password="pwd")
        head_manager_user = User.objects.create_user(username="lk_smoke_head_manager", password="pwd")
        Employee.objects.create(
            full_name="Менеджер Smoke",
            role="manager",
            user=manager_user,
            is_active=True,
        )
        Employee.objects.create(
            full_name="Руководитель Smoke",
            role="head_manager",
            user=head_manager_user,
            is_active=True,
        )
        OrderAuditEntry.objects.create(
            order_id="SMOKE-STATUS-1",
            order_type="processing",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Smoke заявка на обработку",
            payload={"status": "sent_unconfirmed", "status_label": "Ждет подтверждения"},
        )

        stdout = StringIO()
        call_command(
            "smoke_lk",
            client_id=self.agency.id,
            host="testserver",
            stdout=stdout,
        )
        output = stdout.getvalue()

        self.assertIn(f"OK client {self.agency.id}", output)
        self.assertIn("total=2 available=2 requests=1", output)
        self.assertIn("page_ms=", output)
        self.assertIn("dashboard_api_ms=", output)
        self.assertIn("stock_journal_api_ms=", output)
        self.assertIn("OK manager", output)
        self.assertIn("OK head_manager", output)
        self.assertIn("OK LK smoke passed", output)

    def test_smoke_lk_command_fails_when_client_lk_is_too_slow(self):
        stdout = StringIO()
        with patch(
            "client_cabinet.management.commands.smoke_lk.time.perf_counter",
            side_effect=[0.0, 2.0],
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "smoke_lk",
                    client_id=self.agency.id,
                    host="testserver",
                    skip_managers=True,
                    max_client_page_ms=1000,
                    stdout=stdout,
                )

        output = stdout.getvalue()
        self.assertIn("client LK main page too slow: 2000ms > 1000ms", output)

    def test_smoke_lk_command_fails_when_manager_lk_is_too_slow(self):
        manager_user = User.objects.create_user(username="lk_slow_manager", password="pwd")
        head_manager_user = User.objects.create_user(username="lk_slow_head_manager", password="pwd")
        Employee.objects.create(
            full_name="Медленный менеджер Smoke",
            role="manager",
            user=manager_user,
            is_active=True,
        )
        Employee.objects.create(
            full_name="Медленный руководитель Smoke",
            role="head_manager",
            user=head_manager_user,
            is_active=True,
        )

        stdout = StringIO()
        with patch(
            "client_cabinet.management.commands.smoke_lk.time.perf_counter",
            side_effect=[
                0.0, 0.1,  # client LK page
                0.1, 0.2,  # dashboard API
                0.2, 0.3,  # stock journal API
                3.0, 5.0,  # manager LK page
                5.0, 5.1,  # head manager LK page
            ],
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "smoke_lk",
                    client_id=self.agency.id,
                    host="testserver",
                    max_client_page_ms=0,
                    max_client_api_ms=0,
                    max_manager_page_ms=1000,
                    stdout=stdout,
                )

        output = stdout.getvalue()
        self.assertIn("manager LK page too slow: 2000ms > 1000ms", output)

    def test_api_marketplaces_without_credentials(self):
        response = api_marketplaces(self._auth_get(f"/client/api/v1/marketplaces/?client={self.agency.id}"))
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertTrue(payload["ok"])
        markets = payload["data"]["marketplaces"]
        self.assertGreaterEqual(len(markets), 2)
        by_id = {item["id"]: item for item in markets}
        self.assertFalse(by_id["wb"]["configured"])
        self.assertFalse(by_id["ozon"]["configured"])
        self.assertEqual(by_id["wb"]["token_health"], "missing")
        self.assertEqual(by_id["wb"]["action_label"], "Подключить")
        self.assertIn("#/chat", by_id["wb"]["reconnect_url"])
        self.assertIn("from_cache", payload["data"]["mp_stocks"])

    def test_api_other_request_create_and_dashboard_lists_it(self):
        response = api_other_request_create(
            self._auth_post(
                f"/client/api/v1/other-requests/?client={self.agency.id}",
                {
                    "category": "measure_size",
                    "description": "Замерить артикул A-1",
                    "save_as_draft": False,
                    "source": "other_form",
                },
            )
        )
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertTrue(payload["ok"])
        order_id = payload["data"]["order_id"]
        self.assertTrue(str(order_id).isdigit())
        self.assertEqual(payload["data"]["status"], "submitted")
        self.assertTrue(
            Task.objects.filter(
                route=f"/orders/other/{order_id}/",
                assigned_to=self.manager,
            )
            .exclude(status="done")
            .exists()
        )

        dash = api_dashboard(self._auth_get(f"/client/api/v1/dashboard/?client={self.agency.id}"))
        dash_payload = json.loads(dash.content.decode("utf-8"))
        requests_data = dash_payload["data"]["requests"]
        other_row = next(item for item in requests_data if item["order_id"] == order_id)
        self.assertEqual(other_row["type"], "other")
        self.assertEqual(other_row["status"], "waiting")
        self.assertIn("#/request/other/", other_row["detail_url"])
        self.assertIn("/orders/other/", other_row.get("wms_url") or "")

    def test_api_other_request_draft_has_no_manager_task(self):
        response = api_other_request_create(
            self._auth_post(
                f"/client/api/v1/other-requests/?client={self.agency.id}",
                {"category": "custom", "description": "черновик", "save_as_draft": True},
            )
        )
        payload = json.loads(response.content.decode("utf-8"))
        order_id = payload["data"]["order_id"]
        self.assertEqual(payload["data"]["status"], "draft")
        self.assertFalse(Task.objects.filter(route=f"/orders/other/{order_id}/").exists())

    def test_api_other_request_does_not_accept_another_clients_draft_number(self):
        from client_cabinet.models import OtherRequest

        foreign_user = User.objects.create_user(username="other_request_foreign", password="pwd")
        foreign_agency = Agency.objects.create(
            agn_name="Другой клиент API",
            portal_user=foreign_user,
        )
        foreign = create_other_request(
            agency=foreign_agency,
            user=foreign_user,
            category="custom",
            description="чужой API-черновик",
            save_as_draft=True,
        )

        response = api_other_request_create(
            self._auth_post(
                f"/client/api/v1/other-requests/?client={self.agency.id}",
                {
                    "category": "custom",
                    "description": "моя новая заявка",
                    "save_as_draft": False,
                    "order_id": foreign["order_id"],
                },
            )
        )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode("utf-8"))
        created_id = payload["data"]["order_id"]
        self.assertNotEqual(created_id, foreign["order_id"])
        foreign_obj = OtherRequest.objects.get(public_number=foreign["order_id"])
        self.assertEqual(foreign_obj.agency_id, foreign_agency.id)
        self.assertEqual(foreign_obj.status, OtherRequest.STATUS_DRAFT)
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_type="other",
                order_id=foreign["order_id"],
                agency=self.agency,
            ).exists()
        )

    def test_api_dashboard_excludes_stock_move_from_requests(self):
        OrderAuditEntry.objects.create(
            order_id="1392",
            order_type="stock_move",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Складское задание",
            payload={"status": "completed", "status_label": "Выполнена"},
        )
        create_other_request(
            agency=self.agency,
            user=self.user,
            category="measure_size",
            description="настоящая прочая",
        )
        response = api_dashboard(self._auth_get(f"/client/api/v1/dashboard/?client={self.agency.id}"))
        payload = json.loads(response.content.decode("utf-8"))
        requests_data = payload["data"]["requests"]
        self.assertFalse(any(row.get("order_type") == "stock_move" for row in requests_data))
        self.assertFalse(any("/orders/stock_move/" in str(row.get("detail_url") or "") for row in requests_data))
        other_rows = [row for row in requests_data if row.get("type") == "other"]
        self.assertTrue(other_rows)
        self.assertTrue(all(row.get("order_type") == "other" for row in other_rows))
        self.assertTrue(all("#/request/other/" in str(row.get("detail_url") or "") for row in other_rows))
        self.assertIn("other_journal", payload["data"]["action_urls"])
        self.assertIn("#/other-requests", payload["data"]["action_urls"]["other_new"])


class ClientLkOtherRequestFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="lk_other_flow", password="pwd")
        self.manager_user = User.objects.create_user(username="lk_other_manager", password="pwd")
        self.storekeeper_user = User.objects.create_user(username="lk_other_storekeeper", password="pwd")
        self.agency = Agency.objects.create(agn_name="Клиент прочие", portal_user=self.user)
        self.manager = Employee.objects.create(
            full_name="Менеджер Прочих",
            role="manager",
            user=self.manager_user,
            is_active=True,
        )
        self.storekeeper = Employee.objects.create(
            full_name="Кладовщик Прочих",
            role="storekeeper",
            user=self.storekeeper_user,
            is_active=True,
        )
        self.client = Client()

    def test_status_buckets_through_manager_and_warehouse(self):
        created = create_other_request(
            agency=self.agency,
            user=self.user,
            category="photo_video",
            description="Нужно фото комплекта",
            save_as_draft=False,
        )
        order_id = created["order_id"]
        entry = OrderAuditEntry.objects.filter(order_type="other", order_id=order_id).order_by("-created_at").first()
        self.assertEqual(_order_bucket(entry), "manager")
        self.assertEqual(_lk_request_filter_status(bucket="manager", status_label=_order_status_label(entry)), "waiting")

        manager_task = Task.objects.get(route=f"/orders/other/{order_id}/", assigned_to=self.manager)
        ok = send_other_to_warehouse(manager_task, SimpleNamespace(user=self.manager_user))
        self.assertTrue(ok)
        entry = OrderAuditEntry.objects.filter(order_type="other", order_id=order_id).order_by("-created_at").first()
        self.assertEqual((entry.payload or {}).get("status"), "warehouse")
        self.assertEqual(_order_bucket(entry), "warehouse")
        self.assertEqual(
            _lk_request_filter_status(bucket="warehouse", status_label=_order_status_label(entry)),
            "processing",
        )
        self.assertEqual(Task.objects.get(pk=manager_task.pk).status, "done")
        self.assertTrue(
            Task.objects.filter(route=f"/orders/other/{order_id}/", assigned_to=self.storekeeper)
            .exclude(status="done")
            .exists()
        )

        self.assertTrue(accept_other_by_warehouse(order_id=order_id, user=self.storekeeper_user))
        take_other_in_work(order_id=order_id, user=self.storekeeper_user)
        entry = OrderAuditEntry.objects.filter(order_type="other", order_id=order_id).order_by("-created_at").first()
        self.assertEqual((entry.payload or {}).get("status"), "in_work")
        self.assertEqual(_order_bucket(entry), "warehouse")

        from client_cabinet.models import OtherRequest

        OtherRequest.objects.filter(public_number=order_id).update(result_comment="Фото комплекта сделано")
        complete_other_request(order_id=order_id, user=self.storekeeper_user)
        entry = OrderAuditEntry.objects.filter(order_type="other", order_id=order_id).order_by("-created_at").first()
        self.assertEqual((entry.payload or {}).get("workflow_status"), "awaiting_manager_check")
        self.assertEqual((entry.payload or {}).get("status"), "in_work")
        self.assertEqual(_order_bucket(entry), "warehouse")

        complete_other_request(order_id=order_id, user=self.manager_user)
        entry = OrderAuditEntry.objects.filter(order_type="other", order_id=order_id).order_by("-created_at").first()
        self.assertEqual((entry.payload or {}).get("status"), "completed")
        self.assertEqual(_order_bucket(entry), "done")
        self.assertEqual(
            _lk_request_filter_status(bucket="done", status_label=_order_status_label(entry)),
            "completed",
        )
        self.assertFalse(
            Task.objects.filter(route=f"/orders/other/{order_id}/").exclude(status="done").exists()
        )

    def test_manager_todo_complete_sends_other_to_warehouse(self):
        created = create_other_request(
            agency=self.agency,
            user=self.user,
            category="repack",
            description="Перепаковать",
        )
        order_id = created["order_id"]
        task = Task.objects.get(route=f"/orders/other/{order_id}/", assigned_to=self.manager)
        request = RequestFactory().post("/todo/1/", {"action": "complete"})
        request.user = self.manager_user
        request._messages = Mock()
        state = TaskDetailState(
            can_complete=True,
            can_create_receiving_act=False,
            receiving_act_url="",
            can_create_placement_act=False,
            placement_act_url="",
            placement_act_exists=False,
            receiving_act_exists=False,
            receiving_act_label="",
            placement_act_label="",
            receiving_act_open_url="",
            placement_act_open_url="",
            can_send_act_to_client=False,
            can_edit=False,
            return_url="/",
            order_context=None,
            trip_context=None,
        )
        with patch("todo.services.messages"):
            handle_task_detail_post(request=request, task=task, state=state)
        entry = OrderAuditEntry.objects.filter(order_type="other", order_id=order_id).order_by("-created_at").first()
        self.assertEqual((entry.payload or {}).get("status"), "warehouse")
        task.refresh_from_db()
        self.assertEqual(task.status, "done")

    def test_storekeeper_todo_complete_finishes_other_request(self):
        created = create_other_request(
            agency=self.agency,
            user=self.user,
            category="courier_box",
            description="Короб курьеру",
        )
        order_id = created["order_id"]
        manager_task = Task.objects.get(route=f"/orders/other/{order_id}/", assigned_to=self.manager)
        todo_send_other(manager_task, SimpleNamespace(user=self.manager_user))
        store_task = (
            Task.objects.filter(route=f"/orders/other/{order_id}/", assigned_to=self.storekeeper)
            .exclude(status="done")
            .get()
        )
        self.assertFalse(complete_other_from_task(store_task, SimpleNamespace(user=self.storekeeper_user)))
        self.assertTrue(accept_other_by_warehouse(order_id=order_id, user=self.storekeeper_user))
        take_other_in_work(order_id=order_id, user=self.storekeeper_user)
        from client_cabinet.models import OtherRequest

        OtherRequest.objects.filter(public_number=order_id).update(result_comment="Короб подготовлен")
        self.assertTrue(complete_other_from_task(store_task, SimpleNamespace(user=self.storekeeper_user)))
        entry = OrderAuditEntry.objects.filter(order_type="other", order_id=order_id).order_by("-created_at").first()
        self.assertEqual((entry.payload or {}).get("workflow_status"), "awaiting_manager_check")
        self.assertTrue(complete_other_request(order_id=order_id, user=self.manager_user))
        entry = OrderAuditEntry.objects.filter(order_type="other", order_id=order_id).order_by("-created_at").first()
        self.assertEqual((entry.payload or {}).get("status"), "completed")

    def test_orders_other_detail_actions_for_manager_and_storekeeper(self):
        created = create_other_request(
            agency=self.agency,
            user=self.user,
            category="custom",
            description="Свободная задача",
        )
        order_id = created["order_id"]

        self.client.force_login(self.manager_user)
        response = self.client.post(f"/orders/other/{order_id}/", {"action": "send_to_warehouse"})
        self.assertEqual(response.status_code, 302)
        entry = OrderAuditEntry.objects.filter(order_type="other", order_id=order_id).order_by("-created_at").first()
        self.assertEqual((entry.payload or {}).get("status"), "warehouse")

        self.client.force_login(self.storekeeper_user)
        response = self.client.post(f"/orders/other/{order_id}/", {"action": "accept_warehouse"})
        self.assertEqual(response.status_code, 302)
        entry = OrderAuditEntry.objects.filter(order_type="other", order_id=order_id).order_by("-created_at").first()
        self.assertEqual((entry.payload or {}).get("workflow_status"), "warehouse_accepted")

        response = self.client.post(f"/orders/other/{order_id}/", {"action": "take_in_work"})
        self.assertEqual(response.status_code, 302)
        entry = OrderAuditEntry.objects.filter(order_type="other", order_id=order_id).order_by("-created_at").first()
        self.assertEqual((entry.payload or {}).get("status"), "in_work")

        response = self.client.post(
            f"/orders/other/{order_id}/",
            {
                "action": "complete",
                "what_done": "Свободная задача выполнена",
                "service_name": ["Другое"],
                "service_qty": ["1"],
                "service_unit": ["шт."],
            },
        )
        self.assertEqual(response.status_code, 302)
        entry = OrderAuditEntry.objects.filter(order_type="other", order_id=order_id).order_by("-created_at").first()
        # Исполнитель завершает работу → проверка менеджером (legacy payload: in_work).
        self.assertEqual((entry.payload or {}).get("status"), "in_work")
        self.assertEqual((entry.payload or {}).get("workflow_status"), "awaiting_manager_check")

        self.client.force_login(self.manager_user)
        response = self.client.post(f"/orders/other/{order_id}/", {"action": "complete"})
        self.assertEqual(response.status_code, 302)
        entry = OrderAuditEntry.objects.filter(order_type="other", order_id=order_id).order_by("-created_at").first()
        self.assertEqual((entry.payload or {}).get("status"), "completed")

        get_response = self.client.get(f"/orders/other/{order_id}/")
        self.assertEqual(get_response.status_code, 200)
        self.assertContains(get_response, "Закрыта")
        self.assertContains(get_response, "Прочая заявка")
        self.assertContains(get_response, "Результат выполнения")
        self.assertNotContains(get_response, "Подтвердить и отправить на склад")
        self.assertNotContains(get_response, "Взять в работу")

    def test_orders_other_journal_lists_rows(self):
        created = create_other_request(
            agency=self.agency,
            user=self.user,
            category="measure_size",
            description="Замер",
        )
        self.client.force_login(self.manager_user)
        response = self.client.get("/orders/other/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, created["order_id"])
        self.assertContains(response, "Измерить размер")


class ClientLkMarketplacePayloadTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.user = User.objects.create_user(username="lk_mp_client", password="pwd")
        self.agency = Agency.objects.create(agn_name="Клиент МП", portal_user=self.user)
        self.wb = Market.objects.create(id=9101, name="WB")
        self.ozon = Market.objects.create(id=9102, name="OZON")
        MarketCredential.objects.create(
            id=9201,
            agency=self.agency,
            market=self.wb,
            market_key="wb-token",
        )
        MarketCredential.objects.create(
            id=9202,
            agency=self.agency,
            market=self.ozon,
            market_key="ozon-key",
            client_id="12345",
        )
        SKU.objects.create(agency=self.agency, sku_code="SKU-MP-1", name="Товар МП")

    @patch("client_cabinet.marketplace_lk.probe_marketplace_auth")
    def test_build_marketplaces_payload_marks_configured(self, probe_mock):
        probe_mock.return_value = {
            "auth_ok": True,
            "message": "",
            "checked_at": "2026-07-14T12:00:00",
        }
        rows = build_marketplaces_payload(self.agency)
        by_id = {item["id"]: item for item in rows}
        self.assertTrue(by_id["wb"]["configured"])
        self.assertTrue(by_id["ozon"]["configured"])
        self.assertEqual(by_id["wb"]["token_health"], "ok")
        self.assertEqual(by_id["wb"]["action_label"], "Обновить каталог")

    @patch("client_cabinet.marketplace_lk.marketplace_request")
    def test_build_mp_stocks_uses_marketplace_request_and_friendly_proxy_error(self, request_mock):
        from requests.exceptions import ProxyError

        request_mock.side_effect = ProxyError(
            "Unable to connect to proxy",
            OSError("Tunnel connection failed: 403 Forbidden"),
        )
        payload = build_mp_stocks_payload(self.agency, force_refresh=True)
        self.assertTrue(payload["wb"]["configured"])
        self.assertIn("прокси", (payload["wb"].get("client_message") or payload["wb"].get("error") or "").lower())
        self.assertTrue(request_mock.called)

    @patch("client_cabinet.marketplace_lk.marketplace_request")
    def test_build_mp_stocks_maps_wb_statistics_response(self, request_mock):
        response = Mock()
        response.status_code = 200
        response.json.return_value = [
            {"supplierArticle": "SKU-MP-1", "quantity": 7},
        ]
        request_mock.return_value = response
        payload = build_mp_stocks_payload(self.agency, force_refresh=True)
        self.assertTrue(payload["wb"]["ok"])
        self.assertEqual(payload["wb"]["total_present"], 7)
        self.assertTrue(any(row["sku"] == "SKU-MP-1" and row["mp_qty"] == 7 for row in payload["wb"]["rows"]))


class ClientLkIntegrationClientTests(TestCase):
    """HTTP-level checks via Django test client for primary LK routes."""

    def setUp(self):
        self.user = User.objects.create_user(username="lk_http_client", password="pwd")
        self.agency = Agency.objects.create(agn_name="HTTP ЛК", portal_user=self.user)
        self.client = Client()
        self.client.force_login(self.user)

    def test_lk_page_and_main_apis(self):
        page = self.client.get(f"/client/dashboard/lk/?client={self.agency.id}")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Прочие заявки")
        self.assertContains(page, "Маркетплейсы")
        self.assertContains(page, 'data-page="/request"')
        self.assertContains(page, "nom-search")
        self.assertContains(page, "#/other-requests")
        # action_urls.other_journal → WMS journal URL in markup
        self.assertContains(page, f"/orders/other/?client={self.agency.id}")

        dash = self.client.get(f"/client/api/v1/dashboard/?client={self.agency.id}")
        self.assertEqual(dash.status_code, 200)
        self.assertTrue(dash.json()["ok"])

        nomenclature = self.client.get(f"/client/api/v1/nomenclature/?client={self.agency.id}")
        self.assertEqual(nomenclature.status_code, 200)
        self.assertTrue(nomenclature.json()["ok"])

        markets = self.client.get(f"/client/api/v1/marketplaces/?client={self.agency.id}")
        self.assertEqual(markets.status_code, 200)
        self.assertTrue(markets.json()["ok"])

        marking = self.client.get(f"/client/api/v1/marking/?client={self.agency.id}")
        self.assertEqual(marking.status_code, 200)
        self.assertTrue(marking.json()["ok"])

        create = self.client.post(
            f"/client/api/v1/other-requests/?client={self.agency.id}",
            data=json.dumps({"category": "measure_size", "description": "через client"}),
            content_type="application/json",
        )
        self.assertEqual(create.status_code, 200)
        body = create.json()
        self.assertTrue(body["ok"])
        self.assertIn("order_id", body["data"])


class ClientLkPhase1RequestUxTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="lk_phase1_client", password="pwd")
        self.agency = Agency.objects.create(agn_name="Фаза 1 ЛК", portal_user=self.user)
        self.client = Client()
        self.client.force_login(self.user)
        Employee.objects.create(full_name="Менеджер Phase1", role="manager", is_active=True)

    def test_request_detail_api_and_client_actions(self):
        from client_cabinet.other_requests import create_other_request

        created = create_other_request(
            agency=self.agency,
            user=self.user,
            category="measure_size",
            description="Замер phase1",
            save_as_draft=True,
        )
        order_id = created["order_id"]
        get_resp = self.client.get(f"/client/api/v1/requests/other/{order_id}/?client={self.agency.id}")
        self.assertEqual(get_resp.status_code, 200)
        data = get_resp.json()["data"]
        self.assertEqual(data["order_type"], "other")
        self.assertTrue(data["actions"]["can_cancel"])
        self.assertTrue(data["actions"]["can_comment"])
        self.assertFalse(data["actions"]["open_wms"])
        self.assertTrue(data["actions"]["can_continue_draft"])
        self.assertIn("#/other-requests", data.get("wms_url") or "")

        comment = self.client.post(
            f"/client/api/v1/requests/other/{order_id}/?client={self.agency.id}",
            data=json.dumps({"action": "comment", "text": "нужен срочно"}),
            content_type="application/json",
        )
        self.assertEqual(comment.status_code, 200)
        self.assertTrue(comment.json()["ok"])

        cancel = self.client.post(
            f"/client/api/v1/requests/other/{order_id}/?client={self.agency.id}",
            data=json.dumps({"action": "cancel", "text": "не нужно"}),
            content_type="application/json",
        )
        self.assertEqual(cancel.status_code, 200)
        self.assertEqual(cancel.json()["data"]["status"], "cancelled")

    def test_dashboard_lk_uses_shared_action_urls(self):
        response = self.client.get(f"/client/dashboard/lk/?client={self.agency.id}")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("#/other-requests", content)
        self.assertIn(f"/orders/other/?client={self.agency.id}", content)
        self.assertIn('data-page="/request"', content)
        self.assertIn("Карточка открывается внутри личного кабинета", content)

    def test_fast_shell_defers_regular_request_rows_to_api(self):
        OrderAuditEntry.objects.create(
            order_id="PR-FAST-SHELL",
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Обычная заявка",
            payload={"status": "submitted", "status_label": "Ждет подтверждения"},
        )
        invalidate_request_payloads_cache(self.agency.id)
        page = self.client.get(f"/client/dashboard/lk/?client={self.agency.id}")
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context.get("lk_fast_shell"))
        self.assertFalse(
            any(row.get("order_id") == "PR-FAST-SHELL" for row in (page.context.get("orders_list") or []))
        )

        requests = self.client.get(f"/client/api/v1/requests/?client={self.agency.id}")
        self.assertEqual(requests.status_code, 200)
        rows = requests.json()["data"]["requests"]
        self.assertTrue(any(row.get("order_id") == "PR-FAST-SHELL" for row in rows))

    def test_nomenclature_api_pagination(self):
        SKU.objects.create(agency=self.agency, sku_code="SKU-P1-1", name="Товар phase1", brand="BrandA")
        SKU.objects.create(agency=self.agency, sku_code="SKU-P1-2", name="Другой", brand="BrandB")
        response = self.client.get(
            f"/client/api/v1/nomenclature/?client={self.agency.id}&search=phase1&page_size=10"
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        rows = payload["data"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku_code"], "SKU-P1-1")
        self.assertIn("BrandA", payload["data"]["filters"]["brand_options"])

    def test_requests_list_marks_receiving_attention(self):
        from audit.models import OrderAuditEntry

        OrderAuditEntry.objects.create(
            order_id="rcv-p1-attn",
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт отправлен клиенту",
            payload={"act_sent": "Акт приемки"},
        )
        response = self.client.get(f"/client/api/v1/dashboard/?client={self.agency.id}")
        self.assertEqual(response.status_code, 200)
        rows = response.json()["data"]["requests"]
        row = next(r for r in rows if r["order_id"] == "rcv-p1-attn")
        self.assertTrue(row["attention"])
        self.assertEqual(row["detail_url"], "#/request/receiving/rcv-p1-attn")
        self.assertEqual(row["status"], "clarification")

    def test_dashboard_shows_shipping_act_attention_card(self):
        from shipping.models import ShippingOrder

        order = ShippingOrder.objects.create(
            number="SO-ACT-ATTN",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_SHIPPED,
        )
        OrderAuditEntry.objects.create(
            order_id="SO-ACT-ATTN",
            order_type="shipping",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт отгрузки отправлен клиенту",
            payload={
                "status": "shipped",
                "act": "shipping_dispatch_act",
                "act_manager_signed": True,
                "act_sent": True,
            },
        )
        factory = RequestFactory()
        request = factory.get(f"/client/dashboard/lk/?client={self.agency.id}")
        request.user = self.user
        context = build_dashboard_context(
            request=request,
            selected_client=self.agency,
            client_view=True,
            run_inventory_check=False,
        )
        cards = context.get("act_attention_cards") or []
        shipping_card = next(
            (card for card in cards if card.get("order_id") == "SO-ACT-ATTN"),
            None,
        )
        self.assertIsNotNone(shipping_card)
        self.assertEqual(shipping_card["order_type"], "shipping")
        self.assertEqual(shipping_card["detail_url"], "#/request/shipping/SO-ACT-ATTN")
        self.assertIn("Акт отгрузки", shipping_card["title"])

    def test_ssr_orders_list_keeps_receiving_attention_and_lk_links(self):
        from audit.models import OrderAuditEntry

        OrderAuditEntry.objects.create(
            order_id="rcv-ssr-attn",
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Черновик",
            payload={"status": "submitted", "status_label": "Ждет подтверждения"},
        )
        OrderAuditEntry.objects.create(
            order_id="rcv-ssr-attn",
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Акт отправлен клиенту",
            payload={"act_sent": "Акт приемки"},
        )
        response = self.client.get(f"/client/dashboard/lk/?client={self.agency.id}")
        self.assertEqual(response.status_code, 200)
        orders_list = response.context["orders_list"]
        row = next(r for r in orders_list if r["order_id"] == "rcv-ssr-attn")
        self.assertTrue(row["attention"])
        self.assertEqual(row["status"], "clarification")
        self.assertEqual(row["detail_url"], "#/request/receiving/rcv-ssr-attn")
        self.assertTrue(response.context.get("lk_fast_shell"))
        self.assertContains(response, "Акты на согласовании")
        self.assertContains(response, "Посмотреть акты")
        self.assertContains(response, "Акт приемки по заявке №rcv-ssr-attn")
        self.assertNotContains(response, '<a class="card" href="#/knowledge"><i>?</i>База знаний</a>', html=True)
        self.assertEqual(
            response.context["action_urls"]["receiving_new"],
            f"/client/{self.agency.id}/receiving/new/",
        )
        self.assertEqual(
            response.context["action_urls"]["shipping_new"],
            f"/client/{self.agency.id}/shipping/new/",
        )
        self.assertEqual(
            response.context["action_urls"]["processing_new"],
            f"/client/{self.agency.id}/packing/new/",
        )

    def test_shipping_request_bindings_are_agency_scoped(self):
        from audit.models import OrderAuditEntry
        from shipping.models import ShippingOrder, ShippingOrderItem

        other_user = User.objects.create_user(username="lk_other_agency_user", password="pwd")
        other_agency = Agency.objects.create(agn_name="Чужой клиент", portal_user=other_user)
        order = ShippingOrder.objects.create(
            number="SO-LK-SCOPE-1",
            agency=self.agency,
            status="submitted",
            created_by=self.user,
            supply_type=ShippingOrder.SUPPLY_BOX,
        )
        ShippingOrderItem.objects.create(
            order=order,
            sku_code="SKU-1",
            name="Товар",
            size="M",
            qty_requested=3,
        )
        OrderAuditEntry.objects.create(
            order_id=order.number,
            order_type="shipping",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Отгрузка создана",
            payload={"status": "submitted", "shipping_state": "submitted"},
        )
        # Same-looking audit for another client must not leak into this LK.
        OrderAuditEntry.objects.create(
            order_id="SO-OTHER-CLIENT",
            order_type="shipping",
            action="status",
            agency=other_agency,
            user=other_user,
            description="Чужая отгрузка",
            payload={"status": "submitted", "shipping_state": "submitted"},
        )

        dash = self.client.get(f"/client/api/v1/dashboard/?client={self.agency.id}")
        self.assertEqual(dash.status_code, 200)
        rows = dash.json()["data"]["requests"]
        self.assertTrue(any(r["order_id"] == order.number for r in rows))
        self.assertFalse(any(r["order_id"] == "SO-OTHER-CLIENT" for r in rows))
        own = next(r for r in rows if r["order_id"] == order.number)
        self.assertEqual(own["detail_url"], f"#/request/shipping/{order.number}")
        self.assertIn(f"/shipping/{order.id}/", own["wms_url"])
        self.assertIn(f"client={self.agency.id}", own["wms_url"])

        detail = self.client.get(
            f"/client/api/v1/requests/shipping/{order.number}/?client={self.agency.id}"
        )
        self.assertEqual(detail.status_code, 200)
        body = detail.json()["data"]
        self.assertEqual(body["type"], "shipping")
        self.assertEqual(len(body["lines"]), 1)
        self.assertEqual(body["lines"][0]["sku_code"], "SKU-1")
        self.assertEqual(body["lines"][0]["qty_requested"], 3)
        meta = {row["label"]: row["value"] for row in body["meta"]}
        self.assertEqual(meta["Тип поставки"], "Короб")

        # Portal users are locked to their own agency; other client must not see this order.
        other_client = Client()
        other_client.force_login(other_user)
        other_detail = other_client.get(
            f"/client/api/v1/requests/shipping/{order.number}/?client={other_agency.id}"
        )
        self.assertEqual(other_detail.status_code, 404)

        foreign = self.client.get(
            f"/client/api/v1/requests/shipping/SO-OTHER-CLIENT/?client={self.agency.id}"
        )
        self.assertEqual(foreign.status_code, 404)


class ClientLkPhase2OtherOpsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="lk_phase2_client", password="pwd")
        self.manager_user = User.objects.create_user(username="lk_phase2_mgr", password="pwd")
        self.other_manager_user = User.objects.create_user(username="lk_phase2_other_mgr", password="pwd")
        self.agency = Agency.objects.create(
            agn_name="Фаза 2 ЛК",
            portal_user=self.user,
            mened_user_id=None,
        )
        self.manager = Employee.objects.create(
            full_name="Менеджер клиента А",
            role="manager",
            user=self.manager_user,
            is_active=True,
        )
        self.other_manager = Employee.objects.create(
            full_name="Аарон Первый по алфавиту",
            role="manager",
            user=self.other_manager_user,
            is_active=True,
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_other_request_assigns_client_manager_via_mened_user_id(self):
        from client_cabinet.other_requests import create_other_request

        self.agency.mened_user_id = self.manager_user.id
        self.agency.save(update_fields=["mened_user_id"])
        created = create_other_request(
            agency=self.agency,
            user=self.user,
            category="repack",
            description="Назначить менеджеру клиента",
        )
        task = Task.objects.get(pk=created["manager_task_id"])
        self.assertEqual(task.assigned_to_id, self.manager.id)
        self.assertNotEqual(task.assigned_to_id, self.other_manager.id)

    def test_other_request_attachment_and_download(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile("photo-test.jpg", b"fake-image-bytes", content_type="image/jpeg")
        response = self.client.post(
            f"/client/api/v1/other-requests/?client={self.agency.id}",
            data={
                "category": "photo_video",
                "description": "нужно фото",
                "save_as_draft": "1",
                "attachments": upload,
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        attachments = body["data"]["attachments"]
        self.assertEqual(len(attachments), 1)
        self.assertTrue(attachments[0]["filename"].startswith("photo-test"))
        order_id = body["data"]["order_id"]
        detail = self.client.get(f"/client/api/v1/requests/other/{order_id}/?client={self.agency.id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.json()["data"]["attachments"]), 1)
        file_url = attachments[0]["url"] + f"?client={self.agency.id}"
        download = self.client.get(file_url)
        self.assertEqual(download.status_code, 200)

    def test_warehouse_status_pushes_notification(self):
        from client_cabinet.lk_requests import build_client_notifications

        created = create_other_request(
            agency=self.agency,
            user=self.user,
            category="courier_box",
            description="короб",
        )
        order_id = created["order_id"]
        manager_task = Task.objects.get(route=f"/orders/other/{order_id}/", assigned_to=self.manager)
        send_other_to_warehouse(manager_task, SimpleNamespace(user=self.manager_user))
        accept_other_by_warehouse(order_id=order_id, user=self.storekeeper_user)
        take_other_in_work(order_id=order_id, user=self.storekeeper_user)
        notes = build_client_notifications(self.agency, limit=20)
        self.assertTrue(
            any("менеджер принял" in (n.get("text") or "").lower() or "принята в работу" in (n.get("text") or "").lower() for n in notes)
        )
        self.assertFalse(any("кладовщик" in (n.get("text") or "").lower() for n in notes))
        self.assertTrue(any(n.get("detail_url") == f"#/request/other/{order_id}" for n in notes))

        page = self.client.get(f"/client/dashboard/lk/?client={self.agency.id}")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Честный знак · свободно")
        self.assertContains(page, "other-attachments")

    def test_shipping_discrepancy_pushes_client_notification(self):
        from client_cabinet.lk_requests import build_client_notifications

        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="shipping",
            order_id="SO-000082",
            action="shipping_discrepancy_requested",
            description="Отгрузка с расхождением отправлена на согласование.",
            payload={
                "status": "pending",
                "order_title": "Заявка на отгрузку",
                "order_display_title": "Заявка на отгрузку №SO-000082",
                "missing_text": "сушилка006 / ШК 2043106085326 / тип gv: 24 короб x 4 шт = 96 шт",
                "reason": "В OTG 58 коробов, в заявке ожидается 82.",
            },
        )

        notes = build_client_notifications(self.agency, limit=20)
        mismatch_note = next(
            (note for note in notes if note.get("detail_url") == "#/request/shipping/SO-000082"),
            None,
        )

        self.assertIsNotNone(mismatch_note)
        self.assertEqual(mismatch_note["priority"], "high")
        self.assertIn("SO-000082", mismatch_note["title"])
        self.assertIn("Есть расхождения по отгрузке", mismatch_note["text"])
        self.assertIn("сушилка006", mismatch_note["text"])

    def test_marking_widgets_and_api(self):
        from marking.models import MarkingCode
        from django.utils import timezone

        MarkingCode.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="",
            sku_code="SKU-CZ-1",
            barcode="200000000101",
            code="CZ-FREE-1",
        )
        MarkingCode.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="proc-1",
            sku_code="SKU-CZ-1",
            barcode="200000000102",
            code="CZ-USED-1",
            used_at=timezone.now(),
        )
        page = self.client.get(f"/client/dashboard/lk/?client={self.agency.id}")
        self.assertContains(page, ">1</strong>")
        marking = self.client.get(f"/client/api/v1/marking/?client={self.agency.id}")
        self.assertEqual(marking.status_code, 200)
        data = marking.json()["data"]
        self.assertEqual(data["free"], 1)
        self.assertEqual(data["used"], 1)
        self.assertEqual(data["total"], 2)


class ClientLkPhase3FinanceBillingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="lk_phase3_client", password="pwd")
        self.agency = Agency.objects.create(
            agn_name="Фаза 3 ЛК",
            portal_user=self.user,
            contract_numb="Д-100",
            contract_link="https://example.com/contract.pdf",
            sign_oferta=True,
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_finance_documents_include_acts_invoices_and_contract(self):
        from client_cabinet.models import ClientFinanceDocument
        from django.utils import timezone

        OrderAuditEntry.objects.create(
            order_id="rcv-fin-1",
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт отправлен",
            payload={"act_sent": "Акт приемки №rcv-fin-1"},
        )
        ClientFinanceDocument.objects.create(
            agency=self.agency,
            doc_kind=ClientFinanceDocument.KIND_INVOICE,
            title="Счёт за услуги",
            number="INV-1",
            amount="1500.00",
            period=timezone.localdate().strftime("%Y-%m"),
            issued_at=timezone.localdate(),
            status=ClientFinanceDocument.STATUS_ISSUED,
        )
        response = self.client.get(f"/client/api/v1/finance/?client={self.agency.id}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        docs = payload["data"]["documents"]
        kinds = {row["doc_kind"] for row in docs}
        self.assertIn("invoice", kinds)
        self.assertIn("act", kinds)
        self.assertTrue(any(row["id"] == f"contract-{self.agency.id}" for row in docs))
        self.assertTrue(
            any(
                (row.get("detail_url") or "").startswith("#/request/receiving/")
                or (row.get("detail_url") or "").startswith("#/request/shipping/")
                for row in docs
                if row.get("doc_kind") == "act" and row.get("source") == "warehouse"
            )
        )
        self.assertFalse(any("/shipping/" in (row.get("detail_url") or "") and "/act/" in (row.get("detail_url") or "") for row in docs))
        receiving_act = next(row for row in docs if row.get("id") == "act-receiving-rcv-fin-1")
        self.assertEqual(receiving_act["detail_url"], "#/request/receiving/rcv-fin-1")
        self.assertEqual(receiving_act["status_label"], "Отправлен клиенту")
        self.assertIn("/orders/receiving/rcv-fin-1/act/print/", receiving_act.get("print_url") or "")

        OrderAuditEntry.objects.create(
            order_id="rcv-fin-1",
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Клиент подтвердил акт",
            payload={
                "act_sent": "Акт приемки №rcv-fin-1",
                "act_client_response": "confirmed",
                "act_viewed": True,
            },
        )
        confirmed = self.client.get(f"/client/api/v1/finance/?client={self.agency.id}")
        receiving_confirmed = next(
            row for row in confirmed.json()["data"]["documents"] if row.get("id") == "act-receiving-rcv-fin-1"
        )
        self.assertEqual(receiving_confirmed["status_label"], "Подтверждён клиентом")

        from shipping.models import ShippingOrder

        order = ShippingOrder.objects.create(
            agency=self.agency,
            created_by=self.user,
            number="SO-FIN-ACT",
            status=ShippingOrder.STATUS_PACKED,
        )
        finance2 = self.client.get(f"/client/api/v1/finance/?client={self.agency.id}")
        ship_act = next(
            row
            for row in finance2.json()["data"]["documents"]
            if row.get("id") == f"act-shipping-{order.pk}"
        )
        self.assertEqual(ship_act["detail_url"], "#/request/shipping/SO-FIN-ACT")
        self.assertEqual(ship_act["status_label"], "Готов к оформлению")
        self.assertNotEqual(ship_act["status_label"], "Упакована")
        self.assertIn(f"/shipping/{order.pk}/act/", ship_act.get("print_url") or "")
        act_page = self.client.get(f"/shipping/{order.pk}/act/")
        self.assertNotEqual(act_page.status_code, 403)
        self.assertNotContains(act_page, "Доступ запрещен")

        shipped = ShippingOrder.objects.create(
            agency=self.agency,
            created_by=self.user,
            number="SO-FIN-SHIPPED",
            status=ShippingOrder.STATUS_SHIPPED,
        )
        finance_shipped = self.client.get(f"/client/api/v1/finance/?client={self.agency.id}")
        shipped_act = next(
            row
            for row in finance_shipped.json()["data"]["documents"]
            if row.get("id") == f"act-shipping-{shipped.pk}"
        )
        self.assertEqual(shipped_act["status_label"], "Акт не отправлен")
        self.assertNotEqual(shipped_act["status_label"], "Отгружена")

        submitted = ShippingOrder.objects.create(
            agency=self.agency,
            created_by=self.user,
            number="SO-FIN-NEW",
            status=ShippingOrder.STATUS_SUBMITTED,
        )
        finance3 = self.client.get(f"/client/api/v1/finance/?client={self.agency.id}")
        shipping_ids = {
            row.get("id")
            for row in finance3.json()["data"]["documents"]
            if row.get("doc_kind") == "act" and row.get("order_type") == "shipping"
        }
        self.assertNotIn(f"act-shipping-{submitted.pk}", shipping_ids)
        early = self.client.get(f"/shipping/{submitted.pk}/act/")
        self.assertEqual(early.status_code, 302)
        self.assertIn("#/request/shipping/SO-FIN-NEW", early["Location"])

        page = self.client.get(f"/client/dashboard/lk/?client={self.agency.id}")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "finance-docs-list")
        self.assertContains(page, "Открыть акт")
        self.assertContains(page, "Счёт за услуги")
        self.assertContains(page, "billing-services-list")
        self.assertNotContains(page, '<div class="wip-panel">Раздел в разработке</div>')

    def test_billing_hides_draft_charges_until_documents_issued(self):
        """Без тарифа/акта/счёта клиенту не показываем авто-начисления и транспорт."""
        from decimal import Decimal
        from client_cabinet.models import ClientServiceCharge
        from django.utils import timezone
        from shipping.models import ShippingOrder, ShippingTransportNote

        period = timezone.localdate().strftime("%Y-%m")
        ClientServiceCharge.objects.create(
            agency=self.agency,
            service_type=ClientServiceCharge.TYPE_RECEIVING,
            description="Приёмка партии",
            amount=Decimal("250.00"),
            period=period,
            charged_at=timezone.localdate(),
            status=ClientServiceCharge.STATUS_OPEN,
            order_type="receiving",
            order_id="rcv-bill-1",
        )
        order = ShippingOrder.objects.create(
            number="OTG-BILL-1",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_SHIPPED,
        )
        ShippingTransportNote.objects.create(
            order=order,
            document_number="TN-1",
            service_cost=Decimal("800.00"),
        )
        response = self.client.get(f"/client/api/v1/billing/?client={self.agency.id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["data"]["billing_available"])
        billing = body["data"]["billing"]
        self.assertEqual(billing["total_due"], 0)
        self.assertEqual(billing.get("services") or [], [])
        self.assertFalse(billing.get("documents_issued"))
        note = (billing.get("tariff_note") or "").lower()
        self.assertTrue("тариф" in note or "начислен" in note or "акт" in note)

        dash = self.client.get(f"/client/api/v1/dashboard/?client={self.agency.id}")
        self.assertTrue(dash.json()["data"]["billing_available"])
        self.assertTrue(dash.json()["data"]["documents"])


class ClientLkPhase4NotificationsChatTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="lk_phase4_client", password="pwd")
        self.staff = User.objects.create_user(username="lk_phase4_staff", password="pwd", is_staff=True)
        self.agency = Agency.objects.create(agn_name="Фаза 4 ЛК", portal_user=self.user)
        self.client = Client()
        self.client.force_login(self.user)
        Employee.objects.create(full_name="Менеджер P4", role="manager", is_active=True)

    def test_notifications_model_and_mark_read(self):
        from client_cabinet.other_requests import create_other_request, set_other_status
        from client_cabinet.models import ClientNotification

        created = create_other_request(
            agency=self.agency,
            user=self.user,
            category="courier_box",
            description="нужен короб",
        )
        set_other_status(order_id=created["order_id"], status="warehouse", user=self.staff, description="Заявка передана на склад")
        self.assertTrue(
            ClientNotification.objects.filter(agency=self.agency, is_read=False).exists()
        )
        response = self.client.get(f"/client/api/v1/notifications/?client={self.agency.id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertGreaterEqual(body["data"]["unread_count"], 1)
        mark = self.client.post(
            f"/client/api/v1/notifications/?client={self.agency.id}",
            data=json.dumps({"action": "mark_read", "all": True}),
            content_type="application/json",
        )
        self.assertEqual(mark.status_code, 200)
        self.assertEqual(mark.json()["data"]["unread_count"], 0)
        page = self.client.get(f"/client/dashboard/lk/?client={self.agency.id}")
        self.assertContains(page, 'data-page="/chat"')
        self.assertContains(page, "nav-notif-badge")
        self.assertContains(page, "nav-chat-badge")

    def test_chat_post_and_attachment(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from client_cabinet.models import ClientChatMessage

        create = self.client.post(
            f"/client/api/v1/chat/messages/?client={self.agency.id}",
            data=json.dumps({"text": "Здравствуйте, нужен статус по отгрузке"}),
            content_type="application/json",
        )
        self.assertEqual(create.status_code, 200)
        self.assertTrue(create.json()["ok"])
        self.assertEqual(ClientChatMessage.objects.filter(agency=self.agency).count(), 1)

        # Staff reply
        staff_client = Client()
        staff_client.force_login(self.staff)
        reply = staff_client.post(
            f"/client/api/v1/chat/messages/?client={self.agency.id}",
            data=json.dumps({"text": "Приняли в работу, ответим в течение часа"}),
            content_type="application/json",
        )
        self.assertEqual(reply.status_code, 200)
        unread = self.client.get(f"/client/api/v1/chat/messages/?client={self.agency.id}")
        self.assertEqual(unread.status_code, 200)
        # Opening chat marks staff messages read for client
        self.assertEqual(unread.json()["data"]["unread_count"], 0)

        upload = SimpleUploadedFile("chat-photo.jpg", b"chat-bytes", content_type="image/jpeg")
        with_file = self.client.post(
            f"/client/api/v1/chat/messages/?client={self.agency.id}",
            data={"text": "Вложение", "attachments": upload},
        )
        self.assertEqual(with_file.status_code, 200)
        payload = with_file.json()["data"]["message"]
        self.assertEqual(len(payload["attachments"]), 1)
        file_url = payload["attachments"][0]["url"]
        download = self.client.get(file_url if "?" in file_url else file_url + f"?client={self.agency.id}")
        self.assertEqual(download.status_code, 200)

        dash = self.client.get(f"/client/api/v1/dashboard/?client={self.agency.id}")
        self.assertIn("chat_unread_count", dash.json()["data"])
        self.assertIn("notifications_unread", dash.json()["data"])


class ClientLkPhase5MarketplaceHardeningTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.user = User.objects.create_user(username="lk_phase5_client", password="pwd")
        self.agency = Agency.objects.create(agn_name="Фаза 5 ЛК", portal_user=self.user)
        self.wb = Market.objects.create(id=9301, name="WB")
        self.ozon = Market.objects.create(id=9302, name="OZON")
        self.client = Client()
        self.client.force_login(self.user)

    def test_not_connected_ux_fields(self):
        rows = build_marketplaces_payload(self.agency, check_auth=False)
        by_id = {item["id"]: item for item in rows}
        self.assertFalse(by_id["wb"]["configured"])
        self.assertEqual(by_id["wb"]["status"], "setup")
        self.assertEqual(by_id["wb"]["status_label"], "Не подключён")
        self.assertEqual(by_id["wb"]["token_health"], "missing")
        self.assertEqual(by_id["wb"]["action_label"], "Подключить")
        self.assertIn("#/chat", by_id["wb"]["reconnect_url"])

    @patch("client_cabinet.marketplace_lk.marketplace_request")
    def test_token_health_auth_error(self, request_mock):
        MarketCredential.objects.create(
            id=9311,
            agency=self.agency,
            market=self.wb,
            market_key="bad-token",
        )
        response = Mock()
        response.status_code = 401
        request_mock.return_value = response
        rows = build_marketplaces_payload(self.agency, check_auth=True)
        wb = {item["id"]: item for item in rows}["wb"]
        self.assertTrue(wb["configured"])
        self.assertEqual(wb["token_health"], "error")
        self.assertFalse(wb["auth_ok"])
        self.assertEqual(wb["action_label"], "Переподключить")
        self.assertIn("#/chat", wb["reconnect_url"])
        self.assertEqual(wb["status"], "error")

    @patch("client_cabinet.marketplace_lk.marketplace_request")
    def test_mp_stocks_cache_avoids_repeat_live_calls(self, request_mock):
        MarketCredential.objects.create(
            id=9312,
            agency=self.agency,
            market=self.wb,
            market_key="wb-token",
        )
        SKU.objects.create(agency=self.agency, sku_code="SKU-P5", name="Товар P5")
        response = Mock()
        response.status_code = 200
        response.json.return_value = [{"supplierArticle": "SKU-P5", "quantity": 3}]
        request_mock.return_value = response

        first = build_mp_stocks_payload(self.agency, force_refresh=True)
        self.assertFalse(first["from_cache"])
        calls_after_first = request_mock.call_count

        second = build_mp_stocks_payload(self.agency, force_refresh=False)
        self.assertTrue(second["from_cache"])
        self.assertEqual(request_mock.call_count, calls_after_first)
        self.assertEqual(second["wb"]["total_present"], 3)

        third = build_mp_stocks_payload(self.agency, force_refresh=True)
        self.assertFalse(third["from_cache"])
        self.assertGreater(request_mock.call_count, calls_after_first)

    def test_supplies_link_to_shipping_wms_detail(self):
        from datetime import timedelta

        from django.utils import timezone

        from client_cabinet.marketplace_lk import build_supplies_payload
        from shipping.models import ShippingOrder

        order = ShippingOrder.objects.create(
            number="OTG-P5-1",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_SUBMITTED,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            marketplace=self.wb,
            slot_date=timezone.localdate() + timedelta(days=1),
            destination_warehouse="Коледино",
        )
        payload = build_supplies_payload(self.agency)
        self.assertTrue(payload["upcoming"])
        row = payload["upcoming"][0]
        self.assertEqual(row["id"], order.id)
        self.assertEqual(row["detail_url"], f"/shipping/{order.id}/?client={self.agency.id}")
        self.assertEqual(row["wms_url"], row["detail_url"])
        self.assertEqual(row["open_label"], "Открыть отгрузку в WMS")

    def test_api_marketplaces_refresh_flag_and_page_markers(self):
        page = self.client.get(f"/client/dashboard/lk/?client={self.agency.id}")
        self.assertContains(page, "mp-stocks-refresh")
        self.assertContains(page, "mp-open-wms")
        self.assertContains(page, "refresh=1")

        response = self.client.get(f"/client/api/v1/marketplaces/?client={self.agency.id}&refresh=1")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertIn("from_cache", body["data"]["mp_stocks"])
        markets = {item["id"]: item for item in body["data"]["marketplaces"]}
        self.assertEqual(markets["wb"]["action_label"], "Подключить")


class ClientLkInCabinetRequestDetailTests(TestCase):
    """Request cards stay in LK: lines/meta for receiving/shipping/processing, no WMS CTA."""

    def setUp(self):
        self.user = User.objects.create_user(username="lk_req_detail", password="pwd")
        self.agency = Agency.objects.create(agn_name="ЛК заявки в кабинете", portal_user=self.user)
        self.client = Client()
        self.client.force_login(self.user)
        self.wb = Market.objects.create(id=9501, name="WB")
        self.ozon = Market.objects.create(id=9502, name="OZON")

    def test_receiving_detail_shows_plan_fact_and_no_wms_cta(self):
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="receiving",
            order_id="214",
            action="status",
            description="Создана приёмка",
            payload={
                "status": "submitted",
                "status_label": "Ожидает поставку",
                "expected_boxes": 2,
                "comment": "Паллета у ворот",
                "items": [
                    {"sku_code": "SKU-R1", "name": "Товар R1", "size": "M", "qty": 10},
                    {"sku_code": "SKU-R2", "name": "Товар R2", "size": "", "qty": 5},
                ],
            },
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="receiving",
            order_id="214",
            action="status",
            description="Акт приёмки",
            payload={
                "status": "in_progress",
                "status_label": "В обработке",
                "act": "receiving",
                "act_sent": True,
                "act_items": [
                    {"sku_code": "SKU-R1", "name": "Товар R1", "size": "M", "planned_qty": 10, "actual_qty": 8},
                    {"sku_code": "SKU-R2", "name": "Товар R2", "planned_qty": 5, "actual_qty": 5},
                ],
            },
        )
        response = self.client.get(f"/client/api/v1/requests/receiving/214/?client={self.agency.id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertFalse(data["actions"]["open_wms"])
        self.assertTrue(data["actions"]["can_export_excel"])
        self.assertTrue(data["has_mismatch"])
        self.assertEqual(len(data["lines"]), 2)
        act = data.get("act") or {}
        self.assertTrue(act.get("exists"))
        self.assertTrue(act.get("needs_confirm"))
        self.assertIn("/orders/receiving/214/act/print/", act.get("print_url") or "")
        self.assertNotEqual(data.get("status_label"), "Выполнена")
        self.assertIn("акт", (data.get("status_label") or "").lower())
        by_sku = {row["sku_code"]: row for row in data["lines"]}
        self.assertEqual(by_sku["SKU-R1"]["qty_planned"], 10)
        self.assertEqual(by_sku["SKU-R1"]["qty_actual"], 8)
        self.assertTrue(by_sku["SKU-R1"]["mismatch"])
        self.assertTrue(any(row["label"] == "Ожидаемые короба" for row in data["meta"]))
        export = self.client.get(f"/client/api/v1/requests/receiving/214/export/?client={self.agency.id}")
        self.assertEqual(export.status_code, 200)
        self.assertIn(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            export["Content-Type"],
        )
        self.assertIn("attachment", export["Content-Disposition"])
        self.assertTrue(export.content[:2] == b"PK")
        from io import BytesIO
        from openpyxl import load_workbook

        book = load_workbook(BytesIO(export.content))
        sheet = book.active
        self.assertEqual(sheet.title, "Артикулы")
        headers = [cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
        self.assertEqual(
            headers,
            ["ШК", "Артикул", "Наименование", "Размер", "План", "Факт", "Короба", "Расхождение"],
        )
        values = [[cell.value for cell in row] for row in sheet.iter_rows(min_row=2, max_row=sheet.max_row)]
        self.assertTrue(any(row[1] == "SKU-R1" for row in values))
        self.assertFalse(any(row and row[0] == "Заявка" for row in values))
        self.assertFalse(any(row and row[0] == "Расхождения" for row in values))
        page = self.client.get(f"/client/dashboard/lk/?client={self.agency.id}")
        self.assertNotIn(">Открыть в WMS<", page.content.decode("utf-8"))

    def test_receiving_detail_shows_act_after_manager_send_even_if_viewed(self):
        """Act fields often sit on earlier audit rows; detail must still expose confirm/download."""
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="receiving",
            order_id="127",
            action="status",
            description="Акт отправлен клиенту",
            payload={
                "act_sent": "Акт приемки с расхождениями",
                "act_manager_signed": True,
                "status": "done",
                "status_label": "Завершена",
            },
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="receiving",
            order_id="127",
            action="status",
            description="Клиент просмотрел акт",
            payload={
                "status": "done",
                "status_label": "Завершена",
                "act_viewed": True,
            },
        )
        response = self.client.get(f"/client/api/v1/requests/receiving/127/?client={self.agency.id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        act = data.get("act") or {}
        self.assertTrue(act.get("exists"))
        self.assertTrue(act.get("needs_confirm"))
        self.assertEqual(act.get("status_label"), "Ожидает подтверждения")
        self.assertIn("/orders/receiving/127/act/print/", act.get("print_url") or "")
        self.assertEqual(data.get("status_label"), "Акт отправлен клиенту")
        self.assertTrue(data.get("attention"))
        page = self.client.get(f"/client/dashboard/lk/?client={self.agency.id}")
        html = page.content.decode("utf-8")
        self.assertIn("Открыть / скачать акт", html)
        self.assertIn("Подтвердить акт", html)

    def test_client_history_hides_storekeeper_and_warehouse_steps(self):
        from employees.models import Employee
        from types import SimpleNamespace
        from unittest.mock import patch

        storekeeper_user = get_user_model().objects.create_user(
            username="lk_storekeeper_hist",
            password="pass",
        )
        Employee.objects.create(
            full_name="Кладовщик Тест",
            role="storekeeper",
            user=storekeeper_user,
            is_active=True,
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="receiving",
            order_id="VIS-R-1",
            action="status",
            description="Создана приёмка",
            payload={"status": "submitted", "status_label": "Ждет подтверждения"},
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="receiving",
            order_id="VIS-R-1",
            action="status",
            description="Подтверждено и отправлено на склад",
            payload={"status": "warehouse", "status_label": "В ожидании поставки товара"},
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=storekeeper_user,
            order_type="receiving",
            order_id="VIS-R-1",
            action="status",
            description="Заявка взята в работу кладовщиком",
            payload={"status": "warehouse", "status_label": "Взята в работу", "storekeeper_name": "Кладовщик Тест"},
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=storekeeper_user,
            order_type="receiving",
            order_id="VIS-R-1",
            action="update",
            description="Открыт акт размещения",
            payload={"status": "warehouse", "status_label": "Размещение на складе", "act": "placement", "act_state": "open"},
        )

        resolved = SimpleNamespace(
            is_terminal=False,
            is_ready_for_next_step=False,
            label_for=lambda audience="default": "В ожидании поставки товара",
            next_step_for=lambda audience="default": "",
        )
        with patch(
            "sklad.services.warehouse_state.WarehouseGoodsStateResolver.resolve_for_receiving_order",
            return_value=resolved,
        ), patch(
            "sklad.services.WarehouseGoodsStateResolver.resolve_for_receiving_order",
            return_value=resolved,
        ):
            detail = self.client.get(f"/client/api/v1/requests/receiving/VIS-R-1/?client={self.agency.id}")
        self.assertEqual(detail.status_code, 200)
        data = detail.json()["data"]
        self.assertEqual(data["status_label"], "В работе")
        history = data["history"]
        texts = " ".join((row.get("description") or "") for row in history).lower()
        users = [row.get("user") or "" for row in history]
        self.assertTrue(any("менеджер принял" in (row.get("description") or "").lower() for row in history))
        self.assertNotIn("кладовщик", texts)
        self.assertNotIn("размещения", texts)
        self.assertTrue(all(not u for u in users))
        self.assertFalse(any("lk_storekeeper_hist" in u for u in users))

    def test_client_can_cancel_receiving_before_warehouse(self):
        Employee.objects.create(full_name="Менеджер отмен", role="manager", is_active=True)
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="receiving",
            order_id="CANCEL-R-1",
            action="status",
            description="Создана приёмка",
            payload={
                "status": "submitted",
                "status_label": "Ждет подтверждения",
                "items": [{"sku_code": "SKU-CANCEL", "qty": 1}],
            },
        )

        detail = self.client.get(f"/client/api/v1/requests/receiving/CANCEL-R-1/?client={self.agency.id}")
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.json()["data"]["actions"]["can_cancel"])
        response = self.client.post(
            f"/client/api/v1/requests/receiving/CANCEL-R-1/?client={self.agency.id}",
            data=json.dumps({"action": "cancel", "text": "не актуально"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        latest = OrderAuditEntry.objects.filter(order_type="receiving", order_id="CANCEL-R-1").latest("id")
        self.assertEqual((latest.payload or {}).get("status"), "cancelled")
        self.assertEqual((latest.payload or {}).get("status_label"), "Отменена")
        self.assertEqual(response.json()["data"]["status"], "cancelled")

    def test_client_cannot_cancel_receiving_when_awaits_manager_sign(self):
        """После склада (ожидает подписи) — кнопки отмены у клиента нет."""
        Employee.objects.create(full_name="Менеджер подписи", role="manager", is_active=True)
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="receiving",
            order_id="CANCEL-R-SIGN-1",
            action="status",
            description="Акт отправлен менеджеру",
            payload={
                "status": "act_sent",
                "status_label": "Ожидает подписи менеджера",
                "act_storekeeper_signed": True,
                "items": [{"sku_code": "SKU-SIGN", "qty": 1, "actual_qty": 1}],
            },
        )

        detail = self.client.get(
            f"/client/api/v1/requests/receiving/CANCEL-R-SIGN-1/?client={self.agency.id}"
        )
        self.assertEqual(detail.status_code, 200)
        self.assertFalse(detail.json()["data"]["actions"]["can_cancel"])

        response = self.client.post(
            f"/client/api/v1/requests/receiving/CANCEL-R-SIGN-1/?client={self.agency.id}",
            data=json.dumps({"action": "cancel", "text": "ошибка в поставке"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("Отмена недоступна", response.json().get("error") or "")

    def test_client_requests_manager_cancel_after_warehouse(self):
        manager = Employee.objects.create(full_name="Менеджер согласования", role="manager", is_active=True)
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="processing",
            order_id="CANCEL-P-1",
            action="status",
            description="Передано в обработку",
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "stock_rows": [{"sku_code": "SKU-P", "qty": 2}],
            },
        )

        detail = self.client.get(f"/client/api/v1/requests/processing/CANCEL-P-1/?client={self.agency.id}")
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.json()["data"]["actions"]["can_cancel"])
        self.assertEqual(
            detail.json()["data"]["actions"]["cancel_label"],
            "Запросить отмену у менеджера",
        )
        response = self.client.post(
            f"/client/api/v1/requests/processing/CANCEL-P-1/?client={self.agency.id}",
            data=json.dumps({"action": "cancel", "text": "клиент просит остановить"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        latest = OrderAuditEntry.objects.filter(order_type="processing", order_id="CANCEL-P-1").latest("id")
        self.assertEqual(latest.action, "cancel_request")
        self.assertEqual((latest.payload or {}).get("status"), "cancel_requested")
        self.assertEqual((latest.payload or {}).get("status_label"), "Отмена на согласовании менеджера")
        self.assertTrue(
            Task.objects.filter(
                route="/orders/processing/CANCEL-P-1/",
                assigned_to=manager,
                title__icontains="Согласуйте отмену заявки",
            ).exists()
        )
        self.assertEqual(response.json()["data"]["status_label"], "Отмена на согласовании менеджера")

    def test_receiving_detail_splits_ready_and_defect_like_warehouse_act(self):
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="receiving",
            order_id="215",
            action="status",
            description="Создана приёмка",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "goods_type": "gv",
                "items": [
                    {"sku_code": "нож027", "name": "Нож туристический с ножнами", "size": "0", "qty": 2000, "barcode": "2045130281743"},
                    {"sku_code": "нож028", "name": "Нож туристический складной", "size": "0", "qty": 1920, "barcode": "2045130312799"},
                ],
            },
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="receiving",
            order_id="215",
            action="update",
            description="Создан акт размещения",
            payload={
                "act": "placement",
                "act_state": "closed",
                "goods_type": "br",
                "goods_type_label": "Брак",
                "act_items": [
                    {"sku_code": "нож027", "name": "Нож туристический с ножнами", "size": "0", "actual_qty": 1998, "barcode": "2045130281743"},
                    {"sku_code": "нож028", "name": "Нож туристический складной", "size": "0", "actual_qty": 1915, "barcode": "2045130312799"},
                ],
                "act_boxes": [
                    {
                        "code": "BOX-GV-1",
                        "goods_type": "gv",
                        "items": [
                            {
                                "sku_code": "нож027",
                                "name": "Нож туристический с ножнами",
                                "size": "0",
                                "qty": 1995,
                                "barcode": "2045130281743",
                                "goods_type": "gv",
                            }
                        ],
                    },
                    {
                        "code": "BOX-BR-1",
                        "goods_type": "br",
                        "items": [
                            {
                                "sku_code": "нож027",
                                "name": "Нож туристический с ножнами",
                                "size": "0",
                                "qty": 3,
                                "barcode": "2045130281743",
                                "goods_type": "br",
                            }
                        ],
                    },
                    {
                        "code": "BOX-GV-2",
                        "goods_type": "gv",
                        "items": [
                            {
                                "sku_code": "нож028",
                                "name": "Нож туристический складной",
                                "size": "0",
                                "qty": 1915,
                                "barcode": "2045130312799",
                                "goods_type": "gv",
                            }
                        ],
                    },
                ],
                "act_pallets": [],
            },
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="receiving",
            order_id="215",
            action="status",
            description="Создан акт приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "act": "receiving",
                "goods_type": "br",
                "act_items": [
                    {"sku_code": "нож027", "name": "Нож туристический с ножнами", "size": "0", "planned_qty": 2000, "actual_qty": 1998},
                    {"sku_code": "нож028", "name": "Нож туристический складной", "size": "0", "planned_qty": 1920, "actual_qty": 1915},
                ],
            },
        )
        response = self.client.get(f"/client/api/v1/requests/receiving/215/?client={self.agency.id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(len(data["lines"]), 3)
        by_name = {row["name"]: row for row in data["lines"]}
        ready = by_name["Нож туристический с ножнами (Готовый)"]
        defect = by_name["Нож туристический с ножнами (Брак)"]
        ready2 = by_name["Нож туристический складной (Готовый)"]
        self.assertEqual(ready["qty_planned"], 2000)
        self.assertEqual(ready["qty_actual"], 1995)
        self.assertEqual(ready["box_qty"], 1)
        self.assertEqual(ready["delta_qty"], -5)
        self.assertEqual(defect["qty_planned"], 0)
        self.assertEqual(defect["qty_actual"], 3)
        self.assertEqual(defect["box_qty"], 1)
        self.assertEqual(defect["delta_qty"], 3)
        self.assertEqual(ready2["qty_planned"], 1920)
        self.assertEqual(ready2["qty_actual"], 1915)
        self.assertEqual(ready2["delta_qty"], -5)
        totals = {row["label"]: row["value"] for row in data["meta"] if str(row["label"]).startswith("Итого")}
        self.assertEqual(totals.get("Итого план"), "3920")
        self.assertEqual(totals.get("Итого факт"), "3913")
        self.assertEqual(totals.get("Итого коробов"), "3")
        self.assertEqual(totals.get("Итого расхождение"), "-7")

    def test_shipping_detail_uses_db_items(self):
        from shipping.models import ShippingOrder, ShippingOrderItem

        order = ShippingOrder.objects.create(
            number="SO-LK-214",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_SUBMITTED,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            marketplace=self.wb,
            destination_warehouse="Коледино",
            comment="Срочно",
        )
        ShippingOrderItem.objects.create(
            order=order,
            sku_code="SKU-S1",
            name="Отгрузка 1",
            size="42",
            qty_requested=12,
            qty_reserved=12,
            qty_shipped=0,
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="shipping",
            order_id=order.number,
            action="status",
            description="Создана отгрузка",
            payload={"status": "submitted", "status_label": "Новая"},
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="shipping",
            order_id=order.number,
            action="status",
            description="Упаковка закрыта",
            payload={
                "act": "shipping_packing",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BX-CLIENT-1",
                        "qty": 12,
                        "items": [
                            {
                                "sku_code": "SKU-S1",
                                "name": "Отгрузка 1",
                                "barcode": "2042187621577",
                                "qty": 12,
                            }
                        ],
                    }
                ],
                "act_pallets": [{"code": "PAL-CLIENT-1", "label": "1", "boxes": ["BX-CLIENT-1"]}],
            },
        )
        response = self.client.get(
            f"/client/api/v1/requests/shipping/{order.number}/?client={self.agency.id}"
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertFalse(data["actions"]["open_wms"])
        self.assertTrue(data["actions"]["can_export_excel"])
        self.assertEqual(len(data["lines"]), 1)
        self.assertEqual(data["lines"][0]["qty_requested"], 12)
        self.assertTrue(any(row["label"] == "Склад назначения" for row in data["meta"]))
        export = self.client.get(
            f"/client/api/v1/requests/shipping/{order.number}/export/?client={self.agency.id}"
        )
        self.assertEqual(export.status_code, 200)
        self.assertTrue(export.content[:2] == b"PK")
        workbook = load_workbook(BytesIO(export.content))
        rows = list(workbook.active.iter_rows(values_only=True))
        self.assertEqual(rows[0], ("Баркод товара", "Кол-во товаров", "ШК короба", "Срок годности"))
        self.assertEqual(rows[1], ("2042187621577", 12, "BX-CLIENT-1", None))
        self.assertEqual(workbook.active.max_column, 4)
        self.assertEqual(data["shipping_view"]["documents"][0]["title"], "Состав коробов (Excel)")
        self.assertFalse(
            any(doc.get("title") == "ШК / номер поставки" for doc in data["shipping_view"]["documents"])
        )
        # Phase A UI markup is always in the LK shell; shipping_view is asserted in unit tests
        # (local sqlite may drift on shipping.distribution_status vs models.py).
        page = self.client.get(f"/client/dashboard/lk/?client={self.agency.id}")
        self.assertIn("ship-scale", page.content.decode("utf-8"))

    def test_shipping_detail_canonicalizes_reopened_audit_id(self):
        from shipping.models import ShippingOrder, ShippingOrderItem

        order = ShippingOrder.objects.create(
            number="SO-LK-REOPEN",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_SHIPPED,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            marketplace=self.wb,
            destination_warehouse="Коледино",
        )
        ShippingOrderItem.objects.create(
            order=order,
            sku_code="SKU-REOPEN",
            name="Товар после переоткрытия",
            barcode="2040000000011",
            qty_requested=12,
            qty_reserved=12,
            qty_shipped=12,
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="shipping",
            order_id=f"{order.number}#reopened#5891",
            action="status",
            description="Старая служебная запись переоткрытия",
            payload={
                "shipping_state": "packed",
                "order_title": "Заявка на отгрузку",
                "items": [
                    {"sku_code": "SKU-REOPEN", "name": "Старый payload", "qty_requested": 12},
                ],
            },
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="shipping",
            order_id=order.number,
            action="status",
            description="Заявка отгружена",
            payload={"shipping_state": "shipped", "order_title": "Заявка на отгрузку"},
        )

        with patch("client_cabinet.web_ui._shipping_trip_status", return_value="completed"):
            response = self.client.get(
                f"/client/api/v1/requests/shipping/{order.number}%23reopened%235891/?client={self.agency.id}"
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["order_id"], order.number)
        self.assertEqual(data["number"], order.number)
        self.assertEqual(data["status_label"], "Закрыта")
        self.assertEqual(data["bucket"], "done")
        self.assertEqual(data["lines"][0]["sku_code"], "SKU-REOPEN")
        self.assertEqual(data["lines"][0]["qty_shipped"], 12)
        self.assertEqual(data["lk_url"], f"#/request/shipping/{order.number}")

    def test_shipping_detail_accepts_legacy_otg_display_number(self):
        from shipping.models import ShippingOrder, ShippingOrderItem

        order = ShippingOrder.objects.create(
            number="OTG-000136",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_SUBMITTED,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            marketplace=self.wb,
            destination_warehouse="Коледино",
            expected_boxes=1,
        )
        ShippingOrderItem.objects.create(
            order=order,
            sku_code="SKU-LEGACY-OTG",
            name="Товар из старого номера заявки",
            barcode="2040000000136",
            qty_requested=7,
            qty_reserved=7,
            qty_shipped=0,
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="shipping",
            order_id=order.number,
            action="status",
            description="Заявка отправлена клиентом",
            payload={"shipping_state": "submitted", "order_title": "Заявка на отгрузку"},
        )

        response = self.client.get(f"/client/api/v1/requests/shipping/136_OTG/?client={self.agency.id}")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["order_id"], order.number)
        self.assertEqual(data["number"], order.number)
        self.assertEqual(data["lk_url"], f"#/request/shipping/{order.number}")
        self.assertEqual(len(data["lines"]), 1)
        self.assertEqual(data["lines"][0]["sku_code"], "SKU-LEGACY-OTG")
        self.assertEqual(data["lines"][0]["qty_requested"], 7)
        self.assertEqual(data["shipping_view"]["lines"][0]["sku_code"], "SKU-LEGACY-OTG")

    def test_ozon_shipping_detail_shows_supply_and_gm_codes(self):
        from shipping.models import ShippingDestination, ShippingOrder, ShippingOrderItem

        order = ShippingOrder.objects.create(
            number="SO-OZON-GM",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_SUBMITTED,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            marketplace=self.ozon,
            destination_warehouse="МО_ЩЕРБИНКА_ХАБ",
            expected_boxes=2,
            shipping_barcode="GM-001",
            supply_number="2000059613908",
            comment="Пакет Ozon\nШК ГМ: GM-001, GM-002",
        )
        ShippingDestination.objects.create(
            order=order,
            warehouse_name="Пушкино",
            planned_boxes=2,
            planned_units=24,
            comment="ШК ГМ: GM-001, GM-002",
        )
        ShippingOrderItem.objects.create(
            order=order,
            sku_code="SKU-OZON",
            name="Ozon товар",
            barcode="2048056437522",
            qty_requested=24,
            qty_reserved=24,
            comment="Коробов: 2; кратность: 12; ШК ГМ Ozon: GM-001, GM-002",
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="shipping",
            order_id=order.number,
            action="status",
            description="Создана Ozon-отгрузка",
            payload={"status": "submitted", "status_label": "На согласовании менеджера"},
        )

        response = self.client.get(
            f"/client/api/v1/requests/shipping/{order.number}/?client={self.agency.id}"
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        meta = {row["label"]: row["value"] for row in data["meta"]}
        self.assertEqual(meta["Номер поставки Ozon"], "2000059613908")
        self.assertEqual(meta["ШК поставки"], "GM-001")
        self.assertIn("GM-002", meta["ШК ГМ Ozon"])
        self.assertIn("Пушкино", meta["Конечные склады Ozon"])
        column_labels = [col["label"] for col in data["columns"]]
        self.assertIn("ШК товара", column_labels)
        self.assertIn("ШК ГМ Ozon", column_labels)
        self.assertEqual(data["lines"][0]["barcode"], "2048056437522")
        self.assertIn("GM-001", data["lines"][0]["gm_barcodes"])

    def test_processing_detail_shows_stock_rows_and_discrepancies(self):
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="processing",
            order_id="77",
            action="status",
            description="Обработка",
            payload={
                "status": "in_progress",
                "status_label": "В работе",
                "product_name": "Худи",
                "article": "HD-1",
                "stock_rows": [
                    {"article": "HD-1", "size": "M", "barcode": "46001", "qty": 4},
                ],
                "discrepancy_detected": True,
                "discrepancy_items": [
                    {
                        "sku_code": "HD-1",
                        "size": "M",
                        "name": "Худи",
                        "expected_qty": 5,
                        "factual_qty": 4,
                        "delta_qty": -1,
                    }
                ],
            },
        )
        response = self.client.get(f"/client/api/v1/requests/processing/77/?client={self.agency.id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertFalse(data["actions"]["open_wms"])
        self.assertTrue(data["actions"]["can_export_excel"])
        self.assertEqual(data["lines"][0]["qty"], 4)
        self.assertTrue(data["has_mismatch"])
        self.assertEqual(data["discrepancies"][0]["delta_qty"], -1)
        export = self.client.get(f"/client/api/v1/requests/processing/77/export/?client={self.agency.id}")
        self.assertEqual(export.status_code, 200)
        self.assertTrue(export.content[:2] == b"PK")


class ClientLkHomeMetricsRegressionTests(TestCase):
    """Guardrails for home metrics: no archived stock, live WB/Ozon slots."""

    def setUp(self):
        self.user = User.objects.create_user(username="lk_home_metrics", password="pwd")
        self.agency = Agency.objects.create(agn_name="ЛК метрики регресс", portal_user=self.user)
        self.client = Client()
        self.client.force_login(self.user)
        self.wb = Market.objects.create(id=9401, name="WB")
        self.ozon = Market.objects.create(id=9402, name="OZON")
        SKU.objects.create(agency=self.agency, sku_code="SKU-ACTIVE", name="Активный остаток")
        SKU.objects.create(agency=self.agency, sku_code="SKU-ARCH", name="Архивный остаток")

        self.active = create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="RCV-ACTIVE",
            sku="SKU-ACTIVE",
            name="Активный остаток",
            qty=100,
            available_qty=100,
            box_code="BOX-ACTIVE-1",
            pallet_code="PAL-ACTIVE-1",
        )
        archived = create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="RCV-ARCH",
            sku="SKU-ARCH",
            name="Архивный остаток",
            qty=50,
            available_qty=50,
            box_code="BOX-ARCH-1",
            pallet_code="PAL-ARCH-1",
        )
        archived.is_archived = True
        archived.save(update_fields=["is_archived"])

        self.ready = create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="shipping",
            order_id="SO-READY",
            sku="SKU-ACTIVE",
            name="К отгрузке",
            qty=40,
            available_qty=0,
            box_code="BOX-READY-1",
            pallet_code="PAL-READY-1",
            zone="OTG",
            warehouse_state_code="in_otg",
        )
        self.processing = create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="processing",
            order_id="PROC-1",
            sku="SKU-ACTIVE",
            name="В обработке",
            qty=15,
            available_qty=0,
            box_code="BOX-PROC-1",
            pallet_code="PAL-PROC-1",
            zone="OBR",
            warehouse_state_code="processing_in_progress",
        )
        # Finished processing leftovers must NOT inflate "В обработке".
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="processing",
            order_id="PROC-DONE",
            sku="SKU-ACTIVE",
            name="Израсходовано обработкой",
            qty=99,
            available_qty=0,
            box_code="BOX-PROC-DONE",
            pallet_code="PAL-PROC-DONE",
            zone="OBR",
            warehouse_state_code="processing_consumed",
        )

        from django.utils import timezone
        from django.db import connection
        from shipping.models import ShippingOrder

        # Model lacks DB columns added by shipping migrations (supply_number, etc.).
        # Insert via SQL so ORM drift does not block LK home regression.
        today = timezone.localdate()
        now = timezone.now()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO shipping_shippingorder (
                    number, status, delivery_type, destination_address, comment,
                    created_at, updated_at, agency_id, created_by_id, marketplace_id,
                    slot_date, vehicle_type, wb_supply_barcode, wb_transit_warehouse,
                    driver_phone, expected_boxes, place_type, vehicle_number,
                    destination_warehouse, shipping_barcode, supply_type, transit_address,
                    shipping_discrepancy_payload, shipping_discrepancy_status,
                    distribution_status, supply_number
                ) VALUES (
                    %s, %s, %s, '', '',
                    %s, %s, %s, %s, %s,
                    %s, '', '', false,
                    '', 0, '', '',
                    '', '', '', '',
                    '{}'::jsonb, '',
                    '', ''
                )
                RETURNING id
                """,
                [
                    "SO-REG-073",
                    ShippingOrder.STATUS_PACKED,
                    ShippingOrder.DELIVERY_MARKETPLACE,
                    now,
                    now,
                    self.agency.id,
                    self.user.id,
                    self.wb.id,
                    today,
                ],
            )
            supply_id = cursor.fetchone()[0]
        self.supply = ShippingOrder.objects.get(pk=supply_id)

    def test_dashboard_lk_excludes_archived_stock_and_shows_wb_slot(self):
        page = self.client.get(f"/client/dashboard/lk/?client={self.agency.id}")
        self.assertEqual(page.status_code, 200)
        content = page.content.decode("utf-8")

        # Клиентский KPI остатков: только доступное.
        self.assertIn('id="kpi-stock-total">100 ед.', content)
        # Process stages show real warehouse units (not available stock).
        self.assertIn('id="metric-ready-ship">40 ед.', content)
        self.assertIn('id="metric-processing">15 ед.', content)
        self.assertNotIn('id="metric-processing">99 ед.', content)
        self.assertNotIn('id="metric-processing">114 ед.', content)
        self.assertIn("SO-REG-073", content)
        self.assertIn('id="metric-supply-wb"', content)
        wb_block = content.split('id="metric-supply-wb"', 1)[1].split('id="metric-supply-ozon"', 1)[0].lower()
        self.assertIn("so-reg-073", wb_block)
        self.assertNotIn("нет слота на сегодня", wb_block)
        oz_block = content.split('id="metric-supply-ozon"', 1)[1][:280].lower()
        self.assertIn("нет слота на сегодня", oz_block)

    def test_dashboard_api_matches_active_stock_and_live_slots(self):
        from client_cabinet.marketplace_lk import build_live_dashboard, _fullbox_qty_by_sku

        response = self.client.get(f"/client/api/v1/dashboard/?client={self.agency.id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["kpi"]["stock_units"], 100)
        self.assertEqual(data["stock"]["summary"]["total_units"], 100)
        self.assertEqual(data["stock"]["summary"]["sku_count"], 2)
        live = data["live_dashboard"]
        self.assertEqual(live["ready_for_shipping_units"], 40)
        self.assertEqual(live["in_processing_units"], 15)
        self.assertIn("SO-REG-073", live["supply_wb_label"])
        self.assertIn("нет слота", live["supply_ozon_label"].lower())
        self.assertEqual(live["stock"]["units"], 100)
        self.assertEqual(live["processing"]["units"], 15)
        self.assertEqual(live["ready_to_ship"]["units"], 40)

        # MP comparison qty also ignores archived rows
        self.assertEqual(_fullbox_qty_by_sku(self.agency).get("SKU-ACTIVE"), 100)
        self.assertNotIn("SKU-ARCH", _fullbox_qty_by_sku(self.agency))
        self.assertEqual(build_live_dashboard(self.agency)["supply_wb_label"], live["supply_wb_label"])
        self.assertEqual(build_live_dashboard(self.agency)["ready_for_shipping_units"], 40)
        self.assertEqual(build_live_dashboard(self.agency)["in_processing_units"], 15)

    def test_client_cabinet_stock_filters_always_exclude_archived(self):
        """Source guard: every WarehouseStockSnapshot filter in LK must exclude archived."""
        from pathlib import Path

        root = Path(__file__).resolve().parent
        offenders: list[str] = []
        for path in sorted(root.glob("*.py")):
            if path.name.startswith("test") or path.name.endswith(".bak"):
                continue
            text = path.read_text(encoding="utf-8")
            needle = "WarehouseStockSnapshot.objects"
            start = 0
            while True:
                idx = text.find(needle, start)
                if idx < 0:
                    break
                window = text[idx : idx + 280].replace("\n", " ")
                if "is_archived=False" not in window and "is_archived=True" not in window:
                    # allow select_related(...).filter( with is_archived later in the same chain
                    # look ahead a bit more for chained .filter(
                    wider = text[idx : idx + 500].replace("\n", " ")
                    if "is_archived=False" not in wider:
                        line = text.count("\n", 0, idx) + 1
                        offenders.append(f"{path.name}:{line}: {window[:120]}...")
                start = idx + len(needle)
        self.assertEqual(offenders, [], msg="LK stock queries missing is_archived filter:\n" + "\n".join(offenders))


class ClientLkStatusCorrectnessRegressionTests(TestCase):
    """Prod guardrails: finance act statuses and request status pills stay consistent."""

    def setUp(self):
        self.user = User.objects.create_user(username="lk_status_guard", password="pwd")
        self.agency = Agency.objects.create(agn_name="ЛК статусы регресс", portal_user=self.user)
        self.client = Client()
        self.client.force_login(self.user)

    def test_finance_act_status_helpers(self):
        from client_cabinet.finance_lk import (
            _receiving_act_status_from_payloads,
            _shipping_act_status,
        )
        from shipping.models import ShippingOrder

        self.assertEqual(
            _receiving_act_status_from_payloads([{"act_sent": "Акт приемки"}])[1],
            "Отправлен клиенту",
        )
        self.assertEqual(
            _receiving_act_status_from_payloads(
                [{"act_sent": "Акт", "act_viewed": True}]
            )[1],
            "Выполнена",
        )
        self.assertEqual(
            _receiving_act_status_from_payloads(
                [{"act_sent": "Акт", "act_client_response": "confirmed"}]
            )[1],
            "Подтверждён клиентом",
        )
        self.assertEqual(
            _receiving_act_status_from_payloads(
                [{"act_sent": "Акт", "act_client_response": "dispute"}]
            )[1],
            "Разногласия по акту",
        )

        packed = ShippingOrder(status=ShippingOrder.STATUS_PACKED)
        shipped = ShippingOrder(status=ShippingOrder.STATUS_SHIPPED)
        self.assertEqual(_shipping_act_status(packed, {})[1], "Готов к оформлению")
        self.assertEqual(_shipping_act_status(shipped, {})[1], "Акт не отправлен")
        self.assertEqual(
            _shipping_act_status(shipped, {"act_sent": True})[1],
            "Отправлен клиенту",
        )
        self.assertEqual(
            _shipping_act_status(packed, {"logistician_signed": True})[1],
            "Ожидает подписи менеджера",
        )
        for label in (
            _shipping_act_status(packed, {})[1],
            _shipping_act_status(shipped, {})[1],
            _shipping_act_status(shipped, {"act_sent": True})[1],
        ):
            self.assertNotIn(label, {"Упакована", "Отгружена", "Новая", "Отгружена частично"})

    def test_request_list_cancelled_and_completed_labels(self):
        from shipping.models import ShippingOrder

        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="receiving",
            order_id="rcv-done-1",
            action="status",
            description="Готово",
            payload={"status": "done", "status_label": "Закрыта складом"},
        )
        ShippingOrder.objects.create(
            agency=self.agency,
            created_by=self.user,
            number="SO-CANCEL-1",
            status=ShippingOrder.STATUS_CANCELED
            if hasattr(ShippingOrder, "STATUS_CANCELED")
            else "canceled",
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="shipping",
            order_id="SO-CANCEL-1",
            action="status",
            description="Отмена",
            payload={"status": "canceled", "status_label": "Отменена"},
        )
        response = self.client.get(f"/client/api/v1/dashboard/?client={self.agency.id}")
        self.assertEqual(response.status_code, 200)
        rows = {row["order_id"]: row for row in response.json()["data"]["requests"]}
        self.assertEqual(rows["rcv-done-1"]["status_label"], "Завершена")
        self.assertEqual(rows["rcv-done-1"]["status_pill"], "Завершена")
        self.assertEqual(rows["SO-CANCEL-1"]["status_label"], "ОТМЕНЕНА")
        self.assertEqual(rows["SO-CANCEL-1"]["status_pill"], "Отменена")

    def test_requests_list_exposes_safe_cancel_controls(self):
        from shipping.models import ShippingOrder

        ShippingOrder.objects.create(
            agency=self.agency,
            created_by=self.user,
            number="SO-WAIT-MANAGER-1",
            status=ShippingOrder.STATUS_SUBMITTED,
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="shipping",
            order_id="SO-WAIT-MANAGER-1",
            action="status",
            description="Отправлена менеджеру",
            payload={"status": "submitted", "status_label": "На согласовании менеджера"},
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="processing",
            order_id="draft-processing-1",
            action="draft",
            description="Черновик обработки",
            payload={"status": "draft", "status_label": "Черновик"},
        )

        response = self.client.get(f"/client/api/v1/requests/?client={self.agency.id}")
        self.assertEqual(response.status_code, 200, response.content)
        rows = {row["order_id"]: row for row in response.json()["data"]["requests"]}

        submitted = rows["SO-WAIT-MANAGER-1"]
        self.assertTrue(submitted["can_cancel"])
        self.assertEqual(submitted["cancel_policy"], "manager_approval")
        self.assertEqual(submitted["cancel_label"], "Запросить отмену")

        draft = rows["draft-processing-1"]
        self.assertTrue(draft["can_cancel"])
        self.assertEqual(draft["cancel_policy"], "direct")
        self.assertEqual(draft["cancel_label"], "Отменить заявку")
        self.assertEqual(draft["status_label"], "Черновик")
        self.assertEqual(draft["bucket"], "client")

    def test_list_and_detail_use_same_receiving_act_status(self):
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="receiving",
            order_id="RCV-ACT-WAIT-1",
            action="status",
            description="Акт отправлен клиенту",
            payload={
                "status": "done",
                "status_label": "Завершена",
                "act_sent": True,
                "act_manager_signed": True,
                "act_items": [{"sku_code": "SKU-ACT", "planned_qty": 5, "actual_qty": 5}],
            },
        )

        listing = self.client.get(f"/client/api/v1/requests/?client={self.agency.id}")
        detail = self.client.get(
            f"/client/api/v1/requests/receiving/RCV-ACT-WAIT-1/?client={self.agency.id}"
        )
        self.assertEqual(listing.status_code, 200, listing.content)
        self.assertEqual(detail.status_code, 200, detail.content)
        row = next(
            item
            for item in listing.json()["data"]["requests"]
            if item["order_id"] == "RCV-ACT-WAIT-1"
        )
        detail_data = detail.json()["data"]
        self.assertEqual(row["status_label"], "Ожидает подтверждения акта")
        self.assertEqual(row["status_label"], detail_data["status_label"])
        self.assertEqual(row["bucket"], detail_data["bucket"])
