from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.postgres.aggregates import JSONBAgg
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.contrib import messages
from django.db.models import Count, Exists, F, IntegerField, JSONField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce, JSONObject
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views import View
from django.views.generic import TemplateView

from employees.access import RoleRequiredMixin, get_request_employee, get_request_role
from employees.models import Employee
from head_manager.models import OwnCompany
from fbs.models import FbsIntegrationProfile, FbsStockBalance
from sku.models import Agency

from audit.models import OrderAuditEntry

from .models import (
    ApplicationCharge,
    BillingAct,
    BillingApplication,
    BillingDiscrepancy,
    BillingService,
    BillingStaffNotification,
    BillingStorageDay,
    ClientBillingContract,
    ClientInvoice,
    ClientInvoiceAct,
    ClientTariff,
    ClientTariffVersion,
    FbsClientRate,
    StandardServicePrice,
    WarehouseServiceFact,
)
from .manager_billing import (
    applications_ready_to_invoice,
    build_overview_action_board,
    can_access_client,
    clients_table_rows,
    get_attention_items,
    get_client_billing_summary,
)
from .permissions import (
    can_add_client_tariff,
    can_edit_invoice_prices,
    can_review_billing_document,
    filter_agencies_for_user,
    filter_applications_for_user,
    get_billing_role,
)
from .selectors import billing_applications_queryset, invoices_queryset
from .services import BillingWorkflowService
from .statuses import ActStatus, BillingStatus, DiscrepancyStatus, DocumentReviewStatus, InvoiceStatus
from .invoice_print import build_invoice_print_context
from .tariff_services import grouped_tariff_items
from .client_tariff_sections import build_client_tariff_sections


BILLING_ROLES = ("manager", "head_manager", "accountant", "admin", "director")

BILLING_SECTION_STUBS = {
    "documents": ("Все документы", "Единый реестр документов — используйте Счета, Акты и Черновики."),
}

BILLING_SERVICE_STAGE_KEYS = {
    "work",
    "missing",
    "waiting",
    "confirmed",
    "draft_acts",
    "confirmed_missing",
}
BILLING_SERVICE_EXTRA_TYPES = (
    BillingApplication.TYPE_PACKING,
    BillingApplication.TYPE_LOGISTICS,
    BillingApplication.TYPE_OTHER,
)


def _scalar_filtered_count(queryset, condition=None):
    """Return a scalar count subquery without joining unrelated billing tables."""
    return (
        queryset.order_by()
        .annotate(_constant=Value(1))
        .values("_constant")
        .annotate(_total=Count("id", filter=condition))
        .values("_total")[:1]
    )


def _billing_permission_subject(request):
    """Reuse one resolved employee for permission helpers that accept a user or request."""
    cached = getattr(request, "_billing_permission_subject", None)
    if cached is not None:
        return cached
    employee = get_request_employee(request)
    relation = Employee._meta.get_field("user").remote_field
    relation.set_cached_value(request.user, employee)
    request._billing_permission_subject = request.user
    return request.user


def _billing_badge_counts(*, request, application_queryset, portfolio):
    """Load every billing navigation badge in one scalar-subquery statement."""
    invoice_queryset = ClientInvoice.objects.filter(application__in=application_queryset).exclude(
        status=InvoiceStatus.CANCELLED
    )
    charge_queryset = ApplicationCharge.objects.filter(application__in=application_queryset)
    act_queryset = BillingAct.objects.filter(application__in=application_queryset)
    discrepancy_queryset = BillingDiscrepancy.objects.filter(client__in=portfolio).exclude(
        status=DiscrepancyStatus.CLOSED
    )
    notification_queryset = BillingStaffNotification.objects.filter(
        recipient=request.user,
        is_read=False,
    )
    today = timezone.localdate()
    scalar_counts = {
        "not_calculated": _scalar_filtered_count(
            application_queryset,
            Q(billing_status=BillingStatus.NOT_CALCULATED),
        ),
        "acts_without_invoice": _scalar_filtered_count(
            application_queryset,
            Q(billing_status=BillingStatus.INVOICE_REQUIRED),
        ),
        "overdue": _scalar_filtered_count(
            invoice_queryset,
            Q(debt_amount__gt=0, due_date__lt=today),
        ),
        "charges_review": _scalar_filtered_count(
            charge_queryset,
            Q(is_confirmed=False) & ~Q(application__billing_status=BillingStatus.CANCELLED),
        ),
        "no_price": _scalar_filtered_count(
            charge_queryset,
            Q(client_tariff_version__isnull=True, is_manual_override=False),
        ),
        "docs_submitted": _scalar_filtered_count(
            act_queryset,
            Q(review_status=DocumentReviewStatus.SUBMITTED),
        ),
        "invoice_docs_submitted": _scalar_filtered_count(
            invoice_queryset,
            Q(review_status=DocumentReviewStatus.SUBMITTED, status=InvoiceStatus.DRAFT),
        ),
        "docs_returned": _scalar_filtered_count(
            act_queryset,
            Q(review_status=DocumentReviewStatus.RETURNED),
        ),
        "invoice_docs_returned": _scalar_filtered_count(
            invoice_queryset,
            Q(review_status=DocumentReviewStatus.RETURNED, status=InvoiceStatus.DRAFT),
        ),
        "discrepancies": _scalar_filtered_count(discrepancy_queryset),
        "notifications": _scalar_filtered_count(notification_queryset),
    }
    annotations = {
        key: Coalesce(Subquery(queryset, output_field=IntegerField()), Value(0))
        for key, queryset in scalar_counts.items()
    }
    counts = (
        get_user_model()._default_manager.filter(pk=request.user.pk)
        .annotate(**annotations)
        .values(*annotations)
        .first()
    ) or {key: 0 for key in annotations}
    counts["docs_submitted"] += counts.pop("invoice_docs_submitted")
    counts["docs_returned"] += counts.pop("invoice_docs_returned")
    return counts


def _billing_service_stage_counts(
    portfolio,
    *,
    date_from=None,
    date_to=None,
    order_type="",
):
    """Count the five manager stages in one SQL query without multiplying JOINs."""
    facts = WarehouseServiceFact.objects.filter(client__in=portfolio)
    charges = ApplicationCharge.objects.filter(client__in=portfolio)
    acts = BillingAct.objects.filter(client__in=portfolio)

    if date_from:
        facts = facts.filter(reported_at__date__gte=date_from)
    if date_to:
        facts = facts.filter(reported_at__date__lte=date_to)
    if order_type == "extra":
        facts = facts.filter(order_type__in=BILLING_SERVICE_EXTRA_TYPES)
        charges = charges.filter(application__application_type__in=BILLING_SERVICE_EXTRA_TYPES)
        acts = acts.filter(application__application_type__in=BILLING_SERVICE_EXTRA_TYPES)
    elif order_type:
        facts = facts.filter(order_type=order_type)
        charges = charges.filter(application__application_type=order_type)
        acts = acts.filter(application__application_type=order_type)

    facts_count = _scalar_filtered_count(facts)
    missing_count = _scalar_filtered_count(
        charges,
        Q(is_excluded=False, client_tariff_version__isnull=True, is_manual_override=False),
    )
    waiting_count = _scalar_filtered_count(
        charges,
        Q(is_excluded=False, is_confirmed=False, client_tariff_version__isnull=False),
    )
    confirmed_count = _scalar_filtered_count(
        charges,
        Q(is_confirmed=True, is_excluded=False, is_included_in_act=False),
    )
    draft_count = _scalar_filtered_count(acts, Q(status=ActStatus.DRAFT))

    counts = (
        portfolio.order_by("pk")
        .annotate(
            stage_work_count=Coalesce(Subquery(facts_count, output_field=IntegerField()), Value(0)),
            stage_missing_count=Coalesce(Subquery(missing_count, output_field=IntegerField()), Value(0)),
            stage_waiting_count=Coalesce(Subquery(waiting_count, output_field=IntegerField()), Value(0)),
            stage_confirmed_count=Coalesce(Subquery(confirmed_count, output_field=IntegerField()), Value(0)),
            stage_draft_count=Coalesce(Subquery(draft_count, output_field=IntegerField()), Value(0)),
        )
        .values(
            "stage_work_count",
            "stage_missing_count",
            "stage_waiting_count",
            "stage_confirmed_count",
            "stage_draft_count",
        )
        .first()
    )
    return counts or {
        "stage_work_count": 0,
        "stage_missing_count": 0,
        "stage_waiting_count": 0,
        "stage_confirmed_count": 0,
        "stage_draft_count": 0,
    }


def _billing_stage_cards(counts, *, selected_stage="work", query=None):
    query = dict(query or {})
    definitions = (
        ("work", "Работы за период", "stage_work_count", "neutral"),
        ("missing", "Нет цены", "stage_missing_count", "danger"),
        ("waiting", "Ждут подтверждения", "stage_waiting_count", "orange"),
        ("confirmed", "Подтверждено, не в акте", "stage_confirmed_count", "orange"),
        ("draft_acts", "Черновики актов", "stage_draft_count", "neutral"),
    )
    cards = []
    for key, label, count_key, tone in definitions:
        params = dict(query)
        params["stage"] = key
        if key != "work":
            params.pop("date_from", None)
            params.pop("date_to", None)
        count = counts[count_key]
        cards.append(
            {
                "key": key,
                "label": label,
                "count": count,
                "tone": tone if count else "neutral",
                "active": key == selected_stage,
                "href": f"/team-manager/billing/services/?{urlencode(params)}",
            }
        )
    return cards


def _previous_complete_week_start(today=None):
    today = today or timezone.localdate()
    return today - timedelta(days=today.weekday() + 7)


def _application_comment_rows(application) -> list[dict]:
    entries = (
        OrderAuditEntry.objects.filter(
            order_type=application.application_type,
            order_id=str(application.application_id),
            action="comment",
        )
        .select_related("user", "agency")
        .order_by("-created_at", "-id")[:100]
    )
    rows = []
    for entry in entries:
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        text = str(entry.description or payload.get("comment") or "").strip()
        if not text:
            continue
        actor = ""
        if entry.user_id:
            actor = (entry.user.get_full_name() or entry.user.get_username() or "").strip()
        if not actor and entry.agency_id:
            actor = str(entry.agency.agn_name or "").strip()
        rows.append(
            {
                "text": text,
                "actor": actor or "Система",
                "created_at": entry.created_at,
            }
        )
    return rows


def _shipping_application_fact_payload(application) -> dict:
    """Read actuals for historical shipping orders without billing service facts."""
    if application.application_type != BillingApplication.TYPE_SHIPPING:
        return {}

    from shipping.models import ShippingOrder
    from sklad.models import WarehouseContainer, WarehouseStockSnapshot

    order = ShippingOrder.objects.filter(
        number=str(application.application_id),
        agency_id=application.client_id,
    ).first()
    if order is None or order.status not in {
        ShippingOrder.STATUS_SHIPPED,
        ShippingOrder.STATUS_PARTIAL,
    }:
        return {}

    item_rows = list(
        order.items.order_by("id").values(
            "sku_code",
            "name",
            "barcode",
            "qty_requested",
            "qty_shipped",
        )
    )
    act_items = [
        {
            "sku_code": row["sku_code"],
            "name": row["name"],
            "barcode": row["barcode"],
            "planned_qty": row["qty_requested"],
            "actual_qty": row["qty_shipped"],
        }
        for row in item_rows
    ]

    snapshots = list(
        WarehouseStockSnapshot.objects.filter(
            agency_id=application.client_id,
            last_event__stock_context_type="shipping",
            last_event__stock_context_id=str(application.application_id),
            warehouse_state_code="shipped",
            container__container_type=WarehouseContainer.TYPE_BOX,
        ).values(
            "container_id",
            "container_code",
            "parent_container_id",
            "parent_container__container_code",
        )
    )
    box_keys = {
        ("id", row["container_id"])
        if row["container_id"]
        else ("code", str(row["container_code"] or "").strip())
        for row in snapshots
        if row["container_id"] or str(row["container_code"] or "").strip()
    }
    pallet_keys = {
        ("id", row["parent_container_id"])
        if row["parent_container_id"]
        else ("code", str(row["parent_container__container_code"] or "").strip())
        for row in snapshots
        if row["parent_container_id"] or str(row["parent_container__container_code"] or "").strip()
    }

    if not box_keys or not pallet_keys:
        packing_payload = (
            OrderAuditEntry.objects.filter(
                order_type=BillingApplication.TYPE_SHIPPING,
                order_id=str(application.application_id),
                payload__act="shipping_packing",
            )
            .order_by("-created_at", "-id")
            .values_list("payload", flat=True)
            .first()
        )
        if isinstance(packing_payload, dict):
            if not box_keys:
                box_keys = {
                    ("audit", str(row.get("code") or row.get("row_key") or index))
                    for index, row in enumerate(packing_payload.get("act_boxes") or [], start=1)
                    if isinstance(row, dict)
                }
            if not pallet_keys:
                pallet_keys = {
                    ("audit", str(row.get("code") or row.get("label") or index))
                    for index, row in enumerate(packing_payload.get("act_pallets") or [], start=1)
                    if isinstance(row, dict)
                }

    return {
        "act_items": act_items,
        "shipped_boxes": len(box_keys) if box_keys else None,
        "shipped_pallets": len(pallet_keys) if pallet_keys else None,
        "shipped_at": order.shipped_at,
    }


class BillingBaseView(RoleRequiredMixin, TemplateView):
    allowed_roles = BILLING_ROLES
    active_nav = "billing"
    billing_section = "overview"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            self.billing_permission_subject = _billing_permission_subject(request)
        if request.user.is_authenticated and request.user.is_superuser:
            return TemplateView.dispatch(self, request, *args, **kwargs)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        permission_subject = getattr(
            self,
            "billing_permission_subject",
            _billing_permission_subject(self.request),
        )
        app_qs = filter_applications_for_user(BillingApplication.objects.all(), permission_subject).distinct()
        portfolio = filter_agencies_for_user(Agency.objects.all(), permission_subject)
        badge_counts = _billing_badge_counts(
            request=self.request,
            application_queryset=app_qs,
            portfolio=portfolio,
        )
        ctx.update(
            {
                "active_nav": self.active_nav,
                "billing_section": getattr(self, "billing_section", "overview"),
                "billing_can_edit_tariffs": can_add_client_tariff(permission_subject),
                "billing_can_review_docs": can_review_billing_document(permission_subject),
                "billing_badges": badge_counts,
            }
        )
        return ctx


