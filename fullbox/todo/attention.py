from django.urls import reverse
from django.utils import timezone

from .models import Task, TaskAttention


def task_recipient_ids(task: Task) -> set[int]:
    recipient_ids = {
        int(employee_id)
        for employee_id in (task.assigned_to_id, task.observer_id)
        if employee_id
    }
    if task.pk:
        recipient_ids.update(task.participants.values_list("id", flat=True))
    return recipient_ids


def reset_task_attention(task: Task, employee_ids) -> None:
    if task.status == "done":
        return
    delivered_at = timezone.now()
    for employee_id in {int(value) for value in employee_ids or [] if value}:
        TaskAttention.objects.update_or_create(
            task=task,
            employee_id=employee_id,
            defaults={
                "delivered_at": delivered_at,
                "viewed_at": None,
            },
        )


def remove_stale_task_attention(task: Task, recipient_ids=None) -> None:
    desired_ids = set(recipient_ids if recipient_ids is not None else task_recipient_ids(task))
    stale = TaskAttention.objects.filter(task=task)
    if desired_ids:
        stale = stale.exclude(employee_id__in=desired_ids)
    stale.delete()


def mark_task_viewed(task: Task, employee) -> bool:
    if not employee:
        return False
    return bool(
        TaskAttention.objects.filter(
            task=task,
            employee=employee,
            viewed_at__isnull=True,
        ).update(viewed_at=timezone.now(), updated_at=timezone.now())
    )


def unread_task_ids(employee, task_ids=None) -> set[int]:
    if not employee:
        return set()
    rows = TaskAttention.objects.filter(
        employee=employee,
        viewed_at__isnull=True,
    ).exclude(task__status="done")
    if task_ids is not None:
        rows = rows.filter(task_id__in=task_ids)
    return set(rows.values_list("task_id", flat=True))


def unread_task_count(employee) -> int:
    if not employee:
        return 0
    return (
        TaskAttention.objects.filter(
            employee=employee,
            viewed_at__isnull=True,
        )
        .exclude(task__status="done")
        .count()
    )


def apply_task_attention_state(tasks, employee):
    task_list = list(tasks)
    unread_ids = unread_task_ids(employee, [task.id for task in task_list])
    for task in task_list:
        task.is_unread_for_employee = task.id in unread_ids
        task.open_url = reverse("todo:open", args=[task.id])
        task.mark_view_url = f"{task.open_url}?mark=1"
    return task_list
