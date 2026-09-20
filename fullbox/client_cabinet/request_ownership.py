"""Ownership rules for requests created by client-portal employees.

The company request journal remains shared.  This module only controls write
access: a regular portal employee may change requests created by that user;
the portal owner and portal administrators may change every company request.
"""

from __future__ import annotations

from collections.abc import Iterable

from audit.models import OrderAuditEntry


PORTAL_REQUEST_OWNER_MESSAGE = (
    "Можно изменять только свои заявки. "
    "Администратор компании может изменять все заявки."
)


def normalize_request_type(order_type: str) -> str:
    value = str(order_type or "").strip().lower()
    return "processing" if value == "packing" else value


def _order_ids(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        order_id = str(value or "").strip()
        if not order_id or order_id in seen:
            continue
        seen.add(order_id)
        result.append(order_id)
    return result


def _portal_user_ids(agency) -> set[int]:
    from .models import AgencyPortalMember

    ids = {
        int(user_id)
        for user_id in AgencyPortalMember.objects.filter(agency=agency)
        .exclude(user_id__isnull=True)
        .values_list("user_id", flat=True)
    }
    portal_user_id = getattr(agency, "portal_user_id", None)
    if portal_user_id:
        ids.add(int(portal_user_id))
    return ids


def portal_request_owner_map(*, agency, order_type: str, order_ids: Iterable[object]) -> dict[str, int]:
    """Resolve request authors in one batch without changing request visibility."""

    normalized = normalize_request_type(order_type)
    requested = _order_ids(order_ids)
    if not agency or not requested:
        return {}

    owners: dict[str, int] = {}
    if normalized == "shipping":
        from shipping.models import ShippingOrder

        rows = (
            ShippingOrder.objects.filter(agency=agency, number__in=requested)
            .values("number", "created_by_id")
        )
        for row in rows:
            user_id = row.get("created_by_id")
            if user_id:
                owners[str(row["number"])] = int(user_id)
    elif normalized == "other":
        from .models import OtherRequest

        for number, user_id in OtherRequest.objects.filter(
            agency=agency,
            public_number__in=requested,
        ).values_list("public_number", "created_by_id"):
            if user_id:
                owners[str(number)] = int(user_id)

    missing = [order_id for order_id in requested if order_id not in owners]
    if not missing:
        return owners

    audit_types = ("processing", "packing") if normalized == "processing" else (normalized,)
    portal_user_ids = _portal_user_ids(agency)
    if portal_user_ids:
        entries = (
            OrderAuditEntry.objects.filter(
                agency=agency,
                order_type__in=audit_types,
                order_id__in=missing,
                user_id__in=portal_user_ids,
            )
            .values_list("order_id", "user_id")
            .order_by("order_id", "created_at", "id")
        )
        for order_id, user_id in entries:
            key = str(order_id or "").strip()
            if key and key not in owners and user_id:
                owners[key] = int(user_id)

    # Старые учетные записи сотрудников клиента могли получить доступ к ЛК
    # до появления AgencyPortalMember. Разрешаем такому пользователю продолжить
    # только тот черновик обработки, который он сам создал. Числовые заявки,
    # заявки других типов и действия сотрудников Fullbox сюда не попадают.
    draft_missing = [
        order_id
        for order_id in missing
        if order_id not in owners and normalized == "processing" and order_id.startswith("draft-")
    ]
    if draft_missing:
        draft_entries = (
            OrderAuditEntry.objects.filter(
                agency=agency,
                order_type__in=audit_types,
                order_id__in=draft_missing,
                action="create",
                user_id__isnull=False,
                user__is_staff=False,
                user__is_superuser=False,
            )
            .values("order_id", "user_id", "payload")
            .order_by("order_id", "created_at", "id")
        )
        for entry in draft_entries:
            key = str(entry.get("order_id") or "").strip()
            payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
            state = str(payload.get("status") or payload.get("submit_action") or "").strip().lower()
            status_label = str(payload.get("status_label") or "").strip().lower()
            if state not in {"draft", "client_draft"} and "черновик" not in status_label:
                continue
            user_id = entry.get("user_id")
            if key and key not in owners and user_id:
                owners[key] = int(user_id)
    return owners


def portal_request_owner_user_id(*, agency, order_type: str, order_id: object) -> int | None:
    key = str(order_id or "").strip()
    if not key:
        return None
    return portal_request_owner_map(
        agency=agency,
        order_type=order_type,
        order_ids=[key],
    ).get(key)


def _is_fullbox_staff_request(request) -> bool:
    if request is None:
        return False
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return True
    from employees.access import get_request_role, is_staff_role

    return is_staff_role(get_request_role(request))


def _is_active_company_draft(*, agency, order_type: str, order_id: object) -> bool:
    """Drafts are shared inside the company; submitted requests keep ownership."""

    normalized = normalize_request_type(order_type)
    key = str(order_id or "").strip()
    if not key:
        return False
    if normalized == "shipping":
        from shipping.models import ShippingOrder
        from shipping.workflow import is_order_returned_for_rework

        order = ShippingOrder.objects.filter(
            agency=agency,
            number=key,
            status=ShippingOrder.STATUS_DRAFT,
        ).first()
        return bool(order and not is_order_returned_for_rework(order))
    if normalized == "other":
        from .models import OtherRequest

        return OtherRequest.objects.filter(
            agency=agency,
            public_number=key,
            status=OtherRequest.STATUS_DRAFT,
        ).exists()

    audit_types = ("processing", "packing") if normalized == "processing" else (normalized,)
    latest = (
        OrderAuditEntry.objects.filter(
            agency=agency,
            order_type__in=audit_types,
            order_id=key,
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if not latest:
        return False
    from .client_drafts import is_draft_payload

    return is_draft_payload(latest.payload)


def portal_user_can_edit_request(
    *,
    user,
    agency,
    order_type: str,
    order_id: object,
    request=None,
) -> bool:
    """Return write permission; ownerless legacy requests are admin-only."""

    if not user or not agency or not getattr(user, "is_authenticated", False):
        return False
    if _is_fullbox_staff_request(request):
        return True

    from .portal_members import portal_user_is_admin

    if portal_user_is_admin(user, agency):
        return True
    if int(user.id) in _portal_user_ids(agency) and _is_active_company_draft(
        agency=agency,
        order_type=order_type,
        order_id=order_id,
    ):
        return True
    owner_id = portal_request_owner_user_id(
        agency=agency,
        order_type=order_type,
        order_id=order_id,
    )
    return bool(owner_id and int(owner_id) == int(user.id))