class BillingOverviewView(BillingBaseView):
    template_name = "billing/overview.html"
    billing_section = "overview"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        today = timezone.localdate()
        portfolio = filter_agencies_for_user(Agency.objects.all(), self.request)
        app_qs = filter_applications_for_user(BillingApplication.objects.all(), self.request).distinct()
        invoice_qs = ClientInvoice.objects.filter(application__in=app_qs).exclude(status=InvoiceStatus.CANCELLED)
        stage_counts = _billing_service_stage_counts(
            portfolio,
            date_from=today.replace(day=1),
            date_to=today,
        )
        board = build_overview_action_board(self.request)
        totals = app_qs.aggregate(paid_total=Sum("paid_total"), total_debt=Sum("debt_total"))
        status_labels = dict(BillingStatus.choices)
        status_rows = list(
            app_qs.values("billing_status")
            .annotate(count=Count("id"))
            .order_by("billing_status")
        )
        for row in status_rows:
            row["label"] = status_labels.get(row["billing_status"], row["billing_status"])
        ctx.update(
            {
                "overview_cards": _billing_stage_cards(stage_counts),
                "action_rows": board["action_rows"],
                "applications_count": app_qs.count(),
                "invoices_count": invoice_qs.count(),
                "paid_total": totals["paid_total"] or 0,
                "total_debt": totals["total_debt"] or 0,
                "status_rows": status_rows,
            }
        )
        return ctx


class BillingClientsView(BillingBaseView):
    template_name = "billing/clients.html"
    billing_section = "clients"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        today = timezone.localdate()
        q = (self.request.GET.get("q") or "").strip()
        date_from_raw = (self.request.GET.get("date_from") or "").strip()
        date_to_raw = (self.request.GET.get("date_to") or "").strip()
        date_from = parse_date(date_from_raw) if date_from_raw else today.replace(day=1)
        date_to = parse_date(date_to_raw) if date_to_raw else today
        date_from = date_from or today.replace(day=1)
        date_to = date_to or today
        ctx.update(
            {
                "client_rows": clients_table_rows(
                    self.billing_permission_subject,
                    q=q,
                    date_from=date_from,
                    date_to=date_to,
                ),
                "filter_q": q,
                "filter_date_from": date_from.isoformat(),
                "filter_date_to": date_to.isoformat(),
            }
        )
        return ctx


CLIENT_WORK_SECTION_DEFINITIONS = (
    ("receiving", "Приёмка"),
    ("processing", "Обработка"),
    ("shipping", "Отгрузка"),
    ("fbs", "FBS"),
    ("storage", "Хранение"),
)


def _client_work_section_key(application_type: str) -> str:
    if application_type in {BillingApplication.TYPE_PROCESSING, BillingApplication.TYPE_PACKING, BillingApplication.TYPE_OTHER}:
        return "processing"
    if application_type in {BillingApplication.TYPE_SHIPPING, BillingApplication.TYPE_LOGISTICS}:
        return "shipping"
    if application_type == BillingApplication.TYPE_FBS:
        return "fbs"
    if application_type == BillingApplication.TYPE_STORAGE:
        return "storage"
    return "receiving"


def _json_array_subquery(queryset, *, fields: dict, ordering: tuple[str, ...]):
    return (
        queryset.order_by()
        .annotate(_constant=Value(1))
        .values("_constant")
        .annotate(_rows=JSONBAgg(JSONObject(**fields), ordering=ordering))
        .values("_rows")[:1]
    )


def _client_work_sections(client: Agency, *, date_from, date_to) -> list[dict]:
    """Read all five client work sections in one JSON-aggregate query."""
    from logistics.models import LogisticsTripOrder

    from .charge_status import charge_manager_status

    fact_rows = WarehouseServiceFact.objects.filter(
        client=client,
        reported_at__date__gte=date_from,
        reported_at__date__lte=date_to,
    )
    charge_rows = (
        ApplicationCharge.objects.filter(
            client=client,
            billing_period__gte=date_from,
            billing_period__lte=date_to,
        )
        .exclude(application__billing_status=BillingStatus.CANCELLED)
    )
    storage_rows = (
        BillingStorageDay.objects.filter(
            client=client,
            day__gte=date_from,
            day__lte=date_to,
        )
        .exclude(status=BillingStorageDay.STATUS_CANCELLED)
    )
    trip_rows = LogisticsTripOrder.objects.filter(shipping_order__agency_id=client.id)
    payload = (
        Agency.objects.filter(pk=client.pk)
        .annotate(
            work_facts=Coalesce(
                Subquery(
                    _json_array_subquery(
                        fact_rows,
                        fields={
                            "id": F("id"),
                            "application_pk": F("application_id"),
                            "application_number": F("application__application_id"),
                            "application_type": F("application__application_type"),
                            "application_date": F("application__created_at_source"),
                            "order_type": F("order_type"),
                            "order_id": F("order_id"),
                            "performed_at": F("performed_at"),
                            "reported_at": F("reported_at"),
                            "service_name_snapshot": F("service_name_snapshot"),
                            "service_name": F("service__name"),
                            "quantity": F("quantity"),
                            "unit": F("unit"),
                            "status": F("status"),
                            "charge_id": F("charge_id"),
                        },
                        ordering=("-reported_at", "-id"),
                    ),
                    output_field=JSONField(),
                ),
                Value([], output_field=JSONField()),
            ),
            work_charges=Coalesce(
                Subquery(
                    _json_array_subquery(
                        charge_rows,
                        fields={
                            "id": F("id"),
                            "application_pk": F("application_id"),
                            "application_number": F("application__application_id"),
                            "application_type": F("application__application_type"),
                            "application_date": F("application__created_at_source"),
                            "performed_at": F("performed_at"),
                            "created_at": F("created_at"),
                            "service_name_snapshot": F("service_name_snapshot"),
                            "service_name": F("service__name"),
                            "quantity": F("quantity"),
                            "unit": F("unit"),
                            "total_amount": F("total_amount"),
                            "is_excluded": F("is_excluded"),
                            "is_disputed": F("is_disputed"),
                            "tariff_version_id": F("client_tariff_version_id"),
                            "is_manual_override": F("is_manual_override"),
                            "service_changed_at": F("service_changed_at"),
                            "is_confirmed": F("is_confirmed"),
                            "is_included_in_act": F("is_included_in_act"),
                            "is_included_in_invoice": F("is_included_in_invoice"),
                            "original_quantity": F("original_quantity"),
                            "tariff": F("tariff"),
                        },
                        ordering=("-performed_at", "-id"),
                    ),
                    output_field=JSONField(),
                ),
                Value([], output_field=JSONField()),
            ),
            storage_days=Coalesce(
                Subquery(
                    _json_array_subquery(
                        storage_rows,
                        fields={
                            "id": F("id"),
                            "day": F("day"),
                            "pallet_count": F("pallet_count"),
                            "billable_volume_l": F("billable_volume_l"),
                            "billable_volume_m3": F("billable_volume_m3"),
                            "billing_mode": F("billing_mode"),
                            "amount": F("amount"),
                            "status": F("status"),
                        },
                        ordering=("-day", "-id"),
                    ),
                    output_field=JSONField(),
                ),
                Value([], output_field=JSONField()),
            ),
            shipping_trips=Coalesce(
                Subquery(
                    _json_array_subquery(
                        trip_rows,
                        fields={
                            "order_number": F("shipping_order__number"),
                            "trip_number": F("trip__number"),
                            "trip_date": F("trip__trip_date"),
                            "vehicle_name": F("trip__vehicle_name"),
                            "vehicle_number": F("trip__vehicle_number"),
                            "loading_sequence": F("loading_sequence"),
                        },
                        ordering=("shipping_order__number", "loading_sequence", "id"),
                    ),
                    output_field=JSONField(),
                ),
                Value([], output_field=JSONField()),
            ),
        )
        .values("work_facts", "work_charges", "storage_days", "shipping_trips")
        .first()
    ) or {"work_facts": [], "work_charges": [], "storage_days": [], "shipping_trips": []}

    def parsed_date(value):
        if not value or not isinstance(value, str):
            return value
        return parse_datetime(value) or parse_date(value)

    def decimal_value(value):
        return Decimal(str(value or "0"))

    def charge_for_status(row):
        return ApplicationCharge(
            id=row["id"],
            is_excluded=bool(row["is_excluded"]),
            is_disputed=bool(row["is_disputed"]),
            client_tariff_version_id=row["tariff_version_id"],
            is_manual_override=bool(row["is_manual_override"]),
            service_changed_at=parsed_date(row["service_changed_at"]),
            is_confirmed=bool(row["is_confirmed"]),
            is_included_in_act=bool(row["is_included_in_act"]),
            is_included_in_invoice=bool(row["is_included_in_invoice"]),
            original_quantity=(
                decimal_value(row["original_quantity"])
                if row["original_quantity"] is not None
                else None
            ),
            quantity=decimal_value(row["quantity"]),
            tariff=decimal_value(row["tariff"]),
        )

    sections = {
        key: {
            "key": key,
            "label": label,
            "applications_map": {},
            "application_count": 0,
            "service_count": 0,
            "amount": Decimal("0"),
            "missing_price": 0,
            "waiting_confirmation": 0,
            "applications": [],
            "storage_days": [],
        }
        for key, label in CLIENT_WORK_SECTION_DEFINITIONS
    }
    application_labels = dict(BillingApplication.APPLICATION_TYPE_CHOICES)
    fact_type_labels = dict(WarehouseServiceFact.ORDER_TYPE_CHOICES)
    fact_status_labels = dict(WarehouseServiceFact.STATUS_CHOICES)

    def application_row(
        section,
        *,
        application_pk=None,
        application_type="",
        application_number="",
        order_type="",
        order_id="",
        row_date=None,
    ):
        row_date = parsed_date(row_date)
        if application_pk:
            row_key = f"application:{application_pk}"
            number = application_number
            type_label = application_labels.get(application_type, "Работа склада")
        else:
            row_key = f"fact:{order_type}:{order_id}"
            number = order_id or "Без номера"
            type_label = fact_type_labels.get(order_type, "Работа склада")
        row = section["applications_map"].get(row_key)
        if row is None:
            row = {
                "application_pk": application_pk,
                "number": number,
                "application_type": application_type or order_type,
                "application_type_label": type_label,
                "date": row_date,
                "sort_value": row_date.isoformat() if row_date else "",
                "services": [],
                "trip_rows": [],
            }
            section["applications_map"][row_key] = row
        return row

    charge_rows_by_id = {row["id"]: row for row in payload["work_charges"]}
    used_charge_ids = set()
    for fact in payload["work_facts"]:
        application_type = fact["application_type"] or fact["order_type"]
        section = sections[_client_work_section_key(application_type)]
        if section["key"] == "storage":
            continue
        app_row = application_row(
            section,
            application_pk=fact["application_pk"],
            application_type=application_type,
            application_number=fact["application_number"],
            order_type=fact["order_type"],
            order_id=fact["order_id"],
            row_date=fact["application_date"] or fact["performed_at"] or fact["reported_at"],
        )
        charge_data = charge_rows_by_id.get(fact["charge_id"])
        if charge_data is not None:
            used_charge_ids.add(charge_data["id"])
            _, status_label, status_badge = charge_manager_status(charge_for_status(charge_data))
            amount = decimal_value(charge_data["total_amount"])
        else:
            status_label = fact_status_labels.get(fact["status"], fact["status"])
            status_badge = "gray"
            amount = None
        app_row["services"].append(
            {
                "name": fact["service_name_snapshot"] or fact["service_name"] or "Услуга не указана",
                "quantity": decimal_value(fact["quantity"]),
                "unit": fact["unit"] or "шт",
                "amount": amount,
                "status_label": status_label,
                "status_badge": status_badge,
            }
        )

    for charge_data in payload["work_charges"]:
        application_type = charge_data["application_type"]
        section = sections[_client_work_section_key(application_type)]
        section["amount"] += decimal_value(charge_data["total_amount"])
        if not charge_data["is_excluded"] and charge_data["tariff_version_id"] is None and not charge_data["is_manual_override"]:
            section["missing_price"] += 1
        if not charge_data["is_excluded"] and not charge_data["is_confirmed"] and charge_data["tariff_version_id"] is not None:
            section["waiting_confirmation"] += 1
        if section["key"] == "storage":
            continue
        app_row = application_row(
            section,
            application_pk=charge_data["application_pk"],
            application_type=application_type,
            application_number=charge_data["application_number"],
            row_date=charge_data["application_date"] or charge_data["performed_at"] or charge_data["created_at"],
        )
        if charge_data["id"] in used_charge_ids:
            continue
        _, status_label, status_badge = charge_manager_status(charge_for_status(charge_data))
        app_row["services"].append(
            {
                "name": charge_data["service_name_snapshot"] or charge_data["service_name"] or "Услуга не указана",
                "quantity": decimal_value(charge_data["quantity"]),
                "unit": charge_data["unit"] or "шт",
                "amount": decimal_value(charge_data["total_amount"]),
                "status_label": status_label,
                "status_badge": status_badge,
            }
        )

    trips_by_order = {}
    for trip in payload["shipping_trips"]:
        trips_by_order.setdefault(trip["order_number"], []).append(
            {
                "number": trip["trip_number"],
                "date": parsed_date(trip["trip_date"]),
                "vehicle": trip["vehicle_name"] or trip["vehicle_number"] or "—",
                "loading_sequence": trip["loading_sequence"],
            }
        )
    for row in sections["shipping"]["applications_map"].values():
        if row["application_type"] == BillingApplication.TYPE_SHIPPING:
            row["trip_rows"] = trips_by_order.get(row["number"], [])

    storage = sections["storage"]
    storage["storage_days_amount"] = Decimal("0")
    storage_mode_labels = {
        "pallet_day": "палето-место / сутки",
        "pallet_week": "палето-место / неделя",
        "pallet_month": "палето-место / месяц",
        "liter_day": "литр / сутки",
        "liter_week": "литр / неделя",
        "liter_month": "литр / месяц",
        "m3_day": "м³ / сутки",
        "m3_week": "м³ / неделя",
        "m3_month": "м³ / месяц",
    }
    for storage_day in payload["storage_days"]:
        mode = storage_day["billing_mode"] or ""
        if mode.startswith("liter_"):
            quantity = decimal_value(storage_day["billable_volume_l"])
            unit = "л"
        elif mode.startswith("m3_"):
            quantity = decimal_value(storage_day["billable_volume_m3"])
            unit = "м³"
        else:
            quantity = storage_day["pallet_count"]
            unit = "палето-мест"
        amount = decimal_value(storage_day["amount"])
        storage["storage_days"].append(
            {
                "day": parsed_date(storage_day["day"]),
                "mode_label": storage_mode_labels.get(mode, "режим не указан"),
                "quantity": quantity,
                "unit": unit,
                "amount": amount,
                "status_label": dict(BillingStorageDay.STATUS_CHOICES).get(
                    storage_day["status"],
                    storage_day["status"],
                ),
            }
        )
        storage["storage_days_amount"] += amount
    storage["storage_days_count"] = len(payload["storage_days"])
    storage["storage_pallet_count"] = max(
        (day["pallet_count"] for day in payload["storage_days"]),
        default=0,
    )
    storage["storage_amount_differs"] = storage["amount"] != storage["storage_days_amount"]

    result = []
    for key, _label in CLIENT_WORK_SECTION_DEFINITIONS:
        section = sections[key]
        section["applications"] = sorted(
            section["applications_map"].values(),
            key=lambda row: (row["sort_value"], row["number"]),
            reverse=True,
        )
        section["application_count"] = len(section["applications"])
        section["service_count"] = sum(len(row["services"]) for row in section["applications"])
        section.pop("applications_map", None)
        result.append(section)
    return result


