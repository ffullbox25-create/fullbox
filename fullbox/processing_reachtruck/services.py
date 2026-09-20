from __future__ import annotations

from collections import defaultdict
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse

from audit.models import OrderAuditEntry
from employees.access import get_request_employee, get_request_role
from reachtruck.models import BoxClaim, MoveRequest, MoveRequestItem, MoveTask
from reachtruck.services.claims import claim_boxes_for_task, unavailable_box_claim_codes
from reachtruck.services.move_requests import sync_task_status_by_legacy_order_id
from sklad.models import WarehouseStockSnapshot
from sklad.services.dispatchable_stock import (
    dispatchable_warehouse_state_codes,
    dispatchable_warehouse_state_q,
    is_dispatchable_warehouse_state,
)
from sklad.services.operational_locations import select_operational_location
from sklad.services.stock_availability import StockAvailabilityService
from sklad.services.warehouse_write_path import WarehouseWritePathService
from fbs.goods_types import receiving_placement_allowed_snapshot_ids


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _item_barcodes(item: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    raw = item.get("requested_barcodes") or item.get("barcodes") or []
    if isinstance(raw, str):
        raw = [raw]
    for value in raw:
        normalized = _norm(value)
        if normalized:
            values.add(normalized)
    single = _norm(item.get("barcode") or item.get("requested_barcode"))
    if single:
        values.add(single)
    return values


def _snapshot_matches_item(snapshot: WarehouseStockSnapshot, item: dict[str, Any]) -> bool:
    barcodes = _item_barcodes(item)
    snapshot_barcode = _norm(snapshot.barcode)
    goods_type = StockAvailabilityService.normalize_goods_type(
        item.get("requested_goods_type") or item.get("goods_type")
    )
    snapshot_goods_type = StockAvailabilityService.normalize_goods_type(snapshot.goods_type)
    if goods_type and snapshot_goods_type != goods_type:
        return False

    if barcodes:
        return bool(snapshot_barcode and snapshot_barcode in barcodes)

    article = _norm(
        item.get("requested_article")
        or item.get("requested_sku")
        or item.get("requested_sku_code")
        or item.get("article")
        or item.get("sku")
        or item.get("sku_code")
    )
    snapshot_article = _norm(snapshot.sku_code)
    if article and snapshot_article != article:
        return False
    return bool(article)


def _location_payload(location) -> dict[str, Any]:
    if not location:
        return {"zone": "OS", "row": "", "section": "", "tier": "", "cell": ""}
    payload = {
        "zone": getattr(location, "zone", None) or getattr(location, "zone_code", None) or "OS",
        "row": getattr(location, "row", None) or getattr(location, "row_no", "") or "",
        "section": getattr(location, "section", None) or getattr(location, "section_no", "") or "",
        "tier": getattr(location, "tier", None) or getattr(location, "tier_no", "") or "",
        "cell": getattr(location, "cell", None) or getattr(location, "cell_no", "") or "",
    }
    location_code = str(getattr(location, "location_code", "") or "").strip()
    display_name = str(getattr(location, "display_name", "") or "").strip()
    if location_code:
        payload["code"] = location_code
    if display_name:
        payload["label"] = display_name
    return payload


def _section_letter(section: Any) -> str:
    section_no = _as_int(section)
    if section_no <= 0:
        return ""
    return chr(ord("A") + max(section_no - 2, 0))


def _location_code(location) -> str:
    payload = _location_payload(location)
    zone = str(payload.get("zone") or "OS")
    if zone != "OS":
        return zone
    letter = _section_letter(payload.get("section"))
    row = payload.get("row") or ""
    tier = payload.get("tier") or ""
    cell = payload.get("cell") or ""
    if not letter:
        return "OS"
    return f"{letter}-{row}/{tier}-{cell}"


def _location_label(location) -> str:
    payload = _location_payload(location)
    zone = str(payload.get("zone") or "OS")
    if zone == "OBR":
        return "OBR · Зона обработки"
    if zone != "OS":
        return zone
    letter = _section_letter(payload.get("section"))
    return (
        f"OS · Линия {letter} · Стеллаж {payload.get('row') or ''} · "
        f"Этаж {payload.get('tier') or ''} · Ячейка {payload.get('cell') or ''}"
    )


def _plural_boxes(count: int) -> str:
    count = abs(int(count or 0))
    if count % 10 == 1 and count % 100 != 11:
        return "короб"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "короба"
    return "коробов"


def _employee_name(employee, user) -> str:
    if employee:
        full_name = " ".join(
            part
            for part in (
                getattr(employee, "last_name", ""),
                getattr(employee, "first_name", ""),
                getattr(employee, "middle_name", ""),
            )
            if part
        ).strip()
        if full_name:
            return full_name
        name = str(employee).strip()
        if name:
            return name
    if user and getattr(user, "is_authenticated", False):
        full_name = (user.get_full_name() or "").strip()
        return full_name or str(getattr(user, "username", "") or user)
    return ""


def _row_barcode(row: dict[str, Any]) -> str:
    barcodes = row.get("requested_barcodes") or []
    if isinstance(barcodes, str):
        return barcodes
    if barcodes:
        return str(barcodes[0] or "")
    barcode_qty = row.get("barcode_qty") or {}
    if isinstance(barcode_qty, dict) and barcode_qty:
        return str(next(iter(barcode_qty.keys())) or "")
    return ""


def _aggregate_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], int] = defaultdict(int)
    for row in rows:
        key = (
            str(row.get("requested_article") or ""),
            str(row.get("requested_goods_type") or ""),
            _row_barcode(row),
        )
        grouped[key] += _as_int(row.get("qty"))

    items: list[dict[str, Any]] = []
    for (article, goods_type, barcode), qty in grouped.items():
        item = {
            "requested_article": article,
            "requested_goods_type": goods_type,
            "requested_qty": qty,
        }
        if barcode:
            item["requested_barcodes"] = [barcode]
        items.append(item)
    return items


