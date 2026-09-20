from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import AgentCommand, AgentEvent
from .realtime import on_commit_notify_agent_command, on_commit_notify_agent_event


@receiver(post_save, sender=AgentCommand)
def notify_pending_agent_command(sender, instance: AgentCommand, created: bool, **kwargs) -> None:
    if instance.status != AgentCommand.STATUS_PENDING:
        return
    update_fields = kwargs.get("update_fields")
    if not created and update_fields is not None and "status" not in update_fields:
        return
    on_commit_notify_agent_command(instance)


@receiver(post_save, sender=AgentEvent)
def notify_agent_event(sender, instance: AgentEvent, created: bool, **kwargs) -> None:
    if not created:
        return
    on_commit_notify_agent_event(instance)
