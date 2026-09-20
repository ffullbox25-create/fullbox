from __future__ import annotations

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from fbs.models import FbsPickingCart, FbsWorkstation

from wms_new.models import (
    WmsNewBox,
    WmsNewBoxItem,
    WmsNewEvent,
    WmsNewOrder,
    WmsNewProduct,
    WmsNewWave,
    WmsNewWaveAllocation,
    WmsNewWaveOrder,
    WmsNewWavePickLine,
)
from wms_new.services.settings import system_section_settings


class PickingOperationError(ValueError):
    pass


FBS_PICKING_DEFAULTS = {
    "scan_each_unit": "yes",
    "strict_collection": "no",
    "insufficient_stock_wave": "warn",
}


def _event(entity_type, entity_id, action, actor=None, before=None, after=None, metadata=None):
    WmsNewEvent.objects.create(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor=actor,
        before=before or {},
        after=after or {},
        metadata=metadata or {},
    )


def _match_box_items(order_item):
    match = Q(pk__in=[])
    if order_item.sku_id:
        match |= Q(product__source_sku_id=order_item.sku_id)
    if order_item.barcode:
        match |= Q(barcode__iexact=order_item.barcode)
        match |= Q(product__barcode__iexact=order_item.barcode)
    if order_item.external_sku:
        match |= Q(sku_code__iexact=order_item.external_sku)
        match |= Q(product__article__iexact=order_item.external_sku)
    return (
        WmsNewBoxItem.objects.select_for_update(of=("self",))
        .select_related("box__location", "product")
        .filter(
            match,
            agency_id=order_item.order.agency_id,
            box__status=WmsNewBox.STATUS_ACTIVE,
            available_qty__gt=0,
        )
        .order_by(
            "box__location__warehouse_code",
            "box__location__zone_code",
            "box__location__row_no",
            "box__location__section_no",
            "box__location__tier_no",
            "box__location__cell_no",
            "box__location_code",
            "box__code",
            "id",
        )
    )


def _location_values(box_item: WmsNewBoxItem) -> tuple[str, str, dict]:
    box = box_item.box
    location = box.location
    if location:
        code = location.location_code or box.location_code or location.display_name or box.code
        name = location.display_name or location.location_code or box.location_code or box.code
        route = {
            "warehouse": location.warehouse_code,
            "zone": location.zone_code,
            "row": location.row_no,
            "section": location.section_no,
            "tier": location.tier_no,
            "cell": location.cell_no,
        }
        return str(code), str(name), route
    code = box.location_code or box.code
    return str(code), str(code), {"zone": box.zone_code}


def _change_reserved_stock(box_item: WmsNewBoxItem, quantity: int) -> None:
    if quantity <= 0 or quantity > box_item.available_qty:
        raise PickingOperationError("Остаток FBS-NEW изменился. Пересоздайте волну.")
    box_item.available_qty -= quantity
    box_item.reserved_qty += quantity
    box_item.pilot_revision += 1
    box_item.save(
        update_fields=("available_qty", "reserved_qty", "pilot_revision", "updated_at")
    )
    box = WmsNewBox.objects.select_for_update().get(pk=box_item.box_id)
    box.stock_free = max(0, box.stock_free - quantity)
    box.reserved_qty += quantity
    box.pilot_revision += 1
    box.save(update_fields=("stock_free", "reserved_qty", "pilot_revision", "updated_at"))
    if box_item.product_id:
        product = WmsNewProduct.objects.select_for_update().get(pk=box_item.product_id)
        product.stock_free = max(0, product.stock_free - quantity)
        product.internal_reserved += quantity
        product.pilot_revision += 1
        product.save(
            update_fields=("stock_free", "internal_reserved", "pilot_revision", "updated_at")
        )


