"""Отчёты менеджера по биллингу (этап 7)."""
from __future__ import annotations

import csv
import io
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date

from sku.models import Agency

from .models import (
    ApplicationCharge,
    BillingAct,
    BillingApplication,
    BillingDiscrepancy,
    BillingStorageDay,
    ClientInvoice,
    InvoicePayment,
    PaymentPromise,
)
from .permissions import filter_agencies_for_user, filter_applications_for_user
from .statuses import (
    ActStatus,
    BillingStatus,
    DiscrepancyStatus,
    DocumentReviewStatus,
    InvoiceStatus,
    PaymentPromiseStatus,
    PaymentStatus,
)


REPORT_CATALOG = [
    ("charges_by_client", "Начисления по клиентам"),
    ("charges_by_service", "Начисления по услугам"),
    ("general_charges_by_service", "Общие тарифы: FBO и склад"),
    ("logistics_charges_by_service", "Логистика по клиенту"),
    ("accrued_not_invoiced", "Начислено, но не выставлено"),
    ("invoiced_unpaid", "Выставлено, но не оплачено"),
    ("docs_on_review", "Документы на проверке"),
    ("no_price", "Услуги / начисления без цены"),
    ("apps_without_charges", "Заявки без начислений"),
    ("revenue_by_client", "Выручка по клиентам"),
    ("payments", "Оплаты"),
    ("debt", "Дебиторская задолженность"),
    ("overdue", "Просроченная задолженность"),
    ("promises", "Обещания оплаты"),
    ("discrepancies", "Расхождения"),
    ("storage_by_client", "Хранение по клиентам"),
    ("fbs_period", "FBS за период"),
]


def _parse_filters(params) -> dict:
    report = (params.get("report") or "charges_by_client").strip()
    period_from = parse_date(str(params.get("period_from") or "")) or None
    period_to = parse_date(str(params.get("period_to") or "")) or None
    if report == "fbs_period" and (period_from is None or period_to is None):
        today = timezone.localdate()
        period_to = today - timedelta(days=today.weekday() + 1)
        period_from = period_to - timedelta(days=6)
    return {
        "client_id": (params.get("client") or params.get("client_id") or "").strip(),
        "period_from": period_from,
        "period_to": period_to,
        "report": report,
    }


def _app_qs(user_or_request, filters: dict):
    qs = filter_applications_for_user(BillingApplication.objects.all(), user_or_request).distinct()
    if filters["client_id"]:
        qs = qs.filter(client_id=filters["client_id"])
    if filters["period_from"]:
        qs = qs.filter(created_at_source__date__gte=filters["period_from"])
    if filters["period_to"]:
        qs = qs.filter(created_at_source__date__lte=filters["period_to"])
    return qs


def _charge_qs(user_or_request, filters: dict):
    app_qs = filter_applications_for_user(BillingApplication.objects.all(), user_or_request).distinct()
    qs = ApplicationCharge.objects.filter(application__in=app_qs)
    if filters["client_id"]:
        qs = qs.filter(client_id=filters["client_id"])
    if filters["period_from"]:
        qs = qs.filter(
            Q(performed_at__date__gte=filters["period_from"]) | Q(created_at__date__gte=filters["period_from"])
        )
    if filters["period_to"]:
        qs = qs.filter(
            Q(performed_at__date__lte=filters["period_to"]) | Q(created_at__date__lte=filters["period_to"])
        )
    return qs


def _invoice_qs(user_or_request, filters: dict):
    app_qs = filter_applications_for_user(BillingApplication.objects.all(), user_or_request).distinct()
    qs = ClientInvoice.objects.filter(application__in=app_qs).exclude(status=InvoiceStatus.CANCELLED)
    if filters["client_id"]:
        qs = qs.filter(client_id=filters["client_id"])
    if filters["period_from"]:
        qs = qs.filter(invoice_date__gte=filters["period_from"])
    if filters["period_to"]:
        qs = qs.filter(invoice_date__lte=filters["period_to"])
    return qs


