import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, SimpleTestCase, TestCase

from audit.models import OrderAuditEntry
from employees.models import Employee
from marking.models import MarkingCode
from shipping.models import ShippingOrder, ShippingOrderItem
from sklad.services import WarehouseStateCode
from sklad.test_utils import create_warehouse_snapshot_row
from sku.models import Agency, Market, SKU
from todo.models import Task
from .services import (
    build_agency_form_context,
    build_client_cabinet_url,
    build_dashboard_context,
    build_client_list_context,
    build_client_list_queryset,
    build_client_sku_duplicate_initial,
    build_client_sku_form_context,
    build_client_sku_list_context,
    build_client_sku_list_queryset,
    build_client_receiving_form_context,
    build_marking_tools_context,
    fetch_party_by_inn_response,
    resolve_client_order_redirect_response,
    submit_client_packing_order,
    submit_client_receiving_order,
    toggle_agency_archive_response,
)
from .forms import AgencyForm
from .views import (
    ClientSKUListView,
    _inventory_check_for_agency,
    _lk_request_filter_status,
    _lk_request_status_pill,
    _lk_request_type_badge,
    _lk_request_type_key,
    _lk_request_type_label,
    _order_bucket,
    _order_status_label,
    _receiving_act_needs_client_attention,
    _repair_mojibake_text,
    dashboard,
    dashboard_lk,
    dashboard_new,
)


class ClientMarketplaceSyncGuardTests(SimpleTestCase):
    def test_same_client_cannot_start_parallel_marketplace_sync(self):
        from .marketplace_lk import marketplace_sync_lock, sync_marketplace

        agency = SimpleNamespace(id=987654)
        with marketplace_sync_lock(agency.id) as acquired:
            self.assertTrue(acquired)
            response = sync_marketplace(agency=agency, marketplace="ozon")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response["Retry-After"], "10")
        self.assertIn("уже выполняется", json.loads(response.content)["error"])

    def test_marketplace_sync_exception_returns_controlled_api_error(self):
        from . import api_views

        agency = SimpleNamespace(id=55)
        request = RequestFactory().post(
            "/client/api/v1/marketplace-sync/?client=55",
            data=json.dumps({"marketplace": "ozon"}),
            content_type="application/json",
        )
        request.user = SimpleNamespace(is_authenticated=True, username="client")
        with (
            patch.object(api_views, "_ctx", return_value=(agency, None, True)),
            patch.object(api_views, "sync_marketplace", side_effect=RuntimeError("external failure")),
        ):
            response = api_views.api_marketplace_sync(request)

        self.assertEqual(response.status_code, 502)
        payload = json.loads(response.content)
        self.assertFalse(payload["ok"])
        self.assertIn("временно недоступна", payload["error"])


class AgencyPasswordValidationTests(TestCase):
    def _form(self, password):
        return AgencyForm(
            data={
                "agn_name": "ООО Тест безопасности",
                "portal_login": "security_client",
                "portal_password": password,
                "inn": "7707083893",
                "phone": "+7 (999) 123-45-67",
                "fio_agn": "Иванов Иван Иванович",
                "email": "security-client@example.com",
            }
        )

    def test_common_password_is_rejected(self):
        form = self._form("password")
        form.is_valid()
        self.assertIn("portal_password", form.errors)

    def test_strong_password_passes_password_validators(self):
        form = self._form("N7!vQ2@pL9#xR4$k")
        form.is_valid()
        self.assertNotIn("portal_password", form.errors)


class ClientCabinetShippingStatusTests(SimpleTestCase):
    def test_shipping_submitted_status_is_shown_as_manager_review(self):
        entry = SimpleNamespace(
            order_type="shipping",
            payload={"shipping_state": "submitted"},
        )

        self.assertEqual(_order_status_label(entry), "На согласовании менеджера")
        self.assertEqual(_order_bucket(entry), "manager")

    def test_shipping_reserved_status_moves_to_warehouse_bucket(self):
        entry = SimpleNamespace(
            order_type="shipping",
            payload={"shipping_state": "reserved"},
        )

        self.assertEqual(_order_status_label(entry), "Согласована и передана в работу кладовщику")
        self.assertEqual(_order_bucket(entry), "warehouse")

    def test_shipping_storekeeper_accepted_status_is_shown_in_warehouse_bucket(self):
        entry = SimpleNamespace(
            order_type="shipping",
            payload={"shipping_state": "storekeeper_accepted"},
        )

        self.assertEqual(_order_status_label(entry), "Принята в работу складом")
        self.assertEqual(_order_bucket(entry), "warehouse")

    def test_shipping_packed_status_awaits_logistics(self):
        entry = SimpleNamespace(
            order_type="shipping",
            payload={"shipping_state": "packed"},
        )

        self.assertEqual(_order_status_label(entry), "Подготовлена складом, ожидает логиста")
        self.assertEqual(_order_bucket(entry), "warehouse")

    @patch("client_cabinet.web_ui._shipping_trip_status", return_value="departed")
    def test_shipping_departed_trip_is_shown_as_loaded_in_vehicle(self, _trip_status_mock):
        entry = SimpleNamespace(
            order_type="shipping",
            order_id="SO-000123",
            payload={"shipping_state": "packed"},
        )

        self.assertEqual(_order_status_label(entry), "Загружено в машину")
        self.assertEqual(_order_bucket(entry), "done")

    @patch("client_cabinet.web_ui._shipping_trip_status", return_value="completed")
    def test_shipping_completed_trip_is_shown_as_trip_finished(self, _trip_status_mock):
        entry = SimpleNamespace(
            order_type="shipping",
            order_id="SO-000124",
            payload={"shipping_state": "packed"},
        )

        self.assertEqual(_order_status_label(entry), "Рейс завершен")
        self.assertEqual(_order_bucket(entry), "done")

    def test_receiving_act_card_is_hidden_after_client_confirmation(self):
        act_entry = SimpleNamespace(payload={"act_sent": "Акт приемки"})
        status_entry = SimpleNamespace(payload={"act_client_response": "confirmed"})

        self.assertFalse(_receiving_act_needs_client_attention(act_entry, status_entry))

    def test_receiving_act_card_is_hidden_after_client_view(self):
        act_entry = SimpleNamespace(payload={"act_sent": "Акт приемки"})
        status_entry = SimpleNamespace(payload={"act_viewed": True})

        self.assertFalse(_receiving_act_needs_client_attention(act_entry, status_entry))


