"""Взятие и передача задач без дубликатов заявки/уведомления."""

from __future__ import annotations

from datetime import date

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from employees.access import employee_has_role
from employees.models import Employee
from todo.models import Task

from .models import EmployeeCoverage, TaskHandoff
from .roles import CABINET_ROLES

REASSIGN_POWER_ROLES = {"head_manager", "director", "admin", "developer"}

TRANSFER_REASONS = (
    ("vacation", "Отпуск"),
    ("sick", "Болезнь"),
    ("workload", "Перераспределение нагрузки"),
    ("shift", "Смена смены / графика"),
    ("other", "Другое"),
)


def _snapshot(task: Task) -> dict:
    return {
        "status": task.status,
        "priority": task.priority,
        "assigned_to_id": task.assigned_to_id,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "route": task.route or "",
        "title": task.title or "",
    }


def models_Q_valid_to(day):
    return Q(valid_to__isnull=True) | Q(valid_to__gte=day)


def active_coverages_for(employee: Employee, *, on_date=None) -> list[EmployeeCoverage]:
    """Замещения, где employee — замещающий (видит задачи principal)."""
    if employee is None:
        return []
    day = on_date or timezone.localdate()
    qs = EmployeeCoverage.objects.filter(
        substitute=employee,
        is_active=True,
        valid_from__lte=day,
    ).filter(models_Q_valid_to(day))
    return list(qs.select_related("principal"))


def covered_principal_ids(employee: Employee) -> list[int]:
    return [c.principal_id for c in active_coverages_for(employee)]


def cabinet_colleagues(*, exclude_id: int | None = None) -> list[Employee]:
    qs = Employee.objects.filter(is_active=True, role__in=CABINET_ROLES).order_by("full_name", "id")
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    return list(qs[:200])


def can_reassign(*, author: Employee, task: Task) -> bool:
    if author is None or author.role not in CABINET_ROLES:
        return False
    if any(employee_has_role(author, role) for role in REASSIGN_POWER_ROLES):
        return True
    if not task.assigned_to_id or task.assigned_to_id == author.id:
        return True
    if task.assigned_to_id in set(covered_principal_ids(author)):
        return True
    return False


@transaction.atomic
def claim_task(*, task: Task, employee: Employee, reason: str = "", comment: str = "") -> TaskHandoff:
    if employee is None or employee.role not in CABINET_ROLES:
        raise PermissionError("Нет права взять задачу")
    if task.assigned_to_id and task.assigned_to_id != employee.id:
        raise ValueError("Задача уже назначена другому сотруднику")
    previous = task.assigned_to
    task.assigned_to = employee
    if task.status in {"backlog", "todo", "open", ""}:
        task.status = "in_progress"
    task.save(update_fields=["assigned_to", "status", "updated_at"])
    return TaskHandoff.objects.create(
        task=task,
        action=TaskHandoff.ACTION_CLAIM,
        from_employee=previous,
        to_employee=employee,
        author=employee,
        reason=reason or "Взята в работу",
        comment=comment,
        task_snapshot=_snapshot(task),
    )


@transaction.atomic
def transfer_task(
    *,
    task: Task,
    author: Employee,
    to_employee: Employee | None,
    reason: str = "",
    comment: str = "",
    return_to_queue: bool = False,
) -> TaskHandoff:
    if not can_reassign(author=author, task=task):
        raise PermissionError("Нет права передать эту задачу")
    previous = task.assigned_to
    if return_to_queue or to_employee is None:
        task.assigned_to = None
        action = TaskHandoff.ACTION_RETURN_QUEUE
        to_employee = None
    else:
        if to_employee.role not in CABINET_ROLES:
            raise ValueError("Получатель не из кабинета менеджера")
        task.assigned_to = to_employee
        action = TaskHandoff.ACTION_TRANSFER
    task.save(update_fields=["assigned_to", "updated_at"])
    return TaskHandoff.objects.create(
        task=task,
        action=action,
        from_employee=previous,
        to_employee=to_employee,
        author=author,
        reason=reason or ("Возврат в очередь" if action == TaskHandoff.ACTION_RETURN_QUEUE else "Передача"),
        comment=comment,
        task_snapshot=_snapshot(task),
    )


