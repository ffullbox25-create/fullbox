from __future__ import annotations

import re

from django.db import models
from sku.models import Agency, SKU

from sklad.models import (
    WarehouseContainer,
    WarehouseOperation,
    WarehouseReserve,
    WarehouseStockSnapshot,
    WarehouseTemporaryNomenclature,
)
from sklad.services.putaway_draft_reservations import PutawayDraftReservationService
from sklad.location_occupancy import fbs_storage_occupied_rows
from sklad.services.temporary_nomenclature import temporary_nomenclature_enabled
from sklad.services.warehouse_transitions import WarehouseStateCode
from sklad.services.warehouse_stock_rows import snapshot_stock_rows

GOODS_TYPE_ALIASES = {
    "op": "оптовый",
    "gv": "готовый",
    "no": "не обработанный",
    "br": "брак",
    "vz": "возврат",
    "votg": "возврат с отгрузки",
    "rh": "расходный",
    "необработанный": "не обработанный",
    "возврат с отгрузки": "возврат с отгрузки",
}
_PALLET_CONTAINER_TYPES = {
    WarehouseContainer.TYPE_PALLET,
    WarehouseContainer.TYPE_MIXED_PALLET,
}
_ACTIVE_OS_RESERVATION_STATUSES = (
    WarehouseOperation.STATUS_CREATED,
    WarehouseOperation.STATUS_PLANNED,
    WarehouseOperation.STATUS_IN_PROGRESS,
    WarehouseOperation.STATUS_PARTIAL,
    WarehouseOperation.STATUS_BLOCKED,
)
_PROCESSING_STOCK_STATES = {
    WarehouseStateCode.MOVING_TO_PROCESSING.value,
    WarehouseStateCode.IN_PROCESSING_ZONE.value,
    WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
}
_CONSUMED_STOCK_STATES = {
    WarehouseStateCode.PROCESSING_CONSUMED.value,
}
_NON_OCCUPYING_OS_STATES = {
    "processing_consumed",
    "shipped",
    "canceled",
}
_OTG_STOCK_STATES = {
    WarehouseStateCode.MOVING_TO_OTG.value,
    WarehouseStateCode.IN_OTG.value,
    WarehouseStateCode.PALLETIZING.value,
    WarehouseStateCode.READY_FOR_LOADING.value,
    WarehouseStateCode.ASSIGNED_TO_TRIP.value,
    WarehouseStateCode.LOADING_IN_PROGRESS.value,
}
_SHIPPING_BOX_CODES_RE = re.compile(r"(?:исходные\s+)?короба:\s*([^;]+)", re.IGNORECASE)


