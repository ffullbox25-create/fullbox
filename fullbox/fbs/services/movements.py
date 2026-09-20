from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from fbs.exceptions import FbsFeatureDisabled, FbsMovementError
from fbs.flags import feature_enabled
from fbs.models import (
    FbsBox,
    FbsInternalMovement,
    FbsOrderStockAllocation,
    FbsPallet,
    FbsStockBalance,
)
from sklad.models import WarehouseEvent

from .inventory import (
    assert_balance_unlocked,
    assert_box_unlocked,
    assert_pallet_not_relocating,
)


FLOOR_REPLENISHMENT_COMMENT_PREFIX = "[wave-floor-replenishment]"


def _require_writes() -> None:
    if not feature_enabled("module"):
        raise FbsFeatureDisabled("Модуль FBS выключен.")
    if not feature_enabled("warehouse_writes"):
        raise FbsFeatureDisabled("Складские операции FBS выключены.")


def _authenticated_user(user):
    return user if getattr(user, "is_authenticated", False) else None


def _normalized(value: str) -> str:
    return str(value or "").strip().casefold()


def _require_concrete_container_location(location, *, label: str) -> None:
    zone_code = str(getattr(location, "zone_code", "") or "").strip().upper()
    if (
        zone_code == "OS"
        or str(getattr(location, "zone_kind", "") or "").strip()
        == "storage"
    ):
        concrete = all(
            int(value or 0) > 0
            for value in (
                getattr(location, "row_no", 0),
                getattr(location, "section_no", 0),
                getattr(location, "tier_no", 0),
                getattr(location, "cell_no", 0),
            )
        )
    else:
        concrete = bool(str(getattr(location, "location_code", "") or "").strip())
    if not concrete:
        raise FbsMovementError(
            f"{label} не является точной физической ячейкой. "
            "Укажите ряд, секцию, ярус и место; техническая зона OS запрещена."
        )


def is_floor_replenishment_movement(movement: FbsInternalMovement) -> bool:
    return str(movement.comment or "").startswith(FLOOR_REPLENISHMENT_COMMENT_PREFIX)


def _assert_floor_replenishment_route(
    *,
    source_box: FbsBox,
    target_pallet: FbsPallet,
) -> None:
    source_tier = int(source_box.pallet.cell.location.tier_no or 0)
    target_tier = int(target_pallet.cell.location.tier_no or 0)
    if source_tier <= 1:
        raise FbsMovementError("Источник пополнения должен находиться выше первого яруса.")
    if target_tier != 1:
        raise FbsMovementError("Пополнение волны разрешено только на первый ярус.")


def suggest_internal_destination(*, source_box: FbsBox) -> FbsPallet | None:
    """Keep one client's stock together, preferring pick cells and existing clusters."""
    candidates = (
        FbsPallet.objects.filter(
            agency=source_box.agency,
            status__in=(FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE),
            cell__is_active=True,
        )
        .exclude(pk=source_box.pallet_id)
        .annotate(active_box_count=Count(
            "boxes",
            filter=Q(boxes__status__in=(FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE)),
        ))
        .order_by(
            F("cell__client_cluster").asc(nulls_last=True),
            "cell__purpose",
            "cell__location__row_no",
            "cell__location__section_no",
            "cell__location__tier_no",
            "cell__location__cell_no",
            "id",
        )
    )
    for pallet in candidates:
        if int(pallet.active_box_count or 0) < int(pallet.max_boxes or 0):
            return pallet
    return None


