from __future__ import annotations

from django.db.models import Q, QuerySet

from sku.models import Agency

from .models import ClientLifecycle


ACCOUNTANT_ROLES = {"accountant", "admin", "director", "head_manager"}


def manager_visible_agencies(base: QuerySet | None = None) -> QuerySet:
    """Клиенты, которых менеджер видит в справочнике клиентов.

    Клиент появляется у менеджеров только после активации бухгалтером.
    Черновики, клиенты на проверке и карточки без lifecycle скрыты.
    """
    qs = base if base is not None else Agency.objects.all()
    return qs.filter(archived=False, lifecycle__status=ClientLifecycle.STATUS_ACTIVE)


def accountant_clients_queryset(*, status: str | None = None, q: str = "") -> QuerySet:
    qs = Agency.objects.select_related("lifecycle", "lifecycle__serving_company", "portal_user").all()
    if status:
        qs = qs.filter(lifecycle__status=status)
    if q:
        qs = qs.filter(
            Q(agn_name__icontains=q)
            | Q(short_name__icontains=q)
            | Q(inn__icontains=q)
            | Q(email__icontains=q)
            | Q(phone__icontains=q)
        )
    return qs.order_by("agn_name", "id")


def ensure_lifecycle(agency: Agency, *, status: str = ClientLifecycle.STATUS_DRAFT, user=None) -> ClientLifecycle:
    lifecycle, created = ClientLifecycle.objects.get_or_create(
        agency=agency,
        defaults={
            "status": status,
            "created_by": user if getattr(user, "is_authenticated", False) else None,
            "email_documents": getattr(agency, "email", "") or "",
            "email_notifications": getattr(agency, "email", "") or "",
        },
    )
    return lifecycle


def is_client_billing_ready(agency: Agency) -> bool:
    lifecycle = getattr(agency, "lifecycle", None)
    if lifecycle is None:
        try:
            lifecycle = agency.lifecycle
        except ClientLifecycle.DoesNotExist:
            return False
    return lifecycle.status == ClientLifecycle.STATUS_ACTIVE and not agency.archived