class ClientCabinetLkRequestStatusTests(SimpleTestCase):
    """Filter/pill/type status mapping for the LK «Мои заявки» list."""

    def test_filter_status_from_kanban_bucket(self):
        cases = [
            ("client", "Черновик", False, "waiting"),
            ("manager", "На согласовании менеджера", False, "waiting"),
            ("manager", "Ждет подтверждения", False, "waiting"),
            ("warehouse", "Принята в работу складом", False, "processing"),
            ("warehouse", "Подготовлена складом, ожидает логиста", False, "processing"),
            ("done", "Выполнена", False, "completed"),
            ("done", "Рейс завершен", False, "completed"),
            ("done", "Загружено в машину", False, "completed"),
            ("done", "ОТМЕНЕНА", False, "cancelled"),
            ("done", "Отменена", False, "cancelled"),
            ("manager", "Требует уточнения", False, "clarification"),
            ("client", "Акт отправлен клиенту", True, "clarification"),
        ]
        for bucket, label, attention, expected in cases:
            with self.subTest(bucket=bucket, label=label, attention=attention):
                self.assertEqual(
                    _lk_request_filter_status(
                        bucket=bucket,
                        status_label=label,
                        attention=attention,
                    ),
                    expected,
                )

    def test_status_pill_labels(self):
        self.assertEqual(_lk_request_status_pill("waiting"), "Ожидает")
        self.assertEqual(_lk_request_status_pill("processing"), "В обработке")
        self.assertEqual(_lk_request_status_pill("completed"), "Завершена")
        self.assertEqual(_lk_request_status_pill("clarification"), "Требует уточнения")
        self.assertEqual(_lk_request_status_pill("cancelled"), "Отменена")
        self.assertEqual(_lk_request_status_pill("unknown"), "Ожидает")

    def test_request_type_key_label_and_badge(self):
        self.assertEqual(_lk_request_type_key("shipping"), "shipping")
        self.assertEqual(_lk_request_type_key("packing"), "other")
        self.assertEqual(_lk_request_type_label("shipping"), "Отгрузка")
        self.assertEqual(_lk_request_type_label("receiving"), "Приёмка")
        self.assertEqual(_lk_request_type_label("processing"), "Обработка")
        self.assertEqual(_lk_request_type_label("packing"), "Обработка")
        self.assertEqual(_lk_request_type_label("other"), "Прочая")
        self.assertEqual(_lk_request_type_badge("shipping"), "OT")
        self.assertEqual(_lk_request_type_badge("receiving"), "ПР")
        self.assertEqual(_lk_request_type_badge("processing"), "ОБ")
        self.assertEqual(_lk_request_type_badge("packing"), "ОБ")
        self.assertEqual(_lk_request_type_badge("other"), "ДР")

    def test_shipping_pipeline_label_bucket_filter_and_pill(self):
        cases = [
            (
                {"shipping_state": "submitted"},
                None,
                "На согласовании менеджера",
                "manager",
                "waiting",
                "Ожидает",
            ),
            (
                {"shipping_state": "reserved"},
                None,
                "Согласована и передана в работу кладовщику",
                "warehouse",
                "processing",
                "В обработке",
            ),
            (
                {"shipping_state": "storekeeper_accepted"},
                None,
                "Принята в работу складом",
                "warehouse",
                "processing",
                "В обработке",
            ),
            (
                {"shipping_state": "picking"},
                None,
                "Доставка в зону отгрузки (ричтрак)",
                "warehouse",
                "processing",
                "В обработке",
            ),
            (
                {"shipping_state": "packed"},
                None,
                "Подготовлена складом, ожидает логиста",
                "warehouse",
                "processing",
                "В обработке",
            ),
            (
                {"shipping_state": "packed"},
                "departed",
                "Загружено в машину",
                "done",
                "completed",
                "Завершена",
            ),
            (
                {"shipping_state": "packed"},
                "completed",
                "Рейс завершен",
                "done",
                "completed",
                "Завершена",
            ),
            (
                {"shipping_state": "canceled"},
                None,
                "Отменена",
                "done",
                "cancelled",
                "Отменена",
            ),
        ]
        for payload, trip_status, label, bucket, filter_status, pill in cases:
            with self.subTest(payload=payload, trip=trip_status):
                entry = SimpleNamespace(
                    order_type="shipping",
                    order_id="SO-000200",
                    payload=payload,
                )
                patch_target = "client_cabinet.web_ui._shipping_trip_status"
                with patch(patch_target, return_value=trip_status or ""):
                    status_label = _order_status_label(entry)
                    status_bucket = _order_bucket(entry)
                self.assertEqual(status_label, label)
                self.assertEqual(status_bucket, bucket)
                mapped = _lk_request_filter_status(
                    bucket=status_bucket,
                    status_label=status_label,
                )
                self.assertEqual(mapped, filter_status)
                self.assertEqual(_lk_request_status_pill(mapped), pill)

    def test_receiving_pipeline_label_bucket_filter_and_pill(self):
        cases = [
            (
                {"status": "draft"},
                "Черновик",
                "client",
                "waiting",
                "Ожидает",
            ),
            (
                {"status": "sent_unconfirmed"},
                "Ждет подтверждения",
                "manager",
                "waiting",
                "Ожидает",
            ),
            (
                {"status": "warehouse", "status_label": "В ожидании поставки товара"},
                "В ожидании поставки товара",
                "warehouse",
                "processing",
                "В обработке",
            ),
            (
                {"status": "done"},
                "Выполнена",
                "done",
                "completed",
                "Завершена",
            ),
            (
                {"act_client_response": "confirmed"},
                "Выполнена",
                "done",
                "completed",
                "Завершена",
            ),
        ]
        for payload, label, bucket, filter_status, pill in cases:
            with self.subTest(payload=payload):
                entry = SimpleNamespace(order_type="receiving", payload=payload)
                status_label = _order_status_label(entry)
                status_bucket = _order_bucket(entry)
                self.assertEqual(status_label, label)
                self.assertEqual(status_bucket, bucket)
                mapped = _lk_request_filter_status(
                    bucket=status_bucket,
                    status_label=status_label,
                )
                self.assertEqual(mapped, filter_status)
                self.assertEqual(_lk_request_status_pill(mapped), pill)


class ClientRequestShippingKindTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="shipping_kind_client",
            password="pwd",
        )
        self.agency = Agency.objects.create(
            agn_name="Клиент типов отгрузки",
            portal_user=self.user,
        )

    def test_request_rows_expose_shipping_kind_labels(self):
        from client_cabinet.api_views import _build_request_payloads

        ozon = Market.objects.create(id=970001, name="Ozon")
        wb = Market.objects.create(id=970002, name="Wildberries")
        cases = (
            ("OTG-KIND-OZON", ShippingOrder.DELIVERY_MARKETPLACE, ozon, "Ozon"),
            ("OTG-KIND-WB", ShippingOrder.DELIVERY_MARKETPLACE, wb, "WB"),
            ("OTG-KIND-COURIER", ShippingOrder.DELIVERY_COURIER, None, "Курьер"),
            ("OTG-KIND-TRANSFER", ShippingOrder.DELIVERY_TRANSFER, None, "Перемещение"),
        )
        for number, delivery_type, marketplace, _expected in cases:
            ShippingOrder.objects.create(
                agency=self.agency,
                created_by=self.user,
                number=number,
                status=ShippingOrder.STATUS_SUBMITTED,
                delivery_type=delivery_type,
                marketplace=marketplace,
            )
            OrderAuditEntry.objects.create(
                agency=self.agency,
                user=self.user,
                order_type="shipping",
                order_id=number,
                action="status",
                description="Отправлена менеджеру",
                payload={"status": "submitted", "status_label": "На согласовании менеджера"},
            )

        rows = {row["order_id"]: row for row in _build_request_payloads(self.agency)}
        for number, _delivery_type, _marketplace, expected in cases:
            self.assertEqual(rows[number]["shipping_kind_label"], expected)

    def test_requests_template_renders_shipping_kind_label(self):
        template_path = Path(__file__).resolve().parent / "templates/client_cabinet/dashboard_lk_react.html"
        template_text = template_path.read_text(encoding="utf-8")
        self.assertIn("row.shipping_kind_label", template_text)
        self.assertIn('" · " + escapeHtml(row.shipping_kind_label)', template_text)
        self.assertIn('shipping_kind_label: row.shipping_kind_label || ""', template_text)

    def test_request_detail_redirects_expired_session_before_json_parse(self):
        template_path = Path(__file__).resolve().parent / "templates/client_cabinet/dashboard_lk_react.html"
        template_text = template_path.read_text(encoding="utf-8")
        self.assertIn("function requestDetailAuthExpired(response)", template_text)
        self.assertIn('responseUrl.indexOf("/login/") !== -1', template_text)
        self.assertIn("window.location.assign(requestDetailLoginUrl())", template_text)
        self.assertIn("var payload = await requestDetailJson(response);", template_text)
        self.assertNotIn("Unexpected token", template_text)


class ClientCabinetMojibakeRepairTests(SimpleTestCase):
    def test_repairs_pure_cp1251_mojibake(self):
        raw = "Р—Р°РґР°РЅРёРµ 1426 РІС‹РїРѕР»РЅРµРЅРѕ"
        self.assertEqual(_repair_mojibake_text(raw), "Задание 1426 выполнено")

    def test_repairs_mixed_mojibake_with_correct_cyrillic_tail(self):
        raw = "Р\x98Р·РјРµРЅРµРЅРѕ РјРµСЃС‚Рѕ РЅР°Р·РЅР°С‡РµРЅРёСЏ: OTG · Зона отгрузки"
        self.assertEqual(
            _repair_mojibake_text(raw),
            "Изменено место назначения: OTG · Зона отгрузки",
        )

    def test_repairs_mojibake_prefix_before_person_name(self):
        raw = (
            "РћС‚Р»РѕР¶РµРЅРЅРѕРµ РїРѕСЂСѓС‡РµРЅРёРµ РЅР° СѓРїР°РєРѕРІРєСѓ "
            "Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё РѕС‚РїСЂР°РІР»РµРЅРѕ: Красивская Ольга Алексеевна"
        )
        self.assertEqual(
            _repair_mojibake_text(raw),
            "Отложенное поручение на упаковку автоматически отправлено: Красивская Ольга Алексеевна",
        )


class ClientSKUListViewTests(TestCase):
    def test_defaults_to_table_view(self):
        user = get_user_model().objects.create_user(username="sku_client", password="pwd")
        agency = Agency.objects.create(agn_name="Клиент SKU", portal_user=user)
        request = RequestFactory().get(f"/client/{agency.pk}/sku/")
        request.user = user

        view = ClientSKUListView()
        view.request = request
        view.agency = agency
        view.kwargs = {}
        view.object_list = view.get_queryset()
        context = view.get_context_data()

        self.assertEqual(context["view_mode"], "table")


