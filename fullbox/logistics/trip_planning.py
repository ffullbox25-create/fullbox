from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Prefetch, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from employees.access import resolve_cabinet_url

from .driver_workflow import log_trip_event
from .models import LogisticsTrip, LogisticsTripOrder, is_draft_trip_number


ACTIVE_UNDATED_STATUSES = (
    LogisticsTrip.STATUS_DRAFT,
    LogisticsTrip.STATUS_PLANNED,
    LogisticsTrip.STATUS_LOADING,
    LogisticsTrip.STATUS_DEPARTED,
)
RUSSIAN_MONTHS = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
WEEKDAY_NAMES = (
    ("Понедельник", "Пн"),
    ("Вторник", "Вт"),
    ("Среда", "Ср"),
    ("Четверг", "Чт"),
    ("Пятница", "Пт"),
    ("Суббота", "Сб"),
    ("Воскресенье", "Вс"),
)
PLANNING_ACTION_ROLES = {"logistician", "manager", "head_manager", "director", "admin", "developer"}
PLANNING_LOCKED_STATUSES = {
    LogisticsTrip.STATUS_DEPARTED,
    LogisticsTrip.STATUS_COMPLETED,
    LogisticsTrip.STATUS_CANCELED,
}
PLANNING_LOCKED_DRIVER_STATUSES = {
    LogisticsTrip.DRIVER_STATUS_ACCEPTED,
    LogisticsTrip.DRIVER_STATUS_EN_ROUTE,
    LogisticsTrip.DRIVER_STATUS_AWAITING_DELIVERY,
    LogisticsTrip.DRIVER_STATUS_DELIVERED,
    LogisticsTrip.DRIVER_STATUS_PROBLEM,
}


def normalize_week_start(raw_value: str | None, *, today: date | None = None) -> date:
    current = today or timezone.localdate()
    normalized_value = str(raw_value or "").strip()
    if not normalized_value:
        selected = current + timedelta(days=1) if current.weekday() == 6 else current
    else:
        try:
            selected = date.fromisoformat(normalized_value)
        except (TypeError, ValueError):
            selected = current
    return selected - timedelta(days=selected.weekday())


def _week_label(week_start: date, week_end: date) -> str:
    if week_start.year == week_end.year and week_start.month == week_end.month:
        return f"{week_start.day}–{week_end.day} {RUSSIAN_MONTHS[week_start.month]} {week_end.year}"
    return (
        f"{week_start.day} {RUSSIAN_MONTHS[week_start.month]} — "
        f"{week_end.day} {RUSSIAN_MONTHS[week_end.month]} {week_end.year}"
    )


def _linked_order_date(order) -> date | None:
    if order.slot_date:
        return order.slot_date
    if order.planned_ship_date:
        return order.planned_ship_date
    if order.eta_at:
        return timezone.localtime(order.eta_at).date()
    return None


def _trip_plan_date(trip: LogisticsTrip) -> tuple[date | None, str]:
    if trip.scheduled_at:
        return timezone.localtime(trip.scheduled_at).date(), "scheduled"
    if trip.trip_date:
        return trip.trip_date, "trip"
    linked_dates = {
        linked_date
        for link in trip.orders.all()
        if (linked_date := _linked_order_date(link.shipping_order)) is not None
    }
    if len(linked_dates) == 1:
        return linked_dates.pop(), "orders"
    return None, "undated"


def can_change_trip_from_planning(trip: LogisticsTrip) -> bool:
    return (
        trip.status not in PLANNING_LOCKED_STATUSES
        and trip.driver_status not in PLANNING_LOCKED_DRIVER_STATUSES
    )


def _schedule_input_value(trip: LogisticsTrip, *, plan_date: date | None) -> str:
    if trip.scheduled_at:
        return timezone.localtime(trip.scheduled_at).strftime("%Y-%m-%dT%H:%M")
    if trip.trip_date:
        return f"{trip.trip_date.isoformat()}T09:00"
    if plan_date:
        return f"{plan_date.isoformat()}T09:00"
    return ""


def _route_label(trip: LogisticsTrip) -> str:
    if trip.trip_kind == LogisticsTrip.KIND_EXTERNAL:
        details = getattr(trip, "external_details", None)
        if details:
            return f"{details.pickup_address} → {details.delivery_address}"
    destinations: list[str] = []
    seen: set[str] = set()
    for link in trip.orders.all():
        order = link.shipping_order
        destination = str(order.destination_warehouse or order.destination_address or "").strip()
        if not destination or destination in seen:
            continue
        seen.add(destination)
        destinations.append(destination)
    if not destinations:
        return "Маршрут уточняется"
    visible = destinations[:2]
    label = " · ".join(visible)
    if len(destinations) > len(visible):
        label += f" · ещё {len(destinations) - len(visible)}"
    return label