def _release_allocation(
    allocation: WmsNewWaveAllocation,
    *,
    status: str = WmsNewWaveAllocation.STATUS_RELEASED,
) -> int:
    remaining = max(0, allocation.reserved_quantity - allocation.picked_quantity)
    if remaining and allocation.box_item_id:
        box_item = WmsNewBoxItem.objects.select_for_update().get(pk=allocation.box_item_id)
        released = min(remaining, box_item.reserved_qty)
        box_item.reserved_qty -= released
        box_item.available_qty += released
        box_item.pilot_revision += 1
        box_item.save(
            update_fields=("reserved_qty", "available_qty", "pilot_revision", "updated_at")
        )
        box = WmsNewBox.objects.select_for_update().get(pk=box_item.box_id)
        box.reserved_qty = max(0, box.reserved_qty - released)
        box.stock_free += released
        box.pilot_revision += 1
        box.save(update_fields=("reserved_qty", "stock_free", "pilot_revision", "updated_at"))
        if box_item.product_id:
            product = WmsNewProduct.objects.select_for_update().get(pk=box_item.product_id)
            product.internal_reserved = max(0, product.internal_reserved - released)
            product.stock_free += released
            product.pilot_revision += 1
            product.save(
                update_fields=("internal_reserved", "stock_free", "pilot_revision", "updated_at")
            )
    allocation.status = status
    allocation.completed_at = timezone.now()
    allocation.save(update_fields=("status", "completed_at", "updated_at"))
    return remaining


def allocate_orders_to_wave(
    *, wave: WmsNewWave, orders: list[WmsNewOrder], actor=None
) -> dict:
    """Reserve an independent stock route and attach processable orders to a new wave."""

    settings_values = system_section_settings("fbs", FBS_PICKING_DEFAULTS)
    shortage_mode = str(settings_values.get("insufficient_stock_wave") or "warn")
    warn_on_shortage = shortage_mode != "deny"
    result = {
        "selected_orders_count": len(orders),
        "processed_orders_count": 0,
        "orders_with_one_good_count": 0,
        "orders_with_many_good_count": 0,
        "potentially_short_orders_count": 0,
        "orders_not_added_in_wave_count": 0,
        "problematic_orders": [],
    }
    route_sequence = 0
    order_sequence = 0
    for order in orders:
        if WmsNewWaveOrder.objects.filter(
            order=order,
            wave__status__in=(
                WmsNewWave.STATUS_QUEUED,
                WmsNewWave.STATUS_IN_PROGRESS,
                WmsNewWave.STATUS_VERIFICATION,
            ),
        ).exists():
            result["orders_not_added_in_wave_count"] += 1
            result["problematic_orders"].append(
                {"order": order.external_order_id, "reason": "Заказ уже находится в активной волне."}
            )
            continue

        created_lines: list[WmsNewWavePickLine] = []
        missing_total = 0
        for order_item in order.items.select_related("order").order_by("id"):
            planned = max(1, int(order_item.quantity or 1))
            candidates = list(_match_box_items(order_item))
            product = next((item.product for item in candidates if item.product_id), None)
            if product is None and order_item.sku_id:
                product = WmsNewProduct.objects.filter(source_sku_id=order_item.sku_id).first()
            line = WmsNewWavePickLine.objects.create(
                wave=wave,
                order=order,
                order_item=order_item,
                product=product,
                planned_quantity=planned,
                sequence=route_sequence + 1,
                source_snapshot={"required": planned},
            )
            created_lines.append(line)
            remaining = planned
            for box_item in candidates:
                take = min(remaining, int(box_item.available_qty or 0))
                if take <= 0:
                    continue
                route_sequence += 1
                place_code, place_name, route = _location_values(box_item)
                _change_reserved_stock(box_item, take)
                WmsNewWaveAllocation.objects.create(
                    pick_line=line,
                    box_item=box_item,
                    source_box_code=box_item.box.code,
                    source_place_code=place_code[:128],
                    source_place_name=place_name[:255],
                    reserved_quantity=take,
                    sequence=route_sequence,
                    marking_code=box_item.marking_code,
                    source_snapshot={"route": route},
                )
                remaining -= take
                if remaining <= 0:
                    break
            if remaining:
                missing_total += remaining
                route_sequence += 1
                WmsNewWaveAllocation.objects.create(
                    pick_line=line,
                    status=WmsNewWaveAllocation.STATUS_SHORTAGE,
                    reserved_quantity=remaining,
                    sequence=route_sequence,
                    source_snapshot={"reason": "Недостаточно доступного остатка FBS-NEW"},
                )
                line.source_snapshot = {"required": planned, "shortage": remaining}
                if remaining >= planned:
                    line.status = WmsNewWavePickLine.STATUS_SHORTAGE
                line.save(update_fields=("status", "source_snapshot", "updated_at"))

        if missing_total and not warn_on_shortage:
            for line in created_lines:
                for allocation in line.allocations.select_for_update().all():
                    if allocation.status in (
                        WmsNewWaveAllocation.STATUS_RESERVED,
                        WmsNewWaveAllocation.STATUS_PICKING,
                    ):
                        _release_allocation(allocation, status=WmsNewWaveAllocation.STATUS_CANCELLED)
                line.delete()
            result["orders_not_added_in_wave_count"] += 1
            result["problematic_orders"].append(
                {"order": order.external_order_id, "reason": f"Недостаточно {missing_total} шт."}
            )
            continue

        order_sequence += 1
        WmsNewWaveOrder.objects.create(wave=wave, order=order, sequence=order_sequence)
        unit_count = sum(line.planned_quantity for line in created_lines)
        if unit_count <= 1:
            result["orders_with_one_good_count"] += 1
        else:
            result["orders_with_many_good_count"] += 1
        if missing_total:
            result["potentially_short_orders_count"] += 1
        result["processed_orders_count"] += 1
        before = {"status": order.status, "pilot_revision": order.pilot_revision}
        order.status = WmsNewOrder.STATUS_QUEUED
        order.pilot_revision += 1
        order.save(update_fields=("status", "pilot_revision", "updated_at"))
        _event(
            "order",
            order.id,
            "launch_wave",
            actor,
            before,
            {"status": order.status, "pilot_revision": order.pilot_revision},
            {"wave_id": wave.id, "shortage": missing_total},
        )

    wave.planned_orders = result["processed_orders_count"]
    wave.planned_units = int(
        wave.pick_lines.aggregate(total=Sum("planned_quantity"))["total"] or 0
    )
    snapshot = dict(wave.source_snapshot or {})
    snapshot.update(
        {
            "creation_result": result,
            "has_problems": bool(result["potentially_short_orders_count"]),
            "shortage_mode": shortage_mode,
        }
    )
    wave.source_snapshot = snapshot
    wave.save(update_fields=("planned_orders", "planned_units", "source_snapshot", "updated_at"))
    return result


