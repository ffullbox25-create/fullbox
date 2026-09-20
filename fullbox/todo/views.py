import re
from datetime import timedelta

from django.contrib import messages
from django.db import transaction
from django.db.models import Prefetch
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.db.models import Q

from audit.models import OrderAuditEntry, log_order_action
from employees.models import Employee
from employees.access import get_request_employee, get_request_role, resolve_cabinet_url
from logistics.models import LogisticsTrip, LogisticsTripOrder, display_trip_number, is_draft_trip_number
from orders import views as order_views
from .attention import apply_task_attention_state, mark_task_viewed
from .forms import TaskAttachmentForm, TaskChecklistFormSet, TaskCommentForm, TaskForm
from .models import Task, TaskAttachment
from .services import (
    act_entry_from_entries as act_entry_from_entries_service,
    build_task_detail_state,
    build_task_detail_context,
    build_internal_task_detail_context,
    build_internal_task_list_context,
    build_task_list_context,
    build_task_list_queryset,
    build_trip_context as build_trip_context_service,
    can_access_task,
    can_create_receiving_act as can_create_receiving_act_service,
    handle_task_create,
    handle_task_delete,
    handle_task_detail_post,
    handle_internal_task_detail_post,
    handle_task_update,
    resolve_return_url as resolve_return_url_service,
    internal_task_can_create,
    internal_task_can_edit,
    save_checklist_formset,
    send_receiving_to_warehouse as send_receiving_to_warehouse_service,
    status_entry_from_list as status_entry_from_list_service,
)


_RECEIVING_ROUTE_RE = re.compile(r"/orders/receiving/([^/]+)/")
_LOGISTICS_TRIP_ROUTE_RE = re.compile(r"/logistics/trips/(\d+)/")


def _extract_receiving_order_id(route: str | None) -> str | None:
    if not route:
        return None
    match = _RECEIVING_ROUTE_RE.search(route)
    if not match:
        return None
    return match.group(1)


def _extract_logistics_trip_pk(route: str | None) -> int | None:
    if not route:
        return None
    match = _LOGISTICS_TRIP_ROUTE_RE.search(route)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _short_agency_name(value: str | None) -> str:
    return order_views._shorten_ip_name(value)


def _trip_status_label(trip: LogisticsTrip) -> str:
    if trip.status in {LogisticsTrip.STATUS_DRAFT, LogisticsTrip.STATUS_PLANNED} and is_draft_trip_number(trip.number):
        return "Черновик"
    return dict(LogisticsTrip.STATUS_CHOICES).get(trip.status, trip.get_status_display())


def _trip_public_number(trip: LogisticsTrip) -> str:
    if trip.status in {LogisticsTrip.STATUS_DRAFT, LogisticsTrip.STATUS_PLANNED} and is_draft_trip_number(trip.number):
        return "Черновик"
    return display_trip_number(trip.number)


def _trip_packing_payload_map(order_numbers: list[str]) -> dict[str, dict]:
    if not order_numbers:
        return {}
    result: dict[str, dict] = {}
    qs = (
        OrderAuditEntry.objects.filter(
            order_type="shipping",
            order_id__in=order_numbers,
            payload__act="shipping_packing",
        )
        .order_by("order_id", "-created_at")
    )
    for entry in qs:
        if entry.order_id in result:
            continue
        result[entry.order_id] = entry.payload or {}
    return result


def _build_trip_context(task: Task) -> dict | None:
    return build_trip_context_service(task)


def _send_receiving_to_warehouse(task, request) -> bool:
    return send_receiving_to_warehouse_service(task, request)


def _status_entry_from_list(entries):
    return status_entry_from_list_service(entries)


def _act_entry_from_entries(entries, act_type: str):
    return act_entry_from_entries_service(entries, act_type)


def _resolve_return_url(request) -> str:
    return resolve_return_url_service(request)


def _can_create_receiving_act(task) -> tuple[bool, str | None, list]:
    return can_create_receiving_act_service(task)




def task_list(request):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    role = get_request_role(request)
    if not role and not request.user.is_staff:
        return redirect("/")
    tasks = apply_task_attention_state(
        build_task_list_queryset(request=request, role=role),
        get_request_employee(request),
    )
    if str(request.GET.get("kind") or "").strip() == Task.KIND_WAREHOUSE_INTERNAL:
        return render(
            request,
            "todo/warehouse_task_list.html",
            build_internal_task_list_context(request=request, tasks=tasks),
        )
    return render(request, "todo/task_list.html", build_task_list_context(tasks))


