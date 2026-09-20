from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import FieldDoesNotExist
from django.test import Client, TestCase
from django.urls import reverse

from shipping.models import ShippingOrder
from shipping.ozon_supplies import (
    apply_ozon_bundle_to_stock,
    build_form_payload_from_order,
    _encode_ozon_selection_id,
    _extract_gm_from_supplies_cargoes,
    fetch_ozon_gm_label_documents,
    get_ozon_supply_for_agency,
    list_ozon_supplies_for_agency,
    lookup_existing_ozon_supply_shipments,
    match_bundle_to_stock,
    OzonCredentialRef,
    resolve_ozon_credentials,
    supply_ids_from_order,
)
from fbs.models import FbsOzonCredential
from sku.models import Agency, Market, MarketCredential


class OzonMultiCabinetTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Ozon Multi Agency")
        FbsOzonCredential.objects.create(
            agency=self.agency,
            slot=1,
            client_id="111",
            api_key="key-1",
        )
        FbsOzonCredential.objects.create(
            agency=self.agency,
            slot=2,
            client_id="222",
            api_key="key-2",
        )

    def test_resolves_every_configured_cabinet(self):
        credentials, error = resolve_ozon_credentials(self.agency)

        self.assertEqual(error, "")
        self.assertEqual(
            [(row.slot, row.client_id, row.api_key) for row in credentials],
            [(1, "111", "key-1"), (2, "222", "key-2")],
        )

    def test_legacy_primary_key_remains_authoritative_for_same_client_id(self):
        market = Market.objects.create(id=9910, name="OZON")
        MarketCredential.objects.create(
            id=99101,
            agency=self.agency,
            market=market,
            client_id="111",
            market_key="legacy-working-key",
        )

        credentials, error = resolve_ozon_credentials(self.agency)

        self.assertEqual(error, "")
        self.assertEqual(
            [(row.slot, row.client_id, row.api_key) for row in credentials],
            [(1, "111", "legacy-working-key"), (2, "222", "key-2")],
        )

    @patch("shipping.ozon_supplies.fetch_ozon_gm_cargoes", return_value=([], ""))
    @patch("shipping.ozon_supplies.fetch_ozon_supply_orders")
    @patch("shipping.ozon_supplies.list_ozon_supply_order_ids")
    def test_list_aggregates_orders_from_all_cabinets(
        self,
        list_ids_mock,
        fetch_orders_mock,
        _gm_mock,
    ):
        list_ids_mock.side_effect = lambda client_id, _key, **_kwargs: (
            [101] if client_id == "111" else [202],
            "",
        )
        fetch_orders_mock.side_effect = lambda client_id, _key, ids, **_kwargs: (
            [
                {
                    "order_id": ids[0],
                    "order_number": f"ORDER-{client_id}",
                    "state": "READY_TO_SUPPLY",
                    "supplies": [{"supply_id": ids[0] + 1000}],
                }
            ],
            "",
        )

        result = list_ozon_supplies_for_agency(self.agency)

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["orders"]), 2)
        self.assertEqual(
            {row["ozon_client_id"] for row in result["orders"]},
            {"111", "222"},
        )
        self.assertEqual(
            {row["ozon_account_label"] for row in result["orders"]},
            {"Кабинет 1", "Кабинет 2"},
        )
        self.assertEqual(
            {row["order_id"] for row in result["orders"]},
            {
                _encode_ozon_selection_id(101, 1),
                _encode_ozon_selection_id(202, 2),
            },
        )

    @patch("shipping.ozon_supplies.fetch_ozon_supply_bundle_items", return_value=([], ""))
    @patch("shipping.ozon_supplies.fetch_ozon_supply_order_details", return_value=({}, ""))
    @patch("shipping.ozon_supplies.fetch_ozon_gm_cargoes")
    @patch("shipping.ozon_supplies.fetch_ozon_supply_orders")
    def test_detail_finds_order_in_second_cabinet_and_marks_cargoes(
        self,
        fetch_orders_mock,
        gm_mock,
        _details_mock,
        _bundle_mock,
    ):
        fetch_orders_mock.return_value = (
            [
                {
                    "order_id": 202,
                    "order_number": "ORDER-222",
                    "state": "READY_TO_SUPPLY",
                    "supplies": [{"supply_id": 1202}],
                }
            ],
            "",
        )
        gm_mock.return_value = (
            [{"gm_barcode": "GM-222", "supply_id": 1202, "cargo_id": 99}],
            "",
        )

        selection_id = _encode_ozon_selection_id(202, 2)
        result = get_ozon_supply_for_agency(self.agency, selection_id, stock_rows=[])

        self.assertTrue(result["ok"])
        self.assertEqual(result["order_id"], selection_id)
        self.assertEqual(result["ozon_order_id"], 202)
        self.assertEqual(result["ozon_client_id"], "222")
        self.assertEqual(result["ozon_credential_slot"], 2)
        self.assertEqual(result["gm_cargoes"][0]["ozon_client_id"], "222")
        self.assertEqual(result["gm_cargoes"][0]["ozon_credential_slot"], 2)
        self.assertEqual(fetch_orders_mock.call_args.args[0], "222")
        self.assertEqual(fetch_orders_mock.call_count, 1)

    @patch("shipping.ozon_supplies.ozon_get_pdf")
    @patch("shipping.ozon_supplies.ozon_post")
    @patch("shipping.ozon_supplies.resolve_ozon_credentials")
    def test_label_documents_use_the_cabinet_saved_on_each_cargo(
        self,
        resolve_mock,
        post_mock,
        pdf_mock,
    ):
        resolve_mock.return_value = (
            [
                OzonCredentialRef("111", "key-1", 1),
                OzonCredentialRef("222", "key-2", 2),
            ],
            "",
        )
        post_mock.side_effect = [
            ({"operation_id": "operation-111"}, None),
            ({"status": "SUCCESS", "result": {"file_guid": "11111111-abcd"}}, None),
            ({"operation_id": "operation-222"}, None),
            ({"status": "SUCCESS", "result": {"file_guid": "22222222-abcd"}}, None),
        ]
        pdf_mock.side_effect = [
            (b"%PDF-1.4\nfirst\n%%EOF", ""),
            (b"%PDF-1.4\nsecond\n%%EOF", ""),
        ]

        documents, error, pending = fetch_ozon_gm_label_documents(
            SimpleNamespace(id=987655),
            [
                {
                    "gm_barcode": "GM-111",
                    "supply_id": 8001,
                    "cargo_id": 7001,
                    "ozon_client_id": "111",
                },
                {
                    "gm_barcode": "GM-222",
                    "supply_id": 8002,
                    "cargo_id": 7002,
                    "ozon_client_id": "222",
                },
            ],
            expected_gm_barcodes=["GM-111", "GM-222"],
            poll_attempts=1,
        )

        self.assertEqual(error, "")
        self.assertFalse(pending)
        self.assertEqual(len(documents), 2)
        self.assertEqual(post_mock.call_args_list[0].args[1:3], ("111", "key-1"))
        self.assertEqual(post_mock.call_args_list[2].args[1:3], ("222", "key-2"))


