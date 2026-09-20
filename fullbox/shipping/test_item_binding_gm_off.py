from django.test import TestCase, override_settings

from audit.models import OrderAuditEntry
from sku.models import Agency, Market

from .item_binding import (
    enforced_marketplace_item_binding_errors,
    shipping_final_truth_rows,
    validate_marketplace_item_binding,
)
from .models import ShippingOrder, ShippingOrderItem


@override_settings(SHIPPING_OZON_GM_BINDING_ENABLED=False)
class OzonGmBindingDisabledTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Ozon GM disabled client")
        self.ozon = Market.objects.create(id=9983, name="Ozon GM disabled")

    def _order(self, number: str) -> ShippingOrder:
        order = ShippingOrder.objects.create(
            number=number,
            agency=self.agency,
            marketplace=self.ozon,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            status=ShippingOrder.STATUS_PICKING,
        )
        ShippingOrderItem.objects.create(
            order=order,
            sku_code="SKU-GM-OFF",
            name="Ozon item",
            barcode="460000000001",
            qty_requested=20,
        )
        return order

    @staticmethod
    def _box(barcode: str) -> list[dict]:
        return [
            {
                "box_code": "FULLBOX-FACT-BOX",
                "items": [
                    {
                        "sku_code": "SKU-GM-OFF",
                        "barcode": barcode,
                        "qty": 20,
                    }
                ],
            }
        ]

    def test_exact_request_and_scan_pass_without_gm_assignment(self):
        order = self._order("OTG-GM-OFF-EXACT")
        boxes = self._box("460000000001")
        OrderAuditEntry.objects.create(
            order_type="shipping",
            order_id=order.number,
            action="create",
            agency=self.agency,
            payload={
                "gm_cargoes": [
                    {
                        "gm_barcode": "GM-MISMATCH-IGNORED",
                        "items": [
                            {
                                "barcode": "460000000999",
                                "quantity": 99,
                            }
                        ],
                    }
                ]
            },
        )

        result = validate_marketplace_item_binding(order, boxes=boxes)
        rows = shipping_final_truth_rows(order, result=result, boxes=boxes)

        self.assertTrue(result["ready"], result["errors"])
        self.assertFalse(result["gm_binding_enabled"])
        self.assertEqual(result["gm_totals"], {})
        self.assertEqual(result["gm_box_assignments"], {})
        self.assertEqual(
            enforced_marketplace_item_binding_errors(order, boxes=boxes),
            [],
        )
        self.assertEqual(rows[0]["barcode"], "460000000001")
        self.assertEqual(rows[0]["qty_requested"], 20)
        self.assertEqual(rows[0]["qty_shipped"], 20)

    def test_other_scanned_product_stays_blocked(self):
        order = self._order("OTG-GM-OFF-WRONG-FACT")
        boxes = self._box("460000000999")

        result = validate_marketplace_item_binding(order, boxes=boxes)
        errors = enforced_marketplace_item_binding_errors(order, boxes=boxes)

        self.assertFalse(result["ready"])
        self.assertTrue(any("460000000001" in error for error in result["errors"]))
        self.assertTrue(any("460000000999" in error for error in result["errors"]))
        self.assertTrue(errors)
        self.assertFalse(any("ШК ГМ" in error for error in errors))