def build_report(user_or_request, params) -> dict:
    filters = _parse_filters(params)
    report = filters["report"]
    if report not in {r[0] for r in REPORT_CATALOG}:
        report = "charges_by_client"
        filters["report"] = report

    builders = {
        "charges_by_client": _rep_charges_by_client,
        "charges_by_service": _rep_charges_by_service,
        "general_charges_by_service": _rep_general_charges_by_service,
        "logistics_charges_by_service": _rep_logistics_charges_by_service,
        "accrued_not_invoiced": _rep_accrued_not_invoiced,
        "invoiced_unpaid": _rep_invoiced_unpaid,
        "docs_on_review": _rep_docs_on_review,
        "no_price": _rep_no_price,
        "apps_without_charges": _rep_apps_without_charges,
        "revenue_by_client": _rep_revenue_by_client,
        "payments": _rep_payments,
        "debt": _rep_debt,
        "overdue": _rep_overdue,
        "promises": _rep_promises,
        "discrepancies": _rep_discrepancies,
        "storage_by_client": _rep_storage_by_client,
        "fbs_period": _rep_fbs_period,
    }
    report_notice = ""
    if report == "fbs_period":
        columns, rows, totals, report_notice = builders[report](user_or_request, filters)
    else:
        columns, rows, totals = builders[report](user_or_request, filters)
    label = dict(REPORT_CATALOG).get(report, report)
    portfolio = filter_agencies_for_user(Agency.objects.all(), user_or_request).order_by("agn_name")[:500]
    return {
        "report": report,
        "report_label": label,
        "catalog": REPORT_CATALOG,
        "columns": columns,
        "rows": rows,
        "totals": totals,
        "filters": filters,
        "report_clients": portfolio,
        "report_notice": report_notice,
        "is_fbs_period": report == "fbs_period",
        "today": timezone.localdate(),
    }


def _rep_fbs_period(user_or_request, filters):
    columns = ["Операция", "Диапазон", "Количество", "Тариф", "Сумма без НДС"]
    if not filters["client_id"]:
        return columns, [], None, "Выберите клиента — FBS рассчитывается только по его индивидуальным тарифам."
    client = (
        filter_agencies_for_user(Agency.objects.all(), user_or_request)
        .filter(pk=filters["client_id"])
        .first()
    )
    if client is None:
        return columns, [], None, "Клиент не найден или недоступен текущему пользователю."
    if not filters["period_from"] or not filters["period_to"]:
        return columns, [], None, "Укажите начало и окончание периода FBS."
    try:
        from .fbs_period_billing import collect_period_data

        data = collect_period_data(
            client=client,
            date_from=filters["period_from"],
            date_to=filters["period_to"],
        )
    except ValidationError as exc:
        message = "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)
        return columns, [], None, message
    rows = [
        {
            "Операция": group["operation_label"],
            "Диапазон": group["rate_label"],
            "Количество": f'{Decimal(group["quantity"]):.3f}',
            "Тариф": f'{Decimal(group["rate"].price):.4f}',
            "Сумма без НДС": _money(group["amount"]),
        }
        for group in data["groups"]
    ]
    totals = {
        "Операция": f'Итого: {data["orders_count"]} заказов, {data["quantity"]} шт.',
        "Диапазон": "",
        "Количество": "",
        "Тариф": "",
        "Сумма без НДС": _money(data["subtotal"]),
    }
    vat_caption = data.get("client_vat", {}).get("caption") or "Без НДС"
    if vat_caption == "Без НДС":
        amount_notice = f'Без НДС; итого {data["total_amount"]} ₽. '
    else:
        amount_notice = (
            f'{vat_caption}: НДС {data["vat_amount"]} ₽; '
            f'итого {data["total_amount"]} ₽. '
        )
    notice = amount_notice + (
        "Предпросмотр ничего не записывает. Кнопка формирования создаёт один черновик счёта и Excel."
    )
    return columns, rows, totals, notice


def report_to_csv(payload: dict) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(payload["columns"])
    for row in payload["rows"]:
        writer.writerow([row.get(col, "") for col in payload["columns"]])
    if payload.get("totals"):
        writer.writerow([])
        writer.writerow(["ИТОГО"] + [payload["totals"].get(c, "") for c in payload["columns"][1:]])
    return buf.getvalue()


def _money(v) -> str:
    if v is None:
        return "0.00"
    return f"{Decimal(v):.2f}"


def _rep_charges_by_client(user_or_request, filters):
    qs = (
        _charge_qs(user_or_request, filters)
        .values("client_id", "client__short_name", "client__agn_name")
        .annotate(lines=Count("id"), amount=Sum("total_amount"))
        .order_by("-amount")
    )
    columns = ["Клиент", "Строк", "Сумма"]
    rows = []
    total = Decimal("0")
    for r in qs[:500]:
        name = r["client__short_name"] or r["client__agn_name"] or str(r["client_id"])
        amt = r["amount"] or Decimal("0")
        total += amt
        rows.append({"Клиент": name, "Строк": r["lines"], "Сумма": _money(amt)})
    return columns, rows, {"Клиент": "", "Строк": sum(int(x["Строк"]) for x in rows), "Сумма": _money(total)}


