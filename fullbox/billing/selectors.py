from __future__ import annotations

from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date

from .models import BillingApplication, ClientInvoice
from .permissions import filter_applications_for_user
from .statuses import InvoiceStatus


APPLICATION_SORTS = {
    "date": "created_at_source",
    "-date": "-created_at_source",
    "amount": "charges_total",
    "-amount": "-charges_total",
    "debt": "debt_total",
    "-debt": "-debt_total",
    "updated": "updated_at",
    "-updated": "-updated_at",
}

INVOICE_SORTS = {
    "date": "invoice_date",
    "-date": "-invoice_date",
    "due": "due_date",
    "-due": "-due_date",
    "amount": "total_amount",
    "-amount": "-total_amount",
    "debt": "debt_amount",
    "-debt": "-debt_amount",
}


def _as_date(value):
    if not value:
        return None
    return parse_date(str(value))


def billing_applications_queryset(params=None, *, user_or_request=None):
    params = params or {}
    qs = BillingApplication.objects.select_related("client", "legal_entity", "own_company", "manager", "marketplace", "warehouse")
    if user_or_request is not None:
        qs = filter_applications_for_user(qs, user_or_request).distinct()
    date_from = _as_date(params.get("date_from"))
    date_to = _as_date(params.get("date_to"))
    if date_from:
        qs = qs.filter(created_at_source__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at_source__date__lte=date_to)
    if params.get("client"):
        qs = qs.filter(client_id=params.get("client"))
    if params.get("legal_entity"):
        qs = qs.filter(legal_entity_id=params.get("legal_entity"))
    if params.get("manager"):
        qs = qs.filter(manager_id=params.get("manager"))
    if params.get("warehouse"):
        qs = qs.filter(Q(warehouse_id=params.get("warehouse")) | Q(warehouse_label__icontains=params.get("warehouse")))
    if params.get("application_type"):
        qs = qs.filter(application_type=params.get("application_type"))
    if params.get("operational_status"):
        qs = qs.filter(operational_status=params.get("operational_status"))
    if params.get("billing_status"):
        qs = qs.filter(billing_status=params.get("billing_status"))
    if params.get("requires_invoice") in {"1", "true", "True"}:
        qs = qs.filter(invoice_required_at__isnull=False, invoices__isnull=True)
    if params.get("invoice_missing") in {"1", "true", "True"}:
        qs = qs.filter(invoice_required_at__isnull=False, invoices__isnull=True)
    if params.get("debt") == "overdue":
        qs = qs.filter(invoices__status__in=[InvoiceStatus.SENT, InvoiceStatus.PARTIALLY_PAID], invoices__due_date__lt=params.get("today") or timezone.localdate())
    if params.get("paid") == "partial":
        qs = qs.filter(paid_total__gt=0, debt_total__gt=0)
    elif params.get("paid") == "full":
        qs = qs.filter(debt_total=0, invoice_total__gt=0)
    search = str(params.get("q") or "").strip()
    if search:
        qs = qs.filter(
            Q(application_id__icontains=search)
            | Q(client__agn_name__icontains=search)
            | Q(client__short_name__icontains=search)
            | Q(client__inn__icontains=search)
            | Q(acts__number__icontains=search)
            | Q(invoices__number__icontains=search)
        )
    sort = APPLICATION_SORTS.get(params.get("sort") or "-date", "-created_at_source")
    return qs.order_by(sort, "-id").distinct()


def invoices_queryset(params=None, *, user_or_request=None):
    params = params or {}
    app_qs = billing_applications_queryset({}, user_or_request=user_or_request)
    qs = ClientInvoice.objects.select_related("application", "client", "legal_entity", "act").filter(application__in=app_qs)
    if params.get("status"):
        qs = qs.filter(status=params.get("status"))
    if params.get("client"):
        qs = qs.filter(client_id=params.get("client"))
    date_from = _as_date(params.get("date_from"))
    date_to = _as_date(params.get("date_to"))
    if date_from:
        qs = qs.filter(invoice_date__gte=date_from)
    if date_to:
        qs = qs.filter(invoice_date__lte=date_to)
    if params.get("overdue") in {"1", "true", "True"}:
        qs = qs.filter(debt_amount__gt=0, due_date__lt=params.get("today") or timezone.localdate())
    search = str(params.get("q") or "").strip()
    if search:
        qs = qs.filter(
            Q(number__icontains=search)
            | Q(client__agn_name__icontains=search)
            | Q(client__inn__icontains=search)
            | Q(act__number__icontains=search)
            | Q(application__application_id__icontains=search)
        )
    sort = INVOICE_SORTS.get(params.get("sort") or "-date", "-invoice_date")
    return qs.order_by(sort, "-id")
