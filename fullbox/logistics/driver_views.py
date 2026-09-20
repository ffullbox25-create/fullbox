from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache

from employees.access import get_request_employee, get_request_role, role_required
from employees.models import Employee
from sku.models import Agency

from .driver_workflow import (
    DRIVER_ASSIGNMENT_CONFIRMED,
    DRIVER_ASSIGNMENT_PRELIMINARY,
    GLOBAL_PROBLEM_ROLES,
    MANAGER_ROLES,
    add_penalty,
    apply_problem_filters,
    assign_driver,
    can_manage_driver_assignment,
    change_driver_status,
    confirm_delivery,
    confirm_delivery_failure,
    driver_can_open_trip,
    driver_trip_queryset,
    filter_driver_trips,
    penalty_totals,
    problem_queryset,
    trip_summary,
    update_penalty,
    update_problem,
)
from .models import LogisticsTrip, ProblemTrip, TripNotification, TripPenalty
from .services import build_logistics_service_targets


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(exc.messages) if getattr(exc, "messages", None) else str(exc)


def _is_async(request) -> bool:
    return (request.headers.get("X-Requested-With") or "") == "XMLHttpRequest"


def _staff_notification_context(employee: Employee | None) -> dict:
    if employee is None:
        return {"trip_notifications": [], "trip_notifications_unread": 0}
    queryset = TripNotification.objects.filter(recipient=employee)
    return {
        "trip_notifications": list(queryset[:10]),
        "trip_notifications_unread": queryset.filter(read_at__isnull=True).count(),
    }


def _success_response(request, *, redirect_url: str, message: str):
    if _is_async(request):
        return JsonResponse({"ok": True, "message": message, "redirect_url": redirect_url})
    messages.success(request, message)
    return redirect(redirect_url)


def _error_response(request, *, redirect_url: str, message: str, status: int = 400):
    if _is_async(request):
        return JsonResponse({"ok": False, "message": message}, status=status)
    messages.error(request, message)
    return redirect(redirect_url)


@role_required("driver")
def driver_trip_list(request):
    role = get_request_role(request)
    employee = get_request_employee(request)
    bucket = str(request.GET.get("bucket") or "new").strip().lower()
    if bucket not in {"new", "today", "work", "completed", "problem"}:
        bucket = "new"
    queryset = filter_driver_trips(driver_trip_queryset(employee, role), bucket=bucket)
    rows = []
    for trip in queryset:
        summary = trip_summary(trip)
        rows.append({"trip": trip, "summary": summary})
    notifications = []
    unread_count = 0
    if employee:
        notifications = list(TripNotification.objects.filter(recipient=employee)[:12])
        unread_count = TripNotification.objects.filter(recipient=employee, read_at__isnull=True).count()
    return render(
        request,
        "logistics/driver_trip_list.html",
        {
            "role": role,
            "employee": employee,
            "rows": rows,
            "bucket": bucket,
            "notifications": notifications,
            "unread_count": unread_count,
        },
    )


