"""Versioned shipment ownership. Existing, unmarked shipments stay legacy.

The owner is a pick batch (one use of a cart), never the reusable cart barcode,
the workstation, or a controller flow that may not exist during prefetch.
Keep the marker in compatibility_key: marketplace_payload is replaced by API
readbacks and cannot safely store a local ownership policy.
"""
import re

from fbs.exceptions import FbsHandoverError
from fbs.models import FbsPickTask


TOTE_SHIPMENT_PREFIX = "tote-v1:"


def is_tote_shipment(batch) -> bool:
    return str(batch.compatibility_key or "").startswith(TOTE_SHIPMENT_PREFIX)


def shipment_pick_batch_id(batch) -> int | None:
    if not is_tote_shipment(batch):
        return None
    match = re.search(r":pick-batch:(\d+)(?:$|:)", batch.compatibility_key)
    return int(match.group(1)) if match else None


def order_pick_batch_id(order_id: int, *, requested_id=None) -> int:
    task = (
        FbsPickTask.objects.filter(order_id=order_id)
        .exclude(status=FbsPickTask.STATUS_CANCELED)
        .order_by("-id")
        .values("batch_id")
        .first()
    )
    if task is None:
        raise FbsHandoverError(
            "У заказа нет волны подбора: нельзя определить его тару. "
            "Передайте заказ оператору FBS для проверки волны."
        )
    if requested_id is not None and int(requested_id) != task["batch_id"]:
        raise FbsHandoverError(
            "Заказ относится к другой волне. Обновите экран и проверьте тару."
        )
    return task["batch_id"]


def assert_shipment_pick_batch(batch, pick_batch_id: int, *, error_type=FbsHandoverError):
    if is_tote_shipment(batch) and shipment_pick_batch_id(batch) != pick_batch_id:
        raise error_type(
            "Эта отгрузка закреплена за другой тарой/волной. "
            "Для новой тары нужен отдельный поток отгрузки; "
            "уже назначенные заказы не переносите без оператора FBS."
        )
