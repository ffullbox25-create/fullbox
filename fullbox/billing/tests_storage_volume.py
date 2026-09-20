"""Unit-тесты объёма и движка хранения (без склада)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from employees.models import Employee
from sku.models import Agency

from .models import (
    BillingApplication,
    BillingService,
    BillingStorageDay,
    ClientBillingContract,
    ClientTariffItem,
    ClientTariffVersion,
    OwnCompany,
    StorageBillingError,
    StorageBillingPeriod,
    StorageCalculationRule,
    StorageSnapshotLine,
    TariffCategory,
    TariffUnit,
)
from .storage_billing import StorageBillingService
from .storage_engine import apply_free_period, calculate_storage_day, default_storage_rule, service_code_for_mode
from .storage_volume import (
    collect_volume_items,
    liters_to_m3,
    mm_to_liters,
    quantize_volume,
    round_volume,
    summarize_items,
)


class StorageVolumeMathTests(TestCase):
    def test_01_cm_box_to_liters(self):
        # 60×40×40 см = 600×400×400 мм = 96 л
        self.assertEqual(mm_to_liters(600, 400, 400), Decimal("96.000000"))

    def test_02_liters_to_m3(self):
        self.assertEqual(liters_to_m3(Decimal("96")), Decimal("0.096000"))

    def test_03_m3_equals_1000_liters(self):
        self.assertEqual(liters_to_m3(Decimal("1000")), Decimal("1.000000"))

    def test_04_zero_dims(self):
        self.assertEqual(mm_to_liters(0, 100, 100), Decimal("0"))

    def test_05_liter_day_quantity(self):
        rule = StorageCalculationRule(
            billing_mode=StorageCalculationRule.MODE_LITER_DAY,
            volume_level=StorageCalculationRule.LEVEL_UNIT,
            rounding_mode=StorageCalculationRule.ROUND_NONE,
            space_coefficient_default=Decimal("1"),
            missing_dims_policy=StorageCalculationRule.MISSING_SKIP,
        )
        rows = [
            {
                "qty": 10,
                "sku_code": "A",
                "length_mm": 600,
                "width_mm": 400,
                "height_mm": 400,
                "zone": "OS",
            }
        ]
        calc = calculate_storage_day(rows, rule, day=date(2026, 7, 15))
        # 10 * 96 = 960 л
        self.assertEqual(calc.quantity, Decimal("960.000000"))
        self.assertEqual(calc.unit, "л")

    def test_06_m3_day_and_month_prorate(self):
        rule = StorageCalculationRule(
            billing_mode=StorageCalculationRule.MODE_M3_MONTH,
            month_mode=StorageCalculationRule.MONTH_CALENDAR_PRORATE,
            volume_level=StorageCalculationRule.LEVEL_UNIT,
            rounding_mode=StorageCalculationRule.ROUND_NONE,
            space_coefficient_default=Decimal("1"),
            missing_dims_policy=StorageCalculationRule.MISSING_SKIP,
        )
        rows = [
            {
                "qty": 1,
                "sku_code": "A",
                "length_mm": 1000,
                "width_mm": 1000,
                "height_mm": 1000,
                "zone": "OS",
            }
        ]
        # 1 м³ / 31 дней июля
        calc = calculate_storage_day(rows, rule, day=date(2026, 7, 15))
        self.assertAlmostEqual(float(calc.quantity), 1.0 / 31.0, places=5)


class StorageDayRulesTests(TestCase):
    def test_07_free_calendar_days(self):
        rule = StorageCalculationRule(
            free_period_type=StorageCalculationRule.FREE_CALENDAR_DAYS,
            free_period_value=Decimal("3"),
        )
        receive = date(2026, 7, 1)
        self.assertTrue(apply_free_period(rule, receive_day=receive, day=date(2026, 7, 1)))
        self.assertTrue(apply_free_period(rule, receive_day=receive, day=date(2026, 7, 3)))
        self.assertFalse(apply_free_period(rule, receive_day=receive, day=date(2026, 7, 4)))

    def test_08_free_skips_charge(self):
        rule = StorageCalculationRule(
            billing_mode=StorageCalculationRule.MODE_PALLET_DAY,
            free_period_type=StorageCalculationRule.FREE_CALENDAR_DAYS,
            free_period_value=Decimal("2"),
            volume_level=StorageCalculationRule.LEVEL_PALLET,
            missing_dims_policy=StorageCalculationRule.MISSING_SKIP,
        )
        calc = calculate_storage_day(
            [{"qty": 1, "pallet_code": "P1", "zone": "OS"}],
            rule,
            day=date(2026, 7, 1),
            pallet_counts={"total": 1, "by_zone": {"OS": 1}},
            receive_day=date(2026, 7, 1),
        )
        self.assertTrue(calc.free_day)
        self.assertTrue(calc.skip_charge)

    def test_09_min_billable_volume(self):
        rule = StorageCalculationRule(
            billing_mode=StorageCalculationRule.MODE_LITER_DAY,
            volume_level=StorageCalculationRule.LEVEL_UNIT,
            min_billable_volume=Decimal("100"),
            space_coefficient_default=Decimal("1"),
            missing_dims_policy=StorageCalculationRule.MISSING_SKIP,
        )
        rows = [{"qty": 1, "sku_code": "A", "length_mm": 100, "width_mm": 100, "height_mm": 100, "zone": "OS"}]
        # 1 л → поднимается до 100
        calc = calculate_storage_day(rows, rule, day=date(2026, 7, 1))
        self.assertEqual(calc.quantity, Decimal("100.000000"))


class StorageRoundingCoefficientTests(TestCase):
    def test_10_round_ceil_liter(self):
        self.assertEqual(round_volume(Decimal("10.1"), StorageCalculationRule.ROUND_LITER), Decimal("11"))

    def test_11_coefficient(self):
        rule = StorageCalculationRule(
            billing_mode=StorageCalculationRule.MODE_LITER_DAY,
            volume_level=StorageCalculationRule.LEVEL_UNIT,
            space_coefficient_default=Decimal("1.5"),
            missing_dims_policy=StorageCalculationRule.MISSING_SKIP,
        )
        rows = [{"qty": 1, "sku_code": "A", "length_mm": 100, "width_mm": 100, "height_mm": 100, "zone": "OS"}]
        calc = calculate_storage_day(rows, rule, day=date(2026, 7, 1))
        self.assertEqual(calc.quantity, Decimal("1.500000"))

    def test_12_service_code_by_mode(self):
        self.assertEqual(service_code_for_mode(StorageCalculationRule.MODE_M3_DAY), "storage_m3_day")
        self.assertEqual(service_code_for_mode(StorageCalculationRule.MODE_PALLET_DAY), "storage_pallet_day")
        self.assertEqual(service_code_for_mode(StorageCalculationRule.MODE_PALLET_WEEK), "storage_pallet_week")

    def test_12b_week_prorate_pallet(self):
        rule = StorageCalculationRule(
            billing_mode=StorageCalculationRule.MODE_PALLET_WEEK,
            charge_basis=StorageCalculationRule.BASIS_PALLET,
            charge_period=StorageCalculationRule.PERIOD_WEEK,
            volume_level=StorageCalculationRule.LEVEL_PALLET,
            missing_dims_policy=StorageCalculationRule.MISSING_SKIP,
        )
        calc = calculate_storage_day(
            [{"qty": 1, "pallet_code": "P1", "zone": "OS"}],
            rule,
            day=date(2026, 7, 1),
            pallet_counts={"total": 7, "by_zone": {"OS": 7}},
        )
        # 7 палет × недельная цена → за день снимка quantity = 7/7 = 1
        self.assertEqual(calc.quantity, Decimal("1.000000"))
        self.assertEqual(calc.payload.get("charge_period"), "week")


class StorageAntiDoubleCountTests(TestCase):
    def test_13_no_dims_status(self):
        items = collect_volume_items(
            [{"qty": 5, "sku_code": "X", "zone": "OS"}],
            volume_level=StorageCalculationRule.LEVEL_UNIT,
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].status, "no_dims")
        summary = summarize_items(items)
        self.assertEqual(summary["billable_volume_l"], Decimal("0"))
        self.assertEqual(summary["no_dims_count"], 1)

    def test_14_single_day_pallet_count(self):
        rule = default_storage_rule()
        calc = calculate_storage_day(
            [
                {"qty": 10, "pallet_code": "P1", "zone": "OS", "sku_code": "A"},
                {"qty": 5, "pallet_code": "P1", "zone": "OS", "sku_code": "B"},
                {"qty": 1, "pallet_code": "P2", "zone": "OBR", "sku_code": "C"},
            ],
            rule,
            day=date(2026, 7, 1),
            pallet_counts={"total": 2, "by_zone": {"OS": 1, "OBR": 1}},
        )
        self.assertEqual(calc.quantity, Decimal("2"))
        self.assertEqual(calc.pallet_count, 2)

    def test_15_box_level_dedup(self):
        items = collect_volume_items(
            [
                {
                    "qty": 10,
                    "box_code": "B1",
                    "pallet_code": "P1",
                    "sku_code": "A",
                    "length_mm": 600,
                    "width_mm": 400,
                    "height_mm": 400,
                    "zone": "OS",
                },
                {
                    "qty": 10,
                    "box_code": "B1",
                    "pallet_code": "P1",
                    "sku_code": "A",
                    "length_mm": 600,
                    "width_mm": 400,
                    "height_mm": 400,
                    "zone": "OS",
                },
            ],
            volume_level=StorageCalculationRule.LEVEL_BOX,
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].total_volume_l, Decimal("96.000000"))

    def test_16_pallet_level_dedup(self):
        items = collect_volume_items(
            [
                {"qty": 1, "pallet_code": "P1", "zone": "OS"},
                {"qty": 2, "pallet_code": "P1", "zone": "OS"},
            ],
            volume_level=StorageCalculationRule.LEVEL_PALLET,
        )
        self.assertEqual(len(items), 1)

    def test_17_anti_double_count_unit_not_plus_box(self):
        # unit level counts qty only once per row — box_code ignored for double add
        items = collect_volume_items(
            [
                {
                    "qty": 2,
                    "box_code": "B1",
                    "pallet_code": "P1",
                    "sku_code": "A",
                    "length_mm": 100,
                    "width_mm": 100,
                    "height_mm": 100,
                    "zone": "OS",
                }
            ],
            volume_level=StorageCalculationRule.LEVEL_UNIT,
        )
        self.assertEqual(items[0].total_volume_l, Decimal("2.000000"))

    def test_18_no_dims_error_policy(self):
        rule = StorageCalculationRule(
            billing_mode=StorageCalculationRule.MODE_LITER_DAY,
            volume_level=StorageCalculationRule.LEVEL_UNIT,
            missing_dims_policy=StorageCalculationRule.MISSING_ERROR,
            space_coefficient_default=Decimal("1"),
        )
        calc = calculate_storage_day(
            [{"qty": 5, "sku_code": "NODIM", "zone": "OS"}],
            rule,
            day=date(2026, 7, 1),
        )
        self.assertTrue(calc.errors)
        self.assertTrue(calc.skip_charge)
        self.assertEqual(calc.skip_reason, "no_dims")


class StorageBillingIntegrationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="stor_mgr", password="pass", is_staff=True)
        self.manager = Employee.objects.create(full_name="Stor Manager", role="manager", user=self.user)
        self.client_agency = Agency.objects.create(agn_name="Stor Client", short_name="Stor", inn="7700000099")
        self.company = OwnCompany.objects.create(
            name="FB", short_name="FB", tax_mode=OwnCompany.TAX_MODE_VAT, vat_rate="20", is_default=True
        )
        self.contract = ClientBillingContract.objects.create(
            client=self.client_agency, own_company=self.company, pricing_mode=ClientBillingContract.PRICING_INDIVIDUAL
        )
        self.category, _ = TariffCategory.objects.get_or_create(
            code="storage", defaults={"name": "Хранение", "sort_order": 10}
        )
        self.unit, _ = TariffUnit.objects.get_or_create(
            code="pallet", defaults={"name": "Палета", "short_name": "пал."}
        )
        self.service, _ = BillingService.objects.get_or_create(
            code="storage_pallet_day",
            defaults={"name": "Хранение палеты/день", "unit": "пал.", "vat_rate": "20"},
        )
        self.version = ClientTariffVersion.objects.create(
            client=self.client_agency,
            contract=self.contract,
            name="Stor tariff",
            version_number=1,
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=timezone.localdate() - timedelta(days=30),
            manager=self.manager,
            created_by=self.user,
        )
        ClientTariffItem.objects.create(
            tariff_version=self.version,
            category=self.category,
            service=self.service,
            service_name=self.service.name,
            unit=self.unit,
            price=Decimal("100"),
        )
        StorageCalculationRule.objects.create(
            tariff_version=self.version,
            billing_mode=StorageCalculationRule.MODE_PALLET_DAY,
            volume_level=StorageCalculationRule.LEVEL_PALLET,
            missing_dims_policy=StorageCalculationRule.MISSING_SKIP,
        )
        self.version.status = ClientTariffVersion.STATUS_ACTIVE
        self.version.save(update_fields=["status", "updated_at"])

    @patch("billing.storage_billing.count_client_pallets")
    def test_19_record_day_creates_charge_and_lines(self, mock_counts):
        mock_counts.return_value = {
            "total": 2,
            "by_zone": {"OS": 2, "OBR": 0, "OTG": 0},
            "pallet_codes": ["P1", "P2"],
            "rows": [
                {"qty": 1, "pallet_code": "P1", "zone": "OS", "sku_code": "A", "sku_ref_id": 0},
                {"qty": 1, "pallet_code": "P2", "zone": "OS", "sku_code": "B", "sku_ref_id": 0},
            ],
        }
        day = timezone.localdate()
        row = StorageBillingService.record_storage_day(self.client_agency, day=day, user=self.user)
        self.assertEqual(row.pallet_count, 2)
        self.assertEqual(row.billing_mode, StorageCalculationRule.MODE_PALLET_DAY)
        self.assertIsNotNone(row.charge)
        self.assertEqual(row.charge.quantity, Decimal("2"))
        self.assertTrue(row.lines.exists())
        self.assertEqual(row.application.application_type, BillingApplication.TYPE_STORAGE)

    @patch("billing.storage_billing.count_client_pallets")
    def test_20_closed_period_forbids_recalc(self, mock_counts):
        day = timezone.localdate()
        StorageBillingPeriod.objects.create(
            client=self.client_agency,
            legal_entity=self.client_agency,
            year=day.year,
            month=day.month,
            status=StorageBillingPeriod.STATUS_CLOSED,
        )
        mock_counts.return_value = {"total": 5, "by_zone": {}, "rows": []}
        row = StorageBillingService.record_storage_day(self.client_agency, day=day, user=self.user)
        self.assertEqual(row.payload.get("skipped"), "period_closed")
        self.assertTrue(
            StorageBillingError.objects.filter(
                client=self.client_agency, error_type=StorageBillingError.TYPE_CLOSED_PERIOD
            ).exists()
        )
        mock_counts.assert_not_called()

    def test_21_quantize_volume_precision(self):
        self.assertEqual(quantize_volume("1.2345678"), Decimal("1.234568"))

    @patch("billing.storage_billing.count_client_pallets")
    def test_22_zero_day_removes_charge(self, mock_counts):
        day = timezone.localdate()
        mock_counts.return_value = {
            "total": 1,
            "by_zone": {"OS": 1},
            "rows": [{"qty": 1, "pallet_code": "P1", "zone": "OS"}],
        }
        row1 = StorageBillingService.record_storage_day(self.client_agency, day=day, user=self.user)
        self.assertIsNotNone(row1.charge)
        mock_counts.return_value = {"total": 0, "by_zone": {}, "rows": []}
        row2 = StorageBillingService.record_storage_day(self.client_agency, day=day, user=self.user)
        self.assertIsNone(row2.charge)
        self.assertEqual(row2.pallet_count, 0)
