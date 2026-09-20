from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from fbs.integrations.http import MarketplaceHttpResponse
from fbs.models import FbsIntegrationProfile, FbsStockExportState
from fbs.services.stock_compare import build_agency_stock_comparison
from sku.models import Agency, SKU


class _StubTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def send(self, profile, spec):
        self.calls.append((profile, spec))
        return self.responses.pop(0)


def _response(payload, *, status=200):
    return MarketplaceHttpResponse(
        status_code=status,
        headers={},
        content=b"{}",
        json_payload=payload,
    )


class FbsStockComparisonTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Тестовый клиент")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-1",
            name="Тестовый товар",
        )

    def _profile(self, marketplace):
        return FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=marketplace,
            name=f"{marketplace} FBS",
            external_account_id=f"account-{marketplace}",
            external_warehouse_id=f"warehouse-{marketplace}",
            stock_mode=FbsIntegrationProfile.STOCK_MODE_MANAGED,
            is_active=True,
            stock_push_enabled=True,
        )

    @patch("fbs.services.stock_compare._local_metadata", return_value={})
    @patch("fbs.services.stock_compare._available_by_binding")
    def test_wb_compares_exact_seller_warehouse_stock(
        self,
        available_mock,
        _metadata_mock,
    ):
        profile = self._profile(FbsIntegrationProfile.MARKETPLACE_WB)
        FbsStockExportState.objects.create(
            profile=profile,
            sku_ref=self.sku,
            barcode="460000000001",
            external_item_id="12345",
            desired_qty=7,
        )
        FbsStockExportState.objects.create(
            profile=profile,
            sku_ref=self.sku,
            barcode="460000000002",
            external_item_id="wb-unmapped:460000000002",
            desired_qty=0,
            status=FbsStockExportState.STATUS_BLOCKED,
        )
        available_mock.return_value = {"460000000001": 7}
        transport = _StubTransport([_response({"stocks": [{"chrtId": 12345, "amount": 7}]})])

        result = build_agency_stock_comparison(
            agency_id=self.agency.id,
            transport=transport,
        )

        self.assertEqual(result["profiles"][0]["mismatch_count"], 0)
        self.assertEqual(result["profiles"][0]["fullbox_total"], 7)
        self.assertEqual(result["profiles"][0]["marketplace_total"], 7)
        self.assertEqual(result["profiles"][0]["mapping_missing_count"], 0)
        self.assertTrue(result["rows"][0]["is_match"])
        self.assertEqual(
            transport.calls[0][1].endpoint,
            f"/api/v3/stocks/{profile.external_warehouse_id}",
        )

    @patch("fbs.services.stock_compare._local_metadata", return_value={})
    @patch("fbs.services.stock_compare._available_by_binding")
    def test_ozon_uses_only_fbs_stock_and_reports_difference(
        self,
        available_mock,
        _metadata_mock,
    ):
        profile = self._profile(FbsIntegrationProfile.MARKETPLACE_OZON)
        FbsStockExportState.objects.create(
            profile=profile,
            sku_ref=self.sku,
            barcode="460000000001",
            external_item_id="OFFER-1",
            desired_qty=5,
        )
        available_mock.return_value = {str(self.sku.id): 5}
        transport = _StubTransport(
            [
                _response(
                    {
                        "items": [
                            {
                                "offer_id": "OFFER-1",
                                "stocks": [
                                    {"type": "fbo", "present": 100},
                                    {"type": "fbs", "present": 8},
                                ],
                            }
                        ],
                        "cursor": "",
                    }
                )
            ]
        )

        result = build_agency_stock_comparison(
            agency_id=self.agency.id,
            transport=transport,
        )

        self.assertEqual(result["profiles"][0]["fullbox_total"], 5)
        self.assertEqual(result["profiles"][0]["marketplace_total"], 8)
        self.assertEqual(result["profiles"][0]["mismatch_count"], 1)
        self.assertEqual(result["rows"][0]["difference"], 3)
        self.assertIn("все FBS-склады", result["rows"][0]["warehouse_note"])

    @patch("fbs.services.stock_compare._local_metadata")
    @patch("fbs.services.stock_compare._available_by_binding")
    def test_unmapped_local_stock_is_visible(
        self,
        available_mock,
        metadata_mock,
    ):
        self._profile(FbsIntegrationProfile.MARKETPLACE_OZON)
        available_mock.return_value = {str(self.sku.id): 4}
        metadata_mock.return_value = {
            str(self.sku.id): {
                "sku_code": self.sku.sku_code,
                "name": self.sku.name,
                "barcode": "",
            }
        }
        transport = _StubTransport([_response({"items": [], "cursor": ""})])

        result = build_agency_stock_comparison(
            agency_id=self.agency.id,
            transport=transport,
        )

        self.assertEqual(result["profiles"][0]["mapping_missing_count"], 1)
        self.assertTrue(result["rows"][0]["mapping_missing"])
        self.assertEqual(result["rows"][0]["fullbox_qty"], 4)
        self.assertIsNone(result["rows"][0]["marketplace_qty"])

    @patch("fbs.services.stock_compare._local_metadata", return_value={})
    @patch("fbs.services.stock_compare._available_by_binding", return_value={})
    def test_api_failure_is_shown_instead_of_breaking_page(
        self,
        _available_mock,
        _metadata_mock,
    ):
        self._profile(FbsIntegrationProfile.MARKETPLACE_OZON)
        transport = _StubTransport([_response({"message": "error"}, status=503)])

        result = build_agency_stock_comparison(
            agency_id=self.agency.id,
            transport=transport,
        )

        self.assertEqual(result["rows"], [])
        self.assertIn("HTTP 503", result["profiles"][0]["error"])
