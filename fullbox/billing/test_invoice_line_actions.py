from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from employees.models import Employee
from sku.models import Agency

from .invoice_print import build_invoice_print_context
from .models import ApplicationCharge, ApplicationChargeHistory, BillingActLine, BillingAuditEvent, BillingApplication, BillingService, BillingStorageDay
from .services import BillingWorkflowService
from .statuses import InvoiceStatus


class DraftInvoiceLineActionTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="invoice_line_editor", password="pass")
        self.manager = Employee.objects.create(full_name="Invoice Line Editor", role="manager", user=self.user)
        self.client = Agency.objects.create(
            agn_name="Storage Client LLC",
            short_name="Storage Client",
            inn="7700123456",
            mened_user_id=self.user.id,
        )
        self.service, _created = BillingService.objects.get_or_create(
            code="storage_m3_day",
            defaults={"name": "Хранение, м³/сутки", "unit": "м³", "vat_rate": "0"},
        )
        self.application = BillingWorkflowService.sync_application_from_source(
            application_type=BillingApplication.TYPE_STORAGE,
            application_id="STR-TEST-2026-07",
            client=self.client,
            manager=self.manager,
            operational_status="done",
            operational_status_label="Выполнена",
            created_at_source=timezone.now(),
            user=self.user,
        )
        performed_at = timezone.make_aware(datetime(2026, 7, 31, 23, 59))
        self.charge = BillingWorkflowService.create_or_update_charge(
            self.application,
            service=self.service,
            quantity=Decimal("10.000"),
            tariff=Decimal("30.0000"),
            unit="м³",
            vat_rate="0",
            source_type=ApplicationCharge.SOURCE_STORAGE_DAY,
            source_id="2026-07-31",
            source_key="storage-day:test:2026-07-31",
            performed_at=performed_at,
            billing_period=date(2026, 7, 1),
            user=self.user,
            tariff_source_label="Test storage tariff",
            is_manual_override=True,
            override_reason="test",
            resolve_from_agreed_tariff=False,
        )
        BillingStorageDay.objects.create(
            client=self.client,
            application=self.application,
            day=date(2026, 7, 31),
            pallet_count=19,
            billing_mode="m3_day",
            charge=self.charge,
        )
        self.charge = BillingWorkflowService.confirm_charge(self.charge, user=self.user)
        with patch.object(BillingWorkflowService, "_assert_client_ready_for_invoice"):
            self.act, self.invoice = BillingWorkflowService.generate_act_and_invoice(
                self.application,
                user=self.user,
            )

    def test_print_context_contains_storage_date_and_edit_metadata(self):
        context = build_invoice_print_context(self.invoice)
        row = context["sections"][0]["lines"][0]

        self.assertEqual(row["date_fmt"], "31.07.2026")
        self.assertEqual(row["quantity_value"], "10.000")
        self.assertEqual(row["edit_version"], self.charge.edit_version)
        self.assertTrue(row["can_edit_quantity"])

    def test_print_context_adds_storage_period_comment(self):
        context = build_invoice_print_context(self.invoice)

        self.assertIn("Хранение товара", context["comment"])
        self.assertIn("с 31.07.2026 по 31.07.2026", context["comment"])
        self.assertIn("19 палето-дней", context["comment"])

    def test_quantity_update_recalculates_charge_act_invoice_and_audit(self):
        charge, invoice = BillingWorkflowService.update_draft_invoice_charge_quantity(
            self.invoice,
            self.charge,
            quantity="8,500",
            reason="Контрольный замер объёма",
            user=self.user,
            expected_version=self.charge.edit_version,
        )
        line = BillingActLine.objects.get(charge=charge)
        self.act.refresh_from_db()
        self.application.refresh_from_db()

        self.assertEqual(charge.quantity, Decimal("8.500"))
        self.assertEqual(charge.original_quantity, Decimal("10.000"))
        self.assertEqual(charge.total_amount, Decimal("255.00"))
        self.assertEqual(line.quantity, Decimal("8.500"))
        self.assertEqual(self.act.total_amount, Decimal("255.00"))
        self.assertEqual(invoice.total_amount, Decimal("255.00"))
        self.assertEqual(invoice.debt_amount, Decimal("255.00"))
        self.assertEqual(self.application.charges_total, Decimal("255.00"))
        self.assertTrue(
            ApplicationChargeHistory.objects.filter(
                charge=charge,
                change_type=ApplicationChargeHistory.CHANGE_QTY,
                reason="invoice_quantity_edit",
            ).exists()
        )
        self.assertTrue(BillingAuditEvent.objects.filter(action="invoice_charge_quantity_overridden").exists())

    def test_exclude_removes_only_draft_line_and_recalculates_documents(self):
        charge, invoice = BillingWorkflowService.exclude_draft_invoice_charge(
            self.invoice,
            self.charge,
            reason=ApplicationCharge.EXCLUDE_DUPLICATE,
            comment="Дублирующее начисление за день хранения",
            user=self.user,
            expected_version=self.charge.edit_version,
        )
        self.act.refresh_from_db()
        self.application.refresh_from_db()

        self.assertTrue(charge.is_excluded)
        self.assertFalse(charge.is_included_in_act)
        self.assertFalse(charge.is_included_in_invoice)
        self.assertFalse(BillingActLine.objects.filter(charge=charge).exists())
        self.assertEqual(self.act.total_amount, Decimal("0.00"))
        self.assertEqual(invoice.total_amount, Decimal("0.00"))
        self.assertEqual(invoice.debt_amount, Decimal("0.00"))
        self.assertEqual(self.application.charges_total, Decimal("0.00"))
        self.assertTrue(
            ApplicationChargeHistory.objects.filter(
                charge=charge,
                change_type=ApplicationChargeHistory.CHANGE_EXCLUDE,
                reason=ApplicationCharge.EXCLUDE_DUPLICATE,
            ).exists()
        )
        self.assertTrue(BillingAuditEvent.objects.filter(action="invoice_charge_excluded").exists())

    def test_sent_invoice_rejects_quantity_change(self):
        self.invoice.status = InvoiceStatus.SENT
        self.invoice.sent_at = timezone.now()
        self.invoice.save(update_fields=["status", "sent_at", "updated_at"])

        with self.assertRaisesMessage(ValidationError, "неотправленном черновике"):
            BillingWorkflowService.update_draft_invoice_charge_quantity(
                self.invoice,
                self.charge,
                quantity="8.000",
                reason="Нельзя применить",
                user=self.user,
                expected_version=self.charge.edit_version,
            )
        self.charge.refresh_from_db()
        self.assertEqual(self.charge.quantity, Decimal("10.000"))

    def test_existing_price_update_still_recalculates_draft_documents(self):
        charge, invoice, tariff_version = BillingWorkflowService.update_draft_invoice_charge_price(
            self.invoice,
            self.charge,
            tariff="25",
            reason="Согласованная цена текущего счёта",
            user=self.user,
        )
        line = BillingActLine.objects.get(charge=charge)

        self.assertIsNone(tariff_version)
        self.assertEqual(charge.tariff, Decimal("25.0000"))
        self.assertEqual(line.tariff, Decimal("25.0000"))
        self.assertEqual(invoice.total_amount, Decimal("250.00"))
