"""Closed, data-driven states for the manager queue."""

from __future__ import annotations

import re
from datetime import timedelta

from django.utils import timezone


QUEUE_STATES = (
    ("to_accept", "Принять"),
    ("to_confirm", "Подтвердить"),
    ("waiting_client", "Ждём клиента"),
    ("waiting_warehouse", "Ждём склад"),
    ("waiting_logistics", "Ждём логистику"),
    ("overdue", "Просрочено"),
    ("stuck", "Зависло"),
    ("done", "Готово"),
)

QUEUE_STATE_LABELS = dict(QUEUE_STATES)

# Порог согласован как общий стартовый ориентир; его нужно уточнять отдельно
# для каждого процесса после накопления статистики очереди.
STUCK_AFTER_HOURS = 48

_TRIP_ROUTE_RE = re.compile(r"^/logistics/trips/\d+/?(?:[?#].*)?$")
_NOT_STARTED_STATUSES = {"", "backlog", "todo", "open"}
_LOGISTICS_WAITING_STATUSES = {"ready_for_routing", "routed", "in_transit"}


def is_trip_confirmation(task) -> bool:
    """Return True only for the system-created trip confirmation task."""

    route = str(getattr(task, "route", "") or "").strip()
    title = str(getattr(task, "title", "") or "")
    return bool(_TRIP_ROUTE_RE.match(route)) and title.startswith("Подтвердить доставку:")


def is_waiting_manager_act(task, payload: dict | None) -> bool:
    """Return True when an existing receiving/processing act awaits a manager."""

    route = str(getattr(task, "route", "") or "")
    is_act_route = "/orders/receiving/" in route or "/orders/processing/" in route
    if not is_act_route:
        return False
    data = payload if isinstance(payload, dict) else {}
    client_response = str(data.get("act_client_response") or "").strip().lower()
    return bool(data.get("act_storekeeper_signed") or data.get("act_logistician_signed")) and not bool(
        data.get("act_manager_signed")
    ) and not bool(data.get("act_sent")) and client_response not in {"confirmed", "dispute"}


def queue_state(
    task,
    *,
    request_status=None,
    payload: dict | None = None,
    routing_status: str = "",
    now=None,
) -> str:
    """Classify one task into exactly one manager-queue state.

    ``to_confirm`` and ``to_accept`` deliberately precede SLA states. This is
    the agreed operational priority: an actionable manager step must stay
    visible even when the underlying task deadline is already overdue.
    """

    current = now or timezone.localtime()
    if is_trip_confirmation(task) or is_waiting_manager_act(task, payload):
        return "to_confirm"

    bucket = str(getattr(request_status, "bucket", "") or "").strip().lower()
    task_status = str(getattr(task, "status", "") or "").strip().lower()
    if bucket == "manager" and task_status in _NOT_STARTED_STATUSES:
        return "to_accept"

    due_date = getattr(task, "due_date", None)
    if due_date is not None:
        due_value = due_date
        if timezone.is_naive(due_value):
            due_value = timezone.make_aware(due_value, timezone.get_current_timezone())
        if task_status != "done" and due_value < current:
            return "overdue"

    updated_at = getattr(task, "updated_at", None)
    if updated_at is not None:
        updated_value = updated_at
        if timezone.is_naive(updated_value):
            updated_value = timezone.make_aware(updated_value, timezone.get_current_timezone())
        if task_status != "done" and updated_value < current - timedelta(hours=STUCK_AFTER_HOURS):
            return "stuck"

    if bucket == "client":
        return "waiting_client"
    if bucket == "warehouse":
        return "waiting_warehouse"
    if str(routing_status or "").strip().lower() in _LOGISTICS_WAITING_STATUSES:
        return "waiting_logistics"
    if bucket == "done" or task_status == "done":
        return "done"

    # An active manager-zone task without a linked request status is still an
    # actionable queue item. This fallback keeps the state set exhaustive while
    # avoiding text/CSS heuristics.
    return "to_accept"
