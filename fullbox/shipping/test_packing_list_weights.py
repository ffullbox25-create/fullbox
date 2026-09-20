from decimal import Decimal

from django.test import TestCase

from shipping.models import ShippingOrder
from shipping.web_ui import (
    _shipping_packing_list_box_summary_rows,
    _shipping_packing_list_enrich_box_rows,
)
from sklad.models import WarehouseContainer
from sku.models import Agency, SKU, SKUBarcode


class ShippingPackingListWeightTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Packing list client")
        self.order = ShippingOrder.objects.create(number="OTG-PACKING-WEIGHT", agency=self.agency)

    def test_enriches_box_with_product_and_container_weight(self):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="ARTICLE-1",
            name="Товар",
            weight_kg=Decimal("0.250"),
        )
        SKUBarcode.objects.create(sku=sku, value="460000000001", is_primary=True)
        WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="BOX-1",
            gross_weight_g=1350,
            width_mm=400,
            depth_mm=300,
            height_mm=200,
        )

        rows = _shipping_packing_list_enrich_box_rows(
            self.order,
            [
                {
                    "pallet": "PALLET-1",
                    "box": "BOX-1",
                    "article": "ARTICLE-1",
                    "name": "Товар",
                    "barcode": "460000000001",
                    "qty": 4,
                    "expiry": "",
                }
            ],
        )

        self.assertEqual(rows[0]["product_weight_kg"], Decimal("1.000"))
        self.assertEqual(rows[0]["box_gross_weight_kg"], Decimal("1.35"))
        self.assertEqual(rows[0]["dimensions_mm"], "400 x 300 x 200")
        self.assertEqual(rows[0]["volume_m3"], Decimal("0.024"))

    def test_missing_weight_stays_empty_and_is_not_invented(self):
        rows = _shipping_packing_list_enrich_box_rows(
            self.order,
            [
                {
                    "pallet": "-",
                    "box": "BOX-WITHOUT-WEIGHT",
                    "article": "UNKNOWN",
                    "name": "",
                    "barcode": "",
                    "qty": 3,
                    "expiry": "",
                }
            ],
        )

        self.assertIsNone(rows[0]["product_weight_kg"])
        self.assertIsNone(rows[0]["box_gross_weight_kg"])

    def test_summary_keeps_one_row_per_physical_box(self):
        summary = _shipping_packing_list_box_summary_rows(
            [
                {
                    "pallet": "PALLET-1",
                    "box": "MIX-BOX",
                    "article": "A-1",
                    "name": "Первый товар",
                    "barcode": "111",
                    "qty": 2,
                    "product_weight_kg": Decimal("0.400"),
                    "box_gross_weight_kg": Decimal("1.500"),
                    "dimensions_mm": "400 x 300 x 200",
                    "volume_m3": Decimal("0.024"),
                },
                {
                    "pallet": "PALLET-1",
                    "box": "MIX-BOX",
                    "article": "A-2",
                    "name": "Второй товар",
                    "barcode": "222",
                    "qty": 3,
                    "product_weight_kg": Decimal("0.600"),
                    "box_gross_weight_kg": Decimal("1.500"),
                    "dimensions_mm": "400 x 300 x 200",
                    "volume_m3": Decimal("0.024"),
                },
            ]
        )

        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["qty"], 5)
        self.assertEqual(summary[0]["product_weight_kg"], Decimal("1.000"))
        self.assertIn("A-1", summary[0]["contents"])
        self.assertIn("A-2", summary[0]["contents"])