def resolve_collection_place(value: str) -> tuple[str, str, str]:
    scan = str(value or "").strip()
    if not scan:
        raise PickingOperationError("Отсканируйте место подбора.")
    cart = FbsPickingCart.objects.filter(is_active=True).filter(
        Q(barcode__iexact=scan) | Q(name__iexact=scan)
    ).first()
    if cart:
        return cart.barcode, cart.name, "cart"
    workstation = FbsWorkstation.objects.filter(is_active=True).filter(
        Q(barcode__iexact=scan) | Q(name__iexact=scan)
    ).first()
    if workstation:
        return workstation.barcode, workstation.name, "workstation"
    raise PickingOperationError("Место подбора не найдено среди активных мест FBS.")


@transaction.atomic
def start_collection(*, wave_id: int, place_scan: str, actor=None) -> WmsNewWave:
    wave = WmsNewWave.objects.select_for_update().get(pk=wave_id)
    if not wave.is_manual or not wave.pick_lines.exists():
        raise PickingOperationError(
            "Эта волна импортирована из старого контура и доступна в FBS-NEW только для просмотра."
        )
    if wave.status not in (WmsNewWave.STATUS_QUEUED, WmsNewWave.STATUS_IN_PROGRESS):
        raise PickingOperationError("Эту волну нельзя запустить в подбор.")
    place_code, place_name, place_type = resolve_collection_place(place_scan)
    if WmsNewWave.objects.exclude(pk=wave.pk).filter(
        status=WmsNewWave.STATUS_IN_PROGRESS,
    ).filter(Q(cart_name__iexact=place_name) | Q(workstation_name__iexact=place_name)).exists():
        raise PickingOperationError("Это место уже используется другой активной волной FBS-NEW.")
    before = {"status": wave.status, "place": wave.cart_name or wave.workstation_name}
    wave.status = WmsNewWave.STATUS_IN_PROGRESS
    wave.assigned_to = actor
    wave.started_at = wave.started_at or timezone.now()
    wave.cart_name = place_name if place_type == "cart" else ""
    wave.workstation_name = place_name if place_type == "workstation" else ""
    snapshot = dict(wave.source_snapshot or {})
    snapshot["collection_place_code"] = place_code
    snapshot.pop("current_source_place_code", None)
    wave.source_snapshot = snapshot
    wave.pilot_revision += 1
    wave.save()
    for order in WmsNewOrder.objects.select_for_update().filter(wave_orders__wave=wave):
        if order.status != WmsNewOrder.STATUS_PICKING:
            order.status = WmsNewOrder.STATUS_PICKING
            order.pilot_revision += 1
            order.save(update_fields=("status", "pilot_revision", "updated_at"))
    _event(
        "wave",
        wave.id,
        "start_collecting",
        actor,
        before,
        {"status": wave.status, "place": place_name, "place_code": place_code},
    )
    return wave


