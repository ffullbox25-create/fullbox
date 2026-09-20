from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from employees.models import Employee
from sku.models import Agency

from .driver_workflow import (
    DRIVER_ASSIGNMENT_CONFIRMED,
    DRIVER_ASSIGNMENT_PRELIMINARY,
    assign_driver,
    log_trip_event,
    notify_employee,
)
from .models import LogisticsExternalTrip, LogisticsTrip, display_trip_number, next_trip_number


ASSIGNMENT_CHOICES = (
    (DRIVER_ASSIGNMENT_PRELIMINARY, "Предварительный план"),
    (DRIVER_ASSIGNMENT_CONFIRMED, "Подтверждённый рейс"),
)


class ExternalTripForm(forms.Form):
    client = forms.ModelChoiceField(label="Клиент для биллинга", queryset=Agency.objects.none())
    pickup_address = forms.CharField(label="Точка А — откуда забрать", widget=forms.Textarea(attrs={"rows": 2}))
    delivery_address = forms.CharField(label="Точка Б — куда доставить", widget=forms.Textarea(attrs={"rows": 2}))
    scheduled_at = forms.DateTimeField(
        label="Дата и время подачи",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=("%Y-%m-%dT%H:%M",),
    )
    driver = forms.ModelChoiceField(label="Водитель", queryset=Employee.objects.none())
    assignment_mode = forms.ChoiceField(
        label="Назначение",
        choices=ASSIGNMENT_CHOICES,
        initial=DRIVER_ASSIGNMENT_CONFIRMED,
    )
    cargo_description = forms.CharField(
        label="Описание груза",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Что забрать, количество мест, особенности груза"}),
    )
    pickup_contact = forms.CharField(label="Контакт в точке А", required=False)
    pickup_phone = forms.CharField(label="Телефон в точке А", required=False)
    delivery_contact = forms.CharField(label="Контакт в точке Б", required=False)
    delivery_phone = forms.CharField(label="Телефон в точке Б", required=False)
    vehicle_type = forms.ChoiceField(
        label="Тип транспорта",
        required=False,
        choices=(("", "Не указан"), *LogisticsTrip.VEHICLE_CHOICES),
    )
    vehicle_name = forms.CharField(label="Машина", required=False)
    vehicle_number = forms.CharField(label="Госномер", required=False)
    route_comment = forms.CharField(label="Комментарий водителю", required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, trip: LogisticsTrip | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["client"].queryset = Agency.objects.filter(archived=False).order_by("agn_name", "id")
        self.fields["driver"].queryset = (
            Employee.objects.filter(role="driver", is_active=True, user__isnull=False)
            .select_related("user")
            .order_by("full_name", "id")
        )
        if trip is None or self.is_bound:
            return
        details = trip.external_details
        local_schedule = timezone.localtime(trip.scheduled_at) if trip.scheduled_at else None
        assignment_mode = (
            DRIVER_ASSIGNMENT_PRELIMINARY
            if trip.driver_status == LogisticsTrip.DRIVER_STATUS_PRELIMINARY
            else DRIVER_ASSIGNMENT_CONFIRMED
        )
        self.initial.update(
            {
                "client": details.client_id,
                "pickup_address": details.pickup_address,
                "delivery_address": details.delivery_address,
                "scheduled_at": local_schedule,
                "driver": trip.assigned_driver_id,
                "assignment_mode": assignment_mode,
                "cargo_description": details.cargo_description,
                "pickup_contact": details.pickup_contact,
                "pickup_phone": details.pickup_phone,
                "delivery_contact": details.delivery_contact,
                "delivery_phone": details.delivery_phone,
                "vehicle_type": trip.vehicle_type,
                "vehicle_name": trip.vehicle_name,
                "vehicle_number": trip.vehicle_number,
                "route_comment": trip.route_comment,
            }
        )


def _trip_status_for_assignment(assignment_mode: str) -> str:
    if assignment_mode == DRIVER_ASSIGNMENT_PRELIMINARY:
        return LogisticsTrip.STATUS_DRAFT
    return LogisticsTrip.STATUS_PLANNED


def _external_values(cleaned_data: dict) -> dict:
    return {
        "client": cleaned_data["client"],
        "pickup_address": str(cleaned_data["pickup_address"] or "").strip(),
        "delivery_address": str(cleaned_data["delivery_address"] or "").strip(),
        "cargo_description": str(cleaned_data.get("cargo_description") or "").strip(),
        "pickup_contact": str(cleaned_data.get("pickup_contact") or "").strip(),
        "pickup_phone": str(cleaned_data.get("pickup_phone") or "").strip(),
        "delivery_contact": str(cleaned_data.get("delivery_contact") or "").strip(),
        "delivery_phone": str(cleaned_data.get("delivery_phone") or "").strip(),
    }


def external_trip_list_rows() -> list[dict]:
    trips = LogisticsTrip.objects.filter(trip_kind=LogisticsTrip.KIND_EXTERNAL).select_related(
        "external_details__client",
        "assigned_driver",
        "assigned_logistician",
    )
    rows = []
    for trip in trips.order_by("-created_at", "-id"):
        details = trip.external_details
        route = f"{details.pickup_address} → {details.delivery_address}"
        rows.append(
            {
                "trip": trip,
                "display_number": display_trip_number(trip.number),
                "status_label": trip.get_status_display(),
                "order_count": "Внешний",
                "client_preview": str(details.client),
                "destination_preview": route,
                "driver_name": getattr(trip.assigned_driver, "full_name", "") or trip.driver_name or "-",
                "vehicle_name": trip.vehicle_name or trip.get_vehicle_type_display() or "-",
                "vehicle_number_display": trip.vehicle_number or "-",
                "logistician_name": getattr(trip.assigned_logistician, "full_name", "") or "-",
                "resolved_delivery_count": int(trip.driver_status == LogisticsTrip.DRIVER_STATUS_DELIVERED),
                "next_action_label": "Открыть внешний рейс",
                "next_action_url": f"/logistics/trips/external/{trip.pk}/",
                "next_action_hint": "Маршрут без внутренних заявок",
                "is_external": True,
            }
        )
    return rows


@transaction.atomic
def create_external_trip(*, cleaned_data: dict, actor: Employee | None, user=None) -> LogisticsTrip:
    scheduled_at = cleaned_data["scheduled_at"]
    assignment_mode = cleaned_data["assignment_mode"]
    trip = LogisticsTrip.objects.create(
        number=next_trip_number(),
        trip_kind=LogisticsTrip.KIND_EXTERNAL,
        trip_date=timezone.localtime(scheduled_at).date(),
        scheduled_at=scheduled_at,
        status=_trip_status_for_assignment(assignment_mode),
        vehicle_type=cleaned_data.get("vehicle_type") or "",
        vehicle_name=str(cleaned_data.get("vehicle_name") or "").strip(),
        vehicle_number=str(cleaned_data.get("vehicle_number") or "").strip(),
        route_comment=str(cleaned_data.get("route_comment") or "").strip(),
        assigned_logistician=actor if actor and actor.role == "logistician" else None,
        last_edited_by=actor,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    LogisticsExternalTrip.objects.create(trip=trip, **_external_values(cleaned_data))
    assign_driver(
        trip=trip,
        driver=cleaned_data["driver"],
        actor=actor,
        user=user,
        scheduled_at=scheduled_at.isoformat(),
        assignment_mode=assignment_mode,
    )
    trip.refresh_from_db()
    log_trip_event(
        trip,
        event_type="external_trip_created",
        description="Создан внешний рейс без внутренних заявок.",
        user=user,
        payload={
            "client_id": trip.external_details.client_id,
            "pickup_address": trip.external_details.pickup_address,
            "delivery_address": trip.external_details.delivery_address,
            "assignment_mode": assignment_mode,
        },
    )
    return trip


@transaction.atomic
def update_external_trip(*, trip: LogisticsTrip, cleaned_data: dict, actor: Employee | None, user=None) -> LogisticsTrip:
    locked = (
        LogisticsTrip.objects.select_for_update()
        .select_related("external_details", "assigned_driver")
        .get(pk=trip.pk)
    )
    if locked.trip_kind != LogisticsTrip.KIND_EXTERNAL:
        raise ValidationError("Этот рейс не является внешним.")
    if locked.status not in {LogisticsTrip.STATUS_DRAFT, LogisticsTrip.STATUS_PLANNED} or locked.driver_status not in {
        LogisticsTrip.DRIVER_STATUS_UNASSIGNED,
        LogisticsTrip.DRIVER_STATUS_PRELIMINARY,
        LogisticsTrip.DRIVER_STATUS_ASSIGNED,
    }:
        raise ValidationError("Маршрут внешнего рейса можно менять только до принятия рейса водителем.")

    scheduled_at = cleaned_data["scheduled_at"]
    assignment_mode = cleaned_data["assignment_mode"]
    locked.trip_date = timezone.localtime(scheduled_at).date()
    locked.scheduled_at = scheduled_at
    locked.status = _trip_status_for_assignment(assignment_mode)
    locked.vehicle_type = cleaned_data.get("vehicle_type") or ""
    locked.vehicle_name = str(cleaned_data.get("vehicle_name") or "").strip()
    locked.vehicle_number = str(cleaned_data.get("vehicle_number") or "").strip()
    locked.route_comment = str(cleaned_data.get("route_comment") or "").strip()
    locked.last_edited_by = actor
    locked.edit_version = int(locked.edit_version or 1) + 1
    locked.save(
        update_fields=[
            "trip_date",
            "scheduled_at",
            "status",
            "vehicle_type",
            "vehicle_name",
            "vehicle_number",
            "route_comment",
            "last_edited_by",
            "edit_version",
            "updated_at",
        ]
    )
    details = locked.external_details
    old_driver_id = locked.assigned_driver_id
    old_route_payload = {
        "client_id": details.client_id,
        "pickup_address": details.pickup_address,
        "delivery_address": details.delivery_address,
        "cargo_description": details.cargo_description,
        "pickup_contact": details.pickup_contact,
        "pickup_phone": details.pickup_phone,
        "delivery_contact": details.delivery_contact,
        "delivery_phone": details.delivery_phone,
    }
    new_route_payload = _external_values(cleaned_data)
    route_changed = (
        old_route_payload["client_id"] != getattr(new_route_payload["client"], "pk", None)
        or any(new_route_payload[field] != old_route_payload[field] for field in old_route_payload if field != "client_id")
    )
    for field, value in new_route_payload.items():
        setattr(details, field, value)
    details.save()
    assign_driver(
        trip=locked,
        driver=cleaned_data["driver"],
        actor=actor,
        user=user,
        scheduled_at=scheduled_at.isoformat(),
        assignment_mode=assignment_mode,
    )
    if route_changed and old_driver_id == cleaned_data["driver"].pk:
        notify_employee(
            recipient=cleaned_data["driver"],
            trip=locked,
            title=f"Изменён маршрут рейса {locked.number}",
            text=f"Проверьте обновлённый маршрут: {details.pickup_address} → {details.delivery_address}.",
            detail_url=f"/logistics/driver/trips/{locked.pk}/",
            event="external-route-updated",
        )
    log_trip_event(
        locked,
        event_type="external_trip_updated",
        description="Параметры внешнего рейса обновлены.",
        user=user,
        payload={"assignment_mode": assignment_mode},
    )
    locked.refresh_from_db()
    return locked
