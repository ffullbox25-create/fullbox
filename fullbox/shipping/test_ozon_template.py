from io import BytesIO

from django.test import SimpleTestCase
from openpyxl import Workbook

from shipping.ozon_template import (
    OZON_TEMPLATE_HEADERS,
    OZON_TEMPLATE_PATH,
    apply_ozon_shipping_template_by_warehouse,
    build_ozon_order_comment,
    build_ozon_shipping_template_response,
    items_from_selected_boxes,
    parse_ozon_shipping_template,
)


def _xlsx(rows, sheet_title="Состав ГМ поставки"):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


class OzonShippingTemplateTests(SimpleTestCase):
    def test_template_file_exists(self):
        self.assertTrue(OZON_TEMPLATE_PATH.exists(), OZON_TEMPLATE_PATH)

    def test_download_ok(self):
        response = build_ozon_shipping_template_response()
        self.assertEqual(response.status_code, 200)
        self.assertIn("ozon-shk-excel.xlsx", response.get("Content-Disposition", ""))

    def test_parse_requires_destination_warehouse(self):
        file_obj = _xlsx(
            [
                ["ШК товара", "Артикул товара", "Кол-во товаров", "ШК ГМ"],
                ["111", "A", 30, "GM1"],
            ]
        )
        _lines, errors = parse_ozon_shipping_template(file_obj)
        self.assertTrue(any("Склад назначения" in e for e in errors))

    def test_parse_accepts_legacy_zone_header(self):
        file_obj = _xlsx(
            [
                ["ШК товара", "Артикул товара", "Кол-во товаров", "Зона размещения (склад)", "ШК ГМ"],
                ["111", "A", 30, "ХОРУГВИНО_РФЦ", "GM1"],
            ]
        )
        lines, errors = parse_ozon_shipping_template(file_obj)
        self.assertEqual(errors, [])
        self.assertEqual(lines[0].warehouse, "ХОРУГВИНО_РФЦ")

    def test_empty_warehouse_row_error(self):
        file_obj = _xlsx(
            [
                list(OZON_TEMPLATE_HEADERS),
                ["111", "A", 30, "", "GM1", ""],
            ]
        )
        _lines, errors = parse_ozon_shipping_template(file_obj)
        self.assertTrue(any("Склад назначения" in e for e in errors))

    def test_split_two_warehouses_and_floor_boxes(self):
        stock = [
            {
                "key": "k30",
                "sku_code": "SKU-1",
                "size": "",
                "barcode": "2039698778296",
                "box_qty": 30,
                "available_boxes": 20,
                "is_mixed_box": False,
                "name": "Вешалка",
            }
        ]
        # WH-A: 445 → 14 boxes / 420; WH-B: 60 → 2 boxes / 60
        file_obj = _xlsx(
            [
                list(OZON_TEMPLATE_HEADERS),
                ["2039698778296", "SKU-1", 445, "Склад А", "GM-A", "Короб"],
                ["2039698778296", "SKU-1", 60, "Склад Б", "GM-B", ""],
            ]
        )
        result = apply_ozon_shipping_template_by_warehouse(file_obj=file_obj, stock_rows=stock)
        self.assertEqual(result.parse_errors, [])
        self.assertEqual(len(result.groups), 2)
        self.assertEqual(result.groups[0].warehouse, "Склад А")
        self.assertEqual(result.groups[0].selected_boxes, {"k30": "14"})
        self.assertEqual(result.groups[0].qty_total, 420)
        self.assertTrue(any("расхождение 25" in d for d in result.groups[0].discrepancies))
        self.assertEqual(result.groups[1].warehouse, "Склад Б")
        self.assertEqual(result.groups[1].selected_boxes, {"k30": "2"})
        self.assertEqual(result.groups[1].qty_total, 60)
        # Shared stock: 14+2 = 16 of 20 used
        self.assertEqual(len(result.applied_groups), 2)

    def test_stock_not_double_booked_across_warehouses(self):
        stock = [
            {
                "key": "k1",
                "sku_code": "SKU-1",
                "size": "",
                "barcode": "111",
                "box_qty": 10,
                "available_boxes": 3,
                "is_mixed_box": False,
                "name": "X",
            }
        ]
        file_obj = _xlsx(
            [
                list(OZON_TEMPLATE_HEADERS),
                ["111", "SKU-1", 30, "WH1", "", ""],  # needs 3 boxes
                ["111", "SKU-1", 30, "WH2", "", ""],  # needs 3 more — none left
            ]
        )
        result = apply_ozon_shipping_template_by_warehouse(file_obj=file_obj, stock_rows=stock)
        self.assertEqual(result.groups[0].selected_boxes, {"k1": "3"})
        self.assertEqual(result.groups[1].selected_boxes, {})
        self.assertEqual(result.groups[1].applied_lines, 0)

    def test_items_from_selected_boxes(self):
        stock = [
            {
                "key": "k1",
                "sku_code": "SKU-1",
                "name": "Name",
                "size": "",
                "barcode": "111",
                "goods_type": "",
                "box_qty": 10,
                "available_boxes": 5,
            }
        ]
        items, boxes, errors = items_from_selected_boxes(stock, {"k1": "2"})
        self.assertEqual(errors, [])
        self.assertEqual(boxes, 2)
        self.assertEqual(items[0]["qty_requested"], 20)

    def test_comment_contains_batch_and_gm(self):
        from shipping.ozon_template import OzonWarehouseGroup, ozon_meta_from_comment

        group = OzonWarehouseGroup(
            warehouse="Склад А",
            boxes_total=2,
            qty_total=60,
            gm_bindings=[
                {
                    "gm_barcode": "GM1",
                    "product_barcode": "111",
                    "sku": "SKU-1",
                    "boxes": 2,
                    "applied_qty": 60,
                    "gm_type": "Короб",
                }
            ],
            discrepancies=["gap"],
        )
        text = build_ozon_order_comment(
            user_comment="hello",
            batch_id="abc123",
            warehouse="Склад А",
            group=group,
            sibling_warehouses=["Склад А", "Склад Б"],
        )
        self.assertIn("[OZON-BATCH:abc123]", text)
        self.assertIn("Склад назначения: Склад А", text)
        self.assertIn("Склад Б", text)
        self.assertIn("Короба Ozon (ШК ГМ — клеить на наши короба)", text)
        self.assertIn("ГМ GM1", text)
        self.assertIn("[РАСХОЖДЕНИЯ ШАБЛОНА]", text)
        meta = ozon_meta_from_comment(text)
        self.assertTrue(meta["is_ozon_batch"])
        self.assertEqual(meta["batch_id"], "abc123")
        self.assertTrue(any("GM1" in line for line in meta["gm_lines"]))

    def test_gm_bindings_and_item_comment(self):
        stock = [
            {
                "key": "k1",
                "sku_code": "SKU-1",
                "name": "Name",
                "size": "",
                "barcode": "111",
                "goods_type": "",
                "box_qty": 30,
                "available_boxes": 5,
                "is_mixed_box": False,
            }
        ]
        file_obj = _xlsx(
            [
                list(OZON_TEMPLATE_HEADERS),
                ["111", "SKU-1", 30, "WH-A", "OZON-GM-99", "Короб"],
            ]
        )
        result = apply_ozon_shipping_template_by_warehouse(file_obj=file_obj, stock_rows=stock)
        group = result.groups[0]
        self.assertEqual(group.gm_barcodes(), ["OZON-GM-99"])
        items, boxes, errors = items_from_selected_boxes(
            stock,
            group.selected_boxes,
            gm_by_product_barcode=group.gm_by_product_barcode(),
        )
        self.assertEqual(errors, [])
        self.assertEqual(boxes, 1)
        self.assertIn("ШК ГМ Ozon: OZON-GM-99", items[0]["comment"])
