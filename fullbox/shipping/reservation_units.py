from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from sklad.models import WarehouseStockSnapshot
from sklad.services.warehouse_stock_rows import normalize_stock_row_from_snapshot
from sklad.services.warehouse_transitions import WarehouseStateCode

from .models import ShippingOrder, ShippingReservationUnit


ACTIVE_RESERVATION_STATUSES = (
    ShippingReservationUnit.STATUS_RESERVED,
    ShippingReservationUnit.STATUS_TASK_CREATED,
    ShippingReservationUnit.STATUS_PICKED,
    ShippingReservationUnit.STATUS_IN_OTG,
)

SHIPPING_FLOW_STATE_CODES = (
    WarehouseStateCode.IN_OTG.value,
    WarehouseStateCode.PALLETIZING.value,
    WarehouseStateCode.READY_FOR_LOADING.value,
    WarehouseStateCode.ASSIGNED_TO_TRIP.value,
    WarehouseStateCode.LOADING_IN_PROGRESS.value,
    WarehouseStateCode.LOADED_TO_VEHICLE.value,
    WarehouseStateCode.SHIPPED.value,
)


def _clean(value) -> str:
    return str(value or "").strip()


def _lower(value) -> str:
    return _clean(value).lower()


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _box_code_from_snapshot(snapshot: WarehouseStockSnapshot) -> str:
    row = normalize_stock_row_from_snapshot(snapshot)
    return _clean(row.get("box_code") or row.get("container_code") or snapshot.container_code)


def _pallet_code_from_snapshot(snapshot: WarehouseStockSnapshot) -> str:
    row = normalize_stock_row_from_snapshot(snapshot)
    parent = getattr(snapshot, "parent_container", None)
    parent_code = getattr(parent, "container_code", "") if parent is not None else ""
    return _clean(row.get("pallet_code") or row.get("parent_container_code") or parent_code)


def _location_from_snapshot(snapshot: WarehouseStockSnapshot) -> str:
    row = normalize_stock_row_from_snapshot(snapshot)
    location = row.get("location") or row.get("location_label") or row.get("address") or ""
    if location:
        return _clean(location)
    obj = getattr(snapshot, "location", None)
    return _clean(obj) if obj is not None else ""


def _zone_from_snapshot(snapshot: WarehouseStockSnapshot) -> str:
    row = normalize_stock_row_from_snapshot(snapshot)
    return _clean(row.get("zone_code") or row.get("warehouse_state_code") or getattr(snapshot, "warehouse_state_code", ""))


def _row_from_snapshot(snapshot: WarehouseStockSnapshot) -> dict:
    row = normalize_stock_row_from_snapshot(snapshot)
    row["snapshot_id"] = snapshot.id
    row["box_code"] = _box_code_from_snapshot(snapshot)
    row["pallet_code"] = _pallet_code_from_snapshot(snapshot)
    row["source_location"] = _location_from_snapshot(snapshot)
    row["source_zone"] = _zone_from_snapshot(snapshot)
    return row


def _snapshot_matches_unit(snapshot: WarehouseStockSnapshot, unit: ShippingReservationUnit) -> bool:
    if _lower(snapshot.sku_code) != _lower(unit.sku_code):
        return False
    if _lower(getattr(snapshot, "size", "")) != _lower(unit.size):
        return False
    if _lower(getattr(snapshot, "barcode", "")) != _lower(unit.barcode):
        return False
    if _lower(getattr(snapshot, "goods_type", "")) != _lower(unit.goods_type):
        return False
    qty_per_box = _as_int(getattr(unit, "qty_per_box", 0))
    if qty_per_box and _as_int(getattr(snapshot, "qty", 0)) != qty_per_box:
        return False
    return True


def _snapshots_for_box_codes(order: ShippingOrder, box_codes: Iterable[str]) -> dict[str, WarehouseStockSnapshot]:
    codes = sorted({_clean(code) for code in box_codes if _clean(code)})
    if not codes:
        return {}
    snapshots = (
        WarehouseStockSnapshot.objects.filter(
            agency=order.agency,
            container_code__in=codes,
            is_archived=False,
            qty__gt=0,
        )
        .select_related("parent_container", "location")
        .order_by("-id")
    )
    result: dict[str, WarehouseStockSnapshot] = {}
    for snapshot in snapshots:
        code = _box_code_from_snapshot(snapshot)
        if code and code not in result:
            result[code] = snapshot
    return result