class BillingClientDetailView(BillingBaseView):
    template_name = "billing/client_detail.html"
    billing_section = "clients"

    def dispatch(self, request, *args, **kwargs):
        client = Agency.objects.filter(pk=kwargs.get("client_id")).select_related(
            "lifecycle", "lifecycle__serving_company"
        ).first()
        if not client:
            raise Http404("Клиент не найден")
        self.billing_permission_subject = _billing_permission_subject(request)
        if not request.user.is_superuser and not can_access_client(self.billing_permission_subject, client):
            return HttpResponseForbidden("Доступ запрещен")
        self.billing_client = client
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        summary = get_client_billing_summary(self.billing_client)
        tariff = summary["tariff"]
        today = timezone.localdate()
        tab = (self.request.GET.get("tab") or "summary").strip()
        date_from_raw = (self.request.GET.get("date_from") or "").strip()
        date_to_raw = (self.request.GET.get("date_to") or "").strip()
        date_from = parse_date(date_from_raw) if date_from_raw else today.replace(day=1)
        date_to = parse_date(date_to_raw) if date_to_raw else today
        date_from = date_from or today.replace(day=1)
        date_to = date_to or today
        ctx.update(
            {
                **summary,
                "tab": tab,
                "client_filter_date_from": date_from.isoformat(),
                "client_filter_date_to": date_to.isoformat(),
                "client_work_sections": (
                    _client_work_sections(self.billing_client, date_from=date_from, date_to=date_to)
                    if tab == "summary"
                    else []
                ),
                "client_fbs_billing_disabled": not getattr(settings, "FBS_BILLING_ENABLED", False),
                "vat_type_label": tariff.get_vat_type_display() if tariff else "",
                "contracts": [],
                "tariff_versions": [],
                "published_tariff_versions": [],
                "tariff_groups": [],
                "tariff_section": "general",
                "tariff_sections": {},
                "recent_applications": [],
                "recent_invoices": [],
            }
        )
        if tab == "contracts":
            ctx["contracts"] = ClientBillingContract.objects.filter(
                client=self.billing_client
            ).select_related("own_company").order_by("-valid_from", "-id")[:50]
        elif tab == "tariffs":
            versions = summary["tariff_history"]
            tariff_section = (self.request.GET.get("tariff_section") or "general").strip()
            if tariff_section not in {"general", "logistics", "fbs"}:
                tariff_section = "general"
            ctx["tariff_versions"] = versions
            ctx["published_tariff_versions"] = [
                version
                for version in versions
                if version.status
                in {
                    ClientTariffVersion.STATUS_ACTIVE,
                    ClientTariffVersion.STATUS_SCHEDULED,
                    ClientTariffVersion.STATUS_EXPIRED,
                    ClientTariffVersion.STATUS_ARCHIVED,
                }
            ]
            ctx["tariff_groups"] = grouped_tariff_items(tariff)
            ctx["tariff_section"] = tariff_section
            ctx["tariff_sections"] = build_client_tariff_sections(
                self.billing_client,
                tariff,
                on_date=today,
            )
        elif tab == "applications":
            ctx["recent_applications"] = list(
                filter_applications_for_user(
                    BillingApplication.objects.filter(client=self.billing_client),
                    self.request,
                ).order_by("-created_at_source", "-id")[:30]
            )
        elif tab == "invoices":
            ctx["recent_invoices"] = list(
                ClientInvoice.objects.filter(client=self.billing_client)
                .exclude(status=InvoiceStatus.CANCELLED)
                .order_by("-invoice_date", "-id")[:30]
            )
        return ctx


class BillingSectionStubView(BillingBaseView):
    template_name = "billing/section_stub.html"

    def dispatch(self, request, *args, **kwargs):
        key = (kwargs.get("section_key") or "").strip()
        if key not in BILLING_SECTION_STUBS:
            raise Http404("Раздел не найден")
        self.section_key = key
        self.billing_section = key
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        title, description = BILLING_SECTION_STUBS[self.section_key]
        ctx.update({"page_title": title, "page_description": description})
        return ctx


class BillingApplicationsView(BillingBaseView):
    template_name = "billing/applications.html"
    billing_section = "applications"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        qs = billing_applications_queryset(self.request.GET, user_or_request=self.request)
        # Полный список по фильтрам (раньше обрезали до 100 — «пропадали» заявки).
        ctx["applications"] = list(qs[:5000])
        ctx["applications_total"] = qs.count()
        ctx["app_type_tabs"] = [
            ("", "Все"),
            (BillingApplication.TYPE_RECEIVING, "Приёмка"),
            (BillingApplication.TYPE_PROCESSING, "Обработка"),
            (BillingApplication.TYPE_SHIPPING, "Отгрузка"),
            (BillingApplication.TYPE_LOGISTICS, "Логистика"),
            (BillingApplication.TYPE_OTHER, "Другие заявки"),
            (BillingApplication.TYPE_PACKING, "Упаковка"),
            (BillingApplication.TYPE_STORAGE, "Хранение"),
            (BillingApplication.TYPE_FBS, "FBS"),
        ]
        ctx["selected_app_type"] = (self.request.GET.get("application_type") or "").strip()
        ctx["application_clients"] = filter_agencies_for_user(Agency.objects.all(), self.request).order_by("agn_name")[:1000]
        return ctx


