from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Q
from django.http import JsonResponse

from employees.access import get_request_employee, get_request_role, role_required
from fbs.models import FbsClientMovementRequest
from shipping.models import ShippingOrder

from .models import Task


SUPPORTED_ROLES = (
    "manager",
    "logistician",
    "storekeeper",
    "processing_head",
    "processing_worker",
)
_TASK_QUEUE_ROLE = {
    "manager": "manager",
    "logistician": "logistician",
    "storekeeper": "storekeeper",
    "processing_head": "processing_head",
}
_RECEIVING_ROUTE_RE = re.compile(r"/orders/receiving/([^/?#]+)/")
_PROCESSING_ROUTE_RE = re.compile(r"/orders/processing/([^/?#]+)/")
_SHIPPING_ROUTE_RE = re.compile(r"/shipping/(\d+)/")
_TASK_SCAN_LIMIT = 600
_ALERT_LIMIT = 250


@dataclass(frozen=True)
class RequestAlert:
    key: str
    kind: str
    created_at: datetime
    url: str

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "kind": self.kind,
            "created_at": self.created_at.isoformat(),
            "url": self.url,
        }


def _task_identity(route: str) -> tuple[str, str] | None:
    route_value = str(route or "")
    receiving_match = _RECEIVING_ROUTE_RE.search(route_value)
    if receiving_match:
        return "receiving", receiving_match.group(1)
    processing_match = _PROCESSING_ROUTE_RE.search(route_value)
    if processing_match:
        return "processing", processing_match.group(1)
    shipping_match = _SHIPPING_ROUTE_RE.search(route_value)
    if shipping_match:
        return "shipping", shipping_match.group(1)
    return None


def _task_rows_for_role(role: str, employee):
    queue = Task.objects.exclude(status="done").filter(
        Q(route__contains="/orders/receiving/")
        | Q(route__contains="/orders/processing/")
        | Q(route__contains="/shipping/")
    )
    if role == "processing_worker":
        queue = queue.filter(
            Q(assigned_to=employee)
            | Q(observer=employee)
            | Q(participants=employee)
        )
    else:
        queue_role = _TASK_QUEUE_ROLE[role]
        queue = queue.filter(
            Q(assigned_to__role=queue_role)
            | Q(observer__role=queue_role)
            | Q(participants__role=queue_role)
        )
    if role in {"processing_head", "processing_worker"}:
        queue = queue.filter(route__contains="/orders/processing/")
    elif role == "logistician":
        queue = queue.filter(route__contains="/shipping/")
    elif role == "storekeeper":
        queue = queue.exclude(route__contains="/orders/processing/")
    return list(
        queue.order_by("-created_at", "-id")
        .values("id", "route", "created_at")
        .distinct()[:_TASK_SCAN_LIMIT]
    )


def _task_alerts(role: str, employee) -> list[RequestAlert]:
    task_rows = _task_rows_for_role(role, employee)
    shipping_ids = {
        int(identity[1])
        for row in task_rows
        if (identity := _task_identity(row["route"])) and identity[0] == "shipping"
    }
    terminal_shipping_statuses = {
        ShippingOrder.STATUS_DRAFT,
        ShippingOrder.STATUS_SHIPPED,
        ShippingOrder.STATUS_PARTIAL,
        ShippingOrder.STATUS_CANCELED,
    }
    shipping_orders = {
        order.id: order
        for order in ShippingOrder.objects.filter(id__in=shipping_ids)
        .exclude(status__in=terminal_shipping_statuses)
        .only("id", "delivery_type")
    }

    alerts_by_key: dict[str, RequestAlert] = {}
    for row in task_rows:
        identity = _task_identity(row["route"])
        if not identity:
            continue
        document_type, document_id = identity
        kind = document_type
        key = f"{document_type}:{document_id}"
        if document_type == "shipping":
            shipping_order = shipping_orders.get(int(document_id))
            if shipping_order is None:
                continue
            key = f"shipping-order:{document_id}"
            kind = (
                "movement"
                if shipping_order.delivery_type == ShippingOrder.DELIVERY_TRANSFER
                else "shipping"
            )
        alert = RequestAlert(
            key=key,
            kind=kind,
            created_at=row["created_at"],
            url=str(row["route"] or ""),
        )
        previous = alerts_by_key.get(key)
        if previous is None or alert.created_at > previous.created_at:
            alerts_by_key[key] = alert
    return list(alerts_by_key.values())


def _fbs_movement_alerts(role: str) -> list[RequestAlert]:
    if role == "manager":
        statuses = (FbsClientMovementRequest.STATUS_SUBMITTED,)
        url_prefix = "/team-manager/fbs/movements/"
    elif role == "storekeeper":
        statuses = (FbsClientMovementRequest.STATUS_APPROVED,)
        url_prefix = "/fbs/operator/movements/"
    else:
        return []
    rows = (
        FbsClientMovementRequest.objects.filter(status__in=statuses)
        .only("id", "created_at", "reviewed_at", "updated_at")
        .order_by("-updated_at", "-id")[:_ALERT_LIMIT]
    )
    return [
        RequestAlert(
            key=f"fbs-movement:{row.id}",
            kind="movement",
            created_at=(
                row.reviewed_at
                if role == "storekeeper" and row.reviewed_at
                else row.created_at
            ),
            url=f"{url_prefix}{row.id}/",
        )
        for row in rows
    ]


def _request_alerts_for_role(role: str, employee) -> list[RequestAlert]:
    alerts = _task_alerts(role, employee)
    alerts.extend(_fbs_movement_alerts(role))
    alerts.sort(key=lambda alert: (alert.created_at, alert.key))
    return alerts[-_ALERT_LIMIT:]


@role_required(*SUPPORTED_ROLES)
def request_voice_alerts(request):
    """Return the current read-only request queue for the active department."""

    employee = get_request_employee(request)
    role = get_request_role(request)
    if employee is None or role not in SUPPORTED_ROLES:
        return JsonResponse(
            {"ok": False, "error": "Голосовые уведомления недоступны для этой роли"},
            status=403,
        )
    alerts = _request_alerts_for_role(role, employee)
    response = JsonResponse(
        {
            "ok": True,
            "data": {
                "scope": f"{role}:{request.user.pk}:{employee.pk}",
                "role": role,
                "alerts": [alert.as_dict() for alert in alerts],
            },
        }
    )
    response["Cache-Control"] = "no-store, max-age=0"
    return response