@transaction.atomic
def create_internal_movement(
    *,
    mode: str,
    source_box: FbsBox,
    target_pallet: FbsPallet | None = None,
    target_box: FbsBox | None = None,
    source_balance: FbsStockBalance | None = None,
    qty: int | None = None,
    requested_by=None,
    comment: str = "",
) -> FbsInternalMovement:
    _require_writes()
    source_box = (
        FbsBox.objects.select_for_update()
        .select_related("pallet__cell__location")
        .get(pk=source_box.pk)
    )
    assert_pallet_not_relocating(
        source_box.pallet_id,
        agency_id=source_box.agency_id,
    )
    if target_pallet is None:
        target_pallet = suggest_internal_destination(source_box=source_box)
    if target_pallet is None:
        raise FbsMovementError("Нет подходящей паллеты FBS для перемещения.")
    target_pallet = (
        FbsPallet.objects.select_for_update()
        .select_related("cell__location")
        .get(pk=target_pallet.pk)
    )
    assert_pallet_not_relocating(
        target_pallet.id,
        agency_id=target_pallet.agency_id,
    )
    if source_box.agency_id != target_pallet.agency_id:
        raise FbsMovementError("Перемещение между клиентами запрещено.")
    floor_replenishment = str(comment or "").startswith(
        FLOOR_REPLENISHMENT_COMMENT_PREFIX
    )
    if floor_replenishment:
        _assert_floor_replenishment_route(
            source_box=source_box,
            target_pallet=target_pallet,
        )
        if source_box.status not in (FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE):
            raise FbsMovementError("Исходный FBS-короб уже не активен.")
        if (
            target_pallet.status not in (FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE)
            or not target_pallet.cell.is_active
        ):
            raise FbsMovementError("Паллета первого яруса уже недоступна.")
        if source_box.pallet_id == target_pallet.id:
            raise FbsMovementError("Короб уже находится на выбранной паллете.")
        pending_incoming = FbsInternalMovement.objects.filter(
            target_pallet=target_pallet,
            mode=FbsInternalMovement.MODE_BOX,
            status__in=(
                FbsInternalMovement.STATUS_PROPOSED,
                FbsInternalMovement.STATUS_IN_PROGRESS,
            ),
            comment__startswith=FLOOR_REPLENISHMENT_COMMENT_PREFIX,
        ).exclude(source_box=source_box).count()
        _target_has_capacity(
            target_pallet,
            moving_box_id=source_box.id,
            reserved_incoming=pending_incoming,
        )
        assert_box_unlocked(source_box.id)
        if source_box.stock_balances.filter(reserved_qty__gt=0).exists() or (
            FbsOrderStockAllocation.objects.filter(
                balance__box=source_box,
                status__in=(
                    FbsOrderStockAllocation.STATUS_RESERVED,
                    FbsOrderStockAllocation.STATUS_PICKING,
                ),
            ).exists()
        ):
            raise FbsMovementError(
                "Короб уже зарезервирован под отбор и не может быть отправлен на первый ярус."
            )
    if mode == FbsInternalMovement.MODE_BOX:
        planned_qty = int(
            source_box.stock_balances.aggregate(total=Sum("qty"))["total"] or 0
        )
        target_box = None
        source_balance = None
    elif mode == FbsInternalMovement.MODE_ITEM:
        if source_balance is None or target_box is None:
            raise FbsMovementError("Для штучного перемещения укажите остаток и целевой короб.")
        source_balance = FbsStockBalance.objects.select_for_update().get(pk=source_balance.pk)
        target_box = FbsBox.objects.select_for_update().get(pk=target_box.pk)
        if source_balance.box_id != source_box.id:
            raise FbsMovementError("Остаток не находится в исходном коробе.")
        if target_box.pallet_id != target_pallet.id:
            raise FbsMovementError("Целевой короб находится на другой паллете.")
        if target_box.id == source_box.id:
            raise FbsMovementError("Исходный и целевой короб совпадают.")
        if target_box.agency_id != source_box.agency_id:
            raise FbsMovementError("Перемещение между клиентами запрещено.")
        planned_qty = int(qty or 0)
        if planned_qty <= 0 or planned_qty > int(source_balance.available_qty or 0):
            raise FbsMovementError("Недопустимое количество штучного перемещения.")
        if source_balance.marking_code and planned_qty != 1:
            raise FbsMovementError("Маркированный товар перемещается по одному КИЗу.")
    else:
        raise FbsMovementError("Неизвестный вид внутреннего перемещения FBS.")
    movement = FbsInternalMovement(
        agency=source_box.agency,
        mode=mode,
        source_box=source_box,
        target_pallet=target_pallet,
        target_box=target_box,
        source_balance=source_balance,
        planned_qty=planned_qty,
        requested_by=_authenticated_user(requested_by),
        comment=str(comment or "").strip(),
    )
    movement.full_clean()
    movement.save()
    return movement


