import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from sku.models import Agency

from .ozon_supplies import apply_ozon_bundle_to_stock


def _stock(key: str, *, barcode: str, box_qty: int, boxes: int) -> dict:
    return {
        "key": key,
        "barcode": barcode,
        "sku_code": "SKU-1",
        "name": "Товар",
        "size": "",
        "goods_type": "gv",
        "box_qty": box_qty,
        "available_boxes": boxes,
        "is_mixed_box": False,
    }


class OzonMassWholeBoxSelectionTests(SimpleTestCase):
    def test_batch_endpoint_requires_login(self):
        response = self.client.get(reverse("shipping:ozon-supplies-batch"))

        self.assertEqual(response.status_code, 302)

    def test_exact_allocator_combines_different_box_multiplicities(self):
        result = apply_ozon_bundle_to_stock(
            [{"offer_id": "SKU-1", "barcode": "4601", "quantity": 50}],
            [
                _stock("box-30", barcode="4601", box_qty=30, boxes=1),
                _stock("box-20", barcode="4601", box_qty=20, boxes=1),
            ],
        )

        self.assertEqual(result["composition"][0]["status"], "matched")
        self.assertEqual(result["composition"][0]["applied_qty"], 50)
        self.assertEqual(result["selected_boxes"], {"box-30": "1", "box-20": "1"})

    def test_non_multiple_is_marked_for_piece_picking(self):
        result = apply_ozon_bundle_to_stock(
            [{"offer_id": "SKU-1", "barcode": "4601", "quantity": 95}],
            [_stock("box-30", barcode="4601", box_qty=30, boxes=4)],
        )

        row = result["composition"][0]
        self.assertFalse(row["matched"])
        self.assertEqual(row["status"], "needs_split")
        self.assertEqual(row["applied_qty"], 90)
        self.assertEqual(row["remaining_qty"], 5)

    def test_shared_pool_cannot_offer_same_box_twice(self):
        shared = {}
        stock = [_stock("only-box", barcode="4601", box_qty=30, boxes=1)]

        first = apply_ozon_bundle_to_stock(
            [{"offer_id": "SKU-1", "barcode": "4601", "quantity": 30}],
            stock,
            already_selected=shared,
        )
        second = apply_ozon_bundle_to_stock(
            [{"offer_id": "SKU-1", "barcode": "4601", "quantity": 30}],
            stock,
            already_selected=shared,
        )

        self.assertEqual(first["composition"][0]["status"], "matched")
        self.assertNotEqual(second["composition"][0]["status"], "matched")
        self.assertEqual(shared, {"only-box": 1})


class OzonMassSubmitEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="ozon_mass_submit_client",
            password="test-password",
        )
        cls.agency = Agency.objects.create(
            agn_name="Ozon mass submit test agency",
            portal_user=cls.user,
        )

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse("shipping:ozon-supplies-batch")

    def _result(self, *, ready: bool) -> dict:
        order = {
            "order_id": 101,
            "order_number": "OZON-101",
            "ready_for_batch": ready,
        }
        return {
            "ok": True,
            "orders": [order],
            "errors": [],
            "ready_order_ids": [101] if ready else [],
            "selected_count": 1,
            "ready_count": 1 if ready else 0,
        }

    def _post(self, payload: dict):
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _scope_patches(self):
        return (
            patch("shipping.web_ui._request_scope", return_value=("client", None, self.agency)),
            patch("shipping.web_ui._can_write", return_value=True),
            patch("shipping.web_ui._resolve_form_agency_for_shipping", return_value=self.agency),
            patch("shipping.web_ui._shipping_stock_picker_rows", return_value=[]),
        )

    def test_preview_does_not_create_shipping_order(self):
        scope, can_write, agency, stock = self._scope_patches()
        with scope, can_write, agency, stock, patch(
            "shipping.ozon_supplies.get_ozon_supplies_batch_for_agency",
            return_value=self._result(ready=True),
        ), patch("shipping.web_ui.create_ozon_api_shipping_orders_batch") as create:
            response = self._post({"mode": "preview", "order_ids": [101]})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["ready_order_ids"], [101])
        create.assert_not_called()

    def test_submit_rejects_supply_that_requires_piece_picking(self):
        scope, can_write, agency, stock = self._scope_patches()
        with scope, can_write, agency, stock, patch(
            "shipping.ozon_supplies.get_ozon_supplies_batch_for_agency",
            return_value=self._result(ready=False),
        ), patch("shipping.web_ui.create_ozon_api_shipping_orders_batch") as create:
            response = self._post(
                {
                    "mode": "submit",
                    "order_ids": [101],
                    "ship_date": "2099-08-03",
                }
            )

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["not_ready"], ["OZON-101"])
        create.assert_not_called()

    def test_submit_passes_only_revalidated_ready_supply_to_existing_service(self):
        scope, can_write, agency, stock = self._scope_patches()
        created_order = SimpleNamespace(pk=501, number="OTG-000501")
        with scope, can_write, agency, stock, patch(
            "shipping.ozon_supplies.get_ozon_supplies_batch_for_agency",
            return_value=self._result(ready=True),
        ), patch(
            "shipping.web_ui.create_ozon_api_shipping_orders_batch",
            return_value=[created_order],
        ) as create:
            response = self._post(
                {
                    "mode": "submit",
                    "order_ids": [101],
                    "ship_date": "2099-08-03",
                    "supply_type": "box",
                    "vehicle_type": "fulfillment",
                    "comment": "Проверка массовой подачи",
                }
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["created"],
            [{"id": 501, "number": "OTG-000501"}],
        )
        create.assert_called_once()
        call = create.call_args.kwargs
        self.assertEqual(call["agency"], self.agency)
        self.assertEqual(call["ship_date"].isoformat(), "2099-08-03")
        self.assertEqual([row["order_id"] for row in call["payloads"]], [101])
