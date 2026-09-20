from __future__ import annotations

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from .models import LogisticsTrip, TripAttachment, TripHistory, TripNotification


@receiver(pre_save, sender=LogisticsTrip)
def remember_driver_trip_state(sender, instance, **kwargs):
    if not instance.pk:
        instance._driver_previous_state = None
        return
    instance._driver_previous_state = (
        sender.objects.filter(pk=instance.pk)
        .values("status", "trip_date", "scheduled_at", "assigned_driver_id")
        .first()
    )


@receiver(post_save, sender=LogisticsTrip)
def notify_driver_about_external_trip_changes(sender, instance, created, **kwargs):
    previous = getattr(instance, "_driver_previous_state", None)
    if created or not previous or not instance.assigned_driver_id:
        return
    same_driver = previous.get("assigned_driver_id") == instance.assigned_driver_id
    if not same_driver:
        return
    if previous.get("status") != LogisticsTrip.STATUS_CANCELED and instance.status == LogisticsTrip.STATUS_CANCELED:
        TripNotification.objects.create(
            recipient=instance.assigned_driver,
            trip=instance,
            title=f"Рейс {instance.number} отменен",
            text="Логист отменил рейс. Выполнение больше не требуется.",
            detail_url="/logistics/driver/trips/",
            source_key=f"trip:{instance.pk}:canceled:{instance.updated_at.timestamp()}",
        )
        TripHistory.objects.create(
            trip=instance,
            event_type="trip_canceled",
            description="Рейс отменен; водитель уведомлен.",
            payload={"status": instance.status},
        )
        return
    schedule_changed = previous.get("scheduled_at") != instance.scheduled_at or previous.get("trip_date") != instance.trip_date
    if schedule_changed:
        TripNotification.objects.create(
            recipient=instance.assigned_driver,
            trip=instance,
            title=f"Изменено время рейса {instance.number}",
            text="Проверьте обновленные дату и время подачи в карточке рейса.",
            detail_url=f"/logistics/driver/trips/{instance.pk}/",
            source_key=f"trip:{instance.pk}:schedule:{instance.updated_at.timestamp()}",
        )
        TripHistory.objects.create(
            trip=instance,
            event_type="schedule_changed",
            description="Изменены дата или время рейса; водитель уведомлен.",
            payload={
                "previous_trip_date": str(previous.get("trip_date") or ""),
                "trip_date": str(instance.trip_date or ""),
                "previous_scheduled_at": previous.get("scheduled_at").isoformat() if previous.get("scheduled_at") else "",
                "scheduled_at": instance.scheduled_at.isoformat() if instance.scheduled_at else "",
            },
        )


@receiver(post_save, sender=TripAttachment)
def log_trip_attachment_uploaded(sender, instance, created, **kwargs):
    if not created:
        return
    TripHistory.objects.create(
        trip=instance.trip,
        event_type="file_uploaded",
        description=f"Загружен файл: {instance.get_attachment_type_display()}.",
        actor=instance.uploaded_by,
        payload={"attachment_id": instance.pk, "attachment_type": instance.attachment_type},
    )


@receiver(post_delete, sender=TripAttachment)
def log_trip_attachment_deleted(sender, instance, **kwargs):
    TripHistory.objects.create(
        trip=instance.trip,
        event_type="file_deleted",
        description=f"Удален файл: {instance.get_attachment_type_display()}.",
        actor=instance.uploaded_by,
        payload={"attachment_id": instance.pk, "attachment_type": instance.attachment_type},
    )
