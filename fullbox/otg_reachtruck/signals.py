from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from reachtruck.models import MoveTask
from shipping.models import ShippingOrder
from sklad.models import WarehouseReserve


logger = logging.getLogger(__name__)
_FINAL_MOVE_STATUSES = {
    MoveTask.STATUS_DONE,
    MoveTask.STATUS_CANCELED,
    MoveTask.STATUS_FAILED,
}
_RELEASED_RESERVE_STATUSES = {
    WarehouseReserve.STATUS_SATISFIED,
    WarehouseReserve.STATUS_RELEASED,
    WarehouseReserve.STATUS_CANCELED,
}


def _schedule_waiting_otg_retry(agency_id: int | None) -> None:
    if not agency_id:
        return

    def _retry() -> None:
        try:
            from .services import retry_waiting_otg_shipping_requests

            retry_waiting_otg_shipping_requests(agency_id=agency_id, limit=5)
        except Exception:
            logger.exception(
                "Failed to schedule waiting OTG shipping requests",
                extra={"agency_id": agency_id},
            )

    transaction.on_commit(_retry)


@receiver(post_save, sender=MoveTask)
def retry_waiting_otg_after_move(sender, instance: MoveTask, **kwargs) -> None:
    if instance.status not in _FINAL_MOVE_STATUSES:
        return
    _schedule_waiting_otg_retry(instance.request.agency_id)


@receiver(post_save, sender=WarehouseReserve)
def retry_waiting_otg_after_reserve(sender, instance: WarehouseReserve, **kwargs) -> None:
    if (
        instance.reserve_type != WarehouseReserve.TYPE_SHIPPING
        or instance.status not in _RELEASED_RESERVE_STATUSES
    ):
        return
    _schedule_waiting_otg_retry(instance.agency_id)


_FINISHED_SHIPPING_STATUSES = {
    ShippingOrder.STATUS_SHIPPED,
    ShippingOrder.STATUS_PARTIAL,
    ShippingOrder.STATUS_CANCELED,
}


@receiver(post_save, sender=ShippingOrder)
def close_otg_requests_after_shipping_finished(sender, instance: ShippingOrder, **kwargs) -> None:
    """A finished order must not leave its OTG delivery requests open."""
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and "status" not in update_fields:
        return
    if instance.status not in _FINISHED_SHIPPING_STATUSES:
        return
    order_id = int(instance.pk or 0)
    if not order_id:
        return

    def _close() -> None:
        try:
            from .services import close_otg_delivery_requests_for_finished_order

            close_otg_delivery_requests_for_finished_order(order_id)
        except Exception:
            logger.exception(
                "Failed to close OTG delivery requests for finished shipping order",
                extra={"shipping_order_id": order_id},
            )

    transaction.on_commit(_close)
