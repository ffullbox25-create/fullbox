from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from sku.models import Agency, MarketplaceBinding, SKU, SKUBarcode
from sklad.models import WarehouseLocation

from .integrations.http import MarketplaceHttpResponse
from .models import (
    FbsBox,
    FbsIntegrationProfile,
    FbsOrder,
    FbsOrderStockAllocation,
    FbsPallet,
    FbsStockBalance,
    FbsStockExportState,
    FbsStorageCell,
)
from .services import (
    pull_profile_orders,
    pull_profile_statuses,
    sync_enabled_stock_exports,
    sync_profile_stock_exports,
)


class StubStockTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def send(self, profile, spec):
        self.requests.append((profile, spec))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _response(payload, status_code=200, headers=None):
    return MarketplaceHttpResponse(
        status_code=status_code,
        headers=headers or {"Content-Type": "application/json"},
        content=b"",
        json_payload=payload,
    )


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_ORDER_PULL_ENABLED=True,
    FBS_STATUS_PULL_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_STOCK_PUSH_ENABLED=True,
    FBS_ZONE_CODE="FBS",
    FBS_OZON_STOCK_MIN_INTERVAL_SECONDS=120,
)
class FbsOrderStockSyncTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="FBS stock client A")
        self.other_agency = Agency.objects.create(agn_name="FBS stock client B")
        self.sku, self.barcode = self._create_sku(
            self.agency,
            code="A-ART-1",
            barcode="4600000000101",
        )
        self.other_sku, self.other_barcode = self._create_sku(
            self.other_agency,
            code="B-ART-1",
            barcode="4600000000202",
        )
        self.balance = self._create_balance(
            self.agency,
            self.sku,
            self.barcode,
            qty=7,
            suffix="A",
        )
        self.other_balance = self._create_balance(
            self.other_agency,
            self.other_sku,
            self.other_barcode,
            qty=19,
            suffix="B",
        )

    @staticmethod
    def _create_sku(agency, *, code, barcode):
        sku = SKU.objects.create(agency=agency, sku_code=code, name=f"Product {code}")
        SKUBarcode.objects.create(sku=sku, value=barcode, is_primary=True)
        return sku, barcode

    @staticmethod
    def _profile(
        agency,
        *,
        marketplace,
        warehouse_id,
        stock_push=True,
        account_id="",
    ):
        return FbsIntegrationProfile.objects.create(
            agency=agency,
            marketplace=marketplace,
            name=f"{marketplace} {warehouse_id}",
            external_account_id=account_id or f"{marketplace}-{agency.id}",
            external_warehouse_id=str(warehouse_id),
            stock_mode=(
                FbsIntegrationProfile.STOCK_MODE_MANAGED
                if stock_push
                else FbsIntegrationProfile.STOCK_MODE_DISABLED
            ),
            is_active=True,
            order_pull_enabled=True,
            status_pull_enabled=True,
            stock_push_enabled=stock_push,
        )

    @staticmethod
    def _create_balance(agency, sku, barcode, *, qty, suffix):
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1 if suffix == "A" else 2,
            location_code=f"FBS-STOCK-{suffix}",
            is_storage=True,
            is_pickable=True,
        )
        cell = FbsStorageCell.objects.create(
            cell_code=f"FBS-STOCK-{suffix}",
            location=location,
        )
        pallet = FbsPallet.objects.create(
            agency=agency,
            pallet_code=f"FBS-PALLET-{suffix}",
            cell=cell,
            status=FbsPallet.STATUS_ACTIVE,
        )
        box = FbsBox.objects.create(
            agency=agency,
            pallet=pallet,
            box_code=f"FBS-BOX-{suffix}",
            status=FbsBox.STATUS_ACTIVE,
        )
        return FbsStockBalance.objects.create(
            agency=agency,
            box=box,
            sku_ref=sku,
            identity_key=("a" if suffix == "A" else "b") * 64,
            sku_code=sku.sku_code,
            name=sku.name,
            barcode=barcode,
            qty=qty,
            available_qty=qty,
        )

    @staticmethod
    def _wb_orders_payload(*, warehouse_id, barcode, count=1, start=7000):
        return {
            "orders": [
                {
                    "id": start + index,
                    "warehouseId": int(warehouse_id),
                    "nmId": 9000 + index,
                    "chrtId": 8000 + index,
                    "skus": [barcode],
                    "article": "A-ART-1",
                    "createdAt": "2026-08-10T08:00:00Z",
                    "requiredMeta": [],
                    "optionalMeta": [],
                }
                for index in range(count)
            ]
        }

    def test_exports_only_enabled_client_warehouses_without_mixing_balances(self):
        first = self._profile(
            self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            warehouse_id="101",
        )
        disabled = self._profile(
            self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            warehouse_id="102",
            stock_push=False,
        )
        second = self._profile(
            self.other_agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            warehouse_id="301",
        )
        FbsStockExportState.objects.create(
            profile=first,
            sku_ref=self.sku,
            barcode=self.barcode,
            external_item_id="8101",
        )
        FbsStockExportState.objects.create(
            profile=disabled,
            sku_ref=self.sku,
            barcode=self.barcode,
            external_item_id="8102",
        )
        FbsStockExportState.objects.create(
            profile=second,
            sku_ref=self.other_sku,
            barcode=self.other_barcode,
            external_item_id="8301",
        )
        transport = StubStockTransport(_response({}), _response({}))

        result = sync_enabled_stock_exports(transport=transport)

        self.assertEqual((result.profiles, result.requests, result.synced), (2, 2, 2))
        payloads = {
            profile.external_warehouse_id: spec.body["stocks"]
            for profile, spec in transport.requests
        }
        self.assertEqual(payloads["101"], [{"chrtId": 8101, "amount": 7}])
        self.assertEqual(payloads["301"], [{"chrtId": 8301, "amount": 19}])
        self.assertNotIn("102", payloads)
        disabled_state = FbsStockExportState.objects.get(profile=disabled)
        self.assertIsNone(disabled_state.last_sent_qty)

    def test_wb_zero_stock_is_exported_before_positive_changes(self):
        profile = self._profile(
            self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            warehouse_id="101",
        )
        zero_sku, zero_barcode = self._create_sku(
            self.agency,
            code="A-ZERO",
            barcode="4600000000100",
        )
        positive_state = FbsStockExportState.objects.create(
            profile=profile,
            sku_ref=self.sku,
            barcode=self.barcode,
            external_item_id="8101",
        )
        zero_state = FbsStockExportState.objects.create(
            profile=profile,
            sku_ref=zero_sku,
            barcode=zero_barcode,
            external_item_id="8100",
            desired_qty=1,
            last_sent_qty=1,
            status=FbsStockExportState.STATUS_SYNCED,
        )
        transport = StubStockTransport(_response({}))

        result = sync_profile_stock_exports(
            profile_id=profile.id,
            transport=transport,
            limit=1,
        )

        positive_state.refresh_from_db()
        zero_state.refresh_from_db()
        self.assertEqual((result.requests, result.synced), (1, 1))
        self.assertEqual(
            transport.requests[0][1].body["stocks"],
            [{"chrtId": 8100, "amount": 0}],
        )
        self.assertEqual(zero_state.last_sent_qty, 0)
        self.assertEqual(positive_state.status, FbsStockExportState.STATUS_PENDING)

    def test_wb_catalog_maps_exact_barcode_and_exports_existing_fbs_stock(self):
        profile = self._profile(
            self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            warehouse_id="101",
        )
        transport = StubStockTransport(
            _response(
                {
                    "cards": [
                        {
                            "nmID": 9001,
                            "vendorCode": self.sku.sku_code,
                            "sizes": [
                                {"chrtID": 8100, "skus": ["4600000000999"]},
                                {"chrtID": 8101, "skus": [self.barcode]},
                            ],
                        }
                    ],
                    "cursor": {"total": 1},
                }
            ),
            _response({}),
        )

        result = sync_profile_stock_exports(profile_id=profile.id, transport=transport)

        state = FbsStockExportState.objects.get(profile=profile)
        self.assertEqual((result.requests, result.synced, result.blocked), (2, 1, 0))
        self.assertEqual(state.external_item_id, "8101")
        self.assertEqual(state.last_sent_qty, 7)
        self.assertEqual(transport.requests[0][1].endpoint, "/content/v2/get/cards/list")
        self.assertEqual(
            transport.requests[1][1].body["stocks"],
            [{"chrtId": 8101, "amount": 7}],
        )

    def test_wb_catalog_never_maps_a_different_barcode(self):
        profile = self._profile(
            self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            warehouse_id="101",
        )
        transport = StubStockTransport(
            _response(
                {
                    "cards": [
                        {
                            "nmID": 9001,
                            "vendorCode": self.sku.sku_code,
                            "sizes": [{"chrtID": 8100, "skus": ["4600000000999"]}],
                        }
                    ],
                    "cursor": {"total": 1},
                }
            )
        )

        result = sync_profile_stock_exports(profile_id=profile.id, transport=transport)

        state = FbsStockExportState.objects.get(profile=profile)
        self.assertEqual((result.requests, result.synced, result.blocked), (1, 0, 1))
        self.assertTrue(state.external_item_id.startswith("wb-unmapped:"))
        self.assertEqual(state.status, FbsStockExportState.STATUS_BLOCKED)
        self.assertIn("точным штрихкодом", state.error)

    def test_wb_catalog_prefers_exact_marketplace_binding_over_local_sku_code(self):
        profile = self._profile(
            self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            warehouse_id="101",
        )
        MarketplaceBinding.objects.create(
            sku=self.sku,
            marketplace="WB",
            external_id="9001",
        )
        transport = StubStockTransport(
            _response(
                {
                    "cards": [
                        {
                            "nmID": 9001,
                            "vendorCode": "different-vendor-code",
                            "sizes": [{"chrtID": 8101, "skus": [self.barcode]}],
                        }
                    ],
                    "cursor": {"total": 1},
                }
            ),
            _response({}),
        )

        result = sync_profile_stock_exports(profile_id=profile.id, transport=transport)

        state = FbsStockExportState.objects.get(profile=profile)
        search_filter = transport.requests[0][1].body["settings"]["filter"]
        self.assertEqual(search_filter["textSearch"], "9001")
        self.assertEqual((result.requests, result.synced, result.blocked), (2, 1, 0))
        self.assertEqual((state.external_item_id, state.last_sent_qty), ("8101", 7))

    def test_wb_catalog_replaces_obsolete_barcode_for_same_chrt_id(self):
        profile = self._profile(
            self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            warehouse_id="101",
        )
        MarketplaceBinding.objects.create(
            sku=self.sku,
            marketplace="WB",
            external_id="9001",
        )
        FbsStockExportState.objects.create(
            profile=profile,
            sku_ref=self.sku,
            barcode="4600000000999",
            external_item_id="8101",
            desired_qty=0,
            last_sent_qty=0,
            status=FbsStockExportState.STATUS_SYNCED,
        )
        transport = StubStockTransport(
            _response(
                {
                    "cards": [
                        {
                            "nmID": 9001,
                            "sizes": [{"chrtID": 8101, "skus": [self.barcode]}],
                        }
                    ],
                    "cursor": {"total": 1},
                }
            ),
            _response({}),
        )

        result = sync_profile_stock_exports(profile_id=profile.id, transport=transport)

        state = FbsStockExportState.objects.get(profile=profile)
        self.assertEqual(FbsStockExportState.objects.filter(profile=profile).count(), 1)
        self.assertEqual((result.requests, result.synced, result.blocked), (2, 1, 0))
        self.assertEqual((state.external_item_id, state.barcode), ("8101", self.barcode))
        self.assertEqual(state.last_sent_qty, 7)
        self.assertEqual(
            transport.requests[1][1].body["stocks"],
            [{"chrtId": 8101, "amount": 7}],
        )

    def test_wb_catalog_stops_batch_after_rate_limit_response(self):
        profile = self._profile(
            self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            warehouse_id="101",
        )
        second_sku, second_barcode = self._create_sku(
            self.agency,
            code="A-ART-2",
            barcode="4600000000102",
        )
        FbsStockExportState.objects.create(
            profile=profile,
            sku_ref=second_sku,
            barcode=second_barcode,
            external_item_id=f"wb-unmapped:{second_barcode}",
            status=FbsStockExportState.STATUS_BLOCKED,
            next_attempt_at=timezone.now() - timedelta(seconds=1),
        )
        transport = StubStockTransport(
            _response(
                {},
                status_code=429,
                headers={"X-Ratelimit-Retry": "2"},
            )
        )

        result = sync_profile_stock_exports(profile_id=profile.id, transport=transport)

        blocked_states = list(FbsStockExportState.objects.filter(profile=profile))
        self.assertEqual((result.requests, result.synced, result.blocked), (1, 0, 2))
        self.assertEqual(len(transport.requests), 1)
        self.assertTrue(
            all(
                state.status == FbsStockExportState.STATUS_BLOCKED
                for state in blocked_states
            )
        )
        self.assertTrue(all("HTTP 429" in state.error for state in blocked_states))
        self.assertTrue(
            all(
                state.next_attempt_at >= timezone.now() + timedelta(seconds=1)
                for state in blocked_states
            )
        )

    def test_wb_order_import_reserves_immediately_and_duplicate_is_idempotent(self):
        profile = self._profile(
            self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            warehouse_id="101",
            stock_push=False,
        )
        payload = self._wb_orders_payload(
            warehouse_id="101",
            barcode=self.barcode,
        )

        first = pull_profile_orders(
            profile_id=profile.id,
            transport=StubStockTransport(_response(payload)),
        )
        second = pull_profile_orders(
            profile_id=profile.id,
            transport=StubStockTransport(_response(payload)),
        )

        order = FbsOrder.objects.get(profile=profile)
        self.balance.refresh_from_db()
        self.assertEqual((first.created, second.duplicate), (1, 1))
        self.assertEqual(order.internal_status, FbsOrder.STATUS_RESERVED)
        self.assertEqual((self.balance.qty, self.balance.available_qty, self.balance.reserved_qty), (7, 6, 1))
        self.assertEqual(FbsOrderStockAllocation.objects.filter(order_item__order=order).count(), 1)

    def test_import_is_all_or_nothing_and_never_makes_available_negative(self):
        profile = self._profile(
            self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            warehouse_id="101",
            stock_push=False,
        )
        payload = self._wb_orders_payload(
            warehouse_id="101",
            barcode=self.barcode,
            count=10,
        )

        result = pull_profile_orders(
            profile_id=profile.id,
            transport=StubStockTransport(_response(payload)),
            limit=20,
        )

        self.balance.refresh_from_db()
        self.assertEqual(result.created, 10)
        self.assertEqual((self.balance.qty, self.balance.available_qty, self.balance.reserved_qty), (7, 0, 7))
        self.assertEqual(FbsOrder.objects.filter(internal_status=FbsOrder.STATUS_RESERVED).count(), 7)
        self.assertEqual(FbsOrder.objects.filter(internal_status=FbsOrder.STATUS_AWAITING_STOCK).count(), 3)
        self.assertEqual(FbsOrderStockAllocation.objects.count(), 7)

    def test_wb_cancellation_before_pick_releases_reservation(self):
        profile = self._profile(
            self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            warehouse_id="101",
            stock_push=False,
        )
        pull_profile_orders(
            profile_id=profile.id,
            transport=StubStockTransport(
                _response(
                    self._wb_orders_payload(
                        warehouse_id="101",
                        barcode=self.barcode,
                    )
                )
            ),
        )
        status_transport = StubStockTransport(
            _response(
                {
                    "orders": [
                        {
                            "id": 7000,
                            "supplierStatus": "cancel",
                            "wbStatus": "declined_by_client",
                        }
                    ]
                }
            )
        )

        pull_profile_statuses(profile_id=profile.id, transport=status_transport)

        order = FbsOrder.objects.get(profile=profile)
        self.balance.refresh_from_db()
        allocation = FbsOrderStockAllocation.objects.get(order_item__order=order)
        self.assertEqual(order.internal_status, FbsOrder.STATUS_CANCELLED)
        self.assertEqual((self.balance.qty, self.balance.available_qty, self.balance.reserved_qty), (7, 7, 0))
        self.assertEqual(allocation.status, FbsOrderStockAllocation.STATUS_CANCELED)

    def test_order_from_another_marketplace_warehouse_does_not_reserve(self):
        profile = self._profile(
            self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            warehouse_id="101",
            stock_push=False,
        )

        result = pull_profile_orders(
            profile_id=profile.id,
            transport=StubStockTransport(
                _response(
                    self._wb_orders_payload(
                        warehouse_id="999",
                        barcode=self.barcode,
                    )
                )
            ),
        )

        self.balance.refresh_from_db()
        self.assertEqual(result.skipped, 1)
        self.assertFalse(FbsOrder.objects.exists())
        self.assertEqual((self.balance.available_qty, self.balance.reserved_qty), (7, 0))

    def test_ozon_stock_export_waits_two_minutes_between_same_item_updates(self):
        profile = self._profile(
            self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            warehouse_id="202",
        )
        state = FbsStockExportState.objects.create(
            profile=profile,
            sku_ref=self.sku,
            barcode=self.barcode,
            external_item_id="A-ART-1",
            external_product_id="555001",
        )
        first_transport = StubStockTransport(
            _response({"result": [{"offer_id": "A-ART-1", "updated": True, "errors": []}]})
        )

        first = sync_profile_stock_exports(profile_id=profile.id, transport=first_transport)
        state.refresh_from_db()
        first_next_attempt = state.next_attempt_at
        self.balance.available_qty = 5
        self.balance.reserved_qty = 2
        self.balance.save(update_fields=["available_qty", "reserved_qty", "updated_at"])
        blocked_transport = StubStockTransport()
        blocked = sync_profile_stock_exports(profile_id=profile.id, transport=blocked_transport)

        self.assertEqual((first.requests, first.synced), (1, 1))
        self.assertGreaterEqual(first_next_attempt, timezone.now() + timedelta(seconds=115))
        self.assertEqual((blocked.requests, blocked.skipped), (0, 1))
        self.assertEqual(blocked_transport.requests, [])

        FbsStockExportState.objects.filter(pk=state.pk).update(
            next_attempt_at=timezone.now() - timedelta(seconds=1)
        )
        second_transport = StubStockTransport(
            _response({"result": [{"offer_id": "A-ART-1", "updated": True, "errors": []}]})
        )
        second = sync_profile_stock_exports(profile_id=profile.id, transport=second_transport)

        self.assertEqual((second.requests, second.synced), (1, 1))
        self.assertEqual(
            second_transport.requests[0][1].body["stocks"],
            [
                {
                    "offer_id": "A-ART-1",
                    "product_id": 555001,
                    "stock": 5,
                    "warehouse_id": 202,
                }
            ],
        )

    def test_ozon_catalog_binding_bootstraps_stock_export_without_an_order(self):
        profile = self._profile(
            self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            warehouse_id="202",
        )
        MarketplaceBinding.objects.create(
            sku=self.sku,
            marketplace="OZON",
            external_id="555001",
        )
        transport = StubStockTransport(
            _response(
                {
                    "result": [
                        {"offer_id": "A-ART-1", "updated": True, "errors": []}
                    ]
                }
            )
        )

        result = sync_profile_stock_exports(profile_id=profile.id, transport=transport)

        state = FbsStockExportState.objects.get(profile=profile)
        self.assertEqual((result.requests, result.synced, result.blocked), (1, 1, 0))
        self.assertEqual(
            (
                state.sku_ref_id,
                state.external_item_id,
                state.external_product_id,
                state.last_sent_qty,
            ),
            (self.sku.id, "A-ART-1", "555001", 7),
        )
        self.assertEqual(
            transport.requests[0][1].body["stocks"],
            [
                {
                    "offer_id": "A-ART-1",
                    "product_id": 555001,
                    "stock": 7,
                    "warehouse_id": 202,
                }
            ],
        )

    def test_ozon_catalog_does_not_export_an_unbound_non_ozon_sku(self):
        profile = self._profile(
            self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            warehouse_id="202",
        )
        transport = StubStockTransport()

        result = sync_profile_stock_exports(profile_id=profile.id, transport=transport)

        self.assertEqual((result.requests, result.synced, result.skipped), (0, 0, 1))
        self.assertFalse(FbsStockExportState.objects.filter(profile=profile).exists())
        self.assertEqual(transport.requests, [])

    def test_invalid_external_stock_id_is_blocked_without_api_request(self):
        profile = self._profile(
            self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            warehouse_id="101",
        )
        state = FbsStockExportState.objects.create(
            profile=profile,
            sku_ref=self.sku,
            barcode=self.barcode,
            external_item_id="not-a-wb-chrt-id",
        )
        transport = StubStockTransport()

        result = sync_profile_stock_exports(profile_id=profile.id, transport=transport)

        state.refresh_from_db()
        self.assertEqual((result.requests, result.blocked), (0, 1))
        self.assertEqual(state.status, FbsStockExportState.STATUS_BLOCKED)
        self.assertEqual(transport.requests, [])

    def test_malformed_ozon_success_response_is_scheduled_for_retry(self):
        profile = self._profile(
            self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            warehouse_id="202",
        )
        state = FbsStockExportState.objects.create(
            profile=profile,
            sku_ref=self.sku,
            barcode=self.barcode,
            external_item_id="A-ART-1",
            external_product_id="555001",
        )

        result = sync_profile_stock_exports(
            profile_id=profile.id,
            transport=StubStockTransport(_response({})),
        )

        state.refresh_from_db()
        self.assertEqual((result.requests, result.retry), (1, 1))
        self.assertEqual(state.status, FbsStockExportState.STATUS_RETRY)
        self.assertGreaterEqual(state.next_attempt_at, timezone.now() + timedelta(seconds=115))
