"""Доп. тесты биллинга хранения: палето-день/месяц, fallback, период, менеджерский API."""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.utils import timezone

from employees.models import Employee
from head_manager.models import OwnCompany
from sku.models import Agency

from .models import (
    ApplicationCharge,
    BillingApplication,
    BillingService,
    BillingStorageDay,
    ClientBillingContract,
    ClientTariffItem,
    ClientTariffVersion,
    StorageBillingError,
    StorageBillingPeriod,
    StorageCalculationRule,
    TariffCategory,
    TariffUnit,
)
from .services import BillingWorkflowService
from .storage_billing import StorageBillingService
from .storage_control import (
    close_storage_period,
    export_storage_days_csv,
    get_or_open_period,
    reopen_storage_period,
)
from .storage_engine import calculate_storage_day, ensure_rule_for_version, service_code_for_mode


User = get_user_model()


class StorageEnginePathTests(TestCase):
    def test_pallet_day_quantity_equals_pallet_total(self):
        rule = StorageCalculationRule(
            billing_mode=StorageCalculationRule.MODE_PALLET_DAY,
            volume_level=StorageCalculationRule.LEVEL_PALLET,
            missing_dims_policy=StorageCalculationRule.MISSING_SKIP,
        )
        calc = calculate_storage_day(
            [{"qty": 1, "pallet_code": "P1", "zone": "OS"}],
            rule,
            day=date(2026, 7, 15),
            pallet_counts={"total": 7, "by_zone": {"OS": 7}},
        )
        self.assertEqual(calc.quantity, Decimal("7"))
        self.assertEqual(calc.unit, "пал.")
        self.assertFalse(calc.skip_charge)

    def test_pallet_month_prorates_by_calendar_days(self):
        rule = StorageCalculationRule(
            billing_mode=StorageCalculationRule.MODE_PALLET_MONTH,
            month_mode=StorageCalculationRule.MONTH_CALENDAR_PRORATE,
            volume_level=StorageCalculationRule.LEVEL_PALLET,
            missing_dims_policy=StorageCalculationRule.MISSING_SKIP,
        )
        calc = calculate_storage_day(
            [],
            rule,
            day=date(2026, 7, 1),
            pallet_counts={"total": 31, "by_zone": {"OS": 31}},
        )
        # 31 пал. / 31 день июля = 1
        self.assertEqual(calc.quantity, Decimal("1.000000"))
        self.assertEqual(service_code_for_mode(rule.billing_mode), "storage_pallet_month")

    def test_pallet_month_full_month_still_prorates_daily_job(self):
        """MONTH_FULL в ежедневном job тоже делит на дни месяца (не полный charge каждый день)."""
        rule = StorageCalculationRule(
            billing_mode=StorageCalculationRule.MODE_PALLET_MONTH,
            month_mode=StorageCalculationRule.MONTH_FULL,
            volume_level=StorageCalculationRule.LEVEL_PALLET,
            missing_dims_policy=StorageCalculationRule.MISSING_SKIP,
        )
        calc = calculate_storage_day(
            [],
            rule,
            day=date(2026, 2, 10),
            pallet_counts={"total": 28, "by_zone": {}},
        )
        self.assertEqual(calc.quantity, Decimal("1.000000"))

    def test_missing_dims_fallback_uses_pallet_volume(self):
        rule = StorageCalculationRule(
            billing_mode=StorageCalculationRule.MODE_LITER_DAY,
            volume_level=StorageCalculationRule.LEVEL_UNIT,
            missing_dims_policy=StorageCalculationRule.MISSING_FALLBACK,
            space_coefficient_default=Decimal("1"),
        )
        calc = calculate_storage_day(
            [{"qty": 5, "sku_code": "NODIM", "zone": "OS"}],
            rule,
            day=date(2026, 7, 1),
            pallet_counts={"total": 2, "by_zone": {"OS": 2}},
        )
        # 2 пал. × 1728 л (резервный объём при отсутствии габаритов)
        self.assertEqual(calc.quantity, Decimal("3456.000000"))
        self.assertTrue(calc.errors)
        self.assertTrue(
            any(e.get("fallback") or "fallback" in str(e.get("message") or "").lower() for e in calc.errors)
        )
        self.assertFalse(calc.skip_charge)

    def test_missing_dims_skip_zero_quantity_no_charge(self):
        rule = StorageCalculationRule(
            billing_mode=StorageCalculationRule.MODE_LITER_DAY,
            volume_level=StorageCalculationRule.LEVEL_UNIT,
            missing_dims_policy=StorageCalculationRule.MISSING_SKIP,
            space_coefficient_default=Decimal("1"),
        )
        calc = calculate_storage_day(
            [{"qty": 3, "sku_code": "NODIM", "zone": "OS"}],
            rule,
            day=date(2026, 7, 1),
            pallet_counts={"total": 0, "by_zone": {}},
        )
        self.assertTrue(calc.skip_charge)
        self.assertEqual(calc.skip_reason, "zero_quantity")
        self.assertEqual(calc.quantity, Decimal("0"))

    def test_m3_week_prorate(self):
        rule = StorageCalculationRule(
            billing_mode=StorageCalculationRule.MODE_M3_WEEK,
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
        calc = calculate_storage_day(rows, rule, day=date(2026, 7, 15))
        # 1 м³ / 7
        self.assertAlmostEqual(float(calc.quantity), 1.0 / 7.0, places=5)
        self.assertEqual(calc.unit, "м³")

    def test_liter_month_prorate(self):
        rule = StorageCalculationRule(
            billing_mode=StorageCalculationRule.MODE_LITER_MONTH,
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
        calc = calculate_storage_day(rows, rule, day=date(2026, 7, 15))
        # 1000 л / 31
        self.assertAlmostEqual(float(calc.quantity), 1000.0 / 31.0, places=4)


class StorageBillingPathIntegrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="stor_path_mgr", password="pass", is_staff=True)
        self.manager = Employee.objects.create(
            full_name="Stor Path Manager", role="manager", user=self.user, is_active=True
        )
        self.client_agency = Agency.objects.create(
            agn_name="Stor Path Client",
            short_name="SPC",
            inn="7700000077",
            mened_user_id=self.user.id,
        )
        self.company = OwnCompany.objects.create(
            name="FB Path",
            short_name="FBP",
            tax_mode=OwnCompany.TAX_MODE_VAT,
            vat_rate="20",
            is_default=True,
        )
        self.contract = ClientBillingContract.objects.create(
            client=self.client_agency,
            own_company=self.company,
            pricing_mode=ClientBillingContract.PRICING_INDIVIDUAL,
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
            name="Stor path tariff",
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

    def test_ensure_rule_for_version_creates_default_pallet_day(self):
        draft = ClientTariffVersion.objects.create(
            client=self.client_agency,
            contract=self.contract,
            name="No rule yet",
            version_number=2,
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=timezone.localdate(),
            manager=self.manager,
            created_by=self.user,
        )
        rule = ensure_rule_for_version(draft)
        self.assertEqual(rule.billing_mode, StorageCalculationRule.MODE_PALLET_DAY)
        self.assertEqual(rule.tariff_version_id, draft.id)

    @patch("billing.storage_billing.count_client_pallets")
    def test_record_storage_day_pallet_day_amount_and_vat(self, mock_counts):
        mock_counts.return_value = {
            "total": 3,
            "by_zone": {"OS": 3},
            "pallet_codes": ["P1", "P2", "P3"],
            "rows": [
                {"qty": 1, "pallet_code": "P1", "zone": "OS", "sku_code": "A"},
                {"qty": 1, "pallet_code": "P2", "zone": "OS", "sku_code": "B"},
                {"qty": 1, "pallet_code": "P3", "zone": "OS", "sku_code": "C"},
            ],
        }
        day = timezone.localdate()
        row = StorageBillingService.record_storage_day(self.client_agency, day=day, user=self.user)
        self.assertIsNotNone(row.charge)
        self.assertEqual(row.charge.service.code, "storage_pallet_day")
        self.assertEqual(row.charge.quantity, Decimal("3"))
        self.assertEqual(row.charge.tariff, Decimal("100.0000"))
        self.assertEqual(row.charge.amount, Decimal("300.00"))
        self.assertEqual(row.charge.vat_amount, Decimal("60.00"))
        self.assertEqual(row.charge.total_amount, Decimal("360.00"))
        self.assertFalse(row.charge.is_confirmed)

    @patch("billing.storage_billing.count_client_pallets")
    def test_record_day_no_tariff_sets_needs_review_and_error(self, mock_counts):
        self.version.status = ClientTariffVersion.STATUS_ARCHIVED
        self.version.save(update_fields=["status", "updated_at"])
        mock_counts.return_value = {
            "total": 1,
            "by_zone": {"OS": 1},
            "rows": [{"qty": 1, "pallet_code": "P1", "zone": "OS"}],
        }
        day = timezone.localdate()
        row = StorageBillingService.record_storage_day(self.client_agency, day=day, user=self.user)
        self.assertEqual(row.status, BillingStorageDay.STATUS_NEEDS_REVIEW)
        self.assertTrue(
            StorageBillingError.objects.filter(
                client=self.client_agency,
                day=day,
                error_type=StorageBillingError.TYPE_NO_TARIFF,
            ).exists()
        )
        self.assertIsNone(row.charge)

    @patch("billing.storage_billing.count_client_pallets")
    def test_reopen_storage_period_allows_recalc(self, mock_counts):
        day = timezone.localdate()
        period = get_or_open_period(self.client_agency, year=day.year, month=day.month)
        close_storage_period(period, user=self.user, checklist={"force": True})
        period.refresh_from_db()
        self.assertEqual(period.status, StorageBillingPeriod.STATUS_CLOSED)

        mock_counts.return_value = {"total": 2, "by_zone": {}, "rows": []}
        blocked = StorageBillingService.record_storage_day(self.client_agency, day=day, user=self.user)
        self.assertEqual(blocked.payload.get("skipped"), "period_closed")

        reopen_storage_period(period, user=self.user)
        period.refresh_from_db()
        self.assertEqual(period.status, StorageBillingPeriod.STATUS_OPEN)

        mock_counts.return_value = {
            "total": 2,
            "by_zone": {"OS": 2},
            "rows": [
                {"qty": 1, "pallet_code": "P1", "zone": "OS"},
                {"qty": 1, "pallet_code": "P2", "zone": "OS"},
            ],
        }
        row = StorageBillingService.record_storage_day(self.client_agency, day=day, user=self.user)
        self.assertNotEqual(row.payload.get("skipped"), "period_closed")
        self.assertEqual(row.pallet_count, 2)
        self.assertIsNotNone(row.charge)

    @patch("billing.storage_billing.count_client_pallets")
    def test_export_storage_days_csv_headers_and_rows(self, mock_counts):
        mock_counts.return_value = {
            "total": 1,
            "by_zone": {"OS": 1},
            "rows": [{"qty": 1, "pallet_code": "P1", "zone": "OS"}],
        }
        day = timezone.localdate()
        StorageBillingService.record_storage_day(self.client_agency, day=day, user=self.user)
        csv_text = export_storage_days_csv(self.client_agency, year=day.year, month=day.month)
        lines = [ln for ln in csv_text.strip().splitlines() if ln.strip()]
        self.assertGreaterEqual(len(lines), 2)
        self.assertIn("date", lines[0])
        self.assertIn("pallet_count", lines[0])
        self.assertIn(day.isoformat(), csv_text)

    @patch("billing.storage_billing.count_client_pallets")
    def test_manager_confirm_storage_application_charges(self, mock_counts):
        mock_counts.return_value = {
            "total": 2,
            "by_zone": {"OS": 2},
            "rows": [
                {"qty": 1, "pallet_code": "P1", "zone": "OS"},
                {"qty": 1, "pallet_code": "P2", "zone": "OS"},
            ],
        }
        day = timezone.localdate()
        row = StorageBillingService.record_storage_day(self.client_agency, day=day, user=self.user)
        app = row.application
        self.assertEqual(app.application_type, BillingApplication.TYPE_STORAGE)
        self.assertFalse(row.charge.is_confirmed)
        count = BillingWorkflowService.confirm_application_charges(app, user=self.user)
        self.assertGreaterEqual(count, 1)
        row.charge.refresh_from_db()
        self.assertTrue(row.charge.is_confirmed)


class StorageManagerApiPathTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="stor_api_mgr", password="pass", is_staff=True)
        self.manager = Employee.objects.create(
            full_name="Stor API Manager", role="manager", user=self.user, is_active=True
        )
        self.client_agency = Agency.objects.create(
            agn_name="Stor API Client",
            short_name="SAC",
            inn="7700000066",
            mened_user_id=self.user.id,
        )
        self.company = OwnCompany.objects.create(
            name="FB API",
            short_name="FBA",
            tax_mode=OwnCompany.TAX_MODE_VAT,
            vat_rate="20",
            is_default=True,
        )
        self.contract = ClientBillingContract.objects.create(
            client=self.client_agency,
            own_company=self.company,
            pricing_mode=ClientBillingContract.PRICING_INDIVIDUAL,
        )
        category, _ = TariffCategory.objects.get_or_create(
            code="storage", defaults={"name": "Хранение", "sort_order": 10}
        )
        unit, _ = TariffUnit.objects.get_or_create(
            code="pallet", defaults={"name": "Палета", "short_name": "пал."}
        )
        service, _ = BillingService.objects.get_or_create(
            code="storage_pallet_day",
            defaults={"name": "Хранение палеты/день", "unit": "пал.", "vat_rate": "20"},
        )
        version = ClientTariffVersion.objects.create(
            client=self.client_agency,
            contract=self.contract,
            name="API tariff",
            version_number=1,
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=timezone.localdate() - timedelta(days=30),
            manager=self.manager,
            created_by=self.user,
        )
        ClientTariffItem.objects.create(
            tariff_version=version,
            category=category,
            service=service,
            service_name=service.name,
            unit=unit,
            price=Decimal("100"),
        )
        StorageCalculationRule.objects.create(
            tariff_version=version,
            billing_mode=StorageCalculationRule.MODE_PALLET_DAY,
            volume_level=StorageCalculationRule.LEVEL_PALLET,
            missing_dims_policy=StorageCalculationRule.MISSING_SKIP,
        )
        version.status = ClientTariffVersion.STATUS_ACTIVE
        version.save(update_fields=["status", "updated_at"])
        self.http = Client()
        self.http.force_login(self.user)
        session = self.http.session
        session["employee_id"] = self.manager.id
        session["employee_role"] = "manager"
        session.save()

    @patch("billing.storage_billing.count_client_pallets")
    def test_manager_record_storage_day_api_ok(self, mock_counts):
        mock_counts.return_value = {
            "total": 4,
            "by_zone": {"OS": 4},
            "rows": [{"qty": 1, "pallet_code": f"P{i}", "zone": "OS"} for i in range(4)],
        }
        day = timezone.localdate()
        app = StorageBillingService.ensure_month_application(
            self.client_agency, year=day.year, month=day.month, user=self.user
        )
        resp = self.http.post(
            f"/team-manager/billing/api/applications/{app.id}/storage-day/",
            data=json.dumps({"date": day.isoformat()}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["pallet_count"], 4)
        self.assertEqual(Decimal(body["data"]["charge_total"]), Decimal("480.00"))

    def test_manager_record_storage_day_rejects_non_storage_application(self):
        app = BillingApplication.objects.create(
            application_type=BillingApplication.TYPE_RECEIVING,
            application_id="RCV-NOT-STORAGE",
            client=self.client_agency,
            legal_entity=self.client_agency,
            manager=self.manager,
            operational_status="done",
            created_at_source=timezone.now(),
        )
        resp = self.http.post(
            f"/team-manager/billing/api/applications/{app.id}/storage-day/",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json().get("ok", True))

    @patch("billing.storage_billing.count_client_pallets")
    def test_manager_storage_day_detail_includes_lines(self, mock_counts):
        mock_counts.return_value = {
            "total": 1,
            "by_zone": {"OS": 1},
            "rows": [{"qty": 1, "pallet_code": "P-DETAIL", "zone": "OS", "sku_code": "SKU1"}],
        }
        day = timezone.localdate()
        row = StorageBillingService.record_storage_day(self.client_agency, day=day, user=self.user)
        resp = self.http.get(f"/team-manager/billing/api/storage/days/{row.id}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["id"], row.id)
        self.assertIn("lines", data)
        self.assertTrue(data["lines"] or data.get("pallet_count") == 1)
