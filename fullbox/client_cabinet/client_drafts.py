"""Client LK draft helpers.

Each client company may keep one shared draft per request type.  The same
draft is reused whether the form is opened by a portal employee or by a
Fullbox employee acting on behalf of the client.
"""

from __future__ import annotations

from typing import Any

from audit.models import OrderAuditEntry, log_order_action
from django.utils import timezone

CLIENT_DRAFT_ORDER_TYPES = ("receiving", "processing", "shipping", "other")
CLIENT_DRAFT_LIMIT = 1


def _payload_dict(payload: Any) -> dict:
    return dict(payload) if isinstance(payload, dict) else {}


def is_draft_status(*, status: str = "", status_label: str = "", submit_action: str = "") -> bool:
    status_l = str(status or submit_action or "").strip().lower()
    label_l = str(status_label or "").strip().lower()
    if status_l in {"draft", "client_draft"}:
        return True
    return "черновик" in label_l


def is_draft_payload(payload: Any) -> bool:
    data = _payload_dict(payload)
    return is_draft_status(
        status=str(data.get("status") or ""),
        status_label=str(data.get("status_label") or ""),
        submit_action=str(data.get("submit_action") or ""),
    )


def _latest_entries_by_order(*, agency, order_type: str) -> dict[str, OrderAuditEntry]:
    qs = (
        OrderAuditEntry.objects.filter(agency=agency, order_type=order_type)
        .order_by("-created_at", "-id")[:800]
    )
    latest: dict[str, OrderAuditEntry] = {}
    for entry in qs:
        order_id = str(entry.order_id or "").strip()
        if not order_id or order_id in latest:
            continue
        latest[order_id] = entry
    return latest


def list_client_draft_order_ids(*, agency, order_type: str, user=None) -> list[str]:
    """Return all active drafts for company+direction.

    ``user`` is retained for call compatibility and audit attribution by
    callers.  Draft ownership is deliberately company-wide: the journal and
    the single editable draft are shared by the client's employees.
    """
    order_type = str(order_type or "").strip().lower()
    if order_type == "packing":
        order_type = "processing"
    if order_type == "shipping":
        from shipping.models import ShippingOrder
        from shipping.workflow import is_order_returned_for_rework

        draft_orders = list(
            ShippingOrder.objects.filter(agency=agency, status=ShippingOrder.STATUS_DRAFT)
            .order_by("-updated_at", "-id")
            .only("id", "number", "status")
        )
        draft_ids = [
            order.number
            for order in draft_orders
            if not is_order_returned_for_rework(order)
        ]
    else:
        draft_ids = []
        for order_id, entry in _latest_entries_by_order(agency=agency, order_type=order_type).items():
            if is_draft_payload(entry.payload):
                draft_ids.append(order_id)
    return draft_ids


def client_draft_count(*, agency, order_type: str, user=None) -> int:
    return len(list_client_draft_order_ids(agency=agency, order_type=order_type, user=user))


def client_draft_limit_reached(*, agency, order_type: str, user=None) -> bool:
    return client_draft_count(agency=agency, order_type=order_type, user=user) >= CLIENT_DRAFT_LIMIT


def find_client_draft(*, agency, order_type: str, user=None) -> dict[str, Any] | None:
    """Return the company's newest open draft for agency+type."""
    order_type = str(order_type or "").strip().lower()
    if order_type == "packing":
        order_type = "processing"
    agency_id = getattr(agency, "id", None)
    if not agency_id:
        return None

    if order_type == "shipping":
        from shipping.models import ShippingOrder

        draft_ids = list_client_draft_order_ids(
            agency=agency,
            order_type=order_type,
            user=user,
        )
        order = ShippingOrder.objects.filter(agency=agency, number__in=draft_ids).order_by(
            "-updated_at", "-id"
        ).first()
        if not order:
            return None
        return {
            "order_type": "shipping",
            "order_id": str(order.number),
            "shipping_pk": order.pk,
            "continue_url": f"/shipping/new/?client={agency_id}&order={order.pk}&edit=1",
        }

    draft_ids = list_client_draft_order_ids(agency=agency, order_type=order_type, user=user)
    if not draft_ids:
        return None
    # Prefer newest by latest audit timestamp among draft ids.
    latest_by_id = _latest_entries_by_order(agency=agency, order_type=order_type)
    draft_ids.sort(
        key=lambda oid: getattr(latest_by_id.get(oid), "created_at", None) or timezone.now(),
        reverse=True,
    )
    order_id = draft_ids[0]
    if order_type == "receiving":
        continue_url = f"/orders/receiving/?client={agency_id}&edit={order_id}"
    elif order_type == "processing":
        continue_url = f"/orders/processing/?client={agency_id}&order={order_id}&status=draft"
    elif order_type == "other":
        continue_url = f"/client/dashboard/lk/?client={agency_id}#/other-requests"
    else:
        continue_url = f"/orders/{order_type}/{order_id}/?client={agency_id}"
    return {
        "order_type": order_type,
        "order_id": order_id,
        "shipping_pk": None,
        "continue_url": continue_url,
    }