@role_required("driver")
@never_cache
def driver_notifications(request):
    employee = get_request_employee(request)
    try:
        after_id = max(0, int(request.GET.get("after") or 0))
    except (TypeError, ValueError):
        after_id = 0
    if employee is None:
        return JsonResponse({"notifications": [], "last_id": after_id})
    queryset = (
        TripNotification.objects.filter(
            recipient=employee,
            read_at__isnull=True,
            pk__gt=after_id,
        )
        .filter(Q(source_key__contains=":assigned:") | Q(source_key__contains=":preliminary:"))
        .select_related("trip")
        .order_by("id")[:20]
    )
    rows = []
    last_id = after_id
    for notification in queryset:
        last_id = max(last_id, notification.pk)
        rows.append(
            {
                "id": notification.pk,
                "title": notification.title,
                "text": notification.text,
                "url": notification.detail_url,
                "trip_number": notification.trip.number,
                "kind": "preliminary" if ":preliminary:" in notification.source_key else "assigned",
                "created_at": timezone.localtime(notification.created_at).isoformat(),
            }
        )
    response = JsonResponse({"notifications": rows, "last_id": last_id})
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@role_required("driver")
def driver_trip_detail(request, pk: int):
    role = get_request_role(request)
    employee = get_request_employee(request)
    trip = get_object_or_404(
        LogisticsTrip.objects.select_related("assigned_driver", "assigned_logistician", "driver_assigned_by", "external_details__client")
        .prefetch_related("driver_history__actor", "driver_attachments", "orders__shipping_order__attachments"),
        pk=pk,
    )
    if not driver_can_open_trip(trip=trip, employee=employee, role=role):
        return HttpResponseForbidden("Этот рейс не назначен текущему водителю.")
    unread = TripNotification.objects.filter(recipient=employee, trip=trip, read_at__isnull=True) if employee else TripNotification.objects.none()
    unread_count = unread.count()
    if unread_count:
        unread.update(read_at=timezone.now())
        from .driver_workflow import log_trip_event

        log_trip_event(
            trip,
            event_type="notification_read",
            description=f"Водитель открыл уведомления рейса: {unread_count}.",
            user=request.user,
            payload={"count": unread_count},
        )
    if request.method == "POST":
        action = str(request.POST.get("action") or "").strip()
        try:
            if action in {"accept", "depart", "arrive"}:
                change_driver_status(trip=trip, employee=employee, role=role, action=action, user=request.user)
                labels = {
                    "accept": "Рейс принят.",
                    "depart": "Выезд зафиксирован.",
                    "arrive": "Прибытие в точку Б зафиксировано." if trip.trip_kind == LogisticsTrip.KIND_EXTERNAL else "Прибытие на склад зафиксировано.",
                }
                return _success_response(request, redirect_url=f"/logistics/driver/trips/{trip.pk}/", message=labels[action])
            if action == "deliver":
                confirm_delivery(request=request, trip=trip, employee=employee, role=role)
                return _success_response(
                    request,
                    redirect_url=f"/logistics/driver/trips/{trip.pk}/",
                    message="Сдача подтверждена. Рейс закрыт у логиста.",
                )
            if action == "fail":
                problem = confirm_delivery_failure(request=request, trip=trip, employee=employee, role=role)
                return _success_response(
                    request,
                    redirect_url=f"/logistics/driver/trips/{trip.pk}/",
                    message=f"Несдача сохранена. Создан проблемный рейс №{problem.pk}.",
                )
            raise ValidationError("Неизвестное действие рейса.")
        except (ValidationError, PermissionError) as exc:
            return _error_response(
                request,
                redirect_url=f"/logistics/driver/trips/{trip.pk}/",
                message=_validation_message(exc) if isinstance(exc, ValidationError) else str(exc),
                status=403 if isinstance(exc, PermissionError) else 400,
            )
        except Exception:
            return _error_response(
                request,
                redirect_url=f"/logistics/driver/trips/{trip.pk}/",
                message="Не удалось сохранить данные. Проверьте соединение и повторите отправку.",
                status=500,
            )
    trip.refresh_from_db()
    service_targets = build_logistics_service_targets(trip)
    if trip.driver_status == LogisticsTrip.DRIVER_STATUS_AWAITING_DELIVERY:
        from billing.warehouse_services import (
            facts_payload,
            list_agreed_services_for_storekeeper,
            list_facts,
        )

        clients = Agency.objects.in_bulk(
            [int(target["clientId"]) for target in service_targets if target.get("clientId")]
        )
        for target in service_targets:
            target_client = clients.get(int(target["clientId"]))
            if target_client is None:
                target["catalog"] = []
                target["facts"] = []
                continue
            target["catalog"] = list_agreed_services_for_storekeeper(
                target_client,
                process_type="logistics",
            )
            target["facts"] = facts_payload(
                list_facts(
                    client=target_client,
                    order_type="logistics",
                    order_id=trip.number,
                )
            )
    return render(
        request,
        "logistics/driver_trip_detail.html",
        {
            "role": role,
            "employee": employee,
            "trip": trip,
            "summary": trip_summary(trip),
            "failure_reasons": ProblemTrip.REASON_CHOICES,
            "party_choices": ProblemTrip.PARTY_CHOICES,
            "now_input": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            "history": trip.driver_history.select_related("actor").all(),
            "attachments": trip.driver_attachments.all(),
            "trip_service_targets": service_targets,
            "wsfm_order_type": "logistics",
            "wsfm_order_id": trip.number,
            "wsfm_client_id": "",
            "wsfm_source": "logistics_manual",
            "wsfm_title": "Фактически оказанные услуги рейса",
            "wsfm_items_label": "Заявок / направлений",
            "wsfm_cancel_label": "Вернуться к рейсу",
        },
    )


