from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from django.db import transaction
from django.db.models import Count, F, Min, Q, Sum
from django.utils import timezone

from fbs.exceptions import FbsMovementError
from fbs.models import (
    FbsBox,
    FbsInternalMovement,
    FbsOrder,
    FbsOrderItem,
    FbsOrderStockAllocation,
    FbsPallet,
    FbsStockBalance,
)

from .inventory import with_fbs_lock_state
from .movements import (
    FLOOR_REPLENISHMENT_COMMENT_PREFIX,
    create_internal_movement,
)


FIRST_PICK_TIER = 1
FLOOR_SOURCE_MIN_TIER = 2
FLOOR_ORDER_STATUSES = (
    FbsOrder.STATUS_RECEIVED,
    FbsOrder.STATUS_AWAITING_STOCK,
)
OPEN_MOVEMENT_STATUSES = (
    FbsInternalMovement.STATUS_PROPOSED,
    FbsInternalMovement.STATUS_IN_PROGRESS,
)
ACTIVE_BOX_STATUSES = (FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE)
ACTIVE_PALLET_STATUSES = (FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE)


@dataclass(frozen=True)
class FbsFloorMoveSuggestion:
    agency_id: int
    agency_name: str
    barcode: str
    sku_code: str
    sku_name: str
    order_count: int
    nearest_cutoff: object
    demand_qty: int
    first_floor_available_qty: int
    pending_move_qty: int
    needed_qty: int
    covered_qty: int
    source_box_id: int
    source_box_code: str
    source_pallet_code: str
    source_location: str
    source_tier: int
    target_pallet_id: int
    target_pallet_code: str
    target_location: str


@dataclass(frozen=True)
class FbsFloorMoveBlocker:
    agency_id: int
    agency_name: str
    barcode: str
    sku_code: str
    needed_qty: int
    reason: str


@dataclass(frozen=True)
class FbsFloorReplenishmentAnalysis:
    suggestions: tuple[FbsFloorMoveSuggestion, ...]
    blockers: tuple[FbsFloorMoveBlocker, ...]
    demanded_qty: int
    first_floor_shortage_qty: int
    pending_move_qty: int


@dataclass(frozen=True)
class FbsFloorMovementCreationResult:
    movement: FbsInternalMovement
    task: object


def _normalized(value: str) -> str:
    return str(value or "").strip()


def _active_balance_queryset():
    return FbsStockBalance.objects.filter(
        available_qty__gt=0,
        box__status__in=ACTIVE_BOX_STATUSES,
        box__pallet__status__in=ACTIVE_PALLET_STATUSES,
        box__pallet__cell__is_active=True,
    ).filter(Q(expiry_date__isnull=True) | Q(expiry_date__gte=timezone.localdate()))


def _floor_available_by_key(keys: set[tuple[int, str]]) -> dict[tuple[int, str], int]:
    if not keys:
        return {}
    agency_ids = {key[0] for key in keys}
    barcodes = {key[1] for key in keys}
    rows = (
        with_fbs_lock_state(
            _active_balance_queryset().filter(
                agency_id__in=agency_ids,
                barcode__in=barcodes,
                box__pallet__cell__location__tier_no=FIRST_PICK_TIER,
            )
        )
        .filter(_fbs_is_locked=False)
        .values("agency_id", "barcode")
        .annotate(qty=Sum("available_qty"))
    )
    return {
        (int(row["agency_id"]), _normalized(row["barcode"])): int(row["qty"] or 0)
        for row in rows
    }


def _pending_move_coverage(
    keys: set[tuple[int, str]],
) -> tuple[dict[tuple[int, str], int], set[int]]:
    movements = list(
        FbsInternalMovement.objects.filter(
            status__in=OPEN_MOVEMENT_STATUSES,
            comment__startswith=FLOOR_REPLENISHMENT_COMMENT_PREFIX,
        ).values_list("source_box_id", flat=True)
    )
    source_box_ids = {int(value) for value in movements}
    if not source_box_ids or not keys:
        return {}, source_box_ids
    rows = (
        FbsStockBalance.objects.filter(
            box_id__in=source_box_ids,
            qty__gt=0,
        )
        .values("agency_id", "barcode")
        .annotate(qty=Sum("available_qty"))
    )
    coverage = defaultdict(int)
    for row in rows:
        key = (int(row["agency_id"]), _normalized(row["barcode"]))
        if key in keys:
            coverage[key] += int(row["qty"] or 0)
    return dict(coverage), source_box_ids