class ClientListServiceTests(TestCase):
    def test_build_client_list_queryset_applies_search_and_sort(self):
        staff_user = get_user_model().objects.create_user(username="staff_list", password="pwd", is_staff=True)
        Agency.objects.create(agn_name="Бета", inn="2")
        alpha = Agency.objects.create(agn_name="Альфа", inn="1")
        request = RequestFactory().get("/client/?q=льф&sort=id&dir=desc")
        request.user = staff_user

        queryset = build_client_list_queryset(
            request=request,
            base_queryset=Agency.objects.all(),
            sort_fields={"id": "id", "name": "agn_name"},
            filter_fields={"agn_name": "agn_name"},
            default_sort="name",
        )

        self.assertEqual(list(queryset), [alpha])

    def test_build_client_list_context_marks_active_sort(self):
        staff_user = get_user_model().objects.create_user(username="staff_ctx", password="pwd", is_staff=True)
        request = RequestFactory().get("/client/?sort=name&dir=asc")
        request.user = staff_user

        context = build_client_list_context(
            request=request,
            items=[],
            view_modes=("table", "cards"),
            sort_fields={"name": "agn_name", "id": "id"},
            default_sort="name",
        )

        self.assertEqual(context["view_mode"], "table")
        self.assertTrue(context["sort_info"]["name"]["active"])
        self.assertEqual(context["sort_info"]["name"]["next_dir"], "desc")


class ClientSKUListServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="sku_service_user", password="pwd")
        self.agency = Agency.objects.create(agn_name="Клиент SKU services", portal_user=self.user)
        self.factory = RequestFactory()

    def test_build_client_sku_list_queryset_filters_by_search(self):
        target = SKU.objects.create(agency=self.agency, sku_code="SKU-TARGET", name="Целевой товар")
        target.barcodes.create(value="200000000777", is_primary=True)
        SKU.objects.create(agency=self.agency, sku_code="SKU-OTHER", name="Другой товар")
        request = self.factory.get("/client/1/sku/?q=TARGET")
        request.user = self.user

        queryset = build_client_sku_list_queryset(request=request, agency=self.agency)

        self.assertEqual(list(queryset), [target])

    def test_build_client_sku_list_context_builds_filter_options_and_size_map(self):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-CTX",
            name="Контекстный товар",
            brand="Brand One",
            size="42",
        )
        sku.barcodes.create(value="200000000888", size="42", is_primary=True)
        request = self.factory.get("/client/1/sku/?filter_brand=Brand+One")
        request.user = self.user

        context = build_client_sku_list_context(
            request=request,
            agency=self.agency,
            items=[sku],
            view_modes=("table", "cards"),
        )

        self.assertEqual(context["filter_values"]["brand"], "Brand One")
        self.assertEqual(context["brand_options"], ["Brand One"])
        self.assertEqual(context["size_options"], ["42"])
        self.assertIn("200000000888", sku.size_map_json)