class BillingFbsView(BillingBaseView):
    template_name = "billing/fbs.html"
    billing_section = "fbs"

    @staticmethod
    def _date_filters(request):
        today = timezone.localdate()
        default_from = _previous_complete_week_start(today)
        raw_from = (request.GET.get("date_from") or request.POST.get("date_from") or "").strip()
        raw_to = (request.GET.get("date_to") or request.POST.get("date_to") or "").strip()
        date_from = parse_date(raw_from) if raw_from else default_from
        date_to = parse_date(raw_to) if raw_to else default_from + timedelta(days=6)
        if not date_from or not date_to or date_from > date_to:
            raise ValidationError("Укажите корректный период FBS.")
        return date_from, date_to

    def _selected_client(self):
        client_id = str(self.request.GET.get("client") or self.request.POST.get("client") or "").strip()
        if not client_id:
            return None
        return get_object_or_404(filter_agencies_for_user(Agency.objects.all(), self.request), pk=client_id)

    def post(self, request, *args, **kwargs):
        try:
            client = self._selected_client()
            if client is None:
                raise ValidationError("Выберите клиента для FBS-расчета.")
            date_from, date_to = self._date_filters(request)
            action = (request.POST.get("action") or "").strip()
            if action == "save_rates":
                if get_billing_role(request) not in {"accountant", "admin", "director"}:
                    raise PermissionDenied("Изменять индивидуальные ставки FBS может бухгалтер.")
                rates = FbsClientRate.objects.filter(client=client).order_by("operation", "valid_from", "liters_from", "id")
                updated = 0
                for rate in rates:
                    value = (request.POST.get(f"rate_{rate.pk}") or "").strip().replace(",", ".")
                    if not value:
                        continue
                    new_price = Decimal(value)
                    if rate.price != new_price:
                        rate.price = new_price
                        rate.full_clean()
                        rate.save(update_fields=["price", "updated_at"])
                        updated += 1
                messages.success(request, f"Сохранено ставок FBS: {updated}.")
            elif action == "calculate":
                from .fbs_standard_shipping import calculate_standard_shipping_fbs

                result = calculate_standard_shipping_fbs(
                    client=client,
                    date_from=date_from,
                    date_to=date_to,
                    user=request.user,
                )
                messages.success(
                    request,
                    f"FBS-расчет сформирован: {result['created_or_updated']} строк начислений по {len(result['applications'])} заявкам.",
                )
                for error in result["errors"][:10]:
                    messages.warning(request, error)
            else:
                raise ValidationError("Неизвестное действие FBS.")
        except (ValidationError, ValueError) as exc:
            messages.error(request, "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc))
        query = f"?client={client.pk if 'client' in locals() and client else ''}&date_from={date_from.isoformat() if 'date_from' in locals() else ''}&date_to={date_to.isoformat() if 'date_to' in locals() else ''}"
        return redirect(f"{request.path}{query}")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        applications = filter_applications_for_user(
            BillingApplication.objects.filter(application_type=BillingApplication.TYPE_FBS),
            self.request,
        ).select_related("client", "manager").order_by("-created_at_source", "-id")
        client_id = str(self.request.GET.get("client") or "").strip()
        selected_client = self._selected_client()
        try:
            date_from, date_to = self._date_filters(self.request)
        except ValidationError:
            date_from = date_to = timezone.localdate()
        if client_id:
            applications = applications.filter(client_id=client_id)
        application_rows = list(applications[:200])
        app_ids = [application.id for application in application_rows]
        facts_query = (
            WarehouseServiceFact.objects.filter(
                application_id__in=app_ids,
                order_type=WarehouseServiceFact.ORDER_FBS,
            )
            .select_related("client", "service", "reported_by", "application")
            .order_by("-performed_at", "-id")
        )
        invoices_query = (
            ClientInvoice.objects.filter(
                application__in=applications,
                billing_contour=ClientInvoice.CONTOUR_FBS,
            )
            .select_related("application", "client")
            .order_by("-invoice_date", "-id")
        )
        facts = list(facts_query[:300])
        invoices = list(invoices_query[:100])
        ctx.update(
            {
                "fbs_applications": application_rows,
                "fbs_facts": facts,
                "fbs_invoices": invoices,
                "fbs_clients": filter_agencies_for_user(Agency.objects.all(), self.request)
                .alias(
                    has_fbs_application=Exists(
                        BillingApplication.objects.filter(
                            client_id=OuterRef("pk"),
                            application_type=BillingApplication.TYPE_FBS,
                        )
                    ),
                    has_fbs_profile=Exists(
                        FbsIntegrationProfile.objects.filter(agency_id=OuterRef("pk"))
                    ),
                    has_fbs_stock=Exists(
                        FbsStockBalance.objects.filter(agency_id=OuterRef("pk"))
                    ),
                    has_active_fbs_rate=Exists(
                        FbsClientRate.objects.filter(client_id=OuterRef("pk"), is_active=True)
                    ),
                )
                .filter(
                    Q(has_fbs_application=True)
                    | Q(has_fbs_profile=True)
                    | Q(has_fbs_stock=True)
                    | Q(has_active_fbs_rate=True)
                )
                .order_by("agn_name", "short_name"),
                "filter_client": client_id,
                "fbs_selected_client": selected_client,
                "fbs_date_from": date_from.isoformat(),
                "fbs_date_to": date_to.isoformat(),
                "fbs_rate_rows": (
                    list(
                        FbsClientRate.objects.filter(client=selected_client, is_active=True)
                        .order_by("operation", "valid_from", "liters_from", "id")
                    )
                    if selected_client
                    else []
                ),
                "fbs_can_edit_rates": get_billing_role(self.request) in {"accountant", "admin", "director"},
                "fbs_summary": {
                    "applications": len(application_rows),
                    "facts": facts_query.count(),
                    "invoices": invoices_query.count(),
                    "quantity": facts_query.aggregate(total=Sum("quantity"))["total"] or 0,
                },
            }
        )
        return ctx


class BillingFbsCatalogExportView(BillingBaseView):
    """Download a read-only per-client FBS SKU/litre catalogue."""

    def get(self, request, *args, **kwargs):
        client_id = str(request.GET.get("client") or "").strip()
        if not client_id.isdigit():
            raise Http404("Выберите клиента для выгрузки FBS-номенклатуры.")
        client = get_object_or_404(
            filter_agencies_for_user(Agency.objects.all(), request),
            pk=int(client_id),
        )
        from .fbs_catalog_export import build_fbs_catalog_export

        return build_fbs_catalog_export(client=client)


class BillingFbsKpUploadView(BillingBaseView):
    """Read-only FBS calculation from uploaded external acts and shipment report."""

    template_name = "billing/fbs_kp_upload.html"
    billing_section = "fbs"

    @staticmethod
    def _date_filters(request):
        today = timezone.localdate()
        default_from = _previous_complete_week_start(today)
        raw_from = (request.GET.get("date_from") or request.POST.get("date_from") or "").strip()
        raw_to = (request.GET.get("date_to") or request.POST.get("date_to") or "").strip()
        date_from = parse_date(raw_from) if raw_from else default_from
        date_to = parse_date(raw_to) if raw_to else default_from + timedelta(days=6)
        if not date_from or not date_to or date_from > date_to:
            raise ValidationError("Укажите корректный период FBS.")
        return date_from, date_to

    def _selected_client(self):
        client_id = str(self.request.GET.get("client") or self.request.POST.get("client") or "").strip()
        if not client_id:
            return None
        return get_object_or_404(filter_agencies_for_user(Agency.objects.all(), self.request), pk=client_id)

    def post(self, request, *args, **kwargs):
        action = (request.POST.get("action") or "preview").strip()
        try:
            client = self._selected_client()
            if client is None:
                raise ValidationError("Выберите клиента для расчета по КП.")
            date_from, date_to = self._date_filters(request)
            receiving_files = request.FILES.getlist("receiving_acts")
            shipping_report = request.FILES.get("shipping_report")
            if not receiving_files and not shipping_report:
                raise ValidationError("Загрузите PDF-акты приемки или Excel-отчет отгрузок.")

            from .fbs_kp_upload import build_kp_upload_workbook, calculate_kp_upload

            result = calculate_kp_upload(
                client=client,
                receiving_files=receiving_files,
                shipping_report_file=shipping_report,
                date_from=date_from,
                date_to=date_to,
            )
            if action == "download":
                content = build_kp_upload_workbook(result)
                filename = f"fbs-kp-{client.pk}-{date_from:%Y%m%d}-{date_to:%Y%m%d}.xlsx"
                response = HttpResponse(
                    content,
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
                response["Content-Disposition"] = f'attachment; filename="{filename}"'
                return response
            ctx = self.get_context_data(result=result)
            messages.success(
                request,
                "Расчет сформирован на экране. Данные не записаны в счета, акты, начисления или остатки.",
            )
            return self.render_to_response(ctx)
        except (ValidationError, ValueError) as exc:
            messages.error(request, "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc))
            return self.render_to_response(self.get_context_data())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        try:
            date_from, date_to = self._date_filters(self.request)
        except ValidationError:
            date_from = date_to = timezone.localdate()
        client_id = str(self.request.GET.get("client") or self.request.POST.get("client") or "").strip()
        ctx.update(
            {
                "fbs_clients": filter_agencies_for_user(
                    Agency.objects.filter(
                        Q(fbs_integration_profiles__isnull=False)
                        | Q(fbs_stock_balances__isnull=False)
                        | Q(fbs_client_rates__is_active=True)
                    ),
                    self.request,
                ).distinct().order_by("agn_name", "short_name"),
                "filter_client": client_id,
                "fbs_date_from": date_from.isoformat(),
                "fbs_date_to": date_to.isoformat(),
                "result": kwargs.get("result"),
            }
        )
        return ctx


class BillingStorageView(BillingBaseView):
    template_name = "billing/storage.html"
    active_nav = "billing_storage"
    billing_section = "storage"

    def get_context_data(self, **kwargs):
        from django.db.models import Count, Q as _Q, Sum

        from .models import BillingStorageDay, StorageBillingError

        ctx = super().get_context_data(**kwargs)
        today = timezone.localdate()
        year = int(self.request.GET.get("year") or today.year)
        month = int(self.request.GET.get("month") or today.month)
        if not 1 <= month <= 12:
            month = today.month
        month_start = today.replace(year=year, month=month, day=1)
        if month == 12:
            month_end = month_start.replace(year=year + 1, month=1) - timedelta(days=1)
        else:
            month_end = month_start.replace(month=month + 1) - timedelta(days=1)
        date_from = parse_date((self.request.GET.get("date_from") or "").strip()) or month_start
        date_to = parse_date((self.request.GET.get("date_to") or "").strip()) or month_end
        if date_to < date_from:
            date_from, date_to = date_to, date_from
        year = date_from.year
        month = date_from.month
        client_id = self.request.GET.get("client") or ""
        status = (self.request.GET.get("status") or "").strip()
        portfolio = filter_agencies_for_user(Agency.objects.all(), self.request)
        qs = BillingStorageDay.objects.select_related(
            "client", "charge", "application", "tariff_version"
        ).filter(day__range=(date_from, date_to), client__in=portfolio)
        if client_id:
            qs = qs.filter(client_id=client_id)
        if status == BillingStorageDay.STATUS_IN_INVOICE:
            # The financial source of truth is the charge relation: legacy rows
            # can still carry ``calculated`` after their charge entered an invoice.
            qs = qs.filter(charge__is_included_in_invoice=True)
        elif status:
            qs = qs.filter(status=status)
            if status in {
                BillingStorageDay.STATUS_PRELIMINARY,
                BillingStorageDay.STATUS_CALCULATED,
                BillingStorageDay.STATUS_NEEDS_REVIEW,
                BillingStorageDay.STATUS_CONFIRMED,
            }:
                qs = qs.exclude(charge__is_included_in_invoice=True)
        if self.request.GET.get("needs_review") in {"1", "true", "True"}:
            qs = qs.filter(status=BillingStorageDay.STATUS_NEEDS_REVIEW)
        agg = qs.aggregate(
            days=Count("id"),
            amount=Sum("amount"),
            pallets=Sum("pallet_count"),
            review=Count("id", filter=_Q(status=BillingStorageDay.STATUS_NEEDS_REVIEW)),
        )
        open_errors = StorageBillingError.objects.filter(
            client__in=portfolio,
            resolved_at__isnull=True,
            day__range=(date_from, date_to),
        )
        if client_id:
            open_errors = open_errors.filter(client_id=client_id)
        open_errors = open_errors.select_related("client").order_by("-created_at")[:40]
        ctx.update(
            {
                "year": year,
                "month": month,
                "storage_filter_date_from": date_from.isoformat(),
                "storage_filter_date_to": date_to.isoformat(),
                "filter_client": client_id,
                "filter_status": status,
                "storage_days": qs.order_by("-day")[:300],
                "clients": portfolio.order_by("agn_name")[:500],
                "storage_kpi_days": agg["days"] or 0,
                "storage_kpi_amount": agg["amount"] or 0,
                "storage_kpi_pallets": agg["pallets"] or 0,
                "storage_kpi_review": agg["review"] or 0,
                "storage_open_errors": open_errors,
                "storage_status_choices": [
                    (code, "Счёт сформирован" if code == BillingStorageDay.STATUS_IN_INVOICE else label)
                    for code, label in BillingStorageDay.STATUS_CHOICES
                ],
            }
        )
        return ctx


class BillingRequestsView(BillingBaseView):
    """Заявки к выставлению: подтверждённые объёмы → документы."""

    template_name = "billing/requests.html"
    billing_section = "requests"

    def get_context_data(self, **kwargs):
        from .permissions import can_mark_billing_external

        ctx = super().get_context_data(**kwargs)
        rows = applications_ready_to_invoice(self.request)
        client_id = self.request.GET.get("client") or ""
        if client_id:
            rows = [r for r in rows if str(r["client"].id) == str(client_id)]
        tab = (self.request.GET.get("tab") or "").strip()
        only_ready = self.request.GET.get("ready") in {"1", "true", "True"}
        if tab not in {"ready", "blocked", "all"}:
            tab = "ready" if only_ready else "all"
        ready_rows = [r for r in rows if r["ready"]]
        blocked_rows = [r for r in rows if not r["ready"]]
        if tab == "ready":
            visible = ready_rows
        elif tab == "blocked":
            visible = blocked_rows
        else:
            visible = rows
        ctx.update(
            {
                "request_rows": visible,
                "requests_ready_count": len(ready_rows),
                "requests_blocked_count": len(blocked_rows),
                "filter_client": client_id,
                "filter_ready": only_ready or tab == "ready",
                "filter_tab": tab,
                "can_mark_billing_external": can_mark_billing_external(self.request),
                "request_clients": filter_agencies_for_user(Agency.objects.all(), self.request).order_by(
                    "agn_name"
                )[:500],
            }
        )
        return ctx


class BillingExecutedServicesView(BillingBaseView):
    """Реестр выполненных складских услуг для биллинга."""

    template_name = "billing/executed_services.html"
    billing_section = "executed-services"

    def get_context_data(self, **kwargs):
        from .charge_status import (
            NO_PRICE_MESSAGE,
            charge_has_agreed_price,
            charge_manager_status,
            charge_missing_price,
        )
        from .permissions import can_manage_charges

        ctx = super().get_context_data(**kwargs)
        base_portfolio = filter_agencies_for_user(
            Agency.objects.all(),
            self.billing_permission_subject,
        )
        today = timezone.localdate()
        date_from_raw = (self.request.GET.get("date_from") or "").strip()
        date_to_raw = (self.request.GET.get("date_to") or "").strip()
        date_from = parse_date(date_from_raw) if date_from_raw else today.replace(day=1)
        date_to = parse_date(date_to_raw) if date_to_raw else today
        client_id = (self.request.GET.get("client") or "").strip()
        order_type = (self.request.GET.get("order_type") or "").strip()
        status = (self.request.GET.get("status") or "").strip()
        tariff_filter = (self.request.GET.get("tariff") or "").strip()
        page_size_raw = (self.request.GET.get("page_size") or "50").strip()
        page_size = int(page_size_raw) if page_size_raw in {"50", "100", "200"} else 50
        selected_stage = (self.request.GET.get("stage") or "work").strip()
        if tariff_filter == "missing":
            selected_stage = "missing"
        elif tariff_filter == "confirmed_missing":
            selected_stage = "confirmed_missing"
        if selected_stage not in BILLING_SERVICE_STAGE_KEYS:
            selected_stage = "work"

        portfolio = base_portfolio
        if client_id:
            portfolio = portfolio.filter(pk=client_id)

        stage_counts = _billing_service_stage_counts(
            portfolio,
            date_from=date_from,
            date_to=date_to,
            order_type=order_type,
        )
        card_query = {}
        if client_id:
            card_query["client"] = client_id
        if order_type:
            card_query["order_type"] = order_type
        card_query["date_from"] = date_from.isoformat()
        card_query["date_to"] = date_to.isoformat()
        card_query["page_size"] = page_size
        stage_cards = _billing_stage_cards(
            stage_counts,
            selected_stage=selected_stage,
            query=card_query,
        )

        type_definitions = (
            ("", "Все"),
            (BillingApplication.TYPE_RECEIVING, "Приёмка"),
            (BillingApplication.TYPE_PROCESSING, "Обработка"),
            (BillingApplication.TYPE_SHIPPING, "Отгрузка"),
            (BillingApplication.TYPE_STORAGE, "Хранение"),
            (BillingApplication.TYPE_FBS, "FBS"),
            ("extra", "Доп. услуги"),
        )
        type_links = []
        for code, label in type_definitions:
            params = {
                "stage": selected_stage,
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
            }
            if client_id:
                params["client"] = client_id
            if status:
                params["status"] = status
            params["page_size"] = page_size
            if tariff_filter and tariff_filter not in {"missing", "confirmed_missing"}:
                params["tariff"] = tariff_filter
            if code:
                params["order_type"] = code
            type_links.append(
                {
                    "code": code,
                    "label": label,
                    "active": order_type == code,
                    "href": f"/team-manager/billing/services/?{urlencode(params)}",
                }
            )

        detail_report_href = {
            BillingApplication.TYPE_RECEIVING: "/team-manager/billing/receiving-report/",
            BillingApplication.TYPE_PROCESSING: "/team-manager/billing/processing-report/",
            BillingApplication.TYPE_SHIPPING: "/team-manager/billing/shipping-pick-report/",
            BillingApplication.TYPE_STORAGE: "/team-manager/billing/storage/",
            BillingApplication.TYPE_FBS: "/team-manager/billing/fbs/",
            "extra": "/team-manager/billing/extra-services/",
        }.get(order_type, "")

        can_manage = can_manage_charges(self.billing_permission_subject)
        rows = []

        def append_charge_row(charge, *, fact=None):
            status_code, status_label, status_badge = charge_manager_status(charge)
            missing_price = charge_missing_price(charge)
            has_agreed_price = charge_has_agreed_price(charge)
            rows.append(
                {
                    "kind": "fact" if fact is not None else "charge",
                    "fact": fact,
                    "charge": charge,
                    "client": charge.client,
                    "application": charge.application,
                    "status_code": status_code,
                    "status_label": status_label,
                    "status_badge": status_badge,
                    "missing_price": missing_price,
                    "has_agreed_price": has_agreed_price,
                    "can_confirm": (
                        can_manage
                        and has_agreed_price
                        and not charge.is_confirmed
                        and not charge.is_excluded
                        and not charge.is_disputed
                        and not charge.is_included_in_act
                        and not charge.is_included_in_invoice
                    ),
                    "confirm_disabled": (
                        can_manage
                        and missing_price
                        and not charge.is_confirmed
                        and not charge.is_excluded
                        and not charge.is_included_in_act
                        and not charge.is_included_in_invoice
                    ),
                }
            )

        if selected_stage == "work":
            qs = (
                WarehouseServiceFact.objects.filter(client__in=portfolio)
                .select_related(
                    "client",
                    "service",
                    "reported_by",
                    "application",
                    "charge",
                    "charge__application",
                    "charge__client",
                    "charge__service",
                    "charge__client_tariff_version",
                )
            )
            if date_from:
                qs = qs.filter(reported_at__date__gte=date_from)
            if date_to:
                qs = qs.filter(reported_at__date__lte=date_to)
            if order_type == "extra":
                qs = qs.filter(order_type__in=BILLING_SERVICE_EXTRA_TYPES)
            elif order_type:
                qs = qs.filter(order_type=order_type)
            if status:
                qs = qs.filter(status=status)
            if tariff_filter == "priced":
                qs = qs.filter(charge__client_tariff_version__isnull=False)
            elif tariff_filter == "invoice":
                qs = qs.filter(charge__is_included_in_invoice=True)

            paginator = Paginator(qs.order_by("-reported_at", "-id"), page_size)
            page_obj = paginator.get_page(self.request.GET.get("page"))
            total_rows = paginator.count
            facts = list(page_obj.object_list)
            fact_badges = {
                WarehouseServiceFact.STATUS_APPROVED: "orange",
                WarehouseServiceFact.STATUS_CHARGED: "orange",
                WarehouseServiceFact.STATUS_NEEDS_CLARIFICATION: "red",
                WarehouseServiceFact.STATUS_CANCELLED: "gray",
            }
            for fact in facts:
                if fact.charge_id:
                    append_charge_row(fact.charge, fact=fact)
                    continue
                rows.append(
                    {
                        "kind": "fact",
                        "fact": fact,
                        "charge": None,
                        "client": fact.client,
                        "application": fact.application,
                        "status_code": fact.status,
                        "status_label": fact.get_status_display(),
                        "status_badge": fact_badges.get(fact.status, "gray"),
                        "missing_price": False,
                        "has_agreed_price": False,
                        "can_confirm": False,
                        "confirm_disabled": False,
                    }
                )
        else:
            charge_qs = (
                ApplicationCharge.objects.filter(client__in=portfolio)
                .select_related(
                    "client",
                    "service",
                    "application",
                    "client_tariff_version",
                )
            )
            if order_type == "extra":
                charge_qs = charge_qs.filter(application__application_type__in=BILLING_SERVICE_EXTRA_TYPES)
            elif order_type:
                charge_qs = charge_qs.filter(application__application_type=order_type)
            if selected_stage == "missing":
                charge_qs = charge_qs.filter(
                    is_excluded=False,
                    client_tariff_version__isnull=True,
                    is_manual_override=False,
                )
            elif selected_stage == "waiting":
                charge_qs = charge_qs.filter(
                    is_excluded=False,
                    is_confirmed=False,
                    client_tariff_version__isnull=False,
                )
            elif selected_stage == "confirmed":
                charge_qs = charge_qs.filter(
                    is_confirmed=True,
                    is_excluded=False,
                    is_included_in_act=False,
                )
            elif selected_stage == "confirmed_missing":
                charge_qs = charge_qs.filter(
                    is_confirmed=True,
                    is_excluded=False,
                    client_tariff_version__isnull=True,
                    is_manual_override=False,
                )
            elif selected_stage == "draft_acts":
                charge_qs = charge_qs.filter(act_lines__act__status=ActStatus.DRAFT).distinct()

            paginator = Paginator(charge_qs.order_by("-performed_at", "-id"), page_size)
            page_obj = paginator.get_page(self.request.GET.get("page"))
            total_rows = paginator.count
            for charge in page_obj.object_list:
                append_charge_row(charge)

        pagination_params = self.request.GET.copy()
        pagination_params.pop("page", None)

        ctx.update(
            {
                "executed_rows": rows,
                "executed_services_total": total_rows,
                "executed_page": page_obj,
                "executed_page_query": pagination_params.urlencode(),
                "executed_page_size": page_size,
                "executed_page_size_options": [
                    {"value": value, "selected": value == page_size}
                    for value in (50, 100, 200)
                ],
                "executed_filter_date_from": date_from.isoformat() if date_from else "",
                "executed_filter_date_to": date_to.isoformat() if date_to else "",
                "executed_filter_client": client_id,
                "executed_filter_order_type": order_type,
                "executed_filter_status": status,
                "executed_filter_tariff": tariff_filter,
                "executed_selected_stage": selected_stage,
                "executed_stage_cards": stage_cards,
                "executed_type_links": type_links,
                "executed_detail_report_href": detail_report_href,
                "executed_clients": base_portfolio.order_by("agn_name")[:500],
                "executed_status_choices": WarehouseServiceFact.STATUS_CHOICES,
                "executed_fbs_billing_disabled": (
                    order_type == BillingApplication.TYPE_FBS
                    and not getattr(settings, "FBS_BILLING_ENABLED", False)
                ),
                "executed_no_price_message": NO_PRICE_MESSAGE,
                "executed_review_services": BillingService.objects.filter(is_active=True).order_by(
                    "name", "code"
                )[:1000],
                "executed_can_review_unlisted": (
                    bool(getattr(self.request.user, "is_superuser", False))
                    or (get_billing_role(self.billing_permission_subject) or "")
                    in {"manager", "head_manager", "admin", "director"}
                ),
            }
        )
        return ctx


class BillingShippingPickReportView(BillingBaseView):
    """Read-only report: whole-box vs piece picking by shipping order."""

    template_name = "billing/shipping_pick_report.html"
    billing_section = "shipping-pick-report"
    SORT_OPTIONS = (
        ("ship_date_asc", "Дата отгрузки: сначала ранние"),
        ("ship_date_desc", "Дата отгрузки: сначала поздние"),
        ("application_asc", "Заявка: по возрастанию"),
        ("application_desc", "Заявка: по убыванию"),
    )
    SORT_ORDERING = {
        "ship_date_asc": ("planned_ship_date", "number", "id"),
        "ship_date_desc": ("-planned_ship_date", "-number", "-id"),
        "application_asc": ("number", "id"),
        "application_desc": ("-number", "-id"),
    }

    def get_context_data(self, **kwargs):
        from collections import defaultdict

        from django.db.models import Q as _Q

        from shipping.models import ShippingOrder

        from .warehouse_services import (
            SHIPPING_PICK_BOX_BANDS,
            SHIPPING_PICK_PIECE_SERVICE_CODE,
            shipping_pick_auto_quantities,
        )

        ctx = super().get_context_data(**kwargs)

        today = timezone.localdate()
        default_from = today - timedelta(days=today.weekday() + 7)
        date_from_raw = (self.request.GET.get("date_from") or "").strip()
        date_to_raw = (self.request.GET.get("date_to") or "").strip()
        date_from = parse_date(date_from_raw) if date_from_raw else default_from
        date_to = parse_date(date_to_raw) if date_to_raw else default_from + timedelta(days=6)
        client_id = (self.request.GET.get("client") or "").strip()
        status = (self.request.GET.get("status") or "").strip()
        delivery_type = (self.request.GET.get("delivery_type") or "").strip()
        q = (self.request.GET.get("q") or "").strip()
        show_empty = (self.request.GET.get("show_empty") or "").strip() == "1"
        sort = (self.request.GET.get("sort") or "ship_date_asc").strip()
        if sort not in self.SORT_ORDERING:
            sort = "ship_date_asc"

        portfolio = filter_agencies_for_user(Agency.objects.all(), self.request)
        qs = (
            ShippingOrder.objects.filter(agency__in=portfolio)
            .select_related("agency", "marketplace")
            .annotate(goods_units=Sum("items__qty_requested"))
        )
        # The report follows the shipment date specified by the manager in
        # the request.  Do not replace it with the later factual warehouse
        # timestamp: the requested date is the business date for billing.
        if date_from:
            qs = qs.filter(planned_ship_date__gte=date_from)
        if date_to:
            qs = qs.filter(planned_ship_date__lte=date_to)
        if client_id:
            qs = qs.filter(agency_id=client_id)
        if status:
            qs = qs.filter(status=status)
        if delivery_type:
            valid_delivery_types = {code for code, _label in ShippingOrder.DELIVERY_TYPE_CHOICES}
            if delivery_type in valid_delivery_types:
                qs = qs.filter(delivery_type=delivery_type)
        if q:
            qs = qs.filter(
                _Q(number__icontains=q)
                | _Q(agency__agn_name__icontains=q)
                | _Q(agency__short_name__icontains=q)
                | _Q(destination_warehouse__icontains=q)
                | _Q(destination_address__icontains=q)
                | _Q(transit_address__icontains=q)
                | _Q(supply_number__icontains=q)
                | _Q(shipping_barcode__icontains=q)
            )

        orders = list(qs.order_by(*self.SORT_ORDERING[sort])[:300])
        order_numbers = [order.number for order in orders]
        box_codes = [code for code, _limit, _label in SHIPPING_PICK_BOX_BANDS]
        pick_codes = set(box_codes)
        pick_codes.add(SHIPPING_PICK_PIECE_SERVICE_CODE)

        facts_by_order: dict[tuple[int, str], list[WarehouseServiceFact]] = defaultdict(list)
        if order_numbers:
            for fact in (
                WarehouseServiceFact.objects.filter(
                    client__in=portfolio,
                    order_type=WarehouseServiceFact.ORDER_SHIPPING,
                    order_id__in=order_numbers,
                    service__code__in=pick_codes,
                )
                .select_related("service", "application", "charge")
                .order_by("order_id", "service__code", "id")
            ):
                facts_by_order[(fact.client_id, fact.order_id)].append(fact)

        applications_by_order: dict[tuple[int, str], BillingApplication] = {}
        if order_numbers:
            for application in (
                BillingApplication.objects.filter(
                    client__in=portfolio,
                    application_type=BillingApplication.TYPE_SHIPPING,
                    application_id__in=order_numbers,
                )
                .select_related("client")
                .order_by("-id")
            ):
                applications_by_order.setdefault((application.client_id, application.application_id), application)

        rows = []
        totals = {
            "orders": 0,
            "goods_units": 0,
            "whole_boxes": 0,
            "piece_units": 0,
            "unclassified_boxes": 0,
            "fact_orders": 0,
            "charged_orders": 0,
        }
        client_totals: dict[int, dict] = {}
        for order in orders:
            key = (order.agency_id, order.number)
            try:
                auto = shipping_pick_auto_quantities(client=order.agency, order_id=order.number)
            except Exception as exc:
                auto = {
                    "quantities": {},
                    "total_boxes": 0,
                    "classified_boxes": 0,
                    "unclassified_boxes": 0,
                    "piece_quantity": 0,
                    "warnings": [str(exc)],
                }
            whole_boxes = sum(int(auto["quantities"].get(code) or 0) for code in box_codes)
            piece_units = int(auto["quantities"].get(SHIPPING_PICK_PIECE_SERVICE_CODE) or 0)
            goods_units = int(order.goods_units or 0)
            if not show_empty and not whole_boxes and not piece_units:
                continue

            facts = facts_by_order.get(key, [])
            application = applications_by_order.get(key)
            manual_invoice = {}
            if application:
                manual_invoice = (application.source_payload or {}).get("shipping_pick_manual_invoice") or {}
            manual_invoice_number = str(manual_invoice.get("number") or "").strip()
            manual_invoice_is_issued = bool(manual_invoice.get("is_issued"))
            charges = [
                fact.charge
                for fact in facts
                if fact.charge_id and fact.charge and not getattr(fact.charge, "is_excluded", False)
            ]
            box_breakdown = [
                {
                    "code": code,
                    "label": label,
                    "quantity": int(auto["quantities"].get(code) or 0),
                }
                for code, _limit, label in SHIPPING_PICK_BOX_BANDS
            ]
            row = {
                "order": order,
                "client": order.agency,
                "application": application,
                "transit_warehouse": str(order.transit_address or "").strip(),
                "manual_invoice": {
                    "number": manual_invoice_number,
                    "is_issued": manual_invoice_is_issued,
                },
                "goods_units": goods_units,
                "whole_boxes": whole_boxes,
                "piece_units": piece_units,
                "total_pick_units": whole_boxes + piece_units,
                "classified_boxes": int(auto.get("classified_boxes") or 0),
                "unclassified_boxes": int(auto.get("unclassified_boxes") or 0),
                "box_breakdown": box_breakdown,
                "facts_count": len(facts),
                "charges_count": len(charges),
                "warnings": auto.get("warnings") or [],
            }
            rows.append(row)

            totals["orders"] += 1
            totals["goods_units"] += goods_units
            totals["whole_boxes"] += whole_boxes
            totals["piece_units"] += piece_units
            totals["unclassified_boxes"] += row["unclassified_boxes"]
            if facts:
                totals["fact_orders"] += 1
            if charges:
                totals["charged_orders"] += 1

            client_row = client_totals.setdefault(
                order.agency_id,
                {
                    "client": order.agency,
                    "orders": 0,
                    "goods_units": 0,
                    "whole_boxes": 0,
                    "piece_units": 0,
                    "unclassified_boxes": 0,
                },
            )
            client_row["orders"] += 1
            client_row["goods_units"] += goods_units
            client_row["whole_boxes"] += whole_boxes
            client_row["piece_units"] += piece_units
            client_row["unclassified_boxes"] += row["unclassified_boxes"]

        ctx.update(
            {
                "shipping_pick_rows": rows,
                "shipping_pick_total_orders_scanned": len(orders),
                "shipping_pick_totals": totals,
                "shipping_pick_client_totals": sorted(
                    client_totals.values(),
                    key=lambda row: (row["client"].agn_name or row["client"].short_name or ""),
                ),
                "shipping_pick_filter_date_from": date_from.isoformat() if date_from else "",
                "shipping_pick_filter_date_to": date_to.isoformat() if date_to else "",
                "shipping_pick_filter_client": client_id,
                "shipping_pick_filter_status": status,
                "shipping_pick_filter_delivery_type": delivery_type,
                "shipping_pick_filter_q": q,
                "shipping_pick_filter_show_empty": show_empty,
                "shipping_pick_filter_sort": sort,
                "shipping_pick_sort_options": self.SORT_OPTIONS,
                "shipping_pick_clients": portfolio.order_by("agn_name")[:500],
                "shipping_pick_status_choices": getattr(ShippingOrder, "STATUS_CHOICES", ()),
                "shipping_pick_delivery_type_choices": ShippingOrder.DELIVERY_TYPE_CHOICES,
                "shipping_pick_is_default_week": date_from == default_from and date_to == default_from + timedelta(days=6),
            }
        )
        return ctx


class BillingOperationServicesReportView(BillingBaseView):
    """Read-only report for warehouse service facts grouped by billing application."""

    template_name = "billing/operation_services_report.html"
    report_application_type = ""
    report_fact_order_type = ""
    report_title = ""
    report_subtitle = ""
    report_empty_hint = ""
    report_reset_url = ""

    def _payload_fact_rows(self, application: BillingApplication) -> list[dict]:
        payload = application.source_payload if isinstance(application.source_payload, dict) else {}
        rows = payload.get("service_facts") or []
        if not isinstance(rows, list):
            return []
        result = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("service_name") or "").strip()
            quantity = str(item.get("quantity") or "").strip()
            unit = str(item.get("unit") or "").strip() or "шт"
            if not name and not quantity:
                continue
            result.append(
                {
                    "name": name or "Услуга склада",
                    "quantity": quantity or "0",
                    "unit": unit,
                    "status_label": str(item.get("status_label") or "").strip(),
                    "comment": str(item.get("comment") or item.get("discrepancy_reason") or "").strip(),
                }
            )
        return result

    def get_context_data(self, **kwargs):
        from collections import defaultdict

        from django.db.models import Q as _Q

        ctx = super().get_context_data(**kwargs)

        today = timezone.localdate()
        default_from = today - timedelta(days=today.weekday() + 7)
        date_from_raw = (self.request.GET.get("date_from") or "").strip()
        date_to_raw = (self.request.GET.get("date_to") or "").strip()
        date_from = parse_date(date_from_raw) if date_from_raw else default_from
        date_to = parse_date(date_to_raw) if date_to_raw else default_from + timedelta(days=6)
        client_id = (self.request.GET.get("client") or "").strip()
        billing_status = (self.request.GET.get("billing_status") or "").strip()
        q = (self.request.GET.get("q") or "").strip()
        show_empty = (self.request.GET.get("show_empty") or "").strip() == "1"

        portfolio = filter_agencies_for_user(Agency.objects.all(), self.request)
        applications_qs = (
            filter_applications_for_user(
                BillingApplication.objects.filter(
                    application_type=self.report_application_type,
                    client__in=portfolio,
                ),
                self.request,
            )
            .select_related("client", "manager", "warehouse")
            .order_by("-operations_completed_at", "-created_at_source", "-id")
        )
        if date_from:
            applications_qs = applications_qs.filter(
                _Q(operations_completed_at__date__gte=date_from)
                | _Q(operations_completed_at__isnull=True, created_at_source__date__gte=date_from)
            )
        if date_to:
            applications_qs = applications_qs.filter(
                _Q(operations_completed_at__date__lte=date_to)
                | _Q(operations_completed_at__isnull=True, created_at_source__date__lte=date_to)
            )
        if client_id:
            applications_qs = applications_qs.filter(client_id=client_id)
        if billing_status:
            applications_qs = applications_qs.filter(billing_status=billing_status)
        if q:
            applications_qs = applications_qs.filter(
                _Q(application_id__icontains=q)
                | _Q(client__agn_name__icontains=q)
                | _Q(client__short_name__icontains=q)
                | _Q(client__inn__icontains=q)
                | _Q(warehouse_label__icontains=q)
                | _Q(operational_status_label__icontains=q)
            )

        applications = list(applications_qs[:300])
        application_ids = [application.id for application in applications]
        order_ids = [str(application.application_id) for application in applications]

        facts_by_application: dict[int, list[WarehouseServiceFact]] = defaultdict(list)
        facts_by_key: dict[tuple[int, str], list[WarehouseServiceFact]] = defaultdict(list)
        if applications:
            facts_qs = (
                WarehouseServiceFact.objects.filter(
                    client__in=portfolio,
                    order_type=self.report_fact_order_type,
                )
                .filter(_Q(application_id__in=application_ids) | _Q(order_id__in=order_ids))
                .select_related("service", "application", "charge", "reported_by")
                .order_by("order_id", "service__sort_order", "service__name", "id")
            )
            for fact in facts_qs:
                if fact.application_id:
                    facts_by_application[fact.application_id].append(fact)
                facts_by_key[(fact.client_id, str(fact.order_id))].append(fact)

        charges_by_application: dict[int, list[ApplicationCharge]] = defaultdict(list)
        if application_ids:
            for charge in (
                ApplicationCharge.objects.filter(application_id__in=application_ids)
                .select_related("service")
                .order_by("service__sort_order", "service__name", "id")
            ):
                charges_by_application[charge.application_id].append(charge)

        rows = []
        totals = {
            "applications": 0,
            "facts": 0,
            "charges": 0,
            "without_facts": 0,
            "without_charges": 0,
            "total_amount": Decimal("0"),
            "manual_invoiced": 0,
        }
        client_totals: dict[int, dict] = {}
        for application in applications:
            key = (application.client_id, str(application.application_id))
            facts = facts_by_application.get(application.id) or facts_by_key.get(key, [])
            payload_facts = [] if facts else self._payload_fact_rows(application)
            active_charges = [
                charge
                for charge in charges_by_application.get(application.id, [])
                if not getattr(charge, "is_excluded", False)
            ]
            if not show_empty and not facts and not payload_facts and not active_charges:
                continue

            manual_invoice = (application.source_payload or {}).get("shipping_pick_manual_invoice") or {}
            manual_invoice_number = str(manual_invoice.get("number") or "").strip()
            manual_invoice_is_issued = bool(manual_invoice.get("is_issued"))
            charge_total = sum((charge.total_amount or Decimal("0")) for charge in active_charges)
            fact_rows = [
                {
                    "name": fact.service_name_snapshot or getattr(fact.service, "name", "") or "Услуга склада",
                    "quantity": fact.quantity,
                    "unit": fact.unit or getattr(fact.service, "unit", "") or "шт",
                    "status_label": fact.get_status_display(),
                    "comment": fact.comment or fact.discrepancy_reason or "",
                }
                for fact in facts
            ] or payload_facts
            charge_rows = [
                {
                    "name": charge.service_name_snapshot or getattr(charge.service, "name", "") or "Услуга",
                    "quantity": charge.quantity,
                    "unit": charge.unit,
                    "total": charge.total_amount,
                    "is_confirmed": charge.is_confirmed,
                }
                for charge in active_charges
            ]
            rows.append(
                {
                    "application": application,
                    "client": application.client,
                    "facts": fact_rows,
                    "charges": charge_rows,
                    "facts_count": len(fact_rows),
                    "charges_count": len(active_charges),
                    "charge_total": charge_total,
                    "manual_invoice": {
                        "number": manual_invoice_number,
                        "is_issued": manual_invoice_is_issued,
                    },
                }
            )

            totals["applications"] += 1
            totals["facts"] += len(fact_rows)
            totals["charges"] += len(active_charges)
            totals["total_amount"] += charge_total
            if not fact_rows:
                totals["without_facts"] += 1
            if not active_charges:
                totals["without_charges"] += 1
            if manual_invoice_is_issued:
                totals["manual_invoiced"] += 1

            client_row = client_totals.setdefault(
                application.client_id,
                {
                    "client": application.client,
                    "applications": 0,
                    "facts": 0,
                    "charges": 0,
                    "total_amount": Decimal("0"),
                },
            )
            client_row["applications"] += 1
            client_row["facts"] += len(fact_rows)
            client_row["charges"] += len(active_charges)
            client_row["total_amount"] += charge_total

        ctx.update(
            {
                "operation_report_title": self.report_title,
                "operation_report_subtitle": self.report_subtitle,
                "operation_report_empty_hint": self.report_empty_hint,
                "operation_report_reset_url": self.report_reset_url,
                "operation_report_rows": rows,
                "operation_report_total_scanned": len(applications),
                "operation_report_totals": totals,
                "operation_report_client_totals": sorted(
                    client_totals.values(),
                    key=lambda row: (row["client"].agn_name or row["client"].short_name or ""),
                ),
                "operation_filter_date_from": date_from.isoformat() if date_from else "",
                "operation_filter_date_to": date_to.isoformat() if date_to else "",
                "operation_filter_client": client_id,
                "operation_filter_billing_status": billing_status,
                "operation_filter_q": q,
                "operation_filter_show_empty": show_empty,
                "operation_clients": portfolio.order_by("agn_name")[:500],
                "operation_billing_status_choices": BillingStatus.choices,
            }
        )
        return ctx


class BillingReceivingReportView(BillingOperationServicesReportView):
    billing_section = "receiving-report"
    report_application_type = BillingApplication.TYPE_RECEIVING
    report_fact_order_type = WarehouseServiceFact.ORDER_RECEIVING
    report_title = "Приемка"
    report_subtitle = (
        "Отчет по заявкам на приемку: факты склада, начисления и выставление счета в одном понятном списке. "
        "Данные читаются из биллинга и складских фактов, остатки не меняются."
    )
    report_empty_hint = "Включите “Показывать без фактов”, если нужно увидеть все заявки приемки периода."
    report_reset_url = "/team-manager/billing/receiving-report/"


class BillingProcessingReportView(BillingOperationServicesReportView):
    billing_section = "processing-report"
    report_application_type = BillingApplication.TYPE_PROCESSING
    report_fact_order_type = WarehouseServiceFact.ORDER_PROCESSING
    report_title = "Обработка"
    report_subtitle = (
        "Отчет по заявкам на обработку: какие услуги склад передал, что начислено и где нужен ручной счет. "
        "Данные читаются из биллинга и складских фактов, складские остатки не меняются."
    )
    report_empty_hint = "Включите “Показывать без фактов”, если нужно увидеть все заявки обработки периода."
    report_reset_url = "/team-manager/billing/processing-report/"


class BillingExtraServicesView(BillingBaseView):
    template_name = "billing/extra_services.html"
    billing_section = "extra-services"

    def get_context_data(self, **kwargs):
        from .extra_services import list_extra_service_requests

        ctx = super().get_context_data(**kwargs)
        status = (self.request.GET.get("status") or "").strip()
        client_id = self.request.GET.get("client") or ""
        portfolio = filter_agencies_for_user(Agency.objects.all(), self.request)
        ctx.update(
            {
                "extra_requests": list_extra_service_requests(
                    self.request, status=status, client_id=client_id
                )[:200],
                "filter_status": status,
                "filter_client": client_id,
                "extra_clients": portfolio.order_by("agn_name")[:500],
                "extra_services": BillingService.objects.filter(is_active=True).order_by("name", "code")[:500],
                "today": timezone.localdate(),
            }
        )
        return ctx


class BillingDocumentDraftsView(BillingBaseView):
    template_name = "billing/document_drafts.html"
    billing_section = "document-drafts"

    def get_context_data(self, **kwargs):
        from .document_review import list_document_drafts

        ctx = super().get_context_data(**kwargs)
        review = (self.request.GET.get("review") or "").strip()
        client_id = self.request.GET.get("client") or ""
        rows = list_document_drafts(self.request, review=review, client_id=client_id)
        ctx.update(
            {
                "draft_rows": rows[:200],
                "filter_review": review,
                "filter_client": client_id,
                "draft_clients": filter_agencies_for_user(Agency.objects.all(), self.request).order_by("agn_name")[
                    :500
                ],
                "submitted_count": sum(1 for r in rows if r["review_status"] == DocumentReviewStatus.SUBMITTED),
                "local_count": sum(1 for r in rows if r["review_status"] == DocumentReviewStatus.LOCAL),
                "returned_count": sum(1 for r in rows if r["review_status"] == DocumentReviewStatus.RETURNED),
            }
        )
        return ctx


class BillingUpdView(BillingBaseView):
    template_name = "billing/upd.html"
    billing_section = "upd"

    def get_context_data(self, **kwargs):
        from .document_review import list_upd_requests

        ctx = super().get_context_data(**kwargs)
        status = (self.request.GET.get("status") or "").strip()
        client_id = self.request.GET.get("client") or ""
        portfolio = filter_agencies_for_user(Agency.objects.all(), self.request)
        ctx.update(
            {
                "upd_requests": list_upd_requests(self.request, status=status, client_id=client_id)[:200],
                "filter_status": status,
                "filter_client": client_id,
                "upd_clients": portfolio.order_by("agn_name")[:500],
                "today": timezone.localdate(),
            }
        )
        return ctx


class BillingAuditView(BillingBaseView):
    template_name = "billing/audit.html"
    billing_section = "audit"

    def get_context_data(self, **kwargs):
        from .document_review import list_audit_timeline

        ctx = super().get_context_data(**kwargs)
        app_id = self.request.GET.get("application_id") or ""
        ctx.update(
            {
                "audit_events": list_audit_timeline(self.request, application_id=app_id, limit=150),
                "filter_application_id": app_id,
            }
        )
        return ctx


class BillingTariffsView(BillingBaseView):
    template_name = "billing/tariffs.html"
    billing_section = "tariffs"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from .service_catalog import ensure_service_catalog
        from .standard_price_catalog import seed_standard_prices

        ensure_service_catalog()
        seed_standard_prices()
        portfolio = filter_agencies_for_user(Agency.objects.all(), self.request)
        qs = ClientTariff.objects.select_related("client", "legal_entity", "service").filter(client__in=portfolio).order_by(
            "client__agn_name", "service__code", "-valid_from"
        )
        if self.request.GET.get("client"):
            qs = qs.filter(client_id=self.request.GET.get("client"))
        versions = ClientTariffVersion.objects.select_related("client", "contract", "manager").filter(client__in=portfolio)
        if not ctx.get("billing_can_edit_tariffs"):
            versions = versions.exclude(status=ClientTariffVersion.STATUS_DRAFT)
        ctx["tariffs"] = qs[:200]
        ctx["services"] = BillingService.objects.filter(is_active=True).order_by("code")
        ctx["clients"] = portfolio.order_by("agn_name", "short_name")[:500]
        ctx["own_companies"] = OwnCompany.objects.filter(is_active=True).order_by("-is_default", "name")
        ctx["contracts"] = (
            ClientBillingContract.objects.select_related("client", "own_company")
            .filter(client__in=portfolio)
            .order_by("client__agn_name", "-valid_from", "-id")[:200]
        )
        ctx["standard_prices"] = StandardServicePrice.objects.select_related("service").filter(is_active=True).order_by(
            "section_code", "line_no"
        )[:300]
        ctx["tariff_versions"] = versions.order_by("client__agn_name", "-valid_from", "-version_number")[:200]
        return ctx


class BillingApplicationDetailView(BillingBaseView):
    template_name = "billing/application_detail.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        application = billing_applications_queryset({}, user_or_request=self.request).get(pk=self.kwargs["pk"])
        ctx["application"] = application
        ctx["source_application_url"] = ""
        if application.application_type == BillingApplication.TYPE_SHIPPING:
            from django.urls import reverse
            from shipping.models import ShippingOrder

            source_order = (
                ShippingOrder.objects.filter(
                    number=str(application.application_id),
                    agency_id=application.client_id,
                )
                .first()
            )
            if source_order:
                ctx["source_application_url"] = (
                    f"{reverse('shipping:detail', args=[source_order.pk])}?client={application.client_id}"
                )
                # The requested date is authoritative for this billing view.
                # The payload is used only if the source order became absent.
                payload_ship_date = (application.source_payload or {}).get("shipping_planned_date") or ""
                ctx["shipping_request_ship_date"] = source_order.planned_ship_date or parse_date(payload_ship_date)
        elif application.application_type == BillingApplication.TYPE_RECEIVING:
            from django.urls import reverse

            source_payload = application.source_payload if isinstance(application.source_payload, dict) else {}
            source_order_id = str(source_payload.get("order_id") or application.application_id or "").strip()
            source_exists = source_order_id and OrderAuditEntry.objects.filter(
                order_id=source_order_id,
                order_type=BillingApplication.TYPE_RECEIVING,
                agency_id=application.client_id,
            ).exists()
            if source_exists:
                ctx["source_application_url"] = (
                    f"{reverse('orders-receiving-detail', args=[source_order_id])}"
                    f"?client={application.client_id}"
                )
        elif application.application_type == BillingApplication.TYPE_PROCESSING:
            source_payload = application.source_payload if isinstance(application.source_payload, dict) else {}
            source_order_id = str(source_payload.get("order_id") or application.application_id or "").strip()
            source_exists = source_order_id and OrderAuditEntry.objects.filter(
                order_id=source_order_id,
                order_type=BillingApplication.TYPE_PROCESSING,
                agency_id=application.client_id,
            ).exists()
            if source_exists:
                ctx["source_application_url"] = f"/orders/processing/{source_order_id}/?client={application.client_id}"
        billing_meta = (application.source_payload or {}).get("billing") or {}
        ctx["missing_tariffs"] = billing_meta.get("missing_tariffs") or []
        from .application_detail_ui import (
            billing_review_status_label,
            build_billing_steps,
            build_finance_summary,
            build_application_fact_summary,
            build_required_actions,
            charge_display_sort_key,
            confirm_blockers,
            enrich_charge_row,
            operational_status_display,
            storage_application_period_label,
            tariff_order_map,
        )
        from .charge_status import NO_PRICE_MESSAGE, charge_manager_status, charge_missing_price
        from .permissions import (
            can_manage_charges,
            can_mark_billing_external,
            can_override_billing_tariff,
            can_propose_client_tariff_price,
        )
        from .service_catalog import ensure_service_catalog
        from .tariff_services import active_tariff_version, list_agreed_billing_services

        on_date = None
        if application.created_at_source:
            on_date = timezone.localdate(application.created_at_source)
        elif application.created_at:
            on_date = timezone.localdate(application.created_at)
        active_tariff = active_tariff_version(application.client, on_date=on_date)

        charges = list(
            application.charges.select_related(
                "service",
                "client_tariff_version",
                "client_tariff_item",
                "client_tariff_item__category",
                "own_company",
            ).all()
        )
        storage_days = []
        storage_days_by_charge: dict[int, list] = {}
        if application.application_type == BillingApplication.TYPE_STORAGE:
            storage_days = list(application.storage_days.select_related("charge").order_by("day", "id"))
            for storage_day in storage_days:
                if storage_day.charge_id:
                    storage_days_by_charge.setdefault(storage_day.charge_id, []).append(storage_day)
        charge_order = tariff_order_map(active_tariff)
        charges.sort(key=lambda charge: charge_display_sort_key(charge, charge_order))
        charge_rows = []
        unconfirmed = 0
        no_price = 0
        for idx, charge in enumerate(charges, start=1):
            code, label, badge = charge_manager_status(charge)
            missing = charge_missing_price(charge)
            if missing:
                no_price += 1
            elif (
                not charge.is_excluded
                and not charge.is_confirmed
                and not charge.is_included_in_act
                and not charge.is_included_in_invoice
            ):
                unconfirmed += 1
            row = {
                "n": idx,
                "charge": charge,
                "status_code": code,
                "status_label": label,
                "status_badge": badge,
                "missing_price": missing,
                # Хранение рассчитывается складским контуром по дням. Менеджер может
                # подтвердить начисление, но не подменять автоматически рассчитанный объём.
                "can_edit_qty": (
                    application.application_type != BillingApplication.TYPE_STORAGE
                    and BillingWorkflowService.charge_editable_by_manager(charge)
                    and not missing
                ),
                "storage_days": storage_days_by_charge.get(charge.id, []),
            }
            charge_rows.append(enrich_charge_row(row))
        ctx["charge_rows"] = charge_rows
        ctx["charges_unconfirmed"] = unconfirmed
        ctx["charges_no_price"] = no_price
        open_ready = [
            row
            for row in charge_rows
            if not row["charge"].is_excluded
            and not row["charge"].is_included_in_act
            and not row["charge"].is_disputed
            and not row["missing_price"]
            and row["charge"].is_confirmed
        ]
        open_blocking = [
            row
            for row in charge_rows
            if not row["charge"].is_excluded
            and not row["charge"].is_included_in_act
            and not row["charge"].is_disputed
            and (row["missing_price"] or not row["charge"].is_confirmed)
        ]
        ctx["can_create_act"] = bool(open_ready) and not open_blocking

        ensure_service_catalog()
        agreed_services = list_agreed_billing_services(application.client, on_date=on_date)
        # Текущие услуги строк оставляем в списке, даже если позиция снята с тарифа.
        agreed_ids = {row["id"] for row in agreed_services}
        for ch in charges:
            if not ch.service_id or ch.service_id in agreed_ids:
                continue
            svc = ch.service
            agreed_services.append(
                {
                    "id": svc.id,
                    "code": svc.code or "",
                    "name": (ch.service_name_snapshot or svc.name or "").strip() or svc.name,
                    "unit": ch.unit or svc.unit or "шт",
                    "price": ch.tariff_price if ch.tariff_price is not None else ch.tariff,
                }
            )
            agreed_ids.add(svc.id)
        import json

        ctx["billing_services"] = agreed_services
        ctx["agreed_services_json"] = json.dumps(
            [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "unit": row["unit"],
                    "price": str(row["price"]),
                }
                for row in agreed_services
            ],
            ensure_ascii=False,
        )
        ctx["tariff_published"] = active_tariff is not None
        ctx["no_price_message"] = NO_PRICE_MESSAGE
        ctx["billing_can_override_price"] = can_override_billing_tariff(self.request)
        ctx["billing_can_propose_tariff_price"] = can_propose_client_tariff_price(self.request, application.client)
        ctx["can_manage_charges"] = can_manage_charges(self.request, application)

        billing_external = (application.source_payload or {}).get("billing_external") or {}
        ctx["billing_external"] = billing_external
        ctx["is_billed_outside"] = bool(
            application.billing_status == BillingStatus.CANCELLED and billing_external.get("reason") == "billed_outside"
        )
        ctx["can_mark_billing_external"] = (
            can_mark_billing_external(self.request, application)
            and application.billing_status
            not in {
                BillingStatus.CANCELLED,
                BillingStatus.FINANCIALLY_CLOSED,
                BillingStatus.PAID,
                BillingStatus.PARTIALLY_PAID,
            }
        )
        if application.application_type == BillingApplication.TYPE_STORAGE:
            ctx["storage_days"] = list(reversed(storage_days))
            ctx["storage_period_label"] = storage_application_period_label(application, storage_days)
        else:
            ctx["storage_days"] = []
            ctx["storage_period_label"] = ""
        from .warehouse_services import facts_payload, list_facts

        # Факты склада только для операционных типов; storage/other — без list_facts.
        if application.application_type in {
            BillingApplication.TYPE_RECEIVING,
            BillingApplication.TYPE_PROCESSING,
            BillingApplication.TYPE_PACKING,
            BillingApplication.TYPE_SHIPPING,
        }:
            warehouse_facts = list_facts(
                client=application.client,
                order_type=application.application_type,
                order_id=str(application.application_id),
            )
            ctx["warehouse_service_facts"] = facts_payload(warehouse_facts)
        else:
            warehouse_facts = []
            ctx["warehouse_service_facts"] = []
        ctx["application_comments"] = _application_comment_rows(application)
        application_fact_summary = build_application_fact_summary(application, warehouse_facts)
        if (
            application.application_type == BillingApplication.TYPE_SHIPPING
            and not application_fact_summary["has_any_fact"]
        ):
            source_fact_payload = _shipping_application_fact_payload(application)
            if source_fact_payload:
                application_fact_summary = build_application_fact_summary(
                    application,
                    warehouse_facts,
                    source_fact_payload=source_fact_payload,
                )
        ctx["application_fact_summary"] = application_fact_summary
        ctx["operational_status_display"] = operational_status_display(application)
        ctx["billing_review_status"] = billing_review_status_label(application)
        ctx["billing_steps"] = build_billing_steps(application)
        ctx["required_actions"] = build_required_actions(application, charge_rows)
        ctx["finance_summary"] = build_finance_summary(application, charges)
        blockers = confirm_blockers(application, charge_rows)
        confirmable_rows = [
            row
            for row in charge_rows
            if not row["charge"].is_excluded
            and not row["charge"].is_included_in_act
            and not row["charge"].is_included_in_invoice
            and not row["charge"].is_disputed
            and not row["charge"].is_confirmed
            and not row["missing_price"]
        ]
        active_rows = [row for row in charge_rows if not row["charge"].is_excluded]
        ctx["confirm_blockers"] = blockers
        ctx["can_confirm_calculation"] = not blockers and bool(confirmable_rows)
        ctx["calculation_confirmed"] = bool(active_rows) and not blockers and not confirmable_rows
        ctx["exclude_reason_choices"] = ApplicationCharge.EXCLUDE_REASON_CHOICES
        ctx["qty_basis_choices"] = ApplicationCharge.QTY_BASIS_CHOICES
        return ctx


class BillingReportView(BillingBaseView):
    """Совместимость /report/ → отчёты этапа 7."""

    template_name = "billing/reports.html"
    billing_section = "reports"

    def get(self, request, *args, **kwargs):
        from django.http import HttpResponse

        from .manager_reports import build_report, report_to_csv

        if request.GET.get("export") == "csv":
            payload = build_report(request, request.GET)
            content = report_to_csv(payload)
            resp = HttpResponse(content, content_type="text/csv; charset=utf-8")
            resp["Content-Disposition"] = f'attachment; filename="billing_report_{payload["report"]}.csv"'
            return resp
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        from .manager_reports import build_report

        ctx = super().get_context_data(**kwargs)
        payload = build_report(self.request, self.request.GET)
        columns = payload["columns"]
        payload["table_rows"] = [[row.get(col, "") for col in columns] for row in payload["rows"]]
        payload["table_totals"] = (
            [payload["totals"].get(col, "") for col in columns] if payload.get("totals") else None
        )
        ctx.update(payload)
        return ctx


class BillingReportsView(BillingReportView):
    """Основной URL /reports/."""


class BillingNotificationsView(BillingBaseView):
    template_name = "billing/notifications.html"
    billing_section = "notifications"

    def get_context_data(self, **kwargs):
        from .staff_notifications import KIND_LABELS, list_staff_notifications, sync_portfolio_alerts

        sync_portfolio_alerts(self.request, limit=40)
        ctx = super().get_context_data(**kwargs)
        unread_only = (self.request.GET.get("unread") or "") == "1"
        rows = list_staff_notifications(self.request, unread_only=unread_only)[:200]
        ctx.update(
            {
                "notifications": rows,
                "filter_unread": unread_only,
                "kind_labels": KIND_LABELS,
                "unread_count": list_staff_notifications(self.request, unread_only=True).count(),
            }
        )
        return ctx


class BillingChargesView(BillingBaseView):
    """Предварительный биллинг: все начисления с источником согласованного тарифа."""

    template_name = "billing/charges.html"
    billing_section = "charges"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from django.db.models import Q as _Q

        app_qs = filter_applications_for_user(BillingApplication.objects.all(), self.request).distinct()
        qs = (
            ApplicationCharge.objects.filter(application__in=app_qs)
            .select_related(
                "client",
                "service",
                "application",
                "client_tariff_version",
                "client_tariff_item",
            )
        )
        params = self.request.GET
        if params.get("client"):
            qs = qs.filter(client_id=params["client"])
        if params.get("application_type"):
            qs = qs.filter(application__application_type=params["application_type"])
        if params.get("billing_period"):
            qs = qs.filter(billing_period=params["billing_period"])
        if params.get("date_from"):
            qs = qs.filter(performed_at__date__gte=params["date_from"])
        if params.get("date_to"):
            qs = qs.filter(performed_at__date__lte=params["date_to"])
        if params.get("override") in {"1", "true", "True"}:
            qs = qs.filter(is_manual_override=True)
        if params.get("missing_tariff") in {"1", "true", "True"} or params.get("no_price") in {"1", "true", "True"}:
            qs = qs.filter(client_tariff_version__isnull=True, is_manual_override=False)
        if params.get("unconfirmed") in {"1", "true", "True"}:
            qs = qs.filter(is_confirmed=False, is_disputed=False).exclude(
                is_included_in_act=True
            ).exclude(is_included_in_invoice=True)
        if params.get("confirmed") in {"1", "true", "True"}:
            qs = qs.filter(is_confirmed=True)
        if params.get("q"):
            q = params["q"].strip()
            qs = qs.filter(
                _Q(service__name__icontains=q)
                | _Q(service_name_snapshot__icontains=q)
                | _Q(application__application_id__icontains=q)
                | _Q(client__agn_name__icontains=q)
                | _Q(client__short_name__icontains=q)
                | _Q(tariff_source_label__icontains=q)
            )
        agg = qs.aggregate(
            subtotal=Sum("amount"),
            vat=Sum("vat_amount"),
            total=Sum("total_amount"),
        )
        apps_count = qs.values("application_id").distinct().count()
        missing_apps = (
            app_qs.filter(source_payload__billing__missing_tariff_count__gt=0)
            .distinct()
            .count()
        )
        unconfirmed_count = (
            ApplicationCharge.objects.filter(application__in=app_qs, is_confirmed=False, is_disputed=False)
            .exclude(is_included_in_act=True)
            .exclude(is_included_in_invoice=True)
            .filter(_Q(client_tariff_version__isnull=False) | _Q(is_manual_override=True))
            .count()
        )
        no_price_count = ApplicationCharge.objects.filter(
            application__in=app_qs,
            client_tariff_version__isnull=True,
            is_manual_override=False,
        ).count()
        from .charge_status import charge_manager_status

        charge_rows = []
        for charge in qs.order_by("-performed_at", "-id")[:300]:
            code, label, badge = charge_manager_status(charge)
            charge_rows.append(
                {
                    "charge": charge,
                    "status_code": code,
                    "status_label": label,
                    "status_badge": badge,
                }
            )
        ctx.update(
            {
                "charge_rows": charge_rows,
                "charges": [row["charge"] for row in charge_rows],
                "charges_count": qs.count(),
                "charges_apps_count": apps_count,
                "charges_subtotal": agg["subtotal"] or 0,
                "charges_vat": agg["vat"] or 0,
                "charges_total": agg["total"] or 0,
                "charges_missing_apps": missing_apps,
                "charges_unconfirmed": unconfirmed_count,
                "charges_no_price": no_price_count,
                "charge_clients": Agency.objects.filter(billing_charges__application__in=app_qs)
                .distinct()
                .order_by("agn_name", "short_name"),
                "today": timezone.localdate(),
            }
        )
        return ctx


class BillingActsView(BillingBaseView):
    template_name = "billing/acts.html"
    billing_section = "acts"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from .statuses import ActStatus as _ActStatus
        app_qs = filter_applications_for_user(BillingApplication.objects.all(), self.request).distinct()
        qs = BillingAct.objects.filter(application__in=app_qs).select_related(
            "client", "application", "created_by", "confirmed_by"
        )
        params = self.request.GET
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("client"):
            qs = qs.filter(client_id=params["client"])
        if params.get("date_from"):
            qs = qs.filter(act_date__gte=params["date_from"])
        if params.get("date_to"):
            qs = qs.filter(act_date__lte=params["date_to"])
        if params.get("q"):
            from django.db.models import Q as _Q
            q = params["q"].strip()
            qs = qs.filter(
                _Q(number__icontains=q) |
                _Q(client__agn_name__icontains=q) |
                _Q(client__short_name__icontains=q) |
                _Q(application__application_id__icontains=q)
            )
        agg = qs.aggregate(total=Sum("total_amount"))
        ctx["acts"] = qs.order_by("-act_date", "-id")[:200]
        ctx["acts_total"] = agg["total"] or 0
        ctx["acts_count"] = qs.count()
        ctx["act_statuses"] = _ActStatus
        ctx["today"] = timezone.localdate()
        ctx["act_clients"] = (
            Agency.objects
            .filter(billing_acts__application__in=app_qs)
            .distinct()
            .order_by("agn_name", "short_name")
        )
        return ctx


class BillingInvoicesView(BillingBaseView):
    template_name = "billing/invoices.html"
    billing_section = "invoices"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from django.db.models import Sum as _Sum
        qs = invoices_queryset(self.request.GET, user_or_request=self.request)
        agg = qs.aggregate(
            total=_Sum("total_amount"),
            paid=_Sum("paid_amount"),
            debt=_Sum("debt_amount"),
        )
        invoices = list(qs.select_related("client", "act", "application").prefetch_related(
            "invoice_acts__act__application",
            "act__application",
        )[:200])
        from .storage_invoice_report import storage_report_invoice_ids

        storage_invoice_ids = storage_report_invoice_ids(invoices)
        for invoice in invoices:
            invoice.has_storage_report = invoice.id in storage_invoice_ids
        ctx["invoices"] = invoices
        ctx["invoice_statuses"] = InvoiceStatus
        ctx["invoices_total"] = agg["total"] or 0
        ctx["invoices_paid"] = agg["paid"] or 0
        ctx["invoices_debt"] = agg["debt"] or 0
        ctx["today"] = timezone.localdate()
        ctx["invoice_clients"] = (
            Agency.objects
            .filter(billing_invoices__isnull=False)
            .distinct()
            .order_by("agn_name", "short_name")
        )
        ctx["today"] = timezone.localdate()
        return ctx


class BillingInvoiceCreateView(BillingBaseView):
    """Сборка общего счёта из отгрузок или подтверждённых актов."""

    template_name = "billing/invoice_create.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        app_qs = filter_applications_for_user(BillingApplication.objects.all(), self.request).distinct()
        invoiced_ids = set(
            ClientInvoice.objects.exclude(status=InvoiceStatus.CANCELLED).values_list("act_id", flat=True)
        )
        invoiced_ids.update(
            ClientInvoiceAct.objects.exclude(invoice__status=InvoiceStatus.CANCELLED).values_list("act_id", flat=True)
        )
        qs = (
            BillingAct.objects.filter(
                application__in=app_qs,
                status=ActStatus.CONFIRMED,
            )
            .exclude(pk__in=invoiced_ids)
            .select_related("client", "legal_entity", "application")
            .order_by("client__agn_name", "-act_date", "-id")
        )
        client_id = self.request.GET.get("client")
        app_type = (self.request.GET.get("application_type") or "").strip()
        allowed_types = {
            BillingApplication.TYPE_RECEIVING,
            BillingApplication.TYPE_PROCESSING,
            BillingApplication.TYPE_SHIPPING,
            BillingApplication.TYPE_LOGISTICS,
            BillingApplication.TYPE_PACKING,
            BillingApplication.TYPE_STORAGE,
            BillingApplication.TYPE_OTHER,
        }
        if client_id:
            qs = qs.filter(client_id=client_id)
        if app_type in allowed_types:
            qs = qs.filter(application__application_type=app_type)

        ready_rows = applications_ready_to_invoice(self.request)
        shipping_rows = []
        if app_type in {"", BillingApplication.TYPE_SHIPPING}:
            shipping_app_ids = [
                row["application"].id
                for row in ready_rows
                if row["application"].application_type == BillingApplication.TYPE_SHIPPING
            ]
            active_invoice_app_ids = set(
                ClientInvoice.objects.exclude(status=InvoiceStatus.CANCELLED)
                .filter(application_id__in=shipping_app_ids)
                .values_list("application_id", flat=True)
            )
            active_invoice_app_ids.update(
                ClientInvoiceAct.objects.exclude(invoice__status=InvoiceStatus.CANCELLED)
                .filter(act__application_id__in=shipping_app_ids)
                .values_list("act__application_id", flat=True)
            )
            blocked_act_statuses = dict(
                BillingAct.objects.filter(application_id__in=shipping_app_ids)
                .exclude(status__in=[ActStatus.CANCELLED, ActStatus.DRAFT, ActStatus.CONFIRMED])
                .values_list("application_id", "status")
            )
            for source_row in ready_rows:
                application = source_row["application"]
                if application.application_type != BillingApplication.TYPE_SHIPPING:
                    continue
                if client_id and str(application.client_id) != str(client_id):
                    continue
                if application.id in active_invoice_app_ids:
                    continue
                row = dict(source_row)
                blockers = list(row["blockers"])
                blocked_status = blocked_act_statuses.get(application.id)
                if blocked_status:
                    blockers.append(f"акт: {ActStatus(blocked_status).label.lower()}")
                row["blockers"] = blockers
                row["ready"] = not blockers
                shipping_rows.append(row)

        ctx["candidate_acts"] = qs[:300]
        act_client_ids = set(
            Agency.objects.filter(
                billing_acts__application__in=app_qs,
                billing_acts__status=ActStatus.CONFIRMED,
            )
            .distinct()
            .values_list("id", flat=True)
        )
        act_client_ids.update(
            row["application"].client_id
            for row in ready_rows
            if row["application"].application_type == BillingApplication.TYPE_SHIPPING
        )
        ctx["act_clients"] = Agency.objects.filter(pk__in=act_client_ids).order_by("agn_name", "short_name")
        ctx["candidate_shipping_rows"] = shipping_rows[:300]
        ctx["show_shipping_applications"] = app_type in {"", BillingApplication.TYPE_SHIPPING}
        ctx["selected_client"] = client_id or ""
        ctx["selected_app_type"] = app_type
        ctx["app_type_tabs"] = [
            ("", "Все"),
            (BillingApplication.TYPE_RECEIVING, "Приёмка"),
            (BillingApplication.TYPE_PROCESSING, "Обработка"),
            (BillingApplication.TYPE_SHIPPING, "Отгрузка"),
            (BillingApplication.TYPE_LOGISTICS, "Логистика"),
            (BillingApplication.TYPE_OTHER, "Другие заявки"),
            (BillingApplication.TYPE_PACKING, "Упаковка"),
            (BillingApplication.TYPE_STORAGE, "Хранение"),
            (BillingApplication.TYPE_FBS, "FBS"),
        ]
        return ctx


class BillingInvoiceDetailView(BillingBaseView):
    template_name = "billing/invoice_detail.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        invoice = get_object_or_404(
            ClientInvoice.objects.select_related("client", "legal_entity", "act", "application").prefetch_related(
                "invoice_acts__act__application",
                "invoice_acts__act__lines__charge__service",
            ),
            pk=self.kwargs["pk"],
        )
        app_qs = filter_applications_for_user(BillingApplication.objects.all(), self.request).distinct()
        if not app_qs.filter(pk=invoice.application_id).exists():
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied("Нет доступа к счёту.")
        print_ctx = build_invoice_print_context(invoice)
        ctx["invoice"] = invoice
        ctx["sections"] = print_ctx["sections"]
        ctx["is_grouped"] = print_ctx["is_grouped"]
        ctx["total_fmt"] = print_ctx["total_fmt"]
        ctx["vat_fmt"] = print_ctx["vat_fmt"]
        ctx["vat_rate"] = print_ctx["vat_rate"]
        ctx["lines_count"] = print_ctx["lines_count"]
        from .storage_invoice_report import build_storage_invoice_report, storage_report_invoice_ids

        ctx["has_storage_report"] = invoice.id in storage_report_invoice_ids([invoice])
        if ctx["has_storage_report"] and self.request.GET.get("storage_report") == "1":
            ctx["storage_report"] = build_storage_invoice_report(
                invoice,
                selected_day_value=(self.request.GET.get("day") or "").strip(),
                search=(self.request.GET.get("q") or "").strip(),
                page_number=self.request.GET.get("page") or 1,
            )
        linked_acts = list(invoice.get_linked_acts())
        invoice_price_state_is_editable = bool(linked_acts) and (
            invoice.status == InvoiceStatus.DRAFT
            and not invoice.sent_at
            and invoice.paid_amount <= 0
            and invoice.review_status in {DocumentReviewStatus.LOCAL, DocumentReviewStatus.RETURNED}
            and all(
                act.status == ActStatus.DRAFT
                and not act.sent_at
                and act.review_status in {DocumentReviewStatus.LOCAL, DocumentReviewStatus.RETURNED}
                for act in linked_acts
            )
        )
        ctx["billing_can_edit_invoice_prices"] = (
            invoice_price_state_is_editable and can_edit_invoice_prices(self.request, invoice)
        )
        ctx["invoice_line_exclude_reasons"] = ApplicationCharge.EXCLUDE_REASON_CHOICES
        ctx["invoice_price_min_valid_from"] = (timezone.localdate() + timedelta(days=1)).isoformat()
        return ctx