@role_required(*tuple(MANAGER_ROLES))
def trip_driver_assignment(request, pk: int):
    role = get_request_role(request)
    actor = get_request_employee(request)
    trip = get_object_or_404(
        LogisticsTrip.objects.select_related("assigned_driver", "assigned_logistician", "driver_assigned_by", "external_details__client")
        .prefetch_related("driver_history__actor", "driver_attachments", "orders__shipping_order__attachments"),
        pk=pk,
    )
    if not can_manage_driver_assignment(role):
        return HttpResponseForbidden("Доступ запрещен")
    if request.method == "POST":
        driver = Employee.objects.filter(pk=request.POST.get("driver_id"), role="driver", is_active=True).select_related("user").first()
        assignment_mode = str(request.POST.get("assignment_mode") or DRIVER_ASSIGNMENT_CONFIRMED).strip().lower()
        try:
            assign_driver(
                trip=trip,
                driver=driver,
                actor=actor,
                user=request.user,
                scheduled_at=str(request.POST.get("scheduled_at") or ""),
                assignment_mode=assignment_mode,
            )
            if assignment_mode == DRIVER_ASSIGNMENT_PRELIMINARY:
                messages.success(request, "Водитель предварительно поставлен на рейс и получил уведомление, что план еще не подтвержден.")
            else:
                messages.success(request, "Рейс подтвержден для водителя; водитель получил уведомление.")
        except ValidationError as exc:
            messages.error(request, _validation_message(exc))
        return redirect("logistics:trip-driver-assignment", pk=trip.pk)
    return render(
        request,
        "logistics/trip_driver_assignment.html",
        {
            "role": role,
            "active_nav": "trips",
            "trip": trip,
            "drivers": Employee.objects.filter(role="driver", is_active=True, user__isnull=False).select_related("user").order_by("full_name"),
            "summary": trip_summary(trip),
            "history": trip.driver_history.select_related("actor").all(),
            "attachments": trip.driver_attachments.all(),
            "scheduled_input": timezone.localtime(trip.scheduled_at).strftime("%Y-%m-%dT%H:%M") if trip.scheduled_at else "",
            **_staff_notification_context(actor),
        },
    )


@role_required(*tuple(MANAGER_ROLES))
def problem_trip_list(request):
    role = get_request_role(request)
    employee = get_request_employee(request)
    queryset = apply_problem_filters(problem_queryset(role=role, employee=employee), request.GET)
    rows = []
    for problem in queryset:
        summary = trip_summary(problem.trip)
        rows.append(
            {
                "problem": problem,
                "summary": summary,
                "totals": penalty_totals(problem),
                "first_order": summary["orders"][0] if summary["orders"] else None,
            }
        )
    return render(
        request,
        "logistics/problem_trip_list.html",
        {
            "role": role,
            "active_nav": "trips",
            "problems_active": True,
            "rows": rows,
            "filters": request.GET,
            "drivers": Employee.objects.filter(role="driver", is_active=True).order_by("full_name"),
            "failure_reasons": ProblemTrip.REASON_CHOICES,
            "party_choices": ProblemTrip.PARTY_CHOICES,
            "problem_statuses": ProblemTrip.STATUS_CHOICES,
            **_staff_notification_context(employee),
        },
    )


@role_required(*tuple(MANAGER_ROLES))
def problem_trip_detail(request, pk: int):
    role = get_request_role(request)
    employee = get_request_employee(request)
    problem = get_object_or_404(problem_queryset(role=role, employee=employee), pk=pk)
    if request.method == "POST":
        action = str(request.POST.get("action") or "").strip()
        try:
            if action == "update_problem":
                update_problem(problem=problem, request=request, employee=employee)
                messages.success(request, "Параметры разбирательства сохранены.")
            elif action == "add_penalty":
                add_penalty(problem=problem, request=request)
                messages.success(request, "Штраф добавлен.")
            elif action == "update_penalty":
                update_penalty(problem=problem, request=request)
                messages.success(request, "Статус штрафа сохранен.")
            else:
                raise ValidationError("Неизвестное действие.")
        except ValidationError as exc:
            messages.error(request, _validation_message(exc))
        return redirect("logistics:problem-trip-detail", pk=problem.pk)
    return render(
        request,
        "logistics/problem_trip_detail.html",
        {
            "role": role,
            "active_nav": "trips",
            "problems_active": True,
            "problem": problem,
            "trip": problem.trip,
            "summary": trip_summary(problem.trip),
            "totals": penalty_totals(problem),
            "employees": Employee.objects.filter(is_active=True, role__in=MANAGER_ROLES).order_by("full_name"),
            "party_choices": ProblemTrip.PARTY_CHOICES,
            "problem_statuses": ProblemTrip.STATUS_CHOICES,
            "penalty_statuses": TripPenalty.STATUS_CHOICES,
            "history": problem.trip.driver_history.select_related("actor").all(),
            "attachments": problem.trip.driver_attachments.all(),
            **_staff_notification_context(employee),
        },
    )
