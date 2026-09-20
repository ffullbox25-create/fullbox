from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from sklad.models import WarehouseLocation
from sku.models import Agency, MarketplaceBinding, SKU, SKUBarcode

from .integrations.contracts import NormalizedMarketplaceItem
from .models import (
    FbsBox,
    FbsIntegrationProfile,
    FbsOrder,
    FbsOrderItem,
    FbsOrderStockAllocation,
    FbsPallet,
    FbsStockBalance,
    FbsStorageCell,
)
from .operator_views import _decorate_order_availability
from .services.demand import analyze_replenishment_demand
from .services.picking import prepare_pick_queue, reserve_order_stock
from .services.sync import _resolve_sku


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
)
class FbsBarcodeAliasTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="FBS barcode alias client")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            name="FBS barcode alias profile",
            external_warehouse_id="fbs-barcode-alias-warehouse",
            is_active=True,
        )
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=97,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="FBS-BARCODE-ALIAS-1",
            is_storage=True,
            is_pickable=True,
        )
        cell = FbsStorageCell.objects.create(
            cell_code="FBS-BARCODE-ALIAS-1",
            location=location,
        )
        pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-BARCODE-ALIAS-PALLET",
            cell=cell,
            status=FbsPallet.STATUS_ACTIVE,
        )
        self.box = FbsBox.objects.create(
            agency=self.agency,
            pallet=pallet,
            box_code="FBS-BARCODE-ALIAS-BOX",
            status=FbsBox.STATUS_ACTIVE,
        )
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="FBS-BARCODE-ALIAS-SKU",
            name="Alias product",
        )
        self.order_barcode = "2037419906119"
        self.stock_barcode = "2037761214672"
        SKUBarcode.objects.create(
            sku=self.sku,
            value=self.order_barcode,
            size="0",
            is_primary=True,
        )
        SKUBarcode.objects.create(
            sku=self.sku,
            value=self.stock_barcode,
            size="",
        )
        self.order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="FBS-BARCODE-ALIAS-ORDER",
            internal_status=FbsOrder.STATUS_AWAITING_STOCK,
        )
        self.item = FbsOrderItem.objects.create(
            order=self.order,
            external_line_id="FBS-BARCODE-ALIAS-LINE",
            external_sku=self.sku.sku_code,
            sku=self.sku,
            barcode=self.order_barcode,
            product_name=self.sku.name,
            quantity=1,
            requirements={},
        )
        self.balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.box,
            sku_ref=self.sku,
            identity_key="fbs-barcode-alias-balance",
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            size="0",
            barcode=self.stock_barcode,
            qty=2,
            available_qty=2,
            reserved_qty=0,
        )

    def test_reservation_uses_stock_under_alias_barcode(self):
        result = reserve_order_stock(order_id=self.order.id)

        self.assertTrue(result.reserved)
        self.assertEqual(result.status, FbsOrder.STATUS_RESERVED)
        self.assertEqual(result.reserved_qty, 1)
        allocation = result.allocations[0]
        self.assertEqual(allocation.balance_id, self.balance.id)
        allocation.full_clean()
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.available_qty, 1)
        self.assertEqual(self.balance.reserved_qty, 1)

    def test_wb_nm_id_binding_uses_stocked_ozon_barcode(self):
        self.profile.marketplace = FbsIntegrationProfile.MARKETPLACE_WB
        MarketplaceBinding.objects.create(
            sku=self.sku,
            marketplace=MarketplaceBinding.MARKETPLACE_WB,
            external_id="189608465",
        )
        item = NormalizedMarketplaceItem(
            external_line_id="WB-LINE-1",
            external_sku="189608465",
            product_name=self.sku.name,
            quantity=1,
            binding_ids=("189608465",),
            barcodes=("WB-BARCODE-NOT-IN-CATALOG",),
            sku_codes=(self.sku.sku_code,),
            requirements={},
            raw_payload={},
        )

        sku, mapping_status, barcode = _resolve_sku(self.profile, item)

        self.assertEqual(sku, self.sku)
        self.assertEqual(mapping_status, "matched_wb_binding")
        self.assertEqual(barcode, self.stock_barcode)

    def test_wb_seller_article_uses_stocked_ozon_barcode(self):
        self.profile.marketplace = FbsIntegrationProfile.MARKETPLACE_WB
        item = NormalizedMarketplaceItem(
            external_line_id="WB-LINE-2",
            external_sku="WB-NM-WITHOUT-BINDING",
            product_name=self.sku.name,
            quantity=1,
            binding_ids=("WB-NM-WITHOUT-BINDING",),
            barcodes=(),
            sku_codes=(self.sku.sku_code,),
            requirements={},
            raw_payload={},
        )

        sku, mapping_status, barcode = _resolve_sku(self.profile, item)

        self.assertEqual(sku, self.sku)
        self.assertEqual(mapping_status, "matched_wb_article")
        self.assertEqual(barcode, self.stock_barcode)

    def test_wb_binding_conflict_with_barcode_is_rejected(self):
        self.profile.marketplace = FbsIntegrationProfile.MARKETPLACE_WB
        MarketplaceBinding.objects.create(
            sku=self.sku,
            marketplace=MarketplaceBinding.MARKETPLACE_WB,
            external_id="WB-CONFLICT-NM",
        )
        other_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="FBS-BARCODE-ALIAS-OTHER",
            name="Other product",
        )
        SKUBarcode.objects.create(
            sku=other_sku,
            value="WB-CONFLICT-BARCODE",
            size="0",
            is_primary=True,
        )
        item = NormalizedMarketplaceItem(
            external_line_id="WB-LINE-CONFLICT",
            external_sku="WB-CONFLICT-NM",
            product_name="Conflicting product",
            quantity=1,
            binding_ids=("WB-CONFLICT-NM",),
            barcodes=("WB-CONFLICT-BARCODE",),
            sku_codes=(),
            requirements={},
            raw_payload={},
        )

        sku, mapping_status, barcode = _resolve_sku(self.profile, item)

        self.assertIsNone(sku)
        self.assertEqual(mapping_status, "wb_sku_ambiguous")
        self.assertEqual(barcode, "")

    def test_alias_reservation_can_be_added_to_pick_queue(self):
        result = prepare_pick_queue(
            limit=1,
            order_ids=[self.order.id],
            single_agency_only=True,
            max_orders_per_batch=50,
            max_units_per_batch=100,
            create_batches=True,
        )

        self.assertEqual(result.reserved_orders, 1)
        self.assertEqual(result.tasks_created, 1)
        self.assertEqual(len(result.batches), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.internal_status, FbsOrder.STATUS_QUEUED_FOR_PICK)
        allocation = FbsOrderStockAllocation.objects.get(order_item=self.item)
        self.assertEqual(allocation.balance_id, self.balance.id)
        self.assertEqual(allocation.pick_task.batch_id, result.batches[0].id)

    def test_availability_uses_all_barcodes_of_same_sku_variant(self):
        _decorate_order_availability([self.order])

        self.assertEqual(self.order.availability_state, "fbs_available")
        self.assertEqual(self.order.availability_rows[0]["fbs_fact_qty"], 2)
        self.assertEqual(self.order.availability_rows[0]["fbs_available_qty"], 2)

    def test_demand_does_not_request_replenishment_for_alias_stock(self):
        analysis = analyze_replenishment_demand(agency=self.agency)

        self.assertEqual(len(analysis.rows), 1)
        self.assertEqual(analysis.rows[0].ordered_qty, 1)
        self.assertEqual(analysis.rows[0].fbs_qty, 2)
        self.assertEqual(analysis.rows[0].uncovered_qty, 0)
        self.assertEqual(analysis.rows[0].proposed_qty, 0)

    def test_different_size_barcode_is_not_an_alias(self):
        other_barcode = "2037000000001"
        SKUBarcode.objects.create(
            sku=self.sku,
            value=other_barcode,
            size="1",
        )
        other_balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.box,
            sku_ref=self.sku,
            identity_key="fbs-barcode-other-size",
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            size="1",
            barcode=other_barcode,
            qty=1,
            available_qty=1,
            reserved_qty=0,
        )
        allocation = FbsOrderStockAllocation(
            order_item=self.item,
            balance=other_balance,
            qty_reserved=1,
        )

        with self.assertRaises(ValidationError):
            allocation.full_clean()

        self.balance.qty = 0
        self.balance.available_qty = 0
        self.balance.save(update_fields=["qty", "available_qty", "updated_at"])
        result = reserve_order_stock(order_id=self.order.id)
        self.assertFalse(result.reserved)
        self.assertEqual(result.status, FbsOrder.STATUS_AWAITING_STOCK)
