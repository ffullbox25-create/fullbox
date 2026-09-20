"""
Резерв связанных OTG после факта приёмки по направлению.

Использует существующие shipping/sklad пути:
- partial: WarehouseWritePathService.replace_shipping_reserves на принятое qty
- полное направление: shipping.services.reserve_order
Не дублирует складской учёт и не создаёт второй регистр.
"""
from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from shipping.models import ShippingOrder
from shipping.services import reserve_order

from ..models import (
    ReceivingDistributionDirection,
    ReceivingDistributionEvent,
    ReceivingDistributionPlan,
)


def _direction_accepted_map(direction: ReceivingDistributionDirection) -> dict[str, int]:
    return {
        str(alloc.item.sku_code or "").strip().casefold(): int(alloc.qty_accepted or 0)
        for alloc in direction.allocations.select_related("item").all()
        if alloc.item_id
    }


@transaction.atomic
def sync_linked_shipping_reserves(
    *,
    plan: ReceivingDistributionPlan | None = None,
    receiving_order_id: str = "",
    user=None,
    source: str = "receiving_distribution.reserve",
) -> list[dict[str, Any]]:
    """
    После фактической приёмки (обычно complete flow):
    - частично принятое направление → резерв на принятый объём;
    - полностью принятое → полный reserve_order + статус reserved/submitted.
    """
    if plan is None:
        plan = (
            ReceivingDistributionPlan.objects.select_for_update()
            .filter(receiving_order_id=str(receiving_order_id or "").strip())
            .first()
        )
    if not plan:
        return []

    from sklad.services.warehouse_write_path import WarehouseWritePathService

    results: list[dict[str, Any]] = []
    directions = (
        plan.directions.select_for_update()
        .select_related("shipping_order")
        .prefetch_related("allocations__item", "shipping_order__items")
        .exclude(kind=ReceivingDistributionDirection.KIND_STORAGE)
        .exclude(status=ReceivingDistributionDirection.STATUS_CANCELLED)
    )

    for direction in directions:
        order = direction.shipping_order
        if not order:
            continue
        if order.is_closed() or order.status in {
            ShippingOrder.STATUS_SHIPPED,
            ShippingOrder.STATUS_PARTIAL,
            ShippingOrder.STATUS_CANCELED,
            ShippingOrder.STATUS_PACKED,
            ShippingOrder.STATUS_PICKING,
        }:
            results.append(
                {
                    "direction_id": direction.id,
                    "shipping": order.number,
                    "ok": False,
                    "skipped": True,
                    "reason": f"status={order.status}",
                }
            )
            continue

        accepted_map = _direction_accepted_map(direction)
        total_accepted = sum(accepted_map.values())
        if total_accepted <= 0:
            results.append(
                {
                    "direction_id": direction.id,
                    "shipping": order.number,
                    "ok": True,
                    "skipped": True,
                    "reason": "nothing_accepted",
                }
            )
            continue

        try:
            if direction.status == ReceivingDistributionDirection.STATUS_DONE:
                outcome = _reserve_full_direction(order=order, user=user)
            else:
                outcome = _reserve_partial_accepted(
                    order=order,
                    accepted_map=accepted_map,
                    user=user,
                    write_path=WarehouseWritePathService,
                )
            if outcome.get("ok") and outcome.get("mode") == "full":
                # Полностью принято и зарезервировано → дальше палеты по текущей логике.
                direction.status = ReceivingDistributionDirection.STATUS_READY
                direction.save(update_fields=["status", "updated_at"])
            results.append({"direction_id": direction.id, "shipping": order.number, **outcome})
        except ValidationError as exc:
            results.append(
                {
                    "direction_id": direction.id,
                    "shipping": order.number,
                    "ok": False,
                    "error": "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc),
                }
            )
        except Exception as exc:  # noqa: BLE001 — не валим приёмку из‑за резерва
            results.append(
                {
                    "direction_id": direction.id,
                    "shipping": order.number,
                    "ok": False,
                    "error": str(exc),
                }
            )

    ReceivingDistributionEvent.objects.create(
        plan=plan,
        action="sync_linked_reserves",
        user=user if getattr(user, "is_authenticated", False) else None,
        role="storekeeper",
        object_type="plan",
        object_id=str(plan.id),
        new_value={"results": results},
        source=source,
    )
    return results


def _reserve_full_direction(*, order: ShippingOrder, user) -> dict[str, Any]:
    """Полный резерв существующим shipping.reserve_order."""
    if order.status == ShippingOrder.STATUS_DRAFT:
        order.status = ShippingOrder.STATUS_SUBMITTED
        order.save(update_fields=["status", "updated_at"])
    reserve_order(
        order,
        user,
        target_status=ShippingOrder.STATUS_RESERVED,
        log_description=(
            "Резерв после полной приёмки направления (приёмка с распределением)"
        ),
    )
    return {"ok": True, "mode": "full", "status": order.status}


def _reserve_partial_accepted(
    *,
    order: ShippingOrder,
    accepted_map: dict[str, int],
    user,
    write_path,
) -> dict[str, Any]:
    """Частичный резерв на принятый объём через WarehouseWritePathService.replace_shipping_reserves."""
    warehouse_items: list[dict] = []
    for item in order.items.select_for_update().all():
        key = str(item.sku_code or "").strip().casefold()
        qty = int(accepted_map.get(key, 0) or 0)
        item.qty_reserved = qty
        item.save(update_fields=["qty_reserved", "updated_at"])
        if qty <= 0:
            continue
        warehouse_items.append(
            {
                "sku": item.sku_code,
                "sku_code": item.sku_code,
                "size": item.size,
                "barcode": item.barcode,
                "goods_type": item.goods_type,
                "qty": qty,
                "box_codes": [],
                "reserve_pool": True,
                "reserve_box_plan_checked": False,
                "allow_partial_box_reserve": True,
            }
        )

    write_path.replace_shipping_reserves(
        agency=order.agency,
        order_id=order.number,
        items=warehouse_items,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        source_document_type="shipping_order",
        source_document_id=order.number,
    )
    if order.status == ShippingOrder.STATUS_DRAFT:
        order.status = ShippingOrder.STATUS_SUBMITTED
        order.reserved_at = timezone.now()
        order.save(update_fields=["status", "reserved_at", "updated_at"])
    elif order.reserved_at is None:
        order.reserved_at = timezone.now()
        order.save(update_fields=["reserved_at", "updated_at"])
    return {"ok": True, "mode": "partial", "status": order.status, "reserved_lines": len(warehouse_items)}