@transaction.atomic
def claim_internal_movement(
    *,
    movement_id: int,
    assigned_to,
    allow_reassignment: bool = False,
) -> FbsInternalMovement:
    _require_writes()
    actor = _authenticated_user(assigned_to)
    if actor is None:
        raise FbsMovementError("Не указан исполнитель перемещения.")
    movement = FbsInternalMovement.objects.select_for_update().get(pk=movement_id)
    if movement.status == FbsInternalMovement.STATUS_IN_PROGRESS:
        if movement.assigned_to_id == actor.id:
            return movement
        if not allow_reassignment:
            raise FbsMovementError("Перемещение уже выполняет другой сотрудник.")
        movement.assigned_to = actor
        movement.save(update_fields=["assigned_to", "updated_at"])
        return movement
    if movement.status != FbsInternalMovement.STATUS_PROPOSED:
        raise FbsMovementError("Перемещение недоступно.")
    movement.assigned_to = actor
    movement.status = FbsInternalMovement.STATUS_IN_PROGRESS
    movement.started_at = timezone.now()
    movement.save(update_fields=["assigned_to", "status", "started_at", "updated_at"])
    return movement


def _assert_actor(movement: FbsInternalMovement, performed_by) -> None:
    actor = _authenticated_user(performed_by)
    if actor is None or movement.assigned_to_id != actor.id:
        raise FbsMovementError("Перемещение назначено другому сотруднику.")
    if movement.status != FbsInternalMovement.STATUS_IN_PROGRESS:
        raise FbsMovementError("Перемещение не находится в работе.")


def _assert_box_available(source_box: FbsBox) -> list[FbsStockBalance]:
    assert_box_unlocked(source_box.id, for_execution=True)
    balances = list(
        FbsStockBalance.objects.select_for_update()
        .filter(box=source_box)
        .order_by("id")
    )
    if any(int(balance.reserved_qty or 0) > 0 for balance in balances):
        raise FbsMovementError("В коробе есть товар, зарезервированный под заказы.")
    if FbsOrderStockAllocation.objects.filter(
        balance__box=source_box,
        status__in=(
            FbsOrderStockAllocation.STATUS_RESERVED,
            FbsOrderStockAllocation.STATUS_PICKING,
        ),
    ).exists():
        raise FbsMovementError("Короб участвует в активном отборе.")
    for balance in balances:
        assert_balance_unlocked(balance.id, for_execution=True)
    return balances


def _target_has_capacity(
    target_pallet: FbsPallet,
    *,
    moving_box_id: int,
    reserved_incoming: int = 0,
) -> None:
    count = target_pallet.boxes.filter(
        status__in=(FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE),
    ).exclude(pk=moving_box_id).count()
    if count + max(int(reserved_incoming or 0), 0) >= int(target_pallet.max_boxes or 0):
        raise FbsMovementError("На паллете назначения нет свободного места для короба.")


def _sku_volume_liters(balance: FbsStockBalance) -> Decimal | None:
    sku = balance.sku_ref
    if sku is None:
        return None
    dimensions = (sku.length_mm, sku.width_mm, sku.height_mm)
    if all(value is not None and Decimal(value) > 0 for value in dimensions):
        return (Decimal(dimensions[0]) * Decimal(dimensions[1]) * Decimal(dimensions[2])) / Decimal(
            1_000_000
        )
    return Decimal(sku.volume) if sku.volume is not None and Decimal(sku.volume) > 0 else None


def _assert_target_volume(target_box: FbsBox, balance: FbsStockBalance, qty: int) -> None:
    dimensions = (target_box.width_mm, target_box.height_mm, target_box.depth_mm)
    if not all(value is not None and int(value) > 0 for value in dimensions):
        return
    capacity = Decimal(dimensions[0] * dimensions[1] * dimensions[2]) / Decimal(1_000_000)
    used = Decimal("0")
    for current in target_box.stock_balances.select_related("sku_ref").filter(qty__gt=0):
        unit_volume = _sku_volume_liters(current)
        if unit_volume is None:
            continue
        used += unit_volume * int(current.qty or 0)
    incoming = _sku_volume_liters(balance)
    if incoming is not None and used + incoming * qty > capacity:
        raise FbsMovementError("В целевом коробе недостаточно расчетного объема.")


