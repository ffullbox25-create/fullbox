from __future__ import annotations

from django.dispatch import receiver

from fbs.signals import (
    delivery_accepted,
    movement_completed,
    movement_warehouse_confirmed,
    order_billing_ready,
    storage_usage_captured,
)

from .fbs_services import (
    sync_fbs_delivery_to_billing,
    sync_fbs_movement_to_billing,
    sync_fbs_order_to_billing,
    sync_fbs_storage_usage_to_billing,
)
from .models import BillingStaffNotification
from .staff_notifications import notify_client_manager


@receiver(
    movement_warehouse_confirmed,
    dispatch_uid="billing.fbs.movement_warehouse_confirmed",
)
def notify_manager_about_fbs_movement(sender, *, request_row, user=None, **kwargs):
    confirmation_key = (
        request_row.warehouse_confirmed_at.isoformat()
        if request_row.warehouse_confirmed_at
        else "unknown"
    )
    return notify_client_manager(
        request_row.agency,
        kind=BillingStaffNotification.KIND_OTHER,
        title=f"FBS-перемещение {request_row.number} выполнено складом",
        message=(
            f"Кладовщик подтвердил {request_row.actual_moved_qty} шт. "
            "Заявка завершена автоматически; подтверждение менеджера не требуется."
        ),
        link_url=f"/team-manager/fbs/movements/{request_row.id}/",
        source_key=(
            f"fbs-movement:warehouse-confirmed:{request_row.id}:{confirmation_key}"
        ),
        actor=user,
    )


@receiver(movement_completed, dispatch_uid="billing.fbs.movement_completed")
def bill_completed_fbs_movement(sender, *, request_row, user=None, **kwargs):
    return sync_fbs_movement_to_billing(request_row=request_row, user=user)


@receiver(storage_usage_captured, dispatch_uid="billing.fbs.storage_usage_captured")
def bill_fbs_storage_usage(sender, *, agency, usage_date, **kwargs):
    return sync_fbs_storage_usage_to_billing(agency=agency, usage_date=usage_date)


@receiver(order_billing_ready, dispatch_uid="billing.fbs.order_billing_ready")
def bill_fbs_order(sender, *, order, user=None, **kwargs):
    return sync_fbs_order_to_billing(order=order, user=user)


@receiver(delivery_accepted, dispatch_uid="billing.fbs.delivery_accepted")
def bill_fbs_delivery(sender, *, batch, user=None, **kwargs):
    return sync_fbs_delivery_to_billing(batch=batch, user=user)
