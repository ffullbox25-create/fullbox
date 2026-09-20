import json
from unittest.mock import Mock, patch

from django.test import TestCase
from django.http import JsonResponse
from django.utils import timezone

from audit.models import AuditEntry
from sku.models import Agency, Market, MarketCredential, MarketplaceBinding, SKU, SKUPhoto

from .models import MarketSyncReport
from .services import (
    build_dashboard_context,
    build_report_detail_response,
    prepare_ozon_settings_page,
    prepare_wb_settings_page,
    submit_ozon_settings,
    submit_wb_settings,
)
from .sync_services import run_ozon_sync_request, run_wb_sync_request


class MarketSyncServiceTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Клиент market sync")
        self.wb_market = Market.objects.create(id=1, name="WB")
        self.ozon_market = Market.objects.create(id=2, name="OZON")

    def test_build_dashboard_context_marks_configured_marketplaces(self):
        MarketCredential.objects.create(
            id=1,
            agency=self.agency,
            market=self.wb_market,
            market_key="wb-token",
        )
        MarketCredential.objects.create(
            id=2,
            agency=self.agency,
            market=self.ozon_market,
            market_key="ozon-token",
            client_id="12345",
        )
        wb_report = MarketSyncReport.objects.create(
            agency=self.agency,
            marketplace="WB",
            status="ok",
            started_at=timezone.now(),
            finished_at=timezone.now(),
            duration_sec=3,
            processed=10,
            created=4,
            updated=6,
            barcodes_created=2,
            errors=[],
        )

        context = build_dashboard_context(client_id=self.agency.id)

        self.assertEqual(context["selected_client"], self.agency)
        self.assertTrue(context["wb_configured"])
        self.assertTrue(context["ozon_configured"])
        self.assertEqual(context["wb_report"], wb_report)
        self.assertEqual(context["marketplaces"][0]["settings_url"], f"/market-sync/wb/?client={self.agency.id}")

    def test_build_dashboard_context_exposes_client_selector_options(self):
        second_agency = Agency.objects.create(agn_name="Клиент B")

        context = build_dashboard_context(client_id=None)

        option_ids = [item.id for item in context["agency_options"]]
        self.assertIn(self.agency.id, option_ids)
        self.assertIn(second_agency.id, option_ids)
        self.assertEqual(context["selected_client_id"], "")

    def test_dashboard_renders_global_sync_button_and_wide_layout(self):
        response = self.client.get("/market-sync/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="sync-run-global"', html=False)
        self.assertContains(response, "Синхронизировать всех")
        self.assertContains(response, "max-width: 1680px", html=False)

    def test_prepare_wb_settings_page_returns_missing_market_context(self):
        Market.objects.filter(pk=self.wb_market.pk).delete()

        response, context = prepare_wb_settings_page(client_id=self.agency.id)

        self.assertIsNone(response)
        self.assertTrue(context["market_missing"])
        self.assertEqual(context["selected_client"], self.agency)

    def test_submit_wb_settings_creates_credential_and_redirects(self):
        response, context = submit_wb_settings(
            client_id=self.agency.id,
            post_data={"client": str(self.agency.id), "market_key": " new-token "},
        )

        self.assertIsNone(context)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/market-sync/?client={self.agency.id}")
        credential = MarketCredential.objects.get(agency=self.agency, market=self.wb_market)
        self.assertEqual(credential.market_key, "new-token")

    def test_submit_ozon_settings_returns_form_errors_for_invalid_client_id(self):
        response, context = submit_ozon_settings(
            client_id=self.agency.id,
            post_data={
                "client": str(self.agency.id),
                "client_id": "abc",
                "market_key": "token",
            },
        )

        self.assertIsNone(response)
        self.assertFalse(context["market_missing"])
        self.assertIn("client_id", context["form"].errors)

    def test_build_report_detail_response_serializes_report(self):
        report = MarketSyncReport.objects.create(
            agency=self.agency,
            marketplace="OZON",
            status="error",
            started_at=timezone.now(),
            finished_at=timezone.now(),
            duration_sec=7,
            processed=15,
            created=5,
            updated=10,
            barcodes_created=1,
            errors=["Ошибка API"],
        )

        response = build_report_detail_response(report_id=report.id)
        payload = json.loads(response.content)

        self.assertEqual(payload["marketplace"], "OZON")
        self.assertEqual(payload["agency"]["id"], self.agency.id)
        self.assertEqual(payload["errors"], ["Ошибка API"])

    def test_run_wb_sync_request_requires_client(self):
        response = run_wb_sync_request(body=b"{}")
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("Не указан клиент.", payload["errors"])

    def test_run_wb_sync_request_requires_token(self):
        response = run_wb_sync_request(body=json.dumps({"client": self.agency.id}).encode("utf-8"))
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("Не указан токен WB.", payload["errors"])

    @patch("market_sync.sync_services.marketplace_request")
    def test_run_wb_sync_request_reads_weight_fields_from_wb_card(self, post_mock):
        MarketCredential.objects.create(
            id=10,
            agency=self.agency,
            market=self.wb_market,
            market_key="wb-token",
        )

        first_response = Mock()
        first_response.status_code = 200
        first_response.json.return_value = {
            "cards": [
                {
                    "vendorCode": "SKU-WB-WEIGHT",
                    "nmID": 123456,
                    "title": "WB товар с весом",
                    "brand": "Brand WB",
                    "dimensions": {
                        "length": 10,
                        "width": 20,
                        "height": 30,
                        "weightBrutto": "1.75 кг",
                    },
                    "weightNetto": "1.20 кг",
                }
            ],
            "cursor": {},
        }
        second_response = Mock()
        second_response.status_code = 200
        second_response.json.return_value = {"cards": [], "cursor": {}}
        post_mock.side_effect = [first_response, second_response]

        response = run_wb_sync_request(body=json.dumps({"client": self.agency.id}).encode("utf-8"))
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        sku = SKU.objects.get(agency=self.agency, sku_code="SKU-WB-WEIGHT")
        self.assertEqual(str(sku.weight_kg), "1.750")
        self.assertEqual(str(sku.weight_gross_kg), "1.750")
        self.assertEqual(str(sku.weight_net_kg), "1.200")

    @patch("market_sync.sync_services.marketplace_request")
    def test_run_wb_sync_request_updates_only_logistics_parameters_by_default(self, post_mock):
        MarketCredential.objects.create(
            id=11,
            agency=self.agency,
            market=self.wb_market,
            market_key="wb-token",
        )
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-WB-MANUAL",
            name="Ручное имя",
            brand="Ручной бренд",
            weight_kg="0.300",
            weight_net_kg="0.250",
            weight_gross_kg="0.300",
            length_mm="100.0",
            width_mm="200.0",
            height_mm="300.0",
            source="manual",
        )

        first_response = Mock()
        first_response.status_code = 200
        first_response.json.return_value = {
            "cards": [
                {
                    "vendorCode": "SKU-WB-MANUAL",
                    "nmID": 9001,
                    "title": "Имя из WB",
                    "brand": "Brand WB",
                    "dimensions": {
                        "length": 40,
                        "width": 50,
                        "height": 60,
                        "weightBrutto": "1.75 кг",
                    },
                    "weightNetto": "1.20 кг",
                }
            ],
            "cursor": {},
        }
        second_response = Mock()
        second_response.status_code = 200
        second_response.json.return_value = {"cards": [], "cursor": {}}
        post_mock.side_effect = [first_response, second_response]

        response = run_wb_sync_request(body=json.dumps({"client": self.agency.id}).encode("utf-8"))
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        sku.refresh_from_db()
        self.assertEqual(sku.name, "Ручное имя")
        self.assertEqual(sku.brand, "Ручной бренд")
        self.assertEqual(str(sku.weight_kg), "1.750")
        self.assertEqual(str(sku.weight_net_kg), "1.200")
        self.assertEqual(str(sku.weight_gross_kg), "1.750")
        self.assertEqual(str(sku.length_mm), "400.0")
        self.assertEqual(str(sku.width_mm), "500.0")
        self.assertEqual(str(sku.height_mm), "600.0")
        self.assertEqual(sku.source, "manual")
        binding = MarketplaceBinding.objects.get(marketplace="WB", external_id="9001")
        self.assertEqual(binding.sku_id, sku.id)
        self.assertEqual(binding.sync_mode, "readonly")
        audit_entry = AuditEntry.objects.get(sku=sku, action="update")
        self.assertIsNone(audit_entry.user)
        self.assertIn("Синхронизация WB", audit_entry.description)
        changes = {
            row["field"]: row
            for row in audit_entry.snapshot["_audit"]["changes"]
        }
        self.assertEqual(changes["length_mm"]["before_display"], "100.0 мм")
        self.assertEqual(changes["length_mm"]["after_display"], "400 мм")
        self.assertEqual(changes["weight_kg"]["before_display"], "0.300 кг")
        self.assertEqual(changes["weight_kg"]["after_display"], "1.75 кг")

    @patch("market_sync.sync_services.marketplace_request")
    def test_run_wb_sync_request_can_filter_single_sku_by_code(self, post_mock):
        MarketCredential.objects.create(
            id=13,
            agency=self.agency,
            market=self.wb_market,
            market_key="wb-token",
        )

        first_response = Mock()
        first_response.status_code = 200
        first_response.json.return_value = {
            "cards": [
                {
                    "vendorCode": "SKU-WB-ONLY",
                    "nmID": 9101,
                    "title": "Нужный товар",
                    "brand": "Brand A",
                },
                {
                    "vendorCode": "SKU-WB-SKIP",
                    "nmID": 9102,
                    "title": "Лишний товар",
                    "brand": "Brand B",
                },
            ],
            "cursor": {},
        }
        second_response = Mock()
        second_response.status_code = 200
        second_response.json.return_value = {"cards": [], "cursor": {}}
        post_mock.side_effect = [first_response, second_response]

        response = run_wb_sync_request(
            body=json.dumps({"client": self.agency.id, "sku_code": "SKU-WB-ONLY"}).encode("utf-8")
        )
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["processed"], 1)
        self.assertTrue(SKU.objects.filter(agency=self.agency, sku_code="SKU-WB-ONLY").exists())
        self.assertFalse(SKU.objects.filter(agency=self.agency, sku_code="SKU-WB-SKIP").exists())

    @patch("market_sync.sync_services.marketplace_request")
    def test_run_wb_sync_request_overwrite_mode_still_preserves_non_parameter_fields(self, post_mock):
        MarketCredential.objects.create(
            id=12,
            agency=self.agency,
            market=self.wb_market,
            market_key="wb-token",
        )
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-WB-OVERWRITE",
            name="Старое имя",
            brand="Старый бренд",
            weight_kg="0.300",
            length_mm="100.0",
            width_mm="200.0",
            height_mm="300.0",
            source="marketplace",
            market=self.wb_market,
        )
        MarketplaceBinding.objects.create(
            sku=sku,
            marketplace="WB",
            external_id="9002",
            sync_mode="overwrite",
        )

        first_response = Mock()
        first_response.status_code = 200
        first_response.json.return_value = {
            "cards": [
                {
                    "vendorCode": "SKU-WB-OVERWRITE",
                    "nmID": 9002,
                    "title": "Новое имя из WB",
                    "brand": "Новый бренд",
                    "dimensions": {
                        "length": 40,
                        "width": 50,
                        "height": 60,
                        "weightBrutto": "1.75 кг",
                    },
                }
            ],
            "cursor": {},
        }
        second_response = Mock()
        second_response.status_code = 200
        second_response.json.return_value = {"cards": [], "cursor": {}}
        post_mock.side_effect = [first_response, second_response]

        response = run_wb_sync_request(body=json.dumps({"client": self.agency.id}).encode("utf-8"))
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        sku.refresh_from_db()
        self.assertEqual(sku.name, "Старое имя")
        self.assertEqual(sku.brand, "Старый бренд")
        self.assertEqual(str(sku.weight_kg), "1.750")
        self.assertEqual(str(sku.length_mm), "400.0")
        self.assertEqual(str(sku.width_mm), "500.0")
        self.assertEqual(str(sku.height_mm), "600.0")

    @patch("market_sync.web_ui._ozon_post")
    def test_run_ozon_sync_request_updates_only_logistics_parameters(self, post_mock):
        MarketCredential.objects.create(
            id=16,
            agency=self.agency,
            market=self.ozon_market,
            market_key="ozon-token",
            client_id="12345",
        )
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-OZON-PARAMS",
            name="Старое имя",
            brand="Старый бренд",
            weight_kg="0.300",
            weight_gross_kg="0.300",
            length_mm="100.0",
            width_mm="200.0",
            height_mm="300.0",
            source="manual",
        )
        MarketplaceBinding.objects.create(
            sku=sku,
            marketplace="OZON",
            external_id="7001",
            sync_mode="readonly",
        )
        post_mock.side_effect = [
            (
                {
                    "result": {
                        "items": [{"product_id": 7001, "offer_id": "SKU-OZON-PARAMS"}],
                        "last_id": "",
                    }
                },
                None,
            ),
            ({"result": {"items": [], "last_id": ""}}, None),
            (
                {
                    "items": [
                        {
                            "product_id": 7001,
                            "offer_id": "SKU-OZON-PARAMS",
                            "name": "Новое имя Ozon",
                            "brand": "Новый бренд Ozon",
                            "weight": 1750,
                            "depth": 400,
                            "width": 500,
                            "height": 600,
                        }
                    ]
                },
                None,
            ),
            ({"result": []}, None),
        ]

        response = run_ozon_sync_request(
            body=json.dumps({"client": self.agency.id}).encode("utf-8")
        )
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        sku.refresh_from_db()
        self.assertEqual(sku.name, "Старое имя")
        self.assertEqual(sku.brand, "Старый бренд")
        self.assertEqual(str(sku.weight_kg), "1.750")
        self.assertEqual(str(sku.weight_gross_kg), "1.750")
        self.assertEqual(str(sku.length_mm), "400.0")
        self.assertEqual(str(sku.width_mm), "500.0")
        self.assertEqual(str(sku.height_mm), "600.0")
        self.assertEqual(sku.source, "manual")

    @patch("market_sync.web_ui._ozon_post")
    def test_run_ozon_sync_request_reads_dimensions_from_attributes_item(self, post_mock):
        MarketCredential.objects.create(
            id=18,
            agency=self.agency,
            market=self.ozon_market,
            market_key="ozon-token",
            client_id="12345",
        )
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-OZON-ATTR-DIMS",
            name="Товар без габаритов",
            source="marketplace",
        )
        MarketplaceBinding.objects.create(
            sku=sku,
            marketplace="OZON",
            external_id="7003",
            sync_mode="readonly",
        )
        post_mock.side_effect = [
            (
                {
                    "result": {
                        "items": [{"product_id": 7003, "offer_id": "SKU-OZON-ATTR-DIMS"}],
                        "last_id": "",
                    }
                },
                None,
            ),
            ({"result": {"items": [], "last_id": ""}}, None),
            (
                {
                    "items": [
                        {
                            "id": 7003,
                            "offer_id": "SKU-OZON-ATTR-DIMS",
                            "name": "Карточка Ozon",
                        }
                    ]
                },
                None,
            ),
            (
                {
                    "result": [
                        {
                            "id": 7003,
                            "offer_id": "SKU-OZON-ATTR-DIMS",
                            "weight": 71,
                            "weight_unit": "g",
                            "depth": 230,
                            "width": 100,
                            "height": 20,
                            "dimension_unit": "mm",
                            "attributes": [],
                        }
                    ]
                },
                None,
            ),
        ]

        response = run_ozon_sync_request(
            body=json.dumps({"client": self.agency.id}).encode("utf-8")
        )

        self.assertEqual(response.status_code, 200)
        sku.refresh_from_db()
        self.assertEqual(str(sku.weight_kg), "0.071")
        self.assertEqual(str(sku.weight_gross_kg), "0.071")
        self.assertEqual(str(sku.length_mm), "230.0")
        self.assertEqual(str(sku.width_mm), "100.0")
        self.assertEqual(str(sku.height_mm), "20.0")

    @patch("market_sync.web_ui._ozon_post")
    def test_run_ozon_sync_request_extracts_url_from_primary_image_list(self, post_mock):
        MarketCredential.objects.create(
            id=17,
            agency=self.agency,
            market=self.ozon_market,
            market_key="ozon-token",
            client_id="12345",
        )
        first_url = "https://ir.ozone.ru/s3/multimedia-1-b/9401948003.jpg"
        second_url = "https://ir.ozone.ru/s3/multimedia-2-b/9401948003.jpg"
        post_mock.side_effect = [
            (
                {
                    "result": {
                        "items": [{"product_id": 7002, "offer_id": "SKU-OZON-IMAGE"}],
                        "last_id": "",
                    }
                },
                None,
            ),
            ({"result": {"items": [], "last_id": ""}}, None),
            (
                {
                    "items": [
                        {
                            "product_id": 7002,
                            "offer_id": "SKU-OZON-IMAGE",
                            "name": "Товар с фотографиями",
                            "primary_image": [first_url],
                            "images": [first_url, second_url],
                            "barcodes": ["2055061006940"],
                        }
                    ]
                },
                None,
            ),
            ({"result": []}, None),
        ]

        response = run_ozon_sync_request(
            body=json.dumps({"client": self.agency.id}).encode("utf-8")
        )

        self.assertEqual(response.status_code, 200)
        sku = SKU.objects.get(agency=self.agency, sku_code="SKU-OZON-IMAGE")
        self.assertEqual(sku.img, first_url)
        self.assertEqual(
            list(SKUPhoto.objects.filter(sku=sku).order_by("sort_order").values_list("url", flat=True)),
            [first_url, second_url],
        )

    @patch("market_sync.web_ui.run_ozon_sync_request")
    @patch("market_sync.web_ui.run_wb_sync_request")
    def test_sync_all_run_processes_all_clients_with_configured_markets(self, wb_sync_mock, ozon_sync_mock):
        second_agency = Agency.objects.create(agn_name="Клиент массовый")
        MarketCredential.objects.create(
            id=14,
            agency=self.agency,
            market=self.wb_market,
            market_key="wb-token",
        )
        MarketCredential.objects.create(
            id=15,
            agency=second_agency,
            market=self.ozon_market,
            market_key="ozon-token",
            client_id="12345",
        )
        wb_sync_mock.return_value = JsonResponse(
            {"ok": True, "processed": 3, "created": 1, "updated": 2, "barcodes_created": 4, "errors": []}
        )
        ozon_sync_mock.return_value = JsonResponse(
            {"ok": True, "processed": 5, "created": 2, "updated": 3, "barcodes_created": 1, "errors": []}
        )

        response = self.client.post("/market-sync/run-all/", data="{}", content_type="application/json")
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["clients_total"], 2)
        self.assertEqual(payload["clients_processed"], 2)
        self.assertEqual(payload["processed"], 8)
        self.assertEqual(payload["created"], 3)
        self.assertEqual(payload["updated"], 5)
        self.assertEqual(payload["barcodes_created"], 5)
        self.assertEqual(wb_sync_mock.call_count, 1)
        self.assertEqual(ozon_sync_mock.call_count, 1)

    def test_sync_all_run_returns_error_when_no_clients_are_configured(self):
        response = self.client.post("/market-sync/run-all/", data="{}", content_type="application/json")
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("Нет клиентов с настроенными маркетплейсами WB или Ozon.", payload["errors"])

    def test_run_ozon_sync_request_rejects_invalid_client_id_format(self):
        MarketCredential.objects.create(
            id=3,
            agency=self.agency,
            market=self.ozon_market,
            market_key="ozon-token",
            client_id="abc",
        )

        response = run_ozon_sync_request(body=json.dumps({"client": self.agency.id}).encode("utf-8"))
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("Client ID Ozon должен быть положительным числом.", payload["errors"])
