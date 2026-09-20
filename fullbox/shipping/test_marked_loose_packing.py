from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase

from shipping.packing import (
    _shipping_loose_items,
    _shipping_normalize_loose_packing_boxes,
    _shipping_snapshot_is_loose,
)
from shipping.models import ShippingOrder, ShippingOrderItem
from sku.models import Agency, SKU, SKUBarcode
from sklad.services.warehouse_write_path import WarehouseWritePathService


class MarkedLoosePackingRuleTests(SimpleTestCase):
    def _snapshot(self, *, marking_code="DM-1"):
        return SimpleNamespace(
            id=101,
            sku_code="SKU-CZ",
            name="Маркированный товар",
            size="",
            barcode="460000000001",
            goods_type="gv",
            marking_code=marking_code,
            warehouse_state_code="in_otg",
            qty=1,
            shipping_reserved_qty=0,
            container_id=None,
            container_code="",
        )

    def test_whole_marked_box_stays_on_whole_box_route_without_rescan(self):
        snapshot = self._snapshot()
        snapshot.container_id = 501
        snapshot.container_code = "BOX-WHOLE-CZ"

        self.assertFalse(_shipping_snapshot_is_loose(snapshot))

    def test_loose_item_requires_pair_scan_without_marketplace_order_flag(self):
        order = SimpleNamespace(
            agency=SimpleNamespace(pk=1),
            number="OTG-CZ-1",
            status=ShippingOrder.STATUS_PICKING,
        )
        snapshot = self._snapshot()
        with patch("shipping.packing._shipping_loose_snapshots", return_value=[snapshot]), patch.object(
            WarehouseWritePathService,
            "_required_loose_packing_marking_barcodes",
            return_value={snapshot.barcode},
        ):
            rows = _shipping_loose_items(order)

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["requires_marking_scan"])

    def test_canceled_order_return_does_not_require_marking_rescan(self):
        order = SimpleNamespace(
            agency=SimpleNamespace(pk=1),
            number="OTG-CZ-CANCELED",
            status=ShippingOrder.STATUS_CANCELED,
        )
        snapshot = self._snapshot()
        with patch("shipping.packing._shipping_loose_snapshots", return_value=[snapshot]), patch.object(
            WarehouseWritePathService,
            "_required_loose_packing_marking_barcodes",
        ) as required_marking:
            rows = _shipping_loose_items(order)

        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["requires_marking_scan"])
        required_marking.assert_not_called()

    def test_marked_item_cannot_be_packed_without_one_data_matrix_per_unit(self):
        key = "SKU-CZ\x1fМаркированный товар\x1f\x1f460000000001\x1fgv"
        expected = {
            key: {
                "key": key,
                "sku_code": "SKU-CZ",
                "name": "Маркированный товар",
                "size": "",
                "barcode": "460000000001",
                "goods_type": "gv",
                "qty": 1,
                "requires_marking_scan": True,
            }
        }
        boxes, errors = _shipping_normalize_loose_packing_boxes(
            [
                {
                    "code": "BOX-NEW-CZ",
                    "sealed": True,
                    "items": [
                        {
                            **expected[key],
                            "marking_units": [],
                        }
                    ],
                }
            ],
            expected,
        )

        self.assertEqual(len(boxes), 1)
        self.assertTrue(any("отсканировано Data Matrix: 0 из 1" in error for error in errors))

    def test_driver_marking_rule_is_not_broadened_by_loose_packing_change(self):
        agency = SimpleNamespace(pk=1)
        order = SimpleNamespace(
            delivery_type="client_delivery",
            requires_marking_scan=False,
        )
        order_manager = MagicMock()
        order_manager.filter.return_value.only.return_value.first.return_value = order

        with patch.object(ShippingOrder, "objects", order_manager):
            required = WarehouseWritePathService._required_marking_barcodes_for_shipping(
                agency=agency,
                order_id="OTG-CZ-1",
                barcodes={"460000000001"},
            )

        self.assertEqual(required, set())

    def test_data_matrix_lookup_is_bound_to_scanned_product_barcode(self):
        agency = SimpleNamespace(pk=1)
        snapshot = self._snapshot()
        manager = MagicMock()
        query = manager.filter.return_value.order_by.return_value
        query.__getitem__.return_value = [snapshot]

        with patch.object(
            WarehouseWritePathService,
            "_required_loose_packing_marking_barcodes",
            return_value={snapshot.barcode},
        ), patch(
            "sklad.services.warehouse_write_path.WarehouseStockSnapshot.objects",
            manager,
        ):
            result = WarehouseWritePathService.validate_loose_shipping_marking_scan(
                agency=agency,
                order_id="OTG-CZ-1",
                barcode=snapshot.barcode,
                marking_code=snapshot.marking_code,
            )

        self.assertEqual(result["snapshot_id"], snapshot.id)
        manager.filter.assert_called_once_with(
            agency=agency,
            barcode=snapshot.barcode,
            marking_code=snapshot.marking_code,
            qty=1,
            is_archived=False,
            container__isnull=True,
            container_code="",
            warehouse_state_code="in_otg",
            last_event__stock_context_type="shipping",
            last_event__stock_context_id="OTG-CZ-1",
        )


class RequiredMarkingIsDrivenBySkuTests(TestCase):
    """The marking rule comes from the SKU card, not from the order form."""

    def test_required_marking_ignores_delivery_type_and_order_checkbox(self):
        agency = Agency.objects.create(agn_name="Клиент ЧЗ")
        sku = SKU.objects.create(
            sku_code="SKU-CZ", name="Маркированный товар", agency=agency, honest_sign=True
        )
        SKUBarcode.objects.create(sku=sku, value="460000000001", size="")
        order = ShippingOrder.objects.create(
            number="OTG-CZ-1",
            agency=agency,
            status=ShippingOrder.STATUS_PICKING,
            delivery_type=ShippingOrder.DELIVERY_COURIER,
            requires_marking_scan=False,
        )
        ShippingOrderItem.objects.create(
            order=order,
            sku_code=sku.sku_code,
            name=sku.name,
            size="",
            barcode="460000000001",
            goods_type="gv",
            qty_requested=1,
            qty_reserved=1,
        )

        required = WarehouseWritePathService._required_loose_packing_marking_barcodes(
            agency=agency,
            order_id=order.number,
            barcodes={"460000000001"},
        )

        self.assertEqual(required, {"460000000001"})
