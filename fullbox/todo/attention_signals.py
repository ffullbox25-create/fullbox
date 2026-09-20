from django.db.models.signals import m2m_changed, post_save, pre_save
from django.dispatch import receiver

from .attention import remove_stale_task_attention, reset_task_attention, task_recipient_ids
from .models import Task


def _direct_recipient_ids(task: Task) -> set[int]:
    return {
        int(employee_id)
        for employee_id in (task.assigned_to_id, task.observer_id)
        if employee_id
    }


@receiver(pre_save, sender=Task)
def remember_task_attention_state(sender, instance, **kwargs):
    if not instance.pk:
        instance._attention_previous_direct_ids = set()
        instance._attention_previous_status = None
        return
    previous = Task.objects.filter(pk=instance.pk).values(
        "assigned_to_id",
        "observer_id",
        "status",
    ).first()
    if not previous:
        instance._attention_previous_direct_ids = set()
        instance._attention_previous_status = None
        return
    instance._attention_previous_direct_ids = {
        int(employee_id)
        for employee_id in (previous["assigned_to_id"], previous["observer_id"])
        if employee_id
    }
    instance._attention_previous_status = previous["status"]


@receiver(post_save, sender=Task)
def sync_task_attention_after_save(sender, instance, created, **kwargs):
    current_direct_ids = _direct_recipient_ids(instance)
    current_recipient_ids = task_recipient_ids(instance)
    previous_direct_ids = getattr(instance, "_attention_previous_direct_ids", set())
    previous_status = getattr(instance, "_attention_previous_status", None)

    reset_ids = set(current_recipient_ids) if created else current_direct_ids - previous_direct_ids
    if previous_status == "done" and instance.status != "done":
        reset_ids = set(current_recipient_ids)
    reset_task_attention(instance, reset_ids)
    remove_stale_task_attention(instance, current_recipient_ids)


@receiver(m2m_changed, sender=Task.participants.through)
def sync_task_attention_after_participant_change(sender, instance, action, pk_set, **kwargs):
    if action == "post_add":
        reset_task_attention(instance, pk_set or set())
        remove_stale_task_attention(instance)
    elif action in {"post_remove", "post_clear"}:
        remove_stale_task_attention(instance)
