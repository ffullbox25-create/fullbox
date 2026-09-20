"""Optimistic lock и soft-presence для совместного редактирования рейса."""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import LogisticsTrip

PRESENCE_TTL = timedelta(minutes=2)
CONFLICT_MESSAGE = (
    "Рейс изменён другим сотрудником. Обновите страницу и повторите действие — "
    "незаметная перезапись данных заблокирована."
)


class TripEditConflict(ValueError):
    """Версия рейса на сервере не совпадает с ожидаемой клиентом."""


def parse_expected_version(raw) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def assert_trip_version(trip: LogisticsTrip, expected_version: int | None) -> None:
    """
    Проверяет версию до мутации.
    Если клиент не прислал версию (старые формы) — пропускаем жёсткую блокировку,
    но всё равно bump после успешного сохранения.
    """
    if expected_version is None:
        return
    current = int(getattr(trip, "edit_version", 1) or 1)
    if expected_version != current:
        editor = ""
        last = getattr(trip, "last_edited_by", None)
        if last is not None:
            editor = f" Последнее изменение: {last.full_name or last}."
        raise TripEditConflict(CONFLICT_MESSAGE + editor)


@transaction.atomic
def bump_trip_edit_version(
    trip: LogisticsTrip,
    *,
    expected_version: int | None = None,
    editor=None,
    extra_updates: dict | None = None,
) -> LogisticsTrip:
    """
    Атомарно увеличивает edit_version.
    Если expected_version задан — обновляет только при совпадении (optimistic lock).
    """
    assert_trip_version(trip, expected_version)
    qs = LogisticsTrip.objects.filter(pk=trip.pk)
    if expected_version is not None:
        qs = qs.filter(edit_version=expected_version)

    updates = {
        "edit_version": F("edit_version") + 1,
        "updated_at": timezone.now(),
    }
    if editor is not None:
        updates["last_edited_by"] = editor
        updates["editing_by"] = editor
        updates["editing_heartbeat_at"] = timezone.now()
    if extra_updates:
        updates.update(extra_updates)

    updated = qs.update(**updates)
    if updated == 0:
        trip.refresh_from_db()
        editor_name = ""
        last = getattr(trip, "last_edited_by", None)
        if last is not None:
            editor_name = f" Последнее изменение: {last.full_name or last}."
        raise TripEditConflict(CONFLICT_MESSAGE + editor_name)

    trip.refresh_from_db()
    return trip


def touch_trip_presence(trip: LogisticsTrip, employee) -> None:
    """Отмечает, что сотрудник сейчас смотрит/редактирует карточку рейса."""
    if employee is None or not getattr(employee, "pk", None):
        return
    LogisticsTrip.objects.filter(pk=trip.pk).update(
        editing_by=employee,
        editing_heartbeat_at=timezone.now(),
    )
    trip.editing_by = employee
    trip.editing_heartbeat_at = timezone.now()


def concurrent_editor(trip: LogisticsTrip, current_employee=None):
    """Кто ещё недавно был на карточке рейса (soft presence)."""
    other = getattr(trip, "editing_by", None)
    if other is None:
        return None
    if current_employee is not None and other.pk == getattr(current_employee, "pk", None):
        return None
    heartbeat = getattr(trip, "editing_heartbeat_at", None)
    if heartbeat is None:
        return None
    if timezone.now() - heartbeat > PRESENCE_TTL:
        return None
    return other


def trip_concurrency_context(trip: LogisticsTrip, current_employee=None) -> dict:
    other = concurrent_editor(trip, current_employee)
    last = getattr(trip, "last_edited_by", None)
    return {
        "trip_edit_version": int(getattr(trip, "edit_version", 1) or 1),
        "trip_last_edited_by": last,
        "trip_last_edited_label": (last.full_name if last else "") or "",
        "trip_concurrent_editor": other,
        "trip_concurrent_editor_label": (other.full_name if other else "") or "",
    }
