from django.test import TestCase, override_settings

from sklad.models import WarehouseContainer, WarehouseLocation, WarehouseOperation
from sku.models import Agency, SKU, SKUBarcode

from .models import (
    FbsBox,
    FbsIntegrationProfile,
    FbsInventorySession,
    FbsOrder,
    FbsOrderItem,
    FbsPallet,
    FbsRack,
    FbsRackStagingBox,
    FbsStockBalance,
    FbsStockExportState,
    FbsStorageCell,
    FbsStorageLock,
)
from .services.inventory import balance_is_locked
from .services.picking import release_order_reservation, reserve_order_stock
from .services.stock_sync import _available_by_binding


@override_settings(
    FBS_CLIENT_SAFETY_STOCK_DEFAULT=0,
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_STOCK_PUSH_ENABLED=True,
)
class FbsStockExportInternalMovementTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="FBS movement stock client")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="MOVE-1",
            name="Movement stock",
        )
        self.barcode = "4600000000991"
        SKUBarcode.objects.create(
            sku=self.sku,
            value=self.barcode,
            is_primary=True,
        )
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="FBS-MOVE-STOCK-1",
            is_storage=True,
            is_pickable=True,
        )
        self.cell = FbsStorageCell.objects.create(
            cell_code="FBS-MOVE-STOCK-1",
            location=location,
        )
        self.pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-MOVE-PALLET-1",
            cell=self.cell,
            status=FbsPallet.STATUS_ACTIVE,
        )
        self.box = FbsBox.objects.create(
            agency=self.agency,
            pallet=self.pallet,
            box_code="FBS-MOVE-BOX-1",
            status=FbsBox.STATUS_ACTIVE,
        )
        self.balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.box,
            sku_ref=self.sku,
            identity_key="m" * 64,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode=self.barcode,
            qty=7,
            available_qty=7,
        )
        self.wb_profile = self._profile(
            FbsIntegrationProfile.MARKETPLACE_WB,
            "101",
        )
        self.ozon_profile = self._profile(
            FbsIntegrationProfile.MARKETPLACE_OZON,
            "201",
        )

    def _profile(self, marketplace, warehouse_id):
        return FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=marketplace,
            name=f"{marketplace} {warehouse_id}",
            external_account_id=f"{marketplace}-{self.agency.id}",
            external_warehouse_id=warehouse_id,
            stock_mode=FbsIntegrationProfile.STOCK_MODE_MANAGED,
            is_active=True,
            stock_push_enabled=True,
        )

    def assert_exported_for_both_marketplaces(self):
        self.assertEqual(
            _available_by_binding(self.wb_profile).get(self.barcode),
            7,
        )
        self.assertEqual(
            _available_by_binding(self.ozon_profile).get(str(self.sku.id)),
            7,
        )

    def _create_export_states(self):
        wb_state = FbsStockExportState.objects.create(
            profile=self.wb_profile,
            sku_ref=self.sku,
            barcode=self.barcode,
            external_item_id="10001",
            desired_qty=7,
            last_sent_qty=7,
            status=FbsStockExportState.STATUS_SYNCED,
        )
        ozon_state = FbsStockExportState.objects.create(
            profile=self.ozon_profile,
            sku_ref=self.sku,
            barcode=self.barcode,
            external_item_id=self.sku.sku_code,
            desired_qty=7,
            last_sent_qty=7,
            status=FbsStockExportState.STATUS_SYNCED,
        )
        return wb_state, ozon_state

    def _create_order(self, profile):
        order = FbsOrder.objects.create(
            profile=profile,
            external_order_id=f"RESERVE-{profile.marketplace}-{profile.id}",
            internal_status=FbsOrder.STATUS_RECEIVED,
        )
        FbsOrderItem.objects.create(
            order=order,
            external_line_id="1",
            external_sku=self.sku.sku_code,
            barcode=self.barcode,
            sku=self.sku,
            product_name=self.sku.name,
            quantity=1,
        )
        return order

    def test_reserve_and_release_refresh_both_marketplace_exports(self):
        wb_state, ozon_state = self._create_export_states()
        order = self._create_order(self.wb_profile)

        with self.captureOnCommitCallbacks(execute=True):
            result = reserve_order_stock(order_id=order.id)

        self.assertTrue(result.reserved)
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.available_qty, 6)
        self.assertEqual(self.balance.reserved_qty, 1)
        for state in (wb_state, ozon_state):
            state.refresh_from_db()
            self.assertEqual(state.desired_qty, 6)
            self.assertEqual(state.status, FbsStockExportState.STATUS_PENDING)

        with self.captureOnCommitCallbacks(execute=True):
            release_order_reservation(order_id=order.id)

        self.balance.refresh_from_db()
        self.assertEqual(self.balance.available_qty, 7)
        self.assertEqual(self.balance.reserved_qty, 0)
        for state in (wb_state, ozon_state):
            state.refresh_from_db()
            self.assertEqual(state.desired_qty, 7)
            self.assertEqual(state.status, FbsStockExportState.STATUS_PENDING)

    def test_active_internal_relocation_keeps_stock_exportable(self):
        WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
            context_type="fbs_free_relocation",
            context_id=str(self.pallet.id),
            status=WarehouseOperation.STATUS_IN_PROGRESS,
        )

        self.assertTrue(balance_is_locked(self.balance.id))
        self.assert_exported_for_both_marketplaces()

    def test_rack_staging_keeps_confirmed_stock_exportable(self):
        rack_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            location_code="PR-MOVE-STOCK",
            display_name="PR movement stock",
            capacity_containers=10,
            is_topology_visible=False,
            is_fbs_visible=True,
            is_active=True,
        )
        rack = FbsRack.objects.create(location=rack_location)
        FbsRackStagingBox.objects.create(
            box=self.box,
            rack=rack,
            status=FbsRackStagingBox.STATUS_AWAITING,
        )

        self.assertTrue(balance_is_locked(self.balance.id))
        self.assert_exported_for_both_marketplaces()

    def test_pr_box_remains_exportable_after_historical_pallet_is_archived(self):
        rack_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            location_code="PR-PHYSICAL-BOX-STOCK",
            display_name="PR physical box stock",
            capacity_containers=10,
            is_topology_visible=False,
            is_fbs_visible=True,
            is_active=True,
        )
        box_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code=self.box.box_code,
            current_location=rack_location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        self.box.source_container = box_container
        self.box.save(update_fields=["source_container", "updated_at"])
        self.pallet.status = FbsPallet.STATUS_ARCHIVED
        self.pallet.save(update_fields=["status", "updated_at"])

        self.assert_exported_for_both_marketplaces()

    def test_inventory_lock_still_removes_stock_from_export(self):
        session = FbsInventorySession.objects.create(
            scope_type=FbsInventorySession.SCOPE_BOX,
            mode=FbsInventorySession.MODE_IMMEDIATE,
            status=FbsInventorySession.STATUS_COUNTING,
            box=self.box,
        )
        FbsStorageLock.objects.create(
            session=session,
            scope_type=FbsInventorySession.SCOPE_BOX,
            box=self.box,
            block_new_reservations=True,
            is_active=True,
        )

        self.assertTrue(balance_is_locked(self.balance.id))
        self.assertNotIn(self.barcode, _available_by_binding(self.wb_profile))
        self.assertNotIn(str(self.sku.id), _available_by_binding(self.ozon_profile))