class BillingInvoiceLinePriceUpdateView(RoleRequiredMixin, View):
    allowed_roles = ("manager",)

    def post(self, request, pk: int, charge_id: int):
        invoice = get_object_or_404(
            ClientInvoice.objects.select_related("application", "client"),
            pk=pk,
        )
        application_is_visible = filter_applications_for_user(
            BillingApplication.objects.filter(pk=invoice.application_id),
            request,
        ).exists()
        if not application_is_visible or not can_edit_invoice_prices(request, invoice):
            return JsonResponse({"ok": False, "error": "Нет права изменять строки счёта."}, status=403)
        charge = get_object_or_404(
            ApplicationCharge.objects.select_related("application", "service"),
            pk=charge_id,
        )
        action = (request.POST.get("action") or "price").strip()
        try:
            if action == "price":
                update_client_tariff = request.POST.get("update_client_tariff") == "1"
                tariff_valid_from = None
                if update_client_tariff:
                    tariff_valid_from = parse_date(request.POST.get("tariff_valid_from") or "")
                    if tariff_valid_from is None:
                        return JsonResponse(
                            {"ok": False, "error": "Укажите дату начала действия новой цены тарифа."},
                            status=400,
                        )
                charge, invoice, tariff_version = BillingWorkflowService.update_draft_invoice_charge_price(
                    invoice,
                    charge,
                    tariff=request.POST.get("tariff"),
                    reason=request.POST.get("reason", ""),
                    user=request.user,
                    update_client_tariff=update_client_tariff,
                    tariff_valid_from=tariff_valid_from,
                )
                response_data = {
                    "tariff": str(charge.tariff),
                    "tariff_version_id": getattr(tariff_version, "id", None),
                }
            elif action == "quantity":
                charge, invoice = BillingWorkflowService.update_draft_invoice_charge_quantity(
                    invoice,
                    charge,
                    quantity=request.POST.get("quantity"),
                    reason=request.POST.get("reason", ""),
                    user=request.user,
                    expected_version=request.POST.get("expected_version"),
                )
                response_data = {"quantity": str(charge.quantity)}
            elif action == "exclude":
                charge, invoice = BillingWorkflowService.exclude_draft_invoice_charge(
                    invoice,
                    charge,
                    reason=request.POST.get("exclude_reason", ""),
                    comment=request.POST.get("comment", ""),
                    user=request.user,
                    expected_version=request.POST.get("expected_version"),
                )
                response_data = {"excluded": True}
            else:
                raise ValidationError("Неизвестное действие со строкой счёта.")
        except (PermissionDenied, ValidationError, ValueError) as exc:
            messages = getattr(exc, "messages", None)
            error = "; ".join(str(item) for item in messages) if messages else str(exc)
            return JsonResponse({"ok": False, "error": error}, status=400)
        response_data.update(
            {
                "charge_id": charge.id,
                "invoice_total": str(invoice.total_amount),
            }
        )
        return JsonResponse(
            {
                "ok": True,
                "data": response_data,
            }
        )


