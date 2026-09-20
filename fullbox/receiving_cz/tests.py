import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from audit.models import OrderAuditEntry
from employees.models import Employee
from marking.models import MarkingCode
from sku.models import Agency, SKU, SKUBarcode
from sklad.models import WarehouseStockSnapshot

from .models import ReceivingCzUnit


class ReceivingCzFlowTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="receiving_cz_flow", password="pwd")
        Employee.objects.create(user=self.user, role="storekeeper", full_name="Storekeeper CZ", is_active=True)
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Client CZ", pref="CZ")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-CZ-FLOW",
            name="Marked item",
            size="42",
            honest_sign=True,
        )
        SKUBarcode.objects.create(sku=self.sku, value="2200000000421", size="42", is_primary=True)

    def _create_order(self, order_id: str, qty: int = 2):
        return OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Status",
            payload={
                "status": "warehouse",
                "status_label": "Warehouse",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "receiving_mode": "standard",
                "items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": self.sku.size,
                        "barcode": "2200000000421",
                        "qty": qty,
                    }
                ],
            },
        )

    def _create_empty_order(self, order_id: str):
        return OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Status",
            payload={
                "status": "warehouse",
                "status_label": "Warehouse",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "receiving_mode": "standard",
                "items": [],
            },
        )

    def _create_gtin_order(
        self,
        order_id: str,
        *,
        sku_code: str = "SKU-CZ-GTIN",
        barcode: str = "4600000000008",
        size: str = "44",
    ):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code=sku_code,
            name=f"Marked item {sku_code}",
            size=size,
            honest_sign=True,
        )
        SKUBarcode.objects.create(sku=sku, value=barcode, size=size, is_primary=True)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Status",
            payload={
                "status": "warehouse",
                "status_label": "Warehouse",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "receiving_mode": "cz",
                "items": [
                    {
                        "sku_code": sku.sku_code,
                        "name": sku.name,
                        "size": sku.size,
                        "barcode": barcode,
                        "qty": 2,
                    }
                ],
            },
        )
        return sku

    def test_detail_cz_start_redirects_to_separate_flow(self):
        order_id = "R-CZ-APP-START"
        self._create_order(order_id)

        response = self.client.post(
            f"/orders/receiving/{order_id}/",
            {
                "action": "create_receiving_act",
                "goods_type": "op",
                "receiving_mode": "cz",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/orders/receiving/{order_id}/cz-flow/")

    def test_empty_receiving_order_shows_and_starts_cz_flow(self):
        order_id = "R-CZ-APP-EMPTY"
        self._create_empty_order(order_id)

        page = self.client.get(f"/orders/receiving/{order_id}/")
        response = self.client.post(
            f"/orders/receiving/{order_id}/",
            {
                "action": "create_receiving_act",
                "goods_type": "op",
                "receiving_mode": "cz",
            },
        )

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Потоковая приемка с ЧЗ")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/orders/receiving/{order_id}/cz-flow/")

    def test_flow_page_renders(self):
        order_id = "R-CZ-APP-PAGE"
        self._create_order(order_id)

        response = self.client.get(f"/orders/receiving/{order_id}/cz-flow/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Потоковая приемка с ЧЗ")
        self.assertContains(response, "Неразмещенные товары")
        self.assertContains(response, "Текущий короб")
        self.assertContains(response, "Палеты")
        self.assertContains(response, "Сканируйте полный DataMatrix ЧЗ")
        self.assertContains(response, "Товар и размер определятся автоматически по GTIN")
        self.assertNotContains(response, 'id="cz-item-select"')
        self.assertContains(response, 'id="cz-scan-modal"')
        self.assertContains(response, "let czScanBusy = false;")
        self.assertContains(response, "let czDuplicateBlocked = false;")
        self.assertContains(response, "Понятно, товар отложен, продолжить")
        self.assertContains(response, "if (czDuplicateBlocked)")
        self.assertContains(response, "if (czScanBusy)")
        self.assertContains(response, "retry_reconcile: hadTransientFailure")
        self.assertContains(response, "Связь восстановлена без перезагрузки")
        self.assertContains(response, "GTIN НЕ СОВПАДАЕТ С ШК ТОВАРА")
        self.assertContains(response, "Подтвердить несовпадение и принять")
        self.assertContains(response, "confirm_gtin_mismatch: Boolean(approvedMismatch)")
        self.assertContains(response, "Сканируйте полный DataMatrix ЧЗ выбранного товара")
        self.assertNotContains(response, "ЧЗ уже сохранен. Синхронизирую приемку")

    def test_full_datamatrix_resolves_ean13_and_accepts_without_product_barcode(self):
        order_id = "R-CZ-ONE-SCAN-EAN13"
        sku = self._create_gtin_order(order_id)
        stock_count = WarehouseStockSnapshot.objects.count()

        response = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "marking_code": "]d2010460000000000821SERIAL-ONE\x1d91ABCD\x1d92CRYPTO",
                    "box_code": "BOX-CZ-ONE-SCAN",
                    "pallet_code": "PAL-CZ-ONE-SCAN",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        unit = ReceivingCzUnit.objects.get(order_id=order_id)
        self.assertEqual(unit.sku, sku)
        self.assertEqual(unit.sku_code, sku.sku_code)
        self.assertEqual(unit.size, "44")
        self.assertEqual(unit.barcode, "4600000000008")
        self.assertEqual(WarehouseStockSnapshot.objects.count(), stock_count)

    def test_full_datamatrix_resolves_exact_gtin14_barcode(self):
        order_id = "R-CZ-ONE-SCAN-GTIN14"
        sku = self._create_gtin_order(
            order_id,
            sku_code="SKU-CZ-GTIN14",
            barcode="04600000000008",
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "marking_code": "010460000000000821SERIAL-GTIN14",
                    "box_code": "BOX-CZ-GTIN14",
                    "pallet_code": "PAL-CZ-GTIN14",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        unit = ReceivingCzUnit.objects.get(order_id=order_id)
        self.assertEqual(unit.sku, sku)
        self.assertEqual(unit.barcode, "04600000000008")

    def test_one_scan_rejects_invalid_gtin_without_inventory_write(self):
        order_id = "R-CZ-ONE-SCAN-BAD-GTIN"
        self._create_gtin_order(order_id)
        stock_count = WarehouseStockSnapshot.objects.count()

        response = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "marking_code": "010460000000000121SERIAL-BAD",
                    "box_code": "BOX-CZ-BAD-GTIN",
                    "pallet_code": "PAL-CZ-BAD-GTIN",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason_code"], "invalid_marking_code")
        self.assertEqual(ReceivingCzUnit.objects.filter(order_id=order_id).count(), 0)
        self.assertEqual(MarkingCode.objects.count(), 0)
        self.assertEqual(WarehouseStockSnapshot.objects.count(), stock_count)

    def test_one_scan_rejects_unknown_gtin_without_inventory_write(self):
        order_id = "R-CZ-ONE-SCAN-UNKNOWN"
        self._create_gtin_order(order_id)
        stock_count = WarehouseStockSnapshot.objects.count()

        response = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "marking_code": "010001234567890521SERIAL-UNKNOWN",
                    "box_code": "BOX-CZ-UNKNOWN",
                    "pallet_code": "PAL-CZ-UNKNOWN",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason_code"], "unknown_gtin")
        self.assertEqual(ReceivingCzUnit.objects.filter(order_id=order_id).count(), 0)
        self.assertEqual(MarkingCode.objects.count(), 0)
        self.assertEqual(WarehouseStockSnapshot.objects.count(), stock_count)

    def test_one_scan_rejects_ambiguous_ean13_gtin14_mapping(self):
        order_id = "R-CZ-ONE-SCAN-AMBIGUOUS"
        self._create_empty_order(order_id)
        first_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-CZ-AMBIGUOUS-1",
            name="Ambiguous one",
            size="42",
            honest_sign=True,
        )
        second_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-CZ-AMBIGUOUS-2",
            name="Ambiguous two",
            size="46",
            honest_sign=True,
        )
        SKUBarcode.objects.create(
            sku=first_sku,
            value="4600000000008",
            size="42",
            is_primary=True,
        )
        SKUBarcode.objects.create(
            sku=second_sku,
            value="04600000000008",
            size="46",
            is_primary=True,
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "marking_code": "010460000000000821SERIAL-AMBIGUOUS",
                    "box_code": "BOX-CZ-AMBIGUOUS",
                    "pallet_code": "PAL-CZ-AMBIGUOUS",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["reason_code"], "ambiguous_gtin")
        self.assertEqual(ReceivingCzUnit.objects.filter(order_id=order_id).count(), 0)
        self.assertEqual(MarkingCode.objects.count(), 0)

    def test_one_scan_rejects_bare_gtin_without_inventory_write(self):
        order_id = "R-CZ-ONE-SCAN-BARE-GTIN"
        self._create_gtin_order(order_id)
        stock_count = WarehouseStockSnapshot.objects.count()

        response = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "marking_code": "04600000000008",
                    "box_code": "BOX-CZ-BARE-GTIN",
                    "pallet_code": "PAL-CZ-BARE-GTIN",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason_code"], "full_marking_code_required")
        self.assertEqual(ReceivingCzUnit.objects.filter(order_id=order_id).count(), 0)
        self.assertEqual(MarkingCode.objects.count(), 0)
        self.assertEqual(WarehouseStockSnapshot.objects.count(), stock_count)

    def test_server_requires_confirmation_when_product_barcode_conflicts_with_marking_gtin(self):
        order_id = "R-CZ-ONE-SCAN-MISMATCH"
        self._create_gtin_order(order_id)
        other_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-CZ-MISMATCH",
            name="Other marked item",
            size="50",
            honest_sign=True,
        )
        SKUBarcode.objects.create(
            sku=other_sku,
            value="5901234123457",
            size="50",
            is_primary=True,
        )
        stock_count = WarehouseStockSnapshot.objects.count()

        response = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "barcode": "5901234123457",
                    "marking_code": "010460000000000821SERIAL-MISMATCH",
                    "box_code": "BOX-CZ-MISMATCH",
                    "pallet_code": "PAL-CZ-MISMATCH",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["reason_code"],
            "marking_gtin_mismatch_confirmation_required",
        )
        self.assertEqual(response.json()["product_barcode"], "5901234123457")
        self.assertEqual(ReceivingCzUnit.objects.filter(order_id=order_id).count(), 0)
        self.assertEqual(MarkingCode.objects.count(), 0)
        self.assertEqual(WarehouseStockSnapshot.objects.count(), stock_count)

    def test_confirmed_gtin_mismatch_accepts_scanned_barcode_and_writes_audit(self):
        order_id = "R-CZ-ONE-SCAN-MISMATCH-CONFIRMED"
        self._create_gtin_order(order_id)
        other_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-CZ-MISMATCH-CONFIRMED",
            name="Confirmed mismatched item",
            size="50",
            honest_sign=True,
        )
        SKUBarcode.objects.create(
            sku=other_sku,
            value="5901234123457",
            size="50",
            is_primary=True,
        )
        stock_count = WarehouseStockSnapshot.objects.count()

        response = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "barcode": "5901234123457",
                    "marking_code": "010460000000000821SERIAL-MISMATCH-CONFIRMED",
                    "box_code": "BOX-CZ-MISMATCH-CONFIRMED",
                    "pallet_code": "PAL-CZ-MISMATCH-CONFIRMED",
                    "confirm_gtin_mismatch": True,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["gtin_mismatch_accepted"])
        unit = ReceivingCzUnit.objects.get(order_id=order_id)
        self.assertEqual(unit.sku, other_sku)
        self.assertEqual(unit.sku_code, "SKU-CZ-MISMATCH-CONFIRMED")
        self.assertEqual(unit.barcode, "5901234123457")
        audit = OrderAuditEntry.objects.get(
            order_id=order_id,
            payload__event_code="receiving_marking_gtin_mismatch_accepted",
        )
        self.assertEqual(audit.payload["marking_gtin"], "04600000000008")
        self.assertEqual(audit.payload["barcode"], "5901234123457")
        self.assertEqual(WarehouseStockSnapshot.objects.count(), stock_count)

    def test_string_confirmation_does_not_bypass_gtin_mismatch_guard(self):
        order_id = "R-CZ-ONE-SCAN-MISMATCH-STRING-CONFIRM"
        self._create_gtin_order(order_id)
        other_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-CZ-MISMATCH-STRING-CONFIRM",
            name="String confirmation item",
            size="50",
            honest_sign=True,
        )
        SKUBarcode.objects.create(
            sku=other_sku,
            value="5901234123457",
            size="50",
            is_primary=True,
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "barcode": "5901234123457",
                    "marking_code": "010460000000000821SERIAL-MISMATCH-STRING",
                    "box_code": "BOX-CZ-MISMATCH-STRING",
                    "pallet_code": "PAL-CZ-MISMATCH-STRING",
                    "confirm_gtin_mismatch": "true",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["reason_code"],
            "marking_gtin_mismatch_confirmation_required",
        )
        self.assertFalse(ReceivingCzUnit.objects.filter(order_id=order_id).exists())
        self.assertFalse(MarkingCode.objects.exists())

    def test_confirmation_cannot_accept_bare_gtin_as_marking_code(self):
        order_id = "R-CZ-ONE-SCAN-BARE-GTIN-CONFIRM"
        self._create_gtin_order(order_id)

        response = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "barcode": "4600000000008",
                    "marking_code": "2044384358003",
                    "box_code": "BOX-CZ-BARE-GTIN-CONFIRM",
                    "pallet_code": "PAL-CZ-BARE-GTIN-CONFIRM",
                    "confirm_gtin_mismatch": True,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason_code"], "full_marking_code_required")
        self.assertFalse(ReceivingCzUnit.objects.filter(order_id=order_id).exists())
        self.assertFalse(MarkingCode.objects.exists())

    def test_flow_page_includes_accepted_cz_units_for_expandable_rows(self):
        order_id = "R-CZ-APP-UNITS"
        self._create_order(order_id)
        self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "barcode": "2200000000421",
                    "marking_code": "CZ-PAGE-UNIT-1",
                    "box_code": "BOX-CZ-PAGE-1",
                    "pallet_code": "PAL-CZ-PAGE-1",
                }
            ),
            content_type="application/json",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/cz-flow/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="cz-accepted-units-data"')
        self.assertContains(response, "CZ-PAGE-UNIT-1")
        self.assertContains(response, "BOX-CZ-PAGE-1")

    def test_scan_catalog_item_for_empty_order(self):
        order_id = "R-CZ-APP-CATALOG"
        self._create_empty_order(order_id)

        response = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "barcode": "2200000000421",
                    "marking_code": "CZ-CATALOG-1",
                    "box_code": "BOX-CZ-CATALOG-1",
                    "pallet_code": "PAL-CZ-CATALOG-1",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        unit = ReceivingCzUnit.objects.get(order_id=order_id)
        self.assertEqual(unit.sku_code, self.sku.sku_code)
        self.assertEqual(unit.barcode, "2200000000421")

    def test_scan_unit_rejects_barcode_as_marking_code(self):
        order_id = "R-CZ-APP-BARCODE-AS-CZ"
        self._create_order(order_id)

        response = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "barcode": "2200000000421",
                    "marking_code": "2200000000421",
                    "box_code": "BOX-CZ-BARCODE-1",
                    "pallet_code": "PAL-CZ-BARCODE-1",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(ReceivingCzUnit.objects.filter(order_id=order_id).count(), 0)
        self.assertIn("Сейчас нужен код ЧЗ", response.json().get("error", ""))

    def test_scan_unit_rejects_duplicate_normalized_marking_code(self):
        order_id = "R-CZ-APP-DUP"
        self._create_gtin_order(order_id)

        first = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "marking_code": "010460000000000821ABC\x1d91ABCD\x1d92CRYPTO",
                    "box_code": "BOX-CZ-1",
                    "pallet_code": "PAL-CZ-1",
                }
            ),
            content_type="application/json",
        )
        second = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "marking_code": "010460000000000821ABC_x001D_91ABCD_x001D_92CRYPTO",
                    "box_code": "BOX-CZ-1",
                    "pallet_code": "PAL-CZ-1",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertIn("Этот ЧЗ уже отсканирован", second.json().get("error", ""))
        self.assertEqual(ReceivingCzUnit.objects.count(), 1)
        self.assertEqual(MarkingCode.objects.count(), 1)

    def test_retry_after_transient_response_recovers_unit_without_duplicate_error(self):
        order_id = "R-CZ-ONE-SCAN-RETRY"
        self._create_gtin_order(order_id)
        marking_code = "010460000000000821SERIAL-RETRY"
        payload = {
            "marking_code": marking_code,
            "box_code": "BOX-CZ-RETRY",
            "pallet_code": "PAL-CZ-RETRY",
        }

        first = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        retry = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps({**payload, "retry_reconcile": True}),
            content_type="application/json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(retry.status_code, 200)
        self.assertTrue(retry.json()["ok"])
        self.assertTrue(retry.json()["reconciled"])
        self.assertEqual(retry.json()["unit"]["box_code"], "BOX-CZ-RETRY")
        self.assertEqual(retry.json()["unit"]["pallet_code"], "PAL-CZ-RETRY")
        self.assertEqual(ReceivingCzUnit.objects.filter(order_id=order_id).count(), 1)

    def test_manual_duplicate_remains_blocked_with_exact_reason(self):
        order_id = "R-CZ-ONE-SCAN-MANUAL-DUP"
        self._create_gtin_order(order_id)
        payload = {
            "marking_code": "010460000000000821SERIAL-MANUAL-DUP",
            "box_code": "BOX-CZ-MANUAL-DUP",
            "pallet_code": "PAL-CZ-MANUAL-DUP",
        }

        self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        duplicate = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(duplicate.status_code, 409)
        self.assertIn("уже отсканирован", duplicate.json()["error"])
        self.assertEqual(ReceivingCzUnit.objects.filter(order_id=order_id).count(), 1)

    def test_retry_reconcile_does_not_hide_duplicate_from_another_box(self):
        order_id = "R-CZ-ONE-SCAN-OTHER-BOX"
        self._create_gtin_order(order_id)
        marking_code = "010460000000000821SERIAL-OTHER-BOX"

        self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "marking_code": marking_code,
                    "box_code": "BOX-CZ-ORIGINAL",
                    "pallet_code": "PAL-CZ-ORIGINAL",
                }
            ),
            content_type="application/json",
        )
        retry = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "marking_code": marking_code,
                    "box_code": "BOX-CZ-OTHER",
                    "pallet_code": "PAL-CZ-OTHER",
                    "retry_reconcile": True,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(retry.status_code, 409)
        self.assertIn("BOX-CZ-ORIGINAL", retry.json()["error"])
        self.assertEqual(ReceivingCzUnit.objects.filter(order_id=order_id).count(), 1)

    def test_scan_accepts_processing_code_and_preserves_processing_history(self):
        order_id = "R-CZ-FROM-PROCESSING"
        self._create_order(order_id)
        source_used_at = timezone.now()
        source_mark = MarkingCode.objects.create(
            order_type="processing",
            order_id="P-CZ-SOURCE",
            agency=self.agency,
            sku=self.sku,
            sku_code=self.sku.sku_code,
            size=self.sku.size,
            barcode="2200000000421",
            box_barcode="BOX-PROCESSING-SOURCE",
            code="CZ-FROM-PROCESSING-1",
            source="scan",
            created_by=self.user,
            used_at=source_used_at,
            used_by=self.user,
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "barcode": "2200000000421",
                    "marking_code": source_mark.code,
                    "box_code": "BOX-RECEIVING-TARGET",
                    "pallet_code": "PAL-RECEIVING-TARGET",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        unit = ReceivingCzUnit.objects.get(order_id=order_id)
        self.assertEqual(unit.marking_code, source_mark.code)
        source_mark.refresh_from_db()
        self.assertEqual(source_mark.order_type, "processing")
        self.assertEqual(source_mark.order_id, "P-CZ-SOURCE")
        self.assertEqual(source_mark.box_barcode, "BOX-PROCESSING-SOURCE")
        self.assertEqual(source_mark.used_at, source_used_at)

    def test_duplicate_block_reports_owner_process_order_and_containers(self):
        order_id = "R-CZ-BLOCK-DETAILS"
        self._create_order(order_id)
        existing_unit = ReceivingCzUnit.objects.create(
            order_id="R-CZ-ORIGINAL",
            agency=self.agency,
            sku=self.sku,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            size=self.sku.size,
            barcode="2200000000421",
            marking_code="CZ-BLOCK-DETAILS-1",
            box_code="BOX-CZ-ORIGINAL",
            pallet_code="PAL-CZ-ORIGINAL",
            accepted_by=self.user,
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "barcode": "2200000000421",
                    "marking_code": existing_unit.marking_code,
                    "box_code": "BOX-CZ-SECOND",
                    "pallet_code": "PAL-CZ-SECOND",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertEqual(payload["reason_code"], "duplicate")
        self.assertEqual(payload["owner_name"], "Client CZ")
        self.assertEqual(payload["process"], "receiving")
        self.assertEqual(payload["order_id"], "R-CZ-ORIGINAL")
        self.assertEqual(payload["box_code"], "BOX-CZ-ORIGINAL")
        self.assertEqual(payload["pallet_code"], "PAL-CZ-ORIGINAL")
        self.assertEqual(payload["used_by"], "receiving_cz_flow")

    def test_other_client_block_reports_processing_source(self):
        order_id = "R-CZ-OTHER-CLIENT"
        self._create_order(order_id)
        other_agency = Agency.objects.create(agn_name="Other CZ Client", pref="OCZ")
        MarkingCode.objects.create(
            order_type="processing",
            order_id="P-CZ-OTHER-CLIENT",
            agency=other_agency,
            sku_code=self.sku.sku_code,
            size=self.sku.size,
            barcode="2200000000421",
            box_barcode="BOX-CZ-OTHER-CLIENT",
            code="CZ-OTHER-CLIENT-1",
            source="scan",
            used_at=timezone.now(),
            used_by=self.user,
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "barcode": "2200000000421",
                    "marking_code": "CZ-OTHER-CLIENT-1",
                    "box_code": "BOX-CZ-CURRENT",
                    "pallet_code": "PAL-CZ-CURRENT",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertEqual(payload["reason_code"], "conflict")
        self.assertEqual(payload["owner_name"], "Other CZ Client")
        self.assertEqual(payload["process"], "processing")
        self.assertEqual(payload["order_id"], "P-CZ-OTHER-CLIENT")
        self.assertEqual(payload["box_code"], "BOX-CZ-OTHER-CLIENT")

    def test_integrity_race_returns_duplicate_details(self):
        order_id = "R-CZ-INTEGRITY-RACE"
        self._create_order(order_id)
        existing_unit = ReceivingCzUnit.objects.create(
            order_id="R-CZ-RACE-SOURCE",
            agency=self.agency,
            sku=self.sku,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            size=self.sku.size,
            barcode="2200000000421",
            marking_code="CZ-INTEGRITY-RACE-1",
            box_code="BOX-CZ-RACE-SOURCE",
            pallet_code="PAL-CZ-RACE-SOURCE",
            accepted_by=self.user,
        )

        with mock.patch("receiving_cz.views.scan_unit", side_effect=IntegrityError("duplicate")):
            response = self.client.post(
                f"/orders/receiving/{order_id}/cz-flow/scan/",
                data=json.dumps(
                    {
                        "barcode": "2200000000421",
                        "marking_code": existing_unit.marking_code,
                        "box_code": "BOX-CZ-RACE-TARGET",
                        "pallet_code": "PAL-CZ-RACE-TARGET",
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertEqual(payload["reason_code"], "duplicate")
        self.assertEqual(payload["order_id"], "R-CZ-RACE-SOURCE")
        self.assertEqual(payload["box_code"], "BOX-CZ-RACE-SOURCE")
        self.assertEqual(payload["used_by"], "receiving_cz_flow")

    def test_delete_unit_frees_marking_code_for_rescan(self):
        order_id = "R-CZ-APP-DELETE"
        self._create_order(order_id)
        first = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "barcode": "2200000000421",
                    "marking_code": "CZ-DELETE-1",
                    "box_code": "BOX-CZ-DELETE-1",
                    "pallet_code": "PAL-CZ-DELETE-1",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)
        unit_id = first.json()["unit"]["id"]

        deleted = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/unit/delete/",
            data=json.dumps({"unit_id": unit_id}),
            content_type="application/json",
        )

        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(ReceivingCzUnit.objects.filter(order_id=order_id).count(), 0)
        self.assertFalse(MarkingCode.objects.filter(code="CZ-DELETE-1").exists())

        rescanned = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "barcode": "2200000000421",
                    "marking_code": "CZ-DELETE-1",
                    "box_code": "BOX-CZ-DELETE-2",
                    "pallet_code": "PAL-CZ-DELETE-2",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(rescanned.status_code, 200)
        self.assertEqual(ReceivingCzUnit.objects.filter(order_id=order_id).count(), 1)

    def test_delete_box_units_frees_marking_codes_for_rescan(self):
        order_id = "R-CZ-APP-DELETE-BOX"
        self._create_order(order_id)
        for index in range(2):
            response = self.client.post(
                f"/orders/receiving/{order_id}/cz-flow/scan/",
                data=json.dumps(
                    {
                        "barcode": "2200000000421",
                        "marking_code": f"CZ-DELETE-BOX-{index}",
                        "box_code": "BOX-CZ-DELETE-BOX-1",
                        "pallet_code": "PAL-CZ-DELETE-BOX-1",
                    }
                ),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)

        deleted = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/box/delete-units/",
            data=json.dumps({"box_code": "BOX-CZ-DELETE-BOX-1"}),
            content_type="application/json",
        )

        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(len(deleted.json()["units"]), 2)
        self.assertEqual(ReceivingCzUnit.objects.filter(order_id=order_id).count(), 0)
        self.assertFalse(MarkingCode.objects.filter(code__startswith="CZ-DELETE-BOX-").exists())

        rescanned = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "barcode": "2200000000421",
                    "marking_code": "CZ-DELETE-BOX-0",
                    "box_code": "BOX-CZ-DELETE-BOX-2",
                    "pallet_code": "PAL-CZ-DELETE-BOX-2",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(rescanned.status_code, 200)
        self.assertEqual(ReceivingCzUnit.objects.filter(order_id=order_id).count(), 1)

    def test_close_flow_writes_marked_units_to_warehouse(self):
        order_id = "R-CZ-APP-CLOSE"
        self._create_order(order_id)
        for idx in range(2):
            response = self.client.post(
                f"/orders/receiving/{order_id}/cz-flow/scan/",
                data=json.dumps(
                    {
                        "barcode": "2200000000421",
                        "marking_code": f"CZ-APP-CLOSE-{idx}",
                        "box_code": "BOX-CZ-CLOSE-1",
                        "pallet_code": "PAL-CZ-CLOSE-1",
                    }
                ),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)

        response = self.client.post(
            f"/orders/receiving/{order_id}/cz-flow/",
            {"action": "close"},
        )

        self.assertEqual(response.status_code, 302)
        rows = list(
            WarehouseStockSnapshot.objects.filter(
                source_context_type="receiving",
                source_context_id=order_id,
                sku_code=self.sku.sku_code,
            ).order_by("marking_code")
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual([row.qty for row in rows], [1, 1])
        self.assertEqual([row.marking_code for row in rows], ["CZ-APP-CLOSE-0", "CZ-APP-CLOSE-1"])
        act_entry = OrderAuditEntry.objects.filter(order_id=order_id, payload__act="receiving").latest("id")
        self.assertEqual(len((act_entry.payload or {}).get("act_units") or []), 2)