def _trip_card(trip: LogisticsTrip, *, plan_date: date | None, date_source: str) -> dict:
    links = list(trip.orders.all())
    carrier_name = ""
    if trip.carrier_id:
        carrier_name = str(trip.carrier.short_name or trip.carrier.name or "").strip()
    vehicle_parts = [
        str(trip.vehicle_name or trip.get_vehicle_type_display() or "").strip(),
        str(trip.vehicle_number or "").strip(),
    ]
    driver_name = (
        str(getattr(trip.assigned_driver, "full_name", "") or "").strip()
        or str(trip.driver_name or "").strip()
        or "Не назначен"
    )
    status_class = {
        LogisticsTrip.STATUS_DRAFT: "draft",
        LogisticsTrip.STATUS_PLANNED: "planned",
        LogisticsTrip.STATUS_LOADING: "loading",
        LogisticsTrip.STATUS_DEPARTED: "departed",
        LogisticsTrip.STATUS_COMPLETED: "completed",
        LogisticsTrip.STATUS_CANCELED: "canceled",
    }.get(trip.status, "default")
    if trip.driver_status == LogisticsTrip.DRIVER_STATUS_PRELIMINARY:
        assignment_class = "preliminary"
    elif trip.driver_status in {
        LogisticsTrip.DRIVER_STATUS_ASSIGNED,
        LogisticsTrip.DRIVER_STATUS_ACCEPTED,
        LogisticsTrip.DRIVER_STATUS_EN_ROUTE,
        LogisticsTrip.DRIVER_STATUS_AWAITING_DELIVERY,
    }:
        assignment_class = "confirmed"
    else:
        assignment_class = ""
    is_external = trip.trip_kind == LogisticsTrip.KIND_EXTERNAL
    external_details = getattr(trip, "external_details", None) if is_external else None
    return {
        "trip": trip,
        "number": "Черновик" if is_draft_trip_number(trip.number) else trip.number,
        "status_label": trip.get_status_display(),
        "status_class": status_class,
        "assignment_label": trip.get_driver_status_display(),
        "assignment_class": assignment_class,
        "plan_date": plan_date,
        "date_source": date_source,
        "date_source_label": "По заявкам" if date_source == "orders" else "",
        "time_label": timezone.localtime(trip.scheduled_at).strftime("%H:%M") if trip.scheduled_at else "",
        "route_label": _route_label(trip),
        "carrier_label": carrier_name or "Не указан",
        "vehicle_label": " · ".join(part for part in vehicle_parts if part) or "—",
        "driver_label": driver_name,
        "order_count": len(links),
        "box_count": sum(int(link.shipping_order.expected_boxes or 0) for link in links),
        "is_external": is_external,
        "kind_label": "Внешний рейс" if is_external else "",
        "client_label": str(external_details.client) if external_details else "",
        "cargo_label": str(getattr(external_details, "cargo_description", "") or "").strip(),
        "detail_url": reverse("logistics:external-trip-detail", args=[trip.pk]) if is_external else reverse("logistics:trip-detail", args=[trip.pk]),
        "planning_action_url": reverse("logistics:trip-planning-action", args=[trip.pk]),
        "schedule_input_value": _schedule_input_value(trip, plan_date=plan_date),
        "can_plan_edit": can_change_trip_from_planning(trip),
    }


def _parse_planning_datetime(value: str | None) -> datetime:
    raw = str(value or "").strip()
    parsed = parse_datetime(raw)
    if parsed is None:
        parsed_date = parse_date(raw)
        if parsed_date is not None:
            parsed = datetime.combine(parsed_date, time(hour=9))
    if parsed is None:
        raise ValidationError("Укажите новую дату и время рейса.")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _reason_label(reason: str) -> str:
    clean_reason = str(reason or "").strip()
    return f" Причина: {clean_reason}" if clean_reason else ""