class BillingPaymentsView(BillingBaseView):
    template_name = "billing/payments.html"
    billing_section = "payments"

    def get_context_data(self, **kwargs):
        from django.db.models import Sum

        from .payment_ops import list_portfolio_payments

        ctx = super().get_context_data(**kwargs)
        client_id = self.request.GET.get("client") or ""
        date_from = self.request.GET.get("date_from") or ""
        date_to = self.request.GET.get("date_to") or ""
        qs = list_portfolio_payments(self.request, client_id=client_id, date_from=date_from, date_to=date_to)
        agg = qs.aggregate(total=Sum("amount"))
        ctx.update(
            {
                "payments": qs[:300],
                "payments_count": qs.count(),
                "payments_total": agg["total"] or 0,
                "filter_client": client_id,
                "filter_date_from": date_from,
                "filter_date_to": date_to,
                "payment_clients": filter_agencies_for_user(Agency.objects.all(), self.request).order_by("agn_name")[
                    :500
                ],
            }
        )
        return ctx


class BillingOverdueView(BillingBaseView):
    template_name = "billing/debts.html"
    billing_section = "debts"

    def get_context_data(self, **kwargs):
        from .payment_ops import debt_summary

        ctx = super().get_context_data(**kwargs)
        client_id = self.request.GET.get("client") or ""
        summary = debt_summary(self.request, client_id=client_id)
        today = summary["today"]
        invoice_rows = []
        for inv in summary["invoices"]:
            days = (today - inv.due_date).days if inv.due_date and inv.due_date < today else 0
            invoice_rows.append({"invoice": inv, "days_overdue": days, "is_overdue": days > 0})
        ctx.update(
            {
                "debt_open_count": summary["open_count"],
                "debt_open_total": summary["open_debt"],
                "debt_overdue_count": summary["overdue_count"],
                "debt_overdue_total": summary["overdue_debt"],
                "debt_rows": invoice_rows,
                "filter_client": client_id,
                "debt_clients": filter_agencies_for_user(Agency.objects.all(), self.request).order_by("agn_name")[:500],
                "today": today,
            }
        )
        return ctx


