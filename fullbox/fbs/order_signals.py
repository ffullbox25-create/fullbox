from __future__ import annotations

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import FbsOrder
from .order_audit import log_order_snapshot


TRACKED_FIELDS = (
    "internal_status",
    "marketplace_status",
    "marketplace_substatus",
    "cutoff_at",
)


@receiver(pre_save, sender=FbsOrder, dispatch_uid="fbs_order_audit_capture_previous")
def capture_previous_order_state(sender, instance, **kwargs):
    if not instance.pk:
        instance._fbs_audit_previous = {}
        return
    instance._fbs_audit_previous = (
        sender.objects.filter(pk=instance.pk).values(*TRACKED_FIELDS).first() or {}
    )


@receiver(post_save, sender=FbsOrder, dispatch_uid="fbs_order_audit_log_transition")
def log_order_transition(sender, instance, created, update_fields=None, **kwargs):
    if created:
        return
    previous = getattr(instance, "_fbs_audit_previous", {})
    changed_fields = update_fields or TRACKED_FIELDS
    log_order_snapshot(
        instance,
        changed_fields=changed_fields,
        previous=previous,
    )
