from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase

from shipping.box_splits import extract_partial_box_split
from shipping.models import ShippingOrder, ShippingOrderItem
from shipping.services import reserve_order
from shipping.web_ui import _parse_selected_stock_items, _shipping_stock_picker_rows
from sklad.models import WarehouseEvent, WarehouseReserve
from sklad.test_utils import create_warehouse_snapshot_row
from sku.models import Agency


class ShippingPartialBoxReserveRegressionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="partial_box_manager", password="pwd")
        self.agency = Agency.objects.create(agn_name="Клиент с частичным коробом")
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_id="R-17",
            sku="SKU-001",
            name="Товар 1",
            size="42",
            barcode="200000000001",
            goods_type="Готовый",
            qty=17,
            box_code="BX-17",
            pallet_code="PL-17",
            cell=1,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_id="R-70",
            sku="SKU-001",
            name="Товар 1",
            size="42",
            barcode="200000000001",
            goods_type="Готовый",
            qty=70,
            box_code="BX-70",
            pallet_code="PL-70",
            cell=2,
        )

    def _create_order(self, number: str, items: list[dict]) -> ShippingOrder:
        order = ShippingOrder.objects.create(
            number=number,
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_SUBMITTED,
        )
        for item in items:
            ShippingOrderItem.objects.create(
                order=order,
                sku_code="SKU-001",
                name="Товар 1",
                size="42",
                barcode="200000000001",
                goods_type="Готовый",
                **item,
            )
        return order

    def test_next_order_can_take_35_from_remaining_52_without_reusing_blocked_box(self):
        blocking_order = self._create_order(
            "SO-000308",
            [
                {
                    "qty_requested": 17,
                    "comment": "Коробов: 1; кратность: 17; короба: BX-17",
                },
                {
                    "qty_requested": 18,
                    "comment": "Исходные короба: BX-70; Разбить короб: 1; отбор из короба: 18 из 70",
                },
            ],
        )
        reserve_order(blocking_order, self.user)

        # Имитируем старые pool-события: до исправления коды коробов в них не сохранялись.
        for event in WarehouseEvent.objects.filter(
            reserve__context_id=blocking_order.number,
            payload__reserve_scope="pool",
        ):
            payload = dict(event.payload or {})
            payload["box_codes"] = []
            event.payload = payload
            event.save(update_fields=["payload"])

        target_order = self._create_order(
            "SO-000309",
            [{"qty_requested": 35, "comment": ""}],
        )
        stock_rows = _shipping_stock_picker_rows(self.agency, exclude_order=target_order)
        sku_rows = [row for row in stock_rows if row["sku_code"] == "SKU-001"]
        self.assertEqual(len(sku_rows), 1)
        partial_row = sku_rows[0]
        self.assertTrue(partial_row["partial_only"])
        self.assertEqual(partial_row["box_codes"], ["BX-70"])
        self.assertEqual(partial_row["box_qty"], 70)
        self.assertEqual(partial_row["available_qty"], 52)

        over_request = RequestFactory().post(
            "/shipping/new/",
            {
                "stock_partial_box_splits_json": json.dumps(
                    [{
                        "identity": f"key:{partial_row['key']}",
                        "row_key": partial_row["key"],
                        "boxes": 1,
                        "items": [{"key": partial_row["key"], "qty": 53}],
                    }]
                )
            },
        )
        _selected, over_errors = _parse_selected_stock_items(over_request, stock_rows, multiple=True)
        self.assertTrue(any("доступно только 52" in error for error in over_errors))

        valid_request = RequestFactory().post(
            "/shipping/new/",
            {
                "stock_partial_box_splits_json": json.dumps(
                    [{
                        "identity": f"key:{partial_row['key']}",
                        "row_key": partial_row["key"],
                        "boxes": 1,
                        "items": [{"key": partial_row["key"], "qty": 35}],
                    }]
                )
            },
        )
        selected, errors = _parse_selected_stock_items(valid_request, stock_rows, multiple=True)
        self.assertEqual(errors, [])
        self.assertEqual(selected[0]["qty_requested"], 35)
        split_meta = extract_partial_box_split(selected[0]["comment"])
        self.assertEqual(split_meta["source_box_codes"], ["BX-70"])
        self.assertEqual(split_meta["source_box_total_qty"], 70)

        target_item = target_order.items.get()
        target_item.qty_requested = 1
        target_item.comment = "Коробов: 1; кратность: 17; короба: BX-17"
        target_item.save(update_fields=["qty_requested", "comment", "updated_at"])
        with self.assertRaisesRegex(ValidationError, "выбранных коробах"):
            reserve_order(target_order, self.user)

        target_item.qty_requested = selected[0]["qty_requested"]
        target_item.comment = selected[0]["comment"]
        target_item.save(update_fields=["qty_requested", "comment", "updated_at"])
        reserve_order(target_order, self.user)

        reserve_event = WarehouseEvent.objects.get(
            reserve__context_id=target_order.number,
            payload__reserve_scope="pool",
        )
        self.assertEqual(reserve_event.payload["box_codes"], ["BX-70"])
        remaining_rows = _shipping_stock_picker_rows(self.agency)
        remaining_sku_rows = [row for row in remaining_rows if row["sku_code"] == "SKU-001"]
        self.assertEqual(len(remaining_sku_rows), 1)
        self.assertEqual(remaining_sku_rows[0]["box_codes"], ["BX-70"])
        self.assertEqual(remaining_sku_rows[0]["available_qty"], 17)

    def test_processing_partial_reserve_is_not_exposed_to_shipping(self):
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="OBR-1",
            sku_code="SKU-001",
            size="42",
            barcode="200000000001",
            goods_type="Готовый",
            qty_reserved=20,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        self.assertEqual(_shipping_stock_picker_rows(self.agency), [])

    def test_fully_available_boxes_keep_regular_picker_behavior(self):
        stock_rows = _shipping_stock_picker_rows(self.agency)

        self.assertEqual(len(stock_rows), 2)
        self.assertTrue(all(not row["partial_only"] for row in stock_rows))
        self.assertEqual({row["available_qty"] for row in stock_rows}, {17, 70})
