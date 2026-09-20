from __future__ import annotations

from datetime import datetime

from django.utils import timezone

from audit.models import OrderAuditEntry


ORDER_AUDIT_TYPE = "fbs_order"


def _status_label(value: str) -> str:
    from .models import FbsOrder

    return dict(FbsOrder.STATUS_CHOICES).get(str(value or ""), str(value or "") or "-")


def log_order_snapshot(
    order,
    *,
    changed_fields=(),
    previous: dict | None = None,
    user=None,
    source: str = "model_save",
    occurred_at: datetime | None = None,
    include_unchanged: bool = False,
) -> OrderAuditEntry | None:
    previous = dict(previous or {})
    fields = {
        field
        for field in changed_fields
        if field in {"internal_status", "marketplace_status", "marketplace_substatus", "cutoff_at"}
    }
    if not fields:
        return None

    changes = {}
    for field in sorted(fields):
        before = previous.get(field)
        after = getattr(order, field, None)
        if before == after and not include_unchanged:
            continue
        changes[field] = {
            "from": before.isoformat() if hasattr(before, "isoformat") else before,
            "to": after.isoformat() if hasattr(after, "isoformat") else after,
        }
    if not changes:
        return None

    internal_change = changes.get("internal_status")
    if internal_change:
        description = (
            f"Внутренний статус: {_status_label(internal_change['from'])} -> "
            f"{_status_label(internal_change['to'])}"
        )
    elif "marketplace_status" in changes or "marketplace_substatus" in changes:
        description = "Маркетплейс обновил статус заказа"
    else:
        description = "Изменен срок отгрузки заказа"

    payload = {
        "source": source,
        "order_pk": order.pk,
        "external_order_id": order.external_order_id,
        "marketplace": order.profile.marketplace,
        "changes": changes,
        "status": order.internal_status,
        "status_label": order.get_internal_status_display(),
        "marketplace_status": order.marketplace_status,
        "marketplace_substatus": order.marketplace_substatus,
        "cutoff_at": order.cutoff_at.isoformat() if order.cutoff_at else None,
        "skip_chat_bridge": True,
    }
    return OrderAuditEntry.objects.create(
        order_id=str(order.pk),
        order_type=ORDER_AUDIT_TYPE,
        action="status" if internal_change or "marketplace_status" in changes else "update",
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=order.profile.agency,
        description=description,
        payload=payload,
        created_at=occurred_at or timezone.now(),
    )


def log_order_bulk_transition(
    orders,
    *,
    previous_internal_status: dict[int, str],
    internal_status: str,
    user=None,
    source: str,
    occurred_at: datetime | None = None,
) -> None:
    for order in orders:
        order.internal_status = internal_status
        log_order_snapshot(
            order,
            changed_fields=("internal_status",),
            previous={"internal_status": previous_internal_status.get(order.pk)},
            user=user,
            source=source,
            occurred_at=occurred_at,
            include_unchanged=True,
        )


def order_audit_timeline(order) -> list[dict]:
    rows = []
    entries = OrderAuditEntry.objects.filter(
        order_type=ORDER_AUDIT_TYPE,
        order_id=str(order.pk),
    ).order_by("-created_at", "-id")
    for entry in entries:
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        changes = payload.get("changes") if isinstance(payload.get("changes"), dict) else {}
        detail_parts = []
        status_change = changes.get("internal_status") or {}
        if status_change:
            detail_parts.append(
                f"Fullbox: {_status_label(status_change.get('from'))} -> "
                f"{_status_label(status_change.get('to'))}"
            )
        marketplace_change = changes.get("marketplace_status") or {}
        if marketplace_change:
            detail_parts.append(
                f"Площадка: {marketplace_change.get('from') or '-'} -> "
                f"{marketplace_change.get('to') or '-'}"
            )
        substatus_change = changes.get("marketplace_substatus") or {}
        if substatus_change:
            detail_parts.append(
                f"Подстатус: {substatus_change.get('from') or '-'} -> "
                f"{substatus_change.get('to') or '-'}"
            )
        cutoff_change = changes.get("cutoff_at") or {}
        if cutoff_change:
            detail_parts.append(
                f"Срок: {cutoff_change.get('from') or '-'} -> {cutoff_change.get('to') or '-'}"
            )
        rows.append(
            {
                "at": entry.created_at,
                "title": entry.description or "Изменение заказа",
                "detail": "; ".join(detail_parts) or payload.get("source", ""),
                "status": payload.get("status_label") or "",
            }
        )
    return rows