def _active_pallet_snapshots(order: ShippingOrder, pallet_code: str):
    if not pallet_code:
        return WarehouseStockSnapshot.objects.none()
    return (
        WarehouseStockSnapshot.objects.filter(
            agency=order.agency,
            parent_container__container_code=pallet_code,
            qty__gt=0,
        )
        .select_related("parent_container", "location")
        .order_by("id")
    )


def _selected_box_codes_by_item_id(reserve_box_plan: dict) -> dict[int, list[str]]:
    raw = (reserve_box_plan or {}).get("selected_box_codes_by_item_id") or {}
    result: dict[int, list[str]] = {}
    for item_id, codes in raw.items():
        try:
            key = int(item_id)
        except (TypeError, ValueError):
            continue
        result[key] = [_clean(code) for code in (codes or []) if _clean(code)]
    return result


def active_shipping_reservation_units(order: ShippingOrder):
    return order.reservation_units.filter(status__in=ACTIVE_RESERVATION_STATUSES).select_related("item")


def active_reserved_pallet_codes_for_agency(agency_id: int, pallet_codes: Iterable[str] | None = None) -> set[str]:
    qs = ShippingReservationUnit.objects.filter(
        agency_id=agency_id,
        status__in=ACTIVE_RESERVATION_STATUSES,
    ).exclude(pallet_code="")
    if pallet_codes is not None:
        normalized = {_clean(code) for code in pallet_codes if _clean(code)}
        if not normalized:
            return set()
        qs = qs.filter(pallet_code__in=normalized)
    return set(qs.values_list("pallet_code", flat=True))


def active_reserved_pallet_codes(pallet_codes: Iterable[str] | None = None) -> set[str]:
    qs = ShippingReservationUnit.objects.filter(status__in=ACTIVE_RESERVATION_STATUSES).exclude(pallet_code="")
    if pallet_codes is not None:
        normalized = {_clean(code) for code in pallet_codes if _clean(code)}
        if not normalized:
            return set()
        qs = qs.filter(pallet_code__in=normalized)
    return set(qs.values_list("pallet_code", flat=True))


@transaction.atomic
def release_shipping_reservation_units(order: ShippingOrder, user=None) -> int:
    now = timezone.now()
    return (
        order.reservation_units.filter(status__in=ACTIVE_RESERVATION_STATUSES)
        .update(status=ShippingReservationUnit.STATUS_RELEASED, released_by=user, updated_at=now)
    )


@transaction.atomic
def materialize_shipping_reservation_units(order: ShippingOrder, reserve_box_plan: dict, user=None) -> list[ShippingReservationUnit]:
    release_shipping_reservation_units(order, user=user)

    selected_by_item = _selected_box_codes_by_item_id(reserve_box_plan)
    created: list[ShippingReservationUnit] = []

    for item in order.items.all():
        selected_codes = selected_by_item.get(item.id) or []
        snapshots_by_box = _snapshots_for_box_codes(order, selected_codes)
        snapshots = [snapshots_by_box[code] for code in selected_codes if code in snapshots_by_box]

        if not snapshots:
            created.append(
                ShippingReservationUnit.objects.create(
                    order=order,
                    item=item,
                    agency=order.agency,
                    created_by=user,
                    reserve_mode=ShippingReservationUnit.MODE_LOOSE_QTY,
                    sku_code=item.sku_code,
                    name=item.name,
                    size=item.size,
                    barcode=item.barcode,
                    goods_type=item.goods_type,
                    qty_required=_as_int(item.qty_requested),
                    payload={"reason": "no_selected_live_boxes"},
                )
            )
            continue

        grouped: dict[str, list[WarehouseStockSnapshot]] = defaultdict(list)
        for snapshot in snapshots:
            grouped[_pallet_code_from_snapshot(snapshot)].append(snapshot)

        for pallet_code, pallet_snapshots in grouped.items():
            box_codes = [_box_code_from_snapshot(snapshot) for snapshot in pallet_snapshots if _box_code_from_snapshot(snapshot)]
            first = pallet_snapshots[0]
            qty_per_box = _as_int(getattr(first, "qty", 0))
            active_pallet_boxes = list(_active_pallet_snapshots(order, pallet_code)) if pallet_code else []
            matching_active = [snapshot for snapshot in active_pallet_boxes if _snapshot_matches_unit(snapshot, item)]
            selected_is_whole_pallet = bool(pallet_code and active_pallet_boxes and len(box_codes) >= len(active_pallet_boxes))
            selected_is_whole_matching_set = bool(
                pallet_code and matching_active and len(box_codes) >= len(matching_active)
            )
            reserve_mode = (
                ShippingReservationUnit.MODE_FULL_PALLET
                if selected_is_whole_pallet or selected_is_whole_matching_set
                else ShippingReservationUnit.MODE_PALLET_QUOTA
            )

            created.append(
                ShippingReservationUnit.objects.create(
                    order=order,
                    item=item,
                    agency=order.agency,
                    created_by=user,
                    reserve_mode=reserve_mode,
                    sku_code=item.sku_code,
                    name=item.name,
                    size=item.size,
                    barcode=item.barcode,
                    goods_type=item.goods_type,
                    qty_per_box=qty_per_box,
                    boxes_required=len(box_codes),
                    qty_required=sum(_as_int(getattr(snapshot, "qty", 0)) for snapshot in pallet_snapshots),
                    pallet_code=pallet_code,
                    snapshot_id=first.id if reserve_mode == ShippingReservationUnit.MODE_FIXED_BOX else None,
                    source_location=_location_from_snapshot(first),
                    source_zone=_zone_from_snapshot(first),
                    payload={
                        "planned_box_codes": box_codes,
                        "planned_snapshot_ids": [snapshot.id for snapshot in pallet_snapshots],
                        "whole_pallet": selected_is_whole_pallet,
                    },
                )
            )

    return created