def normalize_goods_type(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if not text or text == "-":
        return ""
    return GOODS_TYPE_ALIASES.get(text, text)


def _stock_bucket_for_row(row: dict) -> str:
    zone_code = str(row.get("zone") or row.get("zone_code") or "").strip().upper()
    state_code = str(row.get("warehouse_state_code") or "").strip().lower()
    if state_code in _CONSUMED_STOCK_STATES:
        return ""
    if zone_code == "OTG" or state_code in _OTG_STOCK_STATES:
        return "OTG"
    if zone_code == "OBR" or state_code in _PROCESSING_STOCK_STATES:
        return "OBR"
    if zone_code == "OS":
        return "OS"
    return zone_code


def build_processing_reserve_maps(
    agency: Agency | None,
    exclude_processing_order_id: str | None = None,
) -> tuple[dict[tuple[str, str, str], int], dict[tuple[str, str], int]]:
    if not agency:
        return {}, {}
    warehouse_reserves = WarehouseReserve.objects.filter(
        agency=agency,
        reserve_type=WarehouseReserve.TYPE_PROCESSING,
    ).exclude(
        status__in=[
            WarehouseReserve.STATUS_RELEASED,
            WarehouseReserve.STATUS_CANCELED,
            WarehouseReserve.STATUS_SATISFIED,
        ]
)
    if exclude_processing_order_id:
        warehouse_reserves = warehouse_reserves.exclude(
            context_type="processing",
            context_id=str(exclude_processing_order_id),
        )

    reserve_map: dict[tuple[str, str, str], int] = {}
    reserve_any: dict[tuple[str, str], int] = {}
    for entry in warehouse_reserves:
        sku = (entry.sku_code or "").strip()
        if not sku:
            continue
        outstanding_qty = max(int(entry.qty_reserved or 0) - int(entry.qty_satisfied or 0), 0)
        if outstanding_qty <= 0:
            continue
        size = (entry.size or "").strip()
        goods_key = normalize_goods_type(entry.goods_type)
        key = (sku.lower(), size.lower(), goods_key)
        reserve_map[key] = reserve_map.get(key, 0) + outstanding_qty
        any_key = (sku.lower(), size.lower())
        reserve_any[any_key] = reserve_any.get(any_key, 0) + outstanding_qty
    return reserve_map, reserve_any


def build_shipping_reserve_maps(
    agency: Agency | None,
    exclude_shipping_order_id: str | None = None,
) -> tuple[dict[tuple[str, str, str], int], dict[tuple[str, str], int]]:
    if not agency:
        return {}, {}
    reserves = WarehouseReserve.objects.filter(
        agency=agency,
        reserve_type=WarehouseReserve.TYPE_SHIPPING,
    ).exclude(
        status__in=[
            WarehouseReserve.STATUS_RELEASED,
            WarehouseReserve.STATUS_CANCELED,
            WarehouseReserve.STATUS_SATISFIED,
        ]
    )
    if exclude_shipping_order_id:
        reserves = reserves.exclude(context_type="shipping", context_id=str(exclude_shipping_order_id))
    reserve_map: dict[tuple[str, str, str], int] = {}
    reserve_any: dict[tuple[str, str], int] = {}
    for entry in reserves:
        sku = (entry.sku_code or "").strip()
        if not sku:
            continue
        size = (entry.size or "").strip()
        goods_key = normalize_goods_type(entry.goods_type)
        qty = max(int(entry.qty_reserved or 0) - int(entry.qty_satisfied or 0), 0)
        if qty <= 0:
            continue
        key = (sku.lower(), size.lower(), goods_key)
        reserve_map[key] = reserve_map.get(key, 0) + qty
        any_key = (sku.lower(), size.lower())
        reserve_any[any_key] = reserve_any.get(any_key, 0) + qty
    return reserve_map, reserve_any


def reserved_processing_qty(
    reserve_map: dict[tuple[str, str, str], int],
    reserve_any: dict[tuple[str, str], int],
    sku: str,
    size: str,
    goods_type: str | None = None,
) -> int:
    sku_key = (sku or "").strip().lower()
    if not sku_key:
        return 0
    size_key = (size or "").strip().lower()
    goods_key = normalize_goods_type(goods_type)
    if not goods_key:
        return reserve_any.get((sku_key, size_key), 0)
    return reserve_map.get((sku_key, size_key, goods_key), 0) + reserve_map.get(
        (sku_key, size_key, ""),
        0,
    )


def _normalize_barcode_value(value) -> str:
    text = str(value or "").strip()
    if text in {"-", "–", "—"}:
        return ""
    return text


def _stock_truth_exact_key(
    *,
    agency_id: int | None,
    sku: str | None,
    size: str | None,
    goods_type: str | None,
    barcode: str | None,
) -> tuple[int, str, str, str, str] | None:
    sku_key = (sku or "").strip().lower()
    if not sku_key:
        return None
    goods_key = normalize_goods_type(goods_type)
    if not goods_key:
        return None
    return (
        int(agency_id or 0),
        sku_key,
        (size or "").strip().lower(),
        goods_key,
        _normalize_barcode_value(barcode).lower(),
    )


def _stock_truth_exact_group_key(
    *,
    agency_id: int | None,
    sku: str | None,
    size: str | None,
    goods_type: str | None,
) -> tuple[int, str, str, str] | None:
    sku_key = (sku or "").strip().lower()
    if not sku_key:
        return None
    goods_key = normalize_goods_type(goods_type)
    if not goods_key:
        return None
    return (
        int(agency_id or 0),
        sku_key,
        (size or "").strip().lower(),
        goods_key,
    )


def _stock_truth_any_key(
    *,
    agency_id: int | None,
    sku: str | None,
    size: str | None,
    barcode: str | None,
) -> tuple[int, str, str, str] | None:
    sku_key = (sku or "").strip().lower()
    if not sku_key:
        return None
    return (
        int(agency_id or 0),
        sku_key,
        (size or "").strip().lower(),
        _normalize_barcode_value(barcode).lower(),
    )


def _stock_truth_any_group_key(
    *,
    agency_id: int | None,
    sku: str | None,
    size: str | None,
) -> tuple[int, str, str] | None:
    sku_key = (sku or "").strip().lower()
    if not sku_key:
        return None
    return (
        int(agency_id or 0),
        sku_key,
        (size or "").strip().lower(),
    )


def _reserve_truth_maps_from_queryset(
    reserves,
) -> tuple[
    dict[tuple[int, str, str, str, str], int],
    dict[tuple[int, str, str, str], int],
    dict[tuple[int, str, str, str], int],
    dict[tuple[int, str, str], int],
]:
    exact_map: dict[tuple[int, str, str, str, str], int] = {}
    exact_group_map: dict[tuple[int, str, str, str], int] = {}
    any_goods_map: dict[tuple[int, str, str, str], int] = {}
    any_goods_group_map: dict[tuple[int, str, str], int] = {}
    for entry in reserves:
        qty = max(int(entry.qty_reserved or 0) - int(entry.qty_satisfied or 0), 0)
        if qty <= 0:
            continue
        barcode_value = _normalize_barcode_value(entry.barcode)
        exact_group_key = _stock_truth_exact_group_key(
            agency_id=entry.agency_id,
            sku=entry.sku_code,
            size=entry.size,
            goods_type=entry.goods_type,
        )
        if exact_group_key is not None:
            if barcode_value:
                exact_key = (*exact_group_key, barcode_value.lower())
                exact_map[exact_key] = exact_map.get(exact_key, 0) + qty
            else:
                exact_group_map[exact_group_key] = exact_group_map.get(exact_group_key, 0) + qty
            continue
        any_group_key = _stock_truth_any_group_key(
            agency_id=entry.agency_id,
            sku=entry.sku_code,
            size=entry.size,
        )
        if any_group_key is None:
            continue
        if barcode_value:
            any_key = (*any_group_key, barcode_value.lower())
            any_goods_map[any_key] = any_goods_map.get(any_key, 0) + qty
        else:
            any_goods_group_map[any_group_key] = any_goods_group_map.get(any_group_key, 0) + qty
    return exact_map, exact_group_map, any_goods_map, any_goods_group_map


def _active_reserve_truth_queryset(
    *,
    agency: Agency | None = None,
    agency_id: int | None = None,
    reserve_type: str,
    exclude_context_type: str | None = None,
    exclude_context_id: str | None = None,
):
    reserves = WarehouseReserve.objects.filter(reserve_type=reserve_type).exclude(
        status__in=[
            WarehouseReserve.STATUS_RELEASED,
            WarehouseReserve.STATUS_CANCELED,
            WarehouseReserve.STATUS_SATISFIED,
        ]
    )
    if agency_id:
        reserves = reserves.filter(agency_id=int(agency_id))
    elif agency is not None:
        reserves = reserves.filter(agency=agency)
    if exclude_context_type and exclude_context_id:
        reserves = reserves.exclude(
            context_type=str(exclude_context_type),
            context_id=str(exclude_context_id),
        )
    return reserves


def _build_active_reserve_truth_maps(
    *,
    agency: Agency | None = None,
    agency_id: int | None = None,
    reserve_type: str,
    exclude_context_type: str | None = None,
    exclude_context_id: str | None = None,
) -> tuple[
    dict[tuple[int, str, str, str, str], int],
    dict[tuple[int, str, str, str], int],
    dict[tuple[int, str, str, str], int],
    dict[tuple[int, str, str], int],
]:
    reserves = _active_reserve_truth_queryset(
        agency=agency,
        agency_id=agency_id,
        reserve_type=reserve_type,
        exclude_context_type=exclude_context_type,
        exclude_context_id=exclude_context_id,
    )
    return _reserve_truth_maps_from_queryset(reserves)


def _shipping_item_box_codes(comment: str | None) -> list[str]:
    from shipping.box_splits import extract_partial_box_split

    split_meta = extract_partial_box_split(comment)
    raw_codes = split_meta.get("source_box_codes") if split_meta else []
    if not raw_codes:
        match = _SHIPPING_BOX_CODES_RE.search(str(comment or ""))
        raw_codes = match.group(1).split(",") if match else []
    codes: list[str] = []
    seen: set[str] = set()
    for raw_code in raw_codes or []:
        code = str(raw_code or "").strip()
        normalized = code.casefold()
        if not code or normalized in seen:
            continue
        seen.add(normalized)
        codes.append(code)
    return codes


def _shipping_reserve_matches_item(reserve: WarehouseReserve, item) -> bool:
    if str(reserve.sku_code or "").strip().casefold() != str(item.sku_code or "").strip().casefold():
        return False
    if str(reserve.size or "").strip().casefold() != str(item.size or "").strip().casefold():
        return False
    reserve_barcode = _normalize_barcode_value(reserve.barcode).casefold()
    item_barcode = _normalize_barcode_value(item.barcode).casefold()
    if reserve_barcode and reserve_barcode != item_barcode:
        return False
    reserve_goods = normalize_goods_type(reserve.goods_type)
    item_goods = normalize_goods_type(item.goods_type)
    return not reserve_goods or reserve_goods == item_goods


def _shipping_pool_box_claims(reserves: list[WarehouseReserve]) -> list[tuple[WarehouseReserve, list[str], int]]:
    pool_reserves: list[tuple[WarehouseReserve, list[str], int]] = []
    contexts_needing_fallback: set[str] = set()
    for reserve in reserves:
        outstanding_qty = max(int(reserve.qty_reserved or 0) - int(reserve.qty_satisfied or 0), 0)
        if outstanding_qty <= 0:
            continue
        is_pool = False
        event_codes: list[str] = []
        for event in reserve.events.all():
            payload = event.payload if isinstance(event.payload, dict) else {}
            if str(payload.get("reserve_scope") or "").strip() != "pool":
                continue
            is_pool = True
            for raw_code in payload.get("box_codes") or []:
                code = str(raw_code or "").strip()
                if code and code.casefold() not in {value.casefold() for value in event_codes}:
                    event_codes.append(code)
        if not is_pool:
            continue
        if event_codes:
            pool_reserves.append((reserve, event_codes, outstanding_qty))
        else:
            context_id = str(reserve.context_id or "").strip()
            if context_id:
                contexts_needing_fallback.add(context_id)
            pool_reserves.append((reserve, [], outstanding_qty))

    if not contexts_needing_fallback:
        return pool_reserves

    from shipping.models import ShippingOrderItem

    fallback_items: dict[str, list[dict]] = {}
    for item in (
        ShippingOrderItem.objects.filter(
            order__number__in=contexts_needing_fallback,
            qty_reserved__gt=0,
        )
        .select_related("order")
        .order_by("id")
    ):
        codes = _shipping_item_box_codes(item.comment)
        if not codes:
            continue
        fallback_items.setdefault(str(item.order.number or "").strip(), []).append(
            {
                "item": item,
                "box_codes": codes,
                "remaining_qty": max(int(item.qty_reserved or 0), 0),
            }
        )

    resolved: list[tuple[WarehouseReserve, list[str], int]] = []
    for reserve, event_codes, outstanding_qty in pool_reserves:
        if event_codes:
            resolved.append((reserve, event_codes, outstanding_qty))
            continue
        remaining_qty = outstanding_qty
        for candidate in fallback_items.get(str(reserve.context_id or "").strip(), []):
            if remaining_qty <= 0:
                break
            item = candidate["item"]
            item_remaining = max(int(candidate.get("remaining_qty") or 0), 0)
            if item_remaining <= 0 or not _shipping_reserve_matches_item(reserve, item):
                continue
            claimed_qty = min(remaining_qty, item_remaining)
            resolved.append((reserve, list(candidate["box_codes"]), claimed_qty))
            candidate["remaining_qty"] = item_remaining - claimed_qty
            remaining_qty -= claimed_qty
    return resolved


def _shipping_reserve_box_codes_for_context(
    *,
    agency: Agency | None = None,
    agency_id: int | None = None,
    context_id: str,
) -> set[str]:
    reserves = list(
        _active_reserve_truth_queryset(
            agency=agency,
            agency_id=agency_id,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
        )
        .filter(context_type="shipping", context_id=str(context_id))
        .prefetch_related("events")
    )
    codes: set[str] = set()
    for _reserve, pool_codes, outstanding_qty in _shipping_pool_box_claims(reserves):
        if int(outstanding_qty or 0) <= 0:
            continue
        codes.update(str(code or "").strip() for code in pool_codes if str(code or "").strip())
    for reserve in reserves:
        for event in reserve.events.all():
            payload = event.payload if isinstance(event.payload, dict) else {}
            code = str(payload.get("box_code") or "").strip()
            if code:
                codes.add(code)
    return codes


def shipping_reserved_box_codes(
    *,
    agency: Agency | None = None,
    agency_id: int | None = None,
    box_codes: list[str] | tuple[str, ...] | set[str] | None = None,
    exclude_shipping_order_id: str | None = None,
) -> set[str]:
    """Return physical box codes that are unavailable because of shipping.

    Shipping pool reserves deliberately do not mutate snapshot counters.  Their
    concrete boxes live in the reserve event payload, so callers that work with
    physical boxes must consult both that payload and snapshot-backed reserves.
    """

    requested_keys = {
        str(code or "").strip().casefold()
        for code in (box_codes or [])
        if str(code or "").strip()
    }
    reserve_qs = _active_reserve_truth_queryset(
        agency=agency,
        agency_id=agency_id,
        reserve_type=WarehouseReserve.TYPE_SHIPPING,
        exclude_context_type="shipping" if exclude_shipping_order_id else None,
        exclude_context_id=exclude_shipping_order_id,
    ).prefetch_related("events")

    resolved_by_key: dict[str, str] = {}
    for _reserve, reserved_codes, outstanding_qty in _shipping_pool_box_claims(list(reserve_qs)):
        if int(outstanding_qty or 0) <= 0:
            continue
        for raw_code in reserved_codes:
            code = str(raw_code or "").strip()
            key = code.casefold()
            if code and (not requested_keys or key in requested_keys):
                resolved_by_key.setdefault(key, code)

    snapshot_qs = WarehouseStockSnapshot.objects.filter(
        shipping_reserved_qty__gt=0,
        is_archived=False,
    ).exclude(container_code="")
    if agency_id:
        snapshot_qs = snapshot_qs.filter(agency_id=int(agency_id))
    elif agency is not None:
        snapshot_qs = snapshot_qs.filter(agency=agency)
    for raw_code in snapshot_qs.values_list("container_code", flat=True):
        code = str(raw_code or "").strip()
        key = code.casefold()
        if code and (not requested_keys or key in requested_keys):
            resolved_by_key.setdefault(key, code)
    return set(resolved_by_key.values())


def _row_matches_shipping_reserve(row: dict, reserve: WarehouseReserve) -> bool:
    if int(row.get("agency_id") or 0) != int(reserve.agency_id or 0):
        return False
    if str(row.get("sku") or "").strip().casefold() != str(reserve.sku_code or "").strip().casefold():
        return False
    if str(row.get("size") or "").strip().casefold() != str(reserve.size or "").strip().casefold():
        return False
    reserve_barcode = _normalize_barcode_value(reserve.barcode).casefold()
    row_barcode = _normalize_barcode_value(row.get("barcode")).casefold()
    if reserve_barcode and reserve_barcode != row_barcode:
        return False
    reserve_goods = normalize_goods_type(reserve.goods_type)
    return not reserve_goods or reserve_goods == normalize_goods_type(row.get("goods_type"))


def _apply_shipping_pool_box_claims(
    rows: list[dict],
    reserves: list[WarehouseReserve],
    *,
    reserved_field: str,
) -> None:
    claims = _shipping_pool_box_claims(reserves)
    if not claims:
        return
    for reserve, box_codes, claimed_qty in claims:
        remaining_qty = max(int(claimed_qty or 0), 0)
        for box_code in box_codes:
            normalized_code = str(box_code or "").strip().casefold()
            if not normalized_code:
                continue
            for row in rows:
                row_code = str(row.get("box_code") or row.get("container_code") or "").strip().casefold()
                if row_code != normalized_code or not _row_matches_shipping_reserve(row, reserve):
                    continue
                available_qty = max(int(row.get("available_qty") or 0), 0)
                applied_qty = min(available_qty, remaining_qty)
                if applied_qty <= 0:
                    continue
                row["available_qty"] = available_qty - applied_qty
                row[reserved_field] = max(int(row.get(reserved_field) or 0), 0) + applied_qty
                remaining_qty -= applied_qty
                if remaining_qty <= 0:
                    break
            if remaining_qty <= 0:
                break


def _build_snapshot_reserved_truth_maps(
    rows: list[dict],
    *,
    reserved_field: str,
) -> tuple[dict[tuple[int, str, str, str, str], int], dict[tuple[int, str, str, str], int]]:
    exact_map: dict[tuple[int, str, str, str, str], int] = {}
    any_goods_map: dict[tuple[int, str, str, str], int] = {}
    for row in rows:
        qty = max(int(row.get(reserved_field) or 0), 0)
        if qty <= 0:
            continue
        exact_group_key = _stock_truth_exact_group_key(
            agency_id=row.get("agency_id"),
            sku=row.get("sku"),
            size=row.get("size"),
            goods_type=row.get("goods_type"),
        )
        if exact_group_key is not None:
            exact_key = (*exact_group_key, _normalize_barcode_value(row.get("barcode")).lower())
            exact_map[exact_key] = exact_map.get(exact_key, 0) + qty
            continue
        any_group_key = _stock_truth_any_group_key(
            agency_id=row.get("agency_id"),
            sku=row.get("sku"),
            size=row.get("size"),
        )
        if any_group_key is None:
            continue
        any_key = (*any_group_key, _normalize_barcode_value(row.get("barcode")).lower())
        any_goods_map[any_key] = any_goods_map.get(any_key, 0) + qty
    return exact_map, any_goods_map


def _build_truth_delta_map(
    target_map: dict,
    current_map: dict,
) -> dict:
    delta_map: dict = {}
    for key in set(target_map.keys()) | set(current_map.keys()):
        delta = int(target_map.get(key, 0)) - int(current_map.get(key, 0))
        if delta:
            delta_map[key] = delta
    return delta_map


def _stock_row_truth_sort_key(row: dict):
    return (
        int(row.get("agency_id") or 0),
        (str(row.get("sku") or "").strip().lower()),
        (str(row.get("size") or "").strip().lower()),
        normalize_goods_type(row.get("goods_type")),
        _normalize_barcode_value(row.get("barcode")).lower(),
        row.get("updated_at") or row.get("created_at") or timezone.localtime(),
        str(row.get("order_id") or ""),
        str(row.get("box_code") or ""),
        str(row.get("pallet_code") or ""),
        int(row.get("id") or 0),
    )


def _apply_truth_delta_to_row(
    row: dict,
    *,
    reserved_field: str,
    delta_map: dict,
    key,
    allow_restore: bool = True,
) -> None:
    if key is None:
        return
    delta = int(delta_map.get(key, 0))
    if delta == 0:
        return
    available_qty = max(int(row.get("available_qty") or 0), 0)
    reserved_qty = max(int(row.get(reserved_field) or 0), 0)
    if delta > 0:
        applied_qty = min(available_qty, delta)
        if applied_qty <= 0:
            return
        row["available_qty"] = available_qty - applied_qty
        row[reserved_field] = reserved_qty + applied_qty
        remaining_delta = delta - applied_qty
    elif allow_restore:
        applied_qty = min(reserved_qty, abs(delta))
        if applied_qty <= 0:
            return
        row["available_qty"] = available_qty + applied_qty
        row[reserved_field] = reserved_qty - applied_qty
        remaining_delta = delta + applied_qty
    else:
        return
    if remaining_delta:
        delta_map[key] = remaining_delta
    else:
        delta_map.pop(key, None)


def _apply_reserve_truth_to_rows(
    rows: list[dict],
    *,
    reserve_type: str,
    reserved_field: str,
    agency: Agency | None = None,
    agency_id: int | None = None,
    exclude_context_type: str | None = None,
    exclude_context_id: str | None = None,
) -> list[dict]:
    reserve_qs = _active_reserve_truth_queryset(
        agency=agency,
        agency_id=agency_id,
        reserve_type=reserve_type,
        exclude_context_type=exclude_context_type,
        exclude_context_id=exclude_context_id,
    )
    if reserve_type == WarehouseReserve.TYPE_SHIPPING:
        reserve_qs = reserve_qs.prefetch_related("events")
    reserves = list(reserve_qs)
    (
        target_exact_map,
        target_exact_group_map,
        target_any_map,
        target_any_group_map,
    ) = _reserve_truth_maps_from_queryset(reserves)
    adjusted_rows = [dict(row) for row in rows]
    adjusted_rows.sort(key=_stock_row_truth_sort_key)
    if reserve_type == WarehouseReserve.TYPE_SHIPPING:
        _apply_shipping_pool_box_claims(
            adjusted_rows,
            reserves,
            reserved_field=reserved_field,
        )
    current_exact_map, current_any_map = _build_snapshot_reserved_truth_maps(
        adjusted_rows,
        reserved_field=reserved_field,
    )
    delta_exact_map = _build_truth_delta_map(target_exact_map, current_exact_map)
    delta_any_map = _build_truth_delta_map(target_any_map, current_any_map)
    if not delta_exact_map and not delta_any_map and not target_exact_group_map and not target_any_group_map:
        return adjusted_rows

    for row in adjusted_rows:
        exact_key = _stock_truth_exact_key(
            agency_id=row.get("agency_id"),
            sku=row.get("sku"),
            size=row.get("size"),
            goods_type=row.get("goods_type"),
            barcode=row.get("barcode"),
        )
        _apply_truth_delta_to_row(
            row,
            reserved_field=reserved_field,
            delta_map=delta_exact_map,
            key=exact_key,
        )
        exact_group_key = _stock_truth_exact_group_key(
            agency_id=row.get("agency_id"),
            sku=row.get("sku"),
            size=row.get("size"),
            goods_type=row.get("goods_type"),
        )
        _apply_truth_delta_to_row(
            row,
            reserved_field=reserved_field,
            delta_map=target_exact_group_map,
            key=exact_group_key,
            allow_restore=False,
        )
        any_key = _stock_truth_any_key(
            agency_id=row.get("agency_id"),
            sku=row.get("sku"),
            size=row.get("size"),
            barcode=row.get("barcode"),
        )
        _apply_truth_delta_to_row(
            row,
            reserved_field=reserved_field,
            delta_map=delta_any_map,
            key=any_key,
        )
        any_group_key = _stock_truth_any_group_key(
            agency_id=row.get("agency_id"),
            sku=row.get("sku"),
            size=row.get("size"),
        )
        _apply_truth_delta_to_row(
            row,
            reserved_field=reserved_field,
            delta_map=target_any_group_map,
            key=any_group_key,
            allow_restore=False,
        )
    return adjusted_rows


def stock_rows_with_availability(
    *,
    agency: Agency | None = None,
    agency_id: int | None = None,
    zone: str | None = None,
    row_number: int | None = None,
    sku_values: set[str] | None = None,
    barcode_values: set[str] | None = None,
    require_pallet: bool = False,
    require_box: bool = False,
    exclude_processing_order_id: str | None = None,
    exclude_shipping_order_id: str | None = None,
    include_fbs_pool: bool = True,
) -> list[dict]:
    rows = snapshot_stock_rows(
        agency=agency,
        agency_id=agency_id,
        zone=zone,
        row_number=row_number,
        sku_values=sku_values,
        barcode_values=barcode_values,
        require_pallet=require_pallet,
        require_box=require_box,
    )
    rows = _apply_reserve_truth_to_rows(
        rows,
        reserve_type=WarehouseReserve.TYPE_PROCESSING,
        reserved_field="processing_reserved_qty",
        agency=agency,
        agency_id=agency_id,
        exclude_context_type="processing" if exclude_processing_order_id else None,
        exclude_context_id=exclude_processing_order_id,
    )
    rows = _apply_reserve_truth_to_rows(
        rows,
        reserve_type=WarehouseReserve.TYPE_SHIPPING,
        reserved_field="shipping_reserved_qty",
        agency=agency,
        agency_id=agency_id,
        exclude_context_type="shipping" if exclude_shipping_order_id else None,
        exclude_context_id=exclude_shipping_order_id,
    )
    if include_fbs_pool:
        from .fbs_quantity_reserves import apply_to_rows
        protected_box_codes = set()
        if exclude_shipping_order_id:
            protected_box_codes = _shipping_reserve_box_codes_for_context(
                agency=agency,
                agency_id=agency_id,
                context_id=exclude_shipping_order_id,
            )
        rows = apply_to_rows(
            rows,
            agency_id=agency_id or getattr(agency, 'id', None),
            protected_box_codes=protected_box_codes,
        )
    prepared_rows: list[dict] = []
    for row in rows:
        prepared = dict(row)
        stock_bucket = _stock_bucket_for_row(prepared)
        qty_value = int(prepared.get("qty") or 0)
        prepared["stock_main_qty"] = qty_value if stock_bucket == "OS" else 0
        prepared["stock_processing_qty"] = qty_value if stock_bucket == "OBR" else 0
        prepared["stock_otg_qty"] = qty_value if stock_bucket == "OTG" else 0
        prepared_rows.append(prepared)
    return prepared_rows


def _barcode_value_for_sku(sku: SKU | None, size: str | None) -> str:
    if not sku:
        return "-"
    barcodes = list(getattr(sku, "barcodes", []).all())
    if not barcodes:
        return "-"
    size_value = (size or "").strip()
    if size_value:
        for barcode in barcodes:
            if (barcode.size or "").strip() == size_value:
                return barcode.value
    primary = next((barcode for barcode in barcodes if barcode.is_primary), None)
    return primary.value if primary else barcodes[0].value


def _normalize_photo_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith(("http://", "https://", "/")):
        return url
    return f"/{url}"


def _sku_photo_url(sku: SKU | None) -> str:
    if not sku:
        return ""
    url = (sku.img or "").strip()
    if url:
        return _normalize_photo_url(url)
    photos = list(getattr(sku, "photos", []).all())
    if photos:
        return _normalize_photo_url((photos[0].url or "").strip())
    return ""


def inventory_items_for_agency(
    agency: Agency | None,
    exclude_processing_order_id: str | None = None,
    exclude_shipping_order_id: str | None = None,
    warehouse_state_codes: list[str] | tuple[str, ...] | set[str] | None = None,
) -> list[dict]:
    if not agency:
        return []

    stock_rows = stock_rows_with_availability(
        agency=agency,
        exclude_processing_order_id=exclude_processing_order_id,
        exclude_shipping_order_id=exclude_shipping_order_id,
    )
    if warehouse_state_codes:
        allowed_states = {
            str(code or "").strip()
            for code in warehouse_state_codes
            if str(code or "").strip()
        }
        stock_rows = [
            row
            for row in stock_rows
            if str(row.get("warehouse_state_code") or "").strip() in allowed_states
        ]

    totals: dict[tuple[str, str, str, str, str], dict] = {}
    for row in stock_rows:
        sku = (row.get("sku") or "").strip()
        name = (row.get("name") or "").strip()
        size = (row.get("size") or "").strip()
        goods_label = (row.get("goods_type") or "").strip() or "-"
        barcode = _normalize_barcode_value(row.get("barcode"))
        qty = int(row.get("available_qty") or 0)
        sku_ref_id = int(row.get("sku_ref_id") or 0)
        if qty <= 0 or not any((sku, name, size)):
            continue
        key = (sku, name, size, goods_label, barcode)
        existing = totals.setdefault(
            key,
            {
                "sku": sku,
                "sku_id": sku_ref_id,
                "name": name,
                "size": size,
                "barcode": barcode,
                "qty": 0,
                "goods_type": goods_label,
            },
        )
        if not existing.get("sku_id") and sku_ref_id:
            existing["sku_id"] = sku_ref_id
        existing["qty"] += qty

    sku_ids = {int(item["sku_id"]) for item in totals.values() if int(item.get("sku_id") or 0) > 0}
    sku_codes = {item["sku"] for item in totals.values() if item.get("sku")}
    sku_map_by_id: dict[int, SKU] = {}
    sku_map_by_code: dict[str, SKU] = {}
    sku_qs = SKU.objects.filter(agency=agency, deleted=False)
    if sku_ids and sku_codes:
        sku_qs = sku_qs.filter(models.Q(id__in=sku_ids) | models.Q(sku_code__in=sku_codes))
    elif sku_ids:
        sku_qs = sku_qs.filter(id__in=sku_ids)
    elif sku_codes:
        sku_qs = sku_qs.filter(sku_code__in=sku_codes)
    else:
        sku_qs = SKU.objects.none()
    for sku in sku_qs.prefetch_related("barcodes", "photos"):
        sku_map_by_id[int(sku.id)] = sku
        sku_map_by_code[sku.sku_code] = sku

    temporary_feature_enabled = temporary_nomenclature_enabled()
    temporary_map_exact: dict[tuple[str, str, str, str], WarehouseTemporaryNomenclature] = {}
    temporary_map_by_code: dict[str, list[WarehouseTemporaryNomenclature]] = {}
    temp_item_codes = {
        str(item.get("sku") or "").strip()
        for item in totals.values()
        if str(item.get("sku") or "").strip()
    }
    if temp_item_codes:
        for temp_item in WarehouseTemporaryNomenclature.objects.filter(
            agency=agency,
            item_code__in=temp_item_codes,
        ).only("id", "item_code", "name", "size", "goods_type"):
            code_key = str(temp_item.item_code or "").strip().lower()
            if not code_key:
                continue
            exact_key = (
                code_key,
                str(temp_item.name or "").strip().lower(),
                str(temp_item.size or "").strip().lower(),
                normalize_goods_type(temp_item.goods_type),
            )
            temporary_map_exact[exact_key] = temp_item
            temporary_map_by_code.setdefault(code_key, []).append(temp_item)

    items: list[dict] = []
    for item in totals.values():
        sku_obj = sku_map_by_id.get(int(item.get("sku_id") or 0)) or sku_map_by_code.get(item.get("sku"))
        temp_item = None
        if sku_obj is None:
            exact_key = (
                str(item.get("sku") or "").strip().lower(),
                str(item.get("name") or "").strip().lower(),
                str(item.get("size") or "").strip().lower(),
                normalize_goods_type(item.get("goods_type")),
            )
            temp_item = temporary_map_exact.get(exact_key)
            if temp_item is None:
                code_matches = temporary_map_by_code.get(exact_key[0]) or []
                if len(code_matches) == 1:
                    temp_item = code_matches[0]
        if temp_item is not None and not temporary_feature_enabled:
            continue
        available_qty = int(item.get("qty") or 0)
        if available_qty <= 0:
            continue
        row = {
            "sku": item.get("sku") or "",
            "name": item.get("name") or "",
            "size": item.get("size") or "",
            "barcode": item.get("barcode") or "",
            "qty": available_qty,
            "goods_type": item.get("goods_type") or "-",
            "photo": _sku_photo_url(sku_obj),
        }
        if sku_obj is not None:
            row["nomenclature_kind"] = "sku"
        elif temp_item is not None and temporary_feature_enabled:
            row["nomenclature_kind"] = "temporary"
            row["temporary_nomenclature_id"] = int(temp_item.id or 0)
            row["temporary_nomenclature_code"] = str(temp_item.item_code or "").strip()
        items.append(row)
    items.sort(
        key=lambda row: (
            row.get("name") or "",
            row.get("goods_type") or "",
            row.get("size") or "",
            row.get("sku") or "",
        )
    )
    return items


def _occupied_os_snapshot_rows(
    *,
    exclude_order_type: str | None = None,
    exclude_order_id: str | None = None,
    exclude_pallet_code: str | None = None,
) -> list[dict]:
    rows = (
        WarehouseStockSnapshot.objects.filter(is_archived=False, zone_code__iexact="OS", qty__gt=0)
        .exclude(warehouse_state_code__in=_NON_OCCUPYING_OS_STATES)
        .values(
            "source_context_type",
            "source_context_id",
            "container_code",
            "container__container_type",
            "container__container_code",
            "parent_container_id",
            "parent_container__container_code",
            "location__row_no",
            "location__section_no",
            "location__tier_no",
            "location__cell_no",
            "agency_id",
        )
        .order_by("id")
    )
    normalized_pallet_code = str(exclude_pallet_code or "").strip().lower()
    result: list[dict] = []
    for snapshot in rows.iterator(chunk_size=2000):
        if exclude_order_type and exclude_order_id:
            if (
                str(snapshot.get("source_context_type") or "").strip() == str(exclude_order_type or "").strip()
                and str(snapshot.get("source_context_id") or "").strip() == str(exclude_order_id or "").strip()
            ):
                continue
        elif exclude_order_id and str(snapshot.get("source_context_id") or "").strip() == str(exclude_order_id or "").strip():
            continue
        row_no = int(snapshot.get("location__row_no") or 0)
        section = int(snapshot.get("location__section_no") or 0)
        tier = int(snapshot.get("location__tier_no") or 0)
        cell = int(snapshot.get("location__cell_no") or 0)
        if not all((row_no, section, tier, cell)):
            continue
        if snapshot.get("parent_container_id"):
            pallet_code = str(snapshot.get("parent_container__container_code") or "").strip()
        elif snapshot.get("container__container_type") in _PALLET_CONTAINER_TYPES:
            pallet_code = str(snapshot.get("container__container_code") or "").strip()
        else:
            pallet_code = str(snapshot.get("container_code") or "").strip()
        if normalized_pallet_code and pallet_code.lower() == normalized_pallet_code:
            continue
        result.append(
            {
                "row": row_no,
                "section": section,
                "tier": tier,
                "cell": cell,
                "agency_id": int(snapshot.get("agency_id") or 0),
            }
        )
    return result


def _occupied_os_operation_rows(
    *,
    exclude_pallet_code: str | None = None,
    exclude_draft_token: str | None = None,
) -> list[dict]:
    PutawayDraftReservationService.cleanup_expired()
    qs = (
        WarehouseOperation.objects.select_related("destination_location")
        .filter(
            operation_type=WarehouseOperation.TYPE_PUTAWAY,
            status__in=_ACTIVE_OS_RESERVATION_STATUSES,
            destination_location__zone_code__iexact="OS",
        )
        .order_by("id")
        .distinct()
    )
    normalized_pallet_code = str(exclude_pallet_code or "").strip()
    if normalized_pallet_code:
        qs = qs.exclude(tasks__container__container_code__iexact=normalized_pallet_code).distinct()
    normalized_draft_token = str(exclude_draft_token or "").strip()
    if normalized_draft_token:
        qs = qs.exclude(
            context_type=PutawayDraftReservationService.DRAFT_CONTEXT_TYPE,
            context_id=normalized_draft_token,
        )
    rows: list[dict] = []
    for operation in qs:
        location = operation.destination_location
        if location is None:
            continue
        rows.append(
            {
                "row": int(location.row_no or 0),
                "section": int(location.section_no or 0),
                "tier": int(location.tier_no or 0),
                "cell": int(location.cell_no or 0),
                "agency_id": int(operation.agency_id or 0),
            }
        )
    return rows


def _occupied_os_occupancy(
    exclude_order_type: str | None = None,
    exclude_order_id: str | None = None,
    exclude_pallet_code: str | None = None,
    exclude_draft_token: str | None = None,
) -> dict[tuple[int, int, int, int], set[int]]:
    rows = _occupied_os_snapshot_rows(
        exclude_order_type=exclude_order_type,
        exclude_order_id=exclude_order_id,
        exclude_pallet_code=exclude_pallet_code,
    )
    rows = list(rows) + _occupied_os_operation_rows(
        exclude_pallet_code=exclude_pallet_code,
        exclude_draft_token=exclude_draft_token,
    )
    rows += fbs_storage_occupied_rows()
    grouped: dict[tuple[int, int, int, int], set[int]] = {}
    for row in rows:
        row_no = int((row.get("row") if isinstance(row, dict) else row.row) or 0)
        section = int((row.get("section") if isinstance(row, dict) else row.section) or 0)
        tier = int((row.get("tier") if isinstance(row, dict) else row.tier) or 0)
        cell = int((row.get("cell") if isinstance(row, dict) else row.cell) or 0)
        if not all((row_no, section, tier, cell)):
            continue
        agency_id = int((row.get("agency_id") if isinstance(row, dict) else row.agency_id) or 0)
        grouped.setdefault((row_no, section, tier, cell), set()).add(agency_id)
    return grouped


def occupied_os_state(
    exclude_order_type: str | None = None,
    exclude_order_id: str | None = None,
    exclude_pallet_code: str | None = None,
    exclude_draft_token: str | None = None,
) -> tuple[set[tuple[int, int, int, int]], dict[tuple[int, int], set[int]]]:
    grouped = _occupied_os_occupancy(
        exclude_order_type=exclude_order_type,
        exclude_order_id=exclude_order_id,
        exclude_pallet_code=exclude_pallet_code,
        exclude_draft_token=exclude_draft_token,
    )
    sections: dict[tuple[int, int], set[int]] = {}
    for (row_no, section, _tier, _cell), agency_ids in grouped.items():
        valid_agency_ids = {agency_id for agency_id in agency_ids if agency_id > 0}
        if valid_agency_ids:
            sections.setdefault((row_no, section), set()).update(valid_agency_ids)
    return set(grouped), sections


def occupied_os_cell_keys(
    exclude_order_type: str | None = None,
    exclude_order_id: str | None = None,
    exclude_pallet_code: str | None = None,
    exclude_draft_token: str | None = None,
) -> set[tuple[int, int, int, int]]:
    occupied_keys, _section_agencies = occupied_os_state(
        exclude_order_type=exclude_order_type,
        exclude_order_id=exclude_order_id,
        exclude_pallet_code=exclude_pallet_code,
        exclude_draft_token=exclude_draft_token,
    )
    return occupied_keys


def occupied_os_cells(
    exclude_order_type: str | None = None,
    exclude_order_id: str | None = None,
    exclude_pallet_code: str | None = None,
    include_agency: bool = False,
    exclude_draft_token: str | None = None,
) -> list[dict]:
    grouped = _occupied_os_occupancy(
        exclude_order_type=exclude_order_type,
        exclude_order_id=exclude_order_id,
        exclude_pallet_code=exclude_pallet_code,
        exclude_draft_token=exclude_draft_token,
    )
    result: list[dict] = []
    for row_no, section, tier, cell in sorted(grouped.keys()):
        item = {
            "row": row_no,
            "section": section,
            "tier": tier,
            "cell": cell,
        }
        if include_agency:
            agency_ids = sorted(agency_id for agency_id in grouped[(row_no, section, tier, cell)] if agency_id > 0)
            item["agency_ids"] = agency_ids
            item["agency_id"] = agency_ids[0] if len(agency_ids) == 1 else 0
        result.append(item)
    return result


def occupied_os_section_agencies(
    exclude_order_type: str | None = None,
    exclude_order_id: str | None = None,
    exclude_pallet_code: str | None = None,
    exclude_draft_token: str | None = None,
) -> dict[tuple[int, int], set[int]]:
    _occupied_keys, sections = occupied_os_state(
        exclude_order_type=exclude_order_type,
        exclude_order_id=exclude_order_id,
        exclude_pallet_code=exclude_pallet_code,
        exclude_draft_token=exclude_draft_token,
    )
    return sections


def suggest_os_cell_for_agency(
    *,
    agency_id: int | None,
    row_sections: dict[int, int] | dict[str, int],
    tiers: int,
    cells_per_tier: int,
    occupied_keys: set[tuple[int, int, int, int]] | None = None,
    used_cell_keys: set[tuple[int, int, int, int]] | None = None,
    section_agencies: dict[tuple[int, int], set[int]] | None = None,
) -> dict | None:
    occupied_keys = set(occupied_keys or set())
    used_cell_keys = set(used_cell_keys or set())
    section_agencies = {
        (int(row), int(section)): {int(value) for value in values if int(value) > 0}
        for (row, section), values in (section_agencies or {}).items()
    }
    current_agency_id = int(agency_id or 0)
    buckets: dict[str, list[tuple[int, int]]] = {"same": [], "empty": [], "other": []}
    for row in sorted((int(value) for value in row_sections.keys()), key=int):
        for section in range(1, int(row_sections.get(row) or row_sections.get(str(row)) or 0) + 1):
            agencies = section_agencies.get((row, section), set())
            if current_agency_id > 0 and current_agency_id in agencies:
                bucket = "same"
            elif not agencies:
                bucket = "empty"
            else:
                bucket = "other"
            buckets[bucket].append((row, section))
    for bucket in ("same", "empty", "other"):
        for row, section in buckets[bucket]:
            for tier in range(1, int(tiers or 0) + 1):
                for cell in range(1, int(cells_per_tier or 0) + 1):
                    key = (int(row), int(section), int(tier), int(cell))
                    if key in occupied_keys or key in used_cell_keys:
                        continue
                    return {
                        "zone": "OS",
                        "row": int(row),
                        "section": int(section),
                        "tier": int(tier),
                        "cell": int(cell),
                    }
    return None


class StockAvailabilityService:
    normalize_goods_type = staticmethod(normalize_goods_type)
    build_processing_reserve_maps = staticmethod(build_processing_reserve_maps)
    build_shipping_reserve_maps = staticmethod(build_shipping_reserve_maps)
    reserved_processing_qty = staticmethod(reserved_processing_qty)
    shipping_reserved_box_codes = staticmethod(shipping_reserved_box_codes)
    stock_rows_with_availability = staticmethod(stock_rows_with_availability)
    inventory_items_for_agency = staticmethod(inventory_items_for_agency)
    occupied_os_state = staticmethod(occupied_os_state)
    occupied_os_cell_keys = staticmethod(occupied_os_cell_keys)
    occupied_os_cells = staticmethod(occupied_os_cells)
    occupied_os_section_agencies = staticmethod(occupied_os_section_agencies)
    suggest_os_cell_for_agency = staticmethod(suggest_os_cell_for_agency)
