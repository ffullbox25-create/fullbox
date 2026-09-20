from types import SimpleNamespace

from django.test import SimpleTestCase

from fbs.exceptions import FbsIntegrationError, FbsPickingError
from fbs.integrations.http import MarketplaceHttpResponse
from fbs.integrations.wb import (
    apply_wb_metadata_to_requirements,
    build_wb_orders_metadata_spec,
    enrich_wb_orders_with_metadata,
    parse_wb_new_orders,
    parse_wb_orders_metadata,
)
from fbs.services.picking import (
    _assert_required_marking_code,
    _required_marking_codes,
)
from fbs.services.sync import _read_wb_order_metadata


class StubMetadataTransport:
    def __init__(self, payload):
        self.payload = payload
        self.specs = []

    def send(self, profile, spec):
        self.specs.append(spec)
        return MarketplaceHttpResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            content=b"",
            json_payload=self.payload,
        )


class WbPreciseMarkingSyncTests(SimpleTestCase):
    @staticmethod
    def _orders_payload(*, optional_meta=None):
        return {
            "orders": [
                {
                    "id": 123456,
                    "warehouseId": 101,
                    "nmId": 2001,
                    "chrtId": 3001,
                    "skus": ["4600000000001"],
                    "article": "SKU-1",
                    "createdAt": "2026-08-29T10:00:00Z",
                    "requiredMeta": [],
                    "optionalMeta": optional_meta or [],
                }
            ]
        }

    def test_metadata_spec_uses_current_batch_endpoint(self):
        spec = build_wb_orders_metadata_spec([123456, 123457])

        self.assertEqual(spec.endpoint, "/api/marketplace/v3/orders/meta")
        self.assertEqual(spec.body, {"orders": [123456, 123457]})

    def test_meta_details_are_stored_as_order_decision_and_exact_codes(self):
        metadata = parse_wb_orders_metadata(
            {
                "orders": [
                    {
                        "id": 123456,
                        "metaDetails": [
                            {
                                "key": "sgtin",
                                "decision": "filled",
                                "value": ["010460000000000021ABC123"],
                                "errors": [],
                            }
                        ],
                    }
                ]
            }
        )
        orders = parse_wb_new_orders(
            self._orders_payload(optional_meta=["sgtin"])
        )

        enriched = enrich_wb_orders_with_metadata(orders, metadata)
        requirements = enriched[0].items[0].requirements

        self.assertEqual(
            requirements["wb_marking_codes"],
            ["010460000000000021ABC123"],
        )
        self.assertEqual(requirements["wb_meta"]["sgtin"]["decision"], "filled")
        self.assertTrue(requirements["wb_meta"]["sgtin"]["has_value"])
        self.assertIn("_fullbox_wb_meta", enriched[0].raw_payload)

    def test_optional_without_value_is_recorded_but_not_promoted_to_required(self):
        requirements = apply_wb_metadata_to_requirements(
            {"optional_meta": ["sgtin"]},
            {
                "sgtin": {
                    "decision": "optional",
                    "value": None,
                    "errors": [],
                }
            },
        )

        self.assertEqual(requirements["wb_marking_codes"], [])
        self.assertEqual(requirements["wb_meta"]["sgtin"]["decision"], "optional")
        self.assertFalse(requirements["wb_meta"]["sgtin"]["has_value"])

    def test_order_sync_reads_metadata_only_when_sgtin_is_advertised(self):
        transport = StubMetadataTransport(
            {
                "orders": [
                    {
                        "id": 123456,
                        "metaDetails": [
                            {
                                "key": "sgtin",
                                "decision": "required",
                                "value": None,
                            }
                        ],
                    }
                ]
            }
        )
        orders = list(
            parse_wb_new_orders(self._orders_payload(optional_meta=["sgtin"]))
        )

        enriched, request_count = _read_wb_order_metadata(
            profile=SimpleNamespace(),
            orders=orders,
            transport=transport,
        )

        self.assertEqual(request_count, 1)
        self.assertEqual(len(transport.specs), 1)
        self.assertEqual(
            enriched[0].items[0].requirements["wb_meta"]["sgtin"]["decision"],
            "required",
        )

    def test_order_sync_fails_closed_when_wb_omits_requested_order(self):
        transport = StubMetadataTransport({"orders": []})
        orders = list(
            parse_wb_new_orders(self._orders_payload(optional_meta=["sgtin"]))
        )

        with self.assertRaisesMessage(FbsIntegrationError, "не вернул метаданные"):
            _read_wb_order_metadata(
                profile=SimpleNamespace(),
                orders=orders,
                transport=transport,
            )


class WbExactMarkingCodeTests(SimpleTestCase):
    @staticmethod
    def _item(requirements):
        return SimpleNamespace(external_line_id="123456", requirements=requirements)

    def test_wb_codes_are_combined_with_existing_requirement_sources(self):
        item = self._item(
            {
                "wb_marking_codes": ["WB-CODE"],
                "marking_codes": ["LEGACY-CODE", "WB-CODE"],
            }
        )

        self.assertEqual(
            _required_marking_codes(item),
            ("WB-CODE", "LEGACY-CODE"),
        )

    def test_exact_wb_code_is_accepted(self):
        item = self._item({"wb_marking_codes": ["010460000000000021ABC123"]})

        _assert_required_marking_code(item, "010460000000000021ABC123")

    def test_different_wb_code_is_rejected_before_binding(self):
        item = self._item({"wb_marking_codes": ["010460000000000021ABC123"]})

        with self.assertRaisesMessage(FbsPickingError, "не совпадает"):
            _assert_required_marking_code(item, "010460000000000021DIFFERENT")