def _aggregate_barcode_qty(rows: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = defaultdict(int)
    for row in rows:
        barcode_qty = row.get("barcode_qty") or {}
        if not isinstance(barcode_qty, dict):
            continue
        for barcode, qty in barcode_qty.items():
            barcode_key = str(barcode or "").strip()
            if not barcode_key:
                continue
            result[barcode_key] += _as_int(qty)
    return dict(result)


def _delivered_processing_rows(
    *,
    agency,
    processing_order_id: str,
) -> list[dict[str, Any]]:
    delivered_rows: list[dict[str, Any]] = []
    tasks = (
        MoveTask.objects.filter(
            request__agency=agency,
            request__context_type=MoveRequest.CONTEXT_PROCESSING,
            request__context_id=processing_order_id,
            request__destination_zone="OBR",
            status=MoveTask.STATUS_DONE,
        )
        .order_by("completed_at", "id")
    )
    for task in tasks:
        payload = task.payload if isinstance(task.payload, dict) else {}
        rows = [row for row in payload.get("requested_rows") or [] if isinstance(row, dict)]
        if not rows:
            continue
        remaining_task_qty = (
            _as_int(task.qty_done)
            or _as_int(task.qty_planned)
            or _as_int(payload.get("requested_qty"))
        )
        for row in rows:
            if remaining_task_qty <= 0:
                break
            row_qty = min(_as_int(row.get("qty")), remaining_task_qty)
            if row_qty <= 0:
                continue
            delivered_rows.append(
                {
                    "article": _norm(
                        row.get("requested_article")
                        or row.get("requested_sku")
                        or row.get("requested_sku_code")
                    ),
                    "barcode": _norm(_row_barcode(row)),
                    "goods_type": StockAvailabilityService.normalize_goods_type(
                        row.get("requested_goods_type") or row.get("goods_type")
                    ),
                    "remaining_qty": row_qty,
                }
            )
            remaining_task_qty -= row_qty
    return delivered_rows


def _delivery_matches_request_item(delivery: dict[str, Any], item: dict[str, Any]) -> bool:
    requested_goods_type = StockAvailabilityService.normalize_goods_type(
        item.get("requested_goods_type") or item.get("goods_type")
    )
    if requested_goods_type and delivery.get("goods_type") != requested_goods_type:
        return False
    requested_barcodes = _item_barcodes(item)
    if requested_barcodes:
        return bool(delivery.get("barcode") in requested_barcodes)
    requested_article = _norm(
        item.get("requested_article")
        or item.get("requested_sku")
        or item.get("requested_sku_code")
        or item.get("article")
        or item.get("sku")
        or item.get("sku_code")
    )
    return bool(requested_article and delivery.get("article") == requested_article)


def _remaining_processing_request_items(
    *,
    agency,
    processing_order_id: str,
    request_items: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    deliveries = _delivered_processing_rows(
        agency=agency,
        processing_order_id=processing_order_id,
    )
    remaining_items: list[dict[str, Any]] = []
    for raw_item in request_items or []:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        remaining_qty = max(_as_int(item.get("requested_qty") or item.get("qty")), 0)
        for delivery in deliveries:
            if remaining_qty <= 0:
                break
            delivered_qty = _as_int(delivery.get("remaining_qty"))
            if delivered_qty <= 0 or not _delivery_matches_request_item(delivery, item):
                continue
            consumed_qty = min(remaining_qty, delivered_qty)
            remaining_qty -= consumed_qty
            delivery["remaining_qty"] = delivered_qty - consumed_qty
        if remaining_qty <= 0:
            continue
        item["requested_qty"] = remaining_qty
        remaining_items.append(item)
    return remaining_items


def _processing_reserve_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        from_zone = str((row.get("from_location") or {}).get("zone") or "").strip().upper()
        box_code = str(row.get("box_code") or "").strip()
        qty = _as_int(row.get("qty"))
        if from_zone != "OS" or not box_code or qty <= 0:
            continue
        key = (
            str(row.get("requested_article") or "").strip(),
            str(row.get("requested_size") or "").strip(),
            _row_barcode(row),
            str(row.get("requested_goods_type") or "").strip(),
            box_code,
        )
        item = grouped.setdefault(key, {
            "sku_code": str(row.get("requested_article") or "").strip(),
            "size": str(row.get("requested_size") or "").strip(),
            "barcode": _row_barcode(row),
            "goods_type": str(row.get("requested_goods_type") or "").strip(),
            "qty": 0,
            "box_codes": [box_code],
            "_source_box_qty": 0,
            "_has_partial_pick": False,
        })
        item["qty"] += qty
        source_box_qty = _as_int(row.get("source_box_qty"))
        item["_source_box_qty"] = max(_as_int(item.get("_source_box_qty")), source_box_qty)
        item["_has_partial_pick"] = bool(item.get("_has_partial_pick") or row.get("is_partial_pick"))
    items = []
    for item in grouped.values():
        source_box_qty = _as_int(item.pop("_source_box_qty", 0))
        has_partial_pick = bool(item.pop("_has_partial_pick", False))
        if has_partial_pick or (source_box_qty > 0 and _as_int(item.get("qty")) < source_box_qty):
            item["allow_partial_box_reserve"] = True
        items.append(item)
    return items


def _snapshot_source_payload(snapshot: WarehouseStockSnapshot) -> dict[str, Any]:
    location = snapshot.location
    return {
        "from_location": _location_payload(location),
        "from_code": _location_code(location),
        "from_label": _location_label(location),
        "receiving_order_id": snapshot.source_context_id or "",
    }


def _pallet_is_fully_requested(agency, pallet_code: str, requested_box_codes: set[str]) -> bool:
    active_box_codes = set(
        WarehouseStockSnapshot.objects.filter(
            agency=agency,
            is_archived=False,
            parent_container__container_code=pallet_code,
        )
        .exclude(container_code="")
        .values_list("container_code", flat=True)
    )
    return bool(active_box_codes) and active_box_codes.issubset(requested_box_codes)


def _select_exact_boxes(candidates: list[dict[str, Any]], required_qty: int) -> list[dict[str, Any]]:
    required = max(_as_int(required_qty), 0)
    if required <= 0:
        return []
    ordered = sorted(
        candidates,
        key=lambda candidate: (-_as_int(candidate.get("qty")), str(candidate.get("box_code") or "")),
    )
    greedy: list[dict[str, Any]] = []
    remaining = required
    for candidate in ordered:
        qty = _as_int(candidate.get("qty"))
        if qty <= 0 or qty > remaining:
            continue
        greedy.append(candidate)
        remaining -= qty
        if remaining == 0:
            return greedy

    combinations: dict[int, tuple[int, ...]] = {0: ()}
    for index, candidate in enumerate(ordered):
        qty = _as_int(candidate.get("qty"))
        if qty <= 0 or qty > required:
            continue
        for subtotal, selected_indexes in list(combinations.items())[::-1]:
            next_total = subtotal + qty
            if next_total > required or next_total in combinations:
                continue
            combinations[next_total] = selected_indexes + (index,)
        if required in combinations:
            return [ordered[index] for index in combinations[required]]
        if len(combinations) > 50000:
            break
    return []


def _select_fixed_box_pick_plan(
    candidates: list[dict[str, Any]],
    required_qty: int,
) -> list[tuple[dict[str, Any], int]]:
    required = max(_as_int(required_qty), 0)
    if required <= 0:
        return []
    exact_boxes = _select_exact_boxes(candidates, required)
    if exact_boxes:
        return [(box, _as_int(box.get("qty"))) for box in exact_boxes]

    ordered = sorted(
        (
            candidate
            for candidate in candidates
            if _as_int(candidate.get("qty")) > 0
        ),
        key=lambda candidate: (-_as_int(candidate.get("qty")), str(candidate.get("box_code") or "")),
    )
    if sum(_as_int(candidate.get("qty")) for candidate in ordered) < required:
        return []

    selected: list[tuple[dict[str, Any], int]] = []
    remaining = required
    available = list(ordered)
    while remaining > 0:
        full_box = next(
            (
                candidate
                for candidate in available
                if _as_int(candidate.get("qty")) <= remaining
            ),
            None,
        )
        if full_box is not None:
            box_qty = _as_int(full_box.get("qty"))
            selected.append((full_box, box_qty))
            available.remove(full_box)
            remaining -= box_qty
            continue

        covering = sorted(
            (
                candidate
                for candidate in available
                if _as_int(candidate.get("qty")) > remaining
            ),
            key=lambda candidate: (_as_int(candidate.get("qty")), str(candidate.get("box_code") or "")),
        )
        if not covering:
            return []
        selected.append((covering[0], remaining))
        remaining = 0
    return selected


def _candidate_box_query(items: list[dict[str, Any]]) -> Q:
    query = Q(pk__in=[])
    for item in items:
        article = str(
            item.get("requested_article")
            or item.get("requested_sku")
            or item.get("requested_sku_code")
            or ""
        ).strip()
        barcodes = sorted(_item_barcodes(item))
        item_query = Q()
        has_selector = False
        if barcodes:
            barcode_query = Q(pk__in=[])
            for barcode in barcodes:
                barcode_query |= Q(barcode__iexact=barcode)
            item_query &= barcode_query
            has_selector = True
        elif article:
            item_query &= Q(sku_code__iexact=article)
            has_selector = True
        if has_selector:
            query |= item_query
    return query


def _processing_strict_box_selections(
    *,
    agency,
    processing_order_id: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    selection_rows: list[dict[str, Any]] = []
    quantity_rows: list[dict[str, Any]] = []
    payloads = (
        OrderAuditEntry.objects.filter(
            agency=agency,
            order_type="processing",
            order_id=processing_order_id,
        )
        .order_by("-created_at", "-id")
        .values_list("payload", flat=True)
    )
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        rows = payload.get("stock_rows")
        if not isinstance(rows, list):
            rows = [
                row
                for card in payload.get("cards") or []
                if isinstance(card, dict)
                for row in card.get("rows") or []
                if isinstance(row, dict)
            ]
        candidate_rows = [
            row
            for row in rows
            if isinstance(row, dict)
            and bool(row.get("box_codes") or row.get("box_code"))
        ]
        candidate_box_codes: set[str] = set()
        for row in candidate_rows:
            raw_codes = row.get("box_codes") or row.get("box_code") or []
            if isinstance(raw_codes, str):
                raw_codes = [raw_codes]
            candidate_box_codes.update(
                str(code or "").strip()
                for code in raw_codes
                if str(code or "").strip()
            )

        # Older/multi-position processing requests can contain exact boxes from
        # receiving (PR) without the newer strict_selected_box marker.  Such
        # boxes must remain exact: automatic OS substitution cannot move them
        # out of PR, and silently choosing another box would change the request.
        pr_box_code_keys: set[str] = set()
        if candidate_box_codes:
            for snapshot_code, container_code in (
                WarehouseStockSnapshot.objects.filter(
                    agency=agency,
                    is_archived=False,
                    zone_code="PR",
                )
                .filter(
                    Q(container_code__in=candidate_box_codes)
                    | Q(container__container_code__in=candidate_box_codes)
                )
                .values_list("container_code", "container__container_code")
            ):
                code = str(container_code or snapshot_code or "").strip().lower()
                if code:
                    pr_box_code_keys.add(code)

        selection_rows = []
        quantity_rows = []
        for row in candidate_rows:
            raw_codes = row.get("box_codes") or row.get("box_code") or []
            if isinstance(raw_codes, str):
                raw_codes = [raw_codes]
            row_code_keys = {
                str(code or "").strip().lower()
                for code in raw_codes
                if str(code or "").strip()
            }
            saved_pr_selection = bool(row_code_keys) and row_code_keys.issubset(pr_box_code_keys)
            # The unit picker saves a provisional box split, not an explicit
            # physical-box order. Replan ordinary OS stock at dispatch time;
            # keep receiving/mixed-box selections and legacy exact-box orders.
            quantity_hint = (
                payload.get("client_unit_picker_v1") is True
                and str(row.get("source_zone") or "").strip().upper() == "OS"
                and not row.get("is_mixed_box")
                and not row.get("mixed_group")
                and not saved_pr_selection
            )
            if quantity_hint and _as_int(row.get("box_qty")) > 0:
                quantity_rows.append(row)
            if not quantity_hint and (bool(row.get("strict_selected_box")) or saved_pr_selection):
                selection_rows.append(row)
        # Do not resurrect an old exact selection from an earlier audit event
        # after the latest quantity payload deliberately discarded all hints.
        if selection_rows or (candidate_rows and payload.get("client_unit_picker_v1") is True):
            break

    enriched_items: list[dict[str, Any]] = []
    for item in items:
        item_copy = dict(item)
        item_goods_type = StockAvailabilityService.normalize_goods_type(
            item.get("requested_goods_type") or item.get("goods_type")
        )
        item_barcodes = _item_barcodes(item)
        item_article = _norm(
            item.get("requested_article")
            or item.get("requested_sku")
            or item.get("requested_sku_code")
        )
        selected_codes: list[str] = []
        selected_code_keys: set[str] = set()
        selected_box_quantities: set[int] = set()
        for row in selection_rows + quantity_rows:
            row_goods_type = StockAvailabilityService.normalize_goods_type(row.get("goods_type"))
            if item_goods_type and row_goods_type != item_goods_type:
                continue
            row_barcode = _norm(row.get("barcode"))
            row_article = _norm(row.get("article") or row.get("sku") or row.get("sku_code"))
            if item_barcodes:
                if not row_barcode or row_barcode not in item_barcodes:
                    continue
            elif not item_article or row_article != item_article:
                continue
            if row in quantity_rows:
                selected_box_quantities.add(_as_int(row.get("box_qty")))
                continue
            raw_codes = row.get("box_codes") or row.get("box_code") or []
            if isinstance(raw_codes, str):
                raw_codes = [raw_codes]
            for raw_code in raw_codes:
                code = str(raw_code or "").strip()
                code_key = code.lower()
                if code and code_key not in selected_code_keys:
                    selected_codes.append(code)
                    selected_code_keys.add(code_key)
        if selected_box_quantities:
            # Box identities may change as stock moves; the saved packing size
            # is a direct-dispatch hint (e.g. 100, never 360). Quantity queues
            # relax this hint below so their remainder cannot be starved by a
            # different free source-box size.
            item_copy["_source_box_quantities"] = sorted(selected_box_quantities)
        if selected_codes:
            item_copy["_strict_selected_box_codes"] = selected_codes
        enriched_items.append(item_copy)
    return enriched_items


def build_obr_requested_rows(
    *,
    agency,
    processing_order_id: Any,
    request_items: list[dict[str, Any]] | None,
    planning_errors: list[str] | None = None,
    allow_partial: bool = False,
) -> list[dict[str, Any]]:
    order_key = str(processing_order_id or "").strip()
    if not agency or not order_key:
        return []

    items = [item for item in (request_items or []) if isinstance(item, dict)]
    items = _processing_strict_box_selections(
        agency=agency,
        processing_order_id=order_key,
        items=items,
    )
    if allow_partial:
        # A durable quantity queue represents units still required, not a
        # promise to wait for a source box with the same packing quantity as
        # the provisional unit-picker split. Claims and shipping reserves stay
        # enforced below, but stale physical-box hints must not keep compatible
        # free stock in the queue.
        for item in items:
            item.pop("_source_box_quantities", None)
            item.pop("_strict_selected_box_codes", None)
    potential_matching_snapshots = list(
        WarehouseStockSnapshot.objects.select_related("location", "container", "parent_container")
        .filter(
            agency=agency,
            is_archived=False,
            warehouse_state_code__in=dispatchable_warehouse_state_codes(
                include_receiving=True
            ),
        )
        .filter(_candidate_box_query(items))
        .order_by(
            "location__row_no",
            "location__section_no",
            "location__tier_no",
            "location__cell_no",
            "parent_container__container_code",
            "container_code",
            "id",
        )
    )
    receiving_allowed_ids = receiving_placement_allowed_snapshot_ids(
        [
            snapshot
            for snapshot in potential_matching_snapshots
            if str(snapshot.warehouse_state_code or "").strip().casefold()
            == "placed_in_receiving"
        ]
    )
    matching_snapshots = [
        snapshot
        for snapshot in potential_matching_snapshots
        if is_dispatchable_warehouse_state(
            snapshot.warehouse_state_code,
            snapshot_id=snapshot.id,
            receiving_allowed_ids=receiving_allowed_ids,
        )
    ]
    candidate_container_ids = {
        int(snapshot.container_id or 0)
        for snapshot in matching_snapshots
        if int(snapshot.container_id or 0) > 0
    }
    candidate_box_codes = {
        str(snapshot.container_code or "").strip()
        for snapshot in matching_snapshots
        if str(snapshot.container_code or "").strip()
    }
    if not candidate_container_ids and not candidate_box_codes:
        return []
    unavailable_claim_keys = {
        str(code or "").strip().lower()
        for code in unavailable_box_claim_codes(agency_id=getattr(agency, "id", None))
        if str(code or "").strip()
    }
    shipping_reserved_keys = {
        str(code or "").strip().lower()
        for code in StockAvailabilityService.shipping_reserved_box_codes(
            agency=agency,
            box_codes=candidate_box_codes,
        )
        if str(code or "").strip()
    }
    snapshots = list(
        WarehouseStockSnapshot.objects.select_related("location", "container", "parent_container")
        .filter(
            agency=agency,
            is_archived=False,
        )
        .filter(
            dispatchable_warehouse_state_q(
                receiving_allowed_ids=receiving_allowed_ids
            )
        )
        .filter(Q(container_id__in=candidate_container_ids) | Q(container_code__in=candidate_box_codes))
        .order_by(
            "location__row_no",
            "location__section_no",
            "location__tier_no",
            "location__cell_no",
            "parent_container__container_code",
            "container_code",
            "id",
        )
    )
    grouped_boxes: dict[tuple[int, str], dict[str, Any]] = {}
    for snapshot in snapshots:
        box_code = str(
            getattr(snapshot.container, "container_code", "")
            or snapshot.container_code
            or ""
        ).strip()
        parent = snapshot.parent_container or getattr(snapshot.container, "parent_container", None)
        pallet_code = str(getattr(parent, "container_code", "") or "").strip()
        if not box_code or not pallet_code:
            continue
        qty = int(snapshot.qty or 0)
        key = (int(snapshot.container_id or 0), box_code.lower())
        box = grouped_boxes.setdefault(
            key,
            {
                "box_code": box_code,
                "pallet_code": pallet_code,
                "qty": 0,
                "snapshots": [],
                "valid": True,
            },
        )
        if box_code.lower() in unavailable_claim_keys or box_code.lower() in shipping_reserved_keys:
            box["valid"] = False
            continue
        if box["pallet_code"] != pallet_code:
            box["valid"] = False
            continue
        box["snapshots"].append(snapshot)
        if (
            qty <= 0
            or int(snapshot.available_qty or 0) != qty
            or int(snapshot.processing_reserved_qty or 0) > 0
            or int(snapshot.shipping_reserved_qty or 0) > 0
            or int(snapshot.other_reserved_qty or 0) > 0
        ):
            box["valid"] = False
            continue
        box["qty"] += qty

    selected_box_keys: set[tuple[int, str]] = set()
    selected_snapshot_qty: dict[int, int] = defaultdict(int)
    selected_picks: list[dict[str, Any]] = []
    insufficient_stock = False
    for item in items:
        required_qty = _as_int(item.get("requested_qty") or item.get("qty"))
        strict_box_codes = {
            str(code or "").strip().lower()
            for code in item.get("_strict_selected_box_codes") or []
            if str(code or "").strip()
        }
        source_box_quantities = set(item.get("_source_box_quantities") or [])
        item_candidates: list[dict[str, Any]] = []
        for key, box in grouped_boxes.items():
            if not box.get("valid") or not box.get("snapshots"):
                continue
            if source_box_quantities and _as_int(box.get("qty")) not in source_box_quantities:
                continue
            if key in selected_box_keys and not strict_box_codes:
                continue
            if (
                strict_box_codes
                and str(box.get("box_code") or "").strip().lower() not in strict_box_codes
            ):
                continue
            matching_snapshots = [
                snapshot
                for snapshot in box["snapshots"]
                if _snapshot_matches_item(snapshot, item)
                and int(snapshot.qty or 0) > selected_snapshot_qty[int(snapshot.id)]
            ]
            if not matching_snapshots:
                continue
            if not strict_box_codes and len(matching_snapshots) != len(box["snapshots"]):
                continue
            matching_qty = sum(
                int(snapshot.qty or 0) - selected_snapshot_qty[int(snapshot.id)]
                for snapshot in matching_snapshots
            )
            if matching_qty <= 0:
                continue
            candidate = dict(box)
            candidate["qty"] = matching_qty
            candidate["source_box_qty"] = _as_int(box.get("qty"))
            candidate["matching_snapshots"] = matching_snapshots
            item_candidates.append(candidate)
        if allow_partial:
            required_qty = min(required_qty, sum(_as_int(candidate.get("qty")) for candidate in item_candidates))
            if required_qty <= 0:
                continue
        fixed_pick_plan = _select_fixed_box_pick_plan(item_candidates, required_qty)
        if not fixed_pick_plan:
            insufficient_stock = True
            if planning_errors is not None:
                article = str(item.get("requested_article") or item.get("requested_sku") or "Товар")
                available = sum(_as_int(candidate.get("qty")) for candidate in item_candidates)
                planning_errors.append(
                    f"{article}: нужно {required_qty} шт., в подходящих свободных коробах {available} шт."
                )
            continue
        for box, pick_qty in fixed_pick_plan:
            key = (
                int(box["snapshots"][0].container_id or 0),
                str(box.get("box_code") or "").strip().lower(),
            )
            if not strict_box_codes:
                selected_box_keys.add(key)
            remaining_pick_qty = pick_qty
            snapshot_picks: list[tuple[WarehouseStockSnapshot, int]] = []
            for snapshot in box.get("matching_snapshots") or []:
                snapshot_id = int(snapshot.id)
                available_qty = int(snapshot.qty or 0) - selected_snapshot_qty[snapshot_id]
                qty = min(available_qty, remaining_pick_qty)
                if qty <= 0:
                    continue
                snapshot_picks.append((snapshot, qty))
                selected_snapshot_qty[snapshot_id] += qty
                remaining_pick_qty -= qty
                if remaining_pick_qty <= 0:
                    break
            if remaining_pick_qty > 0:
                return []
            selected_picks.append(
                {
                    "box": box,
                    "box_key": key,
                    "pick_qty": pick_qty,
                    "snapshot_picks": snapshot_picks,
                }
            )

    if insufficient_stock:
        return []
    rows: list[dict[str, Any]] = []
    picked_qty_by_box: dict[tuple[int, str], int] = defaultdict(int)
    for selected_pick in selected_picks:
        picked_qty_by_box[selected_pick["box_key"]] += _as_int(selected_pick.get("pick_qty"))
    for selected_pick in selected_picks:
        box = selected_pick["box"]
        box_key = selected_pick["box_key"]
        is_partial_pick = picked_qty_by_box[box_key] < _as_int(box.get("source_box_qty"))
        for snapshot, qty in selected_pick.get("snapshot_picks") or []:
            source_qty = int(snapshot.qty or 0)
            barcode = str(snapshot.barcode or "").strip()
            row: dict[str, Any] = {
                "pallet_code": box["pallet_code"],
                "box_code": box["box_code"],
                "qty": qty,
                "source_box_qty": _as_int(box.get("source_box_qty")),
                "source_snapshot_qty": source_qty,
                "is_partial_pick": is_partial_pick,
                "requested_article": snapshot.sku_code or "",
                "requested_size": snapshot.size or "",
                "requested_goods_type": snapshot.goods_type or "",
                **_snapshot_source_payload(snapshot),
            }
            if barcode:
                row["requested_barcodes"] = [barcode]
            signature_code = barcode or str(snapshot.sku_code or "").strip()
            if signature_code:
                row["barcode_qty"] = {signature_code: qty}
            rows.append(row)
    return rows


def _create_request_items(move_request: MoveRequest, rows: list[dict[str, Any]]) -> None:
    for item in _aggregate_items(rows):
        barcodes = item.get("requested_barcodes") or []
        barcode = barcodes[0] if barcodes else ""
        qty = _as_int(item.get("requested_qty"))
        MoveRequestItem.objects.create(
            request=move_request,
            sku_code=item.get("requested_article") or "",
            barcode=barcode,
            goods_type=item.get("requested_goods_type") or "",
            qty_requested=qty,
            qty_planned=qty,
        )


def _task_payload(
    *,
    move_request: MoveRequest,
    processing_order_id: str,
    pallet_code: str,
    rows: list[dict[str, Any]],
    full_pallet: bool,
    employee_name: str,
    employee_role: str,
    destination_location,
    concrete_location_required: bool,
) -> dict[str, Any]:
    qty = sum(_as_int(row.get("qty")) for row in rows)
    box_codes = list(
        dict.fromkeys(str(row.get("box_code") or "") for row in rows if row.get("box_code"))
    )
    box_count = len(box_codes)
    first = rows[0]
    from_location = first.get("from_location") or {}
    from_code = first.get("from_code") or ""
    from_label = first.get("from_label") or ""
    request_items = _aggregate_items(rows)
    requested_barcodes = sorted(
        {
            barcode
            for item in request_items
            for barcode in (item.get("requested_barcodes") or [])
            if barcode
        }
    )
    box_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        box_code = str(row.get("box_code") or "").strip()
        if box_code:
            box_rows[box_code].append(row)
    pattern_groups: dict[tuple[int, tuple[tuple[str, int], ...]], dict[str, Any]] = {}
    for box_code in box_codes:
        current_rows = box_rows.get(box_code) or []
        barcode_qty: dict[str, int] = defaultdict(int)
        box_qty = 0
        for row in current_rows:
            row_qty = _as_int(row.get("qty"))
            box_qty += row_qty
            for barcode, barcode_count in dict(row.get("barcode_qty") or {}).items():
                barcode_qty[str(barcode)] += _as_int(barcode_count)
        signature = (box_qty, tuple(sorted(barcode_qty.items())))
        pattern = pattern_groups.setdefault(
            signature,
            {
                "box_qty": box_qty,
                "barcode_qty": dict(barcode_qty),
                "requested_box_count": 0,
            },
        )
        pattern["requested_box_count"] += 1
    requested_box_patterns = list(pattern_groups.values())
    piece_pick = any(bool(row.get("is_partial_pick")) for row in rows)
    destination = _location_payload(destination_location)
    destination_code = str(destination.get("code") or "").strip()
    destination_label = str(destination.get("label") or destination_code).strip()

    if full_pallet:
        instruction = f"Возьми паллету {pallet_code} целиком и доставь в {destination_code}."
        move_mode = "pallet_full"
        pick_mode = "full"
        requested_boxes = box_codes
        requested_box_selection = ""
    elif piece_pick:
        box_word = _plural_boxes(box_count)
        instruction = (
            f"Возьми паллету {pallet_code}, отбери поштучно {qty} шт. "
            f"из {box_count} {box_word} строго по плану обработки. Остаток верни в исходные короба "
            f"и поставь паллету на исходное место ({from_label}). Отобранный товар доставь в {destination_code}."
        )
        move_mode = "box_partial"
        pick_mode = "partial"
        requested_boxes = box_codes
        requested_box_selection = ""
    else:
        box_word = _plural_boxes(box_count)
        instruction = (
            f"Возьми паллету {pallet_code}, сними {box_count} {box_word} "
            f"с товаром по плану обработки. Паллету верни на исходное место "
            f"({from_label}). Снятые короба доставь в {destination_code}."
        )
        move_mode = "box_full"
        pick_mode = "full"
        requested_boxes = box_codes
        requested_box_selection = ""

    selected_candidate = {
        "pallet_code": pallet_code,
        "available_qty": qty,
        "from_location": from_location,
        "from_label": from_label,
        "receiving_order_id": first.get("receiving_order_id") or "",
    }
    payload = {
        "obr_contour": True,
        "processing_reachtruck": True,
        "move_request_id": move_request.id,
        "processing_order_id": processing_order_id,
        "pallet_code": pallet_code,
        "planned_pallet_code": pallet_code,
        "selected_pallet_code": pallet_code,
        "selected_candidate_pallet": selected_candidate,
        "candidate_pallets": [selected_candidate],
        "flexible_pallet_choice": False,
        "pallet_choice_pending": False,
        "destination_code": destination_code,
        "to_code": destination_code,
        "to_label": destination_label,
        "to_location": destination,
        "source_code": from_code,
        "from_code": from_code,
        "from_label": from_label,
        "from_location": from_location,
        "move_mode": move_mode,
        "pick_mode": pick_mode,
        "instruction": instruction,
        "requested_qty": qty,
        "available_qty": qty,
        "requested_rows": rows,
        "requested_boxes": requested_boxes,
        "requested_box": requested_boxes[0] if len(requested_boxes) == 1 else "",
        "requested_box_count": box_count,
        "requested_box_selection": requested_box_selection,
        "requested_box_patterns": requested_box_patterns,
        "request_items": request_items,
        "requested_barcodes": requested_barcodes,
        "requested_barcode_qty": _aggregate_barcode_qty(rows),
        "requested_sku": request_items[0].get("requested_article") if len(request_items) == 1 else "",
        "requested_goods_type": request_items[0].get("requested_goods_type") if len(request_items) == 1 else "",
        "requested_by_name": employee_name,
        "requested_by_role": employee_role,
        "task_category": "movement",
        "mobile_category": "movement",
        "status": "created",
        "status_label": "Создано",
    }
    if concrete_location_required:
        payload["concrete_location_required"] = True
        payload["concrete_location_version"] = 1
    return payload


def processing_order_requires_concrete_location(*, agency, processing_order_id: str) -> bool:
    return OrderAuditEntry.objects.filter(
        agency=agency,
        order_type="processing",
        order_id=str(processing_order_id or "").strip(),
        payload__processing_concrete_location_required=True,
    ).exists()


def complete_obr_move_requests_for_processing(*, processing_order_id: str) -> int:
    order_key = str(processing_order_id or "").strip()
    if not order_key:
        return 0

    closed_statuses = {MoveTask.STATUS_DONE, MoveTask.STATUS_CANCELED}
    move_requests = (
        MoveRequest.objects.filter(
            context_type="processing",
            context_id=order_key,
            destination_zone="OBR",
        )
        .exclude(status__in=[MoveRequest.STATUS_DONE, MoveRequest.STATUS_CANCELED])
        .order_by("id")
    )
    completed_count = 0
    for move_request in move_requests:
        active_tasks = move_request.tasks.exclude(status__in=closed_statuses).order_by("id")
        for move_task in active_tasks:
            legacy_order_id = str(move_task.legacy_order_id or "").strip()
            if not legacy_order_id:
                continue
            qty_done = int(move_task.qty_done or 0) or int(move_task.qty_planned or 0)
            synced_task = sync_task_status_by_legacy_order_id(
                legacy_order_id,
                status=MoveTask.STATUS_DONE,
                qty_done=qty_done,
            )
            if synced_task:
                completed_count += 1

        if not move_request.tasks.exclude(status__in=closed_statuses).exists():
            done_task = (
                move_request.tasks.filter(status=MoveTask.STATUS_DONE)
                .exclude(legacy_order_id="")
                .order_by("-updated_at", "-id")
                .first()
            )
            if done_task:
                sync_task_status_by_legacy_order_id(
                    str(done_task.legacy_order_id or ""),
                    status=MoveTask.STATUS_DONE,
                    qty_done=int(done_task.qty_done or 0) or int(done_task.qty_planned or 0),
                )

    return completed_count


def create_obr_move_request_response(
    *,
    request,
    agency,
    processing_order_id: Any,
    request_items: list[dict[str, Any]] | None,
) -> JsonResponse:
    order_key = str(processing_order_id or "").strip()
    if not order_key:
        return JsonResponse({"ok": False, "error": "Не указана заявка обработки."}, status=400)
    concrete_location_required = processing_order_requires_concrete_location(
        agency=agency,
        processing_order_id=order_key,
    )

    from .quantity_queue import create_quantity_queue, quantity_order_payload
    if quantity_order_payload(agency, order_key) is not None:
        return create_quantity_queue(request=request, agency=agency, order_key=order_key, request_items=request_items)

    active_request = (
        MoveRequest.objects.filter(
            agency=agency,
            context_type="processing",
            context_id=order_key,
            destination_zone="OBR",
        )
        .filter(
            Q(
                status__in=[
                    MoveRequest.STATUS_CREATED,
                    MoveRequest.STATUS_PLANNED,
                    MoveRequest.STATUS_IN_PROGRESS,
                    MoveRequest.STATUS_BLOCKED,
                ]
            )
            | Q(
                status=MoveRequest.STATUS_PARTIAL,
                tasks__status__in=[
                    MoveTask.STATUS_CREATED,
                    MoveTask.STATUS_IN_PROGRESS,
                    MoveTask.STATUS_FAILED,
                ],
            )
        )
        .distinct()
        .first()
    )
    if active_request:
        return JsonResponse(
            {"ok": False, "error": "Для этой обработки уже есть активное задание ричтракеру в OBR."},
            status=400,
        )

    user = getattr(request, "user", None)
    employee = get_request_employee(request)
    employee_role = get_request_role(request) or "processing_head"
    employee_name = _employee_name(employee, user)
    requested_by = user if user and getattr(user, "is_authenticated", False) else None
    with transaction.atomic():
        WarehouseWritePathService.replace_processing_reserves(
            agency=agency,
            order_id=order_key,
            items=[],
            created_by=requested_by,
        )
        remaining_request_items = _remaining_processing_request_items(
            agency=agency,
            processing_order_id=order_key,
            request_items=request_items,
        )
        if not remaining_request_items:
            transaction.set_rollback(True)
            return JsonResponse(
                {
                    "ok": False,
                    "error": "Товар по этой заявке уже полностью доставлен в OBR.",
                },
                status=400,
            )
        planning_errors: list[str] = []
        rows = build_obr_requested_rows(
            agency=agency,
            processing_order_id=order_key,
            request_items=remaining_request_items,
            planning_errors=planning_errors,
        )
        if not rows:
            transaction.set_rollback(True)
            return JsonResponse(
                {
                    "ok": False,
                    "error": (
                        "Не удалось подобрать свободные короба. "
                        + (" ".join(planning_errors) + " " if planning_errors else "")
                        + "Короба, занятые другими заданиями или отгрузками, не используются. "
                        "Завершите предыдущие отборы и подтвердите возврат остатков либо проверьте доступность товара."
                    ),
                },
                status=400,
            )
        destination_slots = len(
            {
                (
                    str(row.get("pallet_code") or "").strip(),
                    "partial" if row.get("is_partial_pick") else "full",
                )
                for row in rows
                if str(row.get("pallet_code") or "").strip()
            }
        )
        task_concrete_location_required = concrete_location_required
        if concrete_location_required:
            destination_location = select_operational_location(
                zone_code="OBR",
                required_slots=max(destination_slots, 1),
                lock=True,
            )
            if destination_location is None:
                destination_location = WarehouseWritePathService.ensure_location(
                    warehouse_code="MSK",
                    zone_code="OBR",
                )
                task_concrete_location_required = False
        else:
            destination_location = WarehouseWritePathService.ensure_location(
                warehouse_code="MSK",
                zone_code="OBR",
            )
        reserve_items = _processing_reserve_items(rows)
        if reserve_items:
            try:
                WarehouseWritePathService.reserve_for_processing(
                    agency=agency,
                    order_id=order_key,
                    items=reserve_items,
                    created_by=requested_by,
                    source_document_type="processing_obr_request",
                    source_document_id=order_key,
                )
            except ValueError as exc:
                transaction.set_rollback(True)
                return JsonResponse(
                    {
                        "ok": False,
                        "error": (
                            "Не удалось зарезервировать выбранные короба: "
                            f"{exc} Обновите состав заявки."
                        ),
                    },
                    status=409,
                )
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            pallet_code = str(row.get("pallet_code") or "")
            pick_kind = "partial" if row.get("is_partial_pick") else "full"
            grouped[(pallet_code, pick_kind)].append(row)
        move_request = MoveRequest.objects.create(
            context_type="processing",
            context_id=order_key,
            process=MoveRequest.PROCESS_PROCESSING,
            agency=agency,
            requested_by=requested_by,
            requested_by_role=employee_role,
            requested_by_name=employee_name,
            destination_zone="OBR",
            priority=request.POST.get("priority") or "normal",
            comment=request.POST.get("comment") or "",
            status="planned",
        )
        _create_request_items(move_request, rows)

        tasks_created = 0
        for (pallet_code, pick_kind), pallet_rows in grouped.items():
            if not pallet_code:
                continue
            payload = _task_payload(
                move_request=move_request,
                processing_order_id=order_key,
                pallet_code=pallet_code,
                rows=pallet_rows,
                full_pallet=False,
                employee_name=employee_name,
                employee_role=employee_role,
                destination_location=destination_location,
                concrete_location_required=task_concrete_location_required,
            )
            payload["obr_requires_box_scans"] = True
            payload["processing_pick_fact_mode"] = (
                "scanned_units_v1" if pick_kind == "partial" else "scanned_boxes_v1"
            )
            qty = sum(_as_int(row.get("qty")) for row in pallet_rows)
            from_location = pallet_rows[0].get("from_location") or {}
            legacy_order_id = f"PROC-OBR-{move_request.id}-{tasks_created + 1}"
            payload["legacy_move_id"] = legacy_order_id
            move_task = MoveTask.objects.create(
                request=move_request,
                pallet_code=pallet_code,
                from_zone=from_location.get("zone") or "OS",
                from_row=_as_int(from_location.get("row")) or None,
                from_section=_as_int(from_location.get("section")) or None,
                from_tier=_as_int(from_location.get("tier")) or None,
                from_cell=_as_int(from_location.get("cell")) or None,
                to_zone="OBR",
                move_mode=payload["move_mode"],
                qty_planned=qty,
                payload=payload,
                status="created",
                legacy_order_id=legacy_order_id,
            )
            payload["move_task_id"] = move_task.id
            move_task.payload = payload
            move_task.save(update_fields=["payload", "updated_at"])
            try:
                claim_boxes_for_task(
                    move_task,
                    payload.get("requested_boxes") or [],
                    claimed_by=requested_by,
                    claim_kind=(BoxClaim.KIND_PARTIAL if pick_kind == "partial" else BoxClaim.KIND_BOX),
                    payload={
                        "processing_order_id": order_key,
                        "legacy_order_id": legacy_order_id,
                        "claimed_on": "processing_task_planning",
                    },
                    lock_pallet=False,
                )
            except ValueError as exc:
                transaction.set_rollback(True)
                return JsonResponse({"ok": False, "error": str(exc)}, status=409)
            tasks_created += 1

    return JsonResponse(
        {
            "ok": True,
            "request_id": move_request.id,
            "tasks_created": tasks_created,
            "status": "planned",
        }
    )
