from __future__ import annotations

from datetime import datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.db import transaction
from django.db.models import Q, Prefetch
from django.template.loader import render_to_string
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.generic import TemplateView

from employees.access import RoleRequiredMixin, get_request_employee
from reachtruck.models import MoveTask
from reachtruck.services.putaway_planner import putaway_location_scan_code
from shipping.models import ShippingOrder

from .assignments import assignment_snapshots, assignment_error, lock_assignment_queue, release_shipping_driver_assignment
from .models import OtgDeliveryRequest
from .services import _shipping_acceptance_times, _urgent_shipping_order_ids, refresh_otg_shipping_queue

from .execution import (
    build_otg_mobile_execution_snapshot,
    build_otg_mobile_request_execution_snapshot,
    otg_task_assignment_release_snapshot,
    release_stale_otg_move_request_task,
    report_otg_no_stock,
    scan_otg_move_request_step,
    take_otg_move_request,
    take_selected_otg_move_request_task,
)


ALLOWED_ROLES = (
    "reachtruck_driver",
    "super_car",
    "manager",
    "storekeeper",
    "processing_head",
    "head_manager",
    "director",
    "admin",
)

FINAL_TASK_STATUSES = {
    MoveTask.STATUS_DONE,
    MoveTask.STATUS_CANCELED,
    MoveTask.STATUS_FAILED,
}

ASSIGNMENT_MANAGER_ROLES = {
    "manager",
    "storekeeper",
    "processing_head",
    "head_manager",
    "director",
    "admin",
}

PRIORITY_RANK = {
    "urgent": 0,
    "high": 1,
    "normal": 2,
}
PRIORITY_LABELS = {
    "urgent": "Срочно",
    "high": "Высокий приоритет",
    "normal": "Обычный приоритет",
}


def _shipping_order_key(value) -> str:
    key = str(value or "").strip()
    if key.startswith("shipping:"):
        key = key.split(":", 1)[1].strip()
    return key


def _shipping_label(key: str) -> str:
    prefix, separator, suffix = key.rpartition("-")
    if separator and suffix.isdigit():
        return f"{int(suffix)}_OTG"
    return key


def _validation_error_message(exc: ValidationError) -> str:
    messages = getattr(exc, "messages", None)
    if messages:
        return "; ".join(str(message) for message in messages if str(message).strip())
    return str(exc)