def next_allocation(wave: WmsNewWave) -> WmsNewWaveAllocation | None:
    return (
        wave.pick_lines.filter(
            allocations__status__in=(
                WmsNewWaveAllocation.STATUS_RESERVED,
                WmsNewWaveAllocation.STATUS_PICKING,
            )
        )
        .values_list("allocations__id", flat=True)
        .order_by("allocations__sequence", "allocations__id")
        .first()
        and WmsNewWaveAllocation.objects.select_related(
            "pick_line__order", "pick_line__order_item", "pick_line__product", "box_item"
        ).get(
            pk=wave.pick_lines.filter(
                allocations__status__in=(
                    WmsNewWaveAllocation.STATUS_RESERVED,
                    WmsNewWaveAllocation.STATUS_PICKING,
                )
            )
            .values_list("allocations__id", flat=True)
            .order_by("allocations__sequence", "allocations__id")
            .first()
        )
    )


@transaction.atomic
def scan_source_place(*, wave_id: int, place_scan: str, actor=None) -> WmsNewWaveAllocation:
    wave = WmsNewWave.objects.select_for_update().get(pk=wave_id)
    if wave.status != WmsNewWave.STATUS_IN_PROGRESS:
        raise PickingOperationError("Сначала запустите волну и отсканируйте место подбора.")
    allocation = next_allocation(wave)
    if allocation is None:
        _complete_collection_if_ready(wave, actor=actor)
        raise PickingOperationError("В волне больше нет товаров для подбора.")
    scan = str(place_scan or "").strip()
    expected = {
        allocation.source_place_code.casefold(),
        allocation.source_place_name.casefold(),
        allocation.source_box_code.casefold(),
    }
    if not scan or scan.casefold() not in expected:
        raise PickingOperationError(
            f"Ожидается место {allocation.source_place_name or allocation.source_place_code}."
        )
    allocation.status = WmsNewWaveAllocation.STATUS_PICKING
    allocation.started_at = allocation.started_at or timezone.now()
    allocation.save(update_fields=("status", "started_at", "updated_at"))
    snapshot = dict(wave.source_snapshot or {})
    snapshot["current_source_place_code"] = allocation.source_place_code
    wave.source_snapshot = snapshot
    wave.pilot_revision += 1
    wave.save(update_fields=("source_snapshot", "pilot_revision", "updated_at"))
    _event(
        "wave_allocation", allocation.id, "scan_place", actor,
        after={"place": allocation.source_place_code, "wave_id": wave.id},
    )
    return allocation


def _scan_matches(allocation: WmsNewWaveAllocation, scan: str) -> bool:
    line = allocation.pick_line
    item = line.order_item
    values = {
        str(item.id),
        str(item.source_item_id or ""),
        str(item.barcode or ""),
        str(item.external_sku or ""),
        str(allocation.marking_code or ""),
    }
    if line.product_id:
        values.update(
            {
                str(line.product.id),
                str(line.product.source_sku_id or ""),
                str(line.product.barcode or ""),
                str(line.product.article or ""),
            }
        )
    if allocation.box_item_id:
        values.update(
            {
                str(allocation.box_item.barcode or ""),
                str(allocation.box_item.sku_code or ""),
                str(allocation.box_item.marking_code or ""),
            }
        )
    normalized = scan.casefold()
    return bool(normalized and normalized in {value.casefold() for value in values if value})