def can_manage_coverage(*, actor: Employee, principal: Employee) -> bool:
    if actor is None or actor.role not in CABINET_ROLES:
        return False
    if any(employee_has_role(actor, role) for role in REASSIGN_POWER_ROLES):
        return True
    return actor.id == principal.id


@transaction.atomic
def upsert_coverage(
    *,
    actor: Employee,
    principal: Employee,
    substitute: Employee,
    valid_from: date,
    valid_to: date | None = None,
    task_types: str = "",
    note: str = "",
    user=None,
) -> EmployeeCoverage:
    if not can_manage_coverage(actor=actor, principal=principal):
        raise PermissionError("Нет права настроить замещение")
    if substitute.id == principal.id:
        raise ValueError("Замещающий не может совпадать с основным сотрудником")
    if substitute.role not in CABINET_ROLES or principal.role not in CABINET_ROLES:
        raise ValueError("Оба сотрудника должны быть из кабинета менеджера")
    if valid_to and valid_to < valid_from:
        raise ValueError("Дата окончания раньше даты начала")
    return EmployeeCoverage.objects.create(
        principal=principal,
        substitute=substitute,
        valid_from=valid_from,
        valid_to=valid_to,
        task_types=(task_types or "").strip(),
        note=(note or "").strip()[:255],
        is_active=True,
        created_by=user,
    )


@transaction.atomic
def deactivate_coverage(*, actor: Employee, coverage: EmployeeCoverage) -> EmployeeCoverage:
    if not can_manage_coverage(actor=actor, principal=coverage.principal):
        raise PermissionError("Нет права отключить замещение")
    coverage.is_active = False
    coverage.save(update_fields=["is_active"])
    return coverage


@transaction.atomic
def bulk_transfer_tasks(
    *,
    author: Employee,
    from_employee: Employee,
    to_employee: Employee | None,
    reason: str = "",
    comment: str = "",
    return_to_queue: bool = False,
    task_types: str = "",
) -> dict:
    """
    Массовая передача активных задач (отпуск/болезнь).
    Не создаёт дубликаты Task — только меняет assigned_to + пишет TaskHandoff.
    """
    if author is None or author.role not in CABINET_ROLES:
        raise PermissionError("Нет права на массовую передачу")
    if not any(employee_has_role(author, role) for role in REASSIGN_POWER_ROLES) and author.id != from_employee.id:
        raise PermissionError("Можно передать только свои задачи или иметь право старшего менеджера")
    if not return_to_queue and to_employee is None:
        raise ValueError("Укажите получателя или возврат в очередь")
    if to_employee and to_employee.role not in CABINET_ROLES:
        raise ValueError("Получатель не из кабинета менеджера")

    qs = Task.objects.exclude(status="done").filter(assigned_to=from_employee)
    type_set = {p.strip() for p in (task_types or "").split(",") if p.strip()}
    tasks = list(qs.select_related("assigned_to")[:500])
    transferred = 0
    skipped = 0
    handoffs: list[TaskHandoff] = []
    reason_text = reason or "Массовая передача"
    for task in tasks:
        if type_set:
            route = (task.route or "").lower()
            matched = False
            if "receiving" in type_set and "/orders/receiving" in route:
                matched = True
            if "processing" in type_set and "/orders/processing" in route:
                matched = True
            if "shipping" in type_set and "/shipping/" in route:
                matched = True
            if "logistics" in type_set and "/logistics/" in route:
                matched = True
            if not matched:
                skipped += 1
                continue
        handoff = transfer_task(
            task=task,
            author=author,
            to_employee=to_employee,
            reason=reason_text,
            comment=comment,
            return_to_queue=return_to_queue,
        )
        handoffs.append(handoff)
        transferred += 1
    return {
        "transferred": transferred,
        "skipped": skipped,
        "handoff_ids": [h.id for h in handoffs],
    }


def list_coverages_for_settings(*, actor: Employee) -> list[EmployeeCoverage]:
    qs = EmployeeCoverage.objects.select_related("principal", "substitute").order_by("-valid_from", "-id")
    if any(employee_has_role(actor, role) for role in REASSIGN_POWER_ROLES):
        return list(qs[:100])
    return list(qs.filter(Q(principal=actor) | Q(substitute=actor))[:50])
