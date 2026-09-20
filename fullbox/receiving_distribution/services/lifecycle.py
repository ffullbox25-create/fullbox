"""Жизненный цикл плана распределения — без склада/ШК/резервов."""
from __future__ import annotations

import json
from typing import Any

from django.db import transaction
from django.utils import timezone

from ..models import (
    ReceivingDistributionAllocation,
    ReceivingDistributionDirection,
    ReceivingDistributionEvent,
    ReceivingDistributionPlan,
)


def _iter_flow_boxes(boxes: list | None, pallets: list | None):
    for box in boxes or []:
        if isinstance(box, dict):
            yield box
    for pallet in pallets or []:
        if not isinstance(pallet, dict):
            continue
        for box in pallet.get("boxes") or []:
            if isinstance(box, dict):
                yield box


def _parse_json_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []
    return []


@transaction.atomic
def on_receiving_confirmed_to_warehouse(*, order_id: str, user=None) -> bool:
    """
    Менеджер подтвердил головную приёмку → план «Ожидает приемки»,
    направления «Ожидает приемки». Связанные OTG остаются draft (без резерва).
    """
    plan = (
        ReceivingDistributionPlan.objects.select_for_update()
        .filter(receiving_order_id=str(order_id or "").strip())
        .first()
    )
    if not plan:
        return False

    plan.status = ReceivingDistributionPlan.STATUS_AWAITING_RECEIVING
    plan.save(update_fields=["status", "updated_at"])

    updated = plan.directions.exclude(status=ReceivingDistributionDirection.STATUS_CANCELLED).update(
        status=ReceivingDistributionDirection.STATUS_AWAITING,
        updated_at=timezone.now(),
    )

    ReceivingDistributionEvent.objects.create(
        plan=plan,
        action="confirmed_to_warehouse",
        user=user if getattr(user, "is_authenticated", False) else None,
        role="manager",
        object_type="plan",
        object_id=str(plan.id),
        new_value={"status": plan.status, "directions_updated": updated},
        source="orders.confirm_receiving_to_warehouse",
    )
    return True


@transaction.atomic
def sync_accepted_from_flow_boxes(
    *,
    receiving_order_id: str,
    boxes: list | str | None = None,
    pallets: list | str | None = None,
    user=None,
    source: str = "receiving_flow",
) -> dict[str, Any]:
    """
    Абсолютная синхронизация факта по направлениям из коробов flow.

    Короб должен иметь distribution_direction_id (проставляется в UI).
    Не создаёт короба/ШК/движения/резервы — только qty_accepted в плане.
    """
    plan = (
        ReceivingDistributionPlan.objects.select_for_update()
        .filter(receiving_order_id=str(receiving_order_id or "").strip())
        .prefetch_related("directions", "allocations__item")
        .first()
    )
    if not plan:
        return {"ok": True, "synced": False, "reason": "no_plan"}

    box_list = _parse_json_list(boxes)
    pallet_list = _parse_json_list(pallets)
    totals: dict[tuple[int, str], int] = {}
    for box in _iter_flow_boxes(box_list, pallet_list):
        try:
            direction_id = int(box.get("distribution_direction_id") or 0)
        except (TypeError, ValueError):
            direction_id = 0
        if direction_id <= 0:
            continue
        for item in box.get("items") or []:
            if not isinstance(item, dict):
                continue
            sku = str(item.get("sku_code") or item.get("sku") or "").strip().casefold()
            if not sku:
                continue
            try:
                qty = int(item.get("qty") or 0)
            except (TypeError, ValueError):
                qty = 0
            if qty <= 0:
                continue
            key = (direction_id, sku)
            totals[key] = totals.get(key, 0) + qty

    updated_rows = 0
    for alloc in ReceivingDistributionAllocation.objects.select_for_update().filter(plan=plan).select_related(
        "item", "direction"
    ):
        key = (int(alloc.direction_id), str(alloc.item.sku_code or "").casefold())
        new_val = min(int(totals.get(key, 0)), int(alloc.qty))
        if int(alloc.qty_accepted or 0) != new_val:
            alloc.qty_accepted = new_val
            alloc.save(update_fields=["qty_accepted"])
            updated_rows += 1

    for direction in plan.directions.select_for_update().all():
        if direction.status == ReceivingDistributionDirection.STATUS_CANCELLED:
            continue
        total_plan = 0
        total_acc = 0
        for row in direction.allocations.all():
            total_plan += int(row.qty)
            total_acc += int(row.qty_accepted or 0)
        if total_acc <= 0:
            next_status = ReceivingDistributionDirection.STATUS_AWAITING
        elif total_acc < total_plan:
            next_status = ReceivingDistributionDirection.STATUS_PARTIAL
        else:
            next_status = ReceivingDistributionDirection.STATUS_DONE
        if direction.status != next_status:
            direction.status = next_status
            direction.save(update_fields=["status", "updated_at"])

    all_dirs = list(plan.directions.exclude(status=ReceivingDistributionDirection.STATUS_CANCELLED))
    if all_dirs and all(d.status == ReceivingDistributionDirection.STATUS_DONE for d in all_dirs):
        plan.status = ReceivingDistributionPlan.STATUS_DONE
    elif any(
        d.status
        in {
            ReceivingDistributionDirection.STATUS_PARTIAL,
            ReceivingDistributionDirection.STATUS_DONE,
            ReceivingDistributionDirection.STATUS_RECEIVING,
        }
        for d in all_dirs
    ):
        plan.status = ReceivingDistributionPlan.STATUS_PARTIAL
    elif any(int(a.qty_accepted or 0) > 0 for a in plan.allocations.all()):
        plan.status = ReceivingDistributionPlan.STATUS_RECEIVING
    plan.save(update_fields=["status", "updated_at"])

    ReceivingDistributionEvent.objects.create(
        plan=plan,
        action="sync_from_flow",
        user=user if getattr(user, "is_authenticated", False) else None,
        role="storekeeper",
        object_type="plan",
        object_id=str(plan.id),
        new_value={"totals": {f"{d}:{s}": q for (d, s), q in totals.items()}, "updated_rows": updated_rows},
        source=source,
    )
    return {"ok": True, "synced": True, "updated_rows": updated_rows, "keys": len(totals)}