def _age_label(value) -> str:
    if not value:
        return ""
    total_minutes = max(int((timezone.now() - value).total_seconds() // 60), 0)
    days, remainder = divmod(total_minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes or not parts:
        parts.append(f"{minutes} мин")
    return " ".join(parts[:2])


def _task_step_label(task: MoveTask, payload: dict) -> str:
    execution = dict(payload.get("mobile_execution") or {})
    if execution.get("destination_confirmed"):
        return "место OTG подтверждено"
    if not execution.get("pallet_confirmed"):
        return "ожидается скан паллеты"
    requested_boxes = int(payload.get("requested_box_count") or 0)
    scanned_boxes = len(execution.get("boxes_scanned") or [])
    if requested_boxes > scanned_boxes:
        return f"найдено коробов: {scanned_boxes} из {requested_boxes}"
    if execution.get("units_scanned"):
        return "идёт штучный отбор"
    return "ожидается скан OTG"


def _shipping_deadline(order: ShippingOrder | None, due_at):
    if due_at:
        return due_at
    if order and order.slot_date:
        slot_time = order.slot_time or time.max
        value = datetime.combine(order.slot_date, slot_time)
        return timezone.make_aware(value, timezone.get_current_timezone())
    if order and order.planned_ship_date:
        value = datetime.combine(order.planned_ship_date, time.max)
        return timezone.make_aware(value, timezone.get_current_timezone())
    return None


def _task_status(task: MoveTask, payload: dict | None = None) -> str:
    runtime_payload = dict(payload or task.payload or {})
    return str(runtime_payload.get("status") or task.status or "").strip().lower()


def _task_from_location(task: MoveTask, payload: dict) -> dict:
    location = payload.get("from_location")
    if isinstance(location, dict) and location:
        return dict(location)
    return {
        "zone": str(task.from_zone or "").strip(),
        "row": task.from_row,
        "section": task.from_section,
        "tier": task.from_tier,
        "cell": task.from_cell,
    }


def _route_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _route_location_group(location: dict, source_code: str) -> tuple[tuple, str]:
    zone = str(location.get("zone") or "").strip().upper()
    section = _route_int(location.get("section"))
    row = _route_int(location.get("row"))
    if zone == "OS":
        row_code = (
            source_code.split("/", 1)[0]
            if row and source_code and source_code.upper() != "OS"
            else ""
        )
        label = f"Ряд {row_code}" if row_code else "Основной склад"
    elif zone:
        label = f"Зона {zone}"
    else:
        label = "Место не указано"
    return (zone, section, row), label


def _route_sort_key(task: MoveTask) -> tuple:
    payload = dict(task.payload or {})
    location = _task_from_location(task, payload)
    return (
        str(location.get("zone") or "").strip().upper(),
        _route_int(location.get("section")),
        _route_int(location.get("row")),
        _route_int(location.get("tier")),
        _route_int(location.get("cell")),
        str(task.pallet_code or payload.get("pallet_code") or "").strip(),
        task.created_at,
        task.id,
    )


def _build_route_groups(
    tasks: list[MoveTask],
    *,
    can_choose: bool,
    can_manage_assignments: bool = False,
) -> list[dict]:
    sorted_tasks = sorted(tasks, key=_route_sort_key)
    groups: dict[tuple, dict] = {}
    stops: dict[tuple, dict] = {}
    for task in sorted_tasks:
        payload = dict(task.payload or {})
        status = _task_status(task, payload)
        if status in {MoveTask.STATUS_CANCELED, MoveTask.STATUS_FAILED}:
            continue
        location = _task_from_location(task, payload)
        source_code = str(
            payload.get("source_code")
            or putaway_location_scan_code(location)
            or ""
        ).strip()
        pallet_code = str(task.pallet_code or payload.get("pallet_code") or "").strip()
        group_key, group_label = _route_location_group(location, source_code)
        group = groups.setdefault(
            group_key,
            {
                "key": "|".join(str(value) for value in group_key),
                "label": group_label,
                "stops": [],
            },
        )
        stop_key = (group_key, pallet_code, source_code)
        stop = stops.get(stop_key)
        if stop is None:
            stop = {
                "pallet_code": pallet_code,
                "source_code": source_code,
                "subtask_count": 0,
                "completed_subtask_count": 0,
                "has_current": False,
                "selectable_task_id": "",
                "active_task_id": "",
                "assignee_name": "",
                "assignment_age_label": "",
                "can_release": False,
            }
            stops[stop_key] = stop
            group["stops"].append(stop)
        stop["subtask_count"] += 1
        legacy_order_id = str(task.legacy_order_id or "").strip()
        if status == MoveTask.STATUS_DONE:
            stop["completed_subtask_count"] += 1
        elif status == MoveTask.STATUS_IN_PROGRESS:
            stop["has_current"] = True
            if not stop["active_task_id"]:
                release_snapshot = otg_task_assignment_release_snapshot(task)
                stop["active_task_id"] = legacy_order_id
                stop["assignee_name"] = str(
                    task.assigned_to_name or payload.get("assigned_to_name") or ""
                ).strip()
                stop["assignment_age_label"] = _age_label(
                    task.started_at or task.updated_at
                )
                stop["can_release"] = bool(
                    can_manage_assignments and release_snapshot.get("can_release")
                )
        elif not stop["selectable_task_id"] and legacy_order_id:
            stop["selectable_task_id"] = legacy_order_id

    result = list(groups.values())
    for group in result:
        for stop in group["stops"]:
            if stop["has_current"]:
                stop["status_key"] = "current"
                stop["status_label"] = "Текущая"
            elif stop["completed_subtask_count"] >= stop["subtask_count"]:
                stop["status_key"] = "done"
                stop["status_label"] = "Выполнена"
            else:
                stop["status_key"] = "waiting"
                stop["status_label"] = "Ожидает"
            stop["can_choose"] = bool(
                can_choose
                and stop["status_key"] == "waiting"
                and stop["selectable_task_id"]
            )
    return result


def _active_otg_groups() -> dict[str, dict]:
    groups: dict[str, dict] = {}
    active_tasks = list(
        MoveTask.objects.select_related("request", "request__agency")
        .exclude(status__in=FINAL_TASK_STATUSES)
        .order_by("created_at", "id")
    )
    for task in active_tasks:
        payload = dict(task.payload or {})
        shipping_order = _shipping_order_key(payload.get("shipping_order_id"))
        legacy_order_id = str(task.legacy_order_id or "").strip()
        payload_status = _task_status(task, payload)
        if not shipping_order or not legacy_order_id or payload_status in FINAL_TASK_STATUSES:
            continue
        group = groups.setdefault(
            shipping_order,
            {
                "key": shipping_order,
                "label": _shipping_label(shipping_order),
                "client": str(getattr(getattr(task.request, "agency", None), "agn_name", "") or "").strip(),
                "legacy_order_ids": [],
                "tasks": [],
                "route_tasks": [],
                "request_ids": set(),
                "in_progress": False,
                "created_at": task.created_at,
                "priority": str(task.request.priority or "normal").strip().lower(),
                "due_at": task.request.due_at,
                "request_comment": str(task.request.comment or "").strip(),
                "current_assignees": set(),
                "current_steps": set(),
            },
        )
        group["legacy_order_ids"].append(legacy_order_id)
        group["tasks"].append(task)
        group["route_tasks"].append(task)
        group["request_ids"].add(task.request_id)
        group["created_at"] = min(group["created_at"], task.created_at)
        task_priority = str(task.request.priority or "normal").strip().lower()
        if PRIORITY_RANK.get(task_priority, 2) < PRIORITY_RANK.get(group["priority"], 2):
            group["priority"] = task_priority
        if task.request.due_at and (
            not group["due_at"] or task.request.due_at < group["due_at"]
        ):
            group["due_at"] = task.request.due_at
        if not group["request_comment"] and task.request.comment:
            group["request_comment"] = str(task.request.comment).strip()
        if payload_status == MoveTask.STATUS_IN_PROGRESS:
            group["in_progress"] = True
            assignee_name = str(
                task.assigned_to_name or payload.get("assigned_to_name") or ""
            ).strip()
            if assignee_name:
                group["current_assignees"].add(assignee_name)
            group["current_steps"].add(_task_step_label(task, payload))
    active_request_ids = {
        request_id
        for group in groups.values()
        for request_id in group["request_ids"]
    }
    completed_tasks = (
        MoveTask.objects.select_related("request", "request__agency")
        .filter(request_id__in=active_request_ids, status=MoveTask.STATUS_DONE)
        .order_by("created_at", "id")
    )
    for task in completed_tasks:
        payload = dict(task.payload or {})
        shipping_order = _shipping_order_key(payload.get("shipping_order_id"))
        group = groups.get(shipping_order)
        if (
            group
            and task.request_id in group["request_ids"]
            and _task_status(task, payload) == MoveTask.STATUS_DONE
        ):
            group["route_tasks"].append(task)
    orders = list(ShippingOrder.objects.filter(
        Q(number__in=list(groups)) | Q(status=ShippingOrder.STATUS_STOREKEEPER_ACCEPTED)
    ).select_related("agency").prefetch_related(Prefetch(
        "otg_delivery_requests",
        queryset=OtgDeliveryRequest.objects.filter(status=OtgDeliveryRequest.STATUS_BLOCKED)
        .order_by("-created_at", "-id"),
        to_attr="queue_waiting_requests",
    )))
    orders_by_number = {order.number: order for order in orders}
    acceptance_times = _shipping_acceptance_times(orders)
    urgent_order_ids = _urgent_shipping_order_ids(orders)
    for order in orders:
        if order.number in groups:
            continue
        waiting_request = next((row for row in order.queue_waiting_requests
                                if (row.payload or {}).get("waiting_for_stock")), None)
        needs_attention = not waiting_request and bool(order.queue_waiting_requests)
        if needs_attention:
            waiting_request = order.queue_waiting_requests[0]
        waiting_payload = dict(waiting_request.payload or {}) if waiting_request else {}
        reason = ((waiting_request.planning_error or waiting_payload.get("waiting_message"))
                  if waiting_request else "")
        groups[order.number] = {
            "key": order.number, "label": _shipping_label(order.number),
            "client": str(order.agency.agn_name or ""),
            "legacy_order_ids": [], "tasks": [], "route_tasks": [],
            "in_progress": False, "waiting": True, "needs_attention": needs_attention,
            "waiting_message": reason or "Заявка принята складом. Ожидает автоматического подбора паллет.",
            "created_at": order.created_at, "priority": "normal", "due_at": None,
            "request_comment": "", "current_assignees": set(), "current_steps": set(),
        }
    assignments = assignment_snapshots(orders, tasks_by_order={
        order.pk: groups.get(order.number, {}).get("route_tasks", []) for order in orders
    })
    now = timezone.now()
    for group in groups.values():
        group.pop("request_ids", None)
        group["task_count"] = len(group["legacy_order_ids"])
        group["pallet_count"] = len(
            {
                str(task.pallet_code or (task.payload or {}).get("pallet_code") or "").strip()
                for task in group["tasks"]
                if str(task.pallet_code or (task.payload or {}).get("pallet_code") or "").strip()
            }
        )
        order = orders_by_number.get(group["key"])
        group.setdefault("waiting", False)
        group["queued_at"] = acceptance_times.get(order.pk, group["created_at"]) if order else group["created_at"]
        if order and order.pk in urgent_order_ids:
            group["priority"] = "urgent"
        group["queue_sort_key"] = (
            group["priority"] != "urgent", group["queued_at"],
            order.pk if order else 0, group["key"],
        )
        deadline = _shipping_deadline(order, group["due_at"])
        group["assignment"] = assignments.get(order.pk, {}) if order else {}
        group["shipping_order"] = order
        group["deadline"] = deadline
        group["is_overdue"] = bool(deadline and deadline < now)
        group["age_label"] = _age_label(group["queued_at"])
        group["priority_label"] = PRIORITY_LABELS.get(
            group["priority"],
            PRIORITY_LABELS["normal"],
        )
        group["current_assignee_label"] = ", ".join(sorted(group.pop("current_assignees")))
        group["current_step_label"] = ", ".join(sorted(group.pop("current_steps")))
        group["comment"] = str(
            (order.comment if order else "") or group["request_comment"] or ""
        ).strip()
        group["sort_deadline"] = deadline or now + timedelta(days=36500)
    return groups



def _personalize_queue(groups, employee):
    employee_id = getattr(employee, "pk", None)
    rows = sorted(groups.values(), key=lambda item: item["queue_sort_key"])
    for row in rows:
        state = row.get("assignment") or {}
        row["is_mine"] = bool(employee_id and (state.get("employee_id") == employee_id or employee_id in state.get("employee_ids", [])))
        row["assignment_error"] = assignment_error(state, employee_id)
    own = next((row for row in rows if row["is_mine"]), None)
    for row in rows:
        row["can_take_order"] = not row["waiting"] and not row["assignment_error"] and (own is None or row["is_mine"])
        if own and not row["is_mine"] and not row["assignment_error"]:
            row["assignment_error"] = f"Сначала завершите свою заявку {own['label']} или передайте её через руководителя."
    return rows, own

def _request_context(request, **overrides) -> dict:
    employee = get_request_employee(request)
    employee_id = employee.id if employee else None
    selected_key = _shipping_order_key(
        overrides.get("request_key")
        or request.GET.get("request")
        or request.GET.get("mobile_request")
        or request.POST.get("request")
    )
    groups = _active_otg_groups()
    requests, own_request = _personalize_queue(groups, employee)
    selected = groups.get(selected_key)
    employee_role = str(getattr(employee, "role", "") or "").strip()
    can_manage_assignments = employee_role in ASSIGNMENT_MANAGER_ROLES
    context = {
        "requests": requests,
        "own_request": own_request,
        "queue_action_label": "Продолжить мою заявку" if own_request else "Взять следующую заявку",
        "request_notifications": [
            {
                "key": item["key"],
                "label": item["label"],
                "client": item["client"],
            }
            for item in requests
        ],
        "selected": selected,
        "request_key": selected_key,
        "error": overrides.get("error"),
        "ok_message": overrides.get("ok_message"),
        "completed": bool(request.GET.get("done")),
        "employee": employee,
        "can_manage_assignments": can_manage_assignments,
        "can_refresh_queue": employee_role in {"reachtruck_driver", "super_car"},
    }
    context["flash_state"] = (
        "error"
        if context["error"]
        else "success"
        if context["ok_message"] or context["completed"]
        else ""
    )
    if not selected or selected.get("waiting"):
        return context

    execution = build_otg_mobile_request_execution_snapshot(
        selected["legacy_order_ids"],
        employee_id=employee_id,
    )
    if selected.get("assignment_error") and not selected.get("is_mine"):
        execution["can_take"] = False
        execution["can_scan"] = False
    active_order_id = str(execution.get("active_order_id") or "").strip()
    active_task = next(
        (task for task in selected["tasks"] if str(task.legacy_order_id or "").strip() == active_order_id),
        None,
    )
    active_execution = build_otg_mobile_execution_snapshot(active_order_id) if active_order_id else {}
    active_payload = dict(getattr(active_task, "payload", {}) or {})
    source_code = str(
        active_execution.get("source_code") or active_payload.get("source_code") or ""
    ).strip()
    pallet_code = str(
        active_execution.get("pallet_code")
        or execution.get("active_pallet_code")
        or active_payload.get("pallet_code")
        or ""
    ).strip()
    current_step = str(execution.get("current_step") or active_execution.get("current_step") or "").strip()
    route_groups = _build_route_groups(
        list(selected.get("route_tasks") or []),
        can_choose=bool(execution.get("can_take") and not execution.get("can_scan")),
        can_manage_assignments=can_manage_assignments,
    )
    placeholders = {
        "pallet": "Сканируйте паллету",
        "boxes": "Сканируйте короб",
        "destination": "Сканируйте QR конкретного места OTG",
    }
    context.update(
        {
            "execution": execution,
            "active_execution": active_execution,
            "active_task": active_task,
            "current_step": current_step,
            "pallet_code": pallet_code,
            "source_code": source_code,
            "scan_placeholder": placeholders.get(current_step, "Сканируйте код"),
            "route_groups": route_groups,
            "route_stop_count": sum(len(group["stops"]) for group in route_groups),
        }
    )
    return context


@method_decorator(never_cache, name="dispatch")
class OtgReachtruckDashboardView(RoleRequiredMixin, TemplateView):
    template_name = "otg_reachtruck/dashboard.html"
    allowed_roles = ALLOWED_ROLES

    def get(self, request, *args, **kwargs):
        if str(request.GET.get("queue") or "").strip() == "1":
            groups = _active_otg_groups()
            queue_rows, own_request = _personalize_queue(groups, get_request_employee(request))
            response = JsonResponse(
                {
                    "requests": [
                        {
                            "key": item["key"],
                            "label": item["label"],
                            "client": item["client"],
                            "waiting": item["waiting"],
                        }
                        for item in queue_rows
                    ],
                    "queue_action_label": "Продолжить мою заявку" if own_request else "Взять следующую заявку",
                    "html": render_to_string("otg_reachtruck/_queue_cards.html", {"requests": queue_rows}),
                    "server_time": timezone.now().isoformat(),
                }
            )
            response["Cache-Control"] = "no-store"
            return response
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(_request_context(self.request, **kwargs))
        return context

    def post(self, request, *args, **kwargs):
        action = str(request.POST.get("action") or "").strip()
        if action == "refresh_queue":
            employee = get_request_employee(request)
            if not employee or employee.role not in {"reachtruck_driver", "super_car"}:
                return JsonResponse({"error": "Доступно только водителю складской техники."}, status=403)
            started = refresh_otg_shipping_queue()
            return JsonResponse({"started": started})
        if action not in {"take", "take_task", "take_next", "scan", "no_stock", "release_task", "release_order"}:
            return self.render_to_response(self.get_context_data(error="Неизвестное действие."), status=400)

        employee = get_request_employee(request)
        allowed_action_roles = (
            ASSIGNMENT_MANAGER_ROLES
            if action in {"release_task", "release_order"}
            else {"reachtruck_driver", "super_car"}
        )
        if not employee or employee.role not in allowed_action_roles:
            return self.render_to_response(
                self.get_context_data(
                    error=(
                        "Вернуть задание в очередь может только руководитель."
                        if action == "release_task"
                        else "Доступно только водителю складской техники."
                    )
                ),
                status=403,
            )

        if action == "take_next":
            with transaction.atomic():
                lock_assignment_queue()
                rows, own = _personalize_queue(_active_otg_groups(), employee)
                if own:
                    return redirect("/otg-reachtruck/?request=" + own["key"])
                candidates = [row for row in rows if row["can_take_order"]]
                if not candidates:
                    return self.render_to_response(self.get_context_data(ok_message="Свободных готовых заявок пока нет. Очередь обновляется автоматически."))
                first_error = ""
                for candidate in candidates:
                    result = take_otg_move_request(candidate["legacy_order_ids"], user=request.user,
                        employee_id=employee.pk, employee_name=employee.full_name)
                    if result.ok:
                        return redirect("/otg-reachtruck/?request=" + candidate["key"])
                    first_error = first_error or result.error
                return self.render_to_response(self.get_context_data(error=first_error), status=400)

        request_key = _shipping_order_key(request.POST.get("request"))
        group = _active_otg_groups().get(request_key)
        if not group:
            return self.render_to_response(self.get_context_data(error="Активная заявка ОТГ не найдена."), status=404)

        if action == "release_order":
            order = group.get("shipping_order")
            if order is None:
                return self.render_to_response(self.get_context_data(error="Отгрузка не найдена."), status=404)
            result = release_shipping_driver_assignment(order_id=order.pk, user=request.user,
                employee_id=employee.pk, employee_name=employee.full_name)
            return self.render_to_response(self.get_context_data(ok_message=result.message if result.ok else "", error=result.error if not result.ok else ""), status=200 if result.ok else 400)
        if group.get("waiting"):
            return self.render_to_response(self.get_context_data(error=group["waiting_message"]), status=409)
        try:
            if action == "take":
                result = take_otg_move_request(
                    group["legacy_order_ids"],
                    user=request.user,
                    employee_id=employee.id,
                    employee_name=employee.full_name,
                )
            elif action == "take_task":
                result = take_selected_otg_move_request_task(
                    group["legacy_order_ids"],
                    selected_legacy_order_id=str(request.POST.get("task") or "").strip(),
                    user=request.user,
                    employee_id=employee.id,
                    employee_name=employee.full_name,
                )
            elif action == "no_stock":
                execution = build_otg_mobile_request_execution_snapshot(
                    group["legacy_order_ids"],
                    employee_id=employee.id,
                )
                active_order_id = str(execution.get("active_order_id") or "").strip()
                if not active_order_id:
                    return self.render_to_response(
                        self.get_context_data(request_key=request_key, error="Нет активной паллеты для фиксации."),
                        status=400,
                    )
                result = report_otg_no_stock(
                    legacy_order_id=active_order_id,
                    user=request.user,
                    employee_id=employee.id,
                    employee_name=employee.full_name,
                )
            elif action == "release_task":
                result = release_stale_otg_move_request_task(
                    group["legacy_order_ids"],
                    selected_legacy_order_id=str(request.POST.get("task") or "").strip(),
                    user=request.user,
                    employee_id=employee.id,
                    employee_name=employee.full_name,
                )
            else:
                result = scan_otg_move_request_step(
                    group["legacy_order_ids"],
                    scan_value=str(request.POST.get("scan_value") or "").strip(),
                    user=request.user,
                    employee_id=employee.id,
                    employee_name=employee.full_name,
                )
        except ValidationError as exc:
            return self.render_to_response(
                self.get_context_data(request_key=request_key, error=_validation_error_message(exc)),
                status=400,
            )

        if not result.ok:
            return self.render_to_response(
                self.get_context_data(request_key=request_key, error=result.error),
                status=400,
            )
        if result.completed:
            return redirect("/otg-reachtruck/?done=1")
        return self.render_to_response(
            self.get_context_data(request_key=request_key, ok_message=result.message),
        )