@transaction.atomic
def scan_internal_movement(
    *,
    movement_id: int,
    source_box_scan: str,
    target_scan: str,
    item_scan: str = "",
    source_location_scan: str = "",
    performed_by,
) -> FbsInternalMovement:
    _require_writes()
    movement = (
        FbsInternalMovement.objects.select_for_update(of=("self",))
        .select_related(
            "source_box__pallet__cell__location",
            "target_pallet__cell__location",
            "target_box",
            "source_balance__sku_ref",
        )
        .get(pk=movement_id)
    )
    _assert_actor(movement, performed_by)
    if is_floor_replenishment_movement(movement):
        _assert_floor_replenishment_route(
            source_box=movement.source_box,
            target_pallet=movement.target_pallet,
        )
        accepted_source_locations = {
            _normalized(movement.source_box.pallet.cell.cell_code),
            _normalized(movement.source_box.pallet.cell.warehouse_location_code),
            _normalized(movement.source_box.pallet.cell.location.location_code),
        }
        accepted_source_locations.discard("")
        if _normalized(source_location_scan) not in accepted_source_locations:
            raise FbsMovementError("Скан исходного адреса не совпадает с заданием.")
    if _normalized(source_box_scan) != _normalized(movement.source_box.box_code):
        raise FbsMovementError("Скан исходного короба не совпадает с заданием.")
    accepted_target = {
        _normalized(movement.target_pallet.cell.cell_code),
        _normalized(movement.target_pallet.cell.warehouse_location_code),
        _normalized(movement.target_pallet.cell.location.location_code),
    }
    accepted_target.discard("")
    if movement.mode == FbsInternalMovement.MODE_ITEM:
        accepted_target.add(_normalized(movement.target_pallet.pallet_code))
    if movement.mode == FbsInternalMovement.MODE_ITEM and movement.target_box_id:
        accepted_target.add(_normalized(movement.target_box.box_code))
    if _normalized(target_scan) not in accepted_target:
        raise FbsMovementError(
            "Скан конечной ячейки не совпадает с заданием. "
            "Для короба требуется QR точного места, а не QR паллеты."
        )
    now = timezone.now()
    actor = _authenticated_user(performed_by)
    source_location = movement.source_box.pallet.cell.location
    target_location = movement.target_pallet.cell.location
    _require_concrete_container_location(source_location, label="Исходное место")
    _require_concrete_container_location(target_location, label="Место назначения")

    if movement.mode == FbsInternalMovement.MODE_BOX:
        _assert_box_available(movement.source_box)
        for target_box in movement.target_pallet.boxes.filter(
            status__in=(FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE)
        ):
            assert_box_unlocked(target_box.id, for_execution=True)
        if movement.source_box.pallet_id == movement.target_pallet_id:
            raise FbsMovementError("Короб уже находится на выбранной паллете.")
        _target_has_capacity(movement.target_pallet, moving_box_id=movement.source_box_id)
        movement.source_box.pallet = movement.target_pallet
        movement.source_box.save(update_fields=["pallet", "updated_at"])
        if movement.source_box.source_container_id:
            container = movement.source_box.source_container
            container.current_location = target_location
            container.save(update_fields=["current_location", "updated_at"])
        movement.moved_qty = movement.planned_qty
        movement.status = FbsInternalMovement.STATUS_DONE
        movement.completed_at = now
    else:
        assert_box_unlocked(movement.source_box_id, for_execution=True)
        assert_box_unlocked(movement.target_box_id, for_execution=True)
        balance = (
            FbsStockBalance.objects.select_for_update(of=("self",))
            .select_related("sku_ref")
            .get(pk=movement.source_balance_id)
        )
        assert_balance_unlocked(balance.id, for_execution=True)
        for target_balance in FbsStockBalance.objects.select_for_update().filter(
            box=movement.target_box
        ):
            assert_balance_unlocked(target_balance.id, for_execution=True)
        if int(balance.available_qty or 0) <= 0:
            raise FbsMovementError("В исходном коробе закончился доступный товар.")
        accepted_items = {
            _normalized(balance.marking_code),
            _normalized(balance.barcode),
            _normalized(balance.sku_code),
        }
        accepted_items.discard("")
        if _normalized(item_scan) not in accepted_items:
            raise FbsMovementError("Скан товара или КИЗа не совпадает с заданием.")
        _assert_target_volume(movement.target_box, balance, 1)
        target_balance = FbsStockBalance.objects.select_for_update().filter(
            box=movement.target_box,
            identity_key=balance.identity_key,
        ).first()
        if target_balance is None and int(balance.qty or 0) == 1:
            balance.box = movement.target_box
            balance.save(update_fields=["box", "updated_at"])
        else:
            if balance.marking_code:
                raise FbsMovementError("КИЗ уже существует в другом коробе.")
            if target_balance is None:
                target_balance = FbsStockBalance.objects.create(
                    agency=balance.agency,
                    box=movement.target_box,
                    sku_ref=balance.sku_ref,
                    identity_key=balance.identity_key,
                    sku_code=balance.sku_code,
                    name=balance.name,
                    size=balance.size,
                    barcode=balance.barcode,
                    goods_type=balance.goods_type,
                    lot_code=balance.lot_code,
                    expiry_date=balance.expiry_date,
                    qty=0,
                    available_qty=0,
                )
            target_balance.qty = int(target_balance.qty or 0) + 1
            target_balance.available_qty = int(target_balance.available_qty or 0) + 1
            target_balance.save(update_fields=["qty", "available_qty", "updated_at"])
            balance.qty = int(balance.qty or 0) - 1
            balance.available_qty = int(balance.available_qty or 0) - 1
            balance.save(update_fields=["qty", "available_qty", "updated_at"])
        movement.moved_qty = int(movement.moved_qty or 0) + 1
        if movement.moved_qty >= movement.planned_qty:
            movement.status = FbsInternalMovement.STATUS_DONE
            movement.completed_at = now

    WarehouseEvent.objects.create(
        agency=movement.agency,
        event_type=(
            "fbs_internal_box_moved"
            if movement.mode == FbsInternalMovement.MODE_BOX
            else "fbs_internal_item_moved"
        ),
        stock_context_type="fbs_internal_movement",
        stock_context_id=str(movement.id),
        container=movement.source_box.source_container,
        source_document_type="fbs_internal_movement",
        source_document_id=str(movement.id),
        from_location=source_location,
        to_location=target_location,
        from_zone_code=str(source_location.zone_code or ""),
        to_zone_code=str(target_location.zone_code or ""),
        qty=movement.planned_qty if movement.mode == FbsInternalMovement.MODE_BOX else 1,
        payload={
            "mode": movement.mode,
            "source_box_id": movement.source_box_id,
            "target_pallet_id": movement.target_pallet_id,
            "target_box_id": movement.target_box_id,
            "source_balance_id": movement.source_balance_id,
            "responsible_user_id": actor.id if actor else None,
            "responsible_user_name": str(
                actor.get_full_name() or actor.get_username() or actor.id
            ) if actor else "",
            "source_location_code": str(source_location.location_code or ""),
            "destination_location_code": str(target_location.location_code or ""),
            "physical_place_confirmed_at": timezone.localtime(now).isoformat(),
        },
        performed_by=actor,
        performed_by_role="reachtruck",
        occurred_at=now,
    )
    movement.save(update_fields=["moved_qty", "status", "completed_at", "updated_at"])
    if movement.mode == FbsInternalMovement.MODE_ITEM:
        # A non-rack FBS box must stop occupying its physical place as soon as
        # the last unit is moved out.  The shared release helper also verifies
        # warehouse stock, active allocations, external issues and child tare
        # before archiving the box and detaching its warehouse container.
        from .picking import _archive_empty_fbs_box_after_pick

        _archive_empty_fbs_box_after_pick(
            box_id=movement.source_box_id,
            performed_by=actor,
            occurred_at=now,
        )
    return movement
