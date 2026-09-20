from __future__ import annotations

from collections import defaultdict

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from sklad.models import WarehouseLocation

from ..models import (
    WmsNewBox,
    WmsNewBoxItem,
    WmsNewEvent,
    WmsNewMovement,
    WmsNewProduct,
)


class MovementOperationError(ValueError):
    pass


def _location_name(location: WarehouseLocation | None) -> str:
    if location is None:
        return ""
    return location.display_name or location.location_code or location.zone_code


def _actor_name(actor) -> str:
    if actor is None:
        return ""
    full_name = str(actor.get_full_name() or "").strip()
    return full_name or str(actor.get_username() or actor)


def _recalculate_box(box: WmsNewBox) -> None:
    totals = box.items.aggregate(
        qty=Sum("qty"),
        available=Sum("available_qty"),
        reserved=Sum("reserved_qty"),
    )
    box.stock_on_hand = int(totals["qty"] or 0)
    box.stock_free = int(totals["available"] or 0)
    box.reserved_qty = int(totals["reserved"] or 0)
    box.sku_count = box.items.filter(qty__gt=0).values("sku_code").distinct().count()
    box.marking_count = box.items.exclude(marking_code="").filter(qty__gt=0).count()
    box.pilot_revision += 1
    box.save(
        update_fields=(
            "stock_on_hand",
            "stock_free",
            "reserved_qty",
            "sku_count",
            "marking_count",
            "pilot_revision",
            "updated_at",
        )
    )


def _destination_box(*, agency_id: int, location: WarehouseLocation, actor=None) -> WmsNewBox:
    code = f"FBN-MOVE-{agency_id}-{location.id}"
    box, created = WmsNewBox.objects.select_for_update().get_or_create(
        agency_id=agency_id,
        code=code,
        defaults={
            "location": location,
            "location_code": _location_name(location),
            "zone_code": location.zone_code,
            "is_manual": True,
            "pilot_revision": 1,
            "created_by": actor,
        },
    )
    if not created and box.location_id != location.id:
        box.location = location
        box.location_code = _location_name(location)
        box.zone_code = location.zone_code
        box.pilot_revision += 1
        box.save(
            update_fields=(
                "location",
                "location_code",
                "zone_code",
                "pilot_revision",
                "updated_at",
            )
        )
    return box


def _movement_record(
    *,
    agency_id: int,
    product: WmsNewProduct | None,
    product_name: str,
    article: str,
    source: WarehouseLocation,
    target: WarehouseLocation,
    quantity: int,
    boxes_count: int,
    actor,
    payload: dict,
) -> WmsNewMovement:
    movement = WmsNewMovement.objects.create(
        agency_id=agency_id,
        product=product,
        product_name=product.name if product else product_name,
        article=product.article if product else article,
        action=WmsNewMovement.ACTION_MOVEMENT,
        source_location=source,
        source_location_name=_location_name(source),
        target_location=target,
        target_location_name=_location_name(target),
        quantity=quantity,
        balance_after=product.stock_on_hand if product else 0,
        boxes_count=boxes_count,
        information="Перемещение внутри склада",
        source_event_type="wms_new_manual_movement",
        actor=actor,
        actor_name=_actor_name(actor),
        occurred_at=timezone.now(),
        payload=payload,
        is_manual=True,
    )
    WmsNewEvent.objects.create(
        entity_type="movement",
        entity_id=movement.id,
        action="move",
        actor=actor,
        after={
            "product_id": movement.product_id,
            "quantity": movement.quantity,
            "boxes_count": movement.boxes_count,
            "source_location_id": source.id,
            "target_location_id": target.id,
        },
        metadata=payload,
    )
    return movement


def _validate_locations(source_id: int, target_id: int) -> tuple[WarehouseLocation, WarehouseLocation]:
    if not source_id or not target_id:
        raise MovementOperationError("Укажите место-источник и место размещения.")
    if source_id == target_id:
        raise MovementOperationError("Источник и место размещения должны отличаться.")
    try:
        source = WarehouseLocation.objects.get(pk=source_id, is_active=True)
        target = WarehouseLocation.objects.get(pk=target_id, is_active=True)
    except WarehouseLocation.DoesNotExist as exc:
        raise MovementOperationError("Выбранное место не найдено или неактивно.") from exc
    return source, target