def _charges_by_service_payload(qs):
    qs = (
        qs
        .values("service__code", "service__name", "service_name_snapshot")
        .annotate(lines=Count("id"), amount=Sum("total_amount"), qty=Sum("quantity"))
        .order_by("-amount")
    )
    columns = ["Услуга", "Код", "Кол-во", "Строк", "Сумма"]
    rows = []
    total = Decimal("0")
    for r in qs[:500]:
        name = r["service__name"] or r["service_name_snapshot"] or "—"
        amt = r["amount"] or Decimal("0")
        total += amt
        rows.append(
            {
                "Услуга": name,
                "Код": r["service__code"] or "",
                "Кол-во": _money(r["qty"] or 0),
                "Строк": r["lines"],
                "Сумма": _money(amt),
            }
        )
    return columns, rows, {"Услуга": "", "Код": "", "Кол-во": "", "Строк": "", "Сумма": _money(total)}


def _rep_charges_by_service(user_or_request, filters):
    return _charges_by_service_payload(_charge_qs(user_or_request, filters))


def _rep_general_charges_by_service(user_or_request, filters):
    qs = _charge_qs(user_or_request, filters).exclude(
        application__application_type__in=(
            BillingApplication.TYPE_LOGISTICS,
            BillingApplication.TYPE_FBS,
        )
    )
    return _charges_by_service_payload(qs)


def _rep_logistics_charges_by_service(user_or_request, filters):
    qs = _charge_qs(user_or_request, filters).filter(
        Q(application__application_type=BillingApplication.TYPE_LOGISTICS)
        | Q(client_logistics_tariff__isnull=False)
    )
    return _charges_by_service_payload(qs.distinct())


def _rep_accrued_not_invoiced(user_or_request, filters):
    qs = (
        _charge_qs(user_or_request, filters)
        .filter(is_included_in_invoice=False)
        .exclude(application__billing_status=BillingStatus.CANCELLED)
        .select_related("client", "application", "service")
        .order_by("-created_at")[:500]
    )
    columns = ["Клиент", "Заявка", "Услуга", "Сумма"]
    rows = []
    total = Decimal("0")
    for c in qs:
        amt = c.total_amount or Decimal("0")
        total += amt
        rows.append(
            {
                "Клиент": c.client.short_name or c.client.agn_name,
                "Заявка": c.application.application_id,
                "Услуга": getattr(c.service, "name", None) or c.service_name_snapshot or "—",
                "Сумма": _money(amt),
            }
        )
    return columns, rows, {"Клиент": "", "Заявка": "", "Услуга": "", "Сумма": _money(total)}


def _rep_invoiced_unpaid(user_or_request, filters):
    qs = _invoice_qs(user_or_request, filters).exclude(debt_amount=0).select_related("client").order_by("-debt_amount")[:500]
    columns = ["Клиент", "Счёт", "Сумма", "Оплачено", "Долг", "Срок"]
    rows = []
    total = Decimal("0")
    for inv in qs:
        debt = inv.debt_amount or Decimal("0")
        total += debt
        rows.append(
            {
                "Клиент": inv.client.short_name or inv.client.agn_name,
                "Счёт": inv.number,
                "Сумма": _money(inv.total_amount),
                "Оплачено": _money(inv.paid_amount),
                "Долг": _money(debt),
                "Срок": inv.due_date.isoformat() if inv.due_date else "",
            }
        )
    return columns, rows, {"Клиент": "", "Счёт": "", "Сумма": "", "Оплачено": "", "Долг": _money(total), "Срок": ""}


def _rep_docs_on_review(user_or_request, filters):
    app_qs = _app_qs(user_or_request, filters)
    acts = (
        BillingAct.objects.filter(application__in=app_qs, review_status=DocumentReviewStatus.SUBMITTED)
        .exclude(status=ActStatus.CANCELLED)
        .select_related("client")
    )
    invoices = ClientInvoice.objects.filter(
        application__in=app_qs, review_status=DocumentReviewStatus.SUBMITTED, status=InvoiceStatus.DRAFT
    ).select_related("client")
    columns = ["Тип", "Номер", "Клиент", "Сумма", "Передан"]
    rows = []
    for a in acts[:300]:
        rows.append(
            {
                "Тип": "Акт",
                "Номер": a.number,
                "Клиент": a.client.short_name or a.client.agn_name,
                "Сумма": _money(a.total_amount),
                "Передан": a.submitted_at.strftime("%d.%m.%Y %H:%M") if a.submitted_at else "",
            }
        )
    for inv in invoices[:300]:
        rows.append(
            {
                "Тип": "Счёт",
                "Номер": inv.number,
                "Клиент": inv.client.short_name or inv.client.agn_name,
                "Сумма": _money(inv.total_amount),
                "Передан": inv.submitted_at.strftime("%d.%m.%Y %H:%M") if inv.submitted_at else "",
            }
        )
    return columns, rows, {}


