from django.db.models.signals import post_save
from django.dispatch import receiver

from agent.realtime import on_commit_notify_print_job_available

from .models import ProcessingPrintJob


@receiver(post_save, sender=ProcessingPrintJob)
def notify_pending_print_job(sender, instance: ProcessingPrintJob, created: bool, **kwargs) -> None:
    if instance.status != ProcessingPrintJob.STATUS_PENDING:
        return
    update_fields = kwargs.get("update_fields")
    if not created and update_fields is not None and "status" not in update_fields:
        return
    on_commit_notify_print_job_available(instance)