@transaction.atomic
def record_accepted_for_direction(
    *,
    receiving_order_id: str,
    direction_id: int,
    sku_code: str,
    qty: int,
    user=None,
    source: str = "warehouse_hook",
) -> dict:
    """
    Фиксация факта по направлению (связь принятого qty с планом).

    Не создаёт короба, ШК, движения и резервы — только прогресс распределения.
    Вызывать из существующей приёмки после успешной складской операции.
    """
    from django.core.exceptions import ValidationError

    if int(qty) <= 0:
        raise ValidationError("Количество должно быть > 0.")

    plan = (
        ReceivingDistributionPlan.objects.select_for_update()
        .filter(receiving_order_id=str(receiving_order_id or "").strip())
        .first()
    )
    if not plan:
        raise ValidationError("План распределения не найден.")

    direction = plan.directions.select_for_update().filter(pk=direction_id).first()
    if not direction or direction.status == ReceivingDistributionDirection.STATUS_CANCELLED:
        raise ValidationError("Направление не найдено или отменено.")

    alloc = (
        ReceivingDistributionAllocation.objects.select_for_update()
        .select_related("item")
        .filter(plan=plan, direction=direction, item__sku_code__iexact=str(sku_code or "").strip())
        .first()
    )
    if not alloc:
        raise ValidationError(f"Артикул {sku_code} не запланирован на это направление.")

    new_accepted = int(alloc.qty_accepted or 0) + int(qty)
    if new_accepted > int(alloc.qty):
        raise ValidationError(
            f"Нельзя принять больше плана: план {alloc.qty}, уже {alloc.qty_accepted}, запрос +{qty}."
        )

    old = int(alloc.qty_accepted or 0)
    alloc.qty_accepted = new_accepted
    alloc.save(update_fields=["qty_accepted"])

    # Статус направления по сумме
    total_plan = 0
    total_acc = 0
    for row in direction.allocations.all():
        total_plan += int(row.qty)
        total_acc += int(row.qty_accepted or 0)
    if total_acc <= 0:
        direction.status = ReceivingDistributionDirection.STATUS_AWAITING
    elif total_acc < total_plan:
        direction.status = ReceivingDistributionDirection.STATUS_PARTIAL
    else:
        direction.status = ReceivingDistributionDirection.STATUS_DONE
    direction.save(update_fields=["status", "updated_at"])

    # Статус головной приёмки
    all_dirs = list(plan.directions.exclude(status=ReceivingDistributionDirection.STATUS_CANCELLED))
    if all_dirs and all(d.status == ReceivingDistributionDirection.STATUS_DONE for d in all_dirs):
        plan.status = ReceivingDistributionPlan.STATUS_DONE
    elif any(
        d.status
        in {
            ReceivingDistributionDirection.STATUS_PARTIAL,
            ReceivingDistributionDirection.STATUS_DONE,
            ReceivingDistributionDirection.STATUS_RECEIVING,
        }
        for d in all_dirs
    ):
        plan.status = ReceivingDistributionPlan.STATUS_PARTIAL
    else:
        plan.status = ReceivingDistributionPlan.STATUS_RECEIVING
    plan.save(update_fields=["status", "updated_at"])

    ReceivingDistributionEvent.objects.create(
        plan=plan,
        action="accepted_qty",
        user=user if getattr(user, "is_authenticated", False) else None,
        role="storekeeper",
        object_type="direction",
        object_id=str(direction.id),
        old_value={"sku_code": sku_code, "qty_accepted": old},
        new_value={"sku_code": sku_code, "qty_accepted": new_accepted, "delta": int(qty)},
        source=source,
    )
    return {
        "direction_id": direction.id,
        "sku_code": sku_code,
        "qty_accepted": new_accepted,
        "qty_plan": int(alloc.qty),
        "direction_status": direction.status,
        "plan_status": plan.status,
        "shipping_order_id": direction.shipping_order_id,
        "shipping_number": direction.shipping_order.number if direction.shipping_order_id else "",
    }
