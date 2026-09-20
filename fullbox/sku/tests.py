import json

from django.http import JsonResponse
from django.test import RequestFactory, TestCase, override_settings
from django.contrib.auth import get_user_model
from unittest.mock import patch

from employees.models import Employee
from sklad.models import WarehouseTemporaryNomenclature
from .models import Agency, Market, MarketCredential, SKU, SKUBarcode, abbreviate_agency_name
from .services import build_sku_duplicate_initial, suggest_sku_payload
from .views import SKUListView


class AgencyShortNameTests(TestCase):
    def test_abbreviates_ooo_in_short_name(self):
        agency = Agency.objects.create(agn_name='Общество с ограниченной ответственностью "Кейзи"')

        self.assertEqual(agency.short_name, 'ООО "Кейзи"')

    def test_abbreviates_ip_in_short_name(self):
        agency = Agency.objects.create(agn_name="Индивидуальный предприниматель Иванов Иван Иванович")

        self.assertEqual(agency.short_name, "ИП Иванов Иван Иванович")

    def test_helper_keeps_plain_name_unchanged(self):
        self.assertEqual(abbreviate_agency_name("Кейзи"), "Кейзи")


class SKUListViewTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_context_exposes_agency_options_and_selected_filter(self):
        agency_a = Agency.objects.create(agn_name="Клиент А")
        agency_b = Agency.objects.create(agn_name="Клиент Б")
        SKU.objects.create(sku_code="SKU-A", name="Товар А", agency=agency_a)
        SKU.objects.create(sku_code="SKU-B", name="Товар Б", agency=agency_b)

        request = self.factory.get("/sku/?view=table&agency=%s" % agency_b.id)
        response = SKUListView.as_view()(request)
        response.render()

        self.assertEqual(response.context_data["agency_filter"], str(agency_b.id))
        option_ids = [item.id for item in response.context_data["agency_options"]]
        self.assertIn(agency_a.id, option_ids)
        self.assertIn(agency_b.id, option_ids)
        self.assertContains(response, 'name="agency"')
        self.assertContains(response, "Все клиенты")

    def test_empty_state_is_shown_when_queryset_is_empty(self):
        request = self.factory.get("/sku/?view=table")
        response = SKUListView.as_view()(request)
        response.render()

        self.assertContains(response, "Активные SKU не найдены")
        self.assertContains(response, "Создать первый SKU")

    def test_table_view_renders_sku_rows_in_visible_layout(self):
        agency = Agency.objects.create(agn_name="Клиент А")
        SKU.objects.create(sku_code="SKU-A", name="Товар А", agency=agency, brand="Brand A")

        request = self.factory.get("/sku/?view=table")
        response = SKUListView.as_view()(request)
        response.render()

        self.assertContains(response, "SKU-A")
        self.assertContains(response, "Товар А")
        self.assertNotContains(response, "Активные SKU не найдены")

    @override_settings(WAREHOUSE_TEMP_NOMENCLATURE_ENABLED=True)
    def test_temporary_catalog_table_renders_temp_rows(self):
        agency = Agency.objects.create(agn_name="Клиент А")
        WarehouseTemporaryNomenclature.objects.create(
            agency=agency,
            identity_key="temp-028",
            item_code="028",
            name="Джинсы синие",
            size="27-35",
            barcode="TEMP-BC-028",
            first_context_type="receiving",
            first_context_id="81",
            last_context_type="receiving",
            last_context_id="81",
        )

        request = self.factory.get("/sku/?view=table&catalog=temporary")
        response = SKUListView.as_view()(request)
        response.render()

        self.assertContains(response, "Временная номенклатура")
        self.assertContains(response, "Временные позиции склада")
        self.assertContains(response, "028")
        self.assertContains(response, "Джинсы синие")
        self.assertContains(response, "TEMP-BC-028")
        self.assertNotContains(response, "Создать первый SKU")

    def test_temporary_catalog_request_falls_back_to_sku_mode_when_feature_disabled(self):
        agency = Agency.objects.create(agn_name="Клиент А")
        SKU.objects.create(sku_code="SKU-A", name="Товар А", agency=agency)
        WarehouseTemporaryNomenclature.objects.create(
            agency=agency,
            identity_key="temp-028-disabled",
            item_code="028",
            name="Джинсы синие",
            size="27-35",
            barcode="TEMP-BC-028",
        )

        request = self.factory.get("/sku/?view=table&catalog=temporary")
        response = SKUListView.as_view()(request)
        response.render()

        self.assertEqual(response.context_data["catalog_mode"], "sku")
        self.assertFalse(response.context_data["temporary_catalog_available"])
        self.assertContains(response, "SKU-A")
        self.assertNotContains(response, "Временная номенклатура")
        self.assertNotContains(response, "TEMP-BC-028")

    def test_table_view_renders_size_picker_and_barcodes_by_selected_size(self):
        agency = Agency.objects.create(agn_name="Клиент А")
        sku = SKU.objects.create(
            sku_code="SKU-A",
            name="Товар А",
            agency=agency,
            size="42",
        )
        SKUBarcode.objects.create(sku=sku, value="BC-42", size="42", is_primary=True)
        SKUBarcode.objects.create(sku=sku, value="BC-43", size="43", is_primary=False)

        request = self.factory.get("/sku/?view=table")
        response = SKUListView.as_view()(request)
        response.render()

        self.assertContains(response, 'data-sku-size-picker')
        self.assertContains(response, '<option value="42" selected>42</option>', html=True)
        self.assertContains(response, '<option value="43">43</option>', html=True)
        self.assertContains(
            response,
            '<span class="chip barcode-chip" data-barcode-chip data-barcode-value="BC-42" data-size="42">BC-42*</span>',
            html=True,
        )
        self.assertContains(
            response,
            '<span class="chip barcode-chip is-hidden" data-barcode-chip data-barcode-value="BC-43" data-size="43">BC-43</span>',
            html=True,
        )
        self.assertContains(
            response,
            '<span class="barcode-empty is-hidden" data-barcode-empty>Нет штрихкода для выбранного размера</span>',
            html=True,
        )

    @patch("sku.services.load_label_settings", return_value={"item": {}, "item_cz": {}})
    @patch("sku.services.load_available_printers_data", return_value=(["Zebra ZD421"], {"updated_at": "2026-04-04T09:00:00"}))
    def test_table_view_renders_print_action_and_printer_list(self, _printers_mock, _settings_mock):
        agency = Agency.objects.create(agn_name="Клиент А")
        sku = SKU.objects.create(sku_code="SKU-P", name="Товар Печать", agency=agency, brand="Brand P")
        SKUBarcode.objects.create(sku=sku, value="BC-P", size="42", is_primary=True)

        request = self.factory.get("/sku/?view=table")
        response = SKUListView.as_view()(request)
        response.render()

        self.assertContains(response, 'data-sku-print')
        self.assertContains(response, 'id="sku-print-printers"')
        self.assertContains(response, '<option value="Zebra ZD421"></option>', html=True)
        self.assertContains(response, 'id="label-settings-data"')

    def test_table_view_renders_desktop_direct_print_copies(self):
        agency = Agency.objects.create(agn_name="Клиент Desktop")
        sku = SKU.objects.create(sku_code="SKU-DESKTOP", name="Товар Desktop", agency=agency)
        SKUBarcode.objects.create(sku=sku, value="BC-DESKTOP", size="0", is_primary=True)

        request = self.factory.get("/sku/?view=table")
        response = SKUListView.as_view()(request)
        response.render()

        self.assertContains(response, '/orders/processing/print-jobs/desktop-record/', html=False)
        self.assertContains(response, 'window.fullboxDesktop.printPng', html=False)
        self.assertContains(response, 'copies: chunkCopies', html=False)

    def test_table_view_renders_marketplace_sync_button_in_toolbar(self):
        agency = Agency.objects.create(agn_name="Клиент А")
        SKU.objects.create(sku_code="SKU-SYNC", name="Товар Sync", agency=agency, brand="Brand Sync")

        request = self.factory.get("/sku/?view=table")
        response = SKUListView.as_view()(request)
        response.render()

        self.assertContains(response, 'href="/market-sync/"', html=False)
        self.assertContains(response, "Синхронизация маркетплейсов")
        self.assertNotContains(response, 'data-sku-market-sync', html=False)

    def test_table_view_renders_back_to_cabinet_link_for_employee(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(username="manager_sku_test", password="pwd")
        Employee.objects.create(full_name="Менеджер SKU", role="manager", user=user, is_active=True)
        agency = Agency.objects.create(agn_name="Клиент А")
        SKU.objects.create(sku_code="SKU-A", name="Товар А", agency=agency)

        request = self.factory.get("/sku/?view=table")
        request.user = user
        response = SKUListView.as_view()(request)
        response.render()

        self.assertContains(response, 'href="/team-manager/"')
        self.assertContains(response, "В кабинет")

    @patch("sku.views.run_wb_sync_request")
    def test_sync_marketplaces_endpoint_calls_existing_wb_sync_for_selected_sku(self, wb_sync_mock):
        agency = Agency.objects.create(agn_name="Клиент А")
        wb_market = Market.objects.create(id=1, name="WB")
        MarketCredential.objects.create(id=1, agency=agency, market=wb_market, market_key="wb-token")
        sku = SKU.objects.create(sku_code="SKU-SYNC-WB", name="Товар WB", agency=agency, market=wb_market)
        wb_sync_mock.return_value = JsonResponse(
            {
                "ok": True,
                "processed": 1,
                "created": 0,
                "updated": 1,
                "barcodes_created": 0,
                "errors": [],
            }
        )

        response = self.client.post(f"/sku/{sku.id}/sync-marketplaces/")
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        wb_sync_mock.assert_called_once()
        called_payload = json.loads(wb_sync_mock.call_args.kwargs["body"].decode("utf-8"))
        self.assertEqual(called_payload["client"], agency.id)
        self.assertEqual(called_payload["sku_code"], "SKU-SYNC-WB")

    @patch("sku.views.run_ozon_sync_request")
    def test_sync_marketplaces_endpoint_uses_binding_external_id_for_ozon(self, ozon_sync_mock):
        agency = Agency.objects.create(agn_name="Клиент OZON")
        ozon_market = Market.objects.create(id=2, name="OZON")
        MarketCredential.objects.create(
            id=2,
            agency=agency,
            market=ozon_market,
            market_key="ozon-token",
            client_id="12345",
        )
        sku = SKU.objects.create(sku_code="SKU-SYNC-OZ", name="Товар Ozon", agency=agency, market=ozon_market)
        sku.marketplace_bindings.create(marketplace="OZON", external_id="998877")
        ozon_sync_mock.return_value = JsonResponse(
            {
                "ok": True,
                "processed": 1,
                "created": 0,
                "updated": 1,
                "barcodes_created": 0,
                "errors": [],
            }
        )

        response = self.client.post(f"/sku/{sku.id}/sync-marketplaces/")
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        ozon_sync_mock.assert_called_once()
        called_payload = json.loads(ozon_sync_mock.call_args.kwargs["body"].decode("utf-8"))
        self.assertEqual(called_payload["client"], agency.id)
        self.assertEqual(called_payload["sku_code"], "SKU-SYNC-OZ")
        self.assertEqual(called_payload["external_id"], "998877")


class SKUServiceTests(TestCase):
    def test_suggest_sku_payload_searches_by_name_and_barcode(self):
        agency = Agency.objects.create(agn_name="Клиент А")
        sku = SKU.objects.create(sku_code="SKU-HELLO", name="Товар Hello", agency=agency)
        SKUBarcode.objects.create(sku=sku, value="BAR-123", size="", is_primary=True)

        by_name = suggest_sku_payload("Hello")
        by_barcode = suggest_sku_payload("BAR-123")

        self.assertEqual(by_name["items"][0]["value"], "SKU-HELLO")
        self.assertEqual(by_barcode["items"][0]["label"], "SKU-HELLO — Товар Hello")

    def test_build_sku_duplicate_initial_generates_next_available_code(self):
        agency = Agency.objects.create(agn_name="Клиент А")
        sku = SKU.objects.create(sku_code="SKU-BASE", name="Товар", agency=agency)
        SKU.objects.create(sku_code="SKU-BASE-copy", name="Товар копия", agency=agency)

        initial = build_sku_duplicate_initial(pk=sku.pk)

        self.assertEqual(initial["sku_code"], "SKU-BASE-copy1")
        self.assertEqual(initial["agency"], agency.id)
        self.assertFalse(initial["deleted"])

    @override_settings(WAREHOUSE_TEMP_NOMENCLATURE_ENABLED=True)
    def test_suggest_sku_payload_returns_temporary_items_for_temp_catalog(self):
        agency = Agency.objects.create(agn_name="Клиент А")
        WarehouseTemporaryNomenclature.objects.create(
            agency=agency,
            identity_key="temp-028",
            item_code="028",
            name="Джинсы синие",
            size="27-35",
            barcode="TEMP-BC-028",
        )

        payload = suggest_sku_payload("028", catalog_mode="temporary")

        self.assertEqual(payload["items"][0]["value"], "028")
        self.assertEqual(payload["items"][0]["label"], "028 · Джинсы синие · 27-35")