def _target_pallets_by_agency(agency_ids: set[int]):
    pallets = list(
        FbsPallet.objects.filter(
            agency_id__in=agency_ids,
            status__in=ACTIVE_PALLET_STATUSES,
            cell__is_active=True,
            cell__location__tier_no=FIRST_PICK_TIER,
        )
        .select_related("agency", "cell__location")
        .annotate(
            active_box_count=Count(
                "boxes",
                filter=Q(boxes__status__in=ACTIVE_BOX_STATUSES),
            ),
            open_incoming_count=Count(
                "incoming_fbs_movements",
                filter=Q(
                    incoming_fbs_movements__status__in=OPEN_MOVEMENT_STATUSES,
                    incoming_fbs_movements__mode=FbsInternalMovement.MODE_BOX,
                    incoming_fbs_movements__comment__startswith=(
                        FLOOR_REPLENISHMENT_COMMENT_PREFIX
                    ),
                ),
                distinct=True,
            ),
        )
        .order_by(
            "agency_id",
            "cell__location__section_no",
            "cell__location__row_no",
            "cell__location__cell_no",
            "id",
        )
    )
    result = defaultdict(list)
    for pallet in pallets:
        free_slots = max(
            int(pallet.max_boxes or 0)
            - int(pallet.active_box_count or 0)
            - int(pallet.open_incoming_count or 0),
            0,
        )
        if free_slots:
            result[pallet.agency_id].append([pallet, free_slots])
    return result


def analyze_floor_replenishment_needs() -> FbsFloorReplenishmentAnalysis:
    demand_rows = list(
        FbsOrderItem.objects.filter(
            order__internal_status__in=FLOOR_ORDER_STATUSES,
            order__profile__is_active=True,
            sku_id__isnull=False,
        )
        .exclude(barcode="")
        .values(
            "order__profile__agency_id",
            "order__profile__agency__agn_name",
            "barcode",
            "sku__sku_code",
            "sku__name",
        )
        .annotate(
            demand_qty=Sum("quantity"),
            order_count=Count("order_id", distinct=True),
            nearest_cutoff=Min("order__cutoff_at"),
        )
        .order_by(
            F("nearest_cutoff").asc(nulls_last=True),
            "order__profile__agency__agn_name",
            "barcode",
        )
    )
    keys = {
        (int(row["order__profile__agency_id"]), _normalized(row["barcode"]))
        for row in demand_rows
    }
    first_floor = _floor_available_by_key(keys)
    pending_coverage, used_source_box_ids = _pending_move_coverage(keys)
    agency_ids = {key[0] for key in keys}
    targets_by_agency = _target_pallets_by_agency(agency_ids)

    active_allocation_box_ids = set(
        FbsOrderStockAllocation.objects.filter(
            status__in=(
                FbsOrderStockAllocation.STATUS_RESERVED,
                FbsOrderStockAllocation.STATUS_PICKING,
            )
        ).values_list("balance__box_id", flat=True)
    )
    reserved_box_ids = set(
        FbsStockBalance.objects.filter(reserved_qty__gt=0).values_list(
            "box_id", flat=True
        )
    )
    unavailable_source_box_ids = (
        used_source_box_ids | active_allocation_box_ids | reserved_box_ids
    )

    suggestions = []
    blockers = []
    demanded_qty = 0
    total_shortage = 0
    total_pending = 0
    selected_source_box_ids = set(used_source_box_ids)

    for row in demand_rows:
        agency_id = int(row["order__profile__agency_id"])
        barcode = _normalized(row["barcode"])
        key = (agency_id, barcode)
        demand_qty = int(row["demand_qty"] or 0)
        first_qty = int(first_floor.get(key, 0))
        pending_qty = int(pending_coverage.get(key, 0))
        shortage = max(demand_qty - first_qty - pending_qty, 0)
        demanded_qty += demand_qty
        total_shortage += shortage
        total_pending += min(pending_qty, max(demand_qty - first_qty, 0))
        if shortage <= 0:
            continue

        source_rows = list(
            with_fbs_lock_state(
                _active_balance_queryset()
                .filter(
                    agency_id=agency_id,
                    barcode=barcode,
                    box__pallet__cell__location__tier_no__gte=FLOOR_SOURCE_MIN_TIER,
                )
                .exclude(box_id__in=unavailable_source_box_ids | selected_source_box_ids)
                .select_related("box__pallet__cell__location", "sku_ref")
                .order_by(
                    F("expiry_date").asc(nulls_last=True),
                    "box__pallet__cell__location__tier_no",
                    "box__pallet__cell__cell_code",
                    "box__box_code",
                    "id",
                )
            ).filter(_fbs_is_locked=False)
        )
        sources_by_box = defaultdict(list)
        for balance in source_rows:
            sources_by_box[balance.box_id].append(balance)

        remaining = shortage
        if not sources_by_box:
            blockers.append(
                FbsFloorMoveBlocker(
                    agency_id=agency_id,
                    agency_name=str(row["order__profile__agency__agn_name"] or ""),
                    barcode=barcode,
                    sku_code=str(row["sku__sku_code"] or ""),
                    needed_qty=remaining,
                    reason="Нет свободного короба с товаром на верхних ярусах.",
                )
            )
            continue

        for box_id, balances in sources_by_box.items():
            if remaining <= 0:
                break
            target_slots = targets_by_agency.get(agency_id, [])
            target_entry = next((entry for entry in target_slots if entry[1] > 0), None)
            if target_entry is None:
                blockers.append(
                    FbsFloorMoveBlocker(
                        agency_id=agency_id,
                        agency_name=str(row["order__profile__agency__agn_name"] or ""),
                        barcode=barcode,
                        sku_code=str(row["sku__sku_code"] or ""),
                        needed_qty=remaining,
                        reason="На первом ярусе нет FBS-паллеты со свободным местом для короба.",
                    )
                )
                break
            target_pallet = target_entry[0]
            source_box = balances[0].box
            covered_qty = sum(int(balance.available_qty or 0) for balance in balances)
            if covered_qty <= 0:
                continue
            suggestions.append(
                FbsFloorMoveSuggestion(
                    agency_id=agency_id,
                    agency_name=str(row["order__profile__agency__agn_name"] or ""),
                    barcode=barcode,
                    sku_code=str(row["sku__sku_code"] or balances[0].sku_code or ""),
                    sku_name=str(row["sku__name"] or balances[0].name or ""),
                    order_count=int(row["order_count"] or 0),
                    nearest_cutoff=row["nearest_cutoff"],
                    demand_qty=demand_qty,
                    first_floor_available_qty=first_qty,
                    pending_move_qty=pending_qty,
                    needed_qty=remaining,
                    covered_qty=covered_qty,
                    source_box_id=source_box.id,
                    source_box_code=source_box.box_code,
                    source_pallet_code=source_box.pallet.pallet_code,
                    source_location=source_box.pallet.cell.warehouse_location_code,
                    source_tier=int(source_box.pallet.cell.location.tier_no or 0),
                    target_pallet_id=target_pallet.id,
                    target_pallet_code=target_pallet.pallet_code,
                    target_location=target_pallet.cell.warehouse_location_code,
                )
            )
            selected_source_box_ids.add(box_id)
            target_entry[1] -= 1
            remaining -= covered_qty

        if remaining > 0 and not any(
            blocker.agency_id == agency_id
            and blocker.barcode == barcode
            and blocker.needed_qty == remaining
            for blocker in blockers
        ):
            blockers.append(
                FbsFloorMoveBlocker(
                    agency_id=agency_id,
                    agency_name=str(row["order__profile__agency__agn_name"] or ""),
                    barcode=barcode,
                    sku_code=str(row["sku__sku_code"] or ""),
                    needed_qty=remaining,
                    reason="Верхнего свободного остатка недостаточно для потребности.",
                )
            )

    return FbsFloorReplenishmentAnalysis(
        suggestions=tuple(suggestions),
        blockers=tuple(blockers),
        demanded_qty=demanded_qty,
        first_floor_shortage_qty=total_shortage,
        pending_move_qty=total_pending,
    )