def _consume_reserved_stock(allocation: WmsNewWaveAllocation, quantity: int) -> None:
    if not allocation.box_item_id:
        raise PickingOperationError("Для позиции нет зарезервированного места хранения.")
    remaining = allocation.reserved_quantity - allocation.picked_quantity
    if quantity <= 0 or quantity > remaining:
        raise PickingOperationError(f"Можно подобрать не более {remaining} шт.")
    box_item = WmsNewBoxItem.objects.select_for_update().get(pk=allocation.box_item_id)
    if box_item.reserved_qty < quantity or box_item.qty < quantity:
        raise PickingOperationError("Зарезервированный остаток FBS-NEW изменился.")
    box_item.reserved_qty -= quantity
    box_item.qty -= quantity
    box_item.pilot_revision += 1
    box_item.save(update_fields=("reserved_qty", "qty", "pilot_revision", "updated_at"))
    box = WmsNewBox.objects.select_for_update().get(pk=box_item.box_id)
    box.reserved_qty = max(0, box.reserved_qty - quantity)
    box.stock_on_hand = max(0, box.stock_on_hand - quantity)
    box.pilot_revision += 1
    box.save(update_fields=("reserved_qty", "stock_on_hand", "pilot_revision", "updated_at"))


def _refresh_line(line: WmsNewWavePickLine, actor=None) -> None:
    picked = int(line.allocations.aggregate(total=Sum("picked_quantity"))["total"] or 0)
    line.picked_quantity = picked
    active = line.allocations.filter(
        status__in=(WmsNewWaveAllocation.STATUS_RESERVED, WmsNewWaveAllocation.STATUS_PICKING)
    ).exists()
    if picked >= line.planned_quantity:
        line.status = WmsNewWavePickLine.STATUS_PICKED
        line.completed_at = timezone.now()
    elif not active:
        line.status = WmsNewWavePickLine.STATUS_SHORTAGE
        line.completed_at = timezone.now()
    else:
        line.status = WmsNewWavePickLine.STATUS_PICKING if picked else WmsNewWavePickLine.STATUS_PENDING
    line.picked_by = actor or line.picked_by
    line.started_at = line.started_at or (timezone.now() if picked else None)
    line.save()


def _complete_collection_if_ready(wave: WmsNewWave, actor=None) -> bool:
    if wave.pick_lines.filter(
        allocations__status__in=(
            WmsNewWaveAllocation.STATUS_RESERVED,
            WmsNewWaveAllocation.STATUS_PICKING,
        )
    ).exists():
        return False
    picked_total = int(wave.pick_lines.aggregate(total=Sum("picked_quantity"))["total"] or 0)
    has_problems = wave.pick_lines.exclude(status=WmsNewWavePickLine.STATUS_PICKED).exists()
    for link in wave.wave_orders.select_related("order").all():
        order = link.order
        lines = wave.pick_lines.filter(order=order)
        order.status = (
            WmsNewOrder.STATUS_PICKED
            if lines.exists() and not lines.exclude(status=WmsNewWavePickLine.STATUS_PICKED).exists()
            else WmsNewOrder.STATUS_EXCEPTION
        )
        order.pilot_revision += 1
        order.save(update_fields=("status", "pilot_revision", "updated_at"))
    before = {"status": wave.status, "picked_units": wave.picked_units}
    wave.status = WmsNewWave.STATUS_VERIFICATION
    wave.picked_units = picked_total
    snapshot = dict(wave.source_snapshot or {})
    snapshot["has_problems"] = has_problems
    snapshot.pop("current_source_place_code", None)
    wave.source_snapshot = snapshot
    wave.pilot_revision += 1
    wave.save()
    _event(
        "wave", wave.id, "collecting_complete", actor, before,
        {"status": wave.status, "picked_units": picked_total, "has_problems": has_problems},
    )
    return True


