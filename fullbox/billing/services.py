from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .models import (
    ApplicationCharge,
    ApplicationChargeHistory,
    BillingAct,
    BillingActDispute,
    BillingActLine,
    BillingApplication,
    BillingAuditEvent,
    BillingSequence,
    BillingService,
    ClientInvoice,
    ClientInvoiceAct,
    ClientTariff,
    ClientTariffItem,
    ClientTariffVersion,
    InvoicePayment,
)


from .statuses import ActStatus, BillingStatus, DocumentReviewStatus, InvoiceStatus, PaymentStatus, SequenceKind


class ChargeVersionConflict(ValidationError):
    """Optimistic lock conflict — HTTP 409 на уровне API."""

    status_code = 409


MONEY = Decimal("0.01")
QTY = Decimal("0.001")


def quantize_money(value) -> Decimal:
    return Decimal(value or "0").quantize(MONEY, rounding=ROUND_HALF_UP)


def quantize_qty(value) -> Decimal:
    return Decimal(value or "0").quantize(QTY, rounding=ROUND_HALF_UP)


def parse_vat_rate(vat_rate: str | int | Decimal | None) -> Decimal:
    raw = str(vat_rate or "").strip().replace("%", "").replace(",", ".")
    if raw.lower() in {"", "0", "none", "без ндс", "безндс"}:
        return Decimal("0")
    return Decimal(raw)


def normalize_vat_type(vat_type=None, vat_rate=None) -> str:
    raw = str(vat_type or "").strip().lower()
    if raw in {"0", "none", "без ндс", "безндс", "no_vat", ClientTariffVersion.VAT_NO}:
        return ClientTariffVersion.VAT_NO
    if raw in {"vat", "with_vat", "included", "vat_included", "с ндс", ClientTariffVersion.VAT_WITH}:
        return ClientTariffVersion.VAT_WITH
    if raw in {"vat_extra", "extra", "top", "additional", "сверху", ClientTariffVersion.VAT_EXTRA}:
        return ClientTariffVersion.VAT_EXTRA
    if parse_vat_rate(vat_rate) == 0:
        return ClientTariffVersion.VAT_NO
    # Старые начисления без явного формата исторически считались как НДС сверху.
    return ClientTariffVersion.VAT_EXTRA


def calculate_amounts(quantity, tariff, vat_rate, *, vat_type=None):
    quantity = quantize_qty(quantity)
    tariff = Decimal(tariff or "0").quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    line_value = quantize_money(quantity * tariff)
    rate = parse_vat_rate(vat_rate)
    normalized_vat_type = normalize_vat_type(vat_type, rate)
    if rate == 0 or normalized_vat_type == ClientTariffVersion.VAT_NO:
        amount = line_value
        vat_amount = Decimal("0.00")
        total_amount = line_value
    elif normalized_vat_type == ClientTariffVersion.VAT_WITH:
        total_amount = line_value
        divisor = Decimal("1") + (rate / Decimal("100"))
        amount = quantize_money(total_amount / divisor)
        vat_amount = quantize_money(total_amount - amount)
    else:
        amount = line_value
        vat_amount = quantize_money(amount * rate / Decimal("100"))
        total_amount = quantize_money(amount + vat_amount)
    return amount, vat_amount, total_amount


