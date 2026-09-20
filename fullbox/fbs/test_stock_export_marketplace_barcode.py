from django.test import TestCase, override_settings
from django.utils import timezone

from sklad.models import WarehouseLocation
from sku.models import Agency, MarketplaceBinding, SKU

from .models import (
    FbsBox,
    FbsIntegrationProfile,
    FbsPallet,
    FbsStockBalance,
    FbsStockExportState,
    FbsStorageCell,
)
from .services.stock_sync import (
    WB_UNMAPPED_PREFIX,
    _available_by_binding,
    _ensure_ozon_stock_catalog_states,
    _ensure_wb_stock_catalog_states,
    _refresh_wb_stock_catalog,
)


class _UnexpectedTransport:
    def __init__(self):
        self.calls = 0

    def send(self, profile, spec):
        self.calls += 1
        raise AssertionError("Ozon barcode must not be sent to the WB Content API")


@override_settings(FBS_CLIENT_SAFETY_STOCK_DEFAULT=0)
class FbsStockExportMarketplaceBarcodeTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Marketplace barcode client")
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="FBS-MARKETPLACE-BARCODE-1",
            is_storage=True,
            is_pickable=True,
        )
        self.cell = FbsStorageCell.objects.create(
            cell_code="FBS-MARKETPLACE-BARCODE-1",
            location=location,
        )
        self.pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-MARKETPLACE-BARCODE-PALLET-1",
            cell=self.cell,
            status=FbsPallet.STATUS_ACTIVE,
        )
        self.box = FbsBox.objects.create(
            agency=self.agency,
            pallet=self.pallet,
            box_code="FBS-MARKETPLACE-BARCODE-BOX-1",
            status=FbsBox.STATUS_ACTIVE,
        )
        self.wb_profile = self._profile(
            FbsIntegrationProfile.MARKETPLACE_WB,
            "wb-warehouse",
        )

    def _profile(self, marketplace, warehouse_id):
        return FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=marketplace,
            name=f"{marketplace} test",
            external_account_id=f"{marketplace}-{self.agency.id}",
            external_warehouse_id=warehouse_id,
            stock_mode=FbsIntegrationProfile.STOCK_MODE_MANAGED,
            is_active=True,
            stock_push_enabled=True,
        )

    def _balance(self, barcode, *, suffix):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code=f"SKU-{suffix}",
            name=f"Product {suffix}",
        )
        FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.box,
            sku_ref=sku,
            identity_key=str(suffix).rjust(64, "0"),
            sku_code=sku.sku_code,
            name=sku.name,
            barcode=barcode,
            qty=7,
            available_qty=7,
        )
        return sku

    def test_uppercase_ozon_barcode_does_not_create_wb_placeholder(self):
        self._balance("OZN2446436869", suffix=1)

        created = _ensure_wb_stock_catalog_states(self.wb_profile)

        self.assertEqual(created, 0)
        self.assertFalse(
            FbsStockExportState.objects.filter(profile=self.wb_profile).exists()
        )

    def test_lowercase_ozon_barcode_does_not_create_wb_placeholder(self):
        self._balance("ozn2446436869", suffix=2)

        created = _ensure_wb_stock_catalog_states(self.wb_profile)

        self.assertEqual(created, 0)
        self.assertFalse(
            FbsStockExportState.objects.filter(profile=self.wb_profile).exists()
        )

    def test_regular_ean_still_creates_wb_placeholder(self):
        sku = self._balance("4600000000003", suffix=3)

        created = _ensure_wb_stock_catalog_states(self.wb_profile)

        self.assertEqual(created, 1)
        state = FbsStockExportState.objects.get(profile=self.wb_profile)
        self.assertEqual(state.sku_ref, sku)
        self.assertEqual(state.barcode, "4600000000003")
        self.assertEqual(
            state.external_item_id,
            f"{WB_UNMAPPED_PREFIX}4600000000003",
        )
        self.assertEqual(state.status, FbsStockExportState.STATUS_BLOCKED)

    def test_ozon_barcode_remains_exportable_to_ozon(self):
        sku = self._balance("OZN2446436869", suffix=4)
        MarketplaceBinding.objects.create(
            sku=sku,
            marketplace=MarketplaceBinding.MARKETPLACE_OZON,
            external_id="2446436869",
        )
        ozon_profile = self._profile(
            FbsIntegrationProfile.MARKETPLACE_OZON,
            "ozon-warehouse",
        )

        created = _ensure_ozon_stock_catalog_states(ozon_profile)

        self.assertEqual(created, 1)
        self.assertEqual(_available_by_binding(ozon_profile), {str(sku.id): 7})
        state = FbsStockExportState.objects.get(profile=ozon_profile)
        self.assertEqual(state.sku_ref, sku)
        self.assertEqual(state.external_item_id, sku.sku_code)

    def test_existing_ozon_placeholder_is_not_sent_to_wb_content_api(self):
        sku = self._balance("ozn2446436869", suffix=5)
        state = FbsStockExportState.objects.create(
            profile=self.wb_profile,
            sku_ref=sku,
            barcode="ozn2446436869",
            external_item_id=f"{WB_UNMAPPED_PREFIX}ozn2446436869",
            status=FbsStockExportState.STATUS_BLOCKED,
            next_attempt_at=timezone.now(),
        )
        transport = _UnexpectedTransport()

        result = _refresh_wb_stock_catalog(
            profile=self.wb_profile,
            transport=transport,
        )

        self.assertEqual(result.requests, 0)
        self.assertEqual(transport.calls, 0)
        state.refresh_from_db()
        self.assertEqual(state.status, FbsStockExportState.STATUS_BLOCKED)