@transaction.atomic
def scan_product(
    *, wave_id: int, barcode: str, quantity: int = 1, actor=None
) -> WmsNewWaveAllocation:
    wave = WmsNewWave.objects.select_for_update().get(pk=wave_id)
    if wave.status != WmsNewWave.STATUS_IN_PROGRESS:
        raise PickingOperationError("Волна не находится в подборе.")
    allocation_id = (
        wave.pick_lines.filter(allocations__status=WmsNewWaveAllocation.STATUS_PICKING)
        .values_list("allocations__id", flat=True)
        .order_by("allocations__sequence", "allocations__id")
        .first()
    )
    if not allocation_id:
        raise PickingOperationError("Сначала отсканируйте ожидаемое место хранения.")
    allocation = (
        WmsNewWaveAllocation.objects.select_for_update(of=("self",))
        .select_related("pick_line__order", "pick_line__order_item", "pick_line__product", "box_item")
        .get(pk=allocation_id)
    )
    scan = str(barcode or "").strip()
    if not _scan_matches(allocation, scan):
        raise PickingOperationError(
            f"Ожидается товар «{allocation.pick_line.order_item.product_name or allocation.pick_line.order_item.external_sku}»."
        )
    values = system_section_settings("fbs", FBS_PICKING_DEFAULTS)
    if str(values.get("scan_each_unit") or "yes") == "yes":
        quantity = 1
    try:
        quantity = int(quantity)
    except (TypeError, ValueError) as exc:
        raise PickingOperationError("Количество должно быть целым числом.") from exc
    _consume_reserved_stock(allocation, quantity)
    allocation.picked_quantity += quantity
    allocation.picked_by = actor
    if allocation.picked_quantity >= allocation.reserved_quantity:
        allocation.status = WmsNewWaveAllocation.STATUS_PICKED
        allocation.completed_at = timezone.now()
    allocation.save()
    _refresh_line(allocation.pick_line, actor=actor)
    wave.picked_units = int(wave.pick_lines.aggregate(total=Sum("picked_quantity"))["total"] or 0)
    snapshot = dict(wave.source_snapshot or {})
    if allocation.status == WmsNewWaveAllocation.STATUS_PICKED:
        snapshot.pop("current_source_place_code", None)
    wave.source_snapshot = snapshot
    wave.pilot_revision += 1
    wave.save(update_fields=("picked_units", "source_snapshot", "pilot_revision", "updated_at"))
    _event(
        "wave_allocation", allocation.id, "scan_good", actor,
        after={"barcode": scan, "quantity": quantity, "picked": allocation.picked_quantity},
    )
    _complete_collection_if_ready(wave, actor=actor)
    return allocation


@transaction.atomic
def skip_current_allocation(*, wave_id: int, actor=None) -> WmsNewWaveAllocation:
    wave = WmsNewWave.objects.select_for_update().get(pk=wave_id)
    allocation = next_allocation(wave)
    if wave.status != WmsNewWave.STATUS_IN_PROGRESS or allocation is None:
        raise PickingOperationError("Нет текущего товара для пропуска.")
    _release_allocation(allocation, status=WmsNewWaveAllocation.STATUS_SKIPPED)
    _refresh_line(allocation.pick_line, actor=actor)
    snapshot = dict(wave.source_snapshot or {})
    snapshot["has_problems"] = True
    snapshot.pop("current_source_place_code", None)
    wave.source_snapshot = snapshot
    wave.pilot_revision += 1
    wave.save(update_fields=("source_snapshot", "pilot_revision", "updated_at"))
    _event("wave_allocation", allocation.id, "skip_good", actor, after={"wave_id": wave.id})
    _complete_collection_if_ready(wave, actor=actor)
    return allocation


@transaction.atomic
def finish_collection(*, wave_id: int, actor=None) -> WmsNewWave | None:
    wave = WmsNewWave.objects.select_for_update().get(pk=wave_id)
    if not wave.is_manual or not wave.pick_lines.exists():
        raise PickingOperationError(
            "Импортированную волну нельзя изменять из FBS-NEW."
        )
    if wave.status not in (WmsNewWave.STATUS_QUEUED, WmsNewWave.STATUS_IN_PROGRESS):
        raise PickingOperationError("Подбор этой волны уже завершен.")
    for link in list(wave.wave_orders.select_related("order").all()):
        lines = list(wave.pick_lines.filter(order=link.order))
        picked = sum(line.picked_quantity for line in lines)
        if picked == 0:
            for line in lines:
                for allocation in line.allocations.select_for_update().all():
                    if allocation.status in (
                        WmsNewWaveAllocation.STATUS_RESERVED,
                        WmsNewWaveAllocation.STATUS_PICKING,
                    ):
                        _release_allocation(allocation, status=WmsNewWaveAllocation.STATUS_RELEASED)
                line.delete()
            order = link.order
            order.status = WmsNewOrder.STATUS_NEW
            order.pilot_revision += 1
            order.save(update_fields=("status", "pilot_revision", "updated_at"))
            link.delete()
            continue
        for line in lines:
            for allocation in line.allocations.select_for_update().filter(
                status__in=(
                    WmsNewWaveAllocation.STATUS_RESERVED,
                    WmsNewWaveAllocation.STATUS_PICKING,
                )
            ):
                _release_allocation(allocation, status=WmsNewWaveAllocation.STATUS_SKIPPED)
            _refresh_line(line, actor=actor)
    wave.planned_orders = wave.wave_orders.count()
    wave.planned_units = int(wave.pick_lines.aggregate(total=Sum("planned_quantity"))["total"] or 0)
    if not wave.planned_orders:
        wave_number = wave.number
        wave.delete()
        _event("wave", wave_id, "delete_empty_wave", actor, before={"number": wave_number})
        return None
    wave.save(update_fields=("planned_orders", "planned_units", "updated_at"))
    _complete_collection_if_ready(wave, actor=actor)
    return wave


