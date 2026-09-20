"""Подтягивание операционных заявок в регистр биллинга (только чтение источников)."""

from __future__ import annotations

from typing import Any

from employees.models import Employee

from .models import BillingApplication
from .services import BillingWorkflowService

BILLING_SOURCE_TYPES = {
    BillingApplication.TYPE_RECEIVING,
    BillingApplication.TYPE_PROCESSING,
    BillingApplication.TYPE_PACKING,
    BillingApplication.TYPE_SHIPPING,
    BillingApplication.TYPE_LOGISTICS,
    BillingApplication.TYPE_OTHER,
}


def _normalize_application_type(order_type: str) -> str:
    value = str(order_type or "").strip().lower()
    if value == "packing":
        return BillingApplication.TYPE_PACKING
    if value in BILLING_SOURCE_TYPES:
        return value
    return BillingApplication.TYPE_OTHER


def _payload(entry) -> dict[str, Any]:
    data = getattr(entry, "payload", None)
    return data if isinstance(data, dict) else {}


def _status_from_payload(data: dict[str, Any]) -> tuple[str, str]:
    status = str(
        data.get("status")
        or data.get("submit_action")
        or data.get("shipping_state")
        or ""
    ).strip()
    label = str(data.get("status_label") or "").strip() or status
    return status, label


def _warehouse_label(data: dict[str, Any]) -> str:
    for key in ("warehouse_label", "warehouse_name", "store_name", "warehouse"):
        value = data.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("title") or value.get("label")
        text = str(value or "").strip()
        if text and text != "—":
            return text
    return ""


def _resolve_manager(client) -> Employee | None:
    user_id = getattr(client, "mened_user_id", None)
    if not user_id:
        return None
    return Employee.objects.filter(user_id=user_id).order_by("id").first()


def _latest_audit_entries(*, limit: int = 20000):
    from audit.models import OrderAuditEntry

    qs = (
        OrderAuditEntry.objects.filter(order_type__in=BILLING_SOURCE_TYPES)
        .exclude(order_id="")
        .select_related("agency")
        .order_by("-created_at", "-id")[:limit]
    )
    latest: dict[tuple[str, str, int | None], Any] = {}
    for entry in qs:
        order_id = str(entry.order_id or "").strip()
        if not order_id:
            continue
        app_type = _normalize_application_type(entry.order_type)
        key = (app_type, order_id, entry.agency_id)
        if key not in latest:
            latest[key] = entry
    return list(latest.values())


def _shipping_orders_missing_in_audit(synced_keys: set[tuple[str, str, int | None]]):
    from shipping.models import ShippingOrder

    for order in (
        ShippingOrder.objects.select_related("agency", "marketplace")
        .exclude(number="")
        .order_by("-id")[:5000]
    ):
        number = str(order.number or "").strip()
        if not number or not order.agency_id:
            continue
        key = (BillingApplication.TYPE_SHIPPING, number, order.agency_id)
        if key in synced_keys:
            continue
        yield order


def sync_billing_applications(*, limit: int = 20000, user=None) -> dict[str, int]:
    """Создаёт/обновляет BillingApplication по аудиту и ShippingOrder."""

    created = 0
    updated = 0
    skipped = 0
    synced_keys: set[tuple[str, str, int | None]] = set()

    for entry in _latest_audit_entries(limit=limit):
        client = entry.agency
        if client is None:
            skipped += 1
            continue
        app_type = _normalize_application_type(entry.order_type)
        application_id = str(entry.order_id or "").strip()
        data = _payload(entry)
        status, status_label = _status_from_payload(data)
        before = BillingApplication.objects.filter(
            application_type=app_type,
            application_id=application_id,
            client=client,
        ).first()
        BillingWorkflowService.sync_application_from_source(
            application_type=app_type,
            application_id=application_id,
            client=client,
            legal_entity=client,
            manager=_resolve_manager(client),
            warehouse_label=_warehouse_label(data),
            operational_status=status,
            operational_status_label=status_label,
            created_at_source=entry.created_at,
            source_payload={
                "source": "order_audit",
                "audit_id": entry.id,
                "action": entry.action,
                "description": entry.description or "",
                "payload": data,
            },
            user=user,
        )
        synced_keys.add((app_type, application_id, client.id))
        if before is None:
            created += 1
        else:
            updated += 1

    for order in _shipping_orders_missing_in_audit(synced_keys):
        client = order.agency
        application_id = str(order.number or "").strip()
        status = str(getattr(order, "status", "") or "").strip()
        try:
            status_label = order.get_status_display()
        except Exception:
            status_label = status
        before = BillingApplication.objects.filter(
            application_type=BillingApplication.TYPE_SHIPPING,
            application_id=application_id,
            client=client,
        ).first()
        BillingWorkflowService.sync_application_from_source(
            application_type=BillingApplication.TYPE_SHIPPING,
            application_id=application_id,
            client=client,
            legal_entity=client,
            manager=_resolve_manager(client),
            marketplace=getattr(order, "marketplace", None),
            operational_status=status,
            operational_status_label=status_label,
            created_at_source=getattr(order, "created_at", None),
            source_payload={
                "source": "shipping_order",
                "shipping_order_id": order.id,
                "status": status,
            },
            user=user,
        )
        if before is None:
            created += 1
        else:
            updated += 1

    from .external_trip import sync_completed_external_trips

    external_result = sync_completed_external_trips(limit=min(limit, 5000), user=user)
    created += external_result["created"]
    updated += external_result["updated"]
    skipped += external_result["skipped"]

    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "total": BillingApplication.objects.count(),
    }
