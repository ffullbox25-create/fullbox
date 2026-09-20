"""ТЗ: страница расчёта по заявке — exclude, история, bulk, concurrency."""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from employees.models import Employee
from head_manager.models import OwnCompany
from sku.models import Agency

from .charge_status import charge_manager_status, charge_missing_price
from .models import (
    ApplicationCharge,
    ApplicationChargeHistory,
    BillingApplication,
    BillingService,
    ClientBillingContract,
    ClientTariffItem,
    ClientTariffVersion,
    TariffCategory,
    TariffUnit,
)
from .services import BillingWorkflowService, ChargeVersionConflict
from .statuses import BillingStatus


class ChargeDetailTzTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.manager_user = User.objects.create_user(username="tz_mgr", password="pass")
        self.accountant_user = User.objects.create_user(username="tz_acc", password="pass")
        self.manager = Employee.objects.create(full_name="Mgr", role="manager", user=self.manager_user)
        self.accountant = Employee.objects.create(full_name="Acc", role="accountant", user=self.accountant_user)
        self.agency = Agency.objects.create(
            agn_name="TZ Client",
            short_name="TZ",
            inn="7700111222",
            mened_user_id=self.manager_user.id,
        )
        self.svc_a, _ = BillingService.objects.get_or_create(
            code="tz_loading", defaults={"name": "Погрузочно-разгрузочные", "unit": "шт", "vat_rate": "20"}
        )
        self.svc_b, _ = BillingService.objects.get_or_create(
            code="tz_pickup", defaults={"name": "Забор товара", "unit": "шт", "vat_rate": "20"}
        )
        self.app = BillingWorkflowService.sync_application_from_source(
            application_type=BillingApplication.TYPE_SHIPPING,
            application_id="SO-TZ-001",
            client=self.agency,
            manager=self.manager,
            operational_status="shipped",
            operational_status_label="Отгружена",
            created_at_source=timezone.now(),
            user=self.manager_user,
        )

    def _priced_charge(self, service=None, qty="10", source_key="tz-1"):
        return BillingWorkflowService.create_or_update_charge(
            self.app,
            service=service or self.svc_a,
            quantity=Decimal(qty),
            tariff=Decimal("60.00"),
            vat_rate="20",
            source_key=source_key,
            user=self.manager_user,
            is_manual_override=True,
            override_reason="test",
            resolve_from_agreed_tariff=False,
        )

    def test_qty_change_writes_history_and_status(self):
        charge = self._priced_charge()
        updated = BillingWorkflowService.update_charge_quantity(
            charge, quantity=Decimal("8"), user=self.manager_user, basis="fact", comment=""
        )
        self.assertEqual(updated.quantity, Decimal("8.000"))
        self.assertEqual(updated.original_quantity, Decimal("10.000"))
        self.assertFalse(updated.is_confirmed)
        self.assertTrue(
            ApplicationChargeHistory.objects.filter(
                charge=updated, change_type=ApplicationChargeHistory.CHANGE_QTY
            ).exists()
        )
        code, label, _ = charge_manager_status(updated)
        self.assertEqual(code, "qty_changed")
        self.assertEqual(label, "Количество изменено")

    def test_exclude_restore_and_recalc_skips_excluded(self):
        charge = self._priced_charge(source_key="wh:1")
        BillingWorkflowService.exclude_charge(
            charge, reason=ApplicationCharge.EXCLUDE_WRONG_AUTO, comment="авто", user=self.manager_user
        )
        charge.refresh_from_db()
        self.assertTrue(charge.is_excluded)
        self.assertEqual(charge_manager_status(charge)[0], "excluded")
        # recalculate via create_or_update should not revive
        again = BillingWorkflowService.create_or_update_charge(
            self.app,
            service=self.svc_a,
            quantity=Decimal("99"),
            tariff=Decimal("60"),
            vat_rate="20",
            source_key="wh:1",
            user=self.manager_user,
            is_manual_override=True,
            override_reason="x",
            resolve_from_agreed_tariff=False,
        )
        self.assertEqual(again.pk, charge.pk)
        self.assertTrue(again.is_excluded)
        self.assertEqual(again.quantity, Decimal("10.000"))
        restored = BillingWorkflowService.restore_charge(charge, user=self.manager_user)
        self.assertFalse(restored.is_excluded)
        self.assertTrue(
            ApplicationChargeHistory.objects.filter(change_type=ApplicationChargeHistory.CHANGE_RESTORE).exists()
        )

    def test_cannot_confirm_without_tariff(self):
        charge = ApplicationCharge.objects.create(
            application=self.app,
            client=self.agency,
            legal_entity=self.agency,
            service=self.svc_a,
            quantity=Decimal("1"),
            unit="шт",
            tariff=Decimal("0"),
            amount=Decimal("0"),
            vat_rate="0",
            vat_amount=Decimal("0"),
            total_amount=Decimal("0"),
            billing_period=timezone.localdate().replace(day=1),
            source_key="draft-1",
            source_type=ApplicationCharge.SOURCE_MANUAL,
        )
        self.assertTrue(charge_missing_price(charge))
        with self.assertRaises(ValidationError):
            BillingWorkflowService.confirm_charge(charge, user=self.manager_user)

    def test_add_manual_charge_and_arithmetic(self):
        charge = BillingWorkflowService.create_or_update_charge(
            self.app,
            service=self.svc_a,
            quantity=Decimal("1000"),
            tariff=Decimal("60"),
            vat_rate="20",
            source_key="manual-arith",
            user=self.manager_user,
            is_manual_override=True,
            override_reason="test",
            resolve_from_agreed_tariff=False,
        )
        self.assertEqual(charge.amount, Decimal("60000.00"))
        self.assertEqual(charge.vat_amount, Decimal("12000.00"))
        self.assertEqual(charge.total_amount, Decimal("72000.00"))

    def test_optimistic_lock_conflict(self):
        charge = self._priced_charge(source_key="lock-1")
        with self.assertRaises(ChargeVersionConflict):
            BillingWorkflowService.update_charge_quantity(
                charge, quantity=Decimal("9"), user=self.manager_user, basis="fact", expected_version=999
            )

    def test_bulk_confirm_and_api_403_for_price(self):
        c1 = self._priced_charge(source_key="b1")
        c2 = self._priced_charge(source_key="b2", qty="5")
        result = BillingWorkflowService.bulk_charge_action(
            self.app, action="confirm", charge_ids=[c1.id, c2.id], user=self.manager_user
        )
        self.assertEqual(result["done"], 2)
        c1.refresh_from_db()
        self.assertTrue(c1.is_confirmed)

        self.client.force_login(self.manager_user)
        resp = self.client.patch(
            f"/team-manager/billing/api/charges/{c1.id}/",
            data=json.dumps({"tariff": "1.00", "edit_version": c1.edit_version}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_exclude_api_and_detail_page(self):
        charge = self._priced_charge(source_key="api-ex")
        self.client.force_login(self.manager_user)
        resp = self.client.post(
            f"/team-manager/billing/api/charges/{charge.id}/exclude/",
            data=json.dumps({"reason": "duplicate", "comment": "", "edit_version": 1}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        charge.refresh_from_db()
        self.assertTrue(charge.is_excluded)
        page = self.client.get(f"/team-manager/billing/applications/{self.app.id}/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Начисления по")
        self.assertContains(page, "Подтвердить расчёт")
        self.assertContains(page, "Финансовая сводка")
        self.assertContains(page, "Исключено из расчёта")

    def test_shipping_status_unchanged_after_billing_edits(self):
        from shipping.models import ShippingOrder

        order = ShippingOrder.objects.create(number="SO-TZ-LOCK", agency=self.agency, status="shipped")
        app = BillingWorkflowService.sync_application_from_source(
            application_type=BillingApplication.TYPE_SHIPPING,
            application_id=order.number,
            client=self.agency,
            manager=self.manager,
            operational_status="shipped",
            operational_status_label="Отгружена",
            user=self.manager_user,
        )
        ch = BillingWorkflowService.create_or_update_charge(
            app,
            service=self.svc_a,
            quantity=Decimal("2"),
            tariff=Decimal("10"),
            vat_rate="20",
            source_key="ship-lock",
            user=self.manager_user,
            is_manual_override=True,
            override_reason="t",
            resolve_from_agreed_tariff=False,
        )
        BillingWorkflowService.exclude_charge(ch, reason="not_performed", user=self.manager_user)
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")
        self.assertEqual(app.billing_status, BillingStatus.NOT_CALCULATED)  # may stay or change

    def _publish_tariff(self, prices):
        company = OwnCompany.objects.create(
            name="TZ Co",
            short_name="TZC",
            tax_mode=OwnCompany.TAX_MODE_VAT,
            vat_rate="20",
            is_default=True,
        )
        contract = ClientBillingContract.objects.create(
            client=self.agency,
            own_company=company,
            pricing_mode=ClientBillingContract.PRICING_INDIVIDUAL,
        )
        category, _ = TariffCategory.objects.get_or_create(code="shipping", defaults={"name": "Отгрузка", "sort_order": 10})
        unit, _ = TariffUnit.objects.get_or_create(code="piece", defaults={"name": "Штука", "short_name": "шт."})
        version = ClientTariffVersion.objects.create(
            client=self.agency,
            contract=contract,
            name="TZ tariff",
            version_number=1,
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=timezone.localdate() - timedelta(days=1),
            manager=self.manager,
            created_by=self.manager_user,
        )
        for svc, price in prices:
            ClientTariffItem.objects.create(
                tariff_version=version,
                category=category,
                service=svc,
                service_name=svc.name,
                unit=unit,
                price=price,
            )
        version.status = ClientTariffVersion.STATUS_ACTIVE
        version.save(update_fields=["status", "updated_at"])
        return version

    def test_change_service_updates_price_from_client_tariff(self):
        self._publish_tariff(
            ((self.svc_a, Decimal("100.0000")), (self.svc_b, Decimal("45.5000")))
        )
        charge = BillingWorkflowService.create_or_update_charge(
            self.app,
            service=self.svc_a,
            quantity=Decimal("2.000"),
            source_key="svc-price-1",
            user=self.manager_user,
            resolve_from_agreed_tariff=True,
        )
        self.assertEqual(charge.tariff, Decimal("100.0000"))

        updated = BillingWorkflowService.change_charge_service(
            charge, service=self.svc_b, user=self.manager_user
        )
        self.assertEqual(updated.service_id, self.svc_b.id)
        self.assertEqual(updated.tariff, Decimal("45.5000"))
        self.assertEqual(updated.total_amount, Decimal("109.20"))  # 2 * 45.5 * 1.2
        self.assertFalse(updated.is_manual_override)

        self.client.force_login(self.manager_user)
        page = self.client.get(f"/team-manager/billing/applications/{self.app.id}/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "45")
        self.assertContains(page, "data-price")
        self.assertContains(page, self.svc_b.name)

        resp = self.client.patch(
            f"/team-manager/billing/api/charges/{updated.id}/",
            data=json.dumps({"service_id": self.svc_a.id, "edit_version": updated.edit_version}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        payload = resp.json()["data"]
        self.assertEqual(payload["service"]["id"], self.svc_a.id)
        self.assertEqual(payload["tariff"], "100.0000")
