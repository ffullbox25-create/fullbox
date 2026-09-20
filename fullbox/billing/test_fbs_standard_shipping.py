from datetime import date
from decimal import Decimal

from django.db.models import Sum
from django.test import TestCase

from shipping.models import ShippingOrder, ShippingOrderItem
from sku.models import Agency, SKU

from .fbs_standard_shipping import build_standard_shipping_preview, calculate_standard_shipping_fbs
from .models import BillingService, ClientTariffVersion, FbsClientRate


class FbsStandardShippingCalculationTests(TestCase):
    def setUp(self):
        self.client_agency = Agency.objects.create(agn_name="FBS pilot", inn="7700000999")
        self.sku = SKU.objects.create(
            agency=self.client_agency,
            sku_code="FBS-1L",
            name="Товар 1 л",
            length_mm=100,
            width_mm=100,
            height_mm=100,
        )
        self.order = ShippingOrder.objects.create(
            number="FBS-SHP-1",
            agency=self.client_agency,
            status=ShippingOrder.STATUS_SHIPPED,
            planned_ship_date=date(2026, 8, 4),
        )
        ShippingOrderItem.objects.create(
            order=self.order,
            sku=self.sku,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            qty_requested=3,
            qty_shipped=3,
        )
        ClientTariffVersion.objects.create(
            client=self.client_agency,
            name="Основной тариф",
            version_number=1,
            status=ClientTariffVersion.STATUS_ACTIVE,
            valid_from=date(2026, 8, 1),
        )
        for code, name in (
            ("fbs_pick_item", "FBS · Подбор товара"),
            ("fbs_shipping_item", "FBS · Отгрузка товара"),
        ):
            BillingService.objects.create(code=code, name=name, unit="шт", vat_rate="5")
        for operation, price in ((FbsClientRate.OP_PICKING, "5"), (FbsClientRate.OP_SHIPPING, "3.76")):
            FbsClientRate.objects.create(
                client=self.client_agency,
                operation=operation,
                liters_from=Decimal("0"),
                liters_to=Decimal("1"),
                price=Decimal(price),
                unit="шт",
                valid_from=date(2026, 8, 1),
                vat_rate="5",
                vat_type=ClientTariffVersion.VAT_EXTRA,
            )

    def test_preview_uses_planned_shipping_date_and_liter_band(self):
        preview = build_standard_shipping_preview(
            client=self.client_agency,
            date_from=date(2026, 8, 4),
            date_to=date(2026, 8, 4),
        )
        self.assertEqual(len(preview["rows"]), 2)
        self.assertEqual(preview["amount"], Decimal("26.28"))
        self.assertFalse(preview["errors"])

    def test_calculation_keeps_vat_on_top_and_is_idempotent(self):
        first = calculate_standard_shipping_fbs(
            client=self.client_agency,
            date_from=date(2026, 8, 4),
            date_to=date(2026, 8, 4),
        )
        self.assertEqual(first["created_or_updated"], 2)
        application = first["applications"][0]
        self.assertEqual(application.charges.count(), 2)
        self.assertEqual(application.charges.order_by("id").first().vat_rate, "5")
        self.assertEqual(application.charges.aggregate(total=Sum("total_amount"))["total"], Decimal("27.59"))

        second = calculate_standard_shipping_fbs(
            client=self.client_agency,
            date_from=date(2026, 8, 4),
            date_to=date(2026, 8, 4),
        )
        self.assertEqual(second["applications"][0].charges.count(), 2)