@transaction.atomic
def create_floor_replenishment_movement(
    *,
    source_box_id: int,
    target_pallet_id: int,
    barcode: str,
    needed_qty: int,
    requested_by,
    comment: str = "",
) -> FbsFloorMovementCreationResult:
    try:
        source_box = (
            FbsBox.objects.select_for_update()
            .select_related("agency", "pallet__cell__location")
            .get(pk=source_box_id)
        )
        target_pallet = (
            FbsPallet.objects.select_for_update()
            .select_related("cell__location")
            .get(pk=target_pallet_id)
        )
    except (FbsBox.DoesNotExist, FbsPallet.DoesNotExist) as exc:
        raise FbsMovementError("Короб или паллета заявки больше не существует.") from exc
    normalized_barcode = _normalized(barcode)
    if not normalized_barcode:
        raise FbsMovementError("Не указан штрихкод потребности первого яруса.")
    if not source_box.stock_balances.filter(
        barcode=normalized_barcode,
        available_qty__gt=0,
    ).exists():
        raise FbsMovementError("В выбранном верхнем коробе больше нет нужного товара.")
    if FbsInternalMovement.objects.filter(
        source_box=source_box,
        status__in=OPEN_MOVEMENT_STATUSES,
        comment__startswith=FLOOR_REPLENISHMENT_COMMENT_PREFIX,
    ).exists():
        raise FbsMovementError("Этот короб уже включен в заявку на первый ярус.")
    instruction = str(comment or "").strip()
    if not instruction:
        instruction = (
            f"Забрать короб {source_box.box_code} с "
            f"{source_box.pallet.cell.warehouse_location_code} и переместить на "
            f"{target_pallet.cell.warehouse_location_code}."
        )
    movement = create_internal_movement(
        mode=FbsInternalMovement.MODE_BOX,
        source_box=source_box,
        target_pallet=target_pallet,
        requested_by=requested_by,
        comment=(
            f"{FLOOR_REPLENISHMENT_COMMENT_PREFIX} ШК {normalized_barcode}; "
            f"потребность {max(int(needed_qty or 0), 1)} шт. {instruction}"
        )[:2000],
    )
    from .reachtruck_bridge import sync_internal_floor_movement_reachtruck_task

    task = sync_internal_floor_movement_reachtruck_task(movement)
    return FbsFloorMovementCreationResult(movement=movement, task=task)