@transaction.atomic
def move_product(
    *,
    product_id: int,
    source_location_id: int,
    target_location_id: int,
    units=0,
    box_ids: list[int] | tuple[int, ...] = (),
    actor=None,
) -> list[WmsNewMovement]:
    try:
        amount = int(units or 0)
    except (TypeError, ValueError) as exc:
        raise MovementOperationError("Количество единиц должно быть целым числом.") from exc
    if amount < 0:
        raise MovementOperationError("Количество единиц не может быть отрицательным.")
    normalized_box_ids = sorted({int(value) for value in box_ids if int(value) > 0})
    if amount == 0 and not normalized_box_ids:
        raise MovementOperationError("Укажите количество единиц или выберите коробы.")
    product = WmsNewProduct.objects.select_for_update().get(pk=product_id, is_archived=False)
    source, target = _validate_locations(source_location_id, target_location_id)

    selected_boxes = list(
        WmsNewBox.objects.select_for_update()
        .filter(
            id__in=normalized_box_ids,
            agency_id=product.agency_id,
            location=source,
            status=WmsNewBox.STATUS_ACTIVE,
            items__product=product,
            items__qty__gt=0,
        )
        .distinct()
        .order_by("id")
    )
    if len(selected_boxes) != len(normalized_box_ids):
        raise MovementOperationError("Часть выбранных коробов уже перемещена или не содержит товар.")

    grouped: dict[tuple[int | None, str, str], dict] = defaultdict(
        lambda: {"quantity": 0, "box_ids": set(), "product": None, "name": "", "article": ""}
    )
    selected_ids = {box.id for box in selected_boxes}
    for box in selected_boxes:
        for item in box.items.select_for_update().filter(qty__gt=0).select_related("product"):
            key = (item.product_id, item.sku_code, item.product_name)
            row = grouped[key]
            row["quantity"] += item.qty
            row["box_ids"].add(box.id)
            row["product"] = item.product
            row["name"] = item.product_name
            row["article"] = item.sku_code
        box.location = target
        box.location_code = _location_name(target)
        box.zone_code = target.zone_code
        box.pilot_revision += 1
        box.save(
            update_fields=(
                "location",
                "location_code",
                "zone_code",
                "pilot_revision",
                "updated_at",
            )
        )

    touched_source_boxes: set[int] = set()
    destination_box = None
    if amount:
        source_items = list(
            WmsNewBoxItem.objects.select_for_update()
            .filter(
                product=product,
                box__agency_id=product.agency_id,
                box__location=source,
                box__status=WmsNewBox.STATUS_ACTIVE,
                available_qty__gt=0,
            )
            .exclude(box_id__in=selected_ids)
            .select_related("box")
            .order_by("box_id", "id")
        )
        available = sum(item.available_qty for item in source_items)
        if available < amount:
            raise MovementOperationError(
                f"На выбранном месте доступно только {available} ед. товара."
            )
        destination_box = _destination_box(
            agency_id=product.agency_id,
            location=target,
            actor=actor,
        )
        remaining = amount
        for item in source_items:
            if remaining <= 0:
                break
            moved = min(item.available_qty, remaining)
            item.qty -= moved
            item.available_qty -= moved
            item.pilot_revision += 1
            item.save(update_fields=("qty", "available_qty", "pilot_revision", "updated_at"))
            touched_source_boxes.add(item.box_id)
            destination_item = (
                WmsNewBoxItem.objects.select_for_update()
                .filter(
                    box=destination_box,
                    product=item.product,
                    sku_code=item.sku_code,
                    size=item.size,
                    barcode=item.barcode,
                    marking_code=item.marking_code,
                )
                .first()
            )
            if destination_item is None:
                destination_item = WmsNewBoxItem.objects.create(
                    box=destination_box,
                    product=item.product,
                    agency=item.agency,
                    sku_code=item.sku_code,
                    product_name=item.product_name,
                    size=item.size,
                    barcode=item.barcode,
                    goods_type=item.goods_type,
                    marking_code=item.marking_code,
                    qty=moved,
                    available_qty=moved,
                    warehouse_state_code=item.warehouse_state_code,
                    pilot_revision=1,
                    source_snapshot={"origin": "wms_new_movement", "source_item_id": item.id},
                )
            else:
                destination_item.qty += moved
                destination_item.available_qty += moved
                destination_item.pilot_revision += 1
                destination_item.save(
                    update_fields=("qty", "available_qty", "pilot_revision", "updated_at")
                )
            remaining -= moved
        key = (product.id, product.article, product.name)
        grouped[key]["quantity"] += amount
        grouped[key]["product"] = product
        grouped[key]["name"] = product.name
        grouped[key]["article"] = product.article

    for box_id in touched_source_boxes:
        _recalculate_box(WmsNewBox.objects.select_for_update().get(pk=box_id))
    if destination_box is not None:
        _recalculate_box(destination_box)

    records = []
    for row in grouped.values():
        records.append(
            _movement_record(
                agency_id=product.agency_id,
                product=row["product"],
                product_name=row["name"],
                article=row["article"],
                source=source,
                target=target,
                quantity=row["quantity"],
                boxes_count=len(row["box_ids"]),
                actor=actor,
                payload={
                    "mode": "product",
                    "requested_product_id": product.id,
                    "selected_box_ids": normalized_box_ids,
                    "units": amount,
                },
            )
        )
    return records


