"""Ozon single-order distribution tests (MVP). Legacy name kept for discoverability."""

from io import BytesIO
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from openpyxl import Workbook

from shipping.distribution import compute_distribution_status, validate_ozon_distribution_for_submit
from shipping.models import (
    ShippingDestination,
    ShippingDestinationItem,
    ShippingOrder,
    ShippingOrderItem,
)
from shipping.ozon_template import (
    OZON_TEMPLATE_HEADERS,
    apply_ozon_shipping_template_by_warehouse,
    build_ozon_parent_comment,
)
from shipping.services import (
    _create_ozon_batch_shipping_orders,
    build_shipping_detail_page_context,
    build_shipping_list_page_context,
)
from sku.models import Agency, Market


User = get_user_model()


def _xlsx(rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Состав ГМ поставки"
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


class OzonSingleOrderDistributionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="ozon_dist_tester", password="x")
        cls.agency = Agency.objects.create(agn_name="Ozon Dist Agency", portal_user=cls.user)
        cls.ozon = Market.objects.create(id=9011, name="Ozon")
        cls.wb = Market.objects.create(id=9012, name="WB")

    def _stock(self):
        return [
            {
                "key": "k30",
                "sku_code": "SKU-1",
                "name": "Item",
                "size": "",
                "barcode": "2039698778296",
                "goods_type": "",
                "box_qty": 30,
                "available_boxes": 20,
                "is_mixed_box": False,
            }
        ]

    def _ozon_result(self):
        file_obj = _xlsx(
            [
                list(OZON_TEMPLATE_HEADERS),
                ["2039698778296", "SKU-1", 60, "Склад А", "GM-A", ""],
                ["2039698778296", "SKU-1", 30, "Склад Б", "GM-B", ""],
            ]
        )
        return apply_ozon_shipping_template_by_warehouse(
            file_obj=file_obj,
            stock_rows=self._stock(),
            file_name="ozon-demo.xlsx",
        )

    def _fake_form(self):
        form = MagicMock()
        form.cleaned_data = {
            "comment": "клиентский коммент",
            "marketplace": self.ozon,
            "transit_address": "Пушкино",
            "supply_number": "OZ-100",
            "wb_supply_barcode": "",
        }
        proto = ShippingOrder(
            agency=self.agency,
            created_by=self.user,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            marketplace=self.ozon,
            supply_type=ShippingOrder.SUPPLY_BOX,
            vehicle_type=ShippingOrder.VEHICLE_FULFILLMENT,
            comment="клиентский коммент",
            transit_address="Пушкино",
            wb_transit_warehouse=True,
            supply_number="OZ-100",
        )
        form.save = MagicMock(return_value=proto)
        return form

    @patch("shipping.services.ensure_manager_review_task")
    @patch("shipping.services.apply_client_shipping_reserve_on_submit")
    def test_create_single_order_with_destinations(self, _reserve, _task):
        request = RequestFactory().post("/")
        request.user = self.user
        order = _create_ozon_batch_shipping_orders(
            request=request,
            form=self._fake_form(),
            selected_client=self.agency,
            stock_rows=self._stock(),
            ozon_result=self._ozon_result(),
            scope="client",
            edit_order=None,
        )
        self.assertIsNone(order.parent_id)
        self.assertEqual(order.child_orders.count(), 0)
        destinations = list(order.destinations.order_by("warehouse_name"))
        self.assertEqual(len(destinations), 2)
        self.assertEqual(destinations[0].warehouse_name, "Склад А")
        self.assertEqual(destinations[1].warehouse_name, "Склад Б")
        self.assertEqual(destinations[0].planned_boxes, 2)
        self.assertEqual(destinations[1].planned_boxes, 1)
        self.assertEqual(order.expected_boxes, 3)
        self.assertEqual(order.transit_address, "Пушкино")
        self.assertEqual(order.supply_number, "OZ-100")
        self.assertEqual(order.distribution_status, ShippingOrder.DISTRIBUTION_COMPLETE)
        self.assertEqual(ShippingOrderItem.objects.filter(order=order).count(), 1)
        self.assertEqual(ShippingOrderItem.objects.get(order=order).qty_requested, 90)
        self.assertEqual(ShippingDestinationItem.objects.filter(destination__order=order).count(), 2)

    @patch("shipping.services.ensure_manager_review_task")
    @patch("shipping.services.apply_client_shipping_reserve_on_submit")
    def test_list_shows_single_order_badge(self, _reserve, _task):
        request = RequestFactory().post("/")
        request.user = self.user
        order = _create_ozon_batch_shipping_orders(
            request=request,
            form=self._fake_form(),
            selected_client=self.agency,
            stock_rows=self._stock(),
            ozon_result=self._ozon_result(),
            scope="client",
            edit_order=None,
        )
        list_request = RequestFactory().get("/")
        list_request.user = self.user
        context = build_shipping_list_page_context(
            request=list_request,
            scope="client",
            role=None,
            client_agency=self.agency,
        )
        numbers = {o.number for o in context["orders"]}
        self.assertIn(order.number, numbers)
        shown = next(o for o in context["orders"] if o.number == order.number)
        self.assertTrue(shown.is_ozon_parent)
        self.assertEqual(shown.ozon_child_count, 2)
        self.assertEqual(shown.ozon_transit, "Пушкино")

    @patch("shipping.services.ensure_manager_review_task")
    @patch("shipping.services.apply_client_shipping_reserve_on_submit")
    def test_detail_has_distribution_tables(self, _reserve, _task):
        request = RequestFactory().post("/")
        request.user = self.user
        order = _create_ozon_batch_shipping_orders(
            request=request,
            form=self._fake_form(),
            selected_client=self.agency,
            stock_rows=self._stock(),
            ozon_result=self._ozon_result(),
            scope="client",
            edit_order=None,
        )
        detail_request = RequestFactory().get("/")
        detail_request.user = self.user
        context = build_shipping_detail_page_context(
            request=detail_request,
            order=order,
            scope="client",
            role=None,
        )
        self.assertTrue(context["ozon_has_distribution"])
        self.assertEqual(len(context["ozon_directions"]), 2)
        warehouses = {row["warehouse"] for row in context["ozon_directions"]}
        self.assertEqual(warehouses, {"Склад А", "Склад Б"})
        matrix = context["ozon_distribution_matrix"]
        self.assertIsNotNone(matrix)
        self.assertEqual(len(matrix["rows"]), 1)
        self.assertEqual(matrix["rows"][0]["undistributed"], 0)

    def test_partial_distribution_blocks_submit(self):
        order = ShippingOrder.objects.create(
            number="SO-DIST-1",
            agency=self.agency,
            created_by=self.user,
            marketplace=self.ozon,
            status=ShippingOrder.STATUS_DRAFT,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            transit_address="Пушкино",
            supply_number="OZ-1",
            expected_boxes=2,
        )
        item = ShippingOrderItem.objects.create(
            order=order,
            sku_code="SKU-1",
            name="Item",
            barcode="1",
            qty_requested=100,
        )
        dest = ShippingDestination.objects.create(
            order=order,
            warehouse_name="Хоругвино",
            planned_boxes=2,
            planned_units=90,
        )
        ShippingDestinationItem.objects.create(destination=dest, order_item=item, quantity=90)
        result = compute_distribution_status(order)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, ShippingOrder.DISTRIBUTION_PARTIAL)
        self.assertEqual(result.undistributed_units, 10)
        with self.assertRaises(ValidationError):
            validate_ozon_distribution_for_submit(order)

    def test_boxes_mismatch_blocks_submit(self):
        order = ShippingOrder.objects.create(
            number="SO-DIST-2",
            agency=self.agency,
            created_by=self.user,
            marketplace=self.ozon,
            status=ShippingOrder.STATUS_DRAFT,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            transit_address="Пушкино",
            supply_number="OZ-2",
            expected_boxes=5,
        )
        item = ShippingOrderItem.objects.create(
            order=order,
            sku_code="SKU-1",
            name="Item",
            barcode="1",
            qty_requested=100,
        )
        dest = ShippingDestination.objects.create(
            order=order,
            warehouse_name="Хоругвино",
            planned_boxes=4,
            planned_units=100,
        )
        ShippingDestinationItem.objects.create(destination=dest, order_item=item, quantity=100)
        result = compute_distribution_status(order)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, ShippingOrder.DISTRIBUTION_MISMATCH)

    def test_wb_order_has_no_distribution_requirement(self):
        order = ShippingOrder.objects.create(
            number="SO-WB-1",
            agency=self.agency,
            created_by=self.user,
            marketplace=self.wb,
            status=ShippingOrder.STATUS_DRAFT,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            destination_warehouse="Коледино",
            expected_boxes=3,
        )
        ShippingOrderItem.objects.create(
            order=order,
            sku_code="SKU-WB",
            name="WB Item",
            barcode="2",
            qty_requested=30,
        )
        # Should not raise for WB
        validate_ozon_distribution_for_submit(order)

    def test_parent_comment_helper(self):
        from shipping.ozon_template import OzonWarehouseGroup

        groups = [
            OzonWarehouseGroup(warehouse="A", boxes_total=2, qty_total=60, gm_bindings=[{"gm_barcode": "G1"}]),
            OzonWarehouseGroup(warehouse="B", boxes_total=1, qty_total=30),
        ]
        text = build_ozon_parent_comment(
            user_comment="hi",
            batch_id="abc",
            warehouses=["A", "B"],
            groups=groups,
        )
        self.assertIn("[OZON-BATCH:abc]", text)
        self.assertIn("Склады назначения: A, B", text)
        self.assertIn("одна заявка", text.lower())


# Keep old class name as alias for any external references
OzonParentChildOrderTests = OzonSingleOrderDistributionTests
