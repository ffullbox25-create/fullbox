import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import RequestFactory, SimpleTestCase

from shipping.models import ShippingOrder
from shipping.services import (
    _combine_live_ozon_supply_payloads,
    _normalize_client_ozon_rework_eta_post,
    _reconcile_ozon_gm_meta_with_selected_rows,
    _refresh_client_ozon_post_from_api,
)


class ClientOzonReworkEtaNormalizationTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.order = SimpleNamespace(
            pk=569,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            marketplace=SimpleNamespace(name="Ozon"),
        )
        self.now_after_deadline = datetime(
            2026,
            9,
            2,
            12,
            0,
            tzinfo=ZoneInfo("Europe/Moscow"),
        )

    def normalize(self, data, *, scope="client", action_name="update", autosave=False):
        request = self.factory.post("/shipping/new/", data=data)
        with patch("shipping.services.timezone.localtime", return_value=self.now_after_deadline):
            changed = _normalize_client_ozon_rework_eta_post(
                request,
                scope=scope,
                edit_order=self.order,
                action_name=action_name,
                autosave=autosave,
            )
        return changed, request

    def test_stale_hidden_eta_is_replaced_with_valid_ozon_slot(self):
        changed, request = self.normalize(
            {
                "slot_date": "2026-09-04",
                "eta_at": "2026-09-02T00:00",
            }
        )

        self.assertTrue(changed)
        self.assertEqual(request.POST["eta_at"], "2026-09-04T00:00")

    def test_missing_hidden_eta_is_replaced_with_valid_ozon_slot(self):
        changed, request = self.normalize(
            {
                "slot_date": "2026-09-04",
                "eta_at": "",
            }
        )

        self.assertTrue(changed)
        self.assertEqual(request.POST["eta_at"], "2026-09-04T00:00")

    def test_implicit_final_submit_is_normalized(self):
        changed, request = self.normalize(
            {"slot_date": "2026-09-04", "eta_at": "2026-09-02T00:00"},
            action_name="",
        )

        self.assertTrue(changed)
        self.assertEqual(request.POST["eta_at"], "2026-09-04T00:00")

    def test_valid_ship_date_before_slot_is_preserved(self):
        changed, request = self.normalize(
            {
                "slot_date": "2026-09-05",
                "eta_at": "2026-09-04T00:00",
            }
        )

        self.assertFalse(changed)
        self.assertEqual(request.POST["eta_at"], "2026-09-04T00:00")

    def test_invalid_slot_is_left_for_form_validation(self):
        changed, request = self.normalize(
            {
                "slot_date": "2026-09-03",
                "eta_at": "2026-09-02T00:00",
            }
        )

        self.assertFalse(changed)
        self.assertEqual(request.POST["eta_at"], "2026-09-02T00:00")

    def test_staff_and_autosave_posts_are_not_rewritten(self):
        staff_changed, staff_request = self.normalize(
            {"slot_date": "2026-09-04", "eta_at": "2026-09-02T00:00"},
            scope="staff",
        )
        autosave_changed, autosave_request = self.normalize(
            {"slot_date": "2026-09-04", "eta_at": "2026-09-02T00:00"},
            autosave=True,
        )

        self.assertFalse(staff_changed)
        self.assertFalse(autosave_changed)
        self.assertEqual(staff_request.POST["eta_at"], "2026-09-02T00:00")
        self.assertEqual(autosave_request.POST["eta_at"], "2026-09-02T00:00")


class ClientOzonLiveSourceRefreshTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.agency = SimpleNamespace(id=39)
        self.order = SimpleNamespace(
            pk=569,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            marketplace=SimpleNamespace(name="Ozon"),
            supply_number="2000064968819",
            shipping_barcode="2000064968819",
            wb_supply_barcode="",
            comment="",
        )
        self.stock_rows = [{"key": "stock:1"}, {"key": "stock:2"}]

    def request(self, *, order_ids=None, path="/shipping/new/"):
        ids = list(order_ids or [118001])
        payload = {
            "order_id": str(ids[0]),
            "order_ids": [str(value) for value in ids],
            "order_number": "2000064968819",
            "supply_numbers": ["2000064968819"],
            "form": {
                "supply_number": "2000064968819",
                "slot_date": "2026-09-04",
                "slot_time": "11:00",
            },
        }
        return self.factory.post(
            path,
            data={
                "action": "update",
                "delivery_type": ShippingOrder.DELIVERY_MARKETPLACE,
                "marketplace": "Ozon",
                "ozon_supply_payload_json": json.dumps(payload),
                "shipping_barcode": "OLD",
                "supply_number": "2000064968819",
                "slot_date": "2026-09-04",
                "slot_time": "11:00",
                "eta_at": "2026-09-02T00:00",
                "stock_key_all[]": ["stock:1", "stock:2"],
                "stock_boxes[]": ["9", "9"],
            },
        )

    @staticmethod
    def live_payload(
        *,
        order_id=118001,
        order_number="2000064968819",
        stock_key="stock:1",
        gm_barcode="GM-12",
    ):
        return {
            "ok": True,
            "order_id": order_id,
            "order_number": order_number,
            "state": "DATA_FILLING",
            "form": {
                "shipping_barcode": gm_barcode,
                "supply_number": order_number,
                "slot_date": "2026-09-12",
                "slot_time": "16:00",
                "wb_transit_warehouse": True,
                "transit_address": "СТАРАЯ_КУПАВНА_28",
                "destination_warehouse": "",
                "vehicle_number": "A123BC77",
                "driver_phone": "+79000000000",
                "eta_date": "2026-09-12",
                "ozon_gm_comment": f"ШК ГМ Ozon: {gm_barcode}",
            },
            "destination_warehouses": [],
            "items": [
                {
                    "offer_id": "SKU-1",
                    "barcode": "4600000000001",
                    "quantity": 5,
                }
            ],
            "composition": [
                {
                    "offer_id": "SKU-1",
                    "barcode": "4600000000001",
                    "quantity": 5,
                    "matched": True,
                }
            ],
            "gm_cargoes": [
                {
                    "gm_barcode": gm_barcode,
                    "items": [
                        {
                            "offer_id": "SKU-1",
                            "barcode": "4600000000001",
                            "quantity": 5,
                        }
                    ],
                }
            ],
            "gm_barcodes": [gm_barcode],
            "ozon_boxes_total": 1,
            "selected_boxes": {stock_key: "1"},
            "partial_box_splits": [],
            "piece_pick_total_qty": 0,
            "stock_match": {
                "status": "full",
                "total": 1,
                "matched": 1,
                "missing": 0,
            },
            "discrepancies": [],
        }

    def refresh(self, request, *, scope="client", autosave=False):
        return _refresh_client_ozon_post_from_api(
            request,
            scope=scope,
            selected_client=self.agency,
            edit_order=self.order,
            action_name="update",
            autosave=autosave,
            stock_rows=self.stock_rows,
        )

    @patch("shipping.services._saved_ozon_api_intake_meta", return_value={})
    @patch("shipping.ozon_supplies.get_ozon_supply_for_agency")
    def test_final_post_is_rebuilt_from_current_ozon_source(self, get_supply, _saved_meta):
        get_supply.return_value = self.live_payload()
        request = self.request()

        changed, error = self.refresh(request)

        self.assertTrue(changed)
        self.assertEqual(error, "")
        self.assertEqual(request.POST["slot_date"], "2026-09-12")
        self.assertEqual(request.POST["slot_time"], "16:00")
        self.assertEqual(request.POST["eta_at"], "2026-09-12T00:00")
        self.assertEqual(request.POST["supply_number"], "2000064968819")
        self.assertEqual(request.POST["shipping_barcode"], "GM-12")
        self.assertEqual(request.POST["transit_address"], "СТАРАЯ_КУПАВНА_28")
        self.assertEqual(request.POST["wb_transit_warehouse"], "1")
        self.assertEqual(request.POST.getlist("stock_key_all[]"), ["stock:1", "stock:2"])
        self.assertEqual(request.POST.getlist("stock_boxes[]"), ["1", "0"])
        refreshed_payload = json.loads(request.POST["ozon_supply_payload_json"])
        self.assertEqual(refreshed_payload["gm_barcodes"], ["GM-12"])
        self.assertEqual(refreshed_payload["composition"][0]["quantity"], 5)
        get_supply.assert_called_once_with(
            self.agency,
            118001,
            stock_rows=self.stock_rows,
            enrich_gm=True,
            api_timeout=10,
            bundle_max_pages=4,
            include_gm_cargoes=True,
            gm_enrich_limit=50,
            exclude_order_id=569,
            already_selected={},
        )

    @patch("shipping.services._saved_ozon_api_intake_meta", return_value={})
    @patch("shipping.ozon_supplies.get_ozon_supply_for_agency")
    def test_ozon_api_failure_blocks_submit_without_guessing(self, get_supply, _saved_meta):
        get_supply.return_value = {"ok": False, "error": "timeout"}
        request = self.request()

        changed, error = self.refresh(request)

        self.assertFalse(changed)
        self.assertIn("не сохранена", error)
        self.assertEqual(request.POST["slot_date"], "2026-09-04")
        self.assertEqual(request.POST["eta_at"], "2026-09-02T00:00")

    @patch("shipping.services._saved_ozon_api_intake_meta", return_value={})
    @patch("shipping.ozon_supplies.get_ozon_supply_for_agency")
    def test_incomplete_gm_snapshot_blocks_submit(self, get_supply, _saved_meta):
        payload = self.live_payload()
        payload["gm_cargoes"][0]["items"] = []
        get_supply.return_value = payload
        request = self.request()

        changed, error = self.refresh(request)

        self.assertFalse(changed)
        self.assertIn("полный состав", error)
        self.assertEqual(request.POST["slot_date"], "2026-09-04")

    @patch("shipping.ozon_supplies.get_ozon_supply_for_agency")
    def test_staff_and_autosave_posts_do_not_call_ozon(self, get_supply):
        staff_changed, staff_error = self.refresh(self.request(), scope="staff")
        autosave_changed, autosave_error = self.refresh(self.request(), autosave=True)

        self.assertFalse(staff_changed)
        self.assertFalse(autosave_changed)
        self.assertEqual(staff_error, "")
        self.assertEqual(autosave_error, "")
        get_supply.assert_not_called()

    @patch("shipping.services._saved_ozon_api_intake_meta", return_value={})
    @patch("shipping.ozon_supplies.get_ozon_supply_for_agency")
    def test_staff_using_client_lk_gets_same_final_ozon_refresh(
        self,
        get_supply,
        _saved_meta,
    ):
        get_supply.return_value = self.live_payload()
        request = self.request(path="/shipping/new/?client=39")

        changed, error = self.refresh(request, scope="staff")

        self.assertTrue(changed)
        self.assertEqual(error, "")
        self.assertEqual(request.POST["slot_date"], "2026-09-12")
        self.assertEqual(request.POST["slot_time"], "16:00")
        self.assertEqual(request.POST["shipping_barcode"], "GM-12")
        self.assertEqual(request.POST.getlist("stock_boxes[]"), ["1", "0"])
        get_supply.assert_called_once()

    def test_combined_payload_keeps_each_supply_and_sums_selections(self):
        first = self.live_payload()
        second = self.live_payload(
            order_id=118002,
            order_number="2000064968820",
            stock_key="stock:2",
            gm_barcode="GM-13",
        )

        combined = _combine_live_ozon_supply_payloads([first, second])

        self.assertEqual(combined["order_ids"], ["118001", "118002"])
        self.assertEqual(
            combined["supply_numbers"],
            ["2000064968819", "2000064968820"],
        )
        self.assertEqual(combined["ozon_boxes_total"], 2)
        self.assertEqual(combined["gm_barcodes"], ["GM-12", "GM-13"])
        self.assertEqual(combined["selected_boxes"], {"stock:1": "1", "stock:2": "1"})
        self.assertEqual(len(combined["composition"]), 2)


