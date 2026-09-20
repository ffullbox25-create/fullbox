from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.utils import timezone

from head_manager.models import OwnCompany

from .models import BillingStorageDay, ClientInvoice
from .money_words import money_to_words
from .payment_qr import build_payment_qr_for_invoice
from .statuses import InvoiceStatus

_MONTHS_GENITIVE = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


def _fmt_money(value) -> str:
    amount = Decimal(str(value or "0")).quantize(Decimal("0.01"))
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",")


def _fmt_qty(value) -> str:
    amount = Decimal(str(value or "0"))
    if amount == amount.to_integral_value():
        return str(int(amount))
    return f"{amount.normalize()}".replace(".", ",")


def _date_long(value: date | None) -> str:
    if not value:
        return "—"
    return f"{value.day} {_MONTHS_GENITIVE[value.month]} {value.year} г."


def _supplier_for_invoice(invoice: ClientInvoice) -> OwnCompany | None:
    snapshot = getattr(invoice, "supplier_snapshot", None) or {}
    if snapshot:
        return SimpleNamespace(**snapshot)
    try:
        from billing.models import ClientBillingContract

        active = (
            ClientBillingContract.objects.filter(client=invoice.client, is_active=True)
            .select_related("own_company")
            .order_by("-valid_from", "-id")
            .first()
        )
        if active and active.own_company_id:
            return active.own_company
    except Exception:
        pass
    try:
        lifecycle = getattr(invoice.client, "lifecycle", None)
        if lifecycle and lifecycle.serving_company_id:
            return lifecycle.serving_company
    except Exception:
        pass
    return OwnCompany.objects.filter(is_active=True, is_default=True).first() or OwnCompany.objects.filter(is_active=True).first()


def _buyer_block(agency) -> str:
    parts = [agency.agn_name or agency.short_name or f"Клиент #{agency.id}"]
    if agency.inn:
        parts[0] = f"{parts[0]}, ИНН {agency.inn}"
    address = (
        getattr(agency, "legal_address", None)
        or getattr(agency, "address", None)
        or getattr(agency, "fact_address", None)
        or ""
    )
    if address:
        parts.append(str(address).strip())
    phone = getattr(agency, "phone", None) or getattr(agency, "contact_phone", None) or ""
    if phone:
        parts.append(f"тел.: {phone}")
    return ", ".join(p for p in parts if p)


def _supplier_block(company: OwnCompany | None) -> str:
    if not company:
        return "FULLBOX"
    parts = [company.name or company.short_name or "FULLBOX"]
    if company.inn:
        parts[0] = f"{parts[0]}, ИНН {company.inn}"
    if company.kpp:
        parts[0] = f"{parts[0]}, КПП {company.kpp}"
    if company.address:
        parts.append(company.address.strip())
    if company.phone:
        parts.append(f"тел.: {company.phone}")
    return ", ".join(p for p in parts if p)



def _storage_comment_for_acts(acts) -> str:
    """Build a human-readable storage period from billed daily snapshots."""
    storage_charge_ids = {
        line.charge_id
        for act in acts
        for line in act.lines.all()
        if line.charge_id and line.charge.source_type == line.charge.SOURCE_STORAGE_DAY
    }
    if not storage_charge_ids:
        return ""
    days = list(
        BillingStorageDay.objects.filter(charge_id__in=storage_charge_ids)
        .order_by("day", "id")
        .only("day", "pallet_count")
    )
    if not days:
        return ""
    period_start, period_end = days[0].day, days[-1].day
    pallet_days = sum(day.pallet_count for day in days)
    by_day = "; ".join(
        f"{day.day:%d.%m.%Y} — {day.pallet_count} пал."
        for day in days
    )
    return (
        "Хранение товара: "
        f"период с {period_start:%d.%m.%Y} по {period_end:%d.%m.%Y}; "
        f"дней хранения в счёте: {len(days)}; всего: {pallet_days} палето-дней. "
        f"По дням: {by_day}."
    )

