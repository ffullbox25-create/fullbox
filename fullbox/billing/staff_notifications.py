"""Уведомления менеджера/бухгалтера по биллингу (этап 7)."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from employees.models import Employee
from sku.models import Agency

from .models import BillingStaffNotification
from .permissions import filter_agencies_for_user, get_billing_role


KIND_LABELS = {
    BillingStaffNotification.KIND_TARIFF_PUBLISHED: "Новый тариф",
    BillingStaffNotification.KIND_DOC_RETURNED: "Документ возвращён",
    BillingStaffNotification.KIND_DOC_ACCEPTED: "Документ принят",
    BillingStaffNotification.KIND_DOC_SUBMITTED: "Документ на проверке",
    BillingStaffNotification.KIND_PAYMENT: "Поступила оплата",
    BillingStaffNotification.KIND_OVERDUE: "Просроченный счёт",
    BillingStaffNotification.KIND_NO_PRICE: "Нет цены",
    BillingStaffNotification.KIND_DISCREPANCY: "Расхождение",
    BillingStaffNotification.KIND_UPD: "Запрос УПД",
    BillingStaffNotification.KIND_PROMISE: "Обещание оплаты",
    BillingStaffNotification.KIND_SERVICE_CHANGED: "Услуга изменена",
    BillingStaffNotification.KIND_OTHER: "Биллинг",
}


def manager_user_for_client(client: Agency):
    uid = getattr(client, "mened_user_id", None)
    if not uid:
        return None
    return get_user_model().objects.filter(pk=uid).first()


def accountant_users() -> list:
    return list(
        get_user_model().objects.filter(
            id__in=Employee.objects.filter(role="accountant", user__isnull=False).values_list("user_id", flat=True)
        )
    )


@transaction.atomic
def create_staff_notification(
    *,
    recipient,
    kind: str,
    title: str,
    message: str = "",
    link_url: str = "",
    client: Agency | None = None,
    audience: str = BillingStaffNotification.AUDIENCE_MANAGER,
    source_key: str,
    actor=None,
) -> BillingStaffNotification | None:
    if not recipient or not source_key:
        return None
    existing = BillingStaffNotification.objects.filter(source_key=source_key).first()
    if existing:
        return existing
    return BillingStaffNotification.objects.create(
        recipient=recipient,
        audience=audience,
        client=client,
        kind=kind or BillingStaffNotification.KIND_OTHER,
        title=(title or "Биллинг")[:255],
        message=message or "",
        link_url=link_url or "",
        source_key=source_key[:191],
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
    )


def notify_client_manager(
    client: Agency,
    *,
    kind: str,
    title: str,
    message: str = "",
    link_url: str = "",
    source_key: str,
    actor=None,
) -> BillingStaffNotification | None:
    user = manager_user_for_client(client)
    if not user:
        return None
    return create_staff_notification(
        recipient=user,
        audience=BillingStaffNotification.AUDIENCE_MANAGER,
        client=client,
        kind=kind,
        title=title,
        message=message,
        link_url=link_url,
        source_key=source_key,
        actor=actor,
    )


def notify_accountants(
    *,
    kind: str,
    title: str,
    message: str = "",
    link_url: str = "",
    client: Agency | None = None,
    source_key: str,
    actor=None,
) -> int:
    created = 0
    for user in accountant_users():
        key = f"{source_key}:acc:{user.id}"
        if BillingStaffNotification.objects.filter(source_key=key).exists():
            continue
        create_staff_notification(
            recipient=user,
            audience=BillingStaffNotification.AUDIENCE_ACCOUNTANT,
            client=client,
            kind=kind,
            title=title,
            message=message,
            link_url=link_url,
            source_key=key,
            actor=actor,
        )
        created += 1
    return created


def list_staff_notifications(user_or_request, *, unread_only: bool = False) -> QuerySet:
    user = getattr(user_or_request, "user", user_or_request)
    if not getattr(user, "is_authenticated", False):
        return BillingStaffNotification.objects.none()
    qs = (
        BillingStaffNotification.objects.filter(recipient=user)
        .select_related("client", "created_by")
        .order_by("-created_at", "-id")
    )
    if unread_only:
        qs = qs.filter(is_read=False)
    return qs


def unread_notifications_count(user_or_request) -> int:
    return list_staff_notifications(user_or_request, unread_only=True).count()


def mark_notification_read(row: BillingStaffNotification, *, user) -> BillingStaffNotification:
    if row.recipient_id != getattr(user, "id", None):
        raise PermissionError("Чужое уведомление.")
    if not row.is_read:
        row.is_read = True
        row.read_at = timezone.now()
        row.save(update_fields=["is_read", "read_at"])
    return row


def mark_all_read(user_or_request) -> int:
    user = getattr(user_or_request, "user", user_or_request)
    return (
        BillingStaffNotification.objects.filter(recipient=user, is_read=False).update(
            is_read=True, read_at=timezone.now()
        )
    )


def sync_portfolio_alerts(user_or_request, *, limit: int = 30) -> int:
    """Идемпотентно создать уведомления по просрочке / возвратам / расхождениям портфеля."""
    from .models import BillingAct, BillingDiscrepancy, ClientInvoice
    from .statuses import DiscrepancyStatus, DocumentReviewStatus, InvoiceStatus

    role = get_billing_role(user_or_request)
    user = getattr(user_or_request, "user", user_or_request)
    if not getattr(user, "is_authenticated", False):
        return 0
    portfolio = filter_agencies_for_user(Agency.objects.all(), user_or_request)
    today = timezone.localdate()
    created = 0

    overdue = (
        ClientInvoice.objects.filter(client__in=portfolio, due_date__lt=today, debt_amount__gt=0)
        .exclude(status=InvoiceStatus.CANCELLED)
        .select_related("client")
        .order_by("due_date")[:limit]
    )
    for inv in overdue:
        row = create_staff_notification(
            recipient=user,
            audience=BillingStaffNotification.AUDIENCE_MANAGER
            if role == "manager"
            else BillingStaffNotification.AUDIENCE_ACCOUNTANT,
            client=inv.client,
            kind=BillingStaffNotification.KIND_OVERDUE,
            title=f"Просрочен счёт {inv.number}",
            message=f"Долг {inv.debt_amount}, срок {inv.due_date}",
            link_url=f"/team-manager/billing/invoices/{inv.id}/",
            source_key=f"overdue-invoice:{inv.id}:{today.isoformat()}",
        )
        if row and not row.is_read and row.created_at.date() == today:
            created += 1

    returned_acts = (
        BillingAct.objects.filter(client__in=portfolio, review_status=DocumentReviewStatus.RETURNED)
        .select_related("client")
        .order_by("-reviewed_at")[:limit]
    )
    for act in returned_acts:
        row = create_staff_notification(
            recipient=user,
            kind=BillingStaffNotification.KIND_DOC_RETURNED,
            title=f"Акт {act.number} возвращён",
            message=act.accountant_comment or "Бухгалтер вернул документ",
            link_url="/team-manager/billing/document-drafts/?review=returned",
            client=act.client,
            source_key=f"act-returned:{act.id}:{act.reviewed_at.isoformat() if act.reviewed_at else 'x'}",
        )
        if row:
            created += 1

    returned_inv = (
        ClientInvoice.objects.filter(
            client__in=portfolio, review_status=DocumentReviewStatus.RETURNED, status=InvoiceStatus.DRAFT
        )
        .select_related("client")
        .order_by("-reviewed_at")[:limit]
    )
    for inv in returned_inv:
        create_staff_notification(
            recipient=user,
            kind=BillingStaffNotification.KIND_DOC_RETURNED,
            title=f"Счёт {inv.number} возвращён",
            message=inv.accountant_comment or "Бухгалтер вернул документ",
            link_url="/team-manager/billing/document-drafts/?review=returned",
            client=inv.client,
            source_key=f"invoice-returned:{inv.id}:{inv.reviewed_at.isoformat() if inv.reviewed_at else 'x'}",
        )

    open_disc = (
        BillingDiscrepancy.objects.filter(client__in=portfolio)
        .exclude(status=DiscrepancyStatus.CLOSED)
        .select_related("client")
        .order_by("-created_at")[:limit]
    )
    for d in open_disc:
        create_staff_notification(
            recipient=user,
            kind=BillingStaffNotification.KIND_DISCREPANCY,
            title="Открытое расхождение",
            message=(d.description or "")[:200],
            link_url="/team-manager/billing/disputes/",
            client=d.client,
            source_key=f"discrepancy-open:{d.id}",
        )
    return created