def _rep_no_price(user_or_request, filters):
    qs = (
        _charge_qs(user_or_request, filters)
        .filter(client_tariff_version__isnull=True, is_manual_override=False)
        .select_related("client", "application", "service")
        .order_by("-created_at")[:500]
    )
    columns = ["Клиент", "Заявка", "Услуга", "Кол-во"]
    rows = [
        {
            "Клиент": c.client.short_name or c.client.agn_name,
            "Заявка": c.application.application_id,
            "Услуга": getattr(c.service, "name", None) or c.service_name_snapshot or "—",
            "Кол-во": _money(c.quantity),
        }
        for c in qs
    ]
    return columns, rows, {}


def _rep_apps_without_charges(user_or_request, filters):
    qs = (
        _app_qs(user_or_request, filters)
        .annotate(ch_count=Count("charges"))
        .filter(ch_count=0)
        .exclude(billing_status=BillingStatus.CANCELLED)
        .select_related("client")
        .order_by("-created_at_source")[:500]
    )
    columns = ["Клиент", "Заявка", "Тип", "Статус"]
    rows = [
        {
            "Клиент": a.client.short_name or a.client.agn_name,
            "Заявка": a.application_id,
            "Тип": a.get_application_type_display() if hasattr(a, "get_application_type_display") else a.application_type,
            "Статус": a.get_billing_status_display() if hasattr(a, "get_billing_status_display") else a.billing_status,
        }
        for a in qs
    ]
    return columns, rows, {}


def _rep_revenue_by_client(user_or_request, filters):
    qs = (
        _invoice_qs(user_or_request, filters)
        .values("client_id", "client__short_name", "client__agn_name")
        .annotate(invoices=Count("id"), issued=Sum("total_amount"), paid=Sum("paid_amount"), debt=Sum("debt_amount"))
        .order_by("-issued")
    )
    columns = ["Клиент", "Счетов", "Выставлено", "Оплачено", "Долг"]
    rows = []
    tot_i = tot_p = tot_d = Decimal("0")
    for r in qs[:500]:
        issued = r["issued"] or Decimal("0")
        paid = r["paid"] or Decimal("0")
        debt = r["debt"] or Decimal("0")
        tot_i += issued
        tot_p += paid
        tot_d += debt
        rows.append(
            {
                "Клиент": r["client__short_name"] or r["client__agn_name"] or str(r["client_id"]),
                "Счетов": r["invoices"],
                "Выставлено": _money(issued),
                "Оплачено": _money(paid),
                "Долг": _money(debt),
            }
        )
    return columns, rows, {
        "Клиент": "",
        "Счетов": "",
        "Выставлено": _money(tot_i),
        "Оплачено": _money(tot_p),
        "Долг": _money(tot_d),
    }


def _rep_payments(user_or_request, filters):
    app_qs = filter_applications_for_user(BillingApplication.objects.all(), user_or_request).distinct()
    qs = (
        InvoicePayment.objects.filter(invoice__application__in=app_qs)
        .exclude(status=PaymentStatus.CANCELLED)
        .select_related("invoice", "invoice__client")
        .order_by("-paid_at")
    )
    if filters["client_id"]:
        qs = qs.filter(invoice__client_id=filters["client_id"])
    if filters["period_from"]:
        qs = qs.filter(paid_at__date__gte=filters["period_from"])
    if filters["period_to"]:
        qs = qs.filter(paid_at__date__lte=filters["period_to"])
    columns = ["Дата", "Клиент", "Счёт", "Сумма", "Источник"]
    rows = []
    total = Decimal("0")
    for p in qs[:500]:
        total += p.amount or Decimal("0")
        rows.append(
            {
                "Дата": p.paid_at.strftime("%d.%m.%Y %H:%M") if p.paid_at else "",
                "Клиент": p.invoice.client.short_name or p.invoice.client.agn_name,
                "Счёт": p.invoice.number,
                "Сумма": _money(p.amount),
                "Источник": p.source,
            }
        )
    return columns, rows, {"Дата": "", "Клиент": "", "Счёт": "", "Сумма": _money(total), "Источник": ""}


