"""Resume durable OBR queues only after stock/claim transactions have committed."""
import logging
from django.db import connection, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from reachtruck.models import MoveTask, BoxClaim
from sklad.models import WarehouseStockSnapshot, WarehouseReserve
from .quantity_queue import draining, resume_agency
from audit.models import OrderAuditEntry

logger = logging.getLogger(__name__)


def schedule(agency_id):
    if not agency_id or draining.get():
        return
    for callback_record in connection.run_on_commit:
        if getattr(callback_record[1], "obr_queue_agency", None) == agency_id:
            return
    def run():
        try:
            resume_agency(agency_id)
        except Exception:
            # An OBR scheduling failure must not roll back an already completed
            # physical movement. The durable request remains pending for retry.
            logger.exception("OBR queue resume failed for agency %s", agency_id)
    run.obr_queue_agency = agency_id
    transaction.on_commit(run)


@receiver(post_save, sender=MoveTask, dispatch_uid="obr_queue_task_done")
def task_changed(sender, instance, raw=False, **kwargs):
    if not raw and instance.status in {MoveTask.STATUS_DONE, MoveTask.STATUS_CANCELED}:
        schedule(instance.request.agency_id)


@receiver(post_save, sender=BoxClaim, dispatch_uid="obr_queue_claim_released")
def claim_changed(sender, instance, raw=False, **kwargs):
    if not raw and instance.status != BoxClaim.STATUS_CLAIMED:
        schedule(instance.agency_id)


@receiver(post_save, sender=WarehouseStockSnapshot, dispatch_uid="obr_queue_stock_available")
def stock_changed(sender, instance, raw=False, **kwargs):
    # Shipping/archival can release a source box even when the changed row is
    # outside OS; the planner itself enforces eligible zones and availability.
    if not raw:
        schedule(instance.agency_id)


@receiver(post_save, sender=WarehouseReserve, dispatch_uid="obr_queue_reserve_changed")
def reserve_changed(sender, instance, raw=False, **kwargs):
    if not raw:
        schedule(instance.agency_id)


@receiver(post_save, sender=OrderAuditEntry, dispatch_uid="obr_queue_order_cancelled")
def order_changed(sender, instance, raw=False, **kwargs):
    if not raw and instance.order_type == "processing":
        from processing_app.stages import processing_is_cancelled
        if processing_is_cancelled(instance.payload):
            schedule(instance.agency_id)
