"""Honest Sign rules must not depend on the case of a product barcode.

Marketplace rework writes the outgoing barcode in lower case (``ozn…``)
while the client catalog keeps the original spelling (``OZN…``).  Before
the fix that difference silently turned a marked item into an unmarked
one: the loose packing screen stopped asking for a Data Matrix and every
Data Matrix the storekeeper did scan was rejected with HTTP 400
(OTG-000658 / OTG-000660, 17.09.2026).
"""
from django.test import TestCase

from shipping.models import ShippingOrder, ShippingOrderItem
from sku.models import Agency, SKU, SKUBarcode
from sklad.services.warehouse_write_path import WarehouseWritePathService

CATALOG_BARCODE = "OZN1769869289"
STOCK_BARCODE = "ozn1769869289"


class MarkingBarcodeCaseTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Клиент ЧЗ")
        self.marked_sku = SKU.objects.create(
            sku_code="091124_boardbear",
            name="Мольберт детский",
            agency=self.agency,
            honest_sign=True,
        )
        SKUBarcode.objects.create(sku=self.marked_sku, value=CATALOG_BARCODE, size="0")
        self.order = ShippingOrder.objects.create(
            number="OTG-CASE-1",
            agency=self.agency,
            status=ShippingOrder.STATUS_PICKING,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
        )

    def _add_item(self, barcode, sku_code=None, qty=4):
        return ShippingOrderItem.objects.create(
            order=self.order,
            sku_code=sku_code or self.marked_sku.sku_code,
            name=self.marked_sku.name,
            size="0",
            barcode=barcode,
            goods_type="gv",
            qty_requested=qty,
            qty_reserved=qty,
        )

    def test_loose_packing_requires_scan_for_lowercased_catalog_barcode(self):
        self._add_item(STOCK_BARCODE)

        required = WarehouseWritePathService._required_loose_packing_marking_barcodes(
            agency=self.agency,
            order_id=self.order.number,
            barcodes={STOCK_BARCODE},
        )

        # Answered in the caller's spelling, otherwise the `in` check downstream
        # still misses and every Data Matrix scan is refused.
        self.assertEqual(required, {STOCK_BARCODE})

    def test_partial_picking_requires_scan_for_lowercased_catalog_barcode(self):
        self._add_item(STOCK_BARCODE)

        required = WarehouseWritePathService._required_marking_barcodes_for_shipping(
            agency=self.agency,
            order_id=self.order.number,
            barcodes={STOCK_BARCODE},
        )

        self.assertEqual(required, {STOCK_BARCODE})

    def test_required_set_covers_both_spellings_in_one_order(self):
        self._add_item(STOCK_BARCODE)
        self._add_item(CATALOG_BARCODE)

        required = WarehouseWritePathService._required_loose_packing_marking_barcodes(
            agency=self.agency,
            order_id=self.order.number,
        )

        self.assertEqual(required, {STOCK_BARCODE, CATALOG_BARCODE})

    def test_data_matrix_scan_is_no_longer_refused_as_not_required(self):
        self._add_item(STOCK_BARCODE)

        with self.assertRaises(ValueError) as caught:
            WarehouseWritePathService.validate_loose_shipping_marking_scan(
                agency=self.agency,
                order_id=self.order.number,
                barcode=STOCK_BARCODE,
                marking_code="0104621320255015215Abc",
            )

        # It may still fail later (format or missing stock), but never again
        # with "this item does not need a Data Matrix in this order".
        self.assertNotIn("не требует сканирования", str(caught.exception))

    def test_unmarked_sku_is_not_pulled_in_by_case_insensitive_match(self):
        plain_sku = SKU.objects.create(
            sku_code="010225_grayset",
            name="Стол и стул",
            agency=self.agency,
            honest_sign=False,
        )
        SKUBarcode.objects.create(sku=plain_sku, value="OZN1851453517", size="0")
        self._add_item("ozn1851453517", sku_code=plain_sku.sku_code)

        required = WarehouseWritePathService._required_loose_packing_marking_barcodes(
            agency=self.agency,
            order_id=self.order.number,
            barcodes={"ozn1851453517"},
        )

        self.assertEqual(required, set())

    def test_another_client_catalog_does_not_leak_the_marking_rule(self):
        other = Agency.objects.create(agn_name="Другой клиент")
        other_sku = SKU.objects.create(
            sku_code="foreign", name="Чужой", agency=other, honest_sign=True
        )
        SKUBarcode.objects.create(sku=other_sku, value="OZN9999999999", size="0")
        self._add_item("ozn9999999999", sku_code="foreign")

        required = WarehouseWritePathService._required_loose_packing_marking_barcodes(
            agency=self.agency,
            order_id=self.order.number,
            barcodes={"ozn9999999999"},
        )

        self.assertEqual(required, set())