def reservation_units_as_planning_rows(order: ShippingOrder) -> list[dict]:
    units = list(active_shipping_reservation_units(order))
    if not units:
        return []

    rows: list[dict] = []
    for unit in units:
        if unit.reserve_mode == ShippingReservationUnit.MODE_LOOSE_QTY:
            continue

        snapshots = WarehouseStockSnapshot.objects.filter(
            agency=order.agency,
            is_archived=False,
            qty__gt=0,
        ).exclude(warehouse_state_code__in=SHIPPING_FLOW_STATE_CODES)
        if unit.box_code:
            snapshots = snapshots.filter(container_code=unit.box_code)
        elif unit.pallet_code:
            snapshots = snapshots.filter(parent_container__container_code=unit.pallet_code)
        else:
            continue

        snapshots = snapshots.select_related("parent_container", "location").order_by("id")
        if unit.reserve_mode == ShippingReservationUnit.MODE_FULL_PALLET:
            planned_codes = set(unit.payload.get("planned_box_codes") or [])
            for snapshot in snapshots:
                box_code = _box_code_from_snapshot(snapshot)
                if planned_codes and box_code not in planned_codes:
                    continue
                if _snapshot_matches_unit(snapshot, unit):
                    rows.append(_row_from_snapshot(snapshot))
            continue

        matched = []
        for snapshot in snapshots:
            if _snapshot_matches_unit(snapshot, unit):
                matched.append(snapshot)
        limit = unit.boxes_required or len(matched)
        for snapshot in matched[:limit]:
            rows.append(_row_from_snapshot(snapshot))

    return rows


@transaction.atomic
def mark_reservation_units_arrived_to_otg(order: ShippingOrder, box_codes: Iterable[str], user=None) -> int:
    codes = [_clean(code) for code in box_codes if _clean(code)]
    if not codes:
        return 0

    snapshots_by_box = _snapshots_for_box_codes(order, codes)
    changed = 0
    units = list(active_shipping_reservation_units(order))
    now = timezone.now()

    for code in codes:
        snapshot = snapshots_by_box.get(code)
        if snapshot is None:
            continue
        matched_unit = None
        for unit in units:
            if unit.box_code and _lower(unit.box_code) == _lower(code):
                matched_unit = unit
                break
            if unit.pallet_code and _lower(unit.pallet_code) == _lower(_pallet_code_from_snapshot(snapshot)):
                if _snapshot_matches_unit(snapshot, unit):
                    matched_unit = unit
                    break
        if matched_unit is None:
            continue
        payload = dict(matched_unit.payload or {})
        actual_codes = payload.get("actual_box_codes") or []
        if code not in actual_codes:
            actual_codes.append(code)
        payload["actual_box_codes"] = actual_codes
        matched_unit.payload = payload
        if matched_unit.boxes_required and len(actual_codes) < matched_unit.boxes_required:
            matched_unit.status = ShippingReservationUnit.STATUS_PICKED
        else:
            matched_unit.status = ShippingReservationUnit.STATUS_IN_OTG
        matched_unit.updated_at = now
        matched_unit.save(update_fields=["payload", "status", "updated_at"])
        changed += 1

    return changed