def _rep_debt(user_or_request, filters):
    return _rep_invoiced_unpaid(user_or_request, filters)


def _rep_overdue(user_or_request, filters):
    today = timezone.localdate()
    qs = (
        _invoice_qs(user_or_request, filters)
        .filter(due_date__lt=today, debt_amount__gt=0)
        .select_related("client")
        .order_by("due_date")[:500]
    )
    columns = ["Клиент", "Счёт", "Срок", "Дней", "Долг"]
    rows = []
    total = Decimal("0")
    for inv in qs:
        days = (today - inv.due_date).days if inv.due_date else 0
        debt = inv.debt_amount or Decimal("0")
        total += debt
        rows.append(
            {
                "Клиент": inv.client.short_name or inv.client.agn_name,
                "Счёт": inv.number,
                "Срок": inv.due_date.isoformat() if inv.due_date else "",
                "Дней": days,
                "Долг": _money(debt),
            }
        )
    return columns, rows, {"Клиент": "", "Счёт": "", "Срок": "", "Дней": "", "Долг": _money(total)}


def _rep_promises(user_or_request, filters):
    portfolio = filter_agencies_for_user(Agency.objects.all(), user_or_request)
    qs = PaymentPromise.objects.filter(client__in=portfolio).exclude(status=PaymentPromiseStatus.CANCELLED)
    if filters["client_id"]:
        qs = qs.filter(client_id=filters["client_id"])
    if filters["period_from"]:
        qs = qs.filter(promised_date__gte=filters["period_from"])
    if filters["period_to"]:
        qs = qs.filter(promised_date__lte=filters["period_to"])
    qs = qs.select_related("client", "invoice").order_by("promised_date")[:500]
    columns = ["Клиент", "Дата", "Сумма", "Статус", "Счёт"]
    rows = [
        {
            "Клиент": p.client.short_name or p.client.agn_name,
            "Дата": p.promised_date.isoformat(),
            "Сумма": _money(p.amount),
            "Статус": p.get_status_display(),
            "Счёт": p.invoice.number if p.invoice_id else "",
        }
        for p in qs
    ]
    return columns, rows, {}


def _rep_discrepancies(user_or_request, filters):
    portfolio = filter_agencies_for_user(Agency.objects.all(), user_or_request)
    qs = BillingDiscrepancy.objects.filter(client__in=portfolio).exclude(status=DiscrepancyStatus.CLOSED)
    if filters["client_id"]:
        qs = qs.filter(client_id=filters["client_id"])
    qs = qs.select_related("client").order_by("-created_at")[:500]
    columns = ["Клиент", "Тип", "Статус", "Сумма", "Описание"]
    rows = [
        {
            "Клиент": d.client.short_name or d.client.agn_name,
            "Тип": d.get_discrepancy_type_display(),
            "Статус": d.get_status_display(),
            "Сумма": _money(d.disputed_amount) if d.disputed_amount is not None else "",
            "Описание": (d.description or "")[:120],
        }
        for d in qs
    ]
    return columns, rows, {}


def _rep_storage_by_client(user_or_request, filters):
    portfolio = filter_agencies_for_user(Agency.objects.all(), user_or_request)
    qs = BillingStorageDay.objects.filter(client__in=portfolio)
    if filters["client_id"]:
        qs = qs.filter(client_id=filters["client_id"])
    if filters["period_from"]:
        qs = qs.filter(day__gte=filters["period_from"])
    if filters["period_to"]:
        qs = qs.filter(day__lte=filters["period_to"])
    qs = (
        qs.values("client_id", "client__short_name", "client__agn_name")
        .annotate(days=Count("id"), volume=Sum("billable_volume_m3"), amount=Sum("amount"))
        .order_by("-amount")
    )
    columns = ["Клиент", "Дней", "Объём м³", "Сумма"]
    rows = []
    total = Decimal("0")
    for r in qs[:500]:
        amt = r["amount"] or Decimal("0")
        total += amt
        rows.append(
            {
                "Клиент": r["client__short_name"] or r["client__agn_name"] or str(r["client_id"]),
                "Дней": r["days"],
                "Объём м³": _money(r["volume"] or 0),
                "Сумма": _money(amt),
            }
        )
    return columns, rows, {"Клиент": "", "Дней": "", "Объём м³": "", "Сумма": _money(total)}