class ClientOzonGmBarcodeAlignmentTests(SimpleTestCase):
    def reconcile(self, gm_barcode):
        cargoes = [
            {
                "gm_barcode": "GM-1",
                "items": [
                    {
                        "offer_id": "подставка005",
                        "barcode": gm_barcode,
                        "name": "Подставка для цветов",
                        "quantity": 5,
                    }
                ],
            }
        ]
        meta = {
            "gm_cargoes": cargoes,
            "gm_barcodes": ["GM-1"],
            "boxes_total": 1,
            "payload": {
                "gm_cargoes": cargoes,
                "gm_barcodes": ["GM-1"],
                "ozon_boxes_total": 1,
                "form": {},
            },
        }
        return _reconcile_ozon_gm_meta_with_selected_rows(
            agency=SimpleNamespace(id=5),
            meta=meta,
            selected_rows=[
                {
                    "sku_code": "подставка005",
                    "barcode": "2055061006940",
                    "qty_requested": 5,
                }
            ],
        )

    def test_exact_offer_id_aligns_different_nonempty_ozon_gm_barcode(self):
        result, errors = self.reconcile("2045230785073")

        self.assertEqual(errors, [])
        self.assertEqual(
            result["gm_cargoes"][0]["items"][0]["barcode"],
            "2055061006940",
        )
        self.assertEqual(
            result["payload"]["gm_cargoes"][0]["items"][0]["barcode"],
            "2055061006940",
        )
        self.assertEqual(result["composition"][0]["barcode"], "2055061006940")

    def test_missing_gm_barcode_is_not_filled_from_offer_id(self):
        result, errors = self.reconcile("")

        self.assertEqual(errors, [])
        self.assertEqual(result["gm_cargoes"][0]["items"][0]["barcode"], "")
