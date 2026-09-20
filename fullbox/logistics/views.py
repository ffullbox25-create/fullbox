from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from employees.access import get_employee_for_user, get_request_employee, get_request_role, role_required
from shipping.models import ShippingOrder

from .models import LogisticsTrip
from .routing_services import (
    can_resolve_clarification,
    can_return_for_clarification,
    resolve_clarification_and_return_to_logistics,
    return_to_manager_for_clarification,
)
from .trip_planning import build_trip_planning_context, change_trip_from_planning
from .external_trip import external_trip_list_rows
from .services import (
    build_logistics_dashboard_context,
    build_logistics_trip_detail_context,
    build_logistics_trip_list_context,
    build_logistics_trip_loading_context,
    get_trip_detail_trip,
    get_trip_loading_trip,
    handle_logistics_dashboard_post,
    handle_logistics_trip_detail_post,
    handle_logistics_trip_loading_post,
)


ALLOWED_ROLES = ("logistician", "manager", "head_manager", "director", "admin", "developer")
TRIP_READ_ROLES = ALLOWED_ROLES + ("storekeeper",)


@role_required(*ALLOWED_ROLES)
def logistics_dashboard(request):
    role = get_request_role(request)
    employee = get_employee_for_user(request.user)
    if request.method == "POST":
        response = handle_logistics_dashboard_post(request, role=role, employee=employee)
        if response is not None:
            return response
    context = build_logistics_dashboard_context(role=role, employee=employee)
    return render(request, "logistics/dashboard.html", context)


@role_required(*TRIP_READ_ROLES)
def logistics_help(request):
    """Инструкции по логистике внутри раздела «Рейсы»."""
    from teammanager.knowledge_base import LOGISTICS_ARTICLE
    from teammanager.roles import CABINET_ROLES

    role = get_request_role(request)
    context = {
        "role": role,
        "kb_article": LOGISTICS_ARTICLE,
        "active_nav": "trips",
        "trips_subnav": "help",
        "use_cabinet_shell": (role or "") in CABINET_ROLES,
    }
    return render(request, "logistics/help.html", context)


@role_required(*TRIP_READ_ROLES)
def logistics_trip_list(request):
    role = get_request_role(request)
    bucket = str(request.GET.get("bucket") or "").strip().lower()
    context = build_logistics_trip_list_context(role=role, bucket=bucket)
    context["trips"] = sorted(
        [*list(context.get("trips") or []), *external_trip_list_rows()],
        key=lambda row: (row["trip"].created_at, row["trip"].pk),
        reverse=True,
    )
    return render(request, "logistics/trip_list.html", context)


@role_required(*TRIP_READ_ROLES)
def logistics_trip_planning(request):
    role = get_request_role(request)
    context = build_trip_planning_context(
        role=role,
        week_value=str(request.GET.get("week") or "").strip(),
    )
    return render(request, "logistics/trip_planning.html", context)


@role_required(*ALLOWED_ROLES)
def logistics_trip_planning_action(request, pk: int):
    if request.method != "POST":
        return HttpResponseForbidden("Только POST")
    employee = get_request_employee(request) or get_employee_for_user(request.user)
    week = str(request.POST.get("week") or "").strip()
    redirect_url = reverse("logistics:trip-planning")
    if week:
        redirect_url = f"{redirect_url}?week={week}"
    try:
        change_trip_from_planning(
            trip_id=pk,
            action=str(request.POST.get("action") or ""),
            scheduled_at=str(request.POST.get("scheduled_at") or ""),
            reason=str(request.POST.get("reason") or ""),
            actor=employee,
            user=request.user,
        )
        if str(request.POST.get("action") or "").strip().lower() == "cancel":
            messages.success(request, "Рейс отменен в недельном планировании.")
        else:
            messages.success(request, "Рейс перенесен в недельном планировании.")
    except (LogisticsTrip.DoesNotExist, ValidationError, ValueError) as exc:
        message = "; ".join(getattr(exc, "messages", []) or [str(exc)])
        messages.error(request, message or "Не удалось изменить рейс.")
    return redirect(redirect_url)


@role_required(*TRIP_READ_ROLES)
def logistics_trip_detail(request, pk: int):
    role = get_request_role(request)
    employee = get_employee_for_user(request.user)
    trip = get_trip_detail_trip(pk)
    if trip.trip_kind == trip.KIND_EXTERNAL:
        return redirect("logistics:external-trip-detail", pk=trip.pk)
    if request.method == "POST":
        response = handle_logistics_trip_detail_post(request, role=role, trip=trip)
        if response is not None:
            return response
    context = build_logistics_trip_detail_context(role=role, trip=trip, employee=employee)
    return render(request, "logistics/trip_detail.html", context)


@role_required(*TRIP_READ_ROLES)
def logistics_trip_loading(request, pk: int):
    role = get_request_role(request)
    trip = get_trip_loading_trip(pk)
    if request.method == "POST":
        return handle_logistics_trip_loading_post(request, role=role, trip=trip)
    context = build_logistics_trip_loading_context(role=role, trip=trip)
    return render(request, "logistics/trip_loading.html", context)


@role_required(*ALLOWED_ROLES)
def shipping_return_clarify(request, order_id: int):
    if request.method != "POST":
        return HttpResponseForbidden("Только POST")
    order = get_object_or_404(ShippingOrder, pk=order_id)
    role = get_request_role(request)
    employee = get_request_employee(request) or get_employee_for_user(request.user)
    if not can_return_for_clarification(role, order):
        messages.error(request, "Нельзя вернуть эту заявку на уточнение.")
        return redirect(f"/shipping/{order.pk}/")
    try:
        return_to_manager_for_clarification(
            order=order,
            employee=employee,
            reason_code=str(request.POST.get("reason_code") or ""),
            reason_text=str(request.POST.get("reason_text") or ""),
            user=request.user,
        )
        messages.success(request, "Заявка возвращена менеджеру на уточнение.")
    except (PermissionError, ValueError) as exc:
        messages.error(request, str(exc))
    return redirect(f"/shipping/{order.pk}/")


@role_required(*ALLOWED_ROLES)
def shipping_resolve_clarify(request, order_id: int):
    if request.method != "POST":
        return HttpResponseForbidden("Только POST")
    order = get_object_or_404(ShippingOrder, pk=order_id)
    role = get_request_role(request)
    employee = get_request_employee(request) or get_employee_for_user(request.user)
    if not can_resolve_clarification(role, order):
        messages.error(request, "Нет активного уточнения или недостаточно прав.")
        return redirect(f"/shipping/{order.pk}/")
    try:
        resolve_clarification_and_return_to_logistics(
            order=order,
            employee=employee,
            comment=str(request.POST.get("comment") or ""),
            user=request.user,
        )
        messages.success(request, "Уточнение снято. Заявка снова у логиста.")
    except (PermissionError, ValueError) as exc:
        messages.error(request, str(exc))
    return redirect(f"/shipping/{order.pk}/")