def continue_url_for_entry(*, agency_id: int | None, order_type: str, order_id: str) -> str:
    order_type = str(order_type or "").strip().lower()
    order_id = str(order_id or "").strip()
    if not agency_id or not order_id:
        return "#"
    if order_type == "receiving":
        return f"/orders/receiving/?client={agency_id}&edit={order_id}"
    if order_type in {"processing", "packing"}:
        return f"/orders/processing/?client={agency_id}&order={order_id}&edit=1"
    if order_type == "shipping":
        from shipping.models import ShippingOrder

        shipping = ShippingOrder.objects.filter(agency_id=agency_id, number=order_id).only("id").first()
        if shipping:
            return f"/shipping/new/?client={agency_id}&order={shipping.id}&edit=1"
        return f"/shipping/new/?client={agency_id}"
    if order_type == "other":
        return f"/client/dashboard/lk/?client={agency_id}#/other-requests"
    return f"/orders/{order_type}/{order_id}/?client={agency_id}"


def _close_client_draft(
    *,
    agency,
    order_type: str,
    order_id: str,
    user=None,
    description: str,
    replaced_by: str = "",
) -> bool:
    order_type = str(order_type or "").strip().lower()
    order_id = str(order_id or "").strip()
    if not order_id:
        return False
    if order_type == "shipping":
        from shipping.models import ShippingOrder

        order = ShippingOrder.objects.filter(
            agency=agency,
            number=order_id,
            status=ShippingOrder.STATUS_DRAFT,
        ).first()
        if not order:
            return False
        order.status = ShippingOrder.STATUS_CANCELED
        order.save(update_fields=["status", "updated_at"])
        payload = {
            "status": ShippingOrder.STATUS_CANCELED,
            "status_label": "Отменена",
            "submit_action": "cancel",
        }
        if replaced_by:
            payload["replaced_by_draft"] = replaced_by
            payload["replaced_by_order"] = replaced_by
        log_order_action(
            action="status",
            order_id=order.number,
            order_type="shipping",
            user=user if getattr(user, "is_authenticated", False) else None,
            agency=agency,
            description=description,
            payload=payload,
        )
        return True

    latest = (
        OrderAuditEntry.objects.filter(agency=agency, order_type=order_type, order_id=order_id)
        .order_by("-created_at", "-id")
        .first()
    )
    if not latest or not is_draft_payload(latest.payload):
        return False
    payload = _payload_dict(latest.payload)
    payload["status"] = "cancelled"
    payload["status_label"] = "Отменена"
    payload["submit_action"] = "cancel"
    if replaced_by:
        payload["replaced_by_draft"] = replaced_by
        payload["replaced_by_order"] = replaced_by
    log_order_action(
        action="status",
        order_id=order_id,
        order_type=order_type,
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=agency,
        description=description,
        payload=payload,
    )
    return True


def supersede_extra_client_drafts(
    *,
    agency,
    order_type: str,
    keep_order_id: str,
    user=None,
) -> int:
    """Keep one company draft for the direction and close overflow drafts."""
    order_type = str(order_type or "").strip().lower()
    if order_type == "packing":
        order_type = "processing"
    keep_order_id = str(keep_order_id or "").strip()
    draft_ids = list_client_draft_order_ids(
        agency=agency,
        order_type=order_type,
        user=user,
    )
    allowed_ids: set[str] = set()
    if keep_order_id:
        allowed_ids.add(keep_order_id)
    for order_id in draft_ids:
        if len(allowed_ids) >= CLIENT_DRAFT_LIMIT:
            break
        allowed_ids.add(order_id)
    closed = 0
    for order_id in draft_ids:
        if order_id in allowed_ids:
            continue
        if _close_client_draft(
            agency=agency,
            order_type=order_type,
            order_id=order_id,
            user=user,
            description="Черновик заменён новым" if order_type != "shipping" else "Черновик отгрузки заменён новым",
            replaced_by=keep_order_id,
        ):
            closed += 1
    return closed


def close_client_drafts_after_submit(
    *,
    agency,
    order_type: str,
    submitted_order_id: str = "",
    user=None,
) -> int:
    """После реальной отправки заявки закрыть все оставшиеся черновики этого типа."""
    order_type = str(order_type or "").strip().lower()
    if order_type == "packing":
        order_type = "processing"
    submitted_order_id = str(submitted_order_id or "").strip()
    closed = 0
    for order_id in list_client_draft_order_ids(
        agency=agency,
        order_type=order_type,
        user=user,
    ):
        if order_id == submitted_order_id:
            continue
        if _close_client_draft(
            agency=agency,
            order_type=order_type,
            order_id=order_id,
            user=user,
            description="Черновик закрыт после отправки заявки",
            replaced_by=submitted_order_id,
        ):
            closed += 1
    return closed


def resolve_client_draft_order_id(
    *,
    agency,
    order_type: str,
    preferred_order_id: str = "",
    allow_new: bool = False,
    user=None,
) -> str:
    """Prefer an explicit id, otherwise reuse the company's active draft.

    ``allow_new=True`` permits a new id only while the company has no active
    draft for this direction.
    """
    preferred = str(preferred_order_id or "").strip()
    if preferred:
        return preferred
    if allow_new and not client_draft_limit_reached(
        agency=agency,
        order_type=order_type,
        user=user,
    ):
        return ""
    draft = find_client_draft(agency=agency, order_type=order_type, user=user)
    if draft:
        return str(draft.get("order_id") or "")
    return ""