class OzonSuppliesMappingTests(TestCase):
    def test_supply_ids_skip_non_numeric_order_number(self):
        order = {
            "order_number": "120872012-1",
            "supplies": [
                {"supply_id": 2000061661498},
                {"supply_id": 2000061661499},
            ],
        }

        self.assertEqual(
            supply_ids_from_order(order),
            ["2000061661498", "2000061661499"],
        )

    def test_supply_ids_keep_numeric_order_number_as_fallback(self):
        self.assertEqual(
            supply_ids_from_order({"order_number": "2000060437104"}),
            ["2000060437104"],
        )

    def test_build_form_payload_maps_crossdock_fields(self):
        order = {
            "order_id": 101,
            "order_number": "0234-0001",
            "state": "READY_TO_SUPPLY",
            "drop_off_warehouse": {"name": "Пушкино", "address": "МО"},
            "timeslot": {
                "timeslot": {
                    "from": "2026-07-20T10:00:00+03:00",
                    "to": "2026-07-20T12:00:00+03:00",
                }
            },
            "supplies": [
                {
                    "supply_id": 1,
                    "bundle_id": "b1",
                    "is_crossdock": True,
                    "storage_warehouse": {"name": "Хоругвино"},
                },
                {
                    "supply_id": 2,
                    "bundle_id": "b2",
                    "is_crossdock": True,
                    "storage_warehouse": {"name": "Софьино"},
                },
            ],
        }
        details = {
            "vehicle": {
                "value": {
                    "driver_phone": "79001234567",
                    "vehicle_number": "A123BC77",
                }
            }
        }
        payload = build_form_payload_from_order(
            order=order,
            details=details,
            bundle_items=[
                {"offer_id": "SKU-1", "barcode": "460001", "quantity": 20, "quant": 10, "name": "Item"},
            ],
            stock_rows=[
                {
                    "key": "k1",
                    "sku_code": "SKU-1",
                    "barcode": "460001",
                    "box_qty": 10,
                    "available_boxes": 5,
                }
            ],
        )
        self.assertEqual(payload["form"]["supply_number"], "0234-0001")
        self.assertEqual(payload["form"]["slot_date"], "2026-07-20")
        self.assertEqual(payload["form"]["slot_time"], "10:00")
        self.assertTrue(payload["form"]["wb_transit_warehouse"])
        self.assertIn("Пушкино", payload["form"]["transit_address"])
        self.assertEqual(payload["form"]["destination_warehouse"], "")
        self.assertTrue(payload["is_multi_destination"])
        self.assertEqual(payload["form"]["vehicle_number"], "A123BC77")
        matched = [s for s in payload["stock_suggestions"] if s.get("matched")]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["boxes"], 2)
        self.assertEqual(payload["composition"][0]["barcode"], "460001")
        self.assertEqual(payload["composition"][0]["quantity"], 20)
        self.assertEqual(payload["selected_boxes"].get("k1"), "2")

    def test_crossdock_without_storage_warehouse_uses_ozon_dropoff_fallback(self):
        payload = build_form_payload_from_order(
            order={
                "order_id": 125969048,
                "order_number": "2000064968819",
                "state": "READY_TO_SUPPLY",
                "drop_off_warehouse": {
                    "name": "СТАРАЯ_КУПАВНА_28",
                    "address": "Акрихиновское шоссе, 12 строение 6",
                },
                "timeslot": {
                    "timeslot": {
                        "from": "2026-09-12T13:00:00Z",
                        "to": "2026-09-12T14:00:00Z",
                    }
                },
                "supplies": [
                    {
                        "supply_id": 2000064968819,
                        "bundle_id": "bundle-1",
                        "is_crossdock": True,
                        "storage_warehouse": None,
                    }
                ],
            },
            details=None,
            bundle_items=[],
            stock_rows=[],
        )

        self.assertTrue(payload["form"]["wb_transit_warehouse"])
        self.assertEqual(payload["destination_warehouses"], [])
        self.assertIn("СТАРАЯ_КУПАВНА_28", payload["form"]["transit_address"])
        self.assertEqual(
            payload["form"]["destination_warehouse"],
            payload["form"]["transit_address"],
        )
        self.assertTrue(
            any(
                "автоматически подставлена" in hint
                for hint in payload["hints"]
            )
        )

    def test_match_bundle_caps_by_available_boxes(self):
        suggestions = match_bundle_to_stock(
            [{"offer_id": "A", "barcode": "111", "quantity": 100}],
            [{"key": "1", "sku_code": "A", "barcode": "111", "box_qty": 10, "available_boxes": 3}],
        )
        self.assertTrue(suggestions[0]["matched"])
        self.assertEqual(suggestions[0]["boxes"], 3)

    def test_apply_bundle_combines_all_barcode_groups_of_the_same_sku(self):
        stock_rows = [
            {
                "key": "primary",
                "sku_code": "010225_grayset",
                "barcode": "2042682715481",
                "box_qty": 1,
                "available_boxes": 1,
                "box_codes": ["BOX-PRIMARY"],
                "is_mixed_box": False,
            },
            {
                "key": "ozon-upper",
                "sku_code": "010225_grayset",
                "barcode": "OZN1851453517",
                "box_qty": 1,
                "available_boxes": 3,
                "box_codes": ["BOX-UPPER-1", "BOX-UPPER-2", "BOX-UPPER-3"],
                "is_mixed_box": False,
            },
            {
                "key": "ozon-lower",
                "sku_code": "010225_grayset",
                "barcode": "ozn1851453517",
                "box_qty": 1,
                "available_boxes": 4,
                "box_codes": [
                    "BOX-LOWER-1",
                    "BOX-LOWER-2",
                    "BOX-LOWER-3",
                    "BOX-LOWER-4",
                ],
                "is_mixed_box": False,
            },
        ]

        result = apply_ozon_bundle_to_stock(
            [
                {
                    "offer_id": "010225_grayset",
                    "barcode": "OZN1851453517",
                    "quantity": 8,
                    "name": "Cтол и стул детский комплект",
                }
            ],
            stock_rows,
        )

        self.assertEqual(
            result["selected_boxes"],
            {"primary": "1", "ozon-upper": "3", "ozon-lower": "4"},
        )
        self.assertEqual(result["composition"][0]["applied_qty"], 8)
        self.assertEqual(result["composition"][0]["remaining_qty"], 0)
        self.assertEqual(result["composition"][0]["status"], "matched")
        self.assertTrue(result["composition"][0]["matched"])
        self.assertEqual(result["discrepancies"], [])

    def test_composition_kept_when_not_in_stock_boxes(self):
        result = apply_ozon_bundle_to_stock(
            [{"offer_id": "приборы001", "barcode": "2042861600201", "quantity": 50, "name": "Set"}],
            [],
        )
        self.assertEqual(len(result["composition"]), 1)
        self.assertEqual(result["composition"][0]["barcode"], "2042861600201")
        self.assertEqual(result["composition"][0]["quantity"], 50)
        self.assertFalse(result["composition"][0]["matched"])
        self.assertEqual(result["selected_boxes"], {})
        self.assertIn("нет доступных коробов", result["composition"][0]["reason"])
        self.assertTrue(any("нет доступных коробов" in row for row in result["discrepancies"]))

    @patch("shipping.ozon_supplies.fetch_ozon_supply_bundle_items", return_value=([], ""))
    @patch("shipping.ozon_supplies.fetch_ozon_supply_order_details", return_value=({}, ""))
    @patch("shipping.ozon_supplies.list_ozon_supply_order_ids", return_value=([118594191], ""))
    @patch("shipping.ozon_supplies.fetch_ozon_supply_orders")
    def test_get_supply_resolves_supply_number_to_internal_order_id(
        self,
        fetch_orders,
        _list_ids,
        details_mock,
        _bundle_mock,
    ):
        agency = Agency.objects.create(agn_name="Ozon Client")
        market = Market.objects.create(id=9911, name="OZON")
        MarketCredential.objects.create(
            id=9911,
            agency=agency,
            market=market,
            client_id="123",
            market_key="token",
        )
        order = {
            "order_id": 118594191,
            "order_number": "2000060437104",
            "state": "READY_TO_SUPPLY",
            "drop_off_warehouse": {"name": "СТАРАЯ_КУПАВНА_28"},
            "supplies": [{"supply_id": 2000060437104, "is_crossdock": True}],
        }
        fetch_orders.side_effect = [
            ([], 'Ozon API ошибка 400: {"message":"not found"}'),
            ([order], ""),
        ]

        payload = get_ozon_supply_for_agency(
            agency,
            2000060437104,
            stock_rows=[],
            include_gm_cargoes=False,
            bundle_max_pages=2,
        )

        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["order_id"], 118594191)
        self.assertEqual(payload["order_number"], "2000060437104")
        details_mock.assert_called_once()
        self.assertEqual(details_mock.call_args.args[2], 118594191)

    def test_attach_existing_shipping_flags(self):
        from shipping.ozon_supplies import attach_existing_shipping_to_ozon_orders

        orders = [
            {"order_number": "2000043158731", "order_id": 1},
            {"order_number": "OTHER", "order_id": 2},
        ]
        attach_existing_shipping_to_ozon_orders(
            orders,
            {
                "2000043158731": {
                    "id": 99,
                    "number": "SO-99",
                    "status": "submitted",
                    "status_label": "Новая",
                }
            },
        )
        self.assertTrue(orders[0]["already_uploaded"])
        self.assertEqual(orders[0]["existing_shipping"]["number"], "SO-99")
        self.assertFalse(orders[1]["already_uploaded"])

    def test_partial_stock_match_is_guidance_not_failure(self):
        payload = build_form_payload_from_order(
            order={
                "order_id": 55,
                "order_number": "OZ-55",
                "state": "READY_TO_SUPPLY",
                "drop_off_warehouse": {"name": "Hub"},
                "timeslot": {"timeslot": {"from": "2026-07-21T14:00:00+03:00"}},
                "supplies": [
                    {
                        "supply_id": 1,
                        "bundle_id": "b1",
                        "is_crossdock": False,
                        "storage_warehouse": {"name": "РФЦ"},
                    }
                ],
            },
            details=None,
            bundle_items=[
                {"offer_id": "A", "barcode": "111", "quantity": 10, "name": "A"},
                {"offer_id": "B", "barcode": "222", "quantity": 10, "name": "B"},
            ],
            stock_rows=[
                {
                    "key": "k1",
                    "sku_code": "A",
                    "barcode": "111",
                    "box_qty": 10,
                    "available_boxes": 2,
                }
            ],
        )
        self.assertEqual(payload["stock_match"]["status"], "partial")
        self.assertEqual(payload["stock_match"]["matched"], 1)
        self.assertEqual(payload["stock_match"]["missing"], 1)
        self.assertTrue(payload["hints"][0].startswith("Заявка Ozon подставлена"))
        self.assertIn("подобрано 1 из 2", payload["hints"][0])
        self.assertTrue(any("Нет доступного остатка" in h for h in payload["hints"]))

    def test_annotate_payload_existing_shipping_warning(self):
        from shipping.ozon_supplies import annotate_payload_existing_shipping

        payload = {
            "order_number": "2000043158731",
            "form": {"supply_number": "2000043158731"},
            "hints": ["Заявка Ozon подставлена."],
        }
        with patch(
            "shipping.ozon_supplies.lookup_existing_ozon_supply_shipments",
            return_value={
                "2000043158731": {
                    "id": 77,
                    "number": "SO-77",
                    "status": "draft",
                    "status_label": "Черновик",
                }
            },
        ):
            annotate_payload_existing_shipping(payload, agency=object())
        self.assertTrue(payload["already_uploaded"])
        self.assertEqual(payload["existing_shipping"]["number"], "SO-77")
        self.assertTrue(payload["hints"][0].startswith("Внимание:"))
        self.assertIn("SO-77", payload["hints"][0])

    def test_extract_gm_barcodes_from_cargoes_payload(self):
        rows = _extract_gm_from_supplies_cargoes(
            {
                "supplies_cargoes": [
                    {
                        "supply_id": 2000059613908,
                        "cargoes_without_transport_cargoes": [
                            {"cargo_id": 111, "barcode": "GM-111", "bundle_id": "b1"},
                        ],
                        "transport_cargoes": [
                            {
                                "transport_cargo_id": 9,
                                "cargoes": [
                                    {"cargo_id": 222, "barcode": "GM-222", "bundle_id": "b2"},
                                ],
                            }
                        ],
                    }
                ]
            }
        )
        self.assertEqual([r["gm_barcode"] for r in rows], ["GM-111", "GM-222"])
        payload = build_form_payload_from_order(
            order={
                "order_id": 1,
                "order_number": "N-1",
                "state": "READY_TO_SUPPLY",
                "drop_off_warehouse": {"name": "Hub"},
                "supplies": [{"supply_id": 1, "is_crossdock": True, "storage_warehouse": {}}],
                "timeslot": {"timeslot": {"from": "2026-07-20T10:00:00+03:00"}},
            },
            details=None,
            bundle_items=[],
            gm_cargoes=rows,
        )
        self.assertEqual(payload["gm_barcodes"], ["GM-111", "GM-222"])
        self.assertIn("ШК ГМ Ozon: GM-111, GM-222", payload["form"]["ozon_gm_comment"])

    def test_flatten_items_from_gm_cargoes(self):
        from shipping.ozon_supplies import flatten_bundle_items_from_gm

        items = flatten_bundle_items_from_gm(
            [
                {
                    "gm_barcode": "GM1",
                    "items": [{"offer_id": "A", "barcode": "1", "quantity": 5, "quant": 0, "name": "A"}],
                },
                {
                    "gm_barcode": "GM2",
                    "items": [{"offer_id": "A", "barcode": "1", "quantity": 5, "quant": 0, "name": "A"}],
                },
                {
                    "gm_barcode": "GM3",
                    "items": [{"offer_id": "B", "barcode": "2", "quantity": 10, "quant": 0, "name": "B"}],
                },
            ]
        )
        by_offer = {row["offer_id"]: row["quantity"] for row in items}
        self.assertEqual(by_offer["A"], 10)
        self.assertEqual(by_offer["B"], 10)


class OzonSuppliesEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.user = user_model.objects.create_user(username="ozon_supply_client", password="pwd")
        cls.agency = Agency.objects.create(agn_name="Ozon Supply Agency", portal_user=cls.user)
        cls.ozon = Market.objects.create(id=9901, name="OZON")
        MarketCredential.objects.create(
            id=99011,
            agency=cls.agency,
            market=cls.ozon,
            client_id="123456",
            market_key="test-api-key",
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    @patch("shipping.ozon_supplies.list_ozon_supplies_for_agency")
    def test_list_endpoint_ok(self, list_mock):
        list_mock.return_value = {
            "ok": True,
            "orders": [{"order_id": 7, "order_number": "N-7", "state": "DATA_FILLING"}],
        }
        response = self.client.get(reverse("shipping:ozon-supplies"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["orders"][0]["order_number"], "N-7")
        list_mock.assert_called_once()

    @patch("shipping.ozon_supplies.get_ozon_supply_for_agency")
    def test_detail_endpoint_ok(self, get_mock):
        get_mock.return_value = {
            "ok": True,
            "order_id": 7,
            "form": {"supply_number": "N-7", "slot_date": "2026-07-21"},
        }
        response = self.client.get(reverse("shipping:ozon-supplies-detail", kwargs={"order_id": 7}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["form"]["supply_number"], "N-7")
        get_mock.assert_called_once()
        kwargs = get_mock.call_args.kwargs
        self.assertFalse(kwargs.get("enrich_gm", True))
        self.assertEqual(kwargs.get("api_timeout"), 6)
        self.assertEqual(kwargs.get("bundle_max_pages"), 2)
        self.assertFalse(kwargs.get("include_gm_cargoes", True))

    @patch("shipping.web_ui._shipping_stock_picker_rows", return_value=[])
    @patch("shipping.ozon_supplies.get_ozon_supply_for_agency", side_effect=RuntimeError("boom"))
    def test_detail_endpoint_exception_returns_json(self, get_mock, _stock_mock):
        response = self.client.get(reverse("shipping:ozon-supplies-detail", kwargs={"order_id": 7}))
        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertIn("Ozon", payload["error"])

    @patch("shipping.ozon_supplies.list_ozon_supplies_for_agency", side_effect=RuntimeError("boom"))
    def test_list_endpoint_exception_returns_json(self, _list_mock):
        response = self.client.get(reverse("shipping:ozon-supplies"))
        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["orders"], [])
        self.assertIn("Ozon", payload["error"])

    def test_list_without_credential_message(self):
        MarketCredential.objects.filter(agency=self.agency).delete()
        response = self.client.get(reverse("shipping:ozon-supplies"))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertIn("ключ", response.json()["error"].lower())

    def test_existing_lookup_falls_back_to_wb_barcode_before_supply_migration(self):
        order = ShippingOrder.objects.create(
            number="SO-OZON-FALLBACK",
            agency=self.agency,
            status=ShippingOrder.STATUS_SUBMITTED,
            wb_supply_barcode="OZON-ORDER-1",
        )
        original_get_field = ShippingOrder._meta.get_field

        def get_field_without_supply_number(name):
            if name == "supply_number":
                raise FieldDoesNotExist
            return original_get_field(name)

        with patch.object(ShippingOrder._meta, "get_field", side_effect=get_field_without_supply_number):
            found = lookup_existing_ozon_supply_shipments(self.agency, ["OZON-ORDER-1"])

        self.assertEqual(found["OZON-ORDER-1"]["id"], order.id)


class OzonSupplyDetailFetchTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Ozon Detail Agency")
        self.ozon = Market.objects.create(id=9902, name="OZON")
        MarketCredential.objects.create(
            id=99021,
            agency=self.agency,
            market=self.ozon,
            client_id="654321",
            market_key="detail-key",
        )

    @patch("shipping.ozon_supplies.enrich_gm_cargoes_with_items")
    @patch("shipping.ozon_supplies.fetch_ozon_gm_cargoes")
    @patch("shipping.ozon_supplies.fetch_ozon_supply_bundle_items")
    @patch("shipping.ozon_supplies.fetch_ozon_supply_order_details")
    @patch("shipping.ozon_supplies.fetch_ozon_supply_orders")
    def test_detail_skips_gm_enrich_by_default(
        self,
        orders_mock,
        details_mock,
        bundle_mock,
        gm_mock,
        enrich_mock,
    ):
        from shipping.ozon_supplies import get_ozon_supply_for_agency

        orders_mock.return_value = (
            [
                {
                    "order_id": 55,
                    "order_number": "2000043158731",
                    "state": "DATA_FILLING",
                    "supplies": [{"supply_id": 9, "bundle_id": "b9"}],
                }
            ],
            "",
        )
        details_mock.return_value = ({}, "")
        bundle_mock.return_value = ([], "")
        gm_mock.return_value = (
            [{"gm_barcode": "1022058754473000", "supply_id": 9, "bundle_id": "b9"}],
            "",
        )

        result = get_ozon_supply_for_agency(self.agency, 55, stock_rows=[])

        self.assertTrue(result["ok"])
        self.assertEqual(result["form"]["supply_number"], "2000043158731")
        enrich_mock.assert_not_called()
        self.assertEqual(orders_mock.call_args.kwargs.get("timeout"), 15)
        self.assertEqual(bundle_mock.call_args.kwargs.get("max_pages"), 8)

    @patch("shipping.ozon_supplies.fetch_ozon_gm_cargoes")
    @patch("shipping.ozon_supplies.fetch_ozon_supply_bundle_items")
    @patch("shipping.ozon_supplies.fetch_ozon_supply_order_details", return_value=({}, ""))
    @patch("shipping.ozon_supplies.fetch_ozon_supply_orders")
    def test_detail_prefers_complete_gm_cargo_composition_over_stale_order_bundle(
        self,
        orders_mock,
        _details_mock,
        bundle_mock,
        gm_mock,
    ):
        orders_mock.return_value = (
            [
                {
                    "order_id": 55,
                    "order_number": "126920754-1",
                    "state": "READY_TO_SUPPLY",
                    "supplies": [
                        {"supply_id": 9, "bundle_id": "order-bundle"},
                    ],
                }
            ],
            "",
        )
        gm_mock.return_value = (
            [
                {"gm_barcode": "GM-1", "supply_id": 9, "bundle_id": "cargo-1"},
                {"gm_barcode": "GM-2", "supply_id": 9, "bundle_id": "cargo-2"},
            ],
            "",
        )

        def bundle_result(_client_id, _api_key, bundle_ids, **_kwargs):
            qty = 312 if bundle_ids == ["order-bundle"] else 310
            return (
                [
                    {
                        "offer_id": "WATCH-GOLD",
                        "barcode": "OZN2446436869",
                        "quantity": qty,
                    }
                ],
                "",
            )

        bundle_mock.side_effect = bundle_result

        result = get_ozon_supply_for_agency(self.agency, 55, stock_rows=[])

        self.assertTrue(result["ok"])
        self.assertEqual(result["items"][0]["quantity"], 310)
        self.assertEqual(result["composition"][0]["quantity"], 310)
        self.assertEqual(result["composition_source"], "gm_cargoes")
        self.assertEqual(
            result["composition_reconciliation"],
            {
                "source": "gm_cargoes",
                "order_bundle_total": 312,
                "gm_cargo_total": 310,
            },
        )
        self.assertIn("310 шт.", result["hints"][0])
        self.assertEqual(bundle_mock.call_count, 2)
