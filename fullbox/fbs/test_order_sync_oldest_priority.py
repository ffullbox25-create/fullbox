from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from fbs.exceptions import FbsFeatureDisabled
from fbs.integrations.http import MarketplaceHttpResponse
from fbs.models import FbsIntegrationProfile, FbsOrder, FbsSyncCursor
from fbs.services.picking import _ordered_pick_batch_chunks
from fbs.services.sync import pull_profile_orders
from sku.models import Agency, SKU, SKUBarcode


class StubReadTransport:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def send(self, profile, spec):
        self.requests.append((profile, spec))
        return self.response


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_ORDER_PULL_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=False,
)
class FbsWbOrderReconciliationTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="FBS reconciliation client")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="RECON-SKU-1",
            name="Reconciliation product",
        )
        SKUBarcode.objects.create(
            sku=self.sku,
            value="4600000000001",
            is_primary=True,
        )
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB reconciliation",
            external_account_id="wb-reconciliation",
            external_warehouse_id="101",
            is_active=True,
            order_pull_enabled=True,
        )

    @staticmethod
    def _response(orders):
        return MarketplaceHttpResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            content=b"",
            json_payload={"orders": orders},
        )

    @staticmethod
    def _order(order_id, warehouse_id):
        return {
            "id": order_id,
            "warehouseId": warehouse_id,
            "nmId": 9001,
            "chrtId": 8001,
            "skus": ["4600000000001"],
            "createdAt": "2026-08-30T08:00:00Z",
            "requiredMeta": [],
            "optionalMeta": [],
            "cargoType": 1,
            "options": {"isB2b": False},
        }

    def test_missing_target_warehouse_order_is_prioritized_after_warehouse_filter(self):
        transport = StubReadTransport(
            self._response(
                [
                    self._order(7001, 999),
                    self._order(7002, 999),
                    self._order(7003, 101),
                ]
            )
        )

        result = pull_profile_orders(
            profile_id=self.profile.id,
            transport=transport,
            limit=2,
        )

        self.assertEqual(result.created, 1)
        self.assertTrue(
            FbsOrder.objects.filter(
                profile=self.profile,
                external_order_id="7003",
            ).exists()
        )
        cursor = FbsSyncCursor.objects.get(
            profile=self.profile,
            stream=FbsSyncCursor.STREAM_ORDERS,
        )
        self.assertEqual(cursor.cursor["wb_live_total"], 3)
        self.assertEqual(cursor.cursor["wb_profile_warehouse"], 1)
        self.assertEqual(cursor.cursor["wb_missing_before"], 1)
        self.assertEqual(cursor.cursor["wb_missing_after"], 0)

    def test_disabled_order_pull_checkbox_makes_no_marketplace_request(self):
        self.profile.order_pull_enabled = False
        self.profile.save(update_fields=["order_pull_enabled", "updated_at"])
        transport = Mock()

        with self.assertRaises(FbsFeatureDisabled):
            pull_profile_orders(
                profile_id=self.profile.id,
                transport=transport,
                limit=250,
            )

        transport.send.assert_not_called()
        self.assertFalse(FbsOrder.objects.filter(profile=self.profile).exists())


class FbsOldestWavePriorityTests(SimpleTestCase):
    def test_batch_chunks_are_global_oldest_first_across_clients(self):
        now = timezone.now()
        newest = SimpleNamespace(
            id=1,
            profile=SimpleNamespace(agency_id=1),
            external_order_id="NEW",
            ordered_at=now,
            imported_at=now,
        )
        oldest = SimpleNamespace(
            id=2,
            profile=SimpleNamespace(agency_id=2),
            external_order_id="OLD",
            ordered_at=now - timedelta(hours=20),
            imported_at=now - timedelta(hours=20),
        )
        allocations = {
            newest.id: [SimpleNamespace(qty_reserved=1)],
            oldest.id: [SimpleNamespace(qty_reserved=1)],
        }

        chunks = _ordered_pick_batch_chunks(
            orders_by_profile={1: [newest], 2: [oldest]},
            allocations_by_order=allocations,
            max_orders_per_batch=50,
            max_units_per_batch=100,
        )

        self.assertEqual([agency_id for agency_id, _profile, _chunk in chunks], [2, 1])
        self.assertEqual([chunk[0].external_order_id for _agency, _profile, chunk in chunks], ["OLD", "NEW"])

    def test_batch_chunks_skip_empty_profile_group(self):
        now = timezone.now()
        order = SimpleNamespace(
            id=1,
            profile=SimpleNamespace(agency_id=2),
            external_order_id="PENDING",
            ordered_at=now,
            imported_at=now,
        )

        chunks = _ordered_pick_batch_chunks(
            orders_by_profile={1: [], 2: [order]},
            allocations_by_order={
                order.id: [SimpleNamespace(qty_reserved=1)],
            },
            max_orders_per_batch=50,
            max_units_per_batch=100,
        )

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0][1], 2)
        self.assertEqual(chunks[0][2], [order])

    def test_batch_chunks_return_empty_when_every_profile_group_is_empty(self):
        chunks = _ordered_pick_batch_chunks(
            orders_by_profile={1: [], 2: []},
            allocations_by_order={},
            max_orders_per_batch=50,
            max_units_per_batch=100,
        )

        self.assertEqual(chunks, ())

    def test_batch_chunks_accept_profile_consumed_by_reused_batch(self):
        now = timezone.now()
        reused_order = SimpleNamespace(
            id=1,
            profile=SimpleNamespace(agency_id=1),
            external_order_id="REUSED",
            ordered_at=now,
            imported_at=now,
        )

        chunks = _ordered_pick_batch_chunks(
            # create_pick_batches keeps the profile key after reuse consumes
            # every pending order; its allocations still exist in the map.
            orders_by_profile={1: []},
            allocations_by_order={
                reused_order.id: [SimpleNamespace(qty_reserved=1)],
            },
            max_orders_per_batch=50,
            max_units_per_batch=100,
        )

        self.assertEqual(chunks, ())