class ClientCabinetAdminServiceTests(TestCase):
    def setUp(self):
        self.staff_user = get_user_model().objects.create_user(
            username="client_admin_service",
            password="pwd",
            is_staff=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент админ сервис")
        self.factory = RequestFactory()

    def test_build_agency_form_context_sets_staff_cancel_url(self):
        request = self.factory.get("/client/new/")
        request.user = self.staff_user

        context = build_agency_form_context(
            request=request,
            mode="create",
            title="Создание клиента",
            submit_label="Создать",
            agency=self.agency,
        )

        self.assertEqual(context["cancel_url"], "/client/")
        self.assertTrue(context["staff_view"])
        self.assertEqual(context["card_title"], "Карточка клиента")
        self.assertEqual(context["cabinet_url"], f"/client/dashboard/lk/?client={self.agency.id}")

    def test_toggle_agency_archive_response_toggles_flag(self):
        request = self.factory.get(f"/client/{self.agency.pk}/archive/")
        request.user = self.staff_user

        response = toggle_agency_archive_response(request=request, pk=self.agency.pk)

        self.assertEqual(response.status_code, 302)
        self.agency.refresh_from_db()
        self.assertTrue(self.agency.archived)

    @patch("client_cabinet.services.fetch_party_by_inn", return_value={"inn": "123", "agn_name": "ООО Тест"})
    def test_fetch_party_by_inn_response_returns_payload(self, fetch_mock):
        request = self.factory.get("/client/fetch-by-inn/?inn=123")
        request.user = self.staff_user

        response = fetch_party_by_inn_response(request=request)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertEqual(payload["data"]["agn_name"], "ООО Тест")
        fetch_mock.assert_called_once_with("123")

    def test_build_client_sku_duplicate_initial_generates_copy_code(self):
        sku = SKU.objects.create(agency=self.agency, sku_code="SKU-DUP", name="Оригинал")
        SKU.objects.create(agency=self.agency, sku_code="SKU-DUP-copy", name="Копия")

        initial = build_client_sku_duplicate_initial(agency=self.agency, sku_id=sku.id)

        self.assertEqual(initial["agency"], self.agency.id)
        self.assertEqual(initial["sku_code"], "SKU-DUP-copy1")

    def test_build_client_sku_form_context_sets_client_view(self):
        context = build_client_sku_form_context(agency=self.agency)

        self.assertEqual(context["agency"], self.agency)
        self.assertTrue(context["client_view"])


class ClientInventoryCheckTests(TestCase):
    def test_inventory_check_matches_received_minus_shipped_to_stock(self):
        agency = Agency.objects.create(agn_name="Клиент 1")
        OrderAuditEntry.objects.create(
            order_id="rcv-1",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act": "receiving",
                "act_items": [
                    {"sku_code": "SKU-1", "actual_qty": 10},
                    {"sku_code": "SKU-2", "actual_qty": 5},
                ],
            },
        )
        order = ShippingOrder.objects.create(
            number="SO-000001",
            agency=agency,
            status=ShippingOrder.STATUS_SHIPPED,
        )
        ShippingOrderItem.objects.create(
            order=order,
            sku_code="SKU-1",
            name="Товар",
            qty_requested=4,
            qty_shipped=4,
        )
        create_warehouse_snapshot_row(
            agency=agency,
            order_type="receiving",
            order_id="rcv-1",
            sku="SKU-1",
            name="Товар",
            goods_type="Оптовый",
            qty=11,
        )

        result = _inventory_check_for_agency(agency)

        self.assertIsNotNone(result)
        self.assertEqual(result["received_total"], 15)
        self.assertEqual(result["shipped_total"], 4)
        self.assertEqual(result["expected_stock"], 11)
        self.assertEqual(result["stock_total"], 11)
        self.assertEqual(result["processing_in_progress_total"], 0)
        self.assertEqual(result["owned_total"], 11)
        self.assertEqual(result["discrepancy"], 0)
        self.assertEqual(result["status_tone"], "ok")

    def test_inventory_check_marks_discrepancy(self):
        agency = Agency.objects.create(agn_name="Клиент 2")
        OrderAuditEntry.objects.create(
            order_id="rcv-2",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act": "receiving",
                "act_items": [
                    {"sku_code": "SKU-1", "actual_qty": 12},
                ],
            },
        )
        create_warehouse_snapshot_row(
            agency=agency,
            order_type="receiving",
            order_id="rcv-2",
            sku="SKU-1",
            name="Товар",
            goods_type="Оптовый",
            qty=9,
        )

        result = _inventory_check_for_agency(agency)

        self.assertIsNotNone(result)
        self.assertEqual(result["expected_stock"], 12)
        self.assertEqual(result["stock_total"], 9)
        self.assertEqual(result["processing_in_progress_total"], 0)
        self.assertEqual(result["owned_total"], 9)
        self.assertEqual(result["discrepancy"], -3)
        self.assertEqual(result["status_tone"], "warn")

    def test_inventory_check_counts_goods_in_processing_as_ours(self):
        agency = Agency.objects.create(agn_name="Клиент 3")
        OrderAuditEntry.objects.create(
            order_id="rcv-3",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act": "receiving",
                "act_items": [
                    {"sku_code": "SKU-1", "actual_qty": 10},
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id="proc-3",
            order_type="processing",
            action="status",
            agency=agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "stock_rows": [
                    {"sku": "SKU-1", "size": "", "goods_type": "", "qty": 10},
                ],
            },
        )
        create_warehouse_snapshot_row(
            agency=agency,
            order_type="receiving",
            order_id="rcv-3",
            sku="SKU-1",
            name="Товар",
            goods_type="Оптовый",
            qty=2,
        )
        create_warehouse_snapshot_row(
            agency=agency,
            order_type="processing",
            order_id="proc-3",
            sku="SKU-1",
            name="Товар",
            goods_type="Оптовый",
            qty=8,
            available_qty=0,
            processing_reserved_qty=8,
            zone="OBR",
            row=0,
            section=0,
            tier=0,
            cell=0,
            warehouse_state_code=WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
        )

        result = _inventory_check_for_agency(agency)

        self.assertIsNotNone(result)
        self.assertEqual(result["expected_stock"], 10)
        self.assertEqual(result["stock_total"], 2)
        self.assertEqual(result["processing_in_progress_total"], 8)
        self.assertEqual(result["owned_total"], 10)
        self.assertEqual(result["discrepancy"], 0)
        self.assertEqual(result["status_tone"], "ok")

    def test_inventory_check_does_not_count_goods_already_moved_to_warehouse_as_processing(self):
        agency = Agency.objects.create(agn_name="Клиент 4")
        OrderAuditEntry.objects.create(
            order_id="rcv-4",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act": "receiving",
                "act_items": [
                    {"sku_code": "SKU-1", "actual_qty": 400},
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id="proc-4",
            order_type="processing",
            action="status",
            agency=agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "stock_rows": [
                    {"sku": "SKU-1", "size": "", "goods_type": "", "qty": 150},
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id="proc-4",
            order_type="processing",
            action="status",
            agency=agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BOX-1",
                        "items": [{"sku": "SKU-1", "qty": 150}],
                    }
                ],
                "act_pallets": [
                    {
                        "code": "PAL-1",
                        "boxes": ["BOX-1"],
                        "location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                    }
                ],
            },
        )
        create_warehouse_snapshot_row(
            agency=agency,
            order_type="receiving",
            order_id="rcv-4",
            sku="SKU-1",
            name="Товар",
            goods_type="Оптовый",
            qty=400,
        )

        result = _inventory_check_for_agency(agency)

        self.assertIsNotNone(result)
        self.assertEqual(result["expected_stock"], 400)
        self.assertEqual(result["stock_total"], 400)
        self.assertEqual(result["processing_in_progress_total"], 0)
        self.assertEqual(result["owned_total"], 400)
        self.assertEqual(result["discrepancy"], 0)
        self.assertEqual(result["status_tone"], "ok")

    def test_inventory_check_does_not_rebuild_stock_from_audit_when_stock_empty(self):
        agency = Agency.objects.create(agn_name="Клиент 5")
        OrderAuditEntry.objects.create(
            order_id="rcv-5",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act": "receiving",
                "act_items": [
                    {"sku_code": "SKU-1", "actual_qty": 12},
                ],
            },
        )

        result = _inventory_check_for_agency(agency)

        self.assertIsNotNone(result)
        self.assertEqual(result["expected_stock"], 12)
        self.assertEqual(result["stock_total"], 0)
        self.assertEqual(result["processing_in_progress_total"], 0)
        self.assertEqual(result["owned_total"], 0)
        self.assertEqual(result["discrepancy"], -12)


class ClientCabinetServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="cabinet_service_client", password="pwd")
        self.agency = Agency.objects.create(agn_name="Клиент сервисов", portal_user=self.user)
        self.factory = RequestFactory()

    def test_build_dashboard_context_adds_client_act_attention_card(self):
        OrderAuditEntry.objects.create(
            order_id="rcv-act-1",
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт отправлен клиенту",
            payload={"act_sent": "Акт приемки"},
        )
        request = self.factory.get(f"/client/dashboard/?client={self.agency.id}")
        request.user = self.user

        context = build_dashboard_context(
            request=request,
            selected_client=self.agency,
            client_view=True,
            run_inventory_check=False,
        )

        client_column = next(column for column in context["orders_panel_columns"] if column["status"] == "client")
        self.assertTrue(any(order.get("attention") for order in client_column["orders"]))
        attention_card = next(order for order in client_column["orders"] if order.get("attention"))
        self.assertIn("/orders/receiving/rcv-act-1/act/", attention_card["detail_url"])
        self.assertEqual(context["act_attention_count"], 1)
        self.assertIn("/orders/receiving/rcv-act-1/act/", context["act_attention_first_url"])

    def test_build_marking_tools_context_groups_codes_by_pallet(self):
        MarkingCode.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="proc-1",
            sku_code="SKU-1",
            barcode="200000000001",
            box_barcode="BOX-1",
            code="CZ-1",
        )
        MarkingCode.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="proc-1",
            sku_code="SKU-1",
            barcode="200000000001",
            box_barcode="BOX-1",
            code="CZ-2",
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="processing",
            order_id="proc-1",
            sku="SKU-1",
            name="Товар",
            barcode="200000000001",
            goods_type="gv",
            qty=2,
            box_code="BOX-1",
            pallet_code="PAL-1",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
        )

        context = build_marking_tools_context(selected_client=self.agency)

        self.assertEqual(context["total_count"], 2)
        self.assertEqual(len(context["pallet_rows"]), 1)
        self.assertEqual(context["pallet_rows"][0]["pallet_code"], "PAL-1")
        self.assertEqual(context["pallet_rows"][0]["cz_count"], 2)

    def test_resolve_client_order_redirect_response_returns_target_for_owned_agency(self):
        request = self.factory.get(f"/client/{self.agency.pk}/receiving/new/")
        request.user = self.user

        response = resolve_client_order_redirect_response(
            request=request,
            pk=self.agency.pk,
            destination="receiving",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/orders/receiving/?client={self.agency.pk}")

    def test_submit_client_receiving_order_creates_manager_task_for_non_draft(self):
        request = self.factory.post(
            f"/client/{self.agency.pk}/receiving/new/",
            data={
                "submit_action": "send",
                "eta_at": "2026-04-23T12:00",
                "expected_boxes": "3",
                "sku_code[]": ["SKU-1"],
                "sku_id[]": [""],
                "item_name[]": ["Товар 1"],
                "qty[]": ["7"],
                "position_comment[]": [""],
            },
        )
        request.user = self.user
        Employee.objects.create(full_name="Менеджер Клиента", role="manager", is_active=True)

        result = submit_client_receiving_order(request=request, agency=self.agency)

        self.assertTrue(result["redirect_to_dashboard"])
        entry = OrderAuditEntry.objects.get(order_type="receiving")
        self.assertEqual(entry.payload["status"], "sent_unconfirmed")
        self.assertEqual(Task.objects.filter(route=f"/orders/receiving/{entry.order_id}/").count(), 1)

    def test_submit_client_packing_order_logs_uploaded_file_names(self):
        request = self.factory.post(
            f"/client/{self.agency.pk}/packing/new/",
            data={
                "email": "client@example.com",
                "fio": "Иванов И.И.",
                "org": "Клиент",
            },
        )
        request.user = self.user

        result = submit_client_packing_order(request=request, agency=self.agency)

        self.assertTrue(result["submitted"])
        entry = OrderAuditEntry.objects.get(order_id=result["order_id"], order_type="packing")
        self.assertEqual(entry.payload["email"], "client@example.com")
        self.assertEqual(entry.payload["files_report"], [])

    def test_build_client_receiving_form_context_contains_sku_options(self):
        sku = SKU.objects.create(agency=self.agency, sku_code="SKU-CTX-1", name="Товар контекст")
        sku.barcodes.create(value="200000000123", is_primary=True)

        context = build_client_receiving_form_context(agency=self.agency, submitted=False)

        self.assertEqual(context["agency"], self.agency)
        self.assertEqual(context["sku_options"][0]["code"], "SKU-CTX-1")
        self.assertEqual(context["sku_options"][0]["barcodes_joined"], "200000000123")

    def test_build_client_cabinet_url_points_to_lk_by_default(self):
        self.assertEqual(build_client_cabinet_url(), "/client/dashboard/lk/")
        self.assertEqual(
            build_client_cabinet_url(self.agency.id),
            f"/client/dashboard/lk/?client={self.agency.id}",
        )
        self.assertEqual(
            build_client_cabinet_url(self.agency.id, legacy=True),
            f"/client/dashboard/lk/?client={self.agency.id}",
        )

    def test_dashboard_redirects_to_lk_unless_legacy(self):
        request = self.factory.get(f"/client/dashboard/?client={self.agency.id}&verify=1")
        request.user = self.user
        response = dashboard(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            f"/client/dashboard/lk/?client={self.agency.id}&verify=1",
        )

    def test_dashboard_legacy_redirects_to_lk(self):
        request = self.factory.get(f"/client/dashboard/?client={self.agency.id}&legacy=1")
        request.user = self.user
        response = dashboard(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/client/dashboard/lk/?client={self.agency.id}")

    def test_dashboard_new_redirects_to_lk(self):
        request = self.factory.get(f"/client/dashboard/new/?client={self.agency.id}")
        request.user = self.user
        response = dashboard_new(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/client/dashboard/lk/?client={self.agency.id}")

    def test_dashboard_lk_renders(self):
        request = self.factory.get(f"/client/dashboard/lk/?client={self.agency.id}")
        request.user = self.user
        response = dashboard_lk(request)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("Профиль компании", content)
        self.assertIn(f"/client/{self.agency.id}/edit/", content)
        self.assertIn(f"client={self.agency.id}", content)
        self.assertIn("Складские остатки", content)
        self.assertIn("stock-status-filters", content)
        self.assertIn("Скрыть списанные", content)
        self.assertIn('data-page="/other-requests"', content)
        self.assertIn('data-page="/marketplaces"', content)
        self.assertIn("/client/api/v1/other-requests/", content)


class ClientCabinetStockJournalTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="stock_lk_user", password="pwd")
        self.agency = Agency.objects.create(agn_name="Клиент остатки", portal_user=self.user)
        self.factory = RequestFactory()
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="pr-1",
            sku="SKU-STOCK",
            name="Товар на остатке",
            barcode="200000000111",
            goods_type="gv",
            qty=12,
            available_qty=12,
            box_code="BOX-STOCK-1",
            pallet_code="PAL-STOCK-1",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
        )

    def test_stock_page_groups_rows_by_product_barcode(self):
        template_path = Path(__file__).resolve().parent / "templates/client_cabinet/dashboard_lk_react.html"
        template_text = template_path.read_text(encoding="utf-8")

        self.assertIn('{ key: "barcode", label: "ШК товара"', template_text)
        self.assertIn('placeholder = "Поиск по ШК"', template_text)
        self.assertIn(".stock-table .stock-filter-row input", template_text)
        self.assertIn("function groupRowsByBarcode(source)", template_text)
        self.assertIn("target.available_qty += Number(row.available_qty || 0)", template_text)
        self.assertIn("rows = groupRowsByBarcode", template_text)
        self.assertNotIn('{ key: "box_code", label: "Короб"', template_text)

    def test_stock_journal_rows_expose_lk_columns(self):
        from client_cabinet.api_views import _stock_journal_rows

        rows = _stock_journal_rows(self.agency, hide_consumed=True)
        self.assertGreaterEqual(len(rows), 1)
        row = next(item for item in rows if item["box_code"] == "BOX-STOCK-1")
        self.assertEqual(row["sku"], "SKU-STOCK")
        self.assertEqual(row["goods_type"], "gv")
        self.assertEqual(row["qty"], 12)
        self.assertEqual(row["available_qty"], 12)
        self.assertIn("stock_main_qty", row)
        self.assertIn("stock_processing_qty", row)
        self.assertIn("stock_otg_qty", row)
        self.assertIn("processing_reserved_qty", row)
        self.assertIn("processing_in_progress_qty", row)
        self.assertIn("shipping_reserved_qty", row)
        self.assertTrue(row["when"])
        self.assertTrue(row["when_display"])

    def test_stock_journal_hides_zero_available_rows(self):
        from client_cabinet.api_views import _stock_journal_rows

        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="pr-zero",
            sku="SKU-ZERO",
            name="Пустой короб",
            goods_type="gv",
            qty=10,
            available_qty=0,
            box_code="BOX-ZERO-1",
            pallet_code="PAL-ZERO-1",
            zone="OS",
        )
        rows = _stock_journal_rows(self.agency, hide_consumed=True)
        self.assertTrue(any(item["box_code"] == "BOX-STOCK-1" for item in rows))
        self.assertFalse(any(item["box_code"] == "BOX-ZERO-1" for item in rows))
        self.assertTrue(all(int(item.get("available_qty") or 0) > 0 for item in rows))

    def test_lk_stock_rows_match_warehouse_availability(self):
        """LK stock journal must track the same warehouse truth as sklad."""
        from client_cabinet.api_views import _stock_journal_rows
        from sklad.models import WarehouseReserve
        from sklad.services import WarehouseStateCode
        from sklad.services.stock_availability import StockAvailabilityService
        from sklad.ui_services import _JOURNAL_HIDDEN_WAREHOUSE_STATES

        create_warehouse_snapshot_row(
            agency=self.agency,
            order_id="obr-1",
            sku="SKU-STOCK",
            name="Товар на остатке",
            barcode="200000000111",
            goods_type="gv",
            qty=5,
            available_qty=5,
            box_code="BOX-OBR-1",
            pallet_code="PAL-OBR-1",
            zone="OBR",
            row=2,
            section=1,
            tier=1,
            cell=1,
            warehouse_state_code=WarehouseStateCode.IN_PROCESSING_ZONE.value,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_id="otg-1",
            sku="SKU-STOCK",
            name="Товар на остатке",
            barcode="200000000111",
            goods_type="gv",
            qty=7,
            available_qty=7,
            box_code="BOX-OTG-1",
            pallet_code="PAL-OTG-1",
            zone="OTG",
            row=3,
            section=1,
            tier=1,
            cell=1,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_id="gone-1",
            sku="SKU-GONE",
            name="Списанный",
            barcode="200000000222",
            goods_type="gv",
            qty=0,
            available_qty=0,
            box_code="BOX-GONE-1",
            pallet_code="PAL-GONE-1",
            zone="OS",
            row=4,
            section=1,
            tier=1,
            cell=1,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_id="proc-hidden",
            sku="SKU-PROC",
            name="В обработке",
            barcode="200000000333",
            goods_type="gv",
            qty=4,
            available_qty=0,
            box_code="BOX-PROC-1",
            pallet_code="PAL-PROC-1",
            zone="OBR",
            row=5,
            section=1,
            tier=1,
            cell=1,
            warehouse_state_code=WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="SO-STOCK-1",
            sku_code="SKU-STOCK",
            size="",
            goods_type="gv",
            qty_reserved=3,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="PROC-STOCK-1",
            sku_code="SKU-STOCK",
            size="",
            goods_type="gv",
            qty_reserved=2,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        warehouse_rows = StockAvailabilityService.stock_rows_with_availability(agency=self.agency)
        expected = []
        for item in warehouse_rows:
            state_code = str(item.get("warehouse_state_code") or "").strip()
            if state_code in _JOURNAL_HIDDEN_WAREHOUSE_STATES:
                continue
            qty = int(item.get("qty") or 0)
            available_qty = int(item.get("available_qty") or 0)
            if qty <= 0 or available_qty <= 0:
                continue
            expected.append(item)

        lk_rows = _stock_journal_rows(self.agency, hide_consumed=True)
        self.assertEqual(len(lk_rows), len(expected))
        self.assertTrue(all(int(row.get("available_qty") or 0) > 0 for row in lk_rows))

        expected_by_box = {
            str(item.get("box_code") or item.get("container_code") or "").strip(): item
            for item in expected
        }
        self.assertIn("BOX-STOCK-1", expected_by_box)
        self.assertIn("BOX-OTG-1", expected_by_box)
        self.assertNotIn("BOX-GONE-1", {row["box_code"] for row in lk_rows})
        self.assertNotIn("BOX-PROC-1", {row["box_code"] for row in lk_rows})
        # in_processing_zone is also hidden for client-facing journals
        self.assertNotIn("BOX-OBR-1", {row["box_code"] for row in lk_rows})

        for lk_row in lk_rows:
            wh = expected_by_box[lk_row["box_code"]]
            self.assertEqual(lk_row["sku"], (wh.get("sku") or wh.get("sku_code") or "").strip())
            self.assertEqual(lk_row["name"], (wh.get("name") or "").strip() or "-")
            self.assertEqual(lk_row["goods_type"], (wh.get("goods_type") or "-").strip() or "-")
            self.assertEqual(lk_row["qty"], int(wh.get("qty") or 0))
            self.assertEqual(lk_row["available_qty"], int(wh.get("available_qty") or 0))
            self.assertEqual(lk_row["stock_main_qty"], int(wh.get("stock_main_qty") or 0))
            self.assertEqual(lk_row["stock_processing_qty"], int(wh.get("stock_processing_qty") or 0))
            self.assertEqual(lk_row["stock_otg_qty"], int(wh.get("stock_otg_qty") or 0))
            self.assertEqual(lk_row["processing_reserved_qty"], int(wh.get("processing_reserved_qty") or 0))
            self.assertEqual(lk_row["shipping_reserved_qty"], int(wh.get("shipping_reserved_qty") or 0))

        self.assertEqual(sum(row["qty"] for row in lk_rows), sum(int(item.get("qty") or 0) for item in expected))
        self.assertEqual(
            sum(row["available_qty"] for row in lk_rows),
            sum(int(item.get("available_qty") or 0) for item in expected),
        )
        self.assertEqual(
            sum(row["stock_main_qty"] for row in lk_rows),
            sum(int(item.get("stock_main_qty") or 0) for item in expected),
        )
        self.assertEqual(
            sum(row["stock_otg_qty"] for row in lk_rows),
            sum(int(item.get("stock_otg_qty") or 0) for item in expected),
        )

        os_row = next(row for row in lk_rows if row["box_code"] == "BOX-STOCK-1")
        otg_row = next(row for row in lk_rows if row["box_code"] == "BOX-OTG-1")
        self.assertEqual(os_row["stock_main_qty"], 12)
        self.assertEqual(os_row["stock_otg_qty"], 0)
        self.assertEqual(otg_row["stock_otg_qty"], 7)
        self.assertEqual(otg_row["stock_main_qty"], 0)
        self.assertEqual(os_row["processing_reserved_qty"], 2)
        self.assertEqual(os_row["shipping_reserved_qty"], 3)

    def test_stock_journal_api_matches_helper_and_warehouse(self):
        from client_cabinet.api_views import _stock_journal_rows, api_stock_journal
        from sklad.services.stock_availability import StockAvailabilityService
        from sklad.ui_services import _JOURNAL_HIDDEN_WAREHOUSE_STATES

        create_warehouse_snapshot_row(
            agency=self.agency,
            order_id="otg-api",
            sku="SKU-API",
            name="Товар API",
            barcode="200000000444",
            goods_type="gv",
            qty=9,
            available_qty=9,
            box_code="BOX-API-OTG",
            pallet_code="PAL-API-OTG",
            zone="OTG",
            row=6,
            section=1,
            tier=1,
            cell=1,
        )

        request = self.factory.get(f"/client/api/v1/stock-journal/?client={self.agency.id}&hide_consumed=1")
        request.user = self.user
        response = api_stock_journal(request)
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertTrue(payload.get("ok"))
        api_rows = payload["data"]["rows"]
        helper_rows = _stock_journal_rows(self.agency, hide_consumed=True)

        self.assertEqual(len(api_rows), len(helper_rows))
        api_by_box = {row["box_code"]: row for row in api_rows}
        helper_by_box = {row["box_code"]: row for row in helper_rows}
        self.assertEqual(set(api_by_box), set(helper_by_box))
        for box_code, api_row in api_by_box.items():
            helper_row = helper_by_box[box_code]
            for key in (
                "sku",
                "qty",
                "available_qty",
                "stock_main_qty",
                "stock_processing_qty",
                "stock_otg_qty",
                "processing_reserved_qty",
                "shipping_reserved_qty",
            ):
                self.assertEqual(api_row[key], helper_row[key], msg=f"{box_code}.{key}")

        warehouse_qty = sum(
            int(item.get("qty") or 0)
            for item in StockAvailabilityService.stock_rows_with_availability(agency=self.agency)
            if str(item.get("warehouse_state_code") or "").strip() not in _JOURNAL_HIDDEN_WAREHOUSE_STATES
            and int(item.get("qty") or 0) > 0
        )
        self.assertEqual(payload["data"]["summary"]["total_qty"], warehouse_qty)
        self.assertEqual(payload["data"]["summary"]["total_qty"], sum(row["qty"] for row in api_rows))
        self.assertIn("BOX-API-OTG", api_by_box)
        self.assertEqual(api_by_box["BOX-API-OTG"]["stock_otg_qty"], 9)