def _build_sections(invoice: ClientInvoice, supplier) -> tuple[list[dict], list[dict], str, str]:
    """Секции расшифровки по заявкам/актам + плоский список строк для итогов."""
    acts = list(invoice.get_linked_acts())
    vat_rate = "0"
    sections = []
    flat_lines = []
    line_index = 1
    comments = []

    storage_comment = _storage_comment_for_acts(acts)
    if storage_comment:
        comments.append(storage_comment)

    for act in acts:
        application = act.application
        raw_lines = list(act.lines.all())
        section_lines = []
        for line in raw_lines:
            charge = line.charge if line.charge_id else None
            rate = getattr(charge, "vat_rate", None) if charge else None
            if rate:
                vat_rate = str(rate).rstrip("%") or vat_rate
            performed_date = None
            if charge and charge.source_type == charge.SOURCE_STORAGE_DAY and charge.source_id:
                try:
                    performed_date = date.fromisoformat(charge.source_id)
                except ValueError:
                    performed_date = None
            if performed_date is None and charge and charge.performed_at:
                performed_at = charge.performed_at
                if timezone.is_aware(performed_at):
                    performed_at = timezone.localtime(performed_at)
                performed_date = performed_at.date()
            if performed_date is None and charge:
                performed_date = charge.billing_period
            row = {
                "index": line_index,
                "charge_id": line.charge_id,
                "article": (charge.service.code if charge and charge.service_id else "") or "",
                "name": line.service_name,
                "date_fmt": performed_date.strftime("%d.%m.%Y") if performed_date else "—",
                "qty": _fmt_qty(line.quantity),
                "quantity_value": str(line.quantity),
                "unit": line.unit or "шт",
                "price": _fmt_money(line.tariff),
                "price_value": str(line.tariff),
                "sum": _fmt_money(line.total_amount),
                "edit_version": int(charge.edit_version or 1) if charge else 1,
                "can_edit_quantity": bool(
                    charge
                    and charge.source_type == charge.SOURCE_STORAGE_DAY
                    and charge.service.code == "storage_m3_day"
                ),
                "can_update_client_tariff": bool(
                    charge and charge.client_tariff_item_id
                ),
            }
            section_lines.append(row)
            flat_lines.append(row)
            line_index += 1
        app_label = (
            f"№{application.application_id} ({application.get_application_type_display()})"
            if application
            else "—"
        )
        sections.append(
            {
                "application_id": application.application_id if application else "",
                "application_type": application.get_application_type_display() if application else "",
                "application_type_code": application.application_type if application else "other",
                "application_label": app_label,
                "application_pk": application.id if application else None,
                "act_number": act.number,
                "act_date": act.act_date,
                "act_date_fmt": act.act_date.strftime("%d.%m.%Y") if act.act_date else "—",
                "act_total_fmt": _fmt_money(act.total_amount),
                "lines": section_lines,
                "lines_count": len(section_lines),
            }
        )
        if act.manager_comment:
            comments.append(f"Акт {act.number}: {act.manager_comment.strip()}")

    if supplier and getattr(supplier, "tax_mode", "") == OwnCompany.TAX_MODE_NO_VAT:
        vat_rate = "0"
    elif supplier and str(getattr(supplier, "vat_rate", "") or ""):
        vat_rate = str(supplier.vat_rate).rstrip("%")

    return sections, flat_lines, vat_rate, "\n".join(comments)


def build_invoice_print_context(invoice: ClientInvoice) -> dict:
    supplier = _supplier_for_invoice(invoice)
    sections, lines, vat_rate, comment = _build_sections(invoice, supplier)

    number_digits = "".join(ch for ch in str(invoice.number or "") if ch.isdigit()) or str(invoice.id)
    purpose_code = f"{int(number_digits):010d}"[-10:]

    app_numbers = []
    for section in sections:
        if section["application_id"]:
            app_numbers.append(str(section["application_id"]))
    if len(app_numbers) > 1:
        payment_purpose = f"Оплата по счету №{invoice.number} (заявки: {', '.join(app_numbers)})"
    elif app_numbers:
        payment_purpose = f"Оплата по заказу клиента №{app_numbers[0]}"
    else:
        payment_purpose = f"Оплата по счету №{invoice.number}"

    payment_qr_payload = build_payment_qr_for_invoice(
        supplier=supplier,
        invoice=invoice,
        purpose=payment_purpose,
    )
    return {
        "invoice": invoice,
        "act": invoice.act,
        "sections": sections,
        "is_grouped": len(sections) > 1,
        "supplier": supplier,
        "supplier_block": _supplier_block(supplier),
        "buyer_block": _buyer_block(invoice.client),
        "lines": lines,
        "invoice_title": f"Счет на оплату № {invoice.number} от {_date_long(invoice.invoice_date)}",
        "valid_until": invoice.due_date,
        "valid_until_fmt": invoice.due_date.strftime("%d.%m.%Y") if invoice.due_date else "—",
        "payment_purpose": payment_purpose,
        "payment_qr_payload": payment_qr_payload,
        "purpose_code": purpose_code,
        "vat_rate": vat_rate,
        "subtotal_fmt": _fmt_money(invoice.subtotal),
        "vat_fmt": _fmt_money(invoice.vat_amount),
        "total_fmt": _fmt_money(invoice.total_amount),
        "total_words": money_to_words(invoice.total_amount),
        "lines_count": len(lines),
        "director_name": (supplier.director_name if supplier else "") or "Опря С.Н.",
        "accountant_name": (supplier.director_name if supplier else "") or "Опря С.Н.",
        "comment": comment,
        "generated_at": timezone.localtime(),
    }


class _EmptySupplier:
    name = "FULLBOX"
    short_name = "FULLBOX"
    inn = ""
    kpp = ""
    address = ""
    phone = ""
    bank_name = ""
    bank_address = ""
    bank_bik = ""
    settlement_account = ""
    correspondent_account = ""
    director_name = "Опря С.Н."
    tax_mode = ""
    vat_rate = "0"


def render_invoice_print_response(invoice: ClientInvoice, *, as_attachment: bool = False) -> HttpResponse:
    ctx = build_invoice_print_context(invoice)
    if not ctx.get("supplier"):
        ctx["supplier"] = _EmptySupplier()
    html = render_to_string("billing/invoice_print.html", ctx)
    response = HttpResponse(html, content_type="text/html; charset=utf-8")
    filename = f"invoice_{invoice.number}.html".replace("/", "-")
    disposition = "attachment" if as_attachment else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    return response


def get_printable_invoice(pk: int) -> ClientInvoice:
    return get_object_or_404(
        ClientInvoice.objects.select_related(
            "client",
            "legal_entity",
            "act",
            "application",
            "application__client",
        ).prefetch_related(
            "invoice_acts__act__application",
            "invoice_acts__act__lines__charge__service",
            "act__lines__charge__service",
        ),
        pk=pk,
    )


def client_can_view_invoice(invoice: ClientInvoice) -> bool:
    return invoice.status not in {InvoiceStatus.CANCELLED, InvoiceStatus.DRAFT}
