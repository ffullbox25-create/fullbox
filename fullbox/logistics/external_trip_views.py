from __future__ import annotations

import logging

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render

from employees.access import get_request_employee, get_request_role, role_required

from .external_trip import ExternalTripForm, create_external_trip, update_external_trip
from .models import LogisticsTrip


EXTERNAL_TRIP_ROLES = ("logistician", "manager", "head_manager", "director", "admin", "developer")
logger = logging.getLogger(__name__)


def _error_text(exc: ValidationError) -> str:
    return "; ".join(exc.messages) if getattr(exc, "messages", None) else str(exc)


def _trip_queryset():
    return LogisticsTrip.objects.select_related(
        "external_details__client",
        "assigned_driver",
        "assigned_logistician",
        "driver_assigned_by",
    )


@role_required(*EXTERNAL_TRIP_ROLES)
def external_trip_create(request):
    role = get_request_role(request)
    actor = get_request_employee(request)
    form = ExternalTripForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            trip = create_external_trip(cleaned_data=form.cleaned_data, actor=actor, user=request.user)
        except ValidationError as exc:
            form.add_error(None, _error_text(exc))
        else:
            messages.success(request, f"Внешний рейс {trip.number} создан и показан водителю.")
            return redirect("logistics:external-trip-detail", pk=trip.pk)
    return render(
        request,
        "logistics/external_trip_form.html",
        {
            "role": role,
            "active_nav": "trips",
            "trips_subnav": "planning",
            "form": form,
        },
    )


@role_required(*EXTERNAL_TRIP_ROLES)
def external_trip_detail(request, pk: int):
    role = get_request_role(request)
    actor = get_request_employee(request)
    trip = get_object_or_404(_trip_queryset(), pk=pk, trip_kind=LogisticsTrip.KIND_EXTERNAL)
    can_edit = trip.status in {LogisticsTrip.STATUS_DRAFT, LogisticsTrip.STATUS_PLANNED} and trip.driver_status in {
        LogisticsTrip.DRIVER_STATUS_UNASSIGNED,
        LogisticsTrip.DRIVER_STATUS_PRELIMINARY,
        LogisticsTrip.DRIVER_STATUS_ASSIGNED,
    }
    form = ExternalTripForm(request.POST or None, trip=trip)
    if request.method == "POST":
        if not can_edit:
            form.add_error(None, "Маршрут уже принят водителем и недоступен для изменения.")
        elif form.is_valid():
            try:
                trip = update_external_trip(trip=trip, cleaned_data=form.cleaned_data, actor=actor, user=request.user)
            except ValidationError as exc:
                form.add_error(None, _error_text(exc))
            except Exception:
                logger.exception("Cannot update external logistics trip %s", trip.pk)
                form.add_error(None, "Не удалось сохранить внешний рейс. Ошибка записана в журнал, попробуйте ещё раз или передайте номер рейса администратору.")
            else:
                messages.success(request, f"Внешний рейс {trip.number} обновлён.")
                return redirect("logistics:external-trip-detail", pk=trip.pk)

    billing_application = None
    try:
        from billing.models import BillingApplication

        billing_application = BillingApplication.objects.filter(
            application_type=BillingApplication.TYPE_LOGISTICS,
            application_id=trip.number,
            client=trip.external_details.client,
        ).first()
    except (ImportError, AttributeError):
        billing_application = None
    return render(
        request,
        "logistics/external_trip_detail.html",
        {
            "role": role,
            "active_nav": "trips",
            "trips_subnav": "planning",
            "trip": trip,
            "details": trip.external_details,
            "form": form,
            "can_edit": can_edit,
            "billing_application": billing_application,
        },
    )
