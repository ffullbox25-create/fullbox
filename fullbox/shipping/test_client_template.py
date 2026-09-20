from io import BytesIO
from pathlib import Path

from django.test import SimpleTestCase
from openpyxl import Workbook

from django.test import RequestFactory

from shipping.client_template import (
    DISCREPANCY_MARKER,
    TEMPLATE_PATH,
    apply_client_shipping_template,
    build_client_shipping_template_response,
    merge_comment_with_discrepancies,
    parse_client_shipping_template,
)
from shipping.services import _shipping_use_client_lk_form


def _xlsx(rows):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


class ClientShippingTemplateTests(SimpleTestCase):
    def test_saved_template_file_exists(self):
        self.assertTrue(TEMPLATE_PATH.exists(), TEMPLATE_PATH)

    def test_download_returns_saved_template(self):
        response = build_client_shipping_template_response()
        self.assertEqual(response.status_code, 200)
        self.assertIn("shk-excel.xlsx", response.get("Content-Disposition", ""))

    def test_parse_and_apply_by_barcode_and_qty(self):
        stock = [
            {
                "key": "k1",
                "sku_code": "SKU-1",
                "size": "42",
                "barcode": "4612345678901",
                "box_qty": 10,
                "available_boxes": 5,
                "is_mixed_box": False,
            }
        ]
        file_obj = _xlsx(
            [
                ["Баркод товара", "Кол-во товаров", "ШК короба", "Срок годности"],
                ["4612345678901", 20, "", ""],
            ]
        )
        result = apply_client_shipping_template(file_obj=file_obj, stock_rows=stock)
        self.assertEqual(result.parse_errors, [])
        self.assertFalse(result.has_discrepancies)
        self.assertEqual(result.selected_boxes, {"k1": "2"})

    def test_barcode_from_excel_float(self):
        stock = [
            {
                "key": "k1",
                "sku_code": "SKU-1",
                "size": "",
                "barcode": "2000000000000",
                "box_qty": 5,
                "available_boxes": 3,
                "is_mixed_box": False,
            }
        ]
        file_obj = _xlsx(
            [
                ["Баркод товара", "Кол-во товаров"],
                [2000000000000.0, 10],
            ]
        )
        result = apply_client_shipping_template(file_obj=file_obj, stock_rows=stock)
        self.assertEqual(result.selected_boxes, {"k1": "2"})

    def test_floor_to_whole_boxes_with_discrepancy(self):
        """Client requests 445 pcs; pack 30 with stock 450 → 14 boxes / 420, gap 25."""
        stock = [
            {
                "key": "k30",
                "sku_code": "вешалка003",
                "size": "",
                "barcode": "2039698778296",
                "box_qty": 30,
                "available_boxes": 15,
                "is_mixed_box": False,
                "name": "Вешалка",
            }
        ]
        file_obj = _xlsx(
            [
                ["Баркод товара", "Кол-во товаров"],
                ["2039698778296", 445],
            ]
        )
        result = apply_client_shipping_template(file_obj=file_obj, stock_rows=stock)
        self.assertEqual(result.selected_boxes, {"k30": "14"})
        self.assertEqual(result.assembled_lines[0].applied_qty, 420)
        self.assertEqual(result.assembled_lines[0].boxes, 14)
        self.assertEqual(result.assembled_lines[0].requested_qty, 445)
        self.assertTrue(result.has_discrepancies)
        self.assertTrue(any("расхождение 25" in item for item in result.discrepancies))

    def test_qty_floors_non_multiple(self):
        stock = [
            {
                "key": "k1",
                "sku_code": "SKU-1",
                "size": "",
                "barcode": "111",
                "box_qty": 12,
                "available_boxes": 10,
                "is_mixed_box": False,
            }
        ]
        # 50 → 4 boxes / 48 pcs, shortfall 2
        file_obj = _xlsx(
            [
                ["Баркод товара", "Кол-во товаров"],
                ["111", 50],
            ]
        )
        result = apply_client_shipping_template(file_obj=file_obj, stock_rows=stock)
        self.assertEqual(result.selected_boxes, {"k1": "4"})
        self.assertEqual(result.assembled_lines[0].applied_qty, 48)
        self.assertTrue(any("расхождение 2" in item for item in result.discrepancies))

        # Exact multiple → clean
        file_obj = _xlsx(
            [
                ["Баркод товара", "Кол-во товаров"],
                ["111", 24],
            ]
        )
        result = apply_client_shipping_template(file_obj=file_obj, stock_rows=stock)
        self.assertFalse(result.has_discrepancies)
        self.assertEqual(result.selected_boxes, {"k1": "2"})
        self.assertEqual(result.assembled_lines[0].applied_qty, 24)

    def test_qty_below_one_box_cannot_apply(self):
        stock = [
            {
                "key": "k1",
                "sku_code": "SKU-1",
                "size": "",
                "barcode": "111",
                "box_qty": 66,
                "available_boxes": 5,
                "is_mixed_box": False,
            }
        ]
        file_obj = _xlsx(
            [
                ["Баркод товара", "Кол-во товаров"],
                ["111", 2],
            ]
        )
        result = apply_client_shipping_template(file_obj=file_obj, stock_rows=stock)
        self.assertEqual(result.selected_boxes, {})
        self.assertTrue(any("не удалось набрать" in item for item in result.discrepancies))

    def test_korobov_column(self):
        stock = [
            {
                "key": "k1",
                "sku_code": "SKU-1",
                "size": "",
                "barcode": "111",
                "box_qty": 66,
                "available_boxes": 5,
                "is_mixed_box": False,
            }
        ]
        file_obj = _xlsx(
            [
                ["Баркод товара", "Кол-во товаров", "Коробов"],
                ["111", "", 2],
            ]
        )
        result = apply_client_shipping_template(file_obj=file_obj, stock_rows=stock)
        self.assertEqual(result.selected_boxes, {"k1": "2"})
        self.assertEqual(result.assembled_lines[0].applied_qty, 132)

    def test_447_with_stock_cap(self):
        """447 pcs, pack 30, only 10 boxes (300) → take all 10, gap 147."""
        stock = [
            {
                "key": "k30",
                "sku_code": "вешалка003",
                "size": "",
                "barcode": "2039698778296",
                "box_qty": 30,
                "available_boxes": 10,
                "is_mixed_box": False,
                "name": "Вешалка",
            }
        ]
        file_obj = _xlsx(
            [
                ["Баркод товара", "Кол-во товаров"],
                ["2039698778296", 447],
            ]
        )
        result = apply_client_shipping_template(file_obj=file_obj, stock_rows=stock)
        self.assertEqual(result.selected_boxes, {"k30": "10"})
        self.assertEqual(result.assembled_lines[0].applied_qty, 300)
        self.assertTrue(any("расхождение 147" in item for item in result.discrepancies))

    def test_insufficient_stock_caps_and_flags(self):
        stock = [
            {
                "key": "k1",
                "sku_code": "SKU-1",
                "size": "M",
                "barcode": "222",
                "box_qty": 5,
                "available_boxes": 2,
                "is_mixed_box": False,
            }
        ]
        file_obj = _xlsx(
            [
                ["Баркод товара", "Кол-во товаров"],
                ["222", 25],
            ]
        )
        result = apply_client_shipping_template(file_obj=file_obj, stock_rows=stock)
        self.assertTrue(result.has_discrepancies)
        self.assertEqual(result.selected_boxes, {"k1": "2"})
        self.assertIn(DISCREPANCY_MARKER, result.discrepancy_comment_block())

    def test_unknown_barcode(self):
        file_obj = _xlsx(
            [
                ["Баркод товара", "Кол-во товаров"],
                ["999999", 10],
            ]
        )
        result = apply_client_shipping_template(file_obj=file_obj, stock_rows=[])
        self.assertTrue(result.has_discrepancies)
        self.assertTrue(any("не найден" in item for item in result.discrepancies))

    def test_same_barcode_different_box_qty_picks_by_multiple(self):
        stock = [
            {
                "key": "k30",
                "sku_code": "вешалка003",
                "size": "",
                "barcode": "2039698778296",
                "box_qty": 30,
                "available_boxes": 15,
                "is_mixed_box": False,
            },
            {
                "key": "k20",
                "sku_code": "вешалка003",
                "size": "",
                "barcode": "2039698778296",
                "box_qty": 20,
                "available_boxes": 1,
                "is_mixed_box": False,
            },
        ]
        file_obj = _xlsx(
            [
                ["Баркод товара", "Кол-во товаров", "ШК короба", "Срок годности"],
                ["2039698778296", 30, "", ""],
            ]
        )
        result = apply_client_shipping_template(file_obj=file_obj, stock_rows=stock)
        self.assertEqual(result.parse_errors, [])
        self.assertFalse(result.has_discrepancies)
        self.assertEqual(result.selected_boxes, {"k30": "1"})

        file_obj = _xlsx(
            [
                ["Баркод товара", "Кол-во товаров"],
                ["2039698778296", 20],
            ]
        )
        result = apply_client_shipping_template(file_obj=file_obj, stock_rows=stock)
        self.assertFalse(result.has_discrepancies)
        self.assertEqual(result.selected_boxes, {"k20": "1"})

    def test_same_barcode_mix_packs_to_cover_request(self):
        """50 pcs with packs 30+20 → 1×30 + 1×20, no gap."""
        stock = [
            {
                "key": "k30",
                "sku_code": "вешалка003",
                "size": "",
                "barcode": "2039698778296",
                "box_qty": 30,
                "available_boxes": 15,
                "is_mixed_box": False,
            },
            {
                "key": "k20",
                "sku_code": "вешалка003",
                "size": "",
                "barcode": "2039698778296",
                "box_qty": 20,
                "available_boxes": 1,
                "is_mixed_box": False,
            },
        ]
        file_obj = _xlsx(
            [
                ["Баркод товара", "Кол-во товаров"],
                ["2039698778296", 50],
            ]
        )
        result = apply_client_shipping_template(file_obj=file_obj, stock_rows=stock)
        self.assertEqual(result.selected_boxes, {"k30": "1", "k20": "1"})
        self.assertEqual(sum(line.applied_qty for line in result.assembled_lines), 50)
        self.assertFalse(result.has_discrepancies)

    def test_parse_real_saved_template_headers(self):
        with TEMPLATE_PATH.open("rb") as fh:
            lines, errors = parse_client_shipping_template(fh)
        self.assertEqual(errors, ["В файле нет строк с баркодом или артикулом товара."])
        self.assertEqual(lines, [])

    def test_parse_and_apply_by_sku_only(self):
        stock = [
            {
                "key": "k1",
                "sku_code": "ART-100",
                "name": "Товар А",
                "size": "",
                "barcode": "4611111111111",
                "box_qty": 10,
                "available_boxes": 5,
                "is_mixed_box": False,
            }
        ]
        file_obj = _xlsx(
            [
                ["Артикул", "Кол-во товаров"],
                ["ART-100", 20],
            ]
        )
        result = apply_client_shipping_template(file_obj=file_obj, stock_rows=stock)
        self.assertEqual(result.parse_errors, [])
        self.assertFalse(result.has_discrepancies)
        self.assertEqual(result.selected_boxes, {"k1": "2"})
        self.assertEqual(result.assembled_lines[0].sku_code, "ART-100")

    def test_sku_casefold_and_barcode_preferred(self):
        stock = [
            {
                "key": "k-a",
                "sku_code": "MixSku",
                "size": "A",
                "barcode": "111",
                "box_qty": 5,
                "available_boxes": 10,
                "is_mixed_box": False,
            },
            {
                "key": "k-b",
                "sku_code": "MixSku",
                "size": "B",
                "barcode": "222",
                "box_qty": 5,
                "available_boxes": 10,
                "is_mixed_box": False,
            },
        ]
        # Case-insensitive article match aggregates both pack rows under sku.
        file_obj = _xlsx(
            [
                ["Артикул", "Кол-во товаров"],
                ["mixsku", 10],
            ]
        )
        result = apply_client_shipping_template(file_obj=file_obj, stock_rows=stock)
        self.assertEqual(sum(int(v) for v in result.selected_boxes.values()), 2)
        self.assertEqual(sum(line.applied_qty for line in result.assembled_lines), 10)

        # When barcode is present, prefer that specific stock row.
        file_obj = _xlsx(
            [
                ["Баркод товара", "Артикул", "Кол-во товаров"],
                ["222", "MixSku", 5],
            ]
        )
        result = apply_client_shipping_template(file_obj=file_obj, stock_rows=stock)
        self.assertEqual(result.selected_boxes, {"k-b": "1"})

    def test_merge_comment_replaces_previous_block(self):
        old = "hello\n\n" + DISCREPANCY_MARKER + "\n- old"
        merged = merge_comment_with_discrepancies(old, DISCREPANCY_MARKER + "\n- new")
        self.assertIn("hello", merged)
        self.assertIn("- new", merged)
        self.assertNotIn("- old", merged)

    def test_manager_with_client_query_uses_client_lk_form(self):
        class Agency:
            id = 2763

        rf = RequestFactory()
        request = rf.get("/shipping/new/", {"client": "2763"})
        self.assertTrue(
            _shipping_use_client_lk_form(scope="staff", request=request, selected_client=Agency())
        )
        request_plain = rf.get("/shipping/new/")
        self.assertFalse(
            _shipping_use_client_lk_form(scope="staff", request=request_plain, selected_client=Agency())
        )
        self.assertTrue(
            _shipping_use_client_lk_form(scope="client", request=request_plain, selected_client=Agency())
        )
