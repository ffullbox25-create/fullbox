"""Оплаты (read), обещания, сверки и расхождения — этап 6."""
from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.dateparse import parse_date

from sku.models import Agency

from .models import (
    BillingActDispute,
    BillingApplication,
    BillingDiscrepancy,
    ClientInvoice,
    InvoicePayment,
    PaymentPromise,
    ReconciliationRequest,
)
from .permissions import filter_agencies_for_user, filter_applications_for_user, get_employee
from .services import BillingWorkflowService, quantize_money
from .statuses import (
    DiscrepancyStatus,
    InvoiceStatus,
    PaymentPromiseStatus,
    PaymentStatus,
    ReconciliationStatus,
)


def list_portfolio_payments(user_or_request, *, client_id: str = "", date_from: str = "", date_to: str = ""):
    app_qs = filter_applications_for_user(BillingApplication.objects.all(), user_or_request).distinct()
    qs = (
        InvoicePayment.objects.filter(invoice__application__in=app_qs)
        .exclude(status=PaymentStatus.CANCELLED)
        .select_related("invoice", "invoice__client", "invoice__application", "registered_by")
        .order_by("-paid_at", "-id")
    )
    if client_id:
        qs = qs.filter(invoice__client_id=client_id)
    if date_from:
        qs = qs.filter(paid_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(paid_at__date__lte=date_to)
    return qs


def debt_summary(user_or_request, *, client_id: str = ""):
    app_qs = filter_applications_for_user(BillingApplication.objects.all(), user_or_request).distinct()
    qs = ClientInvoice.objects.filter(application__in=app_qs).exclude(status=InvoiceStatus.CANCELLED).exclude(
        debt_amount=0
    )
    if client_id:
        qs = qs.filter(client_id=client_id)
    today = timezone.localdate()
    overdue = qs.filter(due_date__lt=today, debt_amount__gt=0)
    return {
        "open_count": qs.count(),
        "open_debt": qs.aggregate(total=Sum("debt_amount")).get("total") or Decimal("0"),
        "overdue_count": overdue.count(),
        "overdue_debt": overdue.aggregate(total=Sum("debt_amount")).get("total") or Decimal("0"),
        "invoices": qs.select_related("client", "application").order_by("due_date", "-debt_amount")[:300],
        "today": today,
    }


def _sync_promise_overdue(qs):
    today = timezone.localdate()
    qs.filter(status=PaymentPromiseStatus.PENDING, promised_date__lt=today).update(
        status=PaymentPromiseStatus.OVERDUE
    )


def list_payment_promises(user_or_request, *, status: str = "", client_id: str = ""):
    portfolio = filter_agencies_for_user(Agency.objects.all(), user_or_request)
    qs = PaymentPromise.objects.filter(client__in=portfolio)
    _sync_promise_overdue(qs)
    qs = qs.select_related("client", "invoice", "manager", "created_by").order_by("promised_date", "-id")
    if status:
        qs = qs.filter(status=status)
    if client_id:
        qs = qs.filter(client_id=client_id)
    return qs


@transaction.atomic
def create_payment_promise(
    *,
    user,
    client: Agency,
    amount,
    promised_date,
    invoice: ClientInvoice | None = None,
    contact_person: str = "",
    client_comment: str = "",
    manager_comment: str = "",
) -> PaymentPromise:
    amt = quantize_money(amount)
    if amt <= 0:
        raise ValidationError("Сумма обещания должна быть больше нуля.")
    day = promised_date if hasattr(promised_date, "year") else parse_date(str(promised_date or ""))
    if not day:
        raise ValidationError("Укажите обещанную дату.")
    if invoice and invoice.client_id != client.id:
        raise ValidationError("Счёт принадлежит другому клиенту.")
    employee = get_employee(user)
    promise = PaymentPromise.objects.create(
        client=client,
        invoice=invoice,
        amount=amt,
        promised_date=day,
        contact_person=contact_person or "",
        client_comment=client_comment or "",
        manager_comment=manager_comment or "",
        status=PaymentPromiseStatus.PENDING,
        manager=employee if employee and getattr(employee, "role", "") == "manager" else None,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    promise.refresh_overdue()
    BillingWorkflowService.audit(
        action="payment_promise_created",
        application=invoice.application if invoice else None,
        user=user,
        obj=promise,
        new_value={"amount": str(promise.amount), "promised_date": str(promise.promised_date)},
    )
    return promise


@transaction.atomic
def update_payment_promise_status(promise: PaymentPromise, *, status: str, user=None, result_note: str = ""):
    allowed = {c[0] for c in PaymentPromiseStatus.choices}
    if status not in allowed:
        raise ValidationError("Некорректный статус обещания.")
    promise.status = status
    if result_note:
        promise.result_note = result_note
    promise.save(update_fields=["status", "result_note", "updated_at"])
    BillingWorkflowService.audit(
        action="payment_promise_updated",
        application=promise.invoice.application if promise.invoice_id else None,
        user=user,
        obj=promise,
        new_value={"status": status},
    )
    return promise


def list_reconciliation_requests(user_or_request, *, status: str = "", client_id: str = ""):
    portfolio = filter_agencies_for_user(Agency.objects.all(), user_or_request)
    qs = (
        ReconciliationRequest.objects.filter(client__in=portfolio)
        .select_related("client", "created_by")
        .order_by("-period_to", "-id")
    )
    if status:
        qs = qs.filter(status=status)
    if client_id:
        qs = qs.filter(client_id=client_id)
    return qs


@transaction.atomic
def create_reconciliation_request(
    *,
    user,
    client: Agency,
    period_from,
    period_to,
    opening_balance=0,
    manager_comment: str = "",
    submit: bool = False,
) -> ReconciliationRequest:
    d_from = period_from if hasattr(period_from, "year") else parse_date(str(period_from or ""))
    d_to = period_to if hasattr(period_to, "year") else parse_date(str(period_to or ""))
    if not d_from or not d_to:
        raise ValidationError("Укажите период сверки.")
    if d_from > d_to:
        raise ValidationError("Дата начала периода больше даты окончания.")
    req = ReconciliationRequest.objects.create(
        client=client,
        period_from=d_from,
        period_to=d_to,
        opening_balance=quantize_money(opening_balance),
        status=ReconciliationStatus.SUBMITTED if submit else ReconciliationStatus.DRAFT,
        manager_comment=manager_comment or "",
        created_by=user if getattr(user, "is_authenticated", False) else None,
        submitted_at=timezone.now() if submit else None,
    )
    BillingWorkflowService.audit(
        action="reconciliation_requested",
        application=None,
        user=user,
        obj=req,
        new_value={"client_id": client.id, "status": req.status},
    )
    return req


@transaction.atomic
def submit_reconciliation_request(req: ReconciliationRequest, *, user=None) -> ReconciliationRequest:
    if req.status not in {ReconciliationStatus.DRAFT, ReconciliationStatus.DISPUTED}:
        raise ValidationError("Передать можно только черновик или сверку с расхождениями.")
    req.status = ReconciliationStatus.SUBMITTED
    req.submitted_at = timezone.now()
    req.save(update_fields=["status", "submitted_at", "updated_at"])
    BillingWorkflowService.audit(
        action="reconciliation_submitted",
        application=None,
        user=user,
        obj=req,
        new_value={"status": req.status},
    )
    return req


def list_discrepancies(user_or_request, *, status: str = "", client_id: str = ""):
    portfolio = filter_agencies_for_user(Agency.objects.all(), user_or_request)
    # Подтянуть клиентские разногласия по актам в единый реестр (идемпотентно)
    open_disputes = BillingActDispute.objects.filter(
        act__client__in=portfolio,
        resolved_at__isnull=True,
        discrepancies__isnull=True,
    ).select_related("act", "act__client", "act__application")[:100]
    for dispute in open_disputes:
        BillingDiscrepancy.objects.get_or_create(
            act_dispute=dispute,
            defaults={
                "client": dispute.act.client,
                "application": dispute.act.application,
                "act": dispute.act,
                "discrepancy_type": BillingDiscrepancy.TYPE_DOCUMENT,
                "description": dispute.comment or "Разногласие клиента по акту",
                "disputed_amount": dispute.expected_amount,
                "status": DiscrepancyStatus.MANAGER_REVIEW,
                "created_by": dispute.created_by,
            },
        )

    qs = (
        BillingDiscrepancy.objects.filter(client__in=portfolio)
        .select_related("client", "application", "act", "invoice", "charge", "created_by")
        .order_by("-created_at", "-id")
    )
    if status:
        qs = qs.filter(status=status)
    if client_id:
        qs = qs.filter(client_id=client_id)
    return qs


@transaction.atomic
def create_discrepancy(
    *,
    user,
    client: Agency,
    description: str,
    discrepancy_type: str = BillingDiscrepancy.TYPE_OTHER,
    disputed_amount=None,
    application=None,
    charge=None,
    act=None,
    invoice=None,
    manager_comment: str = "",
) -> BillingDiscrepancy:
    if not (description or "").strip():
        raise ValidationError("Опишите расхождение.")
    types = {c[0] for c in BillingDiscrepancy.TYPE_CHOICES}
    if discrepancy_type not in types:
        discrepancy_type = BillingDiscrepancy.TYPE_OTHER
    row = BillingDiscrepancy.objects.create(
        client=client,
        application=application,
        charge=charge,
        act=act,
        invoice=invoice,
        discrepancy_type=discrepancy_type,
        description=description.strip(),
        disputed_amount=quantize_money(disputed_amount) if disputed_amount not in (None, "") else None,
        status=DiscrepancyStatus.NEW,
        manager_comment=manager_comment or "",
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    BillingWorkflowService.audit(
        action="discrepancy_created",
        application=application,
        user=user,
        obj=row,
        new_value={"type": row.discrepancy_type, "status": row.status},
    )
    from .models import BillingStaffNotification
    from .staff_notifications import notify_accountants, notify_client_manager

    notify_client_manager(
        client,
        kind=BillingStaffNotification.KIND_DISCREPANCY,
        title="Зафиксировано расхождение",
        message=row.description[:200],
        link_url="/team-manager/billing/disputes/",
        source_key=f"discrepancy-created:{row.id}",
        actor=user,
    )
    notify_accountants(
        kind=BillingStaffNotification.KIND_DISCREPANCY,
        title="Обнаружено расхождение",
        message=f"{client.short_name or client.agn_name}: {row.description[:160]}",
        link_url="/team-manager/billing/disputes/",
        client=client,
        source_key=f"discrepancy-created-acc:{row.id}",
        actor=user,
    )
    return row


@transaction.atomic
def update_discrepancy_status(row: BillingDiscrepancy, *, status: str, user=None, result_note: str = ""):
    allowed = {c[0] for c in DiscrepancyStatus.choices}
    if status not in allowed:
        raise ValidationError("Некорректный статус расхождения.")
    row.status = status
    if result_note:
        row.result_note = result_note
    if status == DiscrepancyStatus.CLOSED:
        row.closed_at = timezone.now()
        if row.act_dispute_id and row.act_dispute and not row.act_dispute.resolved_at:
            row.act_dispute.resolved_at = timezone.now()
            row.act_dispute.save(update_fields=["resolved_at"])
    row.save(update_fields=["status", "result_note", "closed_at", "updated_at"])
    BillingWorkflowService.audit(
        action="discrepancy_updated",
        application=row.application,
        user=user,
        obj=row,
        new_value={"status": status},
    )
    return row