def task_open(request, pk):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    task = get_object_or_404(Task, pk=pk)
    role = get_request_role(request)
    if not can_access_task(request=request, task=task, role=role):
        return redirect("/")

    mark_task_viewed(task, get_request_employee(request))
    if str(request.GET.get("mark") or "").strip() == "1":
        return HttpResponse(status=204)
    target = str(task.route or "").strip()
    if not target or not url_has_allowed_host_and_scheme(
        target,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        target = reverse("todo:detail", args=[task.pk])
    return redirect(target)


def task_panel_chunk(request):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")

    status = str(request.GET.get("status") or "").strip()
    if status not in {"backlog", "in_progress", "blocked", "done"}:
        return HttpResponseBadRequest("Неизвестная колонка")

    actual_role = get_request_role(request)
    requested_role = str(request.GET.get("role") or actual_role or "").strip()
    requested_all = requested_role in {"all", "__all__"}
    if requested_role == actual_role:
        role = actual_role
    elif requested_all and (actual_role == "head_manager" or request.user.is_superuser):
        role = "all"
    elif request.user.is_superuser:
        role = requested_role
    else:
        role = actual_role
    if not role and not request.user.is_staff:
        return HttpResponseForbidden("Доступ запрещен")

    try:
        offset = max(0, min(int(request.GET.get("offset") or 0), 10000))
        limit = max(1, min(int(request.GET.get("limit") or 10), 20))
    except (TypeError, ValueError):
        return HttpResponseBadRequest("Некорректная порция")

    include_created_by = str(request.GET.get("include_created_by") or "1").lower() not in {
        "0",
        "false",
        "no",
    }
    from .templatetags.todo_panel import task_panel

    payload = task_panel(
        {
            "request": request,
            "_task_panel_offsets": {status: offset},
            "_task_panel_only_status": status,
        },
        role=role,
        limit=limit,
        show_meta=False,
        include_created_by=include_created_by,
    )
    column = next(
        (item for item in payload.get("task_panel_columns", []) if item.get("status") == status),
        None,
    )
    if column is None:
        return HttpResponseBadRequest("Колонка не найдена")

    html = render_to_string(
        "todo/_task_cards.html",
        {
            "tasks": column.get("tasks") or [],
            "task_panel_role": payload.get("task_panel_role"),
            "task_panel_attention_employee_id": payload.get("task_panel_attention_employee_id"),
        },
        request=request,
    )
    loaded_count = int(column.get("loaded_count") or 0)
    return JsonResponse(
        {
            "html": html,
            "loaded_count": loaded_count,
            "next_offset": offset + loaded_count,
            "has_more": bool(column.get("has_more")),
        }
    )


def task_create(request):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    return_url = _resolve_return_url(request)
    task_kind = str(request.POST.get("kind") or request.GET.get("kind") or "").strip()
    if task_kind == Task.KIND_WAREHOUSE_INTERNAL:
        if not internal_task_can_create(request):
            return HttpResponseForbidden("Создавать внутренние задачи склада может только менеджер")
        task_instance = Task(kind=Task.KIND_WAREHOUSE_INTERNAL)
        if request.method == "POST":
            form = TaskForm(
                request.POST,
                instance=task_instance,
                task_kind=Task.KIND_WAREHOUSE_INTERNAL,
                request_employee=get_request_employee(request),
            )
            formset = TaskChecklistFormSet(
                request.POST,
                instance=task_instance,
                prefix="checklist",
            )
            if form.is_valid() and formset.is_valid():
                with transaction.atomic():
                    task = form.save(commit=False)
                    task.kind = Task.KIND_WAREHOUSE_INTERNAL
                    task.route = ""
                    task.status = "backlog"
                    task.created_by = request.user
                    task.save()
                    form.save_m2m()
                    formset.instance = task
                    save_checklist_formset(formset)
                messages.success(request, "Внутренняя задача склада создана")
                return redirect("todo:detail", pk=task.pk)
        else:
            form = TaskForm(
                instance=task_instance,
                task_kind=Task.KIND_WAREHOUSE_INTERNAL,
                request_employee=get_request_employee(request),
            )
            formset = TaskChecklistFormSet(instance=task_instance, prefix="checklist")
        return render(
            request,
            "todo/warehouse_task_form.html",
            {
                "form": form,
                "formset": formset,
                "title": "Новая задача складу",
                "return_url": return_url,
                "task_kind": Task.KIND_WAREHOUSE_INTERNAL,
            },
        )
    if request.method == "POST":
        response, form = handle_task_create(request)
        if response is not None:
            return response
    else:
        form = TaskForm()
    return render(
        request,
        "todo/task_form.html",
        {"form": form, "title": "Новая задача", "return_url": return_url},
    )


def task_update(request, pk):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    task = get_object_or_404(Task, pk=pk)
    return_url = _resolve_return_url(request)
    if task.kind == Task.KIND_WAREHOUSE_INTERNAL:
        if not internal_task_can_edit(request=request, task=task):
            messages.error(request, "Редактировать задачу может только постановщик")
            return redirect("todo:detail", pk=task.pk)
        if request.method == "POST":
            form = TaskForm(
                request.POST,
                instance=task,
                task_kind=Task.KIND_WAREHOUSE_INTERNAL,
                request_employee=get_request_employee(request),
            )
            formset = TaskChecklistFormSet(
                request.POST,
                instance=task,
                prefix="checklist",
            )
            if form.is_valid() and formset.is_valid():
                with transaction.atomic():
                    form.save()
                    save_checklist_formset(formset)
                messages.success(request, "Внутренняя задача склада обновлена")
                return redirect("todo:detail", pk=task.pk)
        else:
            form = TaskForm(
                instance=task,
                task_kind=Task.KIND_WAREHOUSE_INTERNAL,
                request_employee=get_request_employee(request),
            )
            formset = TaskChecklistFormSet(instance=task, prefix="checklist")
        return render(
            request,
            "todo/warehouse_task_form.html",
            {
                "form": form,
                "formset": formset,
                "title": f"Редактирование: {task.title}",
                "return_url": return_url,
                "task_kind": Task.KIND_WAREHOUSE_INTERNAL,
                "task": task,
            },
        )
    if not (request.user.is_authenticated and task.created_by_id == request.user.id):
        messages.error(request, "Редактировать задачу может только постановщик")
        return redirect("todo:detail", pk=task.pk)
    if request.method == "POST":
        response, form = handle_task_update(request, task)
        if response is not None:
            return response
    else:
        form = TaskForm(instance=task)
    return render(
        request,
        "todo/task_form.html",
        {
            "form": form,
            "title": f"Редактирование: {task.title}",
            "return_url": return_url,
        },
    )


def task_delete(request, pk):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    task = get_object_or_404(Task, pk=pk)
    if task.kind == Task.KIND_WAREHOUSE_INTERNAL and not can_access_task(
        request=request,
        task=task,
        role=get_request_role(request),
    ):
        return HttpResponseForbidden("Доступ запрещен")
    if request.method == "POST":
        return handle_task_delete(request, task)
    return render(request, "todo/task_delete_confirm.html", {"task": task})


def task_detail(request, pk):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    task = get_object_or_404(Task, pk=pk)
    role = get_request_role(request)
    if not can_access_task(request=request, task=task, role=role):
        return redirect("/")
    mark_task_viewed(task, get_request_employee(request))
    comment_form = TaskCommentForm()
    attachment_form = TaskAttachmentForm()

    if task.kind == Task.KIND_WAREHOUSE_INTERNAL:
        if request.method == "POST":
            response, comment_form, attachment_form = handle_internal_task_detail_post(
                request=request,
                task=task,
            )
            if response is not None:
                return response
        context = build_internal_task_detail_context(
            request=request,
            task=task,
            comment_form=comment_form,
            attachment_form=attachment_form,
        )
        return render(request, "todo/warehouse_task_detail.html", context)

    if request.method == "POST":
        detail_state = build_task_detail_state(request=request, task=task)
        response, comment_form, attachment_form = handle_task_detail_post(
            request=request,
            task=task,
            state=detail_state,
        )
        if response is not None:
            return response

    context = build_task_detail_context(
        request=request,
        task=task,
        comment_form=comment_form,
        attachment_form=attachment_form,
    )
    return render(request, "todo/task_detail.html", context)