@transaction.atomic
def change_trip_from_planning(
    *,
    trip_id: int,
    action: str,
    scheduled_at: str | None = None,
    reason: str = "",
    actor=None,
    user=None,
) -> LogisticsTrip:
    locked = LogisticsTrip.objects.select_for_update(of=("self",)).get(pk=trip_id)
    if not can_change_trip_from_planning(locked):
        raise ValidationError("Рейс уже нельзя менять из планирования: он отменен, завершен или принят в работу водителем.")

    action = str(action or "").strip().lower()
    actor_user = user if getattr(user, "is_authenticated", False) else None
    if action == "reschedule":
        parsed_schedule = _parse_planning_datetime(scheduled_at)
        previous = {
            "trip_date": str(locked.trip_date or ""),
            "scheduled_at": locked.scheduled_at.isoformat() if locked.scheduled_at else "",
        }
        locked.scheduled_at = parsed_schedule
        locked.trip_date = timezone.localtime(parsed_schedule).date()
        locked.last_edited_by = actor
        locked.edit_version = int(locked.edit_version or 1) + 1
        locked.save(update_fields=["scheduled_at", "trip_date", "last_edited_by", "edit_version", "updated_at"])
        label = timezone.localtime(parsed_schedule).strftime("%d.%m.%Y %H:%M")
        log_trip_event(
            locked,
            event_type="planning_rescheduled",
            description=f"Рейс перенесен в недельном планировании на {label}.{_reason_label(reason)}",
            user=actor_user,
            payload={**previous, "new_scheduled_at": parsed_schedule.isoformat(), "reason": str(reason or "").strip()},
        )
        return locked

    if action == "cancel":
        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValidationError("Укажите причину отмены рейса.")
        previous_status = locked.status
        locked.status = LogisticsTrip.STATUS_CANCELED
        locked.last_edited_by = actor
        locked.edit_version = int(locked.edit_version or 1) + 1
        locked.save(update_fields=["status", "last_edited_by", "edit_version", "updated_at"])
        log_trip_event(
            locked,
            event_type="planning_trip_canceled",
            description=f"Рейс отменен в недельном планировании. Причина: {clean_reason}",
            user=actor_user,
            payload={"previous_status": previous_status, "status": locked.status, "reason": clean_reason},
        )
        return locked

    raise ValidationError("Некорректное действие с рейсом.")


def build_trip_planning_context(*, role: str, week_value: str | None = None) -> dict:
    from teammanager.roles import CABINET_ROLES

    today = timezone.localdate()
    week_start = normalize_week_start(week_value, today=today)
    week_end = week_start + timedelta(days=6)
    trip_orders = LogisticsTripOrder.objects.select_related(
        "shipping_order",
        "shipping_order__agency",
        "shipping_order__marketplace",
    ).order_by("delivery_sequence", "loading_sequence", "id")
    trips = list(
        LogisticsTrip.objects.select_related(
            "carrier",
            "assigned_driver",
            "assigned_logistician",
            "external_details__client",
        )
        .prefetch_related(Prefetch("orders", queryset=trip_orders))
        .filter(
            Q(scheduled_at__date__range=(week_start, week_end))
            | Q(scheduled_at__isnull=True, trip_date__range=(week_start, week_end))
            | Q(
                scheduled_at__isnull=True,
                trip_date__isnull=True,
                status__in=ACTIVE_UNDATED_STATUSES,
            )
        )
        .order_by("scheduled_at", "trip_date", "created_at", "id")
    )

    grouped: dict[date, list[dict]] = defaultdict(list)
    undated_rows: list[dict] = []
    for trip in trips:
        plan_date, date_source = _trip_plan_date(trip)
        card = _trip_card(trip, plan_date=plan_date, date_source=date_source)
        if plan_date is None:
            undated_rows.append(card)
        elif week_start <= plan_date <= week_end:
            grouped[plan_date].append(card)

    days = []
    for offset, (full_name, short_name) in enumerate(WEEKDAY_NAMES):
        day_date = week_start + timedelta(days=offset)
        rows = grouped.get(day_date, [])
        days.append(
            {
                "date": day_date,
                "full_name": full_name,
                "short_name": short_name,
                "is_today": day_date == today,
                "rows": rows,
                "count": len(rows),
            }
        )

    planning_url = reverse("logistics:trip-planning")
    sidebar_trip = (
        LogisticsTrip.objects.filter(status=LogisticsTrip.STATUS_DRAFT, trip_kind=LogisticsTrip.KIND_INTERNAL)
        .order_by("-updated_at", "-id")
        .first()
    )
    return {
        "role": role,
        "cabinet_url": resolve_cabinet_url(role),
        "active_nav": "trips",
        "trips_subnav": "planning",
        "use_cabinet_shell": (role or "") in CABINET_ROLES,
        "sidebar_trip": sidebar_trip,
        "days": days,
        "undated_rows": undated_rows,
        "week_start": week_start,
        "week_end": week_end,
        "week_label": _week_label(week_start, week_end),
        "previous_week_url": f"{planning_url}?week={(week_start - timedelta(days=7)).isoformat()}",
        "current_week_url": planning_url,
        "next_week_url": f"{planning_url}?week={(week_start + timedelta(days=7)).isoformat()}",
        "refresh_url": f"{planning_url}?week={week_start.isoformat()}",
        "last_updated_label": timezone.localtime().strftime("%H:%M"),
        "external_trip_create_url": reverse("logistics:external-trip-create"),
    }
