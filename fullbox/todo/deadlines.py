from __future__ import annotations

import re
from collections.abc import Iterable

from django.db import transaction
from django.utils import timezone

from .models import Task, TaskComment, WarehouseRequestDeadlineChange


WAREHOUSE_EXECUTOR_ROLES = frozenset(
    {
        "storekeeper",
        "processing_head",
        "processing_worker",
        "packer",
        "picker",
        "reachtruck_driver",
    }
)
DEADLINE_MANAGER_ROLES = frozenset(
    {"manager", "head_manager", "director", "admin", "developer"}
)

_REQUEST_ROUTE_PATTERNS = (
    re.compile(r"^/orders/receiving/([^/]+)/"),
    re.compile(r"^/orders/processing/([^/]+)/"),
    re.compile(r"^/orders/other/([^/]+)/"),
    re.compile(r"^/shipping/(\d+)/"),
)


def canonical_warehouse_request_route(route: str | None) -> str:
    value = str(route or "").strip()
    for pattern in _REQUEST_ROUTE_PATTERNS:
        match = pattern.search(value)
        if match:
            return value[: match.end()]
    return ""


def is_warehouse_executor_task(task: Task) -> bool:
    role = str(getattr(getattr(task, "assigned_to", None), "role", "") or "")
    return role in WAREHOUSE_EXECUTOR_ROLES and bool(
        canonical_warehouse_request_route(getattr(task, "route", ""))
    )


def attach_warehouse_deadline_context(tasks: Iterable[Task]) -> None:
    task_list = list(tasks)
    routes = {
        canonical_warehouse_request_route(getattr(task, "route", ""))
        for task in task_list
        if is_warehouse_executor_task(task)
    }
    routes.discard("")
    if not routes:
        return

    latest_by_route = {}
    counts_by_route = {}
    for change in WarehouseRequestDeadlineChange.objects.filter(
        request_route__in=routes
    ).select_related("changed_by").order_by("request_route", "-created_at", "-id"):
        counts_by_route[change.request_route] = counts_by_route.get(change.request_route, 0) + 1
        latest_by_route.setdefault(change.request_route, change)

    for task in task_list:
        route = canonical_warehouse_request_route(getattr(task, "route", ""))
        change = latest_by_route.get(route)
        task.warehouse_request_route = route
        task.warehouse_deadline_change = change
        task.warehouse_deadline_change_count = counts_by_route.get(route, 0)
        task.effective_due_date = change.due_date if change else task.due_date


def latest_deadline_change_for_task(task: Task):
    route = canonical_warehouse_request_route(getattr(task, "route", ""))
    if not route:
        return None
    return (
        WarehouseRequestDeadlineChange.objects.filter(request_route=route)
        .select_related("changed_by")
        .order_by("-created_at", "-id")
        .first()
    )


def apply_latest_deadline_to_task(task: Task) -> bool:
    if not is_warehouse_executor_task(task):
        return False
    change = latest_deadline_change_for_task(task)
    if not change or task.due_date == change.due_date:
        return False
    Task.objects.filter(pk=task.pk).update(due_date=change.due_date)
    task.due_date = change.due_date
    return True


def reschedule_warehouse_request(
    *,
    anchor_task: Task,
    due_date,
    reason: str,
    changed_by,
    user=None,
) -> WarehouseRequestDeadlineChange:
    normalized_reason = " ".join(str(reason or "").split())
    if len(normalized_reason) < 5:
        raise ValueError("Укажите причину изменения срока")
    if not due_date:
        raise ValueError("Укажите новый срок")
    if timezone.is_naive(due_date):
        due_date = timezone.make_aware(due_date, timezone.get_current_timezone())
    due_date = timezone.localtime(due_date).replace(second=0, microsecond=0)
    if due_date <= timezone.localtime().replace(second=0, microsecond=0):
        raise ValueError("Новый срок должен быть в будущем")

    with transaction.atomic():
        locked_anchor = (
            Task.objects.select_for_update()
            .get(pk=anchor_task.pk)
        )
        route = canonical_warehouse_request_route(locked_anchor.route)
        if not route or not is_warehouse_executor_task(locked_anchor):
            raise ValueError("Активная задача склада для заявки не найдена")

        warehouse_tasks = list(
            Task.objects.select_for_update(of=("self",))
            .filter(
                route__startswith=route,
                assigned_to__role__in=WAREHOUSE_EXECUTOR_ROLES,
            )
            .exclude(status="done")
            .order_by("id")
        )
        if not warehouse_tasks:
            raise ValueError("У заявки нет активных задач склада")

        latest = (
            WarehouseRequestDeadlineChange.objects.select_for_update()
            .filter(request_route=route)
            .order_by("-created_at", "-id")
            .first()
        )
        previous_due_date = latest.due_date if latest else max(
            task.due_date for task in warehouse_tasks if task.due_date
        )
        previous_due_date = timezone.localtime(previous_due_date).replace(
            second=0,
            microsecond=0,
        )
        if previous_due_date == due_date:
            raise ValueError("Новый срок совпадает с текущим")

        change = WarehouseRequestDeadlineChange.objects.create(
            request_route=route,
            previous_due_date=previous_due_date,
            due_date=due_date,
            reason=normalized_reason,
            changed_by=changed_by,
        )
        task_ids = [task.id for task in warehouse_tasks]
        Task.objects.filter(id__in=task_ids).update(due_date=due_date)

        if user and getattr(user, "is_authenticated", False):
            old_label = previous_due_date.strftime("%d.%m.%Y %H:%M")
            new_label = due_date.strftime("%d.%m.%Y %H:%M")
            TaskComment.objects.bulk_create(
                [
                    TaskComment(
                        task_id=task_id,
                        author=user,
                        body=(
                            f"Срок склада изменён: {old_label} → {new_label}. "
                            f"Причина: {normalized_reason}"
                        ),
                    )
                    for task_id in task_ids
                ]
            )
        return change
