import json
from io import BytesIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TestCase, override_settings
from openpyxl import Workbook

from fbs.models import FbsClientMovementRequest, FbsIntegrationProfile
from sklad.models import WarehouseContainer, WarehouseLocation, WarehouseOperation, WarehouseReserve, WarehouseStockSnapshot
from sku.models import Agency, SKU, SKUBarcode


@override_settings(ROOT_URLCONF="client_cabinet.test_urls_fbs")
class ClientFbsApiTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(username="fbs-client", password="pwd")
        self.agency = Agency.objects.create(agn_name="Клиент FBS API", portal_user=self.owner)
        self.other = Agency.objects.create(agn_name="Чужой клиент FBS API")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB",
            external_warehouse_id="1931120",
            is_active=True,
            order_pull_enabled=True,
        )
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-API",
            name="Товар API",
            brand="API Brand",
        )
        SKUBarcode.objects.create(sku=self.sku, value="4600000000018", is_primary=True)
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="STORAGE",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            location_code="API-A-01",
            row_no=3,
            section_no=1,
            tier_no=1,
            cell_no=1,
        )
        self.location = location
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            sku_ref=self.sku,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode="4600000000018",
            goods_type="gv",
            qty=50,
            available_qty=50,
            location=location,
            zone_code="STORAGE",
        )
        self.client = Client()
        self.client.force_login(self.owner)

    def _whole_box(self, *, code: str, qty: int):
        container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code=code,
            current_location=self.location,
        )
        return WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            sku_ref=self.sku,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode="4600000000018",
            goods_type="gv",
            qty=qty,
            available_qty=qty,
            container=container,
            container_code=container.container_code,
            location=self.location,
            zone_code="STORAGE",
        )

    def test_overview_uses_portal_owner_agency(self):
        response = self.client.get("/client/api/v1/fbs/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()["data"]
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["profiles"][0]["warehouse"], "1931120")

    def test_client_fbs_section_follows_head_manager_profile_switch(self):
        enabled_response = self.client.get("/client/")

        self.assertEqual(enabled_response.status_code, 200)
        self.assertContains(enabled_response, 'href="#/fbs"')
        self.assertContains(enabled_response, 'data-page="/fbs"')
        self.assertContains(enabled_response, "Выбрано к перемещению")
        self.assertContains(enabled_response, "К перемещению: 0 шт.")
        self.assertContains(enabled_response, "Товары и короба сохраняются автоматически")
        self.assertContains(enabled_response, "Целый короб со склада")
        self.assertContains(enabled_response, "Физических коробов всего")
        self.assertContains(enabled_response, "Из них микс-коробов")

        self.profile.order_pull_enabled = False
        self.profile.save(update_fields=["order_pull_enabled", "updated_at"])
        disabled_response = self.client.get("/client/")

        self.assertEqual(disabled_response.status_code, 200)
        self.assertNotContains(disabled_response, 'href="#/fbs"')
        self.assertNotContains(disabled_response, 'data-page="/fbs"')
        self.assertNotContains(disabled_response, "client_cabinet/fbs-lk.js")

    def test_disabled_client_cannot_open_fbs_api_directly(self):
        self.profile.order_pull_enabled = False
        self.profile.save(update_fields=["order_pull_enabled", "updated_at"])

        response = self.client.get("/client/api/v1/fbs/")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["error"],
            "FBS для клиента выключен начальником склада.",
        )

    def test_create_piece_movement_request(self):
        response = self.client.post(
            "/client/api/v1/fbs/movements/",
            data=json.dumps(
                {
                    "mode": "item",
                    "lines": [{"barcode": "4600000000018", "qty": 8, "box_count": 2}],
                    "comment": "На тест",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        request_row = FbsClientMovementRequest.objects.get()
        self.assertEqual(request_row.agency_id, self.agency.id)
        self.assertEqual(request_row.requested_qty, 8)
        self.assertEqual(request_row.requested_box_count, 2)
        self.assertEqual(request_row.lines.get().requested_box_count, 2)
        self.assertEqual(request_row.requested_by_id, self.owner.id)
        self.assertFalse(WarehouseReserve.objects.exists())
        self.assertFalse(WarehouseOperation.objects.exists())
        stock_response = self.client.get("/client/api/v1/fbs/movement-stock/")
        self.assertEqual(stock_response.status_code, 200)
        stock = stock_response.json()["data"]["results"][0]
        self.assertEqual(stock["warehouse_fact_qty"], 50)
        self.assertEqual(stock["warehouse_available_qty"], 50)
        self.assertEqual(stock["goods_type"], "Готовый")
        self.assertEqual(stock["pending_movement_qty"], 8)
        self.assertEqual(stock["client_available_qty"], 42)
        snapshot = WarehouseStockSnapshot.objects.get(agency=self.agency)
        self.assertEqual(snapshot.qty, 50)
        self.assertEqual(snapshot.available_qty, 50)

    def test_create_piece_movement_with_one_physical_mixed_box(self):
        second_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-API-2",
            name="Второй товар API",
        )
        SKUBarcode.objects.create(
            sku=second_sku,
            value="4600000000025",
            is_primary=True,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            sku_ref=second_sku,
            sku_code=second_sku.sku_code,
            name=second_sku.name,
            barcode="4600000000025",
            goods_type="gv",
            qty=20,
            available_qty=20,
            location=self.location,
            zone_code="STORAGE",
        )

        response = self.client.post(
            "/client/api/v1/fbs/movements/",
            data=json.dumps(
                {
                    "mode": "item",
                    "lines": [
                        {"barcode": "4600000000018", "qty": 1},
                        {"barcode": "4600000000025", "qty": 1},
                    ],
                    "requested_box_count": 1,
                    "requested_mixed_box_count": 1,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        request_row = FbsClientMovementRequest.objects.get()
        self.assertEqual(request_row.requested_qty, 2)
        self.assertEqual(request_row.requested_box_count, 1)
        self.assertEqual(request_row.requested_mixed_box_count, 1)
        self.assertEqual(response.json()["data"]["requested_mixed_box_count"], 1)
        self.assertEqual(
            list(request_row.lines.values_list("requested_box_count", flat=True)),
            [0, 0],
        )

    def test_movement_stock_api_accepts_separate_catalog_filters(self):
        response = self.client.get(
            "/client/api/v1/fbs/movement-stock/",
            {"article": "sku-api", "brand": "api brand", "barcode": "000018"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()["data"]
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["results"][0]["brand"], "API Brand")

        empty_response = self.client.get(
            "/client/api/v1/fbs/movement-stock/",
            {"brand": "Несуществующий бренд"},
        )
        self.assertEqual(empty_response.status_code, 200)
        self.assertEqual(empty_response.json()["data"]["total"], 0)

    def test_box_mode_api_lists_only_real_whole_box_options(self):
        self._whole_box(code="API-WHOLE-12-A", qty=12)
        self._whole_box(code="API-WHOLE-12-B", qty=12)

        stock_response = self.client.get(
            "/client/api/v1/fbs/movement-stock/",
            {"mode": "box", "barcode": "4600000000018"},
        )

        self.assertEqual(stock_response.status_code, 200)
        option = stock_response.json()["data"]["results"][0]["box_options"][0]
        self.assertEqual(option["units_per_box"], 12)
        self.assertEqual(option["available_box_count"], 2)

        rejected = self.client.post(
            "/client/api/v1/fbs/movements/",
            data=json.dumps(
                {
                    "mode": "box",
                    "lines": [
                        {
                            "barcode": "4600000000018",
                            "qty": 6,
                            "units_per_box": 6,
                        }
                    ],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertTrue(
            any(
                "кратность целого короба" in message
                for message in rejected.json()["details"]
            )
        )

    def test_movement_stock_and_request_use_only_ready_goods_for_same_barcode(self):
        source = WarehouseStockSnapshot.objects.get(agency=self.agency)
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            sku_ref=self.sku,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode="4600000000018",
            goods_type="no",
            qty=30,
            available_qty=30,
            location=source.location,
            zone_code="STORAGE",
        )

        stock_response = self.client.get("/client/api/v1/fbs/movement-stock/")

        self.assertEqual(stock_response.status_code, 200)
        payload = stock_response.json()["data"]
        self.assertEqual(payload["summary"]["goods_type"], "Готовый")
        self.assertEqual(payload["summary"]["warehouse_fact_qty"], 50)
        self.assertEqual(payload["summary"]["warehouse_available_qty"], 50)

        rejected = self.client.post(
            "/client/api/v1/fbs/movements/",
            data=json.dumps(
                {
                    "mode": "item",
                    "lines": [{"barcode": "4600000000018", "qty": 51}],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertTrue(
            any("готовый товар" in message for message in rejected.json()["details"])
        )
        self.assertFalse(FbsClientMovementRequest.objects.exists())

    def test_excel_import_returns_preview_without_saving(self):
        self._whole_box(code="API-IMPORT-12-A", qty=12)
        self._whole_box(code="API-IMPORT-12-B", qty=12)
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Штрихкод", "Количество, шт.", "Кратность короба, шт."])
        sheet.append(["4600000000018", 24, 12])
        stream = BytesIO()
        workbook.save(stream)
        upload = SimpleUploadedFile(
            "movement.xlsx",
            stream.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        response = self.client.post(
            "/client/api/v1/fbs/movements/import/",
            {"mode": "box", "file": upload},
        )

        self.assertEqual(response.status_code, 200, response.content)
        line = response.json()["data"]["lines"][0]
        self.assertEqual(line["barcode"], "4600000000018")
        self.assertEqual(line["box_count"], 2)
        self.assertFalse(FbsClientMovementRequest.objects.exists())

    def test_excel_item_import_accepts_requested_box_count(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(
            [
                "Штрихкод",
                "Количество, шт.",
                "Кратность короба, шт.",
                "Коробов для поштучного",
            ]
        )
        sheet.append(["4600000000018", 8, "", 2])
        stream = BytesIO()
        workbook.save(stream)
        upload = SimpleUploadedFile(
            "movement-items.xlsx",
            stream.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        response = self.client.post(
            "/client/api/v1/fbs/movements/import/",
            {"mode": "item", "file": upload},
        )

        self.assertEqual(response.status_code, 200, response.content)
        line = response.json()["data"]["lines"][0]
        self.assertEqual(line["qty"], 8)
        self.assertEqual(line["box_count"], 2)
        self.assertFalse(FbsClientMovementRequest.objects.exists())

    def test_template_download_is_xlsx(self):
        response = self.client.get("/client/fbs/movement-template.xlsx")

        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response["Content-Type"])
        self.assertGreater(len(response.content), 1000)


class ClientFbsMovementFrontendContractTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        root = Path(settings.BASE_DIR)
        cls.script = (
            root / "client_cabinet" / "static" / "client_cabinet" / "fbs-lk.js"
        ).read_text(encoding="utf-8")
        cls.dashboard = (
            root
            / "client_cabinet"
            / "templates"
            / "client_cabinet"
            / "dashboard_lk_react.html"
        ).read_text(encoding="utf-8")

    def test_box_submit_reconciles_current_stock_before_post(self):
        self.assertIn("function prepareMovementSubmission()", self.script)
        self.assertIn("movementStockLoading || !movementStockLoaded", self.script)
        self.assertIn("var exactQty = calculatedBoxQuantity(selectedUnits, boxCount);", self.script)
        self.assertIn("lines: prepared.rows", self.script)
        self.assertNotIn(
            "box_count: itemMode ? \"0\" : (tr.querySelector('[data-field=\"box_count\"]')"
            ".value || \"1\")",
            self.script,
        )

    def test_old_box_draft_is_reset_after_multiplicity_change(self):
        self.assertIn("var MOVEMENT_DRAFT_VERSION = 6;", self.script)
        self.assertIn(
            'mode === "box" && draft.version < 5',
            self.script,
        )
        self.assertIn("Старый черновик коробов сброшен", self.script)

    def test_movement_submit_includes_idempotency_key(self):
        self.assertIn("function newMovementIdempotencyKey()", self.script)
        self.assertIn("idempotency_key: movementIdempotencyKey", self.script)
        self.assertIn("movementIdempotencyKey = newMovementIdempotencyKey();", self.script)

    def test_fbs_script_cache_version_is_updated(self):
        self.assertIn("fbs-lk.js' %}?v=20260830-qty-delay-2", self.dashboard)

    def test_movement_quantity_waits_for_multidigit_input(self):
        self.assertIn("var MOVEMENT_QTY_COMMIT_DELAY_MS = 3500;", self.script)
        self.assertIn("scheduleMovementStockQuantity(event.target);", self.script)
        self.assertIn("flushPendingMovementQtyCommits();", self.script)
