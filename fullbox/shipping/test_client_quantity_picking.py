import json

from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase

from . import web_ui
from .box_splits import extract_partial_box_split
from .services import (
    _client_quantity_mode_requested,
    _client_quantity_plan,
    _client_quantity_stock_groups,
    _materialize_client_quantity_selection,
)


def _stock_row(
    key,
    *,
    sku="SKU-A",
    name="Товар A",
    size="0",
    barcode="4600000000001",
    goods_type="Готовый",
    box_qty,
    boxes,
    partial_only=False,
    split_available_qty=None,
    is_mixed_box=False,
    mixed_group="",
):
    available_qty = (
        int(split_available_qty)
        if partial_only and split_available_qty is not None
        else int(box_qty) * int(boxes)
    )
    return {
        "key": key,
        "sku_id": abs(hash((sku, size, barcode))) % 100000 + 1,
        "sku_code": sku,
        "name": name,
        "size": size,
        "barcode": barcode,
        "goods_type": goods_type,
        "box_qty": int(box_qty),
        "available_boxes": int(boxes),
        "available_qty": int(available_qty),
        "box_codes": [f"INTERNAL-{key}-{index}" for index in range(1, int(boxes) + 1)],
        "is_mixed_box": bool(is_mixed_box),
        "mixed_group": mixed_group,
        "partial_only": bool(partial_only),
        "split_available_qty": int(
            split_available_qty if split_available_qty is not None else box_qty
        ),
        "is_open_remainder": bool(partial_only),
    }


class ClientQuantityPickingTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _request(self, stock_rows, selections, **extra):
        groups = _client_quantity_stock_groups(stock_rows)
        keys_by_sku = {group["sku_code"]: group["key"] for group in groups}
        data = {
            "action": "submit",
            "stock_input_mode": "quantity",
            "stock_quantity_key[]": [keys_by_sku[sku] for sku in selections],
            "stock_quantity[]": [str(quantity) for quantity in selections.values()],
            **extra,
        }
        return self.factory.post("/shipping/new/", data=data)

    def _materialize_and_parse(self, stock_rows, selections, **extra):
        request = self._request(stock_rows, selections, **extra)
        materialize_errors = _materialize_client_quantity_selection(request, stock_rows)
        selected, parse_errors = web_ui._parse_selected_stock_items(
            request,
            stock_rows,
            multiple=True,
        )
        return request, materialize_errors, selected, parse_errors

    def test_groups_multiplicities_into_one_client_product(self):
        stock_rows = [
            _stock_row("A240", box_qty=240, boxes=2),
            _stock_row("A180", box_qty=180, boxes=3),
        ]

        groups = _client_quantity_stock_groups(stock_rows)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["available_qty"], 1020)
        self.assertEqual(
            groups[0]["pack_options"],
            [
                {"multiplicity": 240, "box_count": 2},
                {"multiplicity": 180, "box_count": 3},
            ],
        )

    def test_quantity_540_uses_three_180_boxes_without_piece_pick(self):
        stock_rows = [
            _stock_row("A240", box_qty=240, boxes=2),
            _stock_row("A180", box_qty=180, boxes=3),
        ]

        request, materialize_errors, selected, parse_errors = self._materialize_and_parse(
            stock_rows,
            {"SKU-A": 540},
        )

        self.assertEqual(materialize_errors, [])
        self.assertEqual(parse_errors, [])
        self.assertEqual(request.POST.getlist("stock_boxes[]"), ["0", "3"])
        self.assertEqual(json.loads(request.POST["stock_partial_box_splits_json"]), [])
        self.assertEqual(sum(item["qty_requested"] for item in selected), 540)

    def test_quantity_100_splits_one_180_box(self):
        stock_rows = [_stock_row("A180", box_qty=180, boxes=1)]

        request, materialize_errors, selected, parse_errors = self._materialize_and_parse(
            stock_rows,
            {"SKU-A": 100},
        )

        self.assertEqual(materialize_errors, [])
        self.assertEqual(parse_errors, [])
        self.assertEqual(request.POST.getlist("stock_boxes[]"), ["0"])
        self.assertEqual(sum(item["qty_requested"] for item in selected), 100)
        self.assertEqual(extract_partial_box_split(selected[0]["comment"])["item_pick_qty"], 100)

    def test_complete_unit_containers_are_whole_boxes(self):
        stock_rows = [_stock_row("A1", box_qty=1, boxes=5)]
        group = _client_quantity_stock_groups(stock_rows)[0]

        plan = _client_quantity_plan(group, 3)
        _request, materialize_errors, selected, parse_errors = self._materialize_and_parse(
            stock_rows,
            {"SKU-A": 3},
        )

        self.assertEqual([(line.multiplicity, line.box_count) for line in plan.lines], [(1, 3)])
        self.assertEqual(plan.piece_qty, 0)
        self.assertEqual(materialize_errors, [])
        self.assertEqual(parse_errors, [])
        self.assertEqual(sum(item["qty_requested"] for item in selected), 3)

    def test_one_unit_box_alongside_5000_unit_box(self):
        stock_rows = [
            _stock_row("A5000", box_qty=5000, boxes=1),
            _stock_row("A1", box_qty=1, boxes=1),
        ]
        group = _client_quantity_stock_groups(stock_rows)[0]
        self.assertEqual(group["available_qty"], 5001)
        self.assertEqual(group["pack_options"], [
            {"multiplicity": 5000, "box_count": 1},
            {"multiplicity": 1, "box_count": 1},
        ])
        for quantity, expected_boxes, expected_lines, pieces in [
            (1, ["0", "1"], [(1, 1)], 0),
            (5000, ["1", "0"], [(5000, 1)], 0),
            (5001, ["1", "1"], [(5000, 1), (1, 1)], 0),
            (2, ["0", "1"], [(1, 1)], 1),
        ]:
            with self.subTest(quantity=quantity):
                plan = _client_quantity_plan(group, quantity)
                self.assertEqual([(line.multiplicity, line.box_count) for line in plan.lines], expected_lines)
                self.assertEqual(plan.piece_qty, pieces)
                self.assertFalse(plan.unattainable)
                request, errors, selected, parse_errors = self._materialize_and_parse(stock_rows, {"SKU-A": quantity})
                self.assertEqual(errors, [])
                self.assertEqual(parse_errors, [])
                self.assertEqual(request.POST.getlist("stock_boxes[]"), expected_boxes)
                self.assertEqual(sum(item["qty_requested"] for item in selected), quantity)
                if quantity == 1:
                    self.assertEqual(json.loads(request.POST["stock_partial_box_splits_json"]), [])
                    self.assertIn("INTERNAL-A1-1", selected[0]["comment"])
                    self.assertNotIn("INTERNAL-A5000-1", selected[0]["comment"])

    def test_partial_unit_and_mixed_unit_do_not_become_whole_boxes(self):
        for flags in [
            {"partial_only": True, "split_available_qty": 1},
            {"is_mixed_box": True, "mixed_group": "mix:1"},
        ]:
            with self.subTest(flags=flags):
                row = _stock_row("P1", box_qty=1, boxes=1, **flags)
                group = _client_quantity_stock_groups([row])[0]
                self.assertEqual(group["pack_options"], [])
                plan = _client_quantity_plan(group, 1)
                self.assertEqual(plan.lines, ())
                self.assertEqual(plan.piece_qty, 1)
                self.assertEqual(plan.missing_qty, 0)

    def test_partial_stock_does_not_inflate_unit_box_capacity(self):
        rows = [
            _stock_row("A1", box_qty=1, boxes=1),
            _stock_row("P5", box_qty=5, boxes=1, partial_only=True, split_available_qty=2),
        ]
        group = _client_quantity_stock_groups(rows)[0]
        plan = _client_quantity_plan(group, 3)
        self.assertEqual([(line.multiplicity, line.box_count) for line in plan.lines], [(1, 1)])
        self.assertEqual(plan.piece_qty, 2)
        self.assertEqual(plan.missing_qty, 0)
        request, errors, selected, parse_errors = self._materialize_and_parse(rows, {"SKU-A": 3})
        self.assertEqual(errors, [])
        self.assertEqual(parse_errors, [])
        self.assertEqual(sum(item["qty_requested"] for item in selected), 3)
        self.assertEqual(_client_quantity_plan(group, 4).missing_qty, 1)

    def test_quantity_without_physical_box_codes_is_not_a_whole_box(self):
        row = _stock_row("LOOSE", box_qty=1, boxes=3)
        row["box_codes"] = []
        group = _client_quantity_stock_groups([row])[0]
        self.assertEqual(group["pack_options"], [])
        plan = _client_quantity_plan(group, 2)
        self.assertEqual(plan.lines, ())
        self.assertEqual(plan.piece_qty, 2)
        self.assertEqual(plan.missing_qty, 0)

    def test_rejects_quantity_above_current_free_stock(self):
        stock_rows = [_stock_row("A10", box_qty=10, boxes=2)]
        request = self._request(stock_rows, {"SKU-A": 21})

        errors = _materialize_client_quantity_selection(request, stock_rows)

        self.assertEqual(len(errors), 1)
        self.assertIn("запрошено 21 шт., доступно 20 шт.", errors[0])
        self.assertEqual(request.POST.getlist("stock_boxes[]"), ["0"])
        self.assertEqual(json.loads(request.POST["stock_partial_box_splits_json"]), [])

    def test_mixed_box_is_selected_whole_only_for_its_complete_composition(self):
        stock_rows = [
            _stock_row(
                "M1",
                sku="SKU-A",
                barcode="111",
                box_qty=6,
                boxes=1,
                is_mixed_box=True,
                mixed_group="mix:1",
            ),
            _stock_row(
                "M2",
                sku="SKU-B",
                name="Товар B",
                barcode="222",
                box_qty=4,
                boxes=1,
                is_mixed_box=True,
                mixed_group="mix:1",
            ),
        ]

        request, materialize_errors, selected, parse_errors = self._materialize_and_parse(
            stock_rows,
            {"SKU-A": 6, "SKU-B": 4},
        )

        self.assertEqual(materialize_errors, [])
        self.assertEqual(parse_errors, [])
        self.assertEqual(request.POST.getlist("stock_boxes[]"), ["1", "1"])
        self.assertEqual(json.loads(request.POST["stock_partial_box_splits_json"]), [])
        self.assertEqual(
            {item["sku_code"]: item["qty_requested"] for item in selected},
            {"SKU-A": 6, "SKU-B": 4},
        )

    def test_mixed_box_partial_quantities_share_one_internal_split(self):
        stock_rows = [
            _stock_row(
                "M1",
                sku="SKU-A",
                barcode="111",
                box_qty=6,
                boxes=1,
                is_mixed_box=True,
                mixed_group="mix:1",
            ),
            _stock_row(
                "M2",
                sku="SKU-B",
                name="Товар B",
                barcode="222",
                box_qty=4,
                boxes=1,
                is_mixed_box=True,
                mixed_group="mix:1",
            ),
        ]

        request, materialize_errors, selected, parse_errors = self._materialize_and_parse(
            stock_rows,
            {"SKU-A": 2, "SKU-B": 1},
        )

        self.assertEqual(materialize_errors, [])
        self.assertEqual(parse_errors, [])
        splits = json.loads(request.POST["stock_partial_box_splits_json"])
        self.assertEqual(len(splits), 1)
        self.assertEqual(
            {item["key"]: item["qty"] for item in splits[0]["items"]},
            {"M1": 2, "M2": 1},
        )
        self.assertEqual(
            {item["sku_code"]: item["qty_requested"] for item in selected},
            {"SKU-A": 2, "SKU-B": 1},
        )

    def test_ozon_payload_keeps_existing_verified_selection_path(self):
        stock_rows = [_stock_row("A10", box_qty=10, boxes=2)]
        request = self._request(
            stock_rows,
            {"SKU-A": 10},
            ozon_supply_payload_json='{"piece_pick_total_qty":0}',
        )

        self.assertFalse(_client_quantity_mode_requested(request))

    def test_client_partial_does_not_render_internal_box_codes(self):
        row = {
            "key": "qty:public",
            "sku_code": "SKU-A",
            "name": "Товар A",
            "size": "0",
            "barcode": "4600000000001",
            "goods_type": "Готовый",
            "available_qty": 180,
            "selected_qty": "100",
            "planned_whole_boxes": 0,
            "planned_piece_qty": 100,
            "pack_options_json": '[{"multiplicity":180,"box_count":1}]',
            "source_rows": [{"box_codes": ["INTERNAL-BOX-CODE"]}],
        }

        html = render_to_string(
            "shipping/_client_quantity_picker.html",
            {
                "client_quantity_rows": [row],
                "client_quantity_piece_qty": 100,
                "client_stock_input_mode": "quantity",
            },
        )

        self.assertIn("SKU-A", html)
        self.assertIn("Поштучно: 100 шт.", html)
        self.assertIn("Часть заявки — <strong id=\"client-piece-pick-total\">100</strong> шт.", html)
        self.assertIn("потому что указанное количество нельзя набрать целыми коробами", html)
        self.assertNotIn("INTERNAL-BOX-CODE", html)

    def test_piece_pick_notice_is_non_blocking_and_hidden_for_exact_plan(self):
        html = render_to_string(
            "shipping/_client_quantity_picker.html",
            {
                "client_quantity_rows": [
                    {
                        "key": "qty:public",
                        "sku_code": "SKU-A",
                        "name": "Товар A",
                        "size": "0",
                        "barcode": "4600000000001",
                        "goods_type": "Готовый",
                        "available_qty": 540,
                        "selected_qty": "540",
                        "planned_whole_boxes": 3,
                        "planned_piece_qty": 0,
                        "pack_options_json": '[{"multiplicity":180,"box_count":3}]',
                    }
                ],
                "client_quantity_piece_qty": 0,
                "client_stock_input_mode": "quantity",
            },
        )

        self.assertIn('id="client-piece-pick-notice"', html)
        self.assertIn("hidden", html)
        self.assertNotIn("name=\"piece_pick_tariff_confirmed\"", html)
