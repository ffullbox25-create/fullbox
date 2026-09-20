from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from employees.models import Employee
from head_manager.models import OwnCompany
from sku.models import Agency

from .models import (
    ApplicationCharge,
    BillingAct,
    BillingApplication,
    BillingAuditEvent,
    BillingService,
    BillingStaffNotification,
    ClientBillingContract,
    ClientInvoice,
    ClientTariff,
    ClientTariffItem,
    ClientTariffVersion,
    InvoicePayment,
    StandardServicePrice,
    TariffCategory,
    TariffUnit,
)  # TariffCategory/TariffUnit used in portfolio/tariff tests
from .permissions import (
    can_add_client_tariff,
    can_edit_invoice_prices,
    can_manage_charges,
    can_override_billing_tariff,
    can_view_billing,
    filter_applications_for_user,
)
from .price_resolver import resolve_client_service_price
from .services import BillingWorkflowService, calculate_amounts
from .statuses import ActStatus, BillingStatus, DocumentReviewStatus, InvoiceStatus
from .tariff_services import TariffService, copy_tariff_version, get_company_tariff
from .application_detail_ui import storage_application_period_label, storage_charge_period_label


class BillingWorkflowTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.manager_user = User.objects.create_user(username="billing_manager", password="pass")
        self.other_manager_user = User.objects.create_user(username="other_manager", password="pass")
        self.accountant_user = User.objects.create_user(username="billing_accountant", password="pass")
        self.client_user = User.objects.create_user(username="billing_client", password="pass")
        self.manager = Employee.objects.create(full_name="Manager", role="manager", user=self.manager_user)
        self.other_manager = Employee.objects.create(full_name="Other Manager", role="manager", user=self.other_manager_user)
        self.accountant = Employee.objects.create(full_name="Accountant", role="accountant", user=self.accountant_user)
        self.agency = Agency.objects.create(
            agn_name="Client LLC",
            short_name="Client",
            inn="7700000000",
            portal_user=self.client_user,
            mened_user_id=self.manager_user.id,
        )
        self.other_client = Agency.objects.create(
            agn_name="Other Client LLC",
            short_name="Other Client",
            inn="7800000000",
            mened_user_id=self.other_manager_user.id,
        )
        self.service, _ = BillingService.objects.get_or_create(
            code="receiving_box",
            defaults={"name": "Приемка коробов", "unit": "шт", "vat_rate": "20"},
        )
        self.application = BillingWorkflowService.sync_application_from_source(
            application_type=BillingApplication.TYPE_RECEIVING,
            application_id="RCV-100",
            client=self.agency,
            manager=self.manager,
            operational_status="done",
            operational_status_label="Выполнена",
            created_at_source=timezone.now(),
            user=self.manager_user,
        )

    def test_calculate_amounts_supports_vat_modes(self):
        amount, vat_amount, total_amount = calculate_amounts(
            Decimal("1"),
            Decimal("1000.00"),
            "5",
            vat_type=ClientTariffVersion.VAT_EXTRA,
        )
        self.assertEqual(amount, Decimal("1000.00"))
        self.assertEqual(vat_amount, Decimal("50.00"))
        self.assertEqual(total_amount, Decimal("1050.00"))

        amount, vat_amount, total_amount = calculate_amounts(
            Decimal("1"),
            Decimal("1000.00"),
            "5",
            vat_type=ClientTariffVersion.VAT_WITH,
        )
        self.assertEqual(amount, Decimal("952.38"))
        self.assertEqual(vat_amount, Decimal("47.62"))
        self.assertEqual(total_amount, Decimal("1000.00"))

    def test_storage_period_labels_are_human_readable(self):
        app = BillingApplication(
            application_type=BillingApplication.TYPE_STORAGE,
            application_id="STR-2026-07",
            source_payload={"period": "STR-2026-07"},
        )
        charge = ApplicationCharge(
            application=app,
            service=self.service,
            quantity=Decimal("1"),
            unit="м³",
            tariff=Decimal("30"),
            amount=Decimal("30"),
            total_amount=Decimal("30"),
            billing_period=date(2026, 7, 1),
        )

        class Day:
            def __init__(self, value):
                self.day = value

        self.assertEqual(
            storage_application_period_label(app, [Day(date(2026, 7, 1)), Day(date(2026, 7, 9))]),
            "Период хранения: июль 2026; дни расчёта 01.07.2026–09.07.2026",
        )
        self.assertEqual(
            storage_charge_period_label(charge, [Day(date(2026, 7, 1))]),
            "День хранения: 01.07.2026",
        )

        amount, vat_amount, total_amount = calculate_amounts(
            Decimal("1"),
            Decimal("1000.00"),
            "5",
            vat_type=ClientTariffVersion.VAT_NO,
        )
        self.assertEqual(amount, Decimal("1000.00"))
        self.assertEqual(vat_amount, Decimal("0.00"))
        self.assertEqual(total_amount, Decimal("1000.00"))

    def _charge(self, *, confirm=True, source_key="test-charge"):
        charge = BillingWorkflowService.create_or_update_charge(
            self.application,
            service=self.service,
            quantity=Decimal("2.500"),
            tariff=Decimal("100.00"),
            vat_rate="20",
            source_key=source_key,
            user=self.manager_user,
            tariff_source_label="Тестовый тариф",
            is_manual_override=True,
            override_reason="test",
            resolve_from_agreed_tariff=False,
        )
        if confirm:
            charge = BillingWorkflowService.confirm_charge(charge, user=self.manager_user)
        return charge

    def _sent_act(self):
        self._charge(confirm=True)
        act = BillingWorkflowService.generate_act(self.application, user=self.manager_user)
        return BillingWorkflowService.send_act(act, user=self.manager_user)

    def test_sync_application_and_decimal_charge(self):
        charge = self._charge()
        self.application.refresh_from_db()
        self.assertEqual(charge.amount, Decimal("250.00"))
        self.assertEqual(charge.vat_amount, Decimal("50.00"))
        self.assertEqual(charge.total_amount, Decimal("300.00"))
        self.assertEqual(self.application.charges_total, Decimal("300.00"))
        self.assertTrue(BillingAuditEvent.objects.filter(action="charge_created", application=self.application).exists())

    def test_act_cannot_be_created_without_charges(self):
        with self.assertRaises(ValidationError):
            BillingWorkflowService.generate_act(self.application, user=self.manager_user)

    def test_client_confirms_sent_act_and_invoice_becomes_required(self):
        act = self._sent_act()
        confirmed = BillingWorkflowService.confirm_act(act, user=self.client_user, comment="OK")
        self.application.refresh_from_db()
        self.assertEqual(confirmed.status, ActStatus.CONFIRMED)
        self.assertEqual(self.application.billing_status, BillingStatus.INVOICE_REQUIRED)
        self.assertEqual(confirmed.confirmed_amount, Decimal("300.00"))
        self.assertTrue(BillingAuditEvent.objects.filter(action="act_confirmed", application=self.application).exists())

    def test_draft_act_cannot_be_confirmed(self):
        self._charge()
        act = BillingWorkflowService.generate_act(self.application, user=self.manager_user)
        with self.assertRaises(ValidationError):
            BillingWorkflowService.confirm_act(act, user=self.client_user)

    def test_invoice_requires_confirmed_act_and_is_idempotent(self):
        from unittest.mock import patch

        with self.assertRaises(ValidationError):
            BillingWorkflowService.create_invoice(self.application, user=self.accountant_user)
        act = BillingWorkflowService.confirm_act(self._sent_act(), user=self.client_user)
        with patch("accountant.selectors.is_client_billing_ready", return_value=True):
            first = BillingWorkflowService.create_invoice(self.application, user=self.accountant_user)
            second = BillingWorkflowService.create_invoice(self.application, user=self.accountant_user)
        self.assertEqual(first.id, second.id)
        self.assertEqual(ClientInvoice.objects.count(), 1)
        self.assertEqual(first.debt_amount, Decimal("300.00"))
        self.assertEqual(first.acts_count, 1)
        self.assertEqual(list(first.get_linked_acts().values_list("id", flat=True)), [act.id])

    def test_grouped_invoice_from_multiple_acts(self):
        from unittest.mock import patch

        act1 = BillingWorkflowService.confirm_act(self._sent_act(), user=self.client_user)
        app2 = BillingWorkflowService.sync_application_from_source(
            application_type=BillingApplication.TYPE_SHIPPING,
            application_id="SHP-101",
            client=self.agency,
            manager=self.manager,
            operational_status="done",
            operational_status_label="Выполнена",
            created_at_source=timezone.now(),
            user=self.manager_user,
        )
        charge2 = BillingWorkflowService.create_or_update_charge(
            app2,
            service=self.service,
            quantity=Decimal("1"),
            tariff=Decimal("200.00"),
            vat_rate="20",
            source_key="test-charge-2",
            user=self.manager_user,
            tariff_source_label="Тестовый тариф",
            is_manual_override=True,
            override_reason="test",
            resolve_from_agreed_tariff=False,
        )
        BillingWorkflowService.confirm_charge(charge2, user=self.manager_user)
        act2 = BillingWorkflowService.generate_act(app2, user=self.manager_user)
        act2 = BillingWorkflowService.send_act(act2, user=self.manager_user)
        act2 = BillingWorkflowService.confirm_act(act2, user=self.client_user)

        with patch("accountant.selectors.is_client_billing_ready", return_value=True):
            invoice = BillingWorkflowService.create_grouped_invoice(
                acts=[act1, act2],
                user=self.accountant_user,
            )
        act1.refresh_from_db()
        act2.refresh_from_db()
        self.assertEqual(invoice.acts_count, 2)
        self.assertEqual(invoice.total_amount, act1.total_amount + act2.total_amount)
        self.assertEqual(set(invoice.get_linked_acts().values_list("id", flat=True)), {act1.id, act2.id})
        from billing.invoice_print import build_invoice_print_context
        from django.template.loader import render_to_string

        ctx = build_invoice_print_context(invoice)
        self.assertTrue(ctx["is_grouped"])
        self.assertEqual(len(ctx["sections"]), 2)
        html = render_to_string("billing/invoice_print.html", ctx)
        self.assertNotIn(">Артикул</th>", html)
        self.assertIn(">Товары (работы, услуги)</th>", html)
        app2.refresh_from_db()
        self.assertEqual(app2.billing_status, BillingStatus.INVOICE_DRAFT)

    def test_disputed_act_blocks_invoice_until_confirmed(self):
        act = self._sent_act()
        BillingWorkflowService.dispute_act(act, user=self.client_user, comment="Need correction")
        self.application.refresh_from_db()
        self.assertEqual(self.application.billing_status, BillingStatus.ACT_DISPUTED)
        with self.assertRaises(ValidationError):
            BillingWorkflowService.create_invoice(self.application, user=self.accountant_user)

    def test_partial_full_payment_and_financial_close(self):
        from unittest.mock import patch

        BillingWorkflowService.confirm_act(self._sent_act(), user=self.client_user)
        with patch("accountant.selectors.is_client_billing_ready", return_value=True):
            invoice = BillingWorkflowService.create_invoice(self.application, user=self.accountant_user)
        BillingWorkflowService.register_payment(invoice, amount=Decimal("100.00"), user=self.accountant_user)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.PARTIALLY_PAID)
        self.assertEqual(invoice.debt_amount, Decimal("200.00"))
        with self.assertRaises(ValidationError):
            BillingWorkflowService.close_financially(self.application, user=self.accountant_user)
        BillingWorkflowService.register_payment(invoice, amount=Decimal("200.00"), user=self.accountant_user)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.PAID)
        closed = BillingWorkflowService.close_financially(self.application, user=self.accountant_user)
        self.assertTrue(closed.is_financially_closed)
        self.assertEqual(closed.billing_status, BillingStatus.FINANCIALLY_CLOSED)

    def test_autorecalc_does_not_change_charge_in_act(self):
        charge = self._charge(confirm=True, source_key="immutable-charge")
        BillingWorkflowService.generate_act(self.application, user=self.manager_user)

        again = BillingWorkflowService.create_or_update_charge(
            self.application,
            service=self.service,
            quantity=Decimal("9.000"),
            tariff=Decimal("777.00"),
            vat_rate="20",
            source_key="immutable-charge",
            user=self.manager_user,
            tariff_source_label="Новый тариф",
            is_manual_override=True,
            override_reason="recalc",
            resolve_from_agreed_tariff=False,
        )

        again.refresh_from_db()
        self.assertEqual(again.pk, charge.pk)
        self.assertEqual(again.quantity, Decimal("2.500"))
        self.assertEqual(again.tariff, Decimal("100.0000"))
        self.assertTrue(again.is_included_in_act)

    def test_excluded_charge_is_not_included_in_act(self):
        excluded = self._charge(confirm=True, source_key="excluded-charge")
        included = self._charge(confirm=True, source_key="included-charge")
        BillingWorkflowService.exclude_charge(
            excluded,
            reason=ApplicationCharge.EXCLUDE_WRONG_AUTO,
            comment="Дубль",
            user=self.manager_user,
        )

        act = BillingWorkflowService.generate_act(self.application, user=self.manager_user)

        self.assertEqual(list(act.lines.values_list("charge_id", flat=True)), [included.id])
        excluded.refresh_from_db()
        self.assertFalse(excluded.is_included_in_act)

    def test_invoice_marks_charges_and_payment_external_id_is_idempotent(self):
        from unittest.mock import patch

        charge = self._charge(confirm=True, source_key="invoice-charge")
        act = BillingWorkflowService.generate_act(self.application, user=self.manager_user)
        act = BillingWorkflowService.send_act(act, user=self.manager_user)
        act = BillingWorkflowService.confirm_act(act, user=self.client_user)
        with patch("accountant.selectors.is_client_billing_ready", return_value=True):
            invoice = BillingWorkflowService.create_invoice(self.application, user=self.accountant_user)
        charge.refresh_from_db()
        self.assertTrue(charge.is_included_in_invoice)
        self.assertEqual(invoice.act_id, act.id)

        first = BillingWorkflowService.register_payment(
            invoice,
            amount=Decimal("300.00"),
            user=self.accountant_user,
            external_id="bank-op-1",
        )
        second = BillingWorkflowService.register_payment(
            invoice,
            amount=Decimal("300.00"),
            user=self.accountant_user,
            external_id="bank-op-1",
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(InvoicePayment.objects.filter(invoice=invoice, external_id="bank-op-1").count(), 1)
        with self.assertRaises(ValidationError):
            BillingWorkflowService.register_payment(
                invoice,
                amount=Decimal("1.00"),
                user=self.accountant_user,
                external_id="bank-op-1",
            )

    def test_autorecalc_updates_charge_in_unsent_invoice(self):
        from unittest.mock import patch

        charge = self._charge(confirm=True, source_key="draft-invoice-recalc")
        act = BillingWorkflowService.generate_act(self.application, user=self.manager_user)
        act = BillingWorkflowService.send_act(act, user=self.manager_user)
        act = BillingWorkflowService.confirm_act(act, user=self.client_user)
        with patch("accountant.selectors.is_client_billing_ready", return_value=True):
            invoice = BillingWorkflowService.create_invoice(self.application, user=self.accountant_user)
        self.assertEqual(invoice.status, InvoiceStatus.DRAFT)

        updated = BillingWorkflowService.create_or_update_charge(
            self.application,
            service=self.service,
            quantity=Decimal("2.500"),
            tariff=Decimal("200.00"),
            vat_rate="20",
            source_key="draft-invoice-recalc",
            user=self.manager_user,
            tariff_source_label="Обновлённый тариф",
            is_manual_override=True,
            override_reason="recalc",
            resolve_from_agreed_tariff=False,
        )

        updated.refresh_from_db()
        act.refresh_from_db()
        invoice.refresh_from_db()
        line = act.lines.get(charge=updated)
        self.assertEqual(updated.pk, charge.pk)
        self.assertEqual(updated.tariff, Decimal("200.0000"))
        self.assertEqual(updated.total_amount, Decimal("600.00"))
        self.assertEqual(line.tariff, Decimal("200.0000"))
        self.assertEqual(line.total_amount, Decimal("600.00"))
        self.assertEqual(act.total_amount, Decimal("600.00"))
        self.assertEqual(invoice.total_amount, Decimal("600.00"))
        self.assertEqual(invoice.debt_amount, Decimal("600.00"))

    def test_manager_with_accountant_access_can_edit_price_in_local_draft_invoice(self):
        from unittest.mock import patch

        charge = self._charge(confirm=True, source_key="invoice-price-editor")
        with patch("accountant.selectors.is_client_billing_ready", return_value=True):
            act, invoice = BillingWorkflowService.generate_act_and_invoice(
                self.application,
                user=self.manager_user,
            )
        self.assertFalse(can_edit_invoice_prices(self.manager_user, invoice))
        self.client.force_login(self.manager_user)
        denied = self.client.post(
            f"/team-manager/billing/invoices/{invoice.id}/lines/{charge.id}/price/",
            {"tariff": "120.00", "reason": "Согласовано с клиентом"},
        )
        self.assertEqual(denied.status_code, 403)

        self.manager.access_roles = ["accountant"]
        self.manager.save(update_fields=["access_roles"])
        self.assertTrue(can_edit_invoice_prices(self.manager_user, invoice))
        self.client.force_login(self.manager_user)
        page = self.client.get(f"/team-manager/billing/invoices/{invoice.id}/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Изменить цену")
        updated = self.client.post(
            f"/team-manager/billing/invoices/{invoice.id}/lines/{charge.id}/price/",
            {"tariff": "120.00", "reason": "Согласовано с клиентом"},
        )
        self.assertEqual(updated.status_code, 200, updated.content)

        charge.refresh_from_db()
        act.refresh_from_db()
        invoice.refresh_from_db()
        line = act.lines.get(charge=charge)
        self.assertEqual(charge.tariff, Decimal("120.0000"))
        self.assertEqual(line.tariff, Decimal("120.0000"))
        self.assertEqual(invoice.total_amount, Decimal("360.00"))
        self.assertTrue(charge.is_manual_override)
        self.assertEqual(charge.overridden_by_id, self.manager_user.id)

    def test_manager_with_accountant_access_can_schedule_tariff_revision_from_invoice_price(self):
        from unittest.mock import patch

        self.manager.access_roles = ["accountant"]
        self.manager.save(update_fields=["access_roles"])
        category, _ = TariffCategory.objects.get_or_create(
            code="invoice-edit",
            defaults={"name": "Редактирование счёта", "sort_order": 50},
        )
        unit, _ = TariffUnit.objects.get_or_create(
            code="invoice-piece",
            defaults={"name": "Штука", "short_name": "шт"},
        )
        version = ClientTariffVersion.objects.create(
            client=self.agency,
            name="Текущий тариф",
            version_number=1,
            status=ClientTariffVersion.STATUS_ACTIVE,
            valid_from=timezone.localdate() - timedelta(days=10),
            created_by=self.accountant_user,
        )
        item = ClientTariffItem.objects.create(
            tariff_version=version,
            category=category,
            service=self.service,
            service_name=self.service.name,
            unit=unit,
            price=Decimal("100.0000"),
        )
        charge = BillingWorkflowService.create_or_update_charge(
            self.application,
            service=self.service,
            quantity=Decimal("2.000"),
            source_key="invoice-tariff-revision",
            user=self.manager_user,
            resolve_from_agreed_tariff=True,
        )
        self.assertEqual(charge.client_tariff_item_id, item.id)
        BillingWorkflowService.confirm_charge(charge, user=self.manager_user)
        with patch("accountant.selectors.is_client_billing_ready", return_value=True):
            _act, invoice = BillingWorkflowService.generate_act_and_invoice(
                self.application,
                user=self.manager_user,
            )

        valid_from = timezone.localdate() + timedelta(days=1)
        self.client.force_login(self.manager_user)
        response = self.client.post(
            f"/team-manager/billing/invoices/{invoice.id}/lines/{charge.id}/price/",
            {
                "tariff": "135.00",
                "reason": "Новая согласованная цена",
                "update_client_tariff": "1",
                "tariff_valid_from": valid_from.isoformat(),
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        revision = ClientTariffVersion.objects.get(pk=response.json()["data"]["tariff_version_id"])
        self.assertEqual(revision.status, ClientTariffVersion.STATUS_SCHEDULED)
        self.assertEqual(revision.valid_from, valid_from)
        self.assertEqual(revision.items.get(service=self.service).price, Decimal("135.0000"))
        version.refresh_from_db()
        self.assertEqual(version.valid_to, timezone.localdate())

    def test_invoice_price_edit_is_blocked_after_review_submission(self):
        from unittest.mock import patch

        self.manager.access_roles = ["accountant"]
        self.manager.save(update_fields=["access_roles"])
        charge = self._charge(confirm=True, source_key="invoice-price-submitted")
        with patch("accountant.selectors.is_client_billing_ready", return_value=True):
            _act, invoice = BillingWorkflowService.generate_act_and_invoice(
                self.application,
                user=self.manager_user,
            )
        invoice.review_status = DocumentReviewStatus.SUBMITTED
        invoice.save(update_fields=["review_status", "updated_at"])
        self.client.force_login(self.manager_user)
        response = self.client.post(
            f"/team-manager/billing/invoices/{invoice.id}/lines/{charge.id}/price/",
            {"tariff": "125.00", "reason": "Попытка после отправки"},
        )
        self.assertEqual(response.status_code, 400)
        charge.refresh_from_db()
        self.assertEqual(charge.tariff, Decimal("100.0000"))

    def test_autorecalc_does_not_change_charge_in_sent_invoice(self):
        from unittest.mock import patch

        charge = self._charge(confirm=True, source_key="sent-invoice-recalc")
        act = BillingWorkflowService.generate_act(self.application, user=self.manager_user)
        act = BillingWorkflowService.send_act(act, user=self.manager_user)
        act = BillingWorkflowService.confirm_act(act, user=self.client_user)
        with patch("accountant.selectors.is_client_billing_ready", return_value=True):
            invoice = BillingWorkflowService.create_invoice(self.application, user=self.accountant_user)
        BillingWorkflowService.send_invoice(invoice, user=self.accountant_user)

        updated = BillingWorkflowService.create_or_update_charge(
            self.application,
            service=self.service,
            quantity=Decimal("2.500"),
            tariff=Decimal("200.00"),
            vat_rate="20",
            source_key="sent-invoice-recalc",
            user=self.manager_user,
            tariff_source_label="Обновлённый тариф",
            is_manual_override=True,
            override_reason="recalc",
            resolve_from_agreed_tariff=False,
        )

        updated.refresh_from_db()
        invoice.refresh_from_db()
        self.assertEqual(updated.pk, charge.pk)
        self.assertEqual(updated.tariff, Decimal("100.0000"))
        self.assertEqual(updated.total_amount, Decimal("300.00"))
        self.assertEqual(invoice.total_amount, Decimal("300.00"))

    def test_mark_billed_outside_cancels_billing_and_closes_manager_task(self):
        from billing.manager_billing import _billing_action_from_app, applications_ready_to_invoice
        from todo.models import Task

        self._charge(confirm=True)
        self.application.refresh_from_db()
        task = Task.objects.create(
            title=f"Заявка на приемку №{self.application.application_id}",
            route=f"/orders/receiving/{self.application.application_id}/",
            assigned_to=self.manager,
            status="in_progress",
        )
        updated = BillingWorkflowService.mark_billed_outside(
            self.application,
            user=self.accountant_user,
            comment="Тест / счета вне системы",
        )
        self.assertEqual(updated.billing_status, BillingStatus.CANCELLED)
        external = (updated.source_payload or {}).get("billing_external") or {}
        self.assertEqual(external.get("reason"), "billed_outside")
        self.assertEqual(external.get("comment"), "Тест / счета вне системы")
        self.assertTrue(
            BillingAuditEvent.objects.filter(
                action="billing_cancelled_external", application=self.application
            ).exists()
        )
        task.refresh_from_db()
        self.assertEqual(task.status, "done")
        self.assertEqual(_billing_action_from_app(updated), "")
        ready_ids = {row["application"].id for row in applications_ready_to_invoice(self.accountant_user)}
        self.assertNotIn(self.application.id, ready_ids)

    def test_mark_billed_outside_api_accountant_ok_manager_forbidden(self):
        self._charge(confirm=True)
        url = f"/team-manager/billing/api/applications/{self.application.id}/mark-external/"
        self.client.force_login(self.manager_user)
        deny = self.client.post(url, data=json.dumps({"comment": "no"}), content_type="application/json")
        self.assertEqual(deny.status_code, 403)
        self.application.refresh_from_db()
        self.assertNotEqual(self.application.billing_status, BillingStatus.CANCELLED)

        self.client.force_login(self.accountant_user)
        ok = self.client.post(url, data=json.dumps({"comment": "вне системы"}), content_type="application/json")
        self.assertEqual(ok.status_code, 200)
        payload = ok.json()
        self.assertTrue(payload.get("ok"))
        self.application.refresh_from_db()
        self.assertEqual(self.application.billing_status, BillingStatus.CANCELLED)

    def test_mark_billed_outside_does_not_touch_shipping_status(self):
        from shipping.models import ShippingOrder

        order = ShippingOrder.objects.create(
            number="SO-TEST-EXT-1",
            agency=self.agency,
            status="shipped",
        )
        app = BillingWorkflowService.sync_application_from_source(
            application_type=BillingApplication.TYPE_SHIPPING,
            application_id=order.number,
            client=self.agency,
            manager=self.manager,
            operational_status="shipped",
            operational_status_label="Отгружена",
            created_at_source=timezone.now(),
            user=self.manager_user,
        )
        BillingWorkflowService.create_or_update_charge(
            app,
            service=self.service,
            quantity=Decimal("1"),
            tariff=Decimal("50.00"),
            vat_rate="20",
            source_key="ext-ship-charge",
            user=self.manager_user,
            is_manual_override=True,
            override_reason="test",
            resolve_from_agreed_tariff=False,
        )
        BillingWorkflowService.mark_billed_outside(app, user=self.accountant_user, comment="legacy")
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")
        app.refresh_from_db()
        self.assertEqual(app.billing_status, BillingStatus.CANCELLED)

    def test_billing_requests_page_shows_guidance_for_accountant(self):
        self.client.force_login(self.accountant_user)
        resp = self.client.get("/team-manager/billing/requests/?tab=ready")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Можно выставлять")
        self.assertContains(resp, "Счета вне системы")
        self.assertContains(resp, "Выставляйте счета только из вкладки")

    def test_manager_can_view_billing_registry(self):
        other_application = BillingWorkflowService.sync_application_from_source(
            application_type=BillingApplication.TYPE_SHIPPING,
            application_id="SHP-200",
            client=self.other_client,
            manager=self.other_manager,
        )
        self.assertTrue(can_view_billing(self.manager_user, self.application))
        self.assertFalse(can_view_billing(self.manager_user, other_application))
        scoped = filter_applications_for_user(BillingApplication.objects.all(), self.manager_user)
        self.assertIn(self.application.id, scoped.values_list("id", flat=True))
        self.assertNotIn(other_application.id, scoped.values_list("id", flat=True))

    def test_manager_api_sees_billing_registry(self):
        BillingWorkflowService.sync_application_from_source(
            application_type=BillingApplication.TYPE_SHIPPING,
            application_id="SHP-200",
            client=self.other_client,
            manager=self.other_manager,
        )
        self.client.force_login(self.manager_user)
        response = self.client.get("/team-manager/billing/api/applications/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        ids = {row["application_id"] for row in payload["data"]}
        self.assertIn("RCV-100", ids)
        self.assertNotIn("SHP-200", ids)

    def test_manager_portfolio_client_card_and_alias(self):
        self.client.force_login(self.manager_user)
        ok = self.client.get(f"/team-manager/billing/clients/{self.agency.id}/")
        self.assertEqual(ok.status_code, 200)
        self.assertContains(ok, "Финансовая карточка")
        forbidden = self.client.get(f"/team-manager/billing/clients/{self.other_client.id}/")
        self.assertEqual(forbidden.status_code, 403)
        alias = self.client.get("/manager/billing/clients/")
        self.assertEqual(alias.status_code, 302)
        self.assertIn("/team-manager/billing/clients/", alias.url)
        self.assertFalse(can_add_client_tariff(self.manager_user))
        self.assertFalse(can_override_billing_tariff(self.manager_user))
        self.assertTrue(can_add_client_tariff(self.accountant_user))
        self.assertTrue(can_manage_charges(self.accountant_user, self.application))

    def test_tariff_service_get_service_price(self):
        from .tariff_services import activate_tariff_version

        cat, _ = TariffCategory.objects.get_or_create(code="recv", defaults={"name": "Приемка", "sort_order": 1})
        unit, _ = TariffUnit.objects.get_or_create(code="box", defaults={"name": "короб", "short_name": "короб"})
        version = ClientTariffVersion.objects.create(
            client=self.agency,
            name="Тест",
            version_number=1,
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=timezone.localdate() - timedelta(days=1),
        )
        ClientTariffItem.objects.create(
            tariff_version=version,
            category=cat,
            service=self.service,
            service_name=self.service.name,
            unit=unit,
            price=Decimal("15.0000"),
        )
        activate_tariff_version(version, user=self.accountant_user)
        price = TariffService.get_service_price(
            self.agency.id,
            self.service.id,
            timezone.localdate(),
            quantity=Decimal("2"),
        )
        self.assertIsNotNone(price)
        self.assertEqual(price["tariff_version_id"], version.id)
        self.assertEqual(price["unit_price"], Decimal("15.0000"))

    def test_manager_cannot_override_price_or_delete_charge(self):
        charge = self._charge(confirm=False)
        self.client.force_login(self.manager_user)
        resp = self.client.patch(
            f"/team-manager/billing/api/charges/{charge.id}/",
            data=json.dumps({"tariff": "1.00", "override_reason": "hack"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)
        resp_vat = self.client.patch(
            f"/team-manager/billing/api/charges/{charge.id}/",
            data=json.dumps({"vat_rate": "0"}),
            content_type="application/json",
        )
        self.assertEqual(resp_vat.status_code, 403)
        resp_del = self.client.delete(f"/team-manager/billing/api/charges/{charge.id}/")
        self.assertEqual(resp_del.status_code, 403)
        self.assertTrue(ApplicationCharge.objects.filter(pk=charge.id).exists())

    def test_manager_confirms_qty_and_act_requires_confirm(self):
        charge = self._charge(confirm=False)
        with self.assertRaises(ValidationError):
            BillingWorkflowService.generate_act(self.application, user=self.manager_user)
        self.client.force_login(self.manager_user)
        resp = self.client.post(
            f"/team-manager/billing/api/charges/{charge.id}/confirm/",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["data"]["is_confirmed"])
        act = BillingWorkflowService.generate_act(self.application, user=self.manager_user)
        self.assertEqual(act.status, ActStatus.DRAFT)

    def test_confirm_promotes_calculation_draft_and_shows_create_act(self):
        """Даже при missing_tariffs в meta кнопка «Создать акт» появляется после confirm."""
        self.application.billing_status = BillingStatus.CALCULATION_DRAFT
        self.application.source_payload = {
            "billing": {
                "missing_tariffs": [
                    {
                        "service_code": "extra_warehouse_operation",
                        "service_name": "Прочая складская услуга",
                        "quantity": "10",
                    }
                ],
                "missing_tariff_count": 1,
            }
        }
        self.application.save(update_fields=["billing_status", "source_payload", "updated_at"])
        self._charge(confirm=False)
        self.client.force_login(self.manager_user)
        resp = self.client.post(
            f"/team-manager/billing/api/applications/{self.application.id}/confirm-charges/",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.application.refresh_from_db()
        self.assertEqual(self.application.billing_status, BillingStatus.CALCULATED)
        page = self.client.get(f"/team-manager/billing/applications/{self.application.id}/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="js-act-btn"')
        self.assertContains(page, "Создать акт")
        from billing.manager_billing import _billing_action_from_app

        self.assertEqual(_billing_action_from_app(self.application, unconfirmed=False), "Сформировать акт")

    def test_manager_can_change_quantity_only(self):
        charge = self._charge(confirm=True)
        self.client.force_login(self.manager_user)
        resp = self.client.patch(
            f"/team-manager/billing/api/charges/{charge.id}/",
            data=json.dumps({"quantity": "3.000"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()["data"]
        self.assertEqual(payload["quantity"], "3.000")
        self.assertFalse(payload["is_confirmed"])
        self.assertEqual(payload["tariff"], "100.0000")

    def _publish_two_services_tariff(self):
        other, _ = BillingService.objects.get_or_create(
            code="shipping_load_boxes_upto_25kg",
            defaults={"name": "Погрузка коробами", "unit": "усл", "vat_rate": "20"},
        )
        company = OwnCompany.objects.create(
            name="FB Test VAT",
            short_name="FB TV",
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
            name="Тест услуг",
            version_number=1,
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=timezone.localdate() - timedelta(days=1),
            manager=self.manager,
            created_by=self.manager_user,
        )
        for svc, price in ((self.service, Decimal("100.0000")), (other, Decimal("60.0000"))):
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
        return other, version

    def test_manager_can_change_service_and_accountant_is_notified(self):
        other, version = self._publish_two_services_tariff()
        charge = BillingWorkflowService.create_or_update_charge(
            self.application,
            service=self.service,
            quantity=Decimal("2.000"),
            source_key="svc-change-1",
            user=self.manager_user,
            resolve_from_agreed_tariff=True,
        )
        charge = BillingWorkflowService.confirm_charge(charge, user=self.manager_user)
        self.assertTrue(charge.is_confirmed)
        self.assertEqual(charge.client_tariff_version_id, version.id)

        self.client.force_login(self.manager_user)
        resp = self.client.patch(
            f"/team-manager/billing/api/charges/{charge.id}/",
            data=json.dumps({"service_id": other.id, "quantity": "2.000"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        payload = resp.json()["data"]
        self.assertEqual(payload["service"]["id"], other.id)
        self.assertFalse(payload["is_confirmed"])
        self.assertTrue(payload["service_changed"])
        self.assertEqual(payload["previous_service_name"], self.service.name)
        self.assertEqual(payload["tariff"], "60.0000")

        charge.refresh_from_db()
        self.assertEqual(charge.service_id, other.id)
        self.assertIsNotNone(charge.service_changed_at)
        self.assertTrue(
            BillingAuditEvent.objects.filter(action="charge_service_changed", application=self.application).exists()
        )
        self.assertTrue(
            BillingStaffNotification.objects.filter(
                recipient=self.accountant_user,
                kind=BillingStaffNotification.KIND_SERVICE_CHANGED,
            ).exists()
        )

        page = self.client.get(f"/team-manager/billing/applications/{self.application.id}/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "js-service-select")
        self.assertContains(page, "Услуга изменена")
        self.assertContains(page, self.service.name)

    def test_manager_can_change_service_in_draft_act_and_open_act(self):
        other, _ = self._publish_two_services_tariff()
        charge = BillingWorkflowService.create_or_update_charge(
            self.application,
            service=self.service,
            quantity=Decimal("1.000"),
            source_key="svc-in-draft-act",
            user=self.manager_user,
            resolve_from_agreed_tariff=True,
        )
        BillingWorkflowService.confirm_charge(charge, user=self.manager_user)
        act = BillingWorkflowService.generate_act(self.application, user=self.manager_user)
        charge.refresh_from_db()
        self.assertTrue(charge.is_included_in_act)

        self.client.force_login(self.manager_user)
        page = self.client.get(f"/team-manager/billing/applications/{self.application.id}/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "js-service-select")
        self.assertContains(page, "Открыть акт")
        self.assertContains(page, f"/team-manager/billing/acts/{act.id}/print/")

        print_resp = self.client.get(f"/team-manager/billing/acts/{act.id}/print/")
        self.assertEqual(print_resp.status_code, 200)
        self.assertContains(print_resp, act.number)

        resp = self.client.patch(
            f"/team-manager/billing/api/charges/{charge.id}/",
            data=json.dumps({"service_id": other.id}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        charge.refresh_from_db()
        act.refresh_from_db()
        self.assertEqual(charge.service_id, other.id)
        self.assertFalse(charge.is_included_in_act)
        self.assertEqual(act.status, ActStatus.CANCELLED)
        self.assertTrue(charge.service_changed_at)

    def test_manager_cannot_change_service_when_act_sent(self):
        other, _ = self._publish_two_services_tariff()
        charge = BillingWorkflowService.create_or_update_charge(
            self.application,
            service=self.service,
            quantity=Decimal("1.000"),
            source_key="svc-in-sent-act",
            user=self.manager_user,
            resolve_from_agreed_tariff=True,
        )
        BillingWorkflowService.confirm_charge(charge, user=self.manager_user)
        act = BillingWorkflowService.generate_act(self.application, user=self.manager_user)
        BillingWorkflowService.send_act(act, user=self.manager_user)
        charge.refresh_from_db()
        self.assertTrue(charge.is_included_in_act)

        self.client.force_login(self.manager_user)
        resp = self.client.patch(
            f"/team-manager/billing/api/charges/{charge.id}/",
            data=json.dumps({"service_id": other.id}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        charge.refresh_from_db()
        self.assertEqual(charge.service_id, self.service.id)

    def test_charges_registry_page_ok(self):
        self._charge(confirm=False)
        self.client.force_login(self.manager_user)
        resp = self.client.get("/team-manager/billing/charges/?unconfirmed=1")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Начисления")

    def test_requests_and_extra_services_pages(self):
        self._charge(confirm=True)
        self.client.force_login(self.manager_user)
        resp = self.client.get("/team-manager/billing/requests/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Заявки к выставлению")
        resp2 = self.client.get("/team-manager/billing/extra-services/")
        self.assertEqual(resp2.status_code, 200)
        self.assertContains(resp2, "Дополнительные услуги")
        resp3 = self.client.get("/team-manager/billing/storage/")
        self.assertEqual(resp3.status_code, 200)

    def test_storage_page_with_client_filter_and_open_error_ok(self):
        from .models import StorageBillingError

        day = timezone.localdate()
        StorageBillingError.objects.create(
            client=self.agency,
            day=day,
            error_type=StorageBillingError.TYPE_OTHER,
            severity=StorageBillingError.SEVERITY_ERROR,
            message="Проверочная ошибка хранения",
        )

        self.client.force_login(self.accountant_user)
        resp = self.client.get(
            f"/team-manager/billing/storage/?date_from={day.isoformat()}&date_to={day.isoformat()}&client={self.agency.id}&status="
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="date_from"')
        self.assertContains(resp, 'name="date_to"')
        self.assertContains(resp, f'value="{day.isoformat()}"', count=2)
        self.assertContains(resp, "Открытые ошибки хранения")

    def test_extra_service_needs_price_without_tariff(self):
        self.client.force_login(self.manager_user)
        resp = self.client.post(
            "/team-manager/billing/api/extra-services/",
            data=json.dumps(
                {
                    "client_id": self.agency.id,
                    "service_id": self.service.id,
                    "quantity": "2",
                    "service_date": timezone.localdate().isoformat(),
                    "description": "Ручная доработка",
                    "reason": "Не пришло из WMS",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["status"], "needs_price")
        self.assertIsNone(data["charge_id"])

    def test_extra_service_creates_charge_with_tariff(self):
        from .tariff_services import activate_tariff_version

        cat, _ = TariffCategory.objects.get_or_create(code="recv", defaults={"name": "Приемка", "sort_order": 1})
        unit, _ = TariffUnit.objects.get_or_create(code="box", defaults={"name": "короб", "short_name": "короб"})
        version = ClientTariffVersion.objects.create(
            client=self.agency,
            name="Extra",
            version_number=1,
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=timezone.localdate() - timedelta(days=1),
        )
        ClientTariffItem.objects.create(
            tariff_version=version,
            category=cat,
            service=self.service,
            service_name=self.service.name,
            unit=unit,
            price=Decimal("10.0000"),
        )
        activate_tariff_version(version, user=self.accountant_user)
        self.client.force_login(self.manager_user)
        resp = self.client.post(
            "/team-manager/billing/api/extra-services/",
            data=json.dumps(
                {
                    "client_id": self.agency.id,
                    "service_id": self.service.id,
                    "quantity": "3",
                    "service_date": timezone.localdate().isoformat(),
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["status"], "charged")
        self.assertIsNotNone(data["charge_id"])
        charge = ApplicationCharge.objects.get(pk=data["charge_id"])
        self.assertEqual(charge.tariff, Decimal("10.0000"))
        self.assertFalse(charge.is_manual_override)

    def test_storage_api_portfolio_scoped(self):
        from .models import BillingStorageDay

        BillingStorageDay.objects.create(
            client=self.other_client,
            day=timezone.localdate(),
            pallet_count=1,
            amount=Decimal("100.00"),
        )
        own = BillingStorageDay.objects.create(
            client=self.agency,
            day=timezone.localdate(),
            pallet_count=2,
            amount=Decimal("200.00"),
        )
        self.client.force_login(self.manager_user)
        resp = self.client.get("/team-manager/billing/api/storage/days/")
        self.assertEqual(resp.status_code, 200)
        ids = {row["id"] for row in resp.json()["data"]}
        self.assertIn(own.id, ids)
        forbidden = self.client.get(f"/team-manager/billing/api/storage/days/{BillingStorageDay.objects.get(client=self.other_client).id}/")
        self.assertEqual(forbidden.status_code, 403)

    def test_manager_invoice_draft_submit_and_accountant_accept(self):
        from unittest.mock import patch

        from .permissions import can_create_invoice, can_create_invoice_draft
        from .statuses import DocumentReviewStatus

        self.assertTrue(can_create_invoice_draft(self.manager_user, self.application))
        self.assertFalse(can_create_invoice(self.manager_user, self.application))
        act = BillingWorkflowService.confirm_act(self._sent_act(), user=self.client_user)
        with patch("accountant.selectors.is_client_billing_ready", return_value=True):
            self.client.force_login(self.manager_user)
            create_resp = self.client.post(f"/team-manager/billing/api/applications/{self.application.id}/invoice/")
            self.assertEqual(create_resp.status_code, 200, create_resp.content)
            invoice_id = create_resp.json()["data"]["id"]
            submit = self.client.post(
                f"/team-manager/billing/api/invoices/{invoice_id}/submit/",
                data=json.dumps({}),
                content_type="application/json",
            )
            self.assertEqual(submit.status_code, 200, submit.content)
            self.assertEqual(submit.json()["data"].get("review_status"), DocumentReviewStatus.SUBMITTED)
            # менеджер не проводит счёт
            deny = self.client.post(f"/team-manager/billing/api/invoices/{invoice_id}/check/")
            self.assertEqual(deny.status_code, 403)
            self.client.force_login(self.accountant_user)
            accept = self.client.post(
                f"/team-manager/billing/api/invoices/{invoice_id}/accept/",
                data=json.dumps({}),
                content_type="application/json",
            )
            self.assertEqual(accept.status_code, 200, accept.content)
            inv = ClientInvoice.objects.get(pk=invoice_id)
            self.assertEqual(inv.review_status, DocumentReviewStatus.ACCEPTED)
            self.assertEqual(inv.status, InvoiceStatus.ISSUED)

    def test_document_drafts_upd_audit_pages(self):
        self.client.force_login(self.manager_user)
        for path, title in (
            ("/team-manager/billing/document-drafts/", "Черновики документов"),
            ("/team-manager/billing/upd/", "Запросы УПД"),
            ("/team-manager/billing/audit/", "История действий"),
        ):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            self.assertContains(resp, title)

    def test_upd_request_create(self):
        self.client.force_login(self.manager_user)
        resp = self.client.post(
            "/team-manager/billing/api/upd-requests/",
            data=json.dumps({"client_id": self.agency.id, "comment": "Нужен УПД за июль"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["status"], "requested")

    def test_stage6_payments_debts_pages(self):
        from unittest.mock import patch

        from .models import InvoicePayment

        BillingWorkflowService.confirm_act(self._sent_act(), user=self.client_user)
        with patch("accountant.selectors.is_client_billing_ready", return_value=True):
            invoice = BillingWorkflowService.create_invoice(self.application, user=self.accountant_user)
        BillingWorkflowService.register_payment(invoice, amount=Decimal("50.00"), user=self.accountant_user)
        self.client.force_login(self.manager_user)
        for path, title in (
            ("/team-manager/billing/payments/", "Оплаты"),
            ("/team-manager/billing/debts/", "Задолженность"),
            ("/team-manager/billing/overdue/", "Задолженность"),
            ("/team-manager/billing/promises/", "Обещания оплаты"),
            ("/team-manager/billing/reconciliation/", "Акты сверки"),
            ("/team-manager/billing/disputes/", "Расхождения"),
        ):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            self.assertContains(resp, title)
        ledger = self.client.get("/team-manager/billing/api/payments/")
        self.assertEqual(ledger.status_code, 200)
        self.assertTrue(any(row["amount"] == "50.00" for row in ledger.json()["data"]))
        self.assertTrue(InvoicePayment.objects.filter(invoice=invoice).exists())
        deny = self.client.post(
            f"/team-manager/billing/api/invoices/{invoice.id}/payments/",
            data=json.dumps({"amount": "10.00"}),
            content_type="application/json",
        )
        self.assertEqual(deny.status_code, 403)
        # чужой менеджер не видит оплату
        self.client.force_login(self.other_manager_user)
        other_ledger = self.client.get("/team-manager/billing/api/payments/")
        self.assertEqual(other_ledger.status_code, 200)
        self.assertEqual(other_ledger.json()["data"], [])

    def test_stage7_reports_and_notifications(self):
        from unittest.mock import patch

        from .models import BillingStaffNotification
        from .staff_notifications import create_staff_notification, list_staff_notifications

        self.client.force_login(self.manager_user)
        for path, title in (
            ("/team-manager/billing/reports/", "Отчёты"),
            ("/team-manager/billing/report/", "Отчёты"),
            ("/team-manager/billing/notifications/", "Уведомления"),
        ):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            self.assertContains(resp, title)

        csv_resp = self.client.get("/team-manager/billing/reports/?report=charges_by_client&export=csv")
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn("text/csv", csv_resp["Content-Type"])

        create_staff_notification(
            recipient=self.manager_user,
            kind=BillingStaffNotification.KIND_OVERDUE,
            title="Тест просрочки",
            message="Счёт просрочен",
            link_url="/team-manager/billing/debts/",
            client=self.agency,
            source_key="test-stage7-overdue-1",
        )
        api = self.client.get("/team-manager/billing/api/notifications/")
        self.assertEqual(api.status_code, 200)
        self.assertTrue(any(n["title"] == "Тест просрочки" for n in api.json()["data"]))
        nid = list_staff_notifications(self.manager_user).first().id
        read = self.client.post(f"/team-manager/billing/api/notifications/{nid}/read/")
        self.assertEqual(read.status_code, 200)
        self.assertTrue(BillingStaffNotification.objects.get(pk=nid).is_read)

        # возврат документа создаёт уведомление менеджеру
        BillingWorkflowService.confirm_act(self._sent_act(), user=self.client_user)
        with patch("accountant.selectors.is_client_billing_ready", return_value=True):
            self.client.force_login(self.manager_user)
            create_resp = self.client.post(f"/team-manager/billing/api/applications/{self.application.id}/invoice/")
            self.assertEqual(create_resp.status_code, 200, create_resp.content)
            invoice_id = create_resp.json()["data"]["id"]
            self.client.post(
                f"/team-manager/billing/api/invoices/{invoice_id}/submit/",
                data=json.dumps({}),
                content_type="application/json",
            )
            self.client.force_login(self.accountant_user)
            ret = self.client.post(
                f"/team-manager/billing/api/invoices/{invoice_id}/return/",
                data=json.dumps({"comment": "Нужны реквизиты"}),
                content_type="application/json",
            )
            self.assertEqual(ret.status_code, 200, ret.content)
            self.assertTrue(
                BillingStaffNotification.objects.filter(
                    recipient=self.manager_user,
                    kind=BillingStaffNotification.KIND_DOC_RETURNED,
                ).exists()
            )

    def test_stage6_promise_recon_discrepancy_api(self):
        self.client.force_login(self.manager_user)
        promise = self.client.post(
            "/team-manager/billing/api/promises/",
            data=json.dumps(
                {
                    "client_id": self.agency.id,
                    "amount": "1000.00",
                    "promised_date": timezone.localdate().isoformat(),
                    "manager_comment": "Оплатит в пятницу",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(promise.status_code, 200, promise.content)
        promise_id = promise.json()["data"]["id"]
        from .models import PaymentPromise

        self.assertEqual(PaymentPromise.objects.get(pk=promise_id).status, "pending")
        # обещание не трогает долг
        self.assertEqual(ClientInvoice.objects.filter(application=self.application).count(), 0)

        recon = self.client.post(
            "/team-manager/billing/api/reconciliation/",
            data=json.dumps(
                {
                    "client_id": self.agency.id,
                    "period_from": "2026-07-01",
                    "period_to": "2026-07-31",
                    "submit": True,
                    "manager_comment": "Сверка за июль",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(recon.status_code, 200, recon.content)
        self.assertEqual(recon.json()["data"]["status"], "submitted")

        disc = self.client.post(
            "/team-manager/billing/api/discrepancies/",
            data=json.dumps(
                {
                    "client_id": self.agency.id,
                    "discrepancy_type": "price",
                    "description": "Цена приёмки не сходится с тарифом",
                    "disputed_amount": "150.00",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(disc.status_code, 200, disc.content)
        disc_id = disc.json()["data"]["id"]
        close = self.client.post(
            f"/team-manager/billing/api/discrepancies/{disc_id}/",
            data=json.dumps({"status": "closed"}),
            content_type="application/json",
        )
        self.assertEqual(close.status_code, 200, close.content)
        self.assertEqual(close.json()["data"]["status"], "closed")

        # чужой клиент запрещён
        deny = self.client.post(
            "/team-manager/billing/api/promises/",
            data=json.dumps(
                {
                    "client_id": self.other_client.id,
                    "amount": "10.00",
                    "promised_date": timezone.localdate().isoformat(),
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(deny.status_code, 403)

    def test_accountant_can_open_billing_overview(self):
        self.client.force_login(self.accountant_user)
        response = self.client.get("/team-manager/billing/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Биллинг и счета")
        self.assertContains(response, 'href="/accountant/"')
        self.assertNotContains(response, 'href="/team-manager/orders/"')

    def test_manager_keeps_manager_navigation_in_billing(self):
        self.client.force_login(self.manager_user)
        response = self.client.get("/team-manager/billing/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="/team-manager/orders/"')

    def test_billing_overview_shows_action_board(self):
        self.client.force_login(self.manager_user)
        response = self.client.get("/team-manager/billing/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "К выставлению")
        self.assertContains(response, "Начисления не проверены")
        self.assertContains(response, "Требуют действий")
        self.assertContains(response, "Ошибки тарификации")
        from billing.manager_billing import billing_next_action_for_wms

        self.assertEqual(
            billing_next_action_for_wms(
                application_type="shipping",
                application_id="missing-id",
                client_id=None,
            ),
            "",
        )

    def test_billing_clients_show_setup_status(self):
        from billing.manager_billing import client_setup_status

        self.client.force_login(self.manager_user)
        response = self.client.get("/team-manager/billing/clients/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Состояние")
        # Без договора/тарифа — один из статусов настройки
        self.assertTrue(
            any(
                label in response.content.decode("utf-8")
                for label in (
                    "всё настроено",
                    "отсутствует договор",
                    "отсутствует прайс",
                    "не настроены тарифы",
                    "есть ошибка реквизитов",
                    "есть задолженность",
                )
            )
        )
        empty = client_setup_status(
            {
                "client": self.agency,
                "contract": None,
                "contract_number": "",
                "tariff": None,
                "commercial_offer": "",
                "overdue_total": 0,
                "published_zero_price": 0,
                "draft_zero_price": 0,
            }
        )
        self.assertEqual(empty["code"], "no_contract")

    def test_client_api_confirm_act(self):
        act = self._sent_act()
        self.client.force_login(self.client_user)
        response = self.client.post(
            f"/client/api/v1/billing/acts/{act.id}/confirm/",
            data=json.dumps({"comment": "Подтверждаем"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        act.refresh_from_db()
        self.assertEqual(act.status, ActStatus.CONFIRMED)

    def test_sequence_numbers_are_unique(self):
        numbers = {BillingWorkflowService.next_number("invoice") for _ in range(5)}
        self.assertEqual(len(numbers), 5)


class AgreedTariffBillingTests(TestCase):
    """Цена биллинга только из ClientTariffVersion на дату услуги."""

    def setUp(self):
        User = get_user_model()
        self.manager_user = User.objects.create_user(username="agreed_mgr", password="pass", is_staff=True)
        self.manager = Employee.objects.create(full_name="Agreed Manager", role="manager", user=self.manager_user)
        self.client_agency = Agency.objects.create(agn_name="Agreed Client", short_name="Agreed", inn="7700000001")
        self.service, _ = BillingService.objects.get_or_create(
            code="shipping_pick_storage_item",
            defaults={"name": "Сборка с хранения", "unit": "шт", "vat_rate": "20"},
        )
        self.company = OwnCompany.objects.create(name="FullBox VAT", short_name="FB VAT", tax_mode=OwnCompany.TAX_MODE_VAT, vat_rate="20", is_default=True)
        self.contract = ClientBillingContract.objects.create(
            client=self.client_agency, own_company=self.company, pricing_mode=ClientBillingContract.PRICING_INDIVIDUAL
        )
        self.category, _ = TariffCategory.objects.get_or_create(code="shipping", defaults={"name": "Отгрузка", "sort_order": 10})
        self.unit, _ = TariffUnit.objects.get_or_create(code="piece", defaults={"name": "Штука", "short_name": "шт."})
        self.application = BillingApplication.objects.create(
            application_type=BillingApplication.TYPE_SHIPPING,
            application_id="SHP-AGREED",
            client=self.client_agency,
            legal_entity=self.client_agency,
            own_company=self.company,
            created_at_source=timezone.now(),
        )

    def _activate_version(self, *, valid_from, price, name="Тарифы", vat_type=ClientTariffVersion.VAT_EXTRA):
        version = ClientTariffVersion.objects.create(
            client=self.client_agency,
            contract=self.contract,
            name=name,
            version_number=ClientTariffVersion.objects.filter(client=self.client_agency).count() + 1,
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=valid_from,
            vat_type=vat_type,
            manager=self.manager,
            created_by=self.manager_user,
        )
        ClientTariffItem.objects.create(
            tariff_version=version,
            category=self.category,
            service=self.service,
            service_name=self.service.name,
            unit=self.unit,
            price=price,
        )
        version.status = ClientTariffVersion.STATUS_ACTIVE
        version.save(update_fields=["status", "updated_at"])
        return version

    def test_price_from_agreed_tariff_version(self):
        version = self._activate_version(valid_from=timezone.localdate(), price=Decimal("5.0000"), name="Тарифы с 01.07.2026")
        resolved = resolve_client_service_price(self.application, self.service, performed_at=timezone.now())
        self.assertTrue(resolved.ok)
        self.assertEqual(resolved.source, "agreed_tariff")
        self.assertEqual(resolved.tariff, Decimal("5.0000"))
        self.assertEqual(resolved.tariff_version.id, version.id)
        self.assertEqual(resolved.tariff_source_label, f"Тариф клиента · действует с {timezone.localdate():%d.%m.%Y}")

    def test_application_detail_uses_tariff_order_and_clear_amounts(self):
        admin_user = get_user_model().objects.create_superuser(
            username="billing_detail_admin",
            password="pass",
            email="admin@example.com",
        )
        services = []
        for code, name in [
            ("detail_sort_c", "Третья услуга"),
            ("detail_sort_a", "Первая услуга"),
            ("detail_sort_b", "Вторая услуга"),
        ]:
            service, _ = BillingService.objects.get_or_create(
                code=code,
                defaults={"name": name, "unit": "шт", "vat_rate": "5"},
            )
            services.append(service)

        version = ClientTariffVersion.objects.create(
            client=self.client_agency,
            contract=self.contract,
            name="Тарифы с 19.07.2026",
            version_number=20,
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=timezone.localdate(),
            manager=self.manager,
            created_by=self.manager_user,
        )
        item_c = ClientTariffItem.objects.create(
            tariff_version=version,
            category=self.category,
            service=services[0],
            service_name=services[0].name,
            unit=self.unit,
            price=Decimal("15.0000"),
            sort_order=30,
        )
        item_a = ClientTariffItem.objects.create(
            tariff_version=version,
            category=self.category,
            service=services[1],
            service_name=services[1].name,
            unit=self.unit,
            price=Decimal("15.0000"),
            sort_order=10,
        )
        item_b = ClientTariffItem.objects.create(
            tariff_version=version,
            category=self.category,
            service=services[2],
            service_name=services[2].name,
            unit=self.unit,
            price=Decimal("15.0000"),
            sort_order=20,
        )
        version.status = ClientTariffVersion.STATUS_ACTIVE
        version.save(update_fields=["status", "updated_at"])

        def create_charge(service, item, *, source_key, excluded=False):
            return ApplicationCharge.objects.create(
                application=self.application,
                client=self.client_agency,
                legal_entity=self.client_agency,
                service=service,
                quantity=Decimal("30.000"),
                unit="шт",
                tariff=Decimal("15.0000"),
                tariff_price=Decimal("15.0000"),
                amount=Decimal("450.00"),
                vat_rate="5",
                vat_amount=Decimal("22.50"),
                total_amount=Decimal("472.50"),
                client_tariff_version=version,
                client_tariff_item=item,
                service_name_snapshot=service.name,
                tariff_source_label="Тарифы с 19.07.2026 с 01.05.2025",
                billing_period=timezone.localdate(),
                source_key=source_key,
                is_excluded=excluded,
            )

        create_charge(services[0], item_c, source_key="detail-c")
        create_charge(services[1], item_a, source_key="detail-a")
        create_charge(services[2], item_b, source_key="detail-b", excluded=True)

        self.client.force_login(admin_user)
        response = self.client.get(f"/team-manager/billing/applications/{self.application.id}/")
        self.assertEqual(response.status_code, 200)
        rows = response.context["charge_rows"]
        self.assertEqual([row["charge"].service.name for row in rows], ["Первая услуга", "Третья услуга", "Вторая услуга"])
        self.assertEqual(rows[0]["tariff_source_label"], f"Тариф клиента · действует с {timezone.localdate():%d.%m.%Y}")
        self.assertContains(response, "450.00 ₽")
        self.assertContains(response, "Итого: 472.50 ₽")

    def test_missing_agreed_tariff_blocks_price(self):
        resolved = resolve_client_service_price(self.application, self.service, performed_at=timezone.now())
        self.assertFalse(resolved.ok)
        self.assertEqual(resolved.source, "missing_agreed_tariff")
        self.assertIn("не найден согласованный тариф", resolved.note)

    def test_cannot_save_charge_price_from_catalog_without_tariff_version(self):
        """default_price каталога не должен попадать в начисление без тарифа клиента."""
        self.service.default_price = Decimal("15.0000")
        self.service.save(update_fields=["default_price"])
        with self.assertRaises(ValidationError):
            BillingWorkflowService.create_or_update_charge(
                self.application,
                service=self.service,
                quantity=Decimal("10"),
                tariff=self.service.default_price,
                source_key="no-tariff-catalog",
                user=self.manager_user,
                resolve_from_agreed_tariff=False,
            )

    def test_historical_tariff_by_operation_date(self):
        old = self._activate_version(valid_from=timezone.localdate() - timedelta(days=40), price=Decimal("5.0000"), name="Старые")
        old.valid_to = timezone.localdate() - timedelta(days=1)
        old.save(update_fields=["valid_to", "updated_at"])
        self._activate_version(valid_from=timezone.localdate(), price=Decimal("7.0000"), name="Новые")
        op_date = timezone.localdate() - timedelta(days=10)
        resolved = resolve_client_service_price(
            self.application,
            self.service,
            performed_at=timezone.make_aware(datetime.combine(op_date, datetime.min.time())),
        )
        self.assertTrue(resolved.ok)
        self.assertEqual(resolved.tariff, Decimal("5.0000"))

    def test_charge_snapshot_and_invoice_blocked_on_missing(self):
        version = self._activate_version(valid_from=timezone.localdate(), price=Decimal("5.0000"))
        charge = BillingWorkflowService.create_or_update_charge(
            self.application,
            service=self.service,
            quantity=Decimal("1500"),
            source_key="agreed-1",
            user=self.manager_user,
        )
        self.assertEqual(charge.client_tariff_version_id, version.id)
        self.assertEqual(charge.tariff_price, Decimal("5.0000"))
        self.assertEqual(charge.total_amount, Decimal("9000.00"))  # 1500*5 + 20% VAT
        self.assertFalse(charge.is_manual_override)

        # без тарифа — create_invoice блокируется
        self.application.source_payload = {
            "billing": {"missing_tariffs": [{"service_name": "X", "service_code": "x"}], "missing_tariff_count": 1}
        }
        self.application.save(update_fields=["source_payload"])
        with self.assertRaises(ValidationError):
            BillingWorkflowService.create_invoice(self.application, user=self.manager_user)

    def test_charge_uses_vat_included_tariff_type(self):
        version = self._activate_version(
            valid_from=timezone.localdate(),
            price=Decimal("1000.0000"),
            vat_type=ClientTariffVersion.VAT_WITH,
        )
        self.company.vat_rate = "5"
        self.company.save(update_fields=["vat_rate"])

        charge = BillingWorkflowService.create_or_update_charge(
            self.application,
            service=self.service,
            quantity=Decimal("1"),
            source_key="agreed-vat-included",
            user=self.manager_user,
        )

        self.assertEqual(charge.client_tariff_version_id, version.id)
        self.assertEqual(charge.vat_rate, "5")
        self.assertEqual(charge.vat_type_snapshot, ClientTariffVersion.VAT_WITH)
        self.assertEqual(charge.amount, Decimal("952.38"))
        self.assertEqual(charge.vat_amount, Decimal("47.62"))
        self.assertEqual(charge.total_amount, Decimal("1000.00"))

        updated = BillingWorkflowService.update_charge_quantity(
            charge,
            quantity=Decimal("2"),
            user=self.manager_user,
        )
        self.assertEqual(updated.amount, Decimal("1904.76"))
        self.assertEqual(updated.vat_amount, Decimal("95.24"))
        self.assertEqual(updated.total_amount, Decimal("2000.00"))

    def test_charge_uses_vat_extra_tariff_type(self):
        version = self._activate_version(
            valid_from=timezone.localdate(),
            price=Decimal("1000.0000"),
            vat_type=ClientTariffVersion.VAT_EXTRA,
        )
        self.company.vat_rate = "5"
        self.company.save(update_fields=["vat_rate"])

        charge = BillingWorkflowService.create_or_update_charge(
            self.application,
            service=self.service,
            quantity=Decimal("1"),
            source_key="agreed-vat-extra",
            user=self.manager_user,
        )

        self.assertEqual(charge.client_tariff_version_id, version.id)
        self.assertEqual(charge.vat_rate, "5")
        self.assertEqual(charge.vat_type_snapshot, ClientTariffVersion.VAT_EXTRA)
        self.assertEqual(charge.amount, Decimal("1000.00"))
        self.assertEqual(charge.vat_amount, Decimal("50.00"))
        self.assertEqual(charge.total_amount, Decimal("1050.00"))


class ClientTariffVersionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.manager_user = User.objects.create_user(username="tariff_manager", password="pass", is_staff=True)
        self.client_user = User.objects.create_user(username="tariff_client", password="pass")
        self.other_user = User.objects.create_user(username="tariff_other", password="pass")
        self.manager = Employee.objects.create(full_name="Tariff Manager", role="manager", user=self.manager_user)
        self.client_agency = Agency.objects.create(
            agn_name="Tariff Version Client",
            short_name="TVC",
            inn="7700000101",
            portal_user=self.client_user,
            mened_user_id=self.manager_user.id,
        )
        self.other_agency = Agency.objects.create(
            agn_name="Other Tariff Client",
            short_name="Other",
            inn="7700000102",
            portal_user=self.other_user,
        )
        self.company = OwnCompany.objects.create(name="FullBox Tariff", short_name="FBT", tax_mode=OwnCompany.TAX_MODE_VAT, vat_rate="20", is_default=True)
        self.contract = ClientBillingContract.objects.create(client=self.client_agency, own_company=self.company, pricing_mode=ClientBillingContract.PRICING_INDIVIDUAL)
        self.service, _ = BillingService.objects.get_or_create(
            code="profile_receiving",
            defaults={"name": "Приёмка профиля", "unit": "шт", "vat_rate": "20"},
        )
        self.category, _ = TariffCategory.objects.get_or_create(
            code="receiving", defaults={"name": "Приёмка товара", "sort_order": 10}
        )
        self.unit, _ = TariffUnit.objects.get_or_create(code="piece", defaults={"name": "Штука", "short_name": "шт."})
        self.version = ClientTariffVersion.objects.create(
            client=self.client_agency,
            contract=self.contract,
            name="Тарифы с 01.07.2026",
            version_number=1,
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=timezone.localdate(),
            manager=self.manager,
            created_by=self.manager_user,
        )
        self.item = ClientTariffItem.objects.create(
            tariff_version=self.version,
            category=self.category,
            service=self.service,
            service_name=self.service.name,
            unit=self.unit,
            price=Decimal("4.5000"),
            conditions="При наличии предварительной заявки",
        )
        self.version.status = ClientTariffVersion.STATUS_ACTIVE
        self.version.save()

    def test_client_sees_own_tariffs_in_profile(self):
        self.client.force_login(self.client_user)
        response = self.client.get(f"/client/{self.client_agency.id}/edit/?tab=tariffs")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Согласованные тарифы")
        self.assertContains(response, "Приёмка профиля")

    def test_client_does_not_see_other_company_tariffs(self):
        self.client.force_login(self.other_user)
        response = self.client.get(f"/client/{self.client_agency.id}/edit/?tab=tariffs")
        self.assertEqual(response.status_code, 403)

    def test_client_cannot_create_tariff_version(self):
        self.client.force_login(self.client_user)
        response = self.client.post(
            "/team-manager/billing/api/tariff-versions/",
            data=json.dumps({"client_id": self.client_agency.id, "valid_from": timezone.localdate().isoformat()}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_manager_cannot_create_tariff_draft(self):
        """Тарифы создаёт бухгалтер; менеджер только использует опубликованные цены."""
        self.client.force_login(self.manager_user)
        response = self.client.post(
            "/team-manager/billing/api/tariff-versions/",
            data=json.dumps({"client_id": self.client_agency.id, "name": "Черновик", "valid_from": timezone.localdate().isoformat()}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_accountant_can_create_tariff_draft(self):
        User = get_user_model()
        acc_user = User.objects.create_user(username="tariff_acc", password="pass")
        Employee.objects.create(full_name="Tariff Acc", role="accountant", user=acc_user)
        self.client.force_login(acc_user)
        response = self.client.post(
            "/team-manager/billing/api/tariff-versions/",
            data=json.dumps({"client_id": self.client_agency.id, "name": "Черновик", "valid_from": timezone.localdate().isoformat()}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["status"], ClientTariffVersion.STATUS_DRAFT)

    def test_copy_keeps_old_version_unchanged(self):
        copied = copy_tariff_version(self.version, user=self.manager_user)
        self.assertEqual(copied.status, ClientTariffVersion.STATUS_DRAFT)
        self.assertEqual(copied.items.count(), 1)
        self.version.refresh_from_db()
        self.assertEqual(self.version.status, ClientTariffVersion.STATUS_ACTIVE)

    def test_active_periods_cannot_overlap(self):
        with self.assertRaises(Exception):
            ClientTariffVersion.objects.create(
                client=self.client_agency,
                contract=self.contract,
                name="Пересечение",
                version_number=2,
                status=ClientTariffVersion.STATUS_ACTIVE,
                valid_from=timezone.localdate(),
            )

    def test_historical_tariff_selected_by_operation_date(self):
        result = get_company_tariff(self.client_agency, self.service, timezone.localdate())
        self.assertIsNotNone(result)
        self.assertEqual(result.price, Decimal("4.5000"))

    def test_negative_price_is_rejected(self):
        with self.assertRaises(Exception):
            ClientTariffItem.objects.create(
                tariff_version=self.version,
                category=self.category,
                service=self.service,
                service_name="Bad",
                unit=self.unit,
                price=Decimal("-1.0000"),
            )

    def test_print_version_contains_client_data(self):
        self.client.force_login(self.client_user)
        response = self.client.get(f"/client/api/v1/billing/tariff-versions/{self.version.id}/download/?client={self.client_agency.id}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.client_agency.agn_name)
        self.assertContains(response, "Приёмка профиля")


class BillingShippingPickReportTests(TestCase):
    def setUp(self):
        from shipping.models import ShippingOrder, ShippingOrderItem

        User = get_user_model()
        self.user = User.objects.create_superuser(
            username="shipping_pick_report_admin",
            password="pass",
            email="shipping-pick-report@example.com",
        )
        self.agency = Agency.objects.create(
            agn_name="Клиент отчета подбора",
            short_name="Клиент подбора",
            inn="7700000999",
        )
        self.order = ShippingOrder.objects.create(
            number="OTG-PICK-UNITS-001",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_SHIPPED,
            planned_ship_date=timezone.localdate(),
        )
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="PICK-SKU-1",
            name="Товар 1",
            qty_requested=120,
            qty_shipped=120,
        )
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="PICK-SKU-2",
            name="Товар 2",
            qty_requested=35,
            qty_shipped=35,
        )

    def test_report_shows_total_goods_units_for_order_and_selection(self):
        report_date = timezone.localdate().isoformat()
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("billing-shipping-pick-report"),
            {
                "date_from": report_date,
                "date_to": report_date,
                "show_empty": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        rows = response.context["shipping_pick_rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["goods_units"], 155)
        self.assertEqual(response.context["shipping_pick_totals"]["goods_units"], 155)
        self.assertEqual(response.context["shipping_pick_client_totals"][0]["goods_units"], 155)
        self.assertContains(response, "155 ед.", count=3)