class BillingWorkflowService:
    @staticmethod
    def _invoice_supplier_snapshot(acts) -> tuple[dict, str]:
        """Зафиксировать поставщика и ставку НДС на момент создания счёта."""
        own_company = None
        vat_rates: set[str] = set()
        for act in acts:
            for line in act.lines.select_related("charge__own_company").all():
                if line.charge_id:
                    if own_company is None and line.charge.own_company_id:
                        own_company = line.charge.own_company
                    rate = str(line.charge.vat_rate or "").rstrip("%")
                    if rate:
                        vat_rates.add(rate)
        if own_company is None:
            app = acts[0].application
            own_company = app.own_company
            if own_company is None:
                contract = app.client.billing_contracts.filter(is_active=True).select_related("own_company").order_by("-valid_from", "-id").first()
                own_company = contract.own_company if contract else None
        if own_company is None:
            return {}, next(iter(vat_rates)) if len(vat_rates) == 1 else ""
        fields = (
            "name", "short_name", "inn", "kpp", "ogrn", "address", "postal_address", "phone", "email",
            "director_name", "director_basis", "bank_name", "bank_bik", "settlement_account",
            "correspondent_account", "bank_address", "tax_mode", "vat_rate",
        )
        snapshot = {field: str(getattr(own_company, field, "") or "") for field in fields}
        vat_rate = next(iter(vat_rates)) if len(vat_rates) == 1 else snapshot.get("vat_rate", "")
        return snapshot, vat_rate

    @staticmethod
    def _locked_invoice_statuses() -> set[str]:
        return {InvoiceStatus.SENT, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.PAID}

    @staticmethod
    def _active_invoices_for_charge(charge: ApplicationCharge):
        return (
            ClientInvoice.objects.filter(
                Q(act__lines__charge=charge) | Q(invoice_acts__act__lines__charge=charge)
            )
            .exclude(status=InvoiceStatus.CANCELLED)
            .distinct()
        )

    @staticmethod
    def charge_locked_by_sent_invoice(charge: ApplicationCharge) -> bool:
        """True если начисление уже попало в отправленный/оплаченный счёт."""
        return (
            BillingWorkflowService._active_invoices_for_charge(charge)
            .filter(
                Q(status__in=BillingWorkflowService._locked_invoice_statuses())
                | Q(sent_at__isnull=False)
                | Q(paid_amount__gt=0)
            )
            .exists()
        )

    @staticmethod
    def _sync_charge_document_snapshots(charge: ApplicationCharge) -> None:
        """
        Обновить строки акта/неотправленного счёта после пересчёта начисления.

        Используется только для счетов, которые ещё не отправлены клиенту. Отправленные
        и оплаченные документы остаются финансовым фактом и не меняются.
        """
        lines = list(BillingActLine.objects.select_related("act").filter(charge=charge))
        if not lines:
            return
        act_ids = set()
        for line in lines:
            line.service_name = charge.service_name_snapshot or charge.service.name
            line.quantity = charge.quantity
            line.unit = charge.unit
            line.tariff = charge.tariff
            line.amount = charge.amount
            line.vat_amount = charge.vat_amount
            line.total_amount = charge.total_amount
            line.save(
                update_fields=[
                    "service_name",
                    "quantity",
                    "unit",
                    "tariff",
                    "amount",
                    "vat_amount",
                    "total_amount",
                ]
            )
            act_ids.add(line.act_id)
        for act in BillingAct.objects.filter(pk__in=act_ids):
            totals = act.lines.aggregate(
                subtotal=Sum("amount"),
                vat=Sum("vat_amount"),
                total=Sum("total_amount"),
            )
            act.subtotal = quantize_money(totals.get("subtotal") or 0)
            act.vat_amount = quantize_money(totals.get("vat") or 0)
            act.total_amount = quantize_money(totals.get("total") or 0)
            if act.status == ActStatus.CONFIRMED:
                act.confirmed_amount = act.total_amount
                act.save(update_fields=["subtotal", "vat_amount", "total_amount", "confirmed_amount", "updated_at"])
            else:
                act.save(update_fields=["subtotal", "vat_amount", "total_amount", "updated_at"])

        invoices = (
            ClientInvoice.objects.filter(Q(act_id__in=act_ids) | Q(invoice_acts__act_id__in=act_ids))
            .exclude(status=InvoiceStatus.CANCELLED)
            .distinct()
        )
        for invoice in invoices:
            if (
                invoice.status in BillingWorkflowService._locked_invoice_statuses()
                or invoice.sent_at
                or invoice.paid_amount > 0
            ):
                continue
            linked = list(invoice.get_linked_acts())
            if not linked and invoice.act_id:
                linked = [invoice.act]
            invoice.subtotal = quantize_money(sum((act.subtotal for act in linked), Decimal("0")))
            invoice.vat_amount = quantize_money(sum((act.vat_amount for act in linked), Decimal("0")))
            invoice.total_amount = quantize_money(sum((act.total_amount for act in linked), Decimal("0")))
            invoice.debt_amount = quantize_money(max(invoice.total_amount - invoice.paid_amount, Decimal("0")))
            invoice.save(update_fields=["subtotal", "vat_amount", "total_amount", "debt_amount", "updated_at"])
            BillingWorkflowService._sync_applications_for_invoice(invoice)

    @staticmethod
    def _create_tariff_price_revision(
        *,
        charge: ApplicationCharge,
        price: Decimal,
        valid_from,
        user,
        reason: str,
        invoice: ClientInvoice,
    ) -> ClientTariffVersion:
        from .tariff_services import active_tariff_version, copy_tariff_version

        today = timezone.localdate()
        if valid_from <= today:
            raise ValidationError("Новую цену тарифа можно применять не раньше следующего дня.")
        if not charge.client_tariff_item_id:
            if charge.client_logistics_tariff_item_id:
                raise ValidationError(
                    "Для логистической строки цену счёта можно изменить, "
                    "но логистический тариф редактируется отдельно."
                )
            raise ValidationError("У начисления нет позиции тарифа клиента для обновления.")

        locked_versions = list(
            ClientTariffVersion.objects.select_for_update()
            .filter(client=charge.client)
            .order_by("valid_from", "version_number", "id")
        )
        source = active_tariff_version(charge.client, on_date=valid_from)
        if source is None:
            source = active_tariff_version(charge.client, on_date=today) or charge.client_tariff_version
        if source is None:
            raise ValidationError("Не найдена действующая версия тарифа клиента.")
        source = ClientTariffVersion.objects.get(pk=source.pk)
        source_item = ClientTariffItem.objects.get(pk=charge.client_tariff_item_id)

        target = copy_tariff_version(source, valid_from=valid_from, user=user)
        target.name = f"Тарифы с {valid_from:%d.%m.%Y} · счёт {invoice.number}"
        note = f"Цена изменена из счёта {invoice.number}. Причина: {reason}"
        target.general_comment = "\n".join(filter(None, [target.general_comment.strip(), note]))
        target.save(update_fields=["name", "general_comment", "updated_at"])

        target_item = (
            target.items.filter(service_id=charge.service_id, conditions=source_item.conditions).order_by("id").first()
            or target.items.filter(service_id=charge.service_id).order_by("id").first()
        )
        if target_item is None:
            raise ValidationError("В новой версии тарифа не найдена эта услуга.")
        old_tariff_price = target_item.price
        target_item.price = price
        target_item.save(update_fields=["price", "updated_at"])

        for previous in locked_versions:
            if previous.pk == target.pk or previous.status not in {
                ClientTariffVersion.STATUS_ACTIVE,
                ClientTariffVersion.STATUS_SCHEDULED,
            }:
                continue
            if previous.valid_from >= valid_from:
                previous.status = ClientTariffVersion.STATUS_ARCHIVED
                previous.save(update_fields=["status", "updated_at"])
            elif previous.valid_to is None or previous.valid_to >= valid_from:
                previous.status = ClientTariffVersion.STATUS_EXPIRED
                previous.valid_to = valid_from - timedelta(days=1)
                previous.save(update_fields=["status", "valid_to", "updated_at"])

        target.status = ClientTariffVersion.STATUS_SCHEDULED
        target.approved_by = user if getattr(user, "is_authenticated", False) else None
        target.approved_at = timezone.now()
        target.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
        BillingWorkflowService.audit(
            action="invoice_price_tariff_revision_created",
            application=charge.application,
            user=user,
            obj=target,
            old_value={"price": str(old_tariff_price), "source_version_id": source.id},
            new_value={
                "price": str(target_item.price),
                "valid_from": str(valid_from),
                "invoice_id": invoice.id,
            },
            comment=reason,
        )
        return target

    @staticmethod
    @transaction.atomic
    def update_draft_invoice_charge_price(
        invoice: ClientInvoice,
        charge: ApplicationCharge,
        *,
        tariff,
        reason: str,
        user,
        update_client_tariff: bool = False,
        tariff_valid_from=None,
    ) -> tuple[ApplicationCharge, ClientInvoice, ClientTariffVersion | None]:
        invoice = ClientInvoice.objects.select_for_update().get(pk=invoice.pk)
        charge = ApplicationCharge.objects.select_for_update().select_related("service").get(pk=charge.pk)
        if invoice.status != InvoiceStatus.DRAFT or invoice.sent_at or invoice.paid_amount > 0:
            raise ValidationError("Редактировать цены можно только в неотправленном черновике счёта.")
        if invoice.review_status not in {DocumentReviewStatus.LOCAL, DocumentReviewStatus.RETURNED}:
            raise ValidationError("Счёт уже передан бухгалтеру. Верните его менеджеру перед изменением цены.")

        act_ids = list(invoice.get_linked_acts().values_list("id", flat=True))
        acts = list(BillingAct.objects.select_for_update().filter(pk__in=act_ids))
        if not acts or any(
            act.status != ActStatus.DRAFT
            or act.sent_at
            or act.review_status not in {DocumentReviewStatus.LOCAL, DocumentReviewStatus.RETURNED}
            for act in acts
        ):
            raise ValidationError("Связанный акт уже отправлен или подтверждён; цену в нём менять нельзя.")
        if not BillingActLine.objects.select_for_update().filter(act_id__in=act_ids, charge=charge).exists():
            raise ValidationError("Строка начисления не относится к этому счёту.")
        if charge.is_excluded:
            raise ValidationError("Нельзя менять цену исключённого начисления.")

        reason = str(reason or "").strip()
        if not reason:
            raise ValidationError("Укажите причину изменения цены.")
        try:
            new_tariff = Decimal(str(tariff).replace(",", ".")).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
        except Exception as exc:
            raise ValidationError("Укажите корректную цену.") from exc
        if new_tariff < 0:
            raise ValidationError("Цена не может быть отрицательной.")
        if new_tariff == charge.tariff:
            raise ValidationError("Новая цена совпадает с текущей.")

        old_tariff = charge.tariff
        old_total = charge.total_amount
        amount, vat_amount, total_amount = calculate_amounts(
            charge.quantity,
            new_tariff,
            charge.vat_rate,
            vat_type=charge.vat_type_snapshot,
        )
        charge.tariff = new_tariff
        charge.amount = amount
        charge.vat_amount = vat_amount
        charge.total_amount = total_amount
        charge.is_manual_override = True
        charge.override_reason = reason
        charge.overridden_by = user
        charge.overridden_at = timezone.now()
        charge.price_source = "manual_override"
        charge.tariff_source_label = f"Ручная цена в счёте {invoice.number}"
        charge.edit_version += 1
        charge.save(
            update_fields=[
                "tariff",
                "amount",
                "vat_amount",
                "total_amount",
                "is_manual_override",
                "override_reason",
                "overridden_by",
                "overridden_at",
                "price_source",
                "tariff_source_label",
                "edit_version",
                "updated_at",
            ]
        )
        ApplicationChargeHistory.objects.create(
            charge=charge,
            application=charge.application,
            change_type=ApplicationChargeHistory.CHANGE_TARIFF,
            old_tariff=old_tariff,
            new_tariff=new_tariff,
            old_total=old_total,
            new_total=total_amount,
            reason="invoice_price_edit",
            comment=reason,
            user=user,
        )
        BillingWorkflowService._sync_charge_document_snapshots(charge)
        BillingWorkflowService.recalculate_application_totals(charge.application)

        tariff_version = None
        if update_client_tariff:
            tariff_version = BillingWorkflowService._create_tariff_price_revision(
                charge=charge,
                price=new_tariff,
                valid_from=tariff_valid_from,
                user=user,
                reason=reason,
                invoice=invoice,
            )

        BillingWorkflowService.audit(
            action="invoice_charge_price_overridden",
            application=charge.application,
            user=user,
            obj=charge,
            old_value={"tariff": str(old_tariff), "total": str(old_total)},
            new_value={
                "tariff": str(new_tariff),
                "total": str(total_amount),
                "invoice_id": invoice.id,
                "tariff_version_id": getattr(tariff_version, "id", None),
            },
            comment=reason,
        )
        invoice.refresh_from_db()
        return charge, invoice, tariff_version

    @staticmethod
    def _lock_editable_draft_invoice_charge(
        invoice: ClientInvoice,
        charge: ApplicationCharge,
        *,
        expected_version=None,
    ) -> tuple[ClientInvoice, ApplicationCharge, list[BillingAct], list[BillingActLine]]:
        invoice = ClientInvoice.objects.select_for_update().get(pk=invoice.pk)
        charge = ApplicationCharge.objects.select_for_update().select_related("service").get(pk=charge.pk)
        if invoice.status != InvoiceStatus.DRAFT or invoice.sent_at or invoice.paid_amount > 0:
            raise ValidationError("Изменять строки можно только в неотправленном черновике счёта.")
        if invoice.review_status not in {DocumentReviewStatus.LOCAL, DocumentReviewStatus.RETURNED}:
            raise ValidationError("Счёт уже передан бухгалтеру. Верните его на доработку перед изменением строк.")

        act_ids = list(invoice.get_linked_acts().values_list("id", flat=True))
        acts = list(BillingAct.objects.select_for_update().filter(pk__in=act_ids).order_by("id"))
        if not acts or any(
            act.status != ActStatus.DRAFT
            or act.sent_at
            or act.review_status not in {DocumentReviewStatus.LOCAL, DocumentReviewStatus.RETURNED}
            for act in acts
        ):
            raise ValidationError("Связанный акт уже отправлен или подтверждён; его строки менять нельзя.")

        lines = list(
            BillingActLine.objects.select_for_update()
            .filter(act_id__in=act_ids, charge=charge)
            .order_by("id")
        )
        if not lines:
            raise ValidationError("Строка начисления не относится к этому счёту.")
        other_invoices = (
            BillingWorkflowService._active_invoices_for_charge(charge)
            .exclude(pk=invoice.pk)
            .exclude(status=InvoiceStatus.CANCELLED)
        )
        if other_invoices.exists():
            raise ValidationError("Начисление связано с другим действующим счётом; автоматическое изменение запрещено.")
        BillingWorkflowService.assert_charge_version(charge, expected_version)
        if charge.is_excluded:
            raise ValidationError("Начисление уже исключено.")
        return invoice, charge, acts, lines

    @staticmethod
    @transaction.atomic
    def update_draft_invoice_charge_quantity(
        invoice: ClientInvoice,
        charge: ApplicationCharge,
        *,
        quantity,
        reason: str,
        user,
        expected_version=None,
    ) -> tuple[ApplicationCharge, ClientInvoice]:
        invoice, charge, _acts, _lines = BillingWorkflowService._lock_editable_draft_invoice_charge(
            invoice,
            charge,
            expected_version=expected_version,
        )
        if charge.source_type != ApplicationCharge.SOURCE_STORAGE_DAY or charge.service.code != "storage_m3_day":
            raise ValidationError("Объём можно корректировать только у начисления хранения в м³/сутки.")

        reason = str(reason or "").strip()
        if not reason:
            raise ValidationError("Укажите причину изменения объёма.")
        try:
            new_quantity = quantize_qty(str(quantity).strip().replace(",", "."))
        except Exception as exc:
            raise ValidationError("Укажите корректный объём.") from exc
        if new_quantity <= 0:
            raise ValidationError("Объём должен быть больше нуля.")
        if new_quantity == charge.quantity:
            raise ValidationError("Новый объём совпадает с текущим.")

        old_quantity = charge.quantity
        old_total = charge.total_amount
        amount, vat_amount, total_amount = calculate_amounts(
            new_quantity,
            charge.tariff,
            charge.vat_rate,
            vat_type=charge.vat_type_snapshot,
        )
        if charge.original_quantity is None:
            charge.original_quantity = old_quantity
        charge.quantity = new_quantity
        charge.amount = amount
        charge.vat_amount = vat_amount
        charge.total_amount = total_amount
        charge.qty_change_basis = ApplicationCharge.QTY_BASIS_MANUAL
        charge.qty_change_comment = reason
        charge.edit_version = int(charge.edit_version or 1) + 1
        charge.save(
            update_fields=[
                "quantity",
                "amount",
                "vat_amount",
                "total_amount",
                "original_quantity",
                "qty_change_basis",
                "qty_change_comment",
                "edit_version",
                "updated_at",
            ]
        )
        BillingWorkflowService.record_charge_history(
            charge,
            change_type=ApplicationChargeHistory.CHANGE_QTY,
            user=user,
            old_quantity=old_quantity,
            new_quantity=new_quantity,
            old_total=old_total,
            new_total=total_amount,
            reason="invoice_quantity_edit",
            comment=reason,
        )
        BillingWorkflowService._sync_charge_document_snapshots(charge)
        BillingWorkflowService.recalculate_application_totals(charge.application)
        BillingWorkflowService.audit(
            action="invoice_charge_quantity_overridden",
            application=charge.application,
            user=user,
            obj=charge,
            old_value={"quantity": str(old_quantity), "total": str(old_total)},
            new_value={
                "quantity": str(new_quantity),
                "total": str(total_amount),
                "invoice_id": invoice.id,
            },
            comment=reason,
        )
        invoice.refresh_from_db()
        return charge, invoice

    @staticmethod
    @transaction.atomic
    def exclude_draft_invoice_charge(
        invoice: ClientInvoice,
        charge: ApplicationCharge,
        *,
        reason: str,
        comment: str,
        user,
        expected_version=None,
    ) -> tuple[ApplicationCharge, ClientInvoice]:
        invoice, charge, acts, lines = BillingWorkflowService._lock_editable_draft_invoice_charge(
            invoice,
            charge,
            expected_version=expected_version,
        )
        valid_reasons = {choice[0] for choice in ApplicationCharge.EXCLUDE_REASON_CHOICES}
        if reason not in valid_reasons:
            raise ValidationError("Выберите причину исключения строки.")
        comment = str(comment or "").strip()
        if not comment:
            raise ValidationError("Опишите причину исключения строки.")

        act_ids = [act.id for act in acts]
        other_invoice_for_act = (
            ClientInvoice.objects.filter(Q(act_id__in=act_ids) | Q(invoice_acts__act_id__in=act_ids))
            .exclude(pk=invoice.pk)
            .exclude(status=InvoiceStatus.CANCELLED)
            .exists()
        )
        if other_invoice_for_act:
            raise ValidationError("Связанный акт входит в другой действующий счёт; автоматическое исключение запрещено.")

        old_total = charge.total_amount
        removed_line_ids = [line.id for line in lines]
        BillingActLine.objects.filter(pk__in=removed_line_ids).delete()
        charge.is_excluded = True
        charge.excluded_at = timezone.now()
        charge.excluded_by = user if getattr(user, "is_authenticated", False) else None
        charge.exclude_reason = reason
        charge.exclude_comment = comment
        charge.is_confirmed = False
        charge.is_included_in_act = False
        charge.is_included_in_invoice = False
        charge.edit_version = int(charge.edit_version or 1) + 1
        charge.save(
            update_fields=[
                "is_excluded",
                "excluded_at",
                "excluded_by",
                "exclude_reason",
                "exclude_comment",
                "is_confirmed",
                "is_included_in_act",
                "is_included_in_invoice",
                "edit_version",
                "updated_at",
            ]
        )

        for act in acts:
            totals = BillingActLine.objects.filter(act=act).aggregate(
                subtotal=Sum("amount"),
                vat=Sum("vat_amount"),
                total=Sum("total_amount"),
            )
            act.subtotal = quantize_money(totals.get("subtotal") or 0)
            act.vat_amount = quantize_money(totals.get("vat") or 0)
            act.total_amount = quantize_money(totals.get("total") or 0)
            act.save(update_fields=["subtotal", "vat_amount", "total_amount", "updated_at"])

        invoice.subtotal = quantize_money(sum((act.subtotal for act in acts), Decimal("0")))
        invoice.vat_amount = quantize_money(sum((act.vat_amount for act in acts), Decimal("0")))
        invoice.total_amount = quantize_money(sum((act.total_amount for act in acts), Decimal("0")))
        invoice.debt_amount = quantize_money(max(invoice.total_amount - invoice.paid_amount, Decimal("0")))
        invoice.save(update_fields=["subtotal", "vat_amount", "total_amount", "debt_amount", "updated_at"])
        BillingWorkflowService._sync_applications_for_invoice(invoice)
        BillingWorkflowService.recalculate_application_totals(charge.application)
        BillingWorkflowService.record_charge_history(
            charge,
            change_type=ApplicationChargeHistory.CHANGE_EXCLUDE,
            user=user,
            old_total=old_total,
            new_total=Decimal("0"),
            reason=reason,
            comment=comment,
        )
        BillingWorkflowService.audit(
            action="invoice_charge_excluded",
            application=charge.application,
            user=user,
            obj=charge,
            old_value={
                "total": str(old_total),
                "invoice_id": invoice.id,
                "act_line_ids": removed_line_ids,
            },
            new_value={"excluded": True, "invoice_total": str(invoice.total_amount)},
            comment=comment,
        )
        return charge, invoice

    @staticmethod
    def audit(*, action, application=None, user=None, obj=None, old_value=None, new_value=None, comment="", request=None):
        ip_address = None
        user_agent = ""
        if request is not None:
            ip_address = request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", "")).split(",")[0] or None
            user_agent = request.META.get("HTTP_USER_AGENT", "")
        return BillingAuditEvent.objects.create(
            application=application,
            user=user,
            action=action,
            object_type=obj.__class__.__name__ if obj is not None else "",
            object_id=str(getattr(obj, "pk", "") or ""),
            old_value=old_value,
            new_value=new_value,
            comment=comment,
            ip_address=ip_address,
            user_agent=user_agent,
        )

    @staticmethod
    @transaction.atomic
    def next_number(kind: str, *, date=None) -> str:
        date = date or timezone.localdate()
        sequence, _created = BillingSequence.objects.select_for_update().get_or_create(
            kind=kind,
            year=date.year,
            defaults={"last_number": 0},
        )
        sequence.last_number += 1
        sequence.save(update_fields=["last_number", "updated_at"])
        prefix = "ACT" if kind == SequenceKind.ACT else "INV"
        return f"{prefix}-{date.year}-{sequence.last_number:06d}"

    @staticmethod
    @transaction.atomic
    def sync_application_from_source(
        *,
        application_type: str,
        application_id: str,
        client,
        legal_entity=None,
        own_company=None,
        manager=None,
        marketplace=None,
        warehouse=None,
        warehouse_label="",
        operational_status="",
        operational_status_label="",
        created_at_source=None,
        source_payload=None,
        user=None,
    ) -> BillingApplication:
        legal_entity = legal_entity or client
        payload = dict(source_payload or {})
        # Keep the manager-specified shipment date in the billing snapshot.
        # It is an informational business date only: no existing charge,
        # tariff, invoice or warehouse process is recalculated or changed.
        if application_type == BillingApplication.TYPE_SHIPPING:
            try:
                from shipping.models import ShippingOrder

                shipping_dates = (
                    ShippingOrder.objects.filter(number=str(application_id), agency=client)
                    .values("planned_ship_date", "shipped_at")
                    .first()
                )
                if shipping_dates and shipping_dates["planned_ship_date"]:
                    payload["shipping_planned_date"] = shipping_dates["planned_ship_date"].isoformat()
                    payload["shipping_planned_date_source"] = "shipping_order.planned_ship_date"
                if shipping_dates and shipping_dates["shipped_at"]:
                    payload["shipping_date"] = shipping_dates["shipped_at"].isoformat()
                    payload["shipping_date_source"] = "shipping_order.shipped_at"
            except Exception:
                # Billing synchronisation must not fail just because a legacy
                # shipment is no longer available in its source contour.
                pass
        application, created = BillingApplication.objects.update_or_create(
            application_type=application_type,
            application_id=str(application_id),
            client=client,
            defaults={
                "legal_entity": legal_entity,
                "own_company": own_company,
                "manager": manager,
                "marketplace": marketplace,
                "warehouse": warehouse,
                "warehouse_label": warehouse_label or "",
                "operational_status": operational_status or "",
                "operational_status_label": operational_status_label or "",
                "created_at_source": created_at_source,
                "source_payload": payload,
            },
        )
        BillingWorkflowService.audit(
            action="application_synced",
            application=application,
            user=user,
            obj=application,
            comment="Создана заявка биллинга" if created else "Обновлена заявка биллинга",
        )
        return application

    @staticmethod
    @transaction.atomic
    def mark_operations_completed(application: BillingApplication, *, completed_at=None, user=None):
        old = {"billing_status": application.billing_status, "is_operations_completed": application.is_operations_completed}
        application.is_operations_completed = True
        application.operations_completed_at = completed_at or timezone.now()
        if application.billing_status == BillingStatus.NOT_CALCULATED:
            application.billing_status = BillingStatus.CALCULATION_DRAFT
        application.save(update_fields=["is_operations_completed", "operations_completed_at", "billing_status", "updated_at"])
        BillingWorkflowService.audit(
            action="operations_completed",
            application=application,
            user=user,
            obj=application,
            old_value=old,
            new_value={"billing_status": application.billing_status, "is_operations_completed": True},
        )
        return application

    @staticmethod
    def find_tariff(application: BillingApplication, service: BillingService, *, performed_at=None):
        on_date = (performed_at or timezone.now()).date()
        return (
            ClientTariff.objects.filter(
                client=application.client,
                legal_entity=application.legal_entity,
                service=service,
                valid_from__lte=on_date,
                is_active=True,
            )
            .filter(models_valid_to_filter(on_date))
            .order_by("-valid_from", "-id")
            .first()
        )

    @staticmethod
    @transaction.atomic
    def create_or_update_charge(
        application: BillingApplication,
        *,
        service: BillingService,
        quantity,
        tariff=None,
        unit=None,
        vat_rate=None,
        vat_type=None,
        source_type=ApplicationCharge.SOURCE_MANUAL,
        source_id="",
        source_key="",
        performed_at=None,
        billing_period=None,
        operation_type="",
        operation_id="",
        comment="",
        user=None,
        client_tariff_version=None,
        client_tariff_item=None,
        client_logistics_tariff=None,
        client_logistics_tariff_item=None,
        tariff_price=None,
        coefficient=None,
        minimum_amount=None,
        tariff_source_label="",
        tariff_basis="",
        service_name_snapshot="",
        is_manual_override=False,
        override_reason="",
        overridden_by=None,
        resolve_from_agreed_tariff: bool = True,
    ) -> ApplicationCharge:
        from .price_resolver import resolve_client_service_price

        performed_at = performed_at or timezone.now()
        billing_period = billing_period or performed_at.date().replace(day=1)

        # Цена только из опубликованного тарифа клиента (ClientTariffVersion) либо ручной override.
        # Каталог / BillingService.default_price / стандартный прайс — не источник цены начисления.
        if (
            resolve_from_agreed_tariff
            and not is_manual_override
            and client_tariff_version is None
            and client_logistics_tariff is None
        ):
            resolved = resolve_client_service_price(
                application, service, performed_at=performed_at, quantity=quantity
            )
            if not resolved.ok or resolved.tariff is None or (
                resolved.tariff_version is None and resolved.logistics_tariff is None
            ):
                raise ValidationError(
                    resolved.note
                    or f'Для услуги «{service.name}» не найден согласованный тариф клиента на дату оказания услуги.'
                )
            tariff = resolved.tariff
            unit = unit or resolved.unit or service.unit
            vat_rate = vat_rate if vat_rate is not None else resolved.vat_rate
            vat_type = vat_type or resolved.vat_type
            client_tariff_version = resolved.tariff_version
            client_tariff_item = resolved.tariff_item
            client_logistics_tariff = resolved.logistics_tariff
            client_logistics_tariff_item = resolved.logistics_tariff_item
            tariff_price = resolved.tariff_price
            coefficient = resolved.coefficient
            minimum_amount = resolved.minimum_amount
            tariff_source_label = resolved.tariff_source_label
            tariff_basis = resolved.tariff_basis
            service_name_snapshot = resolved.service_name or service.name
        else:
            unit = unit or service.unit
            if tariff is None:
                raise ValidationError("Не указана цена начисления.")
            vat_rate = vat_rate if vat_rate is not None else service.vat_rate
            if not vat_type and client_tariff_version is not None:
                vat_type = getattr(client_tariff_version, "vat_type", "") or None
            if tariff_price is None:
                tariff_price = tariff
            if not service_name_snapshot:
                service_name_snapshot = service.name

        if not is_manual_override and client_tariff_version is None and client_logistics_tariff is None:
            raise ValidationError(
                f'Для услуги «{service.name}» нельзя сохранить цену без опубликованного тарифа клиента. '
                "Сначала бухгалтер публикует тарификацию."
            )

        vat_type = normalize_vat_type(vat_type, vat_rate)
        if vat_type == ClientTariffVersion.VAT_NO:
            vat_rate = "0"
        amount, vat_amount, total_amount = calculate_amounts(quantity, tariff, vat_rate, vat_type=vat_type)
        defaults = {
            "client": application.client,
            "legal_entity": application.legal_entity,
            "service": service,
            "operation_type": operation_type or "",
            "operation_id": str(operation_id or ""),
            "quantity": quantize_qty(quantity),
            "unit": unit,
            "tariff": Decimal(tariff or "0").quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
            "tariff_price": (
                Decimal(tariff_price).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) if tariff_price is not None else None
            ),
            "coefficient": Decimal(coefficient or "1").quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
            "minimum_amount": (
                Decimal(minimum_amount).quantize(MONEY, rounding=ROUND_HALF_UP) if minimum_amount is not None else None
            ),
            "amount": amount,
            "vat_rate": str(vat_rate or ""),
            "vat_amount": vat_amount,
            "total_amount": total_amount,
            "vat_type_snapshot": vat_type,
            "client_tariff_version": client_tariff_version,
            "client_tariff_item": client_tariff_item,
            "client_logistics_tariff": client_logistics_tariff,
            "client_logistics_tariff_item": client_logistics_tariff_item,
            "service_name_snapshot": service_name_snapshot or "",
            "tariff_source_label": tariff_source_label or "",
            "tariff_basis": tariff_basis or "",
            "price_source": (
                "manual_override"
                if is_manual_override
                else ("client_logistics_tariff" if client_logistics_tariff is not None else "client_tariff")
            ),
            "is_manual_override": bool(is_manual_override),
            "override_reason": override_reason or "",
            "overridden_by": overridden_by,
            "overridden_at": timezone.now() if is_manual_override else None,
            "source_type": source_type,
            "source_id": str(source_id or ""),
            "performed_at": performed_at,
            "billing_period": billing_period,
            "comment": comment or "",
            "created_by": user,
        }
        # Компания обслуживания / НДС из lifecycle или договора
        try:
            lifecycle = getattr(application.client, "lifecycle", None)
            serving = getattr(lifecycle, "serving_company", None) if lifecycle else None
            if serving is None:
                contract = (
                    application.client.billing_contracts.filter(is_active=True)
                    .select_related("own_company")
                    .order_by("-valid_from", "-id")
                    .first()
                )
                serving = contract.own_company if contract else None
            if serving is not None:
                defaults["own_company"] = serving
                if vat_rate is None or str(vat_rate) in {"", "20"}:
                    if getattr(serving, "tax_mode", "") == "no_vat":
                        vat_type = ClientTariffVersion.VAT_NO
                        defaults["vat_rate"] = "0"
                        defaults["vat_type_snapshot"] = vat_type
                        amount, vat_amount, total_amount = calculate_amounts(
                            quantity,
                            tariff,
                            "0",
                            vat_type=vat_type,
                        )
                        defaults["amount"] = amount
                        defaults["vat_amount"] = vat_amount
                        defaults["total_amount"] = total_amount
                    elif getattr(serving, "vat_rate", None):
                        defaults["vat_rate"] = str(serving.vat_rate)
                        amount, vat_amount, total_amount = calculate_amounts(
                            quantity,
                            tariff,
                            serving.vat_rate,
                            vat_type=vat_type,
                        )
                        defaults["amount"] = amount
                        defaults["vat_amount"] = vat_amount
                        defaults["total_amount"] = total_amount
        except Exception:
            pass
        # Изменение количества снимает подтверждение объёма менеджером
        sync_document_snapshots = False
        if source_key:
            existing = ApplicationCharge.objects.filter(application=application, source_key=source_key).first()
            # Начисления в отправленном/оплаченном счёте становятся неизменяемым финансовым фактом.
            # Если счёт ещё не отправлен, тариф можно пересчитать и синхронизировать черновой документ.
            if existing and existing.is_included_in_invoice:
                if BillingWorkflowService.charge_locked_by_sent_invoice(existing):
                    return existing
                sync_document_snapshots = True
            elif existing and existing.is_included_in_act:
                return existing
            # Исключённые строки не трогаем при автопересчёте — иначе «воскресают»
            if existing and existing.is_excluded:
                return existing
            if existing and existing.quantity != defaults["quantity"]:
                defaults["is_confirmed"] = False
                if existing.original_quantity is None:
                    defaults["original_quantity"] = existing.quantity
            charge, created = ApplicationCharge.objects.update_or_create(
                application=application,
                source_key=source_key,
                defaults=defaults,
            )
        else:
            defaults.setdefault("is_confirmed", False)
            charge = ApplicationCharge.objects.create(application=application, **defaults)
            created = True
        if sync_document_snapshots:
            BillingWorkflowService._sync_charge_document_snapshots(charge)
        BillingWorkflowService.recalculate_application_totals(application)
        BillingWorkflowService.audit(
            action="charge_created" if created else "charge_updated",
            application=application,
            user=user,
            obj=charge,
            new_value={
                "total_amount": str(charge.total_amount),
                "tariff": str(charge.tariff),
                "tariff_source_label": charge.tariff_source_label,
                "is_manual_override": charge.is_manual_override,
                "is_confirmed": charge.is_confirmed,
            },
        )
        return charge

    @staticmethod
    @transaction.atomic
    def cancel_draft_act(act: BillingAct, *, user=None) -> BillingAct:
        """Отмена черновика акта: строки освобождаются для правки менеджером."""
        if act.status != ActStatus.DRAFT:
            raise ValidationError("Отменить можно только черновик акта.")
        charge_ids = list(act.lines.values_list("charge_id", flat=True))
        act.status = ActStatus.CANCELLED
        act.save(update_fields=["status", "updated_at"])
        if charge_ids:
            ApplicationCharge.objects.filter(pk__in=charge_ids).update(is_included_in_act=False)
        application = act.application
        has_active = application.acts.exclude(status=ActStatus.CANCELLED).exists()
        if not has_active:
            application.billing_status = BillingStatus.CALCULATED
            application.act_total = Decimal("0")
            application.save(update_fields=["billing_status", "act_total", "updated_at"])
        BillingWorkflowService.audit(
            action="act_cancelled",
            application=application,
            user=user,
            obj=act,
            new_value={"released_charges": len(charge_ids)},
        )
        return act

    @staticmethod
    @transaction.atomic
    def ensure_charge_editable(charge: ApplicationCharge, *, user=None) -> ApplicationCharge:
        """
        Разрешает правку qty/услуги: счёт — нельзя; черновик акта — отменяем и освобождаем;
        отправленный/подтверждённый акт — нельзя.
        """
        charge.refresh_from_db()
        if charge.is_included_in_invoice:
            raise ValidationError("Нельзя менять начисление, уже включённое в счёт.")
        if not charge.is_included_in_act:
            return charge
        draft_acts = list(
            BillingAct.objects.filter(status=ActStatus.DRAFT, lines__charge=charge).distinct()
        )
        if not draft_acts:
            raise ValidationError(
                "Нельзя менять начисление в отправленном или подтверждённом акте. "
                "Сначала отзовите акт или создайте корректировку."
            )
        for act in draft_acts:
            BillingWorkflowService.cancel_draft_act(act, user=user)
        charge.refresh_from_db()
        return charge

    @staticmethod
    def charge_editable_by_manager(charge: ApplicationCharge) -> bool:
        if charge.is_excluded:
            return False
        if charge.is_included_in_invoice or charge.is_disputed:
            return False
        if not charge.is_included_in_act:
            return True
        return BillingAct.objects.filter(status=ActStatus.DRAFT, lines__charge=charge).exists()

    @staticmethod
    def assert_charge_version(charge: ApplicationCharge, expected_version) -> None:
        if expected_version is None:
            return
        try:
            expected = int(expected_version)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Некорректная версия записи.") from exc
        if int(charge.edit_version or 1) != expected:
            raise ChargeVersionConflict(
                "Расчёт был изменён другим пользователем. Обновите страницу и повторите действие."
            )

    @staticmethod
    def _bump_charge_version(charge: ApplicationCharge) -> None:
        charge.edit_version = int(charge.edit_version or 1) + 1
        charge.save(update_fields=["edit_version", "updated_at"])

    @staticmethod
    def record_charge_history(
        charge: ApplicationCharge,
        *,
        change_type: str,
        user=None,
        old_service_name: str = "",
        new_service_name: str = "",
        old_quantity=None,
        new_quantity=None,
        old_tariff=None,
        new_tariff=None,
        old_total=None,
        new_total=None,
        reason: str = "",
        comment: str = "",
    ) -> ApplicationChargeHistory:
        return ApplicationChargeHistory.objects.create(
            charge=charge,
            application=charge.application,
            change_type=change_type,
            old_service_name=old_service_name or "",
            new_service_name=new_service_name or "",
            old_quantity=old_quantity,
            new_quantity=new_quantity,
            old_tariff=old_tariff,
            new_tariff=new_tariff,
            old_total=old_total,
            new_total=new_total,
            reason=reason or "",
            comment=comment or "",
            user=user if getattr(user, "is_authenticated", False) else None,
        )

    @staticmethod
    @transaction.atomic
    def change_charge_service(
        charge: ApplicationCharge,
        *,
        service: BillingService,
        quantity=None,
        comment=None,
        user=None,
        expected_version=None,
        reason: str = "",
    ) -> ApplicationCharge:
        """Смена услуги на строке начисления: цена из тарифа, сброс confirm, метка для бухгалтера."""
        if charge.is_excluded:
            raise ValidationError("Исключённое начисление нельзя менять. Сначала восстановите строку.")
        if charge.is_disputed:
            raise ValidationError("Спорное начисление нельзя менять до корректировки.")
        BillingWorkflowService.ensure_charge_editable(charge, user=user)
        charge.refresh_from_db()
        BillingWorkflowService.assert_charge_version(charge, expected_version)

        old_service = charge.service
        old_name = (charge.service_name_snapshot or getattr(old_service, "name", "") or "").strip()
        old_qty = charge.quantity
        old_tariff = charge.tariff
        old_total = charge.total_amount
        qty = quantity if quantity is not None else charge.quantity
        new_comment = charge.comment if comment is None else comment
        source_key = charge.source_key or f"charge:{charge.pk}"
        service_changed = int(service.pk) != int(old_service.pk)

        if not service_changed:
            if charge.is_manual_override:
                updated = BillingWorkflowService.create_or_update_charge(
                    charge.application,
                    service=charge.service,
                    quantity=qty,
                    tariff=charge.tariff,
                    unit=charge.unit,
                    vat_rate=charge.vat_rate,
                    source_key=source_key,
                    comment=new_comment,
                    user=user,
                    client_tariff_version=charge.client_tariff_version,
                    client_tariff_item=charge.client_tariff_item,
                    tariff_price=charge.tariff_price if charge.tariff_price is not None else charge.tariff,
                    tariff_source_label=charge.tariff_source_label,
                    tariff_basis=charge.tariff_basis,
                    service_name_snapshot=charge.service_name_snapshot or charge.service.name,
                    is_manual_override=True,
                    override_reason=charge.override_reason or "Сохранение количества",
                    overridden_by=charge.overridden_by or user,
                    resolve_from_agreed_tariff=False,
                )
            else:
                updated = BillingWorkflowService.create_or_update_charge(
                    charge.application,
                    service=charge.service,
                    quantity=qty,
                    unit=charge.unit,
                    source_key=source_key,
                    comment=new_comment,
                    user=user,
                    resolve_from_agreed_tariff=True,
                )
            if not charge.source_key and charge.pk != updated.pk:
                try:
                    charge.delete()
                except Exception:
                    pass
            return updated

        updated = BillingWorkflowService.create_or_update_charge(
            charge.application,
            service=service,
            quantity=qty,
            unit=service.unit or charge.unit,
            source_key=source_key,
            source_type=charge.source_type or ApplicationCharge.SOURCE_MANUAL,
            source_id=charge.source_id or "",
            performed_at=charge.performed_at,
            billing_period=charge.billing_period,
            operation_type=charge.operation_type or "",
            operation_id=charge.operation_id or "",
            comment=new_comment,
            user=user,
            resolve_from_agreed_tariff=True,
            is_manual_override=False,
        )
        if not charge.source_key and charge.pk != updated.pk:
            try:
                charge.delete()
            except Exception:
                pass

        updated.previous_service_name = old_name or str(old_service)
        updated.service_changed_at = timezone.now()
        updated.is_confirmed = False
        # Смена услуги сбрасывает ручной override цены — цена взята из тарифа
        updated.is_manual_override = False
        updated.override_reason = ""
        updated.overridden_by = None
        updated.overridden_at = None
        updated.edit_version = int(updated.edit_version or 1) + 1
        updated.save(
            update_fields=[
                "previous_service_name",
                "service_changed_at",
                "is_confirmed",
                "is_manual_override",
                "override_reason",
                "overridden_by",
                "overridden_at",
                "edit_version",
                "updated_at",
            ]
        )
        BillingWorkflowService.record_charge_history(
            updated,
            change_type=ApplicationChargeHistory.CHANGE_SERVICE,
            user=user,
            old_service_name=old_name,
            new_service_name=updated.service_name_snapshot or updated.service.name,
            old_quantity=old_qty,
            new_quantity=updated.quantity,
            old_tariff=old_tariff,
            new_tariff=updated.tariff,
            old_total=old_total,
            new_total=updated.total_amount,
            reason=reason or "service_changed",
            comment=new_comment or "",
        )
        BillingWorkflowService.audit(
            action="charge_service_changed",
            application=updated.application,
            user=user,
            obj=updated,
            old_value={
                "service_id": old_service.pk,
                "service_name": old_name,
                "service_code": getattr(old_service, "code", ""),
            },
            new_value={
                "service_id": updated.service_id,
                "service_name": updated.service_name_snapshot or updated.service.name,
                "service_code": updated.service.code,
                "tariff": str(updated.tariff),
                "total_amount": str(updated.total_amount),
            },
        )
        try:
            from .models import BillingStaffNotification
            from .staff_notifications import notify_accountants

            app = updated.application
            client_name = getattr(app.client, "short_name", None) or getattr(app.client, "agn_name", "") or f"#{app.client_id}"
            notify_accountants(
                kind=BillingStaffNotification.KIND_SERVICE_CHANGED,
                title="Менеджер изменил услугу в начислении",
                message=(
                    f"{client_name}: {app.get_application_type_display()} {app.application_id}. "
                    f"«{old_name}» → «{updated.service_name_snapshot or updated.service.name}»."
                ),
                link_url=f"/team-manager/billing/applications/{app.id}/",
                client=app.client,
                source_key=f"charge-service-changed:{updated.id}:{int(updated.service_changed_at.timestamp())}",
                actor=user,
            )
        except Exception:
            pass
        BillingWorkflowService._promote_calculated_if_ready(updated.application)
        return updated

    @staticmethod
    @transaction.atomic
    def confirm_charge(charge: ApplicationCharge, *, user=None, comment: str = "", expected_version=None) -> ApplicationCharge:
        from .charge_status import NO_PRICE_MESSAGE, charge_missing_price

        if charge.is_excluded:
            raise ValidationError("Исключённое начисление нельзя подтвердить.")
        if charge.is_included_in_act or charge.is_included_in_invoice:
            raise ValidationError("Нельзя менять подтверждение начисления, уже включённого в акт или счёт.")
        if charge_missing_price(charge):
            raise ValidationError(NO_PRICE_MESSAGE)
        if charge.is_disputed:
            raise ValidationError("Спорное начисление нельзя подтвердить до корректировки.")
        if Decimal(str(charge.quantity or "0")) <= 0:
            raise ValidationError("Количество должно быть больше нуля.")
        BillingWorkflowService.assert_charge_version(charge, expected_version)
        if charge.is_confirmed:
            BillingWorkflowService._promote_calculated_if_ready(charge.application)
            return charge
        charge.is_confirmed = True
        charge.edit_version = int(charge.edit_version or 1) + 1
        if comment:
            note = (charge.comment or "").strip()
            charge.comment = f"{note}\n{comment}".strip() if note else comment
            charge.save(update_fields=["is_confirmed", "comment", "edit_version", "updated_at"])
        else:
            charge.save(update_fields=["is_confirmed", "edit_version", "updated_at"])
        BillingWorkflowService.record_charge_history(
            charge,
            change_type=ApplicationChargeHistory.CHANGE_CONFIRM,
            user=user,
            new_quantity=charge.quantity,
            new_tariff=charge.tariff,
            new_total=charge.total_amount,
            comment=comment or "",
        )
        BillingWorkflowService.audit(
            action="charge_qty_confirmed",
            application=charge.application,
            user=user,
            obj=charge,
            new_value={"quantity": str(charge.quantity), "total_amount": str(charge.total_amount)},
        )
        BillingWorkflowService._promote_calculated_if_ready(charge.application)
        return charge

    @staticmethod
    @transaction.atomic
    def unconfirm_charge(charge: ApplicationCharge, *, user=None, expected_version=None) -> ApplicationCharge:
        if charge.is_included_in_act or charge.is_included_in_invoice:
            raise ValidationError("Нельзя снять подтверждение с начисления в акте или счёте.")
        BillingWorkflowService.assert_charge_version(charge, expected_version)
        if not charge.is_confirmed:
            return charge
        charge.is_confirmed = False
        charge.edit_version = int(charge.edit_version or 1) + 1
        charge.save(update_fields=["is_confirmed", "edit_version", "updated_at"])
        BillingWorkflowService.record_charge_history(
            charge,
            change_type=ApplicationChargeHistory.CHANGE_UNCONFIRM,
            user=user,
            old_quantity=charge.quantity,
            old_total=charge.total_amount,
        )
        BillingWorkflowService.audit(
            action="charge_qty_unconfirmed",
            application=charge.application,
            user=user,
            obj=charge,
            new_value={"quantity": str(charge.quantity)},
        )
        return charge

    @staticmethod
    @transaction.atomic
    def confirm_application_charges(application: BillingApplication, *, user=None) -> int:
        from .charge_status import charge_missing_price

        confirmed = 0
        for charge in application.charges.filter(is_confirmed=False, is_disputed=False, is_excluded=False):
            if charge.is_included_in_act or charge.is_included_in_invoice:
                continue
            if charge_missing_price(charge):
                continue
            BillingWorkflowService.confirm_charge(charge, user=user)
            confirmed += 1
        BillingWorkflowService._promote_calculated_if_ready(application)
        return confirmed

    @staticmethod
    @transaction.atomic
    def update_charge_quantity(
        charge: ApplicationCharge,
        *,
        quantity,
        user=None,
        basis: str = "",
        comment: str = "",
        expected_version=None,
    ) -> ApplicationCharge:
        if charge.is_excluded:
            raise ValidationError("Исключённое начисление нельзя менять.")
        BillingWorkflowService.ensure_charge_editable(charge, user=user)
        charge.refresh_from_db()
        BillingWorkflowService.assert_charge_version(charge, expected_version)
        new_qty = quantize_qty(quantity)
        if new_qty <= 0:
            raise ValidationError("Количество должно быть больше нуля.")
        if basis == ApplicationCharge.QTY_BASIS_MANUAL and not (comment or "").strip():
            raise ValidationError("Для ручной корректировки количества нужен комментарий.")
        old_qty = charge.quantity
        old_total = charge.total_amount
        if charge.original_quantity is None:
            charge.original_quantity = old_qty
        vat_type = charge.vat_type_snapshot or (
            charge.client_tariff_version.vat_type if charge.client_tariff_version_id else None
        )
        amount, vat_amount, total_amount = calculate_amounts(
            new_qty,
            charge.tariff,
            charge.vat_rate,
            vat_type=vat_type,
        )
        charge.quantity = new_qty
        charge.amount = amount
        charge.vat_amount = vat_amount
        charge.total_amount = total_amount
        charge.is_confirmed = False
        charge.qty_change_basis = basis or charge.qty_change_basis
        charge.qty_change_comment = (comment or "").strip()
        charge.edit_version = int(charge.edit_version or 1) + 1
        charge.save(
            update_fields=[
                "quantity",
                "amount",
                "vat_amount",
                "total_amount",
                "is_confirmed",
                "original_quantity",
                "qty_change_basis",
                "qty_change_comment",
                "edit_version",
                "updated_at",
            ]
        )
        BillingWorkflowService.recalculate_application_totals(charge.application)
        BillingWorkflowService.record_charge_history(
            charge,
            change_type=ApplicationChargeHistory.CHANGE_QTY,
            user=user,
            old_quantity=old_qty,
            new_quantity=new_qty,
            old_total=old_total,
            new_total=total_amount,
            reason=basis or "qty_changed",
            comment=comment or "",
        )
        return charge

    @staticmethod
    @transaction.atomic
    def exclude_charge(
        charge: ApplicationCharge,
        *,
        reason: str,
        comment: str = "",
        user=None,
        expected_version=None,
    ) -> ApplicationCharge:
        if charge.is_included_in_invoice:
            raise ValidationError("Начисление в счёте нельзя исключить.")
        if charge.is_included_in_act and not BillingAct.objects.filter(
            status=ActStatus.DRAFT, lines__charge=charge
        ).exists():
            raise ValidationError("Начисление в отправленном акте нельзя исключить.")
        BillingWorkflowService.assert_charge_version(charge, expected_version)
        valid_reasons = {c[0] for c in ApplicationCharge.EXCLUDE_REASON_CHOICES}
        if reason not in valid_reasons:
            raise ValidationError("Укажите причину исключения.")
        if reason == ApplicationCharge.EXCLUDE_OTHER and not (comment or "").strip():
            raise ValidationError("Для причины «Другое» нужен комментарий.")
        if charge.is_excluded:
            return charge
        old_total = charge.total_amount
        charge.is_excluded = True
        charge.excluded_at = timezone.now()
        charge.excluded_by = user if getattr(user, "is_authenticated", False) else None
        charge.exclude_reason = reason
        charge.exclude_comment = (comment or "").strip()
        charge.is_confirmed = False
        charge.edit_version = int(charge.edit_version or 1) + 1
        charge.save(
            update_fields=[
                "is_excluded",
                "excluded_at",
                "excluded_by",
                "exclude_reason",
                "exclude_comment",
                "is_confirmed",
                "edit_version",
                "updated_at",
            ]
        )
        BillingWorkflowService.recalculate_application_totals(charge.application)
        BillingWorkflowService.record_charge_history(
            charge,
            change_type=ApplicationChargeHistory.CHANGE_EXCLUDE,
            user=user,
            old_total=old_total,
            new_total=Decimal("0"),
            reason=reason,
            comment=comment or "",
        )
        BillingWorkflowService.audit(
            action="charge_excluded",
            application=charge.application,
            user=user,
            obj=charge,
            new_value={"reason": reason, "comment": comment or ""},
        )
        return charge

    @staticmethod
    @transaction.atomic
    def restore_charge(charge: ApplicationCharge, *, user=None, expected_version=None) -> ApplicationCharge:
        BillingWorkflowService.assert_charge_version(charge, expected_version)
        if not charge.is_excluded:
            return charge
        charge.is_excluded = False
        charge.excluded_at = None
        charge.excluded_by = None
        charge.exclude_reason = ""
        charge.exclude_comment = ""
        charge.is_confirmed = False
        charge.edit_version = int(charge.edit_version or 1) + 1
        charge.save(
            update_fields=[
                "is_excluded",
                "excluded_at",
                "excluded_by",
                "exclude_reason",
                "exclude_comment",
                "is_confirmed",
                "edit_version",
                "updated_at",
            ]
        )
        BillingWorkflowService.recalculate_application_totals(charge.application)
        BillingWorkflowService.record_charge_history(
            charge,
            change_type=ApplicationChargeHistory.CHANGE_RESTORE,
            user=user,
            new_total=charge.total_amount,
            reason="restore",
        )
        BillingWorkflowService.audit(
            action="charge_restored",
            application=charge.application,
            user=user,
            obj=charge,
        )
        return charge

    @staticmethod
    @transaction.atomic
    def add_manual_charge(
        application: BillingApplication,
        *,
        service: BillingService,
        quantity,
        user=None,
        comment: str = "",
        basis: str = "",
        allow_without_tariff: bool = True,
    ) -> ApplicationCharge:
        """Добавить услугу менеджером. Без тарифа — черновик, подтвердить нельзя."""
        from .price_resolver import resolve_client_service_price
        from .charge_status import charge_missing_price

        qty = quantize_qty(quantity)
        if qty <= 0:
            raise ValidationError("Количество должно быть больше нуля.")
        performed_at = application.created_at_source or timezone.now()
        resolved = resolve_client_service_price(
            application, service, performed_at=performed_at, quantity=qty
        )
        source_key = f"manual:{application.id}:{service.id}:{timezone.now().timestamp()}"
        if resolved.ok and resolved.tariff is not None and resolved.tariff_version is not None:
            charge = BillingWorkflowService.create_or_update_charge(
                application,
                service=service,
                quantity=qty,
                source_type=ApplicationCharge.SOURCE_MANUAL,
                source_key=source_key,
                performed_at=performed_at,
                comment=comment or "",
                user=user,
                resolve_from_agreed_tariff=True,
            )
        elif allow_without_tariff:
            amount, vat_amount, total_amount = calculate_amounts(qty, Decimal("0"), service.vat_rate or "0")
            charge = ApplicationCharge.objects.create(
                application=application,
                client=application.client,
                legal_entity=application.legal_entity,
                service=service,
                quantity=qty,
                unit=service.unit or "шт",
                tariff=Decimal("0"),
                tariff_price=None,
                amount=amount,
                vat_rate=str(service.vat_rate or "0"),
                vat_amount=vat_amount,
                total_amount=total_amount,
                service_name_snapshot=service.name,
                source_type=ApplicationCharge.SOURCE_MANUAL,
                source_key=source_key,
                performed_at=performed_at,
                billing_period=performed_at.date().replace(day=1) if hasattr(performed_at, "date") else timezone.localdate().replace(day=1),
                is_confirmed=False,
                comment=comment or "",
                qty_change_basis=basis or "",
                created_by=user if getattr(user, "is_authenticated", False) else None,
            )
            BillingWorkflowService.recalculate_application_totals(application)
        else:
            raise ValidationError(
                resolved.note
                or f'Для услуги «{service.name}» нет согласованного тарифа клиента.'
            )
        if charge_missing_price(charge):
            charge.is_confirmed = False
            charge.save(update_fields=["is_confirmed", "updated_at"])
        BillingWorkflowService.record_charge_history(
            charge,
            change_type=ApplicationChargeHistory.CHANGE_ADD,
            user=user,
            new_service_name=charge.service_name_snapshot or service.name,
            new_quantity=charge.quantity,
            new_tariff=charge.tariff,
            new_total=charge.total_amount,
            reason=basis or "manual_add",
            comment=comment or "",
        )
        return charge

    @staticmethod
    @transaction.atomic
    def recalculate_charge(charge: ApplicationCharge, *, user=None, expected_version=None) -> ApplicationCharge:
        if charge.is_excluded:
            raise ValidationError("Исключённую строку нельзя пересчитать.")
        BillingWorkflowService.ensure_charge_editable(charge, user=user)
        charge.refresh_from_db()
        BillingWorkflowService.assert_charge_version(charge, expected_version)
        old_tariff = charge.tariff
        old_total = charge.total_amount
        updated = BillingWorkflowService.create_or_update_charge(
            charge.application,
            service=charge.service,
            quantity=charge.quantity,
            unit=charge.unit,
            source_key=charge.source_key or f"charge:{charge.pk}",
            source_type=charge.source_type,
            source_id=charge.source_id,
            performed_at=charge.performed_at,
            billing_period=charge.billing_period,
            operation_type=charge.operation_type,
            operation_id=charge.operation_id,
            comment=charge.comment,
            user=user,
            resolve_from_agreed_tariff=True,
            is_manual_override=False,
        )
        updated.is_confirmed = False
        updated.edit_version = int(updated.edit_version or 1) + 1
        updated.save(update_fields=["is_confirmed", "edit_version", "updated_at"])
        BillingWorkflowService.record_charge_history(
            updated,
            change_type=ApplicationChargeHistory.CHANGE_RECALC,
            user=user,
            old_tariff=old_tariff,
            new_tariff=updated.tariff,
            old_total=old_total,
            new_total=updated.total_amount,
            reason="recalc",
        )
        return updated

    @staticmethod
    @transaction.atomic
    def bulk_charge_action(
        application: BillingApplication,
        *,
        action: str,
        charge_ids: list[int],
        user=None,
        service_id=None,
        reason: str = "",
        comment: str = "",
        expected_versions: dict | None = None,
    ) -> dict:
        expected_versions = expected_versions or {}
        qs = list(
            application.charges.filter(id__in=charge_ids).select_related("service")
        )
        if not qs:
            raise ValidationError("Не выбраны начисления.")
        done = 0
        if action == "confirm":
            for ch in qs:
                BillingWorkflowService.confirm_charge(
                    ch, user=user, expected_version=expected_versions.get(str(ch.id)) or expected_versions.get(ch.id)
                )
                done += 1
        elif action == "exclude":
            for ch in qs:
                BillingWorkflowService.exclude_charge(
                    ch,
                    reason=reason,
                    comment=comment,
                    user=user,
                    expected_version=expected_versions.get(str(ch.id)) or expected_versions.get(ch.id),
                )
                done += 1
        elif action == "restore":
            for ch in qs:
                BillingWorkflowService.restore_charge(
                    ch,
                    user=user,
                    expected_version=expected_versions.get(str(ch.id)) or expected_versions.get(ch.id),
                )
                done += 1
        elif action == "replace_service":
            if not service_id:
                raise ValidationError("Укажите новую услугу.")
            service = BillingService.objects.filter(pk=service_id, is_active=True).first()
            if not service:
                raise ValidationError("Услуга не найдена.")
            for ch in qs:
                BillingWorkflowService.change_charge_service(
                    ch,
                    service=service,
                    user=user,
                    reason=reason or "bulk_replace",
                    comment=comment,
                    expected_version=expected_versions.get(str(ch.id)) or expected_versions.get(ch.id),
                )
                done += 1
        elif action == "recalc":
            for ch in qs:
                if ch.is_excluded:
                    continue
                BillingWorkflowService.recalculate_charge(
                    ch,
                    user=user,
                    expected_version=expected_versions.get(str(ch.id)) or expected_versions.get(ch.id),
                )
                done += 1
        else:
            raise ValidationError("Неизвестное массовое действие.")
        return {"done": done, "action": action}

    @staticmethod
    def _promote_calculated_if_ready(application: BillingApplication) -> None:
        """Если открытые начисления с ценой готовы — статус calculated (для кнопки «Создать акт»)."""
        from .charge_status import charge_missing_price

        if application.billing_status not in {
            BillingStatus.NOT_CALCULATED,
            BillingStatus.CALCULATION_DRAFT,
        }:
            return
        open_charges = list(
            application.charges.filter(is_included_in_act=False, is_disputed=False)
        )
        if not open_charges:
            return
        if any(charge_missing_price(c) for c in open_charges):
            return
        application.billing_status = BillingStatus.CALCULATED
        application.save(update_fields=["billing_status", "updated_at"])

    @staticmethod
    @transaction.atomic
    def create_correction_charge(charge: ApplicationCharge, *, quantity, tariff=None, reason="", user=None):
        correction = BillingWorkflowService.create_or_update_charge(
            charge.application,
            service=charge.service,
            quantity=quantity,
            tariff=tariff if tariff is not None else charge.tariff,
            unit=charge.unit,
            vat_rate=charge.vat_rate,
            source_type=ApplicationCharge.SOURCE_MANUAL,
            performed_at=timezone.now(),
            billing_period=timezone.localdate().replace(day=1),
            comment=reason,
            user=user,
        )
        correction.correction_of = charge
        correction.correction_reason = reason or ""
        correction.save(update_fields=["correction_of", "correction_reason", "updated_at"])
        BillingWorkflowService.audit(
            action="charge_correction_created",
            application=charge.application,
            user=user,
            obj=correction,
            comment=reason or "",
        )
        return correction

    @staticmethod
    @transaction.atomic
    def calculate_charges(application: BillingApplication, *, user=None):
        from .calculators.engine import calculate_application

        try:
            from accountant.selectors import is_client_billing_ready
            from billing.tariff_services import active_tariff_version

            if not is_client_billing_ready(application.client):
                raise ValidationError(
                    "Клиент не активен в контуре бухгалтера. Расчет счета невозможен. Обратитесь к бухгалтеру."
                )
            if not active_tariff_version(application.client):
                raise ValidationError(
                    "У клиента не настроен активный тариф. Расчет счета невозможен. Обратитесь к бухгалтеру."
                )
        except ImportError:
            pass

        if application.application_type in {
            BillingApplication.TYPE_RECEIVING,
            BillingApplication.TYPE_PROCESSING,
            BillingApplication.TYPE_PACKING,
            BillingApplication.TYPE_SHIPPING,
            BillingApplication.TYPE_LOGISTICS,
            BillingApplication.TYPE_STORAGE,
            BillingApplication.TYPE_OTHER,
        }:
            result = calculate_application(application, user=user)
            return result["charges"]

        application.billing_status = BillingStatus.CALCULATION_DRAFT
        application.save(update_fields=["billing_status", "updated_at"])
        created = []
        try:
            from client_cabinet.models import ClientServiceCharge
        except ImportError:
            ClientServiceCharge = None
        if ClientServiceCharge is not None:
            for legacy in ClientServiceCharge.objects.filter(order_type=application.application_type, order_id=application.application_id):
                service, _ = BillingService.objects.get_or_create(
                    code=f"legacy_{legacy.service_type or 'service'}",
                    defaults={"name": legacy.description or legacy.get_service_type_display(), "unit": "усл", "vat_rate": "0"},
                )
                created.append(
                    BillingWorkflowService.create_or_update_charge(
                        application,
                        service=service,
                        quantity=1,
                        tariff=legacy.amount,
                        vat_rate="0",
                        source_type=ApplicationCharge.SOURCE_LEGACY_CHARGE,
                        source_id=legacy.pk,
                        source_key=f"legacy:{legacy.pk}",
                        performed_at=getattr(legacy, "created_at", None) or timezone.now(),
                        billing_period=legacy.period_start or timezone.localdate().replace(day=1),
                        comment=legacy.description or "",
                        user=user,
                    )
                )
        application.refresh_from_db()
        application.billing_status = BillingStatus.CALCULATED
        application.save(update_fields=["billing_status", "updated_at"])
        BillingWorkflowService.audit(action="charges_calculated", application=application, user=user, obj=application)
        return created

    @staticmethod
    @transaction.atomic
    def generate_act(application: BillingApplication, *, user=None) -> BillingAct:
        from .charge_status import NO_PRICE_MESSAGE, charge_missing_price

        candidates = list(
            application.charges.filter(
                is_included_in_act=False,
                is_disputed=False,
                is_excluded=False,
            ).order_by("id")
        )
        if not candidates:
            raise ValidationError("Нельзя создать акт без начислений.")
        missing = [c for c in candidates if charge_missing_price(c)]
        if missing:
            raise ValidationError(NO_PRICE_MESSAGE)
        unconfirmed = [c for c in candidates if not c.is_confirmed]
        if unconfirmed:
            raise ValidationError(
                f"Подтвердите количество по начислениям перед созданием акта "
                f"(осталось {len(unconfirmed)})."
            )
        charges = candidates
        act = BillingAct.objects.create(
            application=application,
            client=application.client,
            legal_entity=application.legal_entity,
            number=BillingWorkflowService.next_number(SequenceKind.ACT),
            status=ActStatus.DRAFT,
            created_by=user,
        )
        subtotal = Decimal("0")
        vat_total = Decimal("0")
        total = Decimal("0")
        for charge in charges:
            BillingActLine.objects.create(
                act=act,
                charge=charge,
                service_name=charge.service.name,
                quantity=charge.quantity,
                unit=charge.unit,
                tariff=charge.tariff,
                amount=charge.amount,
                vat_amount=charge.vat_amount,
                total_amount=charge.total_amount,
            )
            charge.is_included_in_act = True
            charge.save(update_fields=["is_included_in_act", "updated_at"])
            subtotal += charge.amount
            vat_total += charge.vat_amount
            total += charge.total_amount
        act.subtotal = quantize_money(subtotal)
        act.vat_amount = quantize_money(vat_total)
        act.total_amount = quantize_money(total)
        act.save(update_fields=["subtotal", "vat_amount", "total_amount", "updated_at"])
        application.act_total = act.total_amount
        application.billing_status = BillingStatus.ACT_DRAFT
        application.save(update_fields=["act_total", "billing_status", "updated_at"])
        BillingWorkflowService.audit(action="act_generated", application=application, user=user, obj=act)
        return act

    @staticmethod
    @transaction.atomic
    def generate_act_and_invoice(
        application: BillingApplication,
        *,
        due_date=None,
        invoice_type=ClientInvoice.TYPE_REGULAR,
        user=None,
    ) -> tuple[BillingAct, ClientInvoice]:
        """Create both documents without waiting for client act confirmation."""
        existing_invoice = (
            BillingWorkflowService.invoices_for_application(application)
            .select_related("act")
            .order_by("-id")
            .first()
        )
        if existing_invoice:
            return existing_invoice.act, existing_invoice

        act = BillingWorkflowService.generate_act(application, user=user)
        invoice = BillingWorkflowService.create_grouped_invoice(
            acts=[act],
            due_date=due_date,
            invoice_type=invoice_type,
            user=user,
            allow_unconfirmed_acts=True,
        )
        return act, invoice

    @staticmethod
    @transaction.atomic
    def generate_grouped_shipping_invoice(
        applications,
        *,
        due_date=None,
        invoice_type=ClientInvoice.TYPE_REGULAR,
        user=None,
    ) -> ClientInvoice:
        """Create separate acts for shipping applications and one shared invoice."""
        ordered = []
        seen = set()
        for application in applications:
            if application.id in seen:
                continue
            seen.add(application.id)
            ordered.append(application)

        if len(ordered) < 2:
            raise ValidationError("Выберите минимум две отгрузки для общего счёта.")

        client_id = ordered[0].client_id
        legal_entity_id = ordered[0].legal_entity_id
        for application in ordered:
            if application.application_type != BillingApplication.TYPE_SHIPPING:
                raise ValidationError("В общий счёт этим способом можно объединять только отгрузки.")
            if application.client_id != client_id:
                raise ValidationError("В один счёт можно объединить только отгрузки одного клиента.")
            if application.legal_entity_id != legal_entity_id:
                raise ValidationError("В один счёт можно объединить только отгрузки одного юрлица клиента.")
            existing_invoice = BillingWorkflowService.invoices_for_application(application).first()
            if existing_invoice:
                raise ValidationError(
                    f"По отгрузке {application.application_id} уже есть счёт {existing_invoice.number}."
                )
            blocked_act = (
                application.acts.exclude(status=ActStatus.CANCELLED)
                .exclude(status__in=[ActStatus.DRAFT, ActStatus.CONFIRMED])
                .order_by("-id")
                .first()
            )
            if blocked_act:
                raise ValidationError(
                    f"Акт {blocked_act.number} по отгрузке {application.application_id} имеет статус "
                    f"«{blocked_act.get_status_display()}» и не может быть включён автоматически."
                )

        acts = []
        for application in ordered:
            application_acts = list(
                application.acts.filter(status__in=[ActStatus.DRAFT, ActStatus.CONFIRMED]).order_by("act_date", "id")
            )
            for act in application_acts:
                existing_invoice = BillingWorkflowService.active_invoice_for_act(act)
                if existing_invoice:
                    raise ValidationError(f"Акт {act.number} уже входит в счёт {existing_invoice.number}.")
            acts.extend(application_acts)

            has_unbilled_charges = application.charges.filter(
                is_included_in_act=False,
                is_disputed=False,
                is_excluded=False,
            ).exists()
            if has_unbilled_charges:
                acts.append(BillingWorkflowService.generate_act(application, user=user))
            elif not application_acts:
                raise ValidationError(
                    f"По отгрузке {application.application_id} нет начислений для создания акта."
                )

        return BillingWorkflowService.create_grouped_invoice(
            acts=acts,
            due_date=due_date,
            invoice_type=invoice_type,
            user=user,
            allow_unconfirmed_acts=True,
        )

    @staticmethod
    @transaction.atomic
    def send_act(act: BillingAct, *, user=None):
        if act.status != ActStatus.DRAFT:
            raise ValidationError("Отправить можно только черновик акта.")
        act.status = ActStatus.SENT
        act.sent_at = timezone.now()
        act.save(update_fields=["status", "sent_at", "updated_at"])
        application = act.application
        application.billing_status = BillingStatus.ACT_SENT
        application.save(update_fields=["billing_status", "updated_at"])
        BillingWorkflowService.audit(action="act_sent", application=application, user=user, obj=act)
        return act

    @staticmethod
    @transaction.atomic
    def confirm_act(act: BillingAct, *, user=None, comment="", request=None):
        if act.status != ActStatus.SENT:
            raise ValidationError("Подтвердить можно только отправленный акт.")
        act.status = ActStatus.CONFIRMED
        act.confirmed_at = timezone.now()
        act.confirmed_by = user
        act.confirmed_amount = act.total_amount
        act.client_comment = comment or ""
        if request is not None:
            act.client_ip = request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", "")).split(",")[0] or None
            act.client_user_agent = request.META.get("HTTP_USER_AGENT", "")
        act.save(
            update_fields=[
                "status",
                "confirmed_at",
                "confirmed_by",
                "confirmed_amount",
                "client_comment",
                "client_ip",
                "client_user_agent",
                "updated_at",
            ]
        )
        application = act.application
        application.billing_status = BillingStatus.INVOICE_REQUIRED
        application.act_confirmed_at = act.confirmed_at
        application.invoice_required_at = timezone.now()
        application.save(update_fields=["billing_status", "act_confirmed_at", "invoice_required_at", "updated_at"])
        BillingWorkflowService.audit(
            action="act_confirmed",
            application=application,
            user=user,
            obj=act,
            new_value={"confirmed_amount": str(act.confirmed_amount)},
            comment=comment,
            request=request,
        )
        BillingWorkflowService.create_billing_notification(application.client, f"Акт {act.number} подтвержден.", source_key=f"billing-act-confirmed:{act.pk}")
        return act

    @staticmethod
    @transaction.atomic
    def dispute_act(act: BillingAct, *, user=None, comment, line=None, expected_quantity=None, expected_amount=None, file=None, request=None):
        if act.status != ActStatus.SENT:
            raise ValidationError("Разногласия можно отправить только по отправленному акту.")
        dispute = BillingActDispute.objects.create(
            act=act,
            line=line,
            expected_quantity=expected_quantity,
            expected_amount=expected_amount,
            comment=comment,
            file=file,
            created_by=user,
        )
        act.status = ActStatus.DISPUTED
        act.client_comment = comment
        act.save(update_fields=["status", "client_comment", "updated_at"])
        application = act.application
        application.billing_status = BillingStatus.ACT_DISPUTED
        application.save(update_fields=["billing_status", "updated_at"])
        BillingWorkflowService.audit(action="act_disputed", application=application, user=user, obj=dispute, comment=comment, request=request)
        BillingWorkflowService.create_billing_notification(application.client, f"По акту {act.number} отправлены разногласия.", source_key=f"billing-act-disputed:{act.pk}")
        return dispute

    @staticmethod
    def _assert_client_ready_for_invoice(client, applications):
        try:
            from accountant.selectors import is_client_billing_ready

            if not is_client_billing_ready(client):
                raise ValidationError(
                    "Нельзя выставить счёт: клиент не активен или не завершён контур бухгалтера "
                    "(реквизиты, компания обслуживания, опубликованный тариф)."
                )
        except ImportError:
            pass
        missing_names = []
        for application in applications:
            missing = ((application.source_payload or {}).get("billing") or {}).get("missing_tariffs") or []
            for item in missing:
                missing_names.append(item.get("service_name") or item.get("service_code") or "услуга")
            if application.billing_status == BillingStatus.CALCULATION_DRAFT and missing:
                raise ValidationError("Расчёт не готов: есть услуги без согласованного тарифа.")
        if missing_names:
            names = ", ".join(missing_names[:5])
            raise ValidationError(
                f"Нельзя выставить счёт: для услуг нет согласованного тарифа ({names}). "
                f"У клиента не настроен активный тариф. Расчет счета невозможен. Обратитесь к бухгалтеру."
            )

    @staticmethod
    def active_invoice_for_act(act: BillingAct):
        return (
            ClientInvoice.objects.filter(Q(act=act) | Q(invoice_acts__act=act))
            .exclude(status=InvoiceStatus.CANCELLED)
            .distinct()
            .first()
        )

    @staticmethod
    def invoices_for_application(application: BillingApplication):
        return (
            ClientInvoice.objects.filter(Q(application=application) | Q(invoice_acts__act__application=application))
            .exclude(status=InvoiceStatus.CANCELLED)
            .distinct()
        )

    @staticmethod
    def _link_acts_to_invoice(invoice: ClientInvoice, acts):
        act_ids = []
        for index, act in enumerate(acts):
            ClientInvoiceAct.objects.get_or_create(
                invoice=invoice,
                act=act,
                defaults={"sort_order": index},
            )
            act_ids.append(act.id)
        if act_ids:
            ApplicationCharge.objects.filter(
                act_lines__act_id__in=act_ids,
                is_included_in_invoice=False,
            ).update(is_included_in_invoice=True, updated_at=timezone.now())

    @staticmethod
    def _sync_applications_for_invoice(invoice: ClientInvoice, *, billing_status: str | None = None):
        apps = list(invoice.linked_applications())
        if not apps and invoice.application_id:
            apps = [invoice.application]
        paid_ratio = Decimal("0")
        if invoice.total_amount and invoice.total_amount > 0:
            paid_ratio = quantize_money(invoice.paid_amount) / quantize_money(invoice.total_amount)
        for application in apps:
            act_ids = list(
                ClientInvoiceAct.objects.filter(invoice=invoice, act__application=application).values_list(
                    "act_id", flat=True
                )
            )
            if not act_ids and invoice.act_id and invoice.application_id == application.id:
                act_ids = [invoice.act_id]
            share_total = (
                BillingAct.objects.filter(pk__in=act_ids).aggregate(total=Sum("total_amount")).get("total")
                or Decimal("0")
            )
            share_total = quantize_money(share_total)
            share_paid = quantize_money(share_total * paid_ratio) if share_total else Decimal("0")
            if share_paid > share_total:
                share_paid = share_total
            application.invoice_total = share_total
            application.paid_total = share_paid
            application.debt_total = quantize_money(max(share_total - share_paid, Decimal("0")))
            fields = ["invoice_total", "paid_total", "debt_total", "updated_at"]
            if billing_status:
                application.billing_status = billing_status
                fields.append("billing_status")
            application.save(update_fields=fields)

    @staticmethod
    @transaction.atomic
    def create_invoice(application: BillingApplication, *, act=None, due_date=None, invoice_type=ClientInvoice.TYPE_REGULAR, user=None):
        act = act or application.acts.filter(status=ActStatus.CONFIRMED).order_by("-confirmed_at", "-id").first()
        if not act or act.status != ActStatus.CONFIRMED:
            raise ValidationError("Счет можно создать только по подтвержденному акту.")
        return BillingWorkflowService.create_grouped_invoice(
            acts=[act],
            due_date=due_date,
            invoice_type=invoice_type,
            user=user,
        )

    @staticmethod
    @transaction.atomic
    def create_grouped_invoice(
        *,
        acts,
        due_date=None,
        invoice_type=ClientInvoice.TYPE_REGULAR,
        user=None,
        allow_unconfirmed_acts=False,
    ):
        act_list = list(acts)
        if not act_list:
            raise ValidationError("Выберите хотя бы один подтверждённый акт.")
        # Preserve order, dedupe
        seen = set()
        ordered = []
        for act in act_list:
            if act.id in seen:
                continue
            seen.add(act.id)
            ordered.append(act)
        act_list = ordered

        client_id = act_list[0].client_id
        legal_entity_id = act_list[0].legal_entity_id
        applications = []
        for act in act_list:
            auto_document_act = allow_unconfirmed_acts and act.status == ActStatus.DRAFT
            if act.status != ActStatus.CONFIRMED and not auto_document_act:
                raise ValidationError(f"Акт {act.number} не подтверждён клиентом.")
            if act.client_id != client_id:
                raise ValidationError("В один счёт можно объединить только акты одного клиента.")
            if act.legal_entity_id != legal_entity_id:
                raise ValidationError("В один счёт можно объединить только акты одного юрлица клиента.")
            existing = BillingWorkflowService.active_invoice_for_act(act)
            if existing:
                # Один акт → идемпотентный повтор create_invoice
                if len(act_list) == 1:
                    return existing
                raise ValidationError(f"Акт {act.number} уже входит в счёт {existing.number}.")
            applications.append(act.application)

        contours = {
            ClientInvoice.CONTOUR_FBS
            if app.application_type == BillingApplication.TYPE_FBS
            else ClientInvoice.CONTOUR_FBO
            for app in applications
        }
        if len(contours) != 1:
            raise ValidationError("В один счет нельзя объединять услуги FBS и FBO.")
        billing_contour = next(iter(contours))

        BillingWorkflowService._assert_client_ready_for_invoice(act_list[0].client, applications)

        invoice_date = max(act.act_date for act in act_list)
        billing_period = invoice_date.replace(day=1)
        primary = act_list[0]
        application = primary.application

        # Idempotent: same single-act create for this app/period
        if len(act_list) == 1:
            existing = ClientInvoice.objects.filter(
                application=application,
                legal_entity=application.legal_entity,
                billing_period=billing_period,
                invoice_type=invoice_type,
            ).exclude(status=InvoiceStatus.CANCELLED).first()
            if existing:
                BillingWorkflowService._link_acts_to_invoice(existing, act_list)
                return existing

        # Multi-act: block if any linked application already has an active invoice for period as primary
        app_ids = {a.application_id for a in act_list}
        conflict = (
            ClientInvoice.objects.filter(
                application_id__in=app_ids,
                legal_entity_id=legal_entity_id,
                billing_period=billing_period,
                invoice_type=invoice_type,
            )
            .exclude(status=InvoiceStatus.CANCELLED)
            .first()
        )
        if conflict:
            raise ValidationError(
                f"По заявке уже есть активный счёт {conflict.number} за период "
                f"{billing_period.strftime('%m.%Y')}."
            )

        subtotal = quantize_money(sum((act.subtotal for act in act_list), Decimal("0")))
        vat_amount = quantize_money(sum((act.vat_amount for act in act_list), Decimal("0")))
        total_amount = quantize_money(sum((act.total_amount for act in act_list), Decimal("0")))
        due_date = due_date or (invoice_date + timedelta(days=7))

        supplier_snapshot, vat_rate_snapshot = BillingWorkflowService._invoice_supplier_snapshot(act_list)
        invoice = ClientInvoice.objects.create(
            application=application,
            client=primary.client,
            legal_entity=primary.legal_entity,
            act=primary,
            number=BillingWorkflowService.next_number(SequenceKind.INVOICE, date=invoice_date),
            invoice_type=invoice_type,
            billing_contour=billing_contour,
            billing_period=billing_period,
            invoice_date=invoice_date,
            due_date=due_date,
            subtotal=subtotal,
            vat_amount=vat_amount,
            total_amount=total_amount,
            debt_amount=total_amount,
            status=InvoiceStatus.DRAFT,
            supplier_snapshot=supplier_snapshot,
            vat_rate_snapshot=vat_rate_snapshot,
            created_by=user,
        )
        BillingWorkflowService._link_acts_to_invoice(invoice, act_list)
        BillingWorkflowService._sync_applications_for_invoice(invoice, billing_status=BillingStatus.INVOICE_DRAFT)
        BillingWorkflowService.audit(
            action="invoice_created",
            application=application,
            user=user,
            obj=invoice,
            new_value={"act_ids": [a.id for a in act_list], "acts_count": len(act_list)},
        )
        return invoice

    @staticmethod
    @transaction.atomic
    def check_invoice(invoice: ClientInvoice, *, user=None):
        invoice.checked_by = user
        invoice.status = InvoiceStatus.ISSUED
        invoice.save(update_fields=["checked_by", "status", "updated_at"])
        BillingWorkflowService._sync_applications_for_invoice(invoice, billing_status=BillingStatus.INVOICE_ISSUED)
        BillingWorkflowService.audit(action="invoice_checked", application=invoice.application, user=user, obj=invoice)
        return invoice

    @staticmethod
    @transaction.atomic
    def send_invoice(invoice: ClientInvoice, *, user=None):
        if invoice.status not in {InvoiceStatus.DRAFT, InvoiceStatus.ISSUED}:
            raise ValidationError("Отправить можно только черновой или проверенный счет.")
        invoice.status = InvoiceStatus.SENT
        invoice.sent_at = timezone.now()
        invoice.save(update_fields=["status", "sent_at", "updated_at"])
        BillingWorkflowService._sync_applications_for_invoice(invoice, billing_status=BillingStatus.INVOICE_SENT)
        BillingWorkflowService.audit(action="invoice_sent", application=invoice.application, user=user, obj=invoice)
        return invoice

    @staticmethod
    @transaction.atomic
    def register_payment(
        invoice: ClientInvoice,
        *,
        amount,
        paid_at=None,
        source="manual",
        comment="",
        user=None,
        external_id="",
    ):
        amount = quantize_money(amount)
        if amount <= 0:
            raise ValidationError("Сумма оплаты должна быть больше нуля.")
        invoice = ClientInvoice.objects.select_for_update().get(pk=invoice.pk)
        external_id = str(external_id or "").strip()
        if external_id:
            existing = InvoicePayment.objects.filter(invoice=invoice, external_id=external_id).first()
            if existing:
                if quantize_money(existing.amount) != amount:
                    raise ValidationError("Повтор оплаты с тем же внешним ID содержит другую сумму.")
                return existing
        payment = InvoicePayment.objects.create(
            invoice=invoice,
            amount=amount,
            paid_at=paid_at or timezone.now(),
            status=PaymentStatus.CONFIRMED,
            source=source,
            external_id=external_id,
            comment=comment,
            registered_by=user,
        )
        BillingWorkflowService.recalculate_invoice(invoice)
        BillingWorkflowService.audit(action="payment_registered", application=invoice.application, user=user, obj=payment)
        try:
            from .models import BillingStaffNotification
            from .staff_notifications import notify_client_manager

            notify_client_manager(
                invoice.client,
                kind=BillingStaffNotification.KIND_PAYMENT,
                title=f"Оплата по счёту {invoice.number}",
                message=f"Поступило {payment.amount}",
                link_url=f"/team-manager/billing/invoices/{invoice.id}/",
                source_key=f"payment-registered:{payment.id}",
                actor=user,
            )
        except Exception:
            pass
        return payment

    @staticmethod
    @transaction.atomic
    def close_financially(application: BillingApplication, *, user=None):
        application.refresh_from_db()
        if application.debt_total > 0:
            raise ValidationError("Финансовое закрытие возможно только после полной оплаты.")
        if not application.invoices.exists():
            raise ValidationError("Для финансового закрытия нужен счет.")
        application.billing_status = BillingStatus.FINANCIALLY_CLOSED
        application.is_financially_closed = True
        application.financially_closed_at = timezone.now()
        application.save(update_fields=["billing_status", "is_financially_closed", "financially_closed_at", "updated_at"])
        BillingWorkflowService.audit(action="financially_closed", application=application, user=user, obj=application)
        return application

    @staticmethod
    def _manager_task_routes_for_application(application: BillingApplication) -> list[str]:
        """Маршруты менеджерских Task по заявке (не складские MoveTask)."""
        app_type = str(application.application_type or "")
        app_id = str(application.application_id or "").strip()
        if not app_id:
            return []
        routes: list[str] = []
        if app_type == BillingApplication.TYPE_SHIPPING:
            try:
                from shipping.models import ShippingOrder

                order = (
                    ShippingOrder.objects.filter(number=app_id)
                    .only("pk")
                    .first()
                )
                if order is None and app_id.isdigit():
                    order = ShippingOrder.objects.filter(pk=int(app_id)).only("pk").first()
                if order is not None:
                    routes.append(f"/shipping/{order.pk}/")
            except Exception:
                pass
        elif app_type == BillingApplication.TYPE_LOGISTICS:
            try:
                from logistics.models import LogisticsTrip

                trip = LogisticsTrip.objects.filter(number=app_id, trip_kind=LogisticsTrip.KIND_EXTERNAL).only("pk").first()
                if trip is not None:
                    routes.append(f"/logistics/trips/external/{trip.pk}/")
            except Exception:
                pass
        elif app_type == BillingApplication.TYPE_RECEIVING:
            routes.extend(
                [
                    f"/orders/receiving/{app_id}/",
                    f"/orders/receiving/{app_id}/act/print/",
                ]
            )
        elif app_type == BillingApplication.TYPE_PROCESSING:
            routes.append(f"/orders/processing/{app_id}/")
        elif app_type == BillingApplication.TYPE_OTHER:
            routes.extend(
                [
                    f"/team-manager/other-requests/{app_id}/",
                    f"/orders/other/{app_id}/",
                ]
            )
        return routes

    @staticmethod
    def _close_manager_tasks_for_application(application: BillingApplication) -> int:
        from todo.models import Task

        routes = BillingWorkflowService._manager_task_routes_for_application(application)
        if not routes:
            return 0
        return (
            Task.objects.filter(
                route__in=routes,
                assigned_to__role__in=["manager", "head_manager", "logistician"],
            )
            .exclude(status="done")
            .update(status="done")
        )

    @staticmethod
    @transaction.atomic
    def mark_billed_outside(application: BillingApplication, *, user=None, comment: str = ""):
        """
        Пометить биллинг как «счета вне системы / не выставлять».
        Не меняет склад и статусы операционной заявки.
        """
        application.refresh_from_db()
        if application.billing_status == BillingStatus.CANCELLED:
            return application
        if application.is_financially_closed or application.billing_status == BillingStatus.FINANCIALLY_CLOSED:
            raise ValidationError("Заявка уже финансово закрыта — пометка «вне системы» недоступна.")
        if application.billing_status == BillingStatus.PAID and application.debt_total <= 0:
            has_live_invoice = (
                application.invoices.exclude(status=InvoiceStatus.CANCELLED).exists()
            )
            if has_live_invoice:
                raise ValidationError(
                    "По заявке уже есть оплаченный счёт в системе. Используйте финансовое закрытие."
                )
        if application.billing_status == BillingStatus.PARTIALLY_PAID:
            raise ValidationError(
                "По заявке есть частичная оплата в системе. Сначала разберите оплату, затем закройте."
            )

        old_status = application.billing_status
        now = timezone.now()
        payload = dict(application.source_payload or {})
        payload["billing_external"] = {
            "reason": "billed_outside",
            "at": now.isoformat(),
            "by": getattr(user, "id", None),
            "by_username": getattr(user, "username", "") or "",
            "comment": (comment or "").strip()[:500],
            "previous_billing_status": old_status,
        }
        application.billing_status = BillingStatus.CANCELLED
        application.source_payload = payload
        application.save(update_fields=["billing_status", "source_payload", "updated_at"])
        BillingWorkflowService.audit(
            action="billing_cancelled_external",
            application=application,
            user=user,
            obj=application,
            old_value={"billing_status": old_status},
            new_value={"billing_status": BillingStatus.CANCELLED},
            comment=(comment or "").strip()[:500],
        )
        BillingWorkflowService._close_manager_tasks_for_application(application)
        return application

    @staticmethod
    def recalculate_application_totals(application: BillingApplication):
        charges_total = (
            application.charges.filter(is_excluded=False).aggregate(total=Sum("total_amount")).get("total")
            or Decimal("0")
        )
        invoices = BillingWorkflowService.invoices_for_application(application)
        invoice_total = Decimal("0")
        paid_total = Decimal("0")
        debt_total = Decimal("0")
        for invoice in invoices:
            act_ids = list(
                ClientInvoiceAct.objects.filter(invoice=invoice, act__application=application).values_list(
                    "act_id", flat=True
                )
            )
            if not act_ids and invoice.application_id == application.id and invoice.act_id:
                act_ids = [invoice.act_id]
            share = (
                BillingAct.objects.filter(pk__in=act_ids).aggregate(total=Sum("total_amount")).get("total")
                or Decimal("0")
            )
            share = quantize_money(share)
            if invoice.total_amount and invoice.total_amount > 0:
                ratio = quantize_money(invoice.paid_amount) / quantize_money(invoice.total_amount)
                share_paid = quantize_money(share * ratio)
            else:
                share_paid = Decimal("0")
            if share_paid > share:
                share_paid = share
            invoice_total += share
            paid_total += share_paid
            debt_total += quantize_money(max(share - share_paid, Decimal("0")))
        application.charges_total = quantize_money(charges_total)
        application.invoice_total = quantize_money(invoice_total)
        application.paid_total = quantize_money(paid_total)
        application.debt_total = quantize_money(debt_total)
        application.save(update_fields=["charges_total", "invoice_total", "paid_total", "debt_total", "updated_at"])

    @staticmethod
    def recalculate_invoice(invoice: ClientInvoice):
        paid_total = invoice.payments.filter(status=PaymentStatus.CONFIRMED).aggregate(total=Sum("amount")).get("total") or Decimal("0")
        invoice.paid_amount = quantize_money(paid_total)
        invoice.debt_amount = quantize_money(max(invoice.total_amount - invoice.paid_amount, Decimal("0")))
        billing_status = None
        if invoice.debt_amount == 0:
            invoice.status = InvoiceStatus.PAID
            invoice.paid_at = timezone.now()
            billing_status = BillingStatus.PAID
        elif invoice.paid_amount > 0:
            invoice.status = InvoiceStatus.PARTIALLY_PAID
            billing_status = BillingStatus.PARTIALLY_PAID
        invoice.save(update_fields=["paid_amount", "debt_amount", "status", "paid_at", "updated_at"])
        if billing_status:
            BillingWorkflowService._sync_applications_for_invoice(invoice, billing_status=billing_status)
        else:
            BillingWorkflowService._sync_applications_for_invoice(invoice)
        for application in invoice.linked_applications():
            BillingWorkflowService.recalculate_application_totals(application)

    @staticmethod
    def create_billing_notification(agency, message: str, *, source_key: str):
        try:
            from client_cabinet.messaging_lk import create_client_notification
            from client_cabinet.models import ClientNotification
        except ImportError:
            return None
        return create_client_notification(
            agency,
            title="Биллинг и счета",
            message=message,
            notif_type=ClientNotification.TYPE_SYSTEM,
            source_key=source_key,
        )


def models_valid_to_filter(on_date):
    from django.db.models import Q

    return Q(valid_to__isnull=True) | Q(valid_to__gte=on_date)