class BillingDebtsView(BillingOverdueView):
    """Alias /debts/ → тот же реестр задолженности."""


class BillingPromisesView(BillingBaseView):
    template_name = "billing/promises.html"
    billing_section = "promises"

    def get_context_data(self, **kwargs):
        from .payment_ops import list_payment_promises
        from .statuses import PaymentPromiseStatus

        ctx = super().get_context_data(**kwargs)
        status = (self.request.GET.get("status") or "").strip()
        client_id = self.request.GET.get("client") or ""
        portfolio = filter_agencies_for_user(Agency.objects.all(), self.request)
        app_qs = filter_applications_for_user(BillingApplication.objects.all(), self.request).distinct()
        ctx.update(
            {
                "promises": list_payment_promises(self.request, status=status, client_id=client_id)[:200],
                "filter_status": status,
                "filter_client": client_id,
                "promise_clients": portfolio.order_by("agn_name")[:500],
                "promise_invoices": ClientInvoice.objects.filter(application__in=app_qs)
                .exclude(status=InvoiceStatus.CANCELLED)
                .exclude(debt_amount=0)
                .select_related("client")
                .order_by("-invoice_date")[:300],
                "promise_status_choices": PaymentPromiseStatus.choices,
                "today": timezone.localdate(),
            }
        )
        return ctx


