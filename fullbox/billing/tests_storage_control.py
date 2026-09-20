"""Тесты контроля периода хранения и корректировок."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from employees.models import Employee
from sku.models import Agency

from .models import (
    BillingService,
    ClientBillingContract,
    ClientTariffItem,
    ClientTariffVersion,
    OwnCompany,
    StorageAdjustment,
    StorageBillingPeriod,
    StorageCalculationRule,
    TariffCategory,
    TariffUnit,
)
from .storage_billing import StorageBillingService
from .storage_control import approve_adjustment, close_storage_period, create_adjustment, get_or_open_period


class StorageControlTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="ctrl_mgr", password="pass", is_staff=True)
        self.manager = Employee.objects.create(full_name="Ctrl Manager", role="manager", user=self.user)
        self.client_agency = Agency.objects.create(agn_name="Ctrl Client", short_name="Ctrl", inn="7700000088")
        self.company = OwnCompany.objects.create(
            name="FB", short_name="FB", tax_mode=OwnCompany.TAX_MODE_VAT, vat_rate="20", is_default=True
        )
        self.contract = ClientBillingContract.objects.create(
            client=self.client_agency, own_company=self.company, pricing_mode=ClientBillingContract.PRICING_INDIVIDUAL
        )
        category, _ = TariffCategory.objects.get_or_create(code="storage", defaults={"name": "Хранение", "sort_order": 10})
        unit, _ = TariffUnit.objects.get_or_create(code="pallet", defaults={"name": "Палета", "short_name": "пал."})
        service, _ = BillingService.objects.get_or_create(
            code="storage_pallet_day",
            defaults={"name": "Хранение палеты/день", "unit": "пал.", "vat_rate": "20"},
        )
        self.version = ClientTariffVersion.objects.create(
            client=self.client_agency,
            contract=self.contract,
            name="Ctrl tariff",
            version_number=1,
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=timezone.localdate() - timedelta(days=30),
            manager=self.manager,
            created_by=self.user,
        )
        ClientTariffItem.objects.create(
            tariff_version=self.version,
            category=category,
            service=service,
            service_name=service.name,
            unit=unit,
            price=Decimal("100"),
        )
        StorageCalculationRule.objects.get_or_create(
            tariff_version=self.version,
            defaults={
                "billing_mode": StorageCalculationRule.MODE_PALLET_DAY,
                "volume_level": StorageCalculationRule.LEVEL_PALLET,
                "missing_dims_policy": StorageCalculationRule.MISSING_SKIP,
            },
        )
        self.version.status = ClientTariffVersion.STATUS_ACTIVE
        self.version.save(update_fields=["status", "updated_at"])

    def test_close_and_forbid_edit(self):
        day = timezone.localdate()
        period = get_or_open_period(self.client_agency, year=day.year, month=day.month)
        close_storage_period(period, user=self.user, checklist={"force": True})
        period.refresh_from_db()
        self.assertEqual(period.status, StorageBillingPeriod.STATUS_CLOSED)

        with patch("billing.storage_billing.count_client_pallets") as mock_counts:
            mock_counts.return_value = {"total": 3, "by_zone": {}, "rows": []}
            row = StorageBillingService.record_storage_day(self.client_agency, day=day, user=self.user)
            self.assertEqual(row.payload.get("skipped"), "period_closed")
            mock_counts.assert_not_called()

    def test_adjustment_apply(self):
        day = timezone.localdate()
        adj = create_adjustment(
            client=self.client_agency,
            year=day.year,
            month=day.month,
            delta_amount=Decimal("-50.00"),
            reason=StorageAdjustment.REASON_CLIENT_AGREEMENT,
            comment="Скидка",
            user=self.user,
        )
        self.assertEqual(adj.status, StorageAdjustment.STATUS_PENDING)
        applied = approve_adjustment(adj, user=self.user)
        self.assertEqual(applied.status, StorageAdjustment.STATUS_APPLIED)
        self.assertIsNotNone(applied.source_charge_id)

    def test_adjustment_blocked_when_closed(self):
        day = timezone.localdate()
        period = get_or_open_period(self.client_agency, year=day.year, month=day.month)
        close_storage_period(period, user=self.user, checklist={"force": True})
        with self.assertRaises(ValidationError):
            create_adjustment(
                client=self.client_agency,
                year=day.year,
                month=day.month,
                delta_amount=Decimal("10"),
                reason=StorageAdjustment.REASON_OTHER,
                user=self.user,
            )