@transaction.atomic
def move_all_from_location(
    *, source_location_id: int, target_location_id: int, actor=None
) -> list[WmsNewMovement]:
    source, target = _validate_locations(source_location_id, target_location_id)
    boxes = list(
        WmsNewBox.objects.select_for_update()
        .filter(location=source, status=WmsNewBox.STATUS_ACTIVE)
        .order_by("id")
    )
    if not boxes:
        raise MovementOperationError("На выбранном месте нет активных коробов.")
    grouped: dict[tuple[int, int | None, str, str], dict] = defaultdict(
        lambda: {"quantity": 0, "box_ids": set(), "product": None, "name": "", "article": ""}
    )
    box_ids = [box.id for box in boxes]
    for item in (
        WmsNewBoxItem.objects.select_for_update()
        .filter(box_id__in=box_ids, qty__gt=0)
        .select_related("product")
        .order_by("box_id", "id")
    ):
        key = (item.agency_id, item.product_id, item.sku_code, item.product_name)
        row = grouped[key]
        row["quantity"] += item.qty
        row["box_ids"].add(item.box_id)
        row["product"] = item.product
        row["name"] = item.product_name
        row["article"] = item.sku_code
    for box in boxes:
        box.location = target
        box.location_code = _location_name(target)
        box.zone_code = target.zone_code
        box.pilot_revision += 1
        box.save(
            update_fields=(
                "location",
                "location_code",
                "zone_code",
                "pilot_revision",
                "updated_at",
            )
        )
    records = []
    for (agency_id, _product_id, _article, _name), row in grouped.items():
        records.append(
            _movement_record(
                agency_id=agency_id,
                product=row["product"],
                product_name=row["name"],
                article=row["article"],
                source=source,
                target=target,
                quantity=row["quantity"],
                boxes_count=len(row["box_ids"]),
                actor=actor,
                payload={"mode": "all", "box_ids": box_ids},
            )
        )
    if not records:
        agency_ids = {box.agency_id for box in boxes}
        for agency_id in agency_ids:
            records.append(
                _movement_record(
                    agency_id=agency_id,
                    product=None,
                    product_name="Коробы без товарных строк",
                    article="",
                    source=source,
                    target=target,
                    quantity=0,
                    boxes_count=sum(1 for box in boxes if box.agency_id == agency_id),
                    actor=actor,
                    payload={"mode": "all", "box_ids": box_ids},
                )
            )
    return records
