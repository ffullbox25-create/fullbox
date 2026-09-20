from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from head_manager.models import OwnCompany
from shipping.models import ShippingOrder
from sku.models import Agency, Market

from .models import (
    BillingApplication,
    BillingService,
    ClientBillingContract,
    ClientLogisticsTariff,
    ClientLogisticsTariffItem,
    ClientTariffItem,
    ClientTariffVersion,
    TariffCategory,
    TariffUnit,
)
from .price_resolver import resolve_client_service_price


class LogisticsDirectionFallbackTests(TestCase):
    def setUp(self):
        self.client_agency = Agency.objects.create(
            agn_name="Logistics fallback client",
            short_name="Fallback",
            inn="7700000100",
        )
        self.company = OwnCompany.objects.create(
            name="FullBox fallback",
            short_name="FB fallback",
            tax_mode=OwnCompany.TAX_MODE_VAT,
            vat_rate="20",
            is_default=True,
        )
        self.contract = ClientBillingContract.objects.create(
            client=self.client_agency,
            own_company=self.company,
            pricing_mode=ClientBillingContract.PRICING_INDIVIDUAL,
        )
        self.service, _ = BillingService.objects.get_or_create(
            code="logistics_pickup_goods_market",
            defaults={
                "name": "Доставка до склада маркетплейса",
                "unit": "шт",
                "vat_rate": "20",
            },
        )
        category, _ = TariffCategory.objects.get_or_create(
            code="logistics-fallback",
            defaults={"name": "Логистика fallback", "sort_order": 100},
        )
        unit, _ = TariffUnit.objects.get_or_create(
            code="logistics-fallback-piece",
            defaults={"name": "Штука", "short_name": "шт"},
        )
        self.tariff_version = ClientTariffVersion.objects.create(
            client=self.client_agency,
            contract=self.contract,
            name="Обычный тариф клиента",
            version_number=1,
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=timezone.localdate(),
            vat_type=ClientTariffVersion.VAT_EXTRA,
        )
        self.tariff_item = ClientTariffItem.objects.create(
            tariff_version=self.tariff_version,
            category=category,
            service=self.service,
            service_name="Доставка товара до ПВЗ OZON (30 коробок)",
            unit=unit,
            price=Decimal("1500.0000"),
        )
        self.tariff_version.status = ClientTariffVersion.STATUS_ACTIVE
        self.tariff_version.save(update_fields=["status", "updated_at"])

        self.market = Market.objects.create(id=900001, name="OZON")
        self.application = BillingApplication.objects.create(
            application_type=BillingApplication.TYPE_SHIPPING,
            application_id="OTG-FALLBACK",
            client=self.client_agency,
            legal_entity=self.client_agency,
            own_company=self.company,
            created_at_source=timezone.now(),
        )
        ShippingOrder.objects.create(
            number=self.application.application_id,
            agency=self.client_agency,
            marketplace=self.market,
            destination_warehouse="ПВЗ Старая Купавна",
        )
        self.logistics_tariff = ClientLogisticsTariff.objects.create(
            client=self.client_agency,
            name="Маршрутный тариф",
            version_number=1,
            status=ClientLogisticsTariff.STATUS_ACTIVE,
            valid_from=timezone.localdate(),
        )
        ClientLogisticsTariffItem.objects.create(
            tariff=self.logistics_tariff,
            marketplace="ozon",
            warehouse_name="Щербинка",
            price_per_pallet=Decimal("3300.00"),
        )

    def test_missing_direction_falls_back_to_agreed_client_service_tariff(self):
        resolved = resolve_client_service_price(
            self.application,
            self.service,
            performed_at=timezone.now(),
            quantity=Decimal("1"),
        )

        self.assertTrue(resolved.ok)
        self.assertEqual(resolved.source, "agreed_tariff")
        self.assertEqual(resolved.tariff, Decimal("1500.0000"))
        self.assertEqual(resolved.tariff_item, self.tariff_item)
        self.assertIsNone(resolved.logistics_tariff_item)

    def test_matching_direction_keeps_logistics_tariff_priority(self):
        matching_item = ClientLogisticsTariffItem.objects.create(
            tariff=self.logistics_tariff,
            marketplace="ozon",
            warehouse_name="ПВЗ Старая Купавна",
            price_per_pallet=Decimal("3500.00"),
            sort_order=1,
        )

        resolved = resolve_client_service_price(
            self.application,
            self.service,
            performed_at=timezone.now(),
            quantity=Decimal("1"),
        )

        self.assertTrue(resolved.ok)
        self.assertEqual(resolved.source, "client_logistics_tariff")
        self.assertEqual(resolved.tariff, Decimal("3500.00"))
        self.assertEqual(resolved.logistics_tariff_item, matching_item)
        self.assertIsNone(resolved.tariff_item)

    def test_shipping_direction_can_use_other_marketplace_logistics_tariff(self):
        other_item = ClientLogisticsTariffItem.objects.create(
            tariff=self.logistics_tariff,
            marketplace="other",
            warehouse_name="ПВЗ Старая Купавна",
            price_per_pallet=Decimal("3100.00"),
            sort_order=1,
        )

        resolved = resolve_client_service_price(
            self.application,
            self.service,
            performed_at=timezone.now(),
            quantity=Decimal("1"),
        )

        self.assertTrue(resolved.ok)
        self.assertEqual(resolved.source, "client_logistics_tariff")
        self.assertEqual(resolved.tariff, Decimal("3100.00"))
        self.assertEqual(resolved.logistics_tariff_item, other_item)
        self.assertIsNone(resolved.tariff_item)