class BillingReconciliationView(BillingBaseView):
    template_name = "billing/reconciliation.html"
    billing_section = "reconciliation"

    def get_context_data(self, **kwargs):
        from .payment_ops import list_reconciliation_requests

        ctx = super().get_context_data(**kwargs)
        status = (self.request.GET.get("status") or "").strip()
        client_id = self.request.GET.get("client") or ""
        ctx.update(
            {
                "recon_requests": list_reconciliation_requests(self.request, status=status, client_id=client_id)[:200],
                "filter_status": status,
                "filter_client": client_id,
                "recon_clients": filter_agencies_for_user(Agency.objects.all(), self.request).order_by("agn_name")[
                    :500
                ],
                "today": timezone.localdate(),
            }
        )
        return ctx


class BillingDisputesView(BillingBaseView):
    template_name = "billing/disputes.html"
    billing_section = "disputes"

    def get_context_data(self, **kwargs):
        from .models import BillingDiscrepancy
        from .payment_ops import list_discrepancies

        ctx = super().get_context_data(**kwargs)
        status = (self.request.GET.get("status") or "").strip()
        client_id = self.request.GET.get("client") or ""
        rows = list_discrepancies(self.request, status=status, client_id=client_id)
        ctx.update(
            {
                "discrepancies": rows[:200],
                "filter_status": status,
                "filter_client": client_id,
                "dispute_clients": filter_agencies_for_user(Agency.objects.all(), self.request).order_by("agn_name")[
                    :500
                ],
                "discrepancy_type_choices": BillingDiscrepancy.TYPE_CHOICES,
            }
        )
        return ctx