@transaction.atomic
def remove_order_from_wave(*, wave_id: int, order_id: int, actor=None) -> WmsNewWave | None:
    wave = WmsNewWave.objects.select_for_update().get(pk=wave_id)
    if not wave.is_manual or not wave.pick_lines.exists():
        raise PickingOperationError(
            "Импортированную волну нельзя изменять из FBS-NEW."
        )
    if wave.status != WmsNewWave.STATUS_QUEUED:
        raise PickingOperationError("Удалять заказы можно только из новой волны.")
    link = WmsNewWaveOrder.objects.select_for_update().filter(wave=wave, order_id=order_id).first()
    if link is None:
        raise PickingOperationError("Заказ не найден в этой волне.")
    lines = list(wave.pick_lines.filter(order_id=order_id))
    if any(line.picked_quantity for line in lines):
        raise PickingOperationError("По заказу уже начат подбор.")
    for line in lines:
        for allocation in line.allocations.select_for_update().all():
            if allocation.status in (
                WmsNewWaveAllocation.STATUS_RESERVED,
                WmsNewWaveAllocation.STATUS_PICKING,
            ):
                _release_allocation(allocation)
        line.delete()
    order = WmsNewOrder.objects.select_for_update().get(pk=order_id)
    order.status = WmsNewOrder.STATUS_NEW
    order.pilot_revision += 1
    order.save(update_fields=("status", "pilot_revision", "updated_at"))
    link.delete()
    wave.planned_orders = wave.wave_orders.count()
    wave.planned_units = int(wave.pick_lines.aggregate(total=Sum("planned_quantity"))["total"] or 0)
    if not wave.planned_orders:
        wave.delete()
        return None
    wave.pilot_revision += 1
    wave.save(update_fields=("planned_orders", "planned_units", "pilot_revision", "updated_at"))
    _event("wave", wave.id, "remove_order", actor, after={"order_id": order_id})
    return wave


@transaction.atomic
def cancel_wave(*, wave_id: int, actor=None) -> WmsNewWave:
    wave = WmsNewWave.objects.select_for_update().get(pk=wave_id)
    if not wave.is_manual or not wave.pick_lines.exists():
        raise PickingOperationError(
            "Импортированную волну нельзя отменить из FBS-NEW."
        )
    if wave.status in (WmsNewWave.STATUS_DONE, WmsNewWave.STATUS_CANCELLED):
        raise PickingOperationError("Эта волна уже завершена.")
    if wave.pick_lines.filter(picked_quantity__gt=0).exists():
        raise PickingOperationError(
            "В волне уже есть отобранные товары. Завершите подбор, чтобы передать их в сборку."
        )
    for allocation in WmsNewWaveAllocation.objects.select_for_update().filter(
        pick_line__wave=wave,
        status__in=(WmsNewWaveAllocation.STATUS_RESERVED, WmsNewWaveAllocation.STATUS_PICKING),
    ):
        _release_allocation(allocation, status=WmsNewWaveAllocation.STATUS_CANCELLED)
    for order in WmsNewOrder.objects.select_for_update().filter(wave_orders__wave=wave):
        order.status = WmsNewOrder.STATUS_NEW
        order.pilot_revision += 1
        order.save(update_fields=("status", "pilot_revision", "updated_at"))
    before = {"status": wave.status}
    wave.status = WmsNewWave.STATUS_CANCELLED
    wave.completed_at = timezone.now()
    wave.pilot_revision += 1
    wave.save()
    _event("wave", wave.id, "cancel", actor, before, {"status": wave.status})
    return wave
