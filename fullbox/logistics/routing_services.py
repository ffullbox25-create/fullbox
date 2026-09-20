"""Маршрутизация отгрузки и возврат менеджеру на уточнение (без параллельной системы задач)."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from employees.models import Employee
from todo.models import Task

from .models import LogisticsTrip, LogisticsTripOrder, ShippingRoutingState

CLARIFY_REASONS = ShippingRoutingState.REASON_CHOICES


def get_or_create_routing_state(order) -> ShippingRoutingState:
    state, _ = ShippingRoutingState.objects.get_or_create(
        shipping_order=order,
        defaults={"status": ShippingRoutingState.STATUS_READY},
    )
    return state


def _snapshot_from_state_and_trip(state, trip) -> dict:
    state_status = getattr(state, "status", "") or ""
    trip_status = _status_from_trip(trip) if trip else ""
    if trip_status and state_status not in {
        ShippingRoutingState.STATUS_CLARIFY,
        ShippingRoutingState.STATUS_FAILED,
    }:
        status = trip_status
    else:
        status = state_status or trip_status or ""
    return {
        "status": status,
        "status_label": (
            dict(ShippingRoutingState.STATUS_CHOICES).get(status, "")
            or ("Готова к маршрутизации" if status == ShippingRoutingState.STATUS_READY else "")
        ),
        "reason_code": getattr(state, "reason_code", "") or "",
        "reason_label": getattr(state, "reason_label", "") or "",
        "reason_text": getattr(state, "reason_text", "") or "",
        "needs_clarification": status == ShippingRoutingState.STATUS_CLARIFY,
        "trip_number": getattr(trip, "number", "") or "",
        "trip_status": getattr(trip, "status", "") or "",
        "trip_id": getattr(trip, "id", None),
        "driver_name": getattr(trip, "driver_name", "") or "",
        "vehicle_number": getattr(trip, "vehicle_number", "") or "",
        "returned_at": getattr(state, "returned_at", None),
    }


def routing_snapshots_for_orders(order_ids) -> dict[int, dict]:
    """Пакетный снимок маршрутизации — без N+1 на карточке задач."""
    ids = [int(x) for x in order_ids if x]
    if not ids:
        return {}
    states = {
        row.shipping_order_id: row
        for row in ShippingRoutingState.objects.filter(shipping_order_id__in=ids)
    }
    trip_by_order: dict[int, object] = {}
    links = (
        LogisticsTripOrder.objects.select_related("trip")
        .filter(shipping_order_id__in=ids)
        .exclude(trip__status=LogisticsTrip.STATUS_CANCELED)
        .order_by("-id")
    )
    for link in links:
        if link.shipping_order_id not in trip_by_order:
            trip_by_order[link.shipping_order_id] = link.trip
    return {
        oid: _snapshot_from_state_and_trip(states.get(oid), trip_by_order.get(oid))
        for oid in ids
    }


def routing_snapshot(order, *, cache: dict | None = None) -> dict:
    """Безопасный снимок для UI / task row (без исключения, если модели ещё нет в БД)."""
    if order is None or not getattr(order, "pk", None):
        return _snapshot_from_state_and_trip(None, None)
    if cache is not None and order.pk in cache:
        return cache[order.pk]
    state = None
    try:
        state = ShippingRoutingState.objects.filter(shipping_order_id=order.pk).first()
    except Exception:
        state = None
    trip_link = None
    try:
        trip_link = (
            LogisticsTripOrder.objects.select_related("trip")
            .filter(shipping_order_id=order.pk)
            .exclude(trip__status=LogisticsTrip.STATUS_CANCELED)
            .order_by("-id")
            .first()
        )
    except Exception:
        trip_link = None
    trip = getattr(trip_link, "trip", None) if trip_link else None
    snap = _snapshot_from_state_and_trip(state, trip)
    if cache is not None:
        cache[order.pk] = snap
    return snap


def _status_from_trip(trip: LogisticsTrip) -> str:
    if trip.status == LogisticsTrip.STATUS_COMPLETED:
        return ShippingRoutingState.STATUS_DELIVERED
    if trip.status == LogisticsTrip.STATUS_DEPARTED:
        return ShippingRoutingState.STATUS_IN_TRANSIT
    if trip.status in {
        LogisticsTrip.STATUS_PLANNED,
        LogisticsTrip.STATUS_LOADING,
        LogisticsTrip.STATUS_DRAFT,
    }:
        return ShippingRoutingState.STATUS_ROUTED
    return ShippingRoutingState.STATUS_READY


def _close_order_logistician_task_for_trip(order, trip: LogisticsTrip) -> None:
    if trip.status not in {
        LogisticsTrip.STATUS_LOADING,
        LogisticsTrip.STATUS_DEPARTED,
        LogisticsTrip.STATUS_COMPLETED,
    }:
        return
    from shipping.services import close_logistician_task

    close_logistician_task(order)


def mark_ready_for_routing(order, *, user=None) -> ShippingRoutingState:
    state = get_or_create_routing_state(order)
    if state.status == ShippingRoutingState.STATUS_CLARIFY:
        return state
    if state.status in {
        ShippingRoutingState.STATUS_ROUTED,
        ShippingRoutingState.STATUS_IN_TRANSIT,
        ShippingRoutingState.STATUS_DELIVERED,
        ShippingRoutingState.STATUS_FAILED,
    }:
        return state
    state.status = ShippingRoutingState.STATUS_READY
    state.save(update_fields=["status", "updated_at"])
    return state


def sync_routing_from_trip(order) -> ShippingRoutingState | None:
    if not getattr(order, "pk", None):
        return None
    link = (
        LogisticsTripOrder.objects.select_related("trip")
        .filter(shipping_order_id=order.pk)
        .exclude(trip__status=LogisticsTrip.STATUS_CANCELED)
        .order_by("-id")
        .first()
    )
    if not link:
        state = get_or_create_routing_state(order)
        if state.status not in {
            ShippingRoutingState.STATUS_READY,
            ShippingRoutingState.STATUS_CLARIFY,
            ShippingRoutingState.STATUS_DELIVERED,
            ShippingRoutingState.STATUS_FAILED,
        }:
            state.status = ShippingRoutingState.STATUS_READY
            state.save(update_fields=["status", "updated_at"])
        return state
    _close_order_logistician_task_for_trip(order, link.trip)
    state = get_or_create_routing_state(order)
    if state.status == ShippingRoutingState.STATUS_CLARIFY:
        return state
    # Manual MP outcomes must not be overwritten by trip sync.
    if state.status in ShippingRoutingState.TERMINAL_DELIVERY_STATUSES:
        return state
    new_status = _status_from_trip(link.trip)
    if state.status != new_status:
        state.status = new_status
        state.save(update_fields=["status", "updated_at"])
    return state


def _active_trip_for_order(order) -> LogisticsTrip | None:
    link = (
        LogisticsTripOrder.objects.select_related("trip")
        .filter(shipping_order_id=order.pk)
        .exclude(trip__status=LogisticsTrip.STATUS_CANCELED)
        .order_by("-id")
        .first()
    )
    return getattr(link, "trip", None) if link else None


def delivery_outcome_will_complete_trip(trip: LogisticsTrip, order) -> bool:
    """Проверить, станет ли текущая отметка последней в рейсе."""
    if trip is None or trip.status != LogisticsTrip.STATUS_DEPARTED:
        return False
    order_ids = list(trip.orders.values_list("shipping_order_id", flat=True))
    if not order_ids or getattr(order, "pk", None) not in order_ids:
        return False
    other_ids = [order_id for order_id in order_ids if order_id != order.pk]
    if not other_ids:
        return True
    terminal_count = ShippingRoutingState.objects.filter(
        shipping_order_id__in=other_ids,
        status__in=ShippingRoutingState.TERMINAL_DELIVERY_STATUSES,
    ).count()
    return terminal_count == len(other_ids)


@transaction.atomic
def maybe_complete_trip_after_delivery_outcomes(trip: LogisticsTrip, *, user=None) -> bool:
    """If every order on the trip has delivered/failed routing — mark trip completed."""
    if trip is None:
        return False
    locked_trip = LogisticsTrip.objects.select_for_update().get(pk=trip.pk)
    if locked_trip.status != LogisticsTrip.STATUS_DEPARTED:
        return False
    links = list(locked_trip.orders.select_related("shipping_order").all())
    if not links:
        return False
    order_ids = [link.shipping_order_id for link in links if link.shipping_order_id]
    states = {
        row.shipping_order_id: row.status
        for row in ShippingRoutingState.objects.filter(shipping_order_id__in=order_ids)
    }
    for oid in order_ids:
        if states.get(oid) not in ShippingRoutingState.TERMINAL_DELIVERY_STATUSES:
            return False
    from billing.warehouse_services import (
        require_logistics_trip_completion_facts,
        sync_logistics_trip_facts_to_billing,
    )

    require_logistics_trip_completion_facts(locked_trip)
    locked_trip.status = LogisticsTrip.STATUS_COMPLETED
    locked_trip.closed_at = locked_trip.closed_at or timezone.now()
    locked_trip.save(update_fields=["status", "closed_at", "updated_at"])
    sync_logistics_trip_facts_to_billing(trip=locked_trip, user=user)
    Task.objects.filter(
        route=f"/logistics/trips/{locked_trip.pk}/",
        title__startswith="Подтвердить доставку:",
    ).exclude(status="done").update(status="done", updated_at=timezone.now())
    for link in links:
        _close_order_logistician_task_for_trip(link.shipping_order, locked_trip)
    try:
        from client_cabinet.chat_trips import archive_trip_thread

        archive_trip_thread(locked_trip)
    except Exception:
        pass
    return True


@transaction.atomic
def mark_order_delivered(order, *, user=None) -> ShippingRoutingState:
    state = get_or_create_routing_state(order)
    state.status = ShippingRoutingState.STATUS_DELIVERED
    state.save(update_fields=["status", "updated_at"])
    trip = _active_trip_for_order(order)
    if trip is not None:
        maybe_complete_trip_after_delivery_outcomes(trip, user=user)
    _audit_routing(
        order,
        user=user,
        action="status",
        payload={
            "description": "Сдано на маркетплейс",
            "routing_status": state.status,
            "status_label": state.status_label,
        },
    )
    return state


@transaction.atomic
def mark_order_delivery_failed(order, *, user=None) -> ShippingRoutingState:
    state = get_or_create_routing_state(order)
    state.status = ShippingRoutingState.STATUS_FAILED
    state.save(update_fields=["status", "updated_at"])
    trip = _active_trip_for_order(order)
    if trip is not None:
        maybe_complete_trip_after_delivery_outcomes(trip, user=user)
    _audit_routing(
        order,
        user=user,
        action="status",
        payload={
            "description": "Не сдана на маркетплейс",
            "routing_status": state.status,
            "status_label": state.status_label,
        },
    )
    return state


def _audit_routing(order, *, user, action: str, payload: dict) -> None:
    try:
        from audit.models import log_order_action

        log_order_action(
            order_id=str(order.number or order.pk),
            order_type="shipping",
            action=action,
            agency=getattr(order, "agency", None),
            user=user if getattr(user, "is_authenticated", False) else None,
            description=payload.get("description") or "",
            payload={
                "act": "shipping_routing",
                **payload,
            },
        )
    except Exception:
        pass


@transaction.atomic
def return_to_manager_for_clarification(
    *,
    order,
    employee: Employee,
    reason_code: str,
    reason_text: str = "",
    user=None,
) -> ShippingRoutingState:
    if employee is None or employee.role not in {
        "logistician",
        "manager",
        "head_manager",
        "director",
        "admin",
        "developer",
    }:
        raise PermissionError("Нет права вернуть заявку на уточнение")
    valid_codes = {c for c, _ in ShippingRoutingState.REASON_CHOICES}
    if reason_code not in valid_codes:
        raise ValueError("Укажите причину возврата")
    if reason_code == ShippingRoutingState.REASON_OTHER and not (reason_text or "").strip():
        raise ValueError("Для причины «Другое» нужен комментарий")

    state = get_or_create_routing_state(order)
    state.status = ShippingRoutingState.STATUS_CLARIFY
    state.reason_code = reason_code
    state.reason_text = (reason_text or "").strip()
    state.returned_by = employee
    state.returned_at = timezone.now()
    state.resolved_by = None
    state.resolved_at = None
    state.save()

    from shipping.services import close_logistician_task, ensure_manager_review_task

    close_logistician_task(order)
    task = ensure_manager_review_task(order, user=user)
    if task:
        reason_label = dict(ShippingRoutingState.REASON_CHOICES).get(reason_code, reason_code)
        task.description = (
            f"Логист вернул на уточнение: {reason_label}. "
            f"{(reason_text or '').strip()} "
            f"Клиент: {getattr(order.agency, 'agn_name', '') or order.agency_id}."
        ).strip()
        task.status = "backlog" if task.status == "done" else task.status
        task.save(update_fields=["description", "status", "updated_at"])

    _audit_routing(
        order,
        user=user,
        action="status",
        payload={
            "description": "Возврат менеджеру на уточнение",
            "routing_status": state.status,
            "reason_code": reason_code,
            "reason_text": state.reason_text,
            "status_label": "Требуется уточнение",
        },
    )
    return state


@transaction.atomic
def resolve_clarification_and_return_to_logistics(
    *,
    order,
    employee: Employee,
    comment: str = "",
    user=None,
) -> ShippingRoutingState:
    if employee is None or employee.role not in {
        "manager",
        "head_manager",
        "director",
        "admin",
        "developer",
        "logistician",
    }:
        raise PermissionError("Нет права снять уточнение")
    state = get_or_create_routing_state(order)
    if state.status != ShippingRoutingState.STATUS_CLARIFY:
        raise ValueError("Заявка не находится на уточнении")

    state.status = ShippingRoutingState.STATUS_READY
    state.resolved_by = employee
    state.resolved_at = timezone.now()
    state.save(
        update_fields=[
            "status",
            "resolved_by",
            "resolved_at",
            "updated_at",
        ]
    )

    from shipping.services import close_manager_review_task, ensure_logistician_task

    close_manager_review_task(order)
    ensure_logistician_task(order, user=user)

    _audit_routing(
        order,
        user=user,
        action="status",
        payload={
            "description": "Данные уточнены, возврат логисту",
            "routing_status": state.status,
            "comment": (comment or "").strip(),
            "status_label": "Готова к маршрутизации",
        },
    )
    return state


def can_return_for_clarification(role: str | None, order) -> bool:
    if role not in {"logistician", "manager", "head_manager", "director", "admin", "developer"}:
        return False
    if str(getattr(order, "status", "") or "") != "packed":
        return False
    return not routing_snapshot(order).get("needs_clarification")


def can_resolve_clarification(role: str | None, order) -> bool:
    if role not in {"manager", "head_manager", "director", "admin", "developer"}:
        return False
    return bool(routing_snapshot(order).get("needs_clarification"))
