from __future__ import annotations

import copy
import re
import unicodedata
from dataclasses import dataclass
from types import SimpleNamespace

from django.db import transaction
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from audit.models import OrderAuditEntry, log_order_action, log_stock_move
from fullbox.container_codes import validate_scanned_box_identity
from processing_app.stages import (
    PROCESSING_STAGE_OBR_ARRIVED,
    PROCESSING_STAGE_RETURNED_TO_STOCK,
    log_processing_stage,
    processing_return_to_stock_completed,
)
from super_car.models import BoxClaim, MoveTask
from sku.models import SKUBarcode
from sklad.models import WarehouseContainer, WarehouseLocation, WarehouseOperation, WarehouseOperationTask, WarehouseStockSnapshot
from sklad.services.stock_operations import OperationalStockService
from sklad.services.warehouse_write_path import WarehouseWritePathService
from shipping.reservation_units import mark_reservation_units_arrived_to_otg

from .claims import claim_boxes_for_task, lock_pallet_for_task, release_claims_for_task
from .move_requests import (
    _as_int,
    _build_location,
    _location_label,
    _move_instruction,
    _normalize_goods_type,
    _normalize_zone_code,
    _os_line_display_label,
    _resolve_stock_move_mode,
    sync_task_status_by_legacy_order_id,
)
from .putaway_planner import putaway_location_label, putaway_location_scan_code
from .pallet_ops import (
    BOX_SELECTION_ANY_MATCHING,
    BOX_SELECTION_PATTERN_MATCHING,
    MOVE_MODE_BOX_FULL,
    MOVE_MODE_BOX_PARTIAL,
    _barcode_qty_total,
    _box_match_breakdown,
    _consume_box_qty,
    _consume_pallet_qty,
    _find_box_in_placement,
    _find_pallet_in_placement,
    _matching_boxes_for_pallet,
    _move_boxes_to_otg,
    _normalize_barcode_qty_map,
    _normalize_box_code,
    _normalize_move_mode,
    _parse_json_list,
    _payload_box_codes,
    _requested_box_count,
    _requested_box_patterns,
    _requested_box_selection_mode,
    _remove_boxes_from_pallet,
    _resolved_item_barcode,
    _requested_barcode_qty,
    _requested_partial_rows,
    _resolve_otg_box_codes,
    _single_requested_box,
    _stock_box_matches_requested_pattern,
)


@dataclass
class MoveTaskCommandResult:
    ok: bool
    error: str = ""
    task: MoveTask | None = None
    payload: dict | None = None
    message: str = ""
    completed: bool = False

    def __post_init__(self) -> None:
        self.error = _repair_human_mojibake_text(self.error)
        self.message = _repair_human_mojibake_text(self.message)


_HUMAN_MOJIBAKE_REPAIR_STEPS = (
    ("cp1251", "utf-8"),
    ("cp1252", "utf-8"),
    ("latin1", "utf-8"),
)

_HUMAN_MOJIBAKE_PAIR_RE = re.compile(r"[\u0420\u0421\u00c3\u00c2][\u0080-\u00ff\u0400-\u04ff\u2010-\u203f]")
_HUMAN_MOJIBAKE_MARKER_RE = re.compile(
    r"(\u0420[\u0400-\u04ff\u0080-\u00ff\u2010-\u203f])|(\u0421[\u0400-\u04ff\u0080-\u00ff\u2010-\u203f])|(\u00d0[\u0080-\u00ff])|(\u00d1[\u0080-\u00ff])|(\u00c3[\u0080-\u00ff])|(\u00c2[\u0080-\u00ff])"
)


def _human_mojibake_count(value: str) -> int:
    text = str(value or "")
    return len(_HUMAN_MOJIBAKE_MARKER_RE.findall(text)) + text.count("\ufffd") * 3 + text.count("?")


def _human_mojibake_score(value: str) -> tuple[int, int, int, int]:
    text = str(value or "")
    mojibake = _human_mojibake_count(text)
    cyrillic = sum(1 for ch in text if "\u0400" <= ch <= "\u04FF")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    readable = sum(1 for ch in text if ch.isalnum() or ch.isspace() or ch in "-_:;.,/()[]{}?#??+\"'")
    return (-mojibake * 100 + cyrillic + latin, -mojibake, readable, len(text))


def _repair_human_mojibake_text(value: str | None) -> str:
    source = str(value or "")
    if not source:
        return ""
    queue = [source]
    seen: set[str] = set()
    variants: list[str] = []
    while queue and len(seen) < 40:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        variants.append(current)
        for encoding, decoding in _HUMAN_MOJIBAKE_REPAIR_STEPS:
            for errors in ("strict", "ignore"):
                try:
                    repaired = current.encode(encoding, errors=errors).decode(decoding, errors=errors)
                except (UnicodeEncodeError, UnicodeDecodeError, LookupError):
                    continue
                if repaired and repaired not in seen and _human_mojibake_score(repaired) > _human_mojibake_score(current):
                    queue.append(repaired)
    return max(variants, key=_human_mojibake_score)


def _normalize_legacy_order_ids(legacy_order_ids) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in legacy_order_ids or []:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _should_log_source_order_event(*, payload: dict, to_zone: str) -> bool:
    if to_zone != "OTG":
        return True
    shipping_order_id = str(payload.get("shipping_order_id") or "").strip()
    shipping_order_pk = payload.get("shipping_order_pk")
    return not (shipping_order_id or shipping_order_pk)


def _occupied_putaway_pallet_codes(location, *, exclude_code: str = "") -> list[str]:
    exclude_key = str(exclude_code or "").strip().lower()
    seen: set[str] = set()
    result: list[str] = []

    def add_code(raw_code) -> None:
        code = str(raw_code or "").strip()
        key = code.lower()
        if not code or key == exclude_key or key in seen:
            return
        seen.add(key)
        result.append(code)

    for container in WarehouseContainer.objects.filter(
        current_location=location,
        container_type="pallet",
        status="active",
    ).order_by("container_code")[:10]:
        add_code(getattr(container, "container_code", ""))

    if result:
        return result

    snapshots = (
        WarehouseStockSnapshot.objects.filter(location=location, is_archived=False)
        .select_related("parent_container", "container")
        .order_by("container_code")[:50]
    )
    for snapshot in snapshots:
        parent = getattr(snapshot, "parent_container", None)
        container = getattr(snapshot, "container", None)
        add_code(
            getattr(parent, "container_code", "")
            or getattr(container, "container_code", "")
            or getattr(snapshot, "container_code", "")
        )
    return result


def _shipping_order_key_from_payload(payload: dict) -> str:
    for key in ("shipping_order_id", "shipping_order_number", "order_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    order_pk = _as_int(payload.get("shipping_order_pk") or payload.get("order_pk"))
    if order_pk <= 0:
        return ""
    from shipping.models import ShippingOrder

    order = ShippingOrder.objects.filter(pk=order_pk).only("number").first()
    if order and str(order.number or "").strip():
        return str(order.number or "").strip()
    return str(order_pk)


def _otg_operation_container_codes(payload: dict, pallet_code: str) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()

    def add(raw_value) -> None:
        code = str(raw_value or "").strip()
        key = code.lower()
        if not code or key in seen:
            return
        seen.add(key)
        codes.append(code)

    # If completion already resolved the physically delivered boxes, do not
    # broaden the OTG operation back to reserved/requested boxes. In mixed
    # partial picks those fallback lists can include a box that was only opened,
    # while its remaining stock must stay on the source pallet.
    for field_name in ("picked_boxes",):
        raw_values = payload.get(field_name)
        if isinstance(raw_values, str):
            raw_values = raw_values.split(",")
        for raw_code in raw_values or []:
            add(raw_code)
    add(payload.get("picked_box"))
    if codes:
        return codes
    if payload.get("picked_loose_units"):
        return codes
    for field_name in ("requested_boxes", "reserved_box_codes"):
        raw_values = payload.get(field_name)
        if isinstance(raw_values, str):
            raw_values = raw_values.split(",")
        for raw_code in raw_values or []:
            add(raw_code)
    add(payload.get("requested_box"))
    if not codes:
        add(pallet_code)
    return codes


_OTG_COMPLETION_STATE_CODES = {
    "in_otg",
    "palletizing",
    "ready_for_loading",
    "assigned_to_trip",
    "loading_in_progress",
    "loaded_to_vehicle",
}


def _payload_otg_completion_candidate_codes(payload: dict) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()

    def add(raw_value) -> None:
        code = _normalize_box_code(raw_value)
        key = code.lower()
        if not code or key in seen:
            return
        seen.add(key)
        codes.append(code)

    for field_name in ("picked_boxes", "reserved_box_codes", "requested_boxes"):
        raw_values = payload.get(field_name)
        if isinstance(raw_values, str):
            raw_values = raw_values.split(",")
        for raw_code in raw_values or []:
            add(raw_code)
    execution = dict(payload.get("mobile_execution") or {})
    for raw_code in execution.get("boxes_scanned") or []:
        add(raw_code)
    add(payload.get("picked_box"))
    add(payload.get("requested_box"))
    return codes


def _shipping_flow_box_signatures(*, agency, box_codes: list[str]) -> dict[str, dict]:
    normalized_codes: list[str] = []
    seen: set[str] = set()
    for raw_code in box_codes or []:
        code = _normalize_box_code(raw_code)
        key = code.lower()
        if not code or key in seen:
            continue
        seen.add(key)
        normalized_codes.append(code)
    if not agency or not normalized_codes:
        return {}
    rows = (
        WarehouseStockSnapshot.objects.filter(
            agency=agency,
            is_archived=False,
        )
        .filter(Q(container_code__in=normalized_codes) | Q(container__container_code__in=normalized_codes))
        .filter(Q(zone_code="OTG") | Q(warehouse_state_code__in=_OTG_COMPLETION_STATE_CODES))
        .select_related("container")
        .order_by("id")
    )
    by_key: dict[str, dict] = {}
    for snapshot in rows:
        code = _normalize_box_code(
            snapshot.container_code or getattr(snapshot.container, "container_code", "")
        )
        key = code.lower()
        if not code or key not in seen:
            continue
        signature = by_key.setdefault(
            key,
            {
                "code": code,
                "qty": 0,
                "barcode_qty": {},
                "sku_codes": set(),
                "goods_types": set(),
            },
        )
        qty = _as_int(snapshot.qty)
        if qty <= 0:
            continue
        signature["qty"] += qty
        barcode = str(snapshot.barcode or "").strip()
        if barcode:
            signature["barcode_qty"][barcode] = signature["barcode_qty"].get(barcode, 0) + qty
        sku_code = str(snapshot.sku_code or "").strip()
        if sku_code:
            signature["sku_codes"].add(sku_code)
        goods_type = _normalize_goods_type(snapshot.goods_type)
        if goods_type:
            signature["goods_types"].add(goods_type)
    return by_key


def _assign_requested_patterns_to_stock_signatures(payload: dict, signatures: list[dict]) -> list[str]:
    patterns = _expand_requested_box_patterns(payload)
    if not patterns:
        return []
    ordered = [
        signature
        for signature in signatures
        if str(signature.get("code") or "").strip()
    ]
    if len(ordered) < len(patterns):
        return []
    per_pattern: dict[int, list[int]] = {}
    for pattern_idx, pattern in enumerate(patterns):
        matches = [
            box_idx
            for box_idx, signature in enumerate(ordered)
            if _stock_box_matches_requested_pattern(signature, pattern)
        ]
        if not matches:
            return []
        per_pattern[pattern_idx] = matches
    ordered_patterns = sorted(
        per_pattern,
        key=lambda idx: (
            len(per_pattern.get(idx) or []),
            -_as_int((patterns[idx] or {}).get("box_qty")),
            idx,
        ),
    )
    assignment: dict[int, int] = {}
    used_boxes: set[int] = set()

    def backtrack(position: int) -> bool:
        if position >= len(ordered_patterns):
            return True
        pattern_idx = ordered_patterns[position]
        for box_idx in per_pattern.get(pattern_idx) or []:
            if box_idx in used_boxes:
                continue
            used_boxes.add(box_idx)
            assignment[pattern_idx] = box_idx
            if backtrack(position + 1):
                return True
            assignment.pop(pattern_idx, None)
            used_boxes.remove(box_idx)
        return False

    if not backtrack(0):
        return []
    selected_indexes = set(assignment.values())
    return [
        str(signature.get("code") or "").strip()
        for idx, signature in enumerate(ordered)
        if idx in selected_indexes
    ]


def _stock_signature_matches_selector(
    signature: dict,
    barcode_values: set[str],
    sku_values: set[str],
    goods_type_values: set[str],
) -> bool:
    if goods_type_values:
        signature_goods = set(signature.get("goods_types") or set())
        if signature_goods and not (signature_goods & goods_type_values):
            return False
    barcode_qty = _normalize_barcode_qty_map(signature.get("barcode_qty"))
    if barcode_values and set(barcode_qty) & barcode_values:
        return True
    signature_skus = set(signature.get("sku_codes") or set())
    if sku_values and signature_skus & sku_values:
        return True
    return not barcode_values and not sku_values and _as_int(signature.get("qty")) > 0


def otg_already_delivered_box_codes(payload: dict, agency) -> list[str]:
    to_location = payload.get("to_location") or {}
    if _normalize_zone_code(to_location.get("zone") or "") != "OTG":
        return []
    candidate_codes = _payload_otg_completion_candidate_codes(payload)
    signatures_by_key = _shipping_flow_box_signatures(agency=agency, box_codes=candidate_codes)
    if not signatures_by_key:
        return []
    ordered_signatures = [
        signatures_by_key[code.lower()]
        for code in candidate_codes
        if code.lower() in signatures_by_key
    ]
    if _is_any_matching_box_selection(payload):
        selection_mode = _requested_box_selection_mode(payload)
        if selection_mode == BOX_SELECTION_PATTERN_MATCHING:
            return _assign_requested_patterns_to_stock_signatures(payload, ordered_signatures)
        requested_count = _requested_box_count(payload)
        if requested_count <= 0:
            return []
        barcode_values, sku_values, goods_type_values, _requested_qty = _mobile_selector_sets(payload)
        matching = [
            signature
            for signature in ordered_signatures
            if _stock_signature_matches_selector(signature, barcode_values, sku_values, goods_type_values)
        ]
        if len(matching) < requested_count:
            return []
        return [str(signature.get("code") or "").strip() for signature in matching[:requested_count]]
    requested_codes = [
        code
        for code in (_payload_box_codes(payload) or candidate_codes)
        if _normalize_box_code(code)
    ]
    if not requested_codes:
        return []
    if all(_normalize_box_code(code).lower() in signatures_by_key for code in requested_codes):
        return [_normalize_box_code(code) for code in requested_codes]
    return []


def _load_request_tasks(legacy_order_ids) -> list[MoveTask]:
    normalized_ids = _normalize_legacy_order_ids(legacy_order_ids)
    if not normalized_ids:
        return []
    tasks = list(
        MoveTask.objects.select_related("request", "request__agency")
        .filter(legacy_order_id__in=normalized_ids)
        .order_by("created_at", "id")
    )
    by_id = {str(task.legacy_order_id or "").strip(): task for task in tasks}
    return [by_id[target_id] for target_id in normalized_ids if target_id in by_id]


def _request_task_runtime_state(task: MoveTask) -> dict:
    payload = dict(task.payload or {})
    execution = dict(payload.get("mobile_execution") or {})
    status = _task_payload_status(task, payload)
    assigned_to_id = _task_assignee_id(task, payload)
    pallet_code = display_scan_text(payload.get("pallet_code"))
    pallet_choice_pending = _pallet_choice_pending(payload)
    destination_code = _location_scan_code(payload.get("to_location") or {})
    destination_label = str(payload.get("to_label") or destination_code).strip() or destination_code
    override_candidate = (
        execution.get("destination_override_candidate")
        if isinstance(execution.get("destination_override_candidate"), dict)
        else {}
    )
    override_candidate_code = _location_scan_code(override_candidate)
    override_candidate_label = (
        putaway_location_label(override_candidate)
        if override_candidate_code
        else ""
    )
    return {
        "task": task,
        "payload": payload,
        "execution": execution,
        "status": status,
        "assigned_to_id": assigned_to_id,
        "pallet_code": pallet_code,
        "pallet_choice_pending": pallet_choice_pending,
        "candidate_pallets": _candidate_pallet_options(payload),
        "candidate_locations": _candidate_location_groups(payload),
        "destination_code": destination_code,
        "destination_label": destination_label,
        "pallet_confirmed": bool(execution.get("pallet_confirmed")),
        "destination_confirmed": bool(execution.get("destination_confirmed")),
        "destination_override_pending": bool(execution.get("destination_override_pending")),
        "destination_override_candidate": override_candidate,
        "destination_override_candidate_code": override_candidate_code,
        "destination_override_candidate_label": override_candidate_label,
        "destination_override_confirm_pending": bool(
            execution.get("destination_override_pending") and override_candidate_code
        ),
    }


@transaction.atomic
def update_move_task_destination(
    *,
    legacy_order_id: str,
    destination: dict,
    user=None,
    require_created_status: bool = False,
    require_in_progress_employee_id: int | None = None,
    allow_same_context_reservations: bool = False,
    clear_destination_override: bool = True,
) -> MoveTaskCommandResult:
    task = _load_task(legacy_order_id)
    if not task:
        return MoveTaskCommandResult(ok=False, error="Р—Р°РґР°РЅРёРµ РЅРµ РЅР°Р№РґРµРЅРѕ.")

    payload = dict(task.payload or {})
    status = _task_payload_status(task, payload)
    assigned_to_id = _task_assignee_id(task, payload)
    if status == MoveTask.STATUS_CANCELED:
        return MoveTaskCommandResult(ok=False, error="Р—Р°РґР°РЅРёРµ СѓР¶Рµ РѕС‚РјРµРЅРµРЅРѕ.")
    if status == MoveTask.STATUS_DONE:
        return MoveTaskCommandResult(ok=False, error="Р—Р°РґР°РЅРёРµ СѓР¶Рµ РІС‹РїРѕР»РЅРµРЅРѕ.")
    if require_created_status:
        if status != MoveTask.STATUS_CREATED or assigned_to_id:
            return MoveTaskCommandResult(
                ok=False,
                error="Р—Р°РґР°РЅРёРµ СѓР¶Рµ РІР·СЏС‚Рѕ РІ СЂР°Р±РѕС‚Сѓ Рё РЅРµ РјРѕР¶РµС‚ Р±С‹С‚СЊ РёР·РјРµРЅРµРЅРѕ.",
            )
    elif require_in_progress_employee_id is not None:
        if status != MoveTask.STATUS_IN_PROGRESS:
            return MoveTaskCommandResult(ok=False, error="РЎРЅР°С‡Р°Р»Р° РІРѕР·СЊРјРёС‚Рµ Р·Р°РґР°РЅРёРµ РІ СЂР°Р±РѕС‚Сѓ.")
        if assigned_to_id and assigned_to_id != require_in_progress_employee_id:
            return MoveTaskCommandResult(ok=False, error="Р—Р°РґР°РЅРёРµ РЅР°Р·РЅР°С‡РµРЅРѕ РґСЂСѓРіРѕРјСѓ РІРѕРґРёС‚РµР»СЋ.")

    normalized_destination = _build_location(
        _normalize_zone_code((destination or {}).get("zone") or ""),
        _as_int((destination or {}).get("row")),
        _as_int((destination or {}).get("section")),
        _as_int((destination or {}).get("tier")),
        _as_int((destination or {}).get("cell")),
    )
    zone = _normalize_zone_code(normalized_destination.get("zone") or "")
    row = _as_int(normalized_destination.get("row"))
    section = _as_int(normalized_destination.get("section"))
    tier = _as_int(normalized_destination.get("tier"))
    cell = _as_int(normalized_destination.get("cell"))
    if zone not in {"PR", "OTG", "MR", "OS", "OBR"}:
        return MoveTaskCommandResult(ok=False, error="РќРµРєРѕСЂСЂРµРєС‚РЅР°СЏ Р·РѕРЅР° РЅР°Р·РЅР°С‡РµРЅРёСЏ.")
    if zone == "MR" and not row:
        return MoveTaskCommandResult(ok=False, error="Р”Р»СЏ Р·РѕРЅС‹ MR СѓРєР°Р¶РёС‚Рµ СЂСЏРґ.")
    if zone == "OS" and not (row and section and tier and cell):
        return MoveTaskCommandResult(
            ok=False,
            error="РћС‚СЃРєР°РЅРёСЂСѓР№С‚Рµ РїРѕР»РЅРѕРµ РјРµСЃС‚Рѕ С…СЂР°РЅРµРЅРёСЏ РІ С„РѕСЂРјР°С‚Рµ Р»РёРЅРёРё, СЃС‚РµР»Р»Р°Р¶Р°, СЌС‚Р°Р¶Р° Рё СЏС‡РµР№РєРё.",
        )

    location = WarehouseWritePathService.ensure_location(
        warehouse_code="MSK",
        zone_code=zone or "PR",
        row_no=row,
        section_no=section,
        tier_no=tier,
        cell_no=cell,
    )
    location_label = str(location.display_name or putaway_location_label(normalized_destination) or "").strip()
    location_code = _location_scan_code(normalized_destination)

    warehouse_task_id = payload.get("warehouse_operation_task_id")
    warehouse_operation_id = payload.get("warehouse_operation_id")
    warehouse_task = None
    if warehouse_task_id:
        try:
            warehouse_task = WarehouseOperationTask.objects.select_related("operation").get(id=int(warehouse_task_id))
        except (WarehouseOperationTask.DoesNotExist, TypeError, ValueError):
            warehouse_task = None
    if warehouse_task:
        operation = warehouse_task.operation
    else:
        operation = None
        if warehouse_operation_id:
            try:
                operation = WarehouseOperation.objects.get(id=int(warehouse_operation_id))
            except (WarehouseOperation.DoesNotExist, TypeError, ValueError):
                operation = None

    exclude_context_type = ""
    exclude_context_id = ""
    if allow_same_context_reservations:
        exclude_context_type = str(
            getattr(operation, "context_type", "")
            or getattr(getattr(task, "request", None), "context_type", "")
            or ""
        ).strip()
        exclude_context_id = str(
            getattr(operation, "context_id", "")
            or getattr(getattr(task, "request", None), "context_id", "")
            or ""
        ).strip()
    if zone == "OS":
        try:
            WarehouseWritePathService.ensure_putaway_destination_available(
                destination=location,
                exclude_operation_id=operation.id if operation else None,
                exclude_container_code=str(task.pallet_code or payload.get("pallet_code") or "").strip(),
                exclude_context_type=exclude_context_type,
                exclude_context_id=exclude_context_id,
            )
        except ValueError as exc:
            occupied_codes = _occupied_putaway_pallet_codes(
                location,
                exclude_code=str(task.pallet_code or payload.get("pallet_code") or "").strip(),
            )
            if occupied_codes:
                occupied_label = ", ".join(occupied_codes[:3])
                return MoveTaskCommandResult(
                    ok=False,
                    error=(
                        f"\u041c\u0435\u0441\u0442\u043e {location_code} \u0443\u0436\u0435 "
                        f"\u0437\u0430\u043d\u044f\u0442\u043e \u043f\u0430\u043b\u043b\u0435\u0442\u043e\u0439 {occupied_label}. "
                        "\u041e\u0442\u0441\u043a\u0430\u043d\u0438\u0440\u0443\u0439\u0442\u0435 \u0434\u0440\u0443\u0433\u043e\u0435 \u043c\u0435\u0441\u0442\u043e."
                    ),
                )
            return MoveTaskCommandResult(ok=False, error=str(exc))

    task.to_zone = zone or ""
    task.to_row = row or None
    task.to_section = section or None
    task.to_tier = tier or None
    task.to_cell = cell or None
    payload["to_location"] = normalized_destination
    payload["to_label"] = location_label
    payload["destination_label"] = location_label
    payload["to_code"] = location_code
    payload["destination_code"] = location_code
    execution = dict(payload.get("mobile_execution") or {})
    if clear_destination_override and execution:
        execution["destination_override_pending"] = False
        execution.pop("destination_override_candidate", None)
        payload["mobile_execution"] = execution
    task.payload = payload
    task.save(
        update_fields=[
            "to_zone",
            "to_row",
            "to_section",
            "to_tier",
            "to_cell",
            "payload",
            "updated_at",
        ]
    )

    move_request = task.request
    if move_request:
        move_request.destination_zone = zone or ""
        move_request.destination_row = row or None
        move_request.destination_section = section or None
        move_request.destination_tier = tier or None
        move_request.destination_cell = cell or None
        move_request.save(
            update_fields=[
                "destination_zone",
                "destination_row",
                "destination_section",
                "destination_tier",
                "destination_cell",
                "updated_at",
            ]
        )

    if warehouse_task:
        warehouse_task.to_location = location
        warehouse_task.to_zone_code = zone or ""
        warehouse_payload = dict(warehouse_task.payload or {})
        warehouse_payload["destination_label"] = location_label
        warehouse_task.payload = warehouse_payload
        warehouse_task.save(update_fields=["to_location", "to_zone_code", "payload", "updated_at"])
    if operation:
        operation.destination_location = location
        operation.destination_zone_code = zone or ""
        operation.save(update_fields=["destination_location", "destination_zone_code", "updated_at"])

    move_agency = getattr(move_request, "agency", None)
    description = f"РР·РјРµРЅРµРЅРѕ РјРµСЃС‚Рѕ РЅР°Р·РЅР°С‡РµРЅРёСЏ: {location_label or _location_label(normalized_destination)}"
    log_order_action(
        "update",
        order_id=str(task.legacy_order_id or "").strip(),
        order_type="stock_move",
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=move_agency,
        description=description,
        payload=payload,
    )
    log_stock_move(
        "update",
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=move_agency,
        description=description,
        snapshot={
            "move_id": str(task.legacy_order_id or "").strip(),
            "pallet_code": payload.get("pallet_code"),
            "to_location": payload.get("to_location"),
            "to_label": payload.get("to_label"),
            "destination_override": True,
        },
    )
    return MoveTaskCommandResult(
        ok=True,
        task=task,
        payload=payload,
        message=f"РњРµСЃС‚Рѕ РЅР°Р·РЅР°С‡РµРЅРёСЏ РёР·РјРµРЅРµРЅРѕ РЅР° {location_code or location_label}.",
    )


def build_mobile_request_execution_snapshot(
    legacy_order_ids,
    *,
    employee_id: int | None = None,
) -> dict:
    tasks = _load_request_tasks(legacy_order_ids)
    if not tasks:
        return {}

    states = [_request_task_runtime_state(task) for task in tasks]
    remaining = [state for state in states if state["status"] != MoveTask.STATUS_DONE]
    active_task = next(
        (
            state
            for state in remaining
            if state["status"] == MoveTask.STATUS_IN_PROGRESS
            and state["assigned_to_id"]
            and (employee_id is None or state["assigned_to_id"] == employee_id)
            and state["pallet_confirmed"]
            and not state["destination_confirmed"]
        ),
        None,
    )
    taken_by_other = any(
        state["status"] == MoveTask.STATUS_IN_PROGRESS
        and state["assigned_to_id"]
        and employee_id is not None
        and state["assigned_to_id"] != employee_id
        for state in remaining
    )
    all_taken_by_current = bool(remaining) and all(
        state["status"] == MoveTask.STATUS_IN_PROGRESS
        and state["assigned_to_id"]
        and (employee_id is None or state["assigned_to_id"] == employee_id)
        for state in remaining
    )
    can_take = bool(remaining) and not taken_by_other and not all_taken_by_current
    can_scan = bool(remaining) and not taken_by_other and all_taken_by_current
    prompt = "\u041e\u0442\u0441\u043a\u0430\u043d\u0438\u0440\u0443\u0439\u0442\u0435 QR-\u043a\u043e\u0434 \u043f\u0430\u043b\u043b\u0435\u0442\u044b \u0438\u0437 \u0437\u0430\u044f\u0432\u043a\u0438."
    expected_scan = ""
    current_step = "pallet"
    candidate_locations: list[dict] = []
    for state in remaining:
        if state.get("pallet_choice_pending"):
            candidate_locations.extend(state.get("candidate_locations") or [])
    if candidate_locations:
        prompt = "\u0412\u044b\u0431\u0435\u0440\u0438 \u043c\u0435\u0441\u0442\u043e \u0438\u0437 \u0441\u043f\u0438\u0441\u043a\u0430 \u0438 \u043e\u0442\u0441\u043a\u0430\u043d\u0438\u0440\u0443\u0439 QR \u043f\u043e\u0434\u0445\u043e\u0434\u044f\u0449\u0435\u0439 \u043f\u0430\u043b\u043b\u0435\u0442\u044b."
    active_destination_code = ""
    active_destination_label = ""
    destination_override_confirm_pending = False
    destination_override_candidate_code = ""
    destination_override_candidate_label = ""
    if active_task:
        if active_task["destination_override_pending"]:
            destination_override_confirm_pending = bool(
                active_task["destination_override_confirm_pending"]
            )
            destination_override_candidate_code = str(
                active_task["destination_override_candidate_code"] or ""
            ).strip()
            destination_override_candidate_label = str(
                active_task["destination_override_candidate_label"] or ""
            ).strip()
            if destination_override_confirm_pending:
                prompt = "РўРѕС‡РЅРѕ СЃСЋРґР°?"
                active_destination_code = destination_override_candidate_code
                active_destination_label = destination_override_candidate_label or active_destination_code
            else:
                prompt = "РЎРєР°РЅРёСЂСѓР№ РЅРѕРІРѕРµ РјРµСЃС‚Рѕ РЅР°Р·РЅР°С‡РµРЅРёСЏ"
            expected_scan = ""
        else:
            prompt = f"РћС‚РІРµР·Рё -> {active_task['destination_code']}"
            expected_scan = active_task["destination_code"]
            active_destination_code = active_task["destination_code"]
            active_destination_label = active_task["destination_label"]
        current_step = "destination"
    return {
        "total_count": len(tasks),
        "remaining_count": len(remaining),
        "completed_count": max(len(tasks) - len(remaining), 0),
        "can_take": can_take,
        "can_scan": can_scan,
        "taken_by_other": taken_by_other,
        "current_step": current_step,
        "prompt": prompt,
        "expected_scan": expected_scan,
        "candidate_locations": candidate_locations,
        "destination_override_pending": bool(active_task and active_task["destination_override_pending"]),
        "destination_override_confirm_pending": destination_override_confirm_pending,
        "destination_override_candidate_code": destination_override_candidate_code,
        "destination_override_candidate_label": destination_override_candidate_label,
        "active_order_id": str(active_task["task"].legacy_order_id).strip() if active_task else "",
        "active_pallet_code": active_task["pallet_code"] if active_task else "",
        "active_destination_code": active_destination_code,
        "active_destination_label": active_destination_label,
    }


def _store_request_destination_override_candidate(
    *,
    task: MoveTask,
    destination: dict,
    scan_code: str,
) -> MoveTaskCommandResult:
    payload = dict(task.payload or {})
    execution = dict(payload.get("mobile_execution") or {})
    destination_code = _location_scan_code(destination)
    destination_label = putaway_location_label(destination)
    execution["destination_override_pending"] = True
    execution["destination_override_candidate"] = destination
    execution["last_scan"] = destination_code or str(scan_code or "").strip()
    payload["mobile_execution"] = execution
    task.payload = payload
    task.save(update_fields=["payload", "updated_at"])
    return MoveTaskCommandResult(
        ok=True,
        task=task,
        payload=payload,
        message=(
            f"РќРѕРІРѕРµ РјРµСЃС‚Рѕ {destination_code or destination_label} РІС‹Р±СЂР°РЅРѕ. "
            "РџРѕРґС‚РІРµСЂРґРёС‚Рµ РµРіРѕ РєРЅРѕРїРєРѕР№ РёР»Рё РѕС‚СЃРєР°РЅРёСЂСѓР№С‚Рµ РґСЂСѓРіРѕРµ."
        ),
    )


def confirm_move_request_destination_override(
    legacy_order_ids,
    *,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    tasks = _load_request_tasks(legacy_order_ids)
    if not tasks:
        return MoveTaskCommandResult(ok=False, error="Р—Р°СЏРІРєР° СЂРёС‡С‚СЂР°РєР° РЅРµ РЅР°Р№РґРµРЅР°.")
    if not employee_id:
        return MoveTaskCommandResult(ok=False, error="РџСЂРѕС„РёР»СЊ СЃРѕС‚СЂСѓРґРЅРёРєР° РЅРµ РЅР°Р№РґРµРЅ.")

    snapshot = build_mobile_request_execution_snapshot(legacy_order_ids, employee_id=employee_id)
    if not snapshot:
        return MoveTaskCommandResult(ok=False, error="РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕРґРіРѕС‚РѕРІРёС‚СЊ Р·Р°СЏРІРєСѓ Рє РїРѕРґС‚РІРµСЂР¶РґРµРЅРёСЋ.")
    active_order_id = str(snapshot.get("active_order_id") or "").strip()
    if not snapshot.get("can_scan"):
        return MoveTaskCommandResult(ok=False, error="РЎРЅР°С‡Р°Р»Р° РІРѕР·СЊРјРёС‚Рµ РІСЃСЋ Р·Р°СЏРІРєСѓ РІ СЂР°Р±РѕС‚Сѓ.")
    if not bool(snapshot.get("destination_override_pending")):
        return MoveTaskCommandResult(ok=False, error="РЎРјРµРЅР° РјРµСЃС‚Р° РЅР°Р·РЅР°С‡РµРЅРёСЏ СЃРµР№С‡Р°СЃ РЅРµ Р°РєС‚РёРІРЅР°.")
    if not active_order_id:
        return MoveTaskCommandResult(ok=False, error="РђРєС‚РёРІРЅР°СЏ РїР°Р»Р»РµС‚Р° Р·Р°СЏРІРєРё РЅРµ РЅР°Р№РґРµРЅР°.")

    task_map = {str(task.legacy_order_id or "").strip(): task for task in tasks}
    active_task = task_map.get(active_order_id)
    if not active_task:
        return MoveTaskCommandResult(ok=False, error="РђРєС‚РёРІРЅР°СЏ РїР°Р»Р»РµС‚Р° Р·Р°СЏРІРєРё РЅРµ РЅР°Р№РґРµРЅР°.")

    payload = dict(active_task.payload or {})
    execution = dict(payload.get("mobile_execution") or {})
    destination = (
        execution.get("destination_override_candidate")
        if isinstance(execution.get("destination_override_candidate"), dict)
        else {}
    )
    destination_code = _location_scan_code(destination)
    if not destination_code:
        return MoveTaskCommandResult(ok=False, error="РЎРЅР°С‡Р°Р»Р° РѕС‚СЃРєР°РЅРёСЂСѓР№С‚Рµ РЅРѕРІРѕРµ РјРµСЃС‚Рѕ РЅР°Р·РЅР°С‡РµРЅРёСЏ.")

    update_result = update_move_task_destination(
        legacy_order_id=active_order_id,
        destination=destination,
        user=user,
        require_in_progress_employee_id=employee_id,
        allow_same_context_reservations=True,
        clear_destination_override=True,
    )
    if not update_result.ok:
        return update_result

    active_task = update_result.task or active_task
    payload = dict(update_result.payload or active_task.payload or {})
    execution = dict(payload.get("mobile_execution") or {})
    execution["destination_confirmed"] = True
    execution["destination_override_pending"] = False
    execution.pop("destination_override_candidate", None)
    execution["last_scan"] = destination_code
    payload["mobile_execution"] = execution
    active_task.payload = payload
    active_task.save(update_fields=["payload", "updated_at"])

    complete_result = complete_move_task(
        legacy_order_id=active_order_id,
        user=user,
        employee_id=employee_id,
        employee_name=employee_name,
    )
    if not complete_result.ok:
        return complete_result

    for task in tasks:
        task.refresh_from_db(fields=["status", "payload", "updated_at"])
    remaining_count = sum(1 for task in tasks if task.status != MoveTask.STATUS_DONE)
    if remaining_count <= 0:
        return MoveTaskCommandResult(
            ok=True,
            message=f"РњРµСЃС‚Рѕ {destination_code} РїРѕРґС‚РІРµСЂР¶РґРµРЅРѕ. Р’СЃРµ РїР°Р»Р»РµС‚С‹ РїРѕ Р·Р°СЏРІРєРµ РґРѕСЃС‚Р°РІР»РµРЅС‹.",
            completed=True,
        )
    return MoveTaskCommandResult(
        ok=True,
        message=f"РњРµСЃС‚Рѕ {destination_code} РїРѕРґС‚РІРµСЂР¶РґРµРЅРѕ. РџР°Р»Р»РµС‚Р° РґРѕСЃС‚Р°РІР»РµРЅР°, СЃРєР°РЅРёСЂСѓР№С‚Рµ СЃР»РµРґСѓСЋС‰СѓСЋ.",
        completed=False,
    )


def _load_task(legacy_order_id: str) -> MoveTask | None:
    target_id = str(legacy_order_id or "").strip()
    if not target_id:
        return None
    return (
        MoveTask.objects.select_related("request", "request__agency")
        .filter(legacy_order_id=target_id)
        .order_by("-updated_at")
        .first()
    )


def _task_payload_status(task: MoveTask, payload: dict) -> str:
    task_status = str(task.status or "").strip().lower()
    if task_status:
        return task_status
    return str(payload.get("status") or "").strip().lower()


def _task_assignee_id(task: MoveTask, payload: dict) -> int | None:
    assigned_to_id = payload.get("assigned_to_id")
    if assigned_to_id in (None, ""):
        return int(task.assigned_to_id) if task.assigned_to_id else None
    try:
        return int(assigned_to_id)
    except (TypeError, ValueError):
        return int(task.assigned_to_id) if task.assigned_to_id else None


def _promote_created_assigned_task(
    task: MoveTask,
    payload: dict,
    *,
    status: str,
    assigned_to_id: int | None,
    user,
    employee_id: int | None,
    employee_name: str,
) -> tuple[str, int | None, dict]:
    if (
        status != MoveTask.STATUS_CREATED
        or not employee_id
        or not assigned_to_id
        or assigned_to_id != employee_id
    ):
        return status, assigned_to_id, payload

    authenticated_user = user if getattr(user, "is_authenticated", False) else None
    payload["status"] = MoveTask.STATUS_IN_PROGRESS
    payload["status_label"] = "\u0412 \u0440\u0430\u0431\u043e\u0442\u0435"
    payload["assigned_to_id"] = employee_id
    payload["assigned_to_name"] = employee_name or payload.get("assigned_to_name") or task.assigned_to_name or ""
    payload.setdefault("taken_at", timezone.localtime().isoformat())

    task.status = MoveTask.STATUS_IN_PROGRESS
    if authenticated_user and not task.assigned_to_id:
        task.assigned_to = authenticated_user
    task.assigned_to_name = payload["assigned_to_name"]
    task.started_at = task.started_at or timezone.now()
    task.payload = payload
    update_fields = ["status", "assigned_to_name", "started_at", "payload", "updated_at"]
    if authenticated_user and not task.assigned_to_id:
        update_fields.append("assigned_to")
    task.save(update_fields=update_fields)
    sync_task_status_by_legacy_order_id(
        task.legacy_order_id,
        status=MoveTask.STATUS_IN_PROGRESS,
        assigned_to=authenticated_user if authenticated_user else task.assigned_to,
        assigned_to_name=task.assigned_to_name,
    )
    task.refresh_from_db()
    payload = dict(task.payload or {})
    return _task_payload_status(task, payload), _task_assignee_id(task, payload), payload


def _legacy_location_scan_code(location: dict | None) -> str:
    location = location or {}
    raw_zone = str(location.get("zone") or "").strip()
    row = _as_int(location.get("row"))
    section = _as_int(location.get("section"))
    tier = _as_int(location.get("tier"))
    cell = _as_int(location.get("cell"))
    if not raw_zone and not any((row, section, tier, cell)):
        return ""
    zone = _normalize_zone_code(raw_zone) or "PR"
    if zone == "OS":
        return f"OS-{row}-{section}-{tier}-{cell}"
    if zone == "MR":
        return f"MR-{row}" if row else "MR"
    return zone


def _location_scan_code(location: dict | None) -> str:
    code = str(putaway_location_scan_code(location) or "").strip()
    if code:
        return code
    return _legacy_location_scan_code(location)


_SCAN_REPAIR_STEPS = (
    ("gb18030", "utf-8"),
    ("gbk", "utf-8"),
    ("cp1252", "utf-8"),
    ("cp1251", "utf-8"),
    ("latin1", "cp1251"),
    ("latin1", "utf-8"),
)
_SCAN_DASH_TRANSLATION = str.maketrans(
    {
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u2011": "-",
        "\u2010": "-",
        "\u2012": "-",
        "\ufe63": "-",
        "\uff0d": "-",
    }
)
_OS_SECTION_BY_SCAN_LABEL = {
    str(_os_line_display_label(section)).strip().upper(): int(section)
    for section in range(1, 100)
    if str(_os_line_display_label(section)).strip()
}
_OS_SCAN_RE = re.compile(r"^(?P<line>[0-9A-Z]+)-(?P<row>\d+)/(?P<tier>\d+)-(?P<cell>\d+)$")
_LEGACY_OS_SCAN_RE = re.compile(r"^OS-(?P<row>\d+)-(?P<section>\d+)-(?P<tier>\d+)-(?P<cell>\d+)$")
_MR_SCAN_RE = re.compile(r"^MR(?:-(?P<row>\d+))?$")


def _normalized_scan_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.translate(_SCAN_DASH_TRANSLATION)
    text = text.replace("\u00a0", " ")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return "".join(text.split()).casefold()


def _prepared_location_scan_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.translate(_SCAN_DASH_TRANSLATION)
    text = text.replace("\u00a0", " ")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return "".join(text.split()).upper()


def _destination_from_scan_value(scan_value: str | None) -> dict:
    scan_text = _prepared_location_scan_text(scan_value)
    if not scan_text:
        return {}
    if scan_text in {"PR", "OBR", "OTG", "OS", "US"}:
        return {}
    exact_location = (
        WarehouseLocation.objects.filter(
            warehouse_code="MSK",
            location_code__iexact=scan_text,
            is_active=True,
        )
        .order_by("id")
        .first()
    )
    if exact_location is not None:
        try:
            from sklad.services.operational_locations import require_concrete_movement_location

            require_concrete_movement_location(exact_location)
        except ValidationError:
            return {}
        return {
            "zone": str(exact_location.zone_code or "").strip().upper(),
            "code": str(exact_location.location_code or "").strip(),
            "label": str(
                exact_location.display_name or exact_location.location_code or ""
            ).strip(),
            "row": int(exact_location.row_no or 0) or "",
            "section": int(exact_location.section_no or 0) or "",
            "tier": int(exact_location.tier_no or 0) or "",
            "cell": int(exact_location.cell_no or 0) or "",
        }
    legacy_match = _LEGACY_OS_SCAN_RE.match(scan_text)
    if legacy_match:
        return _build_location(
            "OS",
            _as_int(legacy_match.group("row")),
            _as_int(legacy_match.group("section")),
            _as_int(legacy_match.group("tier")),
            _as_int(legacy_match.group("cell")),
        )
    mr_match = _MR_SCAN_RE.match(scan_text)
    if mr_match:
        return _build_location("MR", _as_int(mr_match.group("row")), 0, 0, 0)
    os_match = _OS_SCAN_RE.match(scan_text)
    if not os_match:
        return {}
    section_token = str(os_match.group("line") or "").strip().upper()
    section = _OS_SECTION_BY_SCAN_LABEL.get(section_token)
    if section is None and section_token.isdigit():
        section = _as_int(section_token)
    return _build_location(
        "OS",
        _as_int(os_match.group("row")),
        int(section or 0),
        _as_int(os_match.group("tier")),
        _as_int(os_match.group("cell")),
    )


def _scan_compare_variants(value: str | None) -> set[str]:
    source = str(value or "")
    queue = [source]
    seen_raw: set[str] = set()
    variants: set[str] = set()
    while queue and len(seen_raw) < 16:
        current = queue.pop(0)
        if current in seen_raw:
            continue
        seen_raw.add(current)
        normalized = _normalized_scan_text(current)
        if normalized:
            variants.add(normalized)
        for encoding, decoding in _SCAN_REPAIR_STEPS:
            try:
                repaired = current.encode(encoding).decode(decoding)
            except (UnicodeEncodeError, UnicodeDecodeError, LookupError):
                continue
            if repaired and repaired not in seen_raw:
                queue.append(repaired)
    if not variants:
        variants.add("")
    return variants


def _display_scan_variants(value: str | None) -> list[str]:
    source = str(value or "").strip()
    if not source:
        return [""]
    queue = [source]
    seen_raw: set[str] = set()
    variants: list[str] = []
    while queue and len(seen_raw) < 16:
        current = queue.pop(0)
        if current in seen_raw:
            continue
        seen_raw.add(current)
        variants.append(current)
        for encoding, decoding in _SCAN_REPAIR_STEPS:
            try:
                repaired = current.encode(encoding).decode(decoding)
            except (UnicodeEncodeError, UnicodeDecodeError, LookupError):
                continue
            repaired = str(repaired or "").strip()
            if repaired and repaired not in seen_raw:
                queue.append(repaired)
    return variants or [source]


def _display_variant_score(value: str) -> tuple[int, int, int, int, int]:
    text = str(value or "")
    cyrillic = sum(1 for ch in text if "\u0400" <= ch <= "\u04FF")
    ascii_alnum = sum(1 for ch in text if ch.isascii() and ch.isalnum())
    cjk = sum(
        1
        for ch in text
        if (
            "\u3400" <= ch <= "\u4DBF"
            or "\u4E00" <= ch <= "\u9FFF"
            or "\uF900" <= ch <= "\uFAFF"
        )
    )
    replacements = text.count("пїЅ") + text.count("?")
    normalized = sum(1 for ch in text if ch in "-_/ " or ch.isalnum())
    return (cyrillic, -cjk, -replacements, normalized, ascii_alnum)


def display_scan_text(value: str | None) -> str:
    variants = _display_scan_variants(value)
    return max(variants, key=_display_variant_score)


_PALLET_DISPLAY_TRANSLATION = str.maketrans(
    {
        "\u0410": "A",
        "\u0412": "B",
        "\u0415": "E",
        "\u041a": "K",
        "\u041c": "M",
        "\u041d": "H",
        "\u041e": "O",
        "\u0420": "P",
        "\u0421": "C",
        "\u0422": "T",
        "\u0423": "Y",
        "\u0425": "X",
        "\u0414": "D",
        "\u0430": "a",
        "\u0432": "b",
        "\u0435": "e",
        "\u043a": "k",
        "\u043c": "m",
        "\u043d": "h",
        "\u043e": "o",
        "\u0440": "p",
        "\u0441": "c",
        "\u0442": "t",
        "\u0443": "y",
        "\u0445": "x",
        "\u0434": "d",
    }
)


def display_pallet_scan_text(value: str | None) -> str:
    text = display_scan_text(value)
    prefix, sep, tail = text.partition("-")
    if not sep:
        return text
    ascii_prefix = prefix.translate(_PALLET_DISPLAY_TRANSLATION)
    if ascii_prefix and ascii_prefix.isascii():
        return f"{ascii_prefix}{sep}{tail}"
    return text


def _task_pallet_display(payload: dict, fallback: str | None = None) -> str:
    if _pallet_choice_pending(payload):
        return display_scan_text(fallback) if fallback else "РџРѕРґС…РѕРґСЏС‰Р°СЏ РїР°Р»Р»РµС‚Р°"
    return display_pallet_scan_text(payload.get("pallet_code") or fallback)


def _pallet_choice_pending(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    if not bool(payload.get("flexible_pallet_choice")):
        return False
    pallet_code = str(payload.get("pallet_code") or "").strip()
    return not pallet_code


def _candidate_pallet_options(payload: dict) -> list[dict]:
    raw_options = payload.get("candidate_pallets") if isinstance(payload, dict) else []
    if not isinstance(raw_options, list):
        return []
    result: list[dict] = []
    seen: set[str] = set()
    for raw in raw_options:
        if not isinstance(raw, dict):
            continue
        pallet_code = str(raw.get("pallet_code") or "").strip()
        key = pallet_code.lower()
        if not pallet_code or key in seen:
            continue
        seen.add(key)
        from_location = raw.get("from_location") if isinstance(raw.get("from_location"), dict) else {}
        normalized_location = _build_location(
            _normalize_zone_code((from_location or {}).get("zone") or ""),
            _as_int((from_location or {}).get("row")),
            _as_int((from_location or {}).get("section")),
            _as_int((from_location or {}).get("tier")),
            _as_int((from_location or {}).get("cell")),
        )
        result.append(
            {
                "pallet_code": pallet_code,
                "from_location": normalized_location,
                "from_label": str(raw.get("from_label") or _location_label(normalized_location) or "").strip(),
                "receiving_order_id": str(raw.get("receiving_order_id") or "").strip(),
                "available_qty": _as_int(raw.get("available_qty")),
            }
        )
    return result


def _candidate_pallet_for_scan(payload: dict, scan_code: str) -> dict:
    for option in _candidate_pallet_options(payload):
        if _same_pallet_code_scan(scan_code, option.get("pallet_code")):
            return option
    return {}


def _candidate_location_groups(payload: dict) -> list[dict]:
    groups: dict[str, dict] = {}
    for option in _candidate_pallet_options(payload):
        from_location = option.get("from_location") or {}
        source_code = _location_scan_code(from_location)
        source_label = str(option.get("from_label") or _location_label(from_location) or source_code).strip()
        key = source_code or source_label or "-"
        group = groups.setdefault(
            key,
            {
                "source_code": source_code,
                "source_label": source_label,
                "pallets": [],
                "pallet_codes": [],
                "qty": 0,
            },
        )
        group["pallet_codes"].append(str(option.get("pallet_code") or "").strip())
        group["pallets"].append(display_pallet_scan_text(option.get("pallet_code")))
        group["qty"] += _as_int(option.get("available_qty"))
    result = []
    for group in groups.values():
        pallets = [code for code in group["pallets"] if code]
        result.append(
            {
                **group,
                "pallet_code": next((code for code in group.get("pallet_codes") or [] if code), ""),
                "pallet_count": len(pallets),
                "pallets_label": ", ".join(pallets[:6]) + ("..." if len(pallets) > 6 else ""),
            }
        )
    return result


def _task_source_code(payload: dict) -> str:
    explicit = str(payload.get("source_code") or payload.get("from_code") or "").strip()
    if explicit:
        return explicit
    return _location_scan_code(payload.get("from_location") or {})


def _task_source_prompt_label(payload: dict) -> str:
    source_code = _task_source_code(payload)
    if source_code:
        return source_code
    return str(payload.get("from_label") or "").strip() or "РјРµСЃС‚Сѓ РѕС‚Р±РѕСЂР°"


def _task_destination_code(payload: dict) -> str:
    explicit = str(payload.get("destination_code") or payload.get("to_code") or "").strip()
    if explicit:
        return explicit
    return _location_scan_code(payload.get("to_location") or {})


def _request_remaining_pallet_labels(tasks: list[MoveTask]) -> list[str]:
    labels: list[str] = []
    for task in tasks:
        payload = dict(task.payload or {})
        if _task_payload_status(task, payload) == MoveTask.STATUS_DONE:
            continue
        labels.append(_task_pallet_display(payload))
    return labels


def _request_duplicate_destination_result(tasks: list[MoveTask], scan_code: str) -> MoveTaskCommandResult | None:
    remaining_count = sum(
        1
        for task in tasks
        if _task_payload_status(task, dict(task.payload or {})) != MoveTask.STATUS_DONE
    )
    for task in tasks:
        payload = dict(task.payload or {})
        status = _task_payload_status(task, payload)
        execution = dict(payload.get("mobile_execution") or {})
        if status != MoveTask.STATUS_DONE and not execution.get("destination_confirmed"):
            continue
        if not _same_location_scan(scan_code, payload.get("to_location") or {}):
            continue
        destination_code = _task_destination_code(payload)
        pallet_code = _task_pallet_display(payload, fallback=scan_code)
        if remaining_count <= 0:
            return MoveTaskCommandResult(
                ok=True,
                message=(
                    f"РњРµСЃС‚Рѕ {destination_code} СѓР¶Рµ РїРѕРґС‚РІРµСЂР¶РґРµРЅРѕ. "
                    f"РџР°Р»Р»РµС‚Р° {pallet_code} СѓР¶Рµ СЂР°Р·РјРµС‰РµРЅР°."
                ),
                completed=True,
            )
        return MoveTaskCommandResult(
            ok=True,
            message=(
                f"РњРµСЃС‚Рѕ {destination_code} СѓР¶Рµ РїРѕРґС‚РІРµСЂР¶РґРµРЅРѕ. "
                f"РџР°Р»Р»РµС‚Р° {pallet_code} СѓР¶Рµ СЂР°Р·РјРµС‰РµРЅР°, СЃРєР°РЅРёСЂСѓР№С‚Рµ СЃР»РµРґСѓСЋС‰СѓСЋ РїР°Р»Р»РµС‚Сѓ."
            ),
            completed=False,
        )
    return None


def _same_scan_value(left: str | None, right: str | None) -> bool:
    return bool(_scan_compare_variants(left) & _scan_compare_variants(right))


def _scan_tail_variants(value: str | None) -> set[str]:
    tails: set[str] = set()
    for variant in _scan_compare_variants(value):
        parts = [part for part in variant.split("-") if part]
        if len(parts) >= 2:
            tails.add("-".join(parts[1:]))
    return tails


def _has_garbled_scan_prefix(value: str | None) -> bool:
    raw_value = str(value or "").strip()
    if "-" not in raw_value:
        return False
    prefix = raw_value.split("-", 1)[0].strip()
    if not prefix:
        return False
    if "\ufffd" in prefix:
        return True
    return any(
        ("\u3400" <= ch <= "\u4DBF")
        or ("\u4E00" <= ch <= "\u9FFF")
        or ("\uF900" <= ch <= "\uFAFF")
        for ch in prefix
    )


def _same_box_code_scan(left: str | None, right: str | None) -> bool:
    if _same_scan_value(left, right):
        return True
    if not (_has_garbled_scan_prefix(left) or _has_garbled_scan_prefix(right)):
        return False
    return bool(_scan_tail_variants(left) & _scan_tail_variants(right))


def _pallet_tail_variants(value: str | None) -> set[str]:
    return _scan_tail_variants(value)


def _pallet_numeric_variants(value: str | None) -> set[str]:
    variants: set[str] = set()
    for candidate in _scan_compare_variants(value):
        digits = "".join(re.findall(r"\d+", candidate))
        if len(digits) >= 8:
            variants.add(digits)
    return variants


def _same_pallet_code_scan(left: str | None, right: str | None) -> bool:
    if _same_scan_value(left, right):
        return True
    if _pallet_tail_variants(left) & _pallet_tail_variants(right):
        return True
    return bool(_pallet_numeric_variants(left) & _pallet_numeric_variants(right))


def _operation_container_codes_for_bound_payload(payload: dict, pallet_code: str) -> list[str]:
    move_mode = _normalize_move_mode(payload.get("move_mode"), payload.get("pick_mode"))
    if move_mode == MoveTask.MODE_PALLET_FULL:
        return [pallet_code]
    requested_boxes = _payload_box_codes(payload)
    if move_mode == MOVE_MODE_BOX_FULL and requested_boxes:
        return requested_boxes
    return [pallet_code]


def _ensure_processing_operation_for_bound_task(
    *,
    task: MoveTask,
    payload: dict,
    user,
) -> tuple[dict, str]:
    processing_order_id = str(payload.get("processing_order_id") or "").strip()
    to_zone = _normalize_zone_code((payload.get("to_location") or {}).get("zone") or "")
    if not processing_order_id or to_zone != "OBR" or payload.get("warehouse_operation_id"):
        return payload, ""
    move_mode = _normalize_move_mode(payload.get("move_mode"), payload.get("pick_mode"))
    if move_mode != MoveTask.MODE_PALLET_FULL:
        return payload, ""
    if task.status == MoveTask.STATUS_DONE or str(payload.get("status") or "").strip() == MoveTask.STATUS_DONE:
        return payload, ""
    pallet_code = str(payload.get("pallet_code") or task.pallet_code or "").strip()
    if not pallet_code:
        return payload, "РќРµ РІС‹Р±СЂР°РЅР° РїР°Р»Р»РµС‚Р° РґР»СЏ СЃРєР»Р°РґСЃРєРѕР№ РѕРїРµСЂР°С†РёРё."
    try:
        warehouse_operation = WarehouseWritePathService.request_move_to_processing(
            agency=task.request.agency,
            order_id=processing_order_id,
            container_codes=_operation_container_codes_for_bound_payload(payload, pallet_code),
            requested_by=user if getattr(user, "is_authenticated", False) else None,
            requested_by_role=str(payload.get("requested_by_role") or "super_car"),
            source_document_type="stock_move",
            source_document_id=str(task.legacy_order_id or "").strip(),
            destination_location_code=str(
                (payload.get("to_location") or {}).get("code") or ""
            ).strip(),
            allow_legacy_generic_destination=not bool(
                payload.get("concrete_location_required")
            ),
        )
    except ValueError as exc:
        if str(exc) == "No processing-reserved snapshots ready for move to OBR":
            return payload, ""
        return payload, str(exc)
    if warehouse_operation is None:
        return payload, ""
    warehouse_task = warehouse_operation.tasks.order_by("id").first()
    if warehouse_task:
        warehouse_task.payload = {
            **dict(warehouse_task.payload or {}),
            "legacy_move_id": str(task.legacy_order_id or "").strip(),
            "pallet_code": pallet_code,
        }
        warehouse_task.save(update_fields=["payload", "updated_at"])
    payload["warehouse_operation_id"] = warehouse_operation.id
    payload["warehouse_operation_task_id"] = warehouse_task.id if warehouse_task else ""
    return payload, ""


def _bind_flexible_pallet_choice(
    *,
    task: MoveTask,
    payload: dict,
    option: dict,
    scan_code: str,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    pallet_code = str(option.get("pallet_code") or "").strip()
    if not pallet_code:
        return MoveTaskCommandResult(ok=False, error="РџР°Р»Р»РµС‚Р° РЅРµ РЅР°Р№РґРµРЅР° РІ СЃРїРёСЃРєРµ РїРѕРґС…РѕРґСЏС‰РёС….")
    from_location = option.get("from_location") or {}
    from_location = _build_location(
        _normalize_zone_code((from_location or {}).get("zone") or ""),
        _as_int((from_location or {}).get("row")),
        _as_int((from_location or {}).get("section")),
        _as_int((from_location or {}).get("tier")),
        _as_int((from_location or {}).get("cell")),
    )
    placement_entry = _find_placement_entry_for_pallet(
        pallet_code,
        receiving_order_id=str(option.get("receiving_order_id") or payload.get("receiving_order_id") or "").strip() or None,
        processing_order_id=str(payload.get("processing_order_id") or "").strip() or None,
        agency_id=int(task.request.agency_id) if task.request and task.request.agency_id else None,
    )
    if not placement_entry:
        return MoveTaskCommandResult(ok=False, error=f"РџР°Р»Р»РµС‚Р° {display_scan_text(pallet_code)} РЅРµ РЅР°Р№РґРµРЅР° РІ СЃРєР»Р°РґСЃРєРѕРј РѕСЃС‚Р°С‚РєРµ.")

    placement_payload_for_selection = copy.deepcopy(dict(placement_entry.payload or {}))
    _adapt_flexible_payload_to_selected_pallet(
        payload,
        placement_payload_for_selection,
        pallet_code,
    )

    payload["pallet_code"] = pallet_code
    payload["selected_pallet_code"] = pallet_code
    payload["pallet_choice_pending"] = False
    payload["from_location"] = from_location
    payload["from_label"] = str(option.get("from_label") or _location_label(from_location) or "").strip()
    payload["from_code"] = _location_scan_code(from_location)
    payload["source_code"] = payload["from_code"]
    payload["receiving_order_id"] = str(option.get("receiving_order_id") or payload.get("receiving_order_id") or "").strip()
    payload["selected_candidate_pallet"] = option

    move_mode, selected_box_codes = _resolve_stock_move_mode(
        payload,
        pallet_code,
        agency_id=int(task.request.agency_id) if task.request and task.request.agency_id else None,
        pallet_available_qty=_as_int(option.get("available_qty")),
    )
    payload["move_mode"] = move_mode
    payload["pick_mode"] = "full" if move_mode == MoveTask.MODE_PALLET_FULL else "partial"
    if move_mode == MoveTask.MODE_PALLET_FULL:
        payload["requested_qty"] = ""
        payload["requested_boxes"] = []
        payload["requested_box"] = ""
        payload["requested_rows"] = []
        payload["requested_barcode_qty"] = {}
        payload["available_qty"] = ""
        payload.pop("requested_box_selection", None)
        payload.pop("requested_box_count", None)
    elif selected_box_codes:
        payload["requested_boxes"] = selected_box_codes
        payload["requested_box"] = selected_box_codes[0] if len(selected_box_codes) == 1 else ""
        payload["planned_box_codes"] = list(selected_box_codes)
        payload["selected_box_codes"] = list(selected_box_codes)
    payload["instruction"] = _move_instruction(payload)

    payload, placement_payload = _cache_mobile_placement_snapshot(task, payload, placement_entry)
    payload = _mobile_sync_execution_payload(task, payload, placement_payload)
    execution = dict(payload.get("mobile_execution") or {})
    execution["source_confirmed"] = True
    execution["pallet_confirmed"] = True
    execution["destination_confirmed"] = False
    execution["destination_override_pending"] = False
    execution["last_scan"] = scan_code
    payload["mobile_execution"] = execution

    task.pallet_code = pallet_code
    task.from_zone = str(from_location.get("zone") or "")
    task.from_row = _as_int(from_location.get("row")) or None
    task.from_section = _as_int(from_location.get("section")) or None
    task.from_tier = _as_int(from_location.get("tier")) or None
    task.from_cell = _as_int(from_location.get("cell")) or None
    task.move_mode = move_mode
    task.qty_planned = _as_int(payload.get("requested_qty")) or task.qty_planned

    try:
        lock_pallet_for_task(
            task,
            locked_by=user,
            payload={
                "legacy_order_id": task.legacy_order_id,
                "shipping_order_id": str(payload.get("shipping_order_id") or "").strip(),
                "locked_on": "bind_flexible_pallet_choice",
            },
        )
    except ValueError as exc:
        return MoveTaskCommandResult(ok=False, error=str(exc))

    payload, operation_error = _ensure_processing_operation_for_bound_task(
        task=task,
        payload=payload,
        user=user,
    )
    if operation_error:
        release_claims_for_task(task, delivered=False)
        return MoveTaskCommandResult(ok=False, error=operation_error)

    task.payload = payload
    task.assigned_to_name = employee_name or task.assigned_to_name
    task.save(
        update_fields=[
            "pallet_code",
            "from_zone",
            "from_row",
            "from_section",
            "from_tier",
            "from_cell",
            "move_mode",
            "qty_planned",
            "payload",
            "assigned_to_name",
            "updated_at",
        ]
    )

    agency = getattr(task.request, "agency", None)
    authenticated_user = user if getattr(user, "is_authenticated", False) else None
    log_stock_move(
        "update",
        user=authenticated_user,
        agency=agency,
        description=f"Р”Р»СЏ Р·Р°СЏРІРєРё РІС‹Р±СЂР°РЅР° РїР°Р»Р»РµС‚Р° {pallet_code}",
        snapshot={
            "move_id": task.legacy_order_id,
            "pallet_code": pallet_code,
            "from_location": payload.get("from_location"),
            "to_location": payload.get("to_location"),
            "request_batch_mode": bool(payload.get("mobile_request_batch_mode")),
        },
    )
    destination_code = _location_scan_code(payload.get("to_location") or {})
    return MoveTaskCommandResult(
        ok=True,
        task=task,
        payload=payload,
        message=f"РџР°Р»Р»РµС‚Р° {display_scan_text(pallet_code)} РїРѕРґС‚РІРµСЂР¶РґРµРЅР°. РћС‚РІРµР·Рё -> {destination_code}.",
        completed=False,
    )


def _location_scan_variants(location: dict | None) -> set[str]:
    display_code = _location_scan_code(location)
    legacy_code = _legacy_location_scan_code(location)
    return {
        code
        for code in (display_code, legacy_code)
        if str(code or "").strip()
    }


def _same_location_scan(scan_value: str | None, location: dict | None) -> bool:
    return any(_same_scan_value(scan_value, candidate) for candidate in _location_scan_variants(location))


def _mobile_selector_sets(payload: dict) -> tuple[set[str], set[str], set[str], int]:
    requested_qty = _as_int(payload.get("requested_qty"))
    requested_barcode_qty = _requested_barcode_qty(payload)
    if requested_qty <= 0 and requested_barcode_qty:
        requested_qty = _barcode_qty_total(requested_barcode_qty)
    requested_sku = str(payload.get("requested_sku") or "").strip()
    requested_barcodes_raw = payload.get("requested_barcodes")
    if isinstance(requested_barcodes_raw, list):
        requested_barcodes = requested_barcodes_raw
    else:
        requested_barcodes = _parse_json_list(requested_barcodes_raw)
    barcode_values = {
        str(value).strip()
        for value in requested_barcodes
        if str(value or "").strip()
    }
    if requested_barcode_qty:
        barcode_values.update(requested_barcode_qty.keys())
    sku_values = {requested_sku} if requested_sku else set()
    if barcode_values:
        barcode_queryset = SKUBarcode.objects.filter(value__in=barcode_values)
        agency_id = _as_int(payload.get("agency_id"))
        if agency_id:
            barcode_queryset = barcode_queryset.filter(agency_id=agency_id)
        sku_values.update(
            barcode_queryset.values_list("sku__sku_code", flat=True)
        )
        sku_values.discard(None)
    requested_goods_type = _normalize_goods_type(payload.get("requested_goods_type"))
    goods_type_values = {requested_goods_type} if requested_goods_type else set()
    return barcode_values, sku_values, goods_type_values, requested_qty


def _is_any_matching_box_selection(payload: dict) -> bool:
    move_mode = _normalize_move_mode(payload.get("move_mode"), payload.get("pick_mode"))
    selection_mode = _requested_box_selection_mode(payload)
    return (
        move_mode in {MOVE_MODE_BOX_FULL, MOVE_MODE_BOX_PARTIAL}
        and _requested_box_count(payload) > 0
        and (
            selection_mode == BOX_SELECTION_ANY_MATCHING
            or (
                selection_mode == BOX_SELECTION_PATTERN_MATCHING
                and bool(_requested_box_patterns(payload))
            )
        )
    )


def _normalize_partial_pick_patterns(raw) -> list[dict]:
    source = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            import json

            source = json.loads(text)
        except (ValueError, TypeError):
            return []
    if not isinstance(source, list):
        return []
    result: list[dict] = []
    for item in source:
        if not isinstance(item, dict):
            continue
        source_qty = _as_int(item.get("source_box_qty") or item.get("box_qty"))
        pick_qty = _as_int(item.get("pick_qty") or item.get("qty") or item.get("requested_qty"))
        barcode_qty = _normalize_barcode_qty_map(item.get("barcode_qty") or item.get("pick_barcode_qty"))
        if pick_qty <= 0 and barcode_qty:
            pick_qty = _barcode_qty_total(barcode_qty)
        source_barcode_qty = _normalize_barcode_qty_map(item.get("source_barcode_qty"))
        count = _as_int(item.get("requested_box_count") or item.get("count") or item.get("boxes"))
        if count <= 0:
            count = 1
        if source_qty <= 0 or pick_qty <= 0:
            continue
        result.append(
            {
                "source_box_qty": source_qty,
                "requested_box_count": count,
                "source_barcode_qty": source_barcode_qty,
                "pick_qty": pick_qty,
                "barcode_qty": barcode_qty,
                "requested_article": str(item.get("requested_article") or "").strip(),
                "requested_goods_type": _normalize_goods_type(item.get("requested_goods_type")),
                "requested_barcodes": [
                    str(value).strip()
                    for value in (item.get("requested_barcodes") or [])
                    if str(value or "").strip()
                ],
            }
        )
    return result


def _partial_pick_patterns(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    if payload.get("partial_pick_patterns") is not None:
        return _normalize_partial_pick_patterns(payload.get("partial_pick_patterns"))
    if payload.get("partial_pick_patterns_json") is not None:
        return _normalize_partial_pick_patterns(payload.get("partial_pick_patterns_json"))
    return []


def _expand_partial_pick_patterns(payload: dict) -> list[dict]:
    expanded: list[dict] = []
    for pattern in _partial_pick_patterns(payload):
        count = _as_int(pattern.get("requested_box_count"))
        if count <= 0:
            continue
        for _index in range(count):
            expanded.append(dict(pattern))
    return expanded


def _partial_pick_pattern_matches_box(
    placement_payload: dict,
    pallet_code: str,
    box_code: str,
    pattern: dict,
) -> bool:
    source_pattern = {
        "box_qty": _as_int(pattern.get("source_box_qty")),
        "barcode_qty": _normalize_barcode_qty_map(pattern.get("source_barcode_qty")),
        "requested_article": str(pattern.get("requested_article") or "").strip(),
        "requested_goods_type": _normalize_goods_type(pattern.get("requested_goods_type")),
        "requested_barcodes": list(pattern.get("requested_barcodes") or []),
    }
    return _placement_box_matches_pattern(placement_payload, pallet_code, box_code, source_pattern)


def _partial_pick_rows_for_boxes(
    payload: dict,
    placement_payload: dict,
    pallet_code: str,
    box_codes: list[str],
) -> list[dict]:
    patterns = _expand_partial_pick_patterns(payload)
    if not patterns:
        return []
    rows: list[dict] = []
    used_patterns: set[int] = set()
    for raw_code in box_codes or []:
        box_code = _normalize_box_code(raw_code)
        if not box_code:
            continue
        chosen_idx = None
        for idx, pattern in enumerate(patterns):
            if idx in used_patterns:
                continue
            if _partial_pick_pattern_matches_box(placement_payload, pallet_code, box_code, pattern):
                chosen_idx = idx
                break
        if chosen_idx is None:
            continue
        used_patterns.add(chosen_idx)
        pattern = patterns[chosen_idx]
        rows.append(
            {
                "box_code": box_code,
                "qty": _as_int(pattern.get("pick_qty")),
                "barcode_qty": _normalize_barcode_qty_map(pattern.get("barcode_qty")),
            }
        )
    return rows


def _matching_box_row_map(rows: list[dict]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in rows or []:
        box_code = _normalize_box_code(row.get("code"))
        if box_code:
            result[box_code.lower()] = row
    return result


def _matching_selected_box_codes(raw_codes, matching_rows: dict[str, dict]) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for raw_code in raw_codes or []:
        box_code = _normalize_box_code(raw_code)
        box_key = box_code.lower()
        if not box_code or box_key in seen or box_key not in matching_rows:
            continue
        seen.add(box_key)
        selected.append(box_code)
    return selected


def _split_mixed_partial_box_selection(
    payload: dict,
    placement_payload: dict,
    pallet_code: str,
    selected_boxes: list[str],
) -> tuple[list[str], list[dict]]:
    partial_target = len(_expand_partial_pick_patterns(payload))
    selected = [
        _normalize_box_code(code)
        for code in selected_boxes or []
        if _normalize_box_code(code)
    ]
    if partial_target <= 0 or not selected:
        return selected, []
    preferred_partial = selected[-partial_target:]
    partial_rows = _partial_pick_rows_for_boxes(payload, placement_payload, pallet_code, preferred_partial)
    if len(partial_rows) < partial_target:
        partial_rows = _partial_pick_rows_for_boxes(payload, placement_payload, pallet_code, selected)
    partial_keys = {
        _normalize_box_code(row.get("box_code")).lower()
        for row in partial_rows
        if _normalize_box_code(row.get("box_code"))
    }
    whole_boxes = [code for code in selected if code.lower() not in partial_keys]
    return whole_boxes, partial_rows


def _expand_requested_box_patterns(payload: dict) -> list[dict]:
    expanded: list[dict] = []
    for pattern in _requested_box_patterns(payload):
        box_count = _as_int(pattern.get("requested_box_count"))
        if box_count <= 0:
            continue
        normalized = {
            "box_qty": _as_int(pattern.get("box_qty")),
            "barcode_qty": _normalize_barcode_qty_map(pattern.get("barcode_qty")),
            "requested_article": str(pattern.get("requested_article") or "").strip(),
            "requested_goods_type": _normalize_goods_type(pattern.get("requested_goods_type")),
            "requested_barcodes": [
                str(value).strip()
                for value in (pattern.get("requested_barcodes") or [])
                if str(value or "").strip()
            ],
        }
        for _index in range(box_count):
            expanded.append(dict(normalized))
    return expanded


def _placement_box_signature(
    placement_payload: dict,
    pallet_code: str,
    box_code: str,
) -> tuple[int, dict[str, int]]:
    pallets, _pallet_idx, pallet = _find_pallet_in_placement(placement_payload, pallet_code)
    if not pallets or not pallet:
        return 0, {}
    normalized_box = _normalize_box_code(box_code)
    if normalized_box.lower() not in {
        _normalize_box_code(code).lower()
        for code in (pallet.get("boxes") or [])
        if _normalize_box_code(code)
    }:
        return 0, {}
    _boxes, _box_idx, box = _find_box_in_placement(placement_payload, normalized_box)
    if not box:
        return 0, {}
    total = 0
    barcode_qty: dict[str, int] = {}
    for item in box.get("items") or []:
        barcode = _resolved_item_barcode(item) or str(item.get("sku") or item.get("sku_code") or "").strip()
        qty = _as_int(item.get("qty"))
        if not barcode or qty <= 0:
            continue
        total += qty
        barcode_qty[barcode] = barcode_qty.get(barcode, 0) + qty
    return total, barcode_qty


def _placement_box_matches_pattern(
    placement_payload: dict,
    pallet_code: str,
    box_code: str,
    pattern: dict,
) -> bool:
    total, barcode_qty = _placement_box_signature(placement_payload, pallet_code, box_code)
    pattern_qty = _as_int(pattern.get("box_qty"))
    if total <= 0 or pattern_qty <= 0 or total != pattern_qty:
        return False
    pattern_barcode_qty = _normalize_barcode_qty_map(pattern.get("barcode_qty"))
    if pattern_barcode_qty:
        return barcode_qty == pattern_barcode_qty
    requested_barcodes = {
        str(value).strip()
        for value in (pattern.get("requested_barcodes") or [])
        if str(value or "").strip()
    }
    if requested_barcodes:
        return {barcode for barcode in barcode_qty.keys() if barcode} == requested_barcodes
    return True


def _matching_boxes_for_pattern_selection(
    placement_payload: dict,
    pallet_code: str,
    payload: dict,
) -> list[dict]:
    patterns = _expand_requested_box_patterns(payload)
    if not patterns:
        return []
    pallets, _pallet_idx, pallet = _find_pallet_in_placement(placement_payload, pallet_code)
    if not pallets or not pallet:
        return []
    result: list[dict] = []
    seen: set[str] = set()
    for raw_code in pallet.get("boxes") or []:
        box_code = _normalize_box_code(raw_code)
        box_key = box_code.lower()
        if not box_code or box_key in seen:
            continue
        if not any(
            _placement_box_matches_pattern(placement_payload, pallet_code, box_code, pattern)
            for pattern in patterns
        ):
            continue
        total, barcode_qty = _placement_box_signature(placement_payload, pallet_code, box_code)
        if total <= 0:
            continue
        seen.add(box_key)
        result.append({"code": box_code, "qty": total, "barcode_qty": barcode_qty})
    return result


def _matching_box_rows_for_flexible_selection(
    payload: dict,
    placement_payload: dict,
    pallet_code: str,
) -> list[dict]:
    selection_mode = _requested_box_selection_mode(payload)
    if selection_mode == BOX_SELECTION_PATTERN_MATCHING:
        return _matching_boxes_for_pattern_selection(placement_payload, pallet_code, payload)
    barcode_values, sku_values, goods_type_values, _requested_qty = _mobile_selector_sets(payload)
    return _matching_boxes_for_pallet(
        placement_payload,
        pallet_code,
        barcode_values,
        sku_values,
        goods_type_values,
    )


def _assign_partial_requested_patterns_to_boxes(
    payload: dict,
    placement_payload: dict,
    pallet_code: str,
    box_codes: list[str],
) -> tuple[list[str], list[dict]]:
    raw_patterns = _requested_box_patterns(payload)
    expanded_patterns: list[tuple[int, dict]] = []
    for raw_idx, pattern in enumerate(raw_patterns):
        box_count = _as_int(pattern.get("requested_box_count"))
        if box_count <= 0:
            continue
        normalized = {
            "box_qty": _as_int(pattern.get("box_qty")),
            "barcode_qty": _normalize_barcode_qty_map(pattern.get("barcode_qty")),
            "requested_article": str(pattern.get("requested_article") or "").strip(),
            "requested_goods_type": _normalize_goods_type(pattern.get("requested_goods_type")),
            "requested_barcodes": [
                str(value).strip()
                for value in (pattern.get("requested_barcodes") or [])
                if str(value or "").strip()
            ],
        }
        for _index in range(box_count):
            expanded_patterns.append((raw_idx, dict(normalized)))
    ordered_codes: list[str] = []
    seen_codes: set[str] = set()
    for raw_code in box_codes or []:
        box_code = _normalize_box_code(raw_code)
        box_key = box_code.lower()
        if not box_code or box_key in seen_codes:
            continue
        seen_codes.add(box_key)
        ordered_codes.append(box_code)
    if not expanded_patterns or not ordered_codes or len(ordered_codes) > len(expanded_patterns):
        return [], []

    per_box: dict[str, list[int]] = {}
    for box_code in ordered_codes:
        matches = [
            pattern_idx
            for pattern_idx, (_raw_idx, pattern) in enumerate(expanded_patterns)
            if _placement_box_matches_pattern(placement_payload, pallet_code, box_code, pattern)
        ]
        if not matches:
            return [], []
        per_box[box_code] = matches
    ordered_boxes = sorted(
        ordered_codes,
        key=lambda code: (
            len(per_box.get(code) or []),
            -_as_int(_placement_box_signature(placement_payload, pallet_code, code)[0]),
            code,
        ),
    )
    used_patterns: set[int] = set()
    assignment: dict[str, int] = {}

    def backtrack(position: int) -> bool:
        if position >= len(ordered_boxes):
            return True
        box_code = ordered_boxes[position]
        for pattern_idx in per_box.get(box_code) or []:
            if pattern_idx in used_patterns:
                continue
            used_patterns.add(pattern_idx)
            assignment[box_code] = pattern_idx
            if backtrack(position + 1):
                return True
            assignment.pop(box_code, None)
            used_patterns.remove(pattern_idx)
        return False

    if not backtrack(0):
        return [], []
    counts_by_raw_idx: dict[int, int] = {}
    for box_code in ordered_codes:
        raw_idx = expanded_patterns[assignment[box_code]][0]
        counts_by_raw_idx[raw_idx] = counts_by_raw_idx.get(raw_idx, 0) + 1
    adjusted_patterns: list[dict] = []
    for raw_idx, pattern in enumerate(raw_patterns):
        count = counts_by_raw_idx.get(raw_idx, 0)
        if count <= 0:
            continue
        adjusted = copy.deepcopy(pattern)
        adjusted["requested_box_count"] = count
        adjusted_patterns.append(adjusted)
    return ordered_codes, adjusted_patterns


def _adapt_flexible_payload_to_selected_pallet(
    payload: dict,
    placement_payload: dict,
    pallet_code: str,
) -> dict:
    if not _is_any_matching_box_selection(payload):
        return {}
    requested_box_count = _requested_box_count(payload)
    if requested_box_count <= 0:
        return {}
    matching_rows = _matching_box_rows_for_flexible_selection(payload, placement_payload, pallet_code)
    matching_codes: list[str] = []
    qty_by_code: dict[str, int] = {}
    for row in matching_rows:
        box_code = _normalize_box_code(row.get("code"))
        box_key = box_code.lower()
        if not box_code or box_key in qty_by_code:
            continue
        matching_codes.append(box_code)
        qty_by_code[box_key] = _as_int(row.get("qty"))
    if len(matching_codes) >= requested_box_count:
        return {}
    if _requested_box_selection_mode(payload) == BOX_SELECTION_PATTERN_MATCHING:
        selected_codes, adjusted_patterns = _assign_partial_requested_patterns_to_boxes(
            payload,
            placement_payload,
            pallet_code,
            matching_codes,
        )
        if not selected_codes or not adjusted_patterns:
            return {}
        payload["requested_box_patterns"] = adjusted_patterns
    else:
        selected_codes = list(matching_codes)
    actual_box_count = len(selected_codes)
    if actual_box_count <= 0 or actual_box_count >= requested_box_count:
        return {}

    actual_qty = sum(_as_int(qty_by_code.get(code.lower())) for code in selected_codes)
    if actual_qty <= 0:
        actual_qty = actual_box_count
    reserved_box_codes: list[str] = []
    seen_reserved: set[str] = set()
    for raw_code in payload.get("reserved_box_codes") or []:
        code = str(raw_code or "").strip()
        key = code.lower()
        if not code or key in seen_reserved:
            continue
        seen_reserved.add(key)
        reserved_box_codes.append(code)
    current_reserved_codes = reserved_box_codes[:actual_box_count]
    remaining_reserved_codes = reserved_box_codes[actual_box_count:]
    original_requested_qty = _as_int(payload.get("requested_qty"))

    payload["requested_box_count"] = actual_box_count
    payload["requested_qty"] = actual_qty
    payload["available_qty"] = actual_qty
    if current_reserved_codes:
        payload["reserved_box_codes"] = current_reserved_codes
        payload["planned_box_codes"] = list(current_reserved_codes)
    payload["flexible_pallet_adjustment"] = {
        "pallet_code": pallet_code,
        "original_requested_box_count": requested_box_count,
        "selected_requested_box_count": actual_box_count,
        "remaining_box_count": requested_box_count - actual_box_count,
        "original_requested_qty": original_requested_qty,
        "selected_requested_qty": actual_qty,
        "remaining_requested_qty": max(original_requested_qty - actual_qty, 0),
        "selected_actual_box_codes": list(selected_codes),
        "remaining_reserved_box_codes": remaining_reserved_codes,
    }
    return dict(payload["flexible_pallet_adjustment"])


def _assign_requested_patterns_to_boxes(
    payload: dict,
    placement_payload: dict,
    pallet_code: str,
    box_codes: list[str],
    *,
    allow_partial: bool = False,
) -> list[str]:
    patterns = _expand_requested_box_patterns(payload)
    if not patterns:
        return []
    ordered_codes: list[str] = []
    seen_codes: set[str] = set()
    for raw_code in box_codes or []:
        box_code = _normalize_box_code(raw_code)
        box_key = box_code.lower()
        if not box_code or box_key in seen_codes:
            continue
        seen_codes.add(box_key)
        ordered_codes.append(box_code)
    if not allow_partial and len(ordered_codes) < len(patterns):
        return []
    if allow_partial and len(ordered_codes) > len(patterns):
        return []
    if allow_partial:
        per_box: dict[str, list[int]] = {}
        for box_code in ordered_codes:
            box_matches = [
                pattern_idx
                for pattern_idx, pattern in enumerate(patterns)
                if _placement_box_matches_pattern(placement_payload, pallet_code, box_code, pattern)
            ]
            if not box_matches:
                return []
            per_box[box_code] = box_matches
        ordered_boxes = sorted(
            ordered_codes,
            key=lambda code: (
                len(per_box.get(code) or []),
                -_as_int(_placement_box_signature(placement_payload, pallet_code, code)[0]),
                code,
            ),
        )
        used_patterns: set[int] = set()

        def backtrack_boxes(position: int) -> bool:
            if position >= len(ordered_boxes):
                return True
            box_code = ordered_boxes[position]
            for pattern_idx in per_box.get(box_code) or []:
                if pattern_idx in used_patterns:
                    continue
                used_patterns.add(pattern_idx)
                if backtrack_boxes(position + 1):
                    return True
                used_patterns.remove(pattern_idx)
            return False

        if backtrack_boxes(0):
            return ordered_codes
        return []
    per_pattern: dict[int, list[str]] = {}
    for pattern_idx, pattern in enumerate(patterns):
        matches = [
            box_code
            for box_code in ordered_codes
            if _placement_box_matches_pattern(placement_payload, pallet_code, box_code, pattern)
        ]
        if not matches:
            return []
        per_pattern[pattern_idx] = matches
    ordered_patterns = sorted(
        per_pattern.keys(),
        key=lambda idx: (len(per_pattern.get(idx) or []), -_as_int((patterns[idx] or {}).get("box_qty")), idx),
    )
    assignment: dict[int, str] = {}
    used_box_keys: set[str] = set()

    def backtrack(position: int) -> bool:
        if position >= len(ordered_patterns):
            return True
        pattern_idx = ordered_patterns[position]
        for box_code in per_pattern.get(pattern_idx) or []:
            box_key = box_code.lower()
            if box_key in used_box_keys:
                continue
            used_box_keys.add(box_key)
            assignment[pattern_idx] = box_code
            if backtrack(position + 1):
                return True
            assignment.pop(pattern_idx, None)
            used_box_keys.remove(box_key)
        return False

    if not backtrack(0):
        return []
    selected_keys = {code.lower() for code in assignment.values() if code}
    return [box_code for box_code in ordered_codes if box_code.lower() in selected_keys]


def _resolve_any_matching_boxes_for_completion(
    payload: dict,
    placement_payload: dict,
    pallet_code: str,
    *,
    require_scan_confirmation: bool,
    desktop_selected_boxes: list[str] | None = None,
) -> tuple[list[str], str]:
    requested_box_count = _requested_box_count(payload)
    if requested_box_count <= 0:
        return [], "Р’ Р·Р°РґР°РЅРёРё РЅРµ СѓРєР°Р·Р°РЅРѕ РєРѕР»РёС‡РµСЃС‚РІРѕ РїРѕРґС…РѕРґСЏС‰РёС… РєРѕСЂРѕР±РѕРІ."
    selection_mode = _requested_box_selection_mode(payload)
    if selection_mode == BOX_SELECTION_PATTERN_MATCHING:
        if require_scan_confirmation:
            execution = dict(payload.get("mobile_execution") or {})
            scanned_boxes = [
                _normalize_box_code(raw_code)
                for raw_code in execution.get("boxes_scanned") or []
                if _normalize_box_code(raw_code)
            ]
            resolved_boxes = _assign_requested_patterns_to_boxes(
                payload,
                placement_payload,
                pallet_code,
                scanned_boxes,
            )
            if len(resolved_boxes) < requested_box_count:
                return [], "РЎРЅР°С‡Р°Р»Р° РѕС‚СЃРєР°РЅРёСЂСѓР№С‚Рµ РІСЃРµ РЅСѓР¶РЅС‹Рµ С‚РёРїС‹ РєРѕСЂРѕР±РѕРІ."
            return resolved_boxes, ""
        selected_boxes = []
        if desktop_selected_boxes is not None:
            seen_selected: set[str] = set()
            for raw_code in desktop_selected_boxes:
                box_code = _normalize_box_code(raw_code)
                box_key = box_code.lower()
                if not box_code or box_key in seen_selected:
                    continue
                seen_selected.add(box_key)
                selected_boxes.append(box_code)
            if len(selected_boxes) != requested_box_count:
                return [], f"Р”Р»СЏ desktop-РѕС‚Р±РѕСЂР° РІС‹Р±РµСЂРёС‚Рµ СЂРѕРІРЅРѕ {requested_box_count} РєРѕСЂРѕР±РѕРІ."
            resolved_boxes = _assign_requested_patterns_to_boxes(
                payload,
                placement_payload,
                pallet_code,
                selected_boxes,
            )
            if len(resolved_boxes) != requested_box_count:
                return [], "Р’С‹Р±СЂР°РЅРЅС‹Рµ РєРѕСЂРѕР±Р° РЅРµ РїРѕРґС…РѕРґСЏС‚ РїРѕРґ РєРѕСЂРѕР±РѕС‡РЅСѓСЋ СЃС…РµРјСѓ Р·Р°РґР°РЅРёСЏ."
            return resolved_boxes, ""
        candidate_boxes = [
            str(row.get("code") or "").strip()
            for row in _matching_boxes_for_pattern_selection(placement_payload, pallet_code, payload)
            if str(row.get("code") or "").strip()
        ]
        resolved_boxes = _assign_requested_patterns_to_boxes(
            payload,
            placement_payload,
            pallet_code,
            candidate_boxes,
        )
        if len(resolved_boxes) < requested_box_count:
            return [], "РќР° РїР°Р»Р»РµС‚Рµ РЅРµ РЅР°Р№РґРµРЅ РїРѕР»РЅС‹Р№ РЅР°Р±РѕСЂ РїРѕРґС…РѕРґСЏС‰РёС… РєРѕСЂРѕР±РѕРІ РїРѕ РєРѕСЂРѕР±РѕС‡РЅРѕР№ СЃС…РµРјРµ."
        return resolved_boxes, ""
    if require_scan_confirmation:
        execution = dict(payload.get("mobile_execution") or {})
        scanned_boxes: list[str] = []
        seen_scanned: set[str] = set()
        for raw_code in execution.get("boxes_scanned") or []:
            box_code = _normalize_box_code(raw_code)
            box_key = box_code.lower()
            if not box_code or box_key in seen_scanned:
                continue
            seen_scanned.add(box_key)
            scanned_boxes.append(box_code)
        if len(scanned_boxes) < requested_box_count:
            return [], f"РЎРЅР°С‡Р°Р»Р° РѕС‚СЃРєР°РЅРёСЂСѓР№С‚Рµ {requested_box_count} РїРѕРґС…РѕРґСЏС‰РёС… РєРѕСЂРѕР±РѕРІ."
        return scanned_boxes[:requested_box_count], ""
    if desktop_selected_boxes is not None:
        selected_boxes: list[str] = []
        seen_selected: set[str] = set()
        for raw_code in desktop_selected_boxes:
            box_code = _normalize_box_code(raw_code)
            box_key = box_code.lower()
            if not box_code or box_key in seen_selected:
                continue
            seen_selected.add(box_key)
            selected_boxes.append(box_code)
        if len(selected_boxes) != requested_box_count:
            return [], f"Р”Р»СЏ desktop-РѕС‚Р±РѕСЂР° РІС‹Р±РµСЂРёС‚Рµ СЂРѕРІРЅРѕ {requested_box_count} РєРѕСЂРѕР±РѕРІ."
        barcode_values, sku_values, goods_type_values, _requested_qty = _mobile_selector_sets(payload)
        matching_codes = {
            _normalize_box_code(row.get("code")).lower()
            for row in _matching_boxes_for_pallet(
                placement_payload,
                pallet_code,
                barcode_values,
                sku_values,
                goods_type_values,
            )
            if _normalize_box_code(row.get("code"))
        }
        invalid_boxes = [code for code in selected_boxes if code.lower() not in matching_codes]
        if invalid_boxes:
            return [], f"Р’С‹Р±СЂР°РЅРЅС‹Рµ РєРѕСЂРѕР±Р° РЅРµ РїРѕРґС…РѕРґСЏС‚: {', '.join(invalid_boxes)}."
        return selected_boxes, ""

    barcode_values, sku_values, goods_type_values, _requested_qty = _mobile_selector_sets(payload)
    matched_boxes: list[str] = []
    seen_boxes: set[str] = set()
    for row in _matching_boxes_for_pallet(
        placement_payload,
        pallet_code,
        barcode_values,
        sku_values,
        goods_type_values,
    ):
        box_code = _normalize_box_code(row.get("code"))
        box_key = box_code.lower()
        if not box_code or box_key in seen_boxes:
            continue
        seen_boxes.add(box_key)
        matched_boxes.append(box_code)
        if len(matched_boxes) >= requested_box_count:
            break
    if len(matched_boxes) < requested_box_count:
        return [], (
            f"РќР° РїР°Р»Р»РµС‚Рµ РЅР°Р№РґРµРЅРѕ С‚РѕР»СЊРєРѕ {len(matched_boxes)} РїРѕРґС…РѕРґСЏС‰РёС… РєРѕСЂРѕР±РѕРІ, "
            f"РЅСѓР¶РЅРѕ {requested_box_count}."
        )
    return matched_boxes, ""


def _mobile_allocate_barcode_qty(
    source_qty: dict[str, int],
    qty_needed: int,
    remaining_by_barcode: dict[str, int] | None = None,
) -> dict[str, int]:
    available = {
        str(barcode).strip(): _as_int(qty)
        for barcode, qty in dict(source_qty or {}).items()
        if str(barcode or "").strip() and _as_int(qty) > 0
    }
    remaining = _as_int(qty_needed)
    result: dict[str, int] = {}
    if remaining_by_barcode:
        for barcode, needed in list(remaining_by_barcode.items()):
            take = min(_as_int(needed), _as_int(available.get(barcode)), remaining)
            if take <= 0:
                continue
            result[barcode] = result.get(barcode, 0) + take
            available[barcode] = _as_int(available.get(barcode)) - take
            remaining_by_barcode[barcode] = _as_int(remaining_by_barcode.get(barcode)) - take
            remaining -= take
            if remaining <= 0:
                break
    if remaining <= 0:
        return result
    for barcode, qty in available.items():
        take = min(_as_int(qty), remaining)
        if take <= 0:
            continue
        result[barcode] = result.get(barcode, 0) + take
        remaining -= take
        if remaining <= 0:
            break
    return result


def _mobile_build_row_barcode_qty(
    placement_payload: dict,
    pallet_code: str,
    box_code: str,
    payload: dict,
    qty_needed: int,
    explicit_barcode_qty: dict[str, int] | None = None,
) -> dict[str, int]:
    barcode_values, sku_values, goods_type_values, _requested_qty = _mobile_selector_sets(payload)
    _total, box_barcode_qty = _box_match_breakdown(
        placement_payload,
        pallet_code,
        box_code,
        barcode_values,
        sku_values,
        goods_type_values,
    )
    remaining_by_barcode = (
        {
            str(barcode).strip(): _as_int(qty)
            for barcode, qty in dict(explicit_barcode_qty or {}).items()
            if str(barcode or "").strip() and _as_int(qty) > 0
        }
        if explicit_barcode_qty
        else None
    )
    allocated = _mobile_allocate_barcode_qty(box_barcode_qty, qty_needed, remaining_by_barcode)
    if allocated:
        return allocated
    boxes, box_idx, box = _find_box_in_placement(placement_payload, box_code)
    if box_idx < 0 or not box:
        return {}
    remaining = _as_int(qty_needed)
    fallback: dict[str, int] = {}
    for item in box.get("items") or []:
        barcode = _resolved_item_barcode(item) or str(item.get("sku") or item.get("sku_code") or "").strip()
        qty = _as_int(item.get("qty"))
        if not barcode or qty <= 0:
            continue
        take = min(qty, remaining)
        if take <= 0:
            continue
        fallback[barcode] = fallback.get(barcode, 0) + take
        remaining -= take
        if remaining <= 0:
            break
    return fallback


def _mobile_plan_partial_rows(
    payload: dict,
    placement_payload: dict,
    pallet_code: str,
) -> list[dict]:
    requested_rows = _requested_partial_rows(payload)
    normalized_rows: list[dict] = []
    if requested_rows:
        for row in requested_rows:
            box_code = _normalize_box_code(row.get("box_code"))
            qty = _as_int(row.get("qty"))
            if not box_code or qty <= 0:
                continue
            barcode_qty = _mobile_build_row_barcode_qty(
                placement_payload,
                pallet_code,
                box_code,
                payload,
                qty,
                explicit_barcode_qty=_normalize_barcode_qty_map(row.get("barcode_qty")),
            )
            normalized_rows.append(
                {
                    "box_code": box_code,
                    "qty": qty,
                    "barcode_qty": barcode_qty,
                }
            )
        if normalized_rows:
            return normalized_rows

    if (
        _requested_box_selection_mode(payload) == BOX_SELECTION_PATTERN_MATCHING
        and _requested_box_count(payload) > 0
        and _partial_pick_patterns(payload)
    ):
        execution = dict(payload.get("mobile_execution") or {})
        scanned_boxes = [
            _normalize_box_code(raw_code)
            for raw_code in execution.get("boxes_scanned") or []
            if _normalize_box_code(raw_code)
        ]
        selected_boxes = _assign_requested_patterns_to_boxes(
            payload,
            placement_payload,
            pallet_code,
            scanned_boxes,
            allow_partial=True,
        ) if scanned_boxes else []
        if not selected_boxes:
            candidate_boxes = [
                str(row.get("code") or "").strip()
                for row in _matching_boxes_for_pattern_selection(placement_payload, pallet_code, payload)
                if str(row.get("code") or "").strip()
            ]
            selected_boxes = _assign_requested_patterns_to_boxes(
                payload,
                placement_payload,
                pallet_code,
                candidate_boxes,
            )[: _requested_box_count(payload)]
        selected_rows = _partial_pick_rows_for_boxes(payload, placement_payload, pallet_code, selected_boxes)
        if selected_rows:
            return selected_rows

    requested_box = _single_requested_box(payload)
    requested_codes = _payload_box_codes(payload)
    requested_barcode_qty = _requested_barcode_qty(payload)
    barcode_values, sku_values, goods_type_values, requested_qty = _mobile_selector_sets(payload)
    if requested_box:
        qty = requested_qty or _barcode_qty_total(requested_barcode_qty)
        if qty <= 0:
            qty = _as_int(
                _box_match_breakdown(
                    placement_payload,
                    pallet_code,
                    requested_box,
                    barcode_values,
                    sku_values,
                    goods_type_values,
                )[0]
            )
        if qty > 0:
            return [
                {
                    "box_code": requested_box,
                    "qty": qty,
                    "barcode_qty": _mobile_build_row_barcode_qty(
                        placement_payload,
                        pallet_code,
                        requested_box,
                        payload,
                        qty,
                        explicit_barcode_qty=requested_barcode_qty,
                    ),
                }
            ]
    if requested_codes:
        result: list[dict] = []
        for code in requested_codes:
            total, _barcode_qty = _box_match_breakdown(
                placement_payload,
                pallet_code,
                code,
                barcode_values,
                sku_values,
                goods_type_values,
            )
            qty = total or requested_qty
            if qty <= 0:
                continue
            result.append(
                {
                    "box_code": code,
                    "qty": qty,
                    "barcode_qty": _mobile_build_row_barcode_qty(
                        placement_payload,
                        pallet_code,
                        code,
                        payload,
                        qty,
                        explicit_barcode_qty=requested_barcode_qty,
                    ),
                }
            )
        if result:
            return result

    candidates = _matching_boxes_for_pallet(
        placement_payload,
        pallet_code,
        barcode_values,
        sku_values,
        goods_type_values,
    )
    candidates.sort(key=lambda row: (_as_int(row.get("qty")) or 0, str(row.get("code") or "")))
    if not candidates:
        return []

    remaining_by_barcode = {
        str(barcode).strip(): _as_int(qty)
        for barcode, qty in requested_barcode_qty.items()
        if str(barcode or "").strip() and _as_int(qty) > 0
    }
    remaining_qty = requested_qty or _barcode_qty_total(remaining_by_barcode)
    rows: list[dict] = []
    for candidate in candidates:
        box_code = _normalize_box_code(candidate.get("code"))
        box_qty = _as_int(candidate.get("qty"))
        if not box_code or box_qty <= 0:
            continue
        if remaining_by_barcode:
            barcode_qty = _mobile_allocate_barcode_qty(
                candidate.get("barcode_qty") or {},
                box_qty,
                remaining_by_barcode,
            )
            qty = _barcode_qty_total(barcode_qty)
        else:
            qty = min(box_qty, remaining_qty)
            barcode_qty = _mobile_build_row_barcode_qty(
                placement_payload,
                pallet_code,
                box_code,
                payload,
                qty,
            )
        if qty <= 0:
            continue
        rows.append({"box_code": box_code, "qty": qty, "barcode_qty": barcode_qty})
        remaining_qty = max(remaining_qty - qty, 0)
        if remaining_qty <= 0 and all(qty <= 0 for qty in remaining_by_barcode.values()):
            break
    return rows


def _mobile_sync_execution_payload(task: MoveTask, payload: dict, placement_payload: dict) -> dict:
    move_mode = _normalize_move_mode(payload.get("move_mode"), payload.get("pick_mode"))
    is_flexible_partial = (
        move_mode == MOVE_MODE_BOX_PARTIAL
        and _requested_box_selection_mode(payload) == BOX_SELECTION_PATTERN_MATCHING
        and _requested_box_count(payload) > 0
        and bool(_partial_pick_patterns(payload))
    )
    if move_mode == MOVE_MODE_BOX_PARTIAL and not _requested_partial_rows(payload) and not is_flexible_partial:
        planned_rows = _mobile_plan_partial_rows(payload, placement_payload, str(payload.get("pallet_code") or "").strip())
        if planned_rows:
            payload["requested_rows"] = planned_rows
            payload["requested_boxes"] = [row["box_code"] for row in planned_rows]
            payload["requested_box"] = planned_rows[0]["box_code"] if len(planned_rows) == 1 else ""
            payload["requested_qty"] = sum(_as_int(row.get("qty")) for row in planned_rows)
    payload.setdefault("mobile_execution", {})
    execution = dict(payload.get("mobile_execution") or {})
    execution.setdefault("source_confirmed", True)
    payload["mobile_execution"] = execution
    return payload


def _mobile_box_specs(payload: dict, placement_payload: dict) -> list[dict]:
    pallet_code = str(payload.get("pallet_code") or "").strip()
    move_mode = _normalize_move_mode(payload.get("move_mode"), payload.get("pick_mode"))
    pallets, _pallet_idx, pallet = _find_pallet_in_placement(placement_payload, pallet_code)
    pallet_box_codes = [
        _normalize_box_code(code)
        for code in ((pallet or {}).get("boxes") or [])
        if _normalize_box_code(code)
    ]
    if move_mode == MoveTask.MODE_PALLET_FULL:
        specs = []
        for box_code in pallet_box_codes:
            _boxes, _box_idx, box = _find_box_in_placement(placement_payload, box_code)
            box_qty = 0
            for item in (box or {}).get("items") or []:
                box_qty += _as_int(item.get("qty"))
            specs.append(
                {
                    "box_code": box_code,
                    "box_qty": box_qty,
                    "units_required": 0,
                    "unit_barcode_qty": {},
                    "return_required": False,
                }
            )
        return specs
    if move_mode == MoveTask.MODE_BOX_FULL:
        if _is_any_matching_box_selection(payload):
            selection_mode = _requested_box_selection_mode(payload)
            specs = []
            if selection_mode == BOX_SELECTION_PATTERN_MATCHING:
                matching_rows = _matching_boxes_for_pattern_selection(
                    placement_payload,
                    pallet_code,
                    payload,
                )
            else:
                barcode_values, sku_values, goods_type_values, _requested_qty = _mobile_selector_sets(payload)
                matching_rows = _matching_boxes_for_pallet(
                    placement_payload,
                    pallet_code,
                    barcode_values,
                    sku_values,
                    goods_type_values,
                )
            for row in matching_rows:
                box_code = _normalize_box_code(row.get("code"))
                box_qty = _as_int(row.get("qty"))
                if not box_code or box_qty <= 0:
                    continue
                specs.append(
                    {
                        "box_code": box_code,
                        "box_qty": box_qty,
                        "units_required": 0,
                        "unit_barcode_qty": {},
                        "return_required": False,
                    }
                )
            return specs
        requested_codes = _payload_box_codes(payload)
        if not requested_codes:
            requested_codes = list(pallet_box_codes)
        specs = []
        for box_code in requested_codes:
            _boxes, _box_idx, box = _find_box_in_placement(placement_payload, box_code)
            box_qty = 0
            for item in (box or {}).get("items") or []:
                box_qty += _as_int(item.get("qty"))
            specs.append(
                {
                    "box_code": box_code,
                    "box_qty": box_qty,
                    "units_required": 0,
                    "unit_barcode_qty": {},
                    "return_required": False,
                }
            )
        return specs

    if (
        move_mode == MOVE_MODE_BOX_PARTIAL
        and _requested_box_selection_mode(payload) == BOX_SELECTION_PATTERN_MATCHING
        and _requested_box_count(payload) > 0
        and _partial_pick_patterns(payload)
    ):
        requested_box_count = _requested_box_count(payload)
        matching_rows = _matching_boxes_for_pattern_selection(placement_payload, pallet_code, payload)
        matching_by_code = _matching_box_row_map(matching_rows)
        execution = dict(payload.get("mobile_execution") or {})
        scanned_boxes = _matching_selected_box_codes(execution.get("boxes_scanned") or [], matching_by_code)
        if len(scanned_boxes) >= requested_box_count:
            selected_boxes = scanned_boxes[:requested_box_count]
            _whole_boxes, partial_rows = _split_mixed_partial_box_selection(
                payload,
                placement_payload,
                pallet_code,
                selected_boxes,
            )
            partial_by_code = {
                _normalize_box_code(row.get("box_code")).lower(): row
                for row in partial_rows
                if _normalize_box_code(row.get("box_code"))
            }
            specs = []
            for box_code in selected_boxes:
                row = matching_by_code.get(box_code.lower()) or {}
                box_qty = _as_int(row.get("qty"))
                partial_row = partial_by_code.get(box_code.lower()) or {}
                pick_qty = _as_int(partial_row.get("qty"))
                specs.append(
                    {
                        "box_code": box_code,
                        "box_qty": box_qty,
                        "units_required": pick_qty,
                        "unit_barcode_qty": _normalize_barcode_qty_map(partial_row.get("barcode_qty")),
                        "return_required": bool(partial_row) and box_qty > pick_qty,
                    }
                )
            return specs

        specs = []
        for row in matching_rows:
            box_code = _normalize_box_code(row.get("code"))
            box_qty = _as_int(row.get("qty"))
            if not box_code or box_qty <= 0:
                continue
            specs.append(
                {
                    "box_code": box_code,
                    "box_qty": box_qty,
                    "units_required": 0,
                    "unit_barcode_qty": {},
                    "return_required": False,
                }
            )
        return specs

    specs = []
    for row in _mobile_plan_partial_rows(payload, placement_payload, pallet_code):
        box_code = _normalize_box_code(row.get("box_code"))
        qty = _as_int(row.get("qty"))
        if not box_code or qty <= 0:
            continue
        _boxes, _box_idx, box = _find_box_in_placement(placement_payload, box_code)
        box_qty = 0
        for item in (box or {}).get("items") or []:
            box_qty += _as_int(item.get("qty"))
        specs.append(
            {
                "box_code": box_code,
                "box_qty": box_qty,
                "units_required": qty,
                "unit_barcode_qty": _normalize_barcode_qty_map(row.get("barcode_qty")),
                "return_required": box_qty > qty,
            }
        )
    return specs


def _cached_mobile_placement_payload(payload: dict) -> dict:
    cached_payload = payload.get("mobile_placement_payload")
    if isinstance(cached_payload, dict):
        return copy.deepcopy(cached_payload)
    return {}


def _cached_mobile_placement_source(payload: dict) -> dict:
    cached_source = payload.get("mobile_placement_source")
    if isinstance(cached_source, dict):
        return dict(cached_source)
    return {}


def _cache_mobile_placement_snapshot(task: MoveTask, payload: dict, placement_entry: SimpleNamespace) -> tuple[dict, dict]:
    cached_payload = copy.deepcopy(dict(placement_entry.payload or {}))
    source_kind = "warehouse_stock" if getattr(placement_entry, "stock_tree", None) is not None else "audit_placement"
    payload["mobile_placement_payload"] = cached_payload
    payload["mobile_placement_source"] = {
        "order_id": str(getattr(placement_entry, "order_id", "") or "").strip(),
        "order_type": str(getattr(placement_entry, "order_type", "") or "").strip() or "receiving",
        "source_kind": source_kind,
        "agency_id": int(
            getattr(placement_entry, "agency_id", 0)
            or getattr(getattr(task, "request", None), "agency_id", 0)
            or 0
        ),
    }
    return payload, copy.deepcopy(cached_payload)


def _ensure_mobile_runtime_payload(task: MoveTask, payload: dict) -> tuple[dict, dict, dict, bool, str]:
    original_payload = copy.deepcopy(payload)
    placement_payload = _cached_mobile_placement_payload(payload)
    placement_source = _cached_mobile_placement_source(payload)
    if not placement_payload:
        placement_entry = _find_placement_entry_for_pallet(
            str(payload.get("pallet_code") or "").strip(),
            receiving_order_id=str(payload.get("receiving_order_id") or "").strip() or None,
            processing_order_id=str(payload.get("processing_order_id") or "").strip() or None,
            agency_id=int(task.request.agency_id) if task.request and task.request.agency_id else None,
        )
        if not placement_entry:
            return payload, {}, {}, False, "\u041f\u0430\u043b\u043b\u0435\u0442\u0430 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430 \u0432 \u0440\u0430\u0437\u043c\u0435\u0449\u0435\u043d\u0438\u0438."
        payload, placement_payload = _cache_mobile_placement_snapshot(task, payload, placement_entry)
        placement_source = _cached_mobile_placement_source(payload)
    payload = _mobile_sync_execution_payload(task, payload, placement_payload)
    return payload, placement_payload, placement_source, payload != original_payload, ""


def _build_mobile_execution_snapshot_payload(task: MoveTask, payload: dict, placement_payload: dict) -> dict:
    execution = dict(payload.get("mobile_execution") or {})
    flexible_box_selection = _is_any_matching_box_selection(payload)
    requested_box_target = _requested_box_count(payload) if flexible_box_selection else 0
    scanned_boxes = {
        _normalize_box_code(code).lower()
        for code in (execution.get("boxes_scanned") or [])
        if _normalize_box_code(code)
    }
    scanned_units_raw = execution.get("units_scanned") or {}
    scanned_units: dict[str, dict[str, int]] = {}
    for box_code, values in dict(scanned_units_raw).items():
        normalized_box = _normalize_box_code(box_code)
        if not normalized_box or not isinstance(values, dict):
            continue
        scanned_units[normalized_box.lower()] = {
            str(barcode).strip(): _as_int(qty)
            for barcode, qty in values.items()
            if str(barcode or "").strip() and _as_int(qty) > 0
        }

    box_specs = []
    for spec in _mobile_box_specs(payload, placement_payload):
        box_code = _normalize_box_code(spec.get("box_code"))
        unit_plan = _normalize_barcode_qty_map(spec.get("unit_barcode_qty"))
        unit_scanned = scanned_units.get(box_code.lower(), {})
        unit_total = _barcode_qty_total(unit_plan)
        unit_scanned_total = min(_barcode_qty_total(unit_scanned), unit_total)
        requires_unit_scan = unit_total > 0
        box_scanned = box_code.lower() in scanned_boxes
        box_complete = box_scanned and (not requires_unit_scan or unit_scanned_total >= unit_total)
        box_specs.append(
            {
                "box_code": box_code,
                "box_qty": _as_int(spec.get("box_qty")),
                "units_required": _as_int(spec.get("units_required")),
                "unit_barcode_qty": unit_plan,
                "unit_barcode_preview": ", ".join(
                    f"{barcode} Г— {qty}" for barcode, qty in list(unit_plan.items())[:3]
                ),
                "unit_scanned_total": unit_scanned_total,
                "box_scanned": box_scanned,
                "requires_unit_scan": requires_unit_scan,
                "box_complete": box_complete,
                "return_required": bool(spec.get("return_required")),
            }
        )

    source_label = payload.get("from_label") or ""
    destination_label = payload.get("to_label") or ""
    source_code = _location_scan_code(payload.get("from_location") or {})
    destination_code = _location_scan_code(payload.get("to_location") or {})
    source_prompt_label = _task_source_prompt_label(payload)
    pallet_display = display_scan_text(payload.get("pallet_code"))
    source_confirmed = bool(execution.get("source_confirmed"))
    pallet_confirmed = bool(execution.get("pallet_confirmed"))
    destination_confirmed = bool(execution.get("destination_confirmed"))
    boxes_total = requested_box_target if flexible_box_selection and requested_box_target > 0 else len(box_specs)
    boxes_scanned_count = sum(1 for row in box_specs if row["box_scanned"])
    if flexible_box_selection and boxes_total > 0:
        boxes_scanned_count = min(boxes_scanned_count, boxes_total)
    pallet_boxes_total = len(box_specs)
    pallet_boxes_found_count = min(boxes_scanned_count, pallet_boxes_total)
    box_selection_complete = (not flexible_box_selection) or boxes_total <= 0 or boxes_scanned_count >= boxes_total
    boxes_completed = sum(1 for row in box_specs if row["box_complete"])
    boxes_ready = source_confirmed and pallet_confirmed
    all_boxes_complete = (
        bool(box_specs)
        and boxes_total > 0
        and box_selection_complete
        and boxes_completed >= boxes_total
    )
    expected_units_total = sum(_barcode_qty_total(row["unit_barcode_qty"]) for row in box_specs)
    scanned_units_total = sum(int(row["unit_scanned_total"]) for row in box_specs)
    pending_unit_rows = [
        row
        for row in box_specs
        if row["box_scanned"] and row["requires_unit_scan"] and not row["box_complete"]
    ]
    current_step = "pallet"
    expected_scan = pallet_display
    prompt = "\u041e\u0442\u0441\u043a\u0430\u043d\u0438\u0440\u0443\u0439\u0442\u0435 QR \u043f\u0430\u043b\u043b\u0435\u0442\u044b."
    if source_confirmed and pallet_confirmed:
        if flexible_box_selection and boxes_total > 0 and not box_selection_complete:
            current_step = "boxes"
            expected_scan = "\u043b\u044e\u0431\u043e\u0439 \u043f\u043e\u0434\u0445\u043e\u0434\u044f\u0449\u0438\u0439 \u043a\u043e\u0440\u043e\u0431"
            prompt = (
                "\u0421\u043a\u0430\u043d\u0438\u0440\u0443\u0439 \u043f\u043e\u0434\u0445\u043e\u0434\u044f\u0449\u0438\u0435 \u043a\u043e\u0440\u043e\u0431\u0430. "
                f"\u041d\u0430\u0439\u0434\u0435\u043d\u043e {boxes_scanned_count} \u0438\u0437 {boxes_total}."
            )
        elif pending_unit_rows:
            current_step = "units"
            active_unit_row = pending_unit_rows[0]
            expected_scan = active_unit_row["unit_barcode_preview"] or "\u0442\u043e\u0432\u0430\u0440 \u0438\u0437 \u043a\u043e\u0440\u043e\u0431\u0430"
            prompt = (
                f"\u0421\u043a\u0430\u043d\u0438\u0440\u0443\u0439 \u0442\u043e\u0432\u0430\u0440 \u0438\u0437 \u043a\u043e\u0440\u043e\u0431\u0430 {active_unit_row['box_code']}: "
                f"{active_unit_row['unit_scanned_total']} \u0438\u0437 {active_unit_row['units_required']}."
            )
        else:
            current_step = "boxes"
            expected_scan = ", ".join(row["box_code"] for row in box_specs if not row["box_complete"]) or "-"
            prompt = "\u0421\u043a\u0430\u043d\u0438\u0440\u0443\u0439 \u043d\u0443\u0436\u043d\u044b\u0435 \u043a\u043e\u0440\u043e\u0431\u0430 \u0432 \u043b\u044e\u0431\u043e\u043c \u043f\u043e\u0440\u044f\u0434\u043a\u0435."
    if source_confirmed and pallet_confirmed and all_boxes_complete:
        current_step = "destination"
        expected_scan = destination_code
        prompt = f"\u041e\u0442\u0432\u0435\u0437\u0438 -> {destination_code}"
    if destination_confirmed:
        current_step = "done"
        expected_scan = ""
        prompt = "\u0417\u0430\u0434\u0430\u043d\u0438\u0435 \u0432\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u043e."
    if flexible_box_selection and not box_selection_complete:
        boxes_pending = [row for row in box_specs if not row["box_scanned"]]
        boxes_found = [row for row in box_specs if row["box_scanned"]]
    else:
        boxes_pending = [row for row in box_specs if not row["box_complete"]]
        boxes_found = [row for row in box_specs if row["box_scanned"] or row["box_complete"]]
        if flexible_box_selection and all_boxes_complete:
            boxes_pending = []
    return {
        "source_code": source_code,
        "source_label": source_label,
        "source_confirmed": source_confirmed,
        "pallet_code": display_scan_text(payload.get("pallet_code")),
        "pallet_confirmed": pallet_confirmed,
        "destination_code": destination_code,
        "destination_label": destination_label,
        "destination_confirmed": destination_confirmed,
        "boxes": box_specs,
        "boxes_pending": boxes_pending,
        "boxes_found": boxes_found,
        "boxes_total": boxes_total,
        "pallet_boxes_total": pallet_boxes_total,
        "pallet_boxes_found_count": pallet_boxes_found_count,
        "boxes_completed": boxes_completed,
        "boxes_scanned_count": boxes_scanned_count,
        "box_selection_complete": box_selection_complete,
        "flexible_box_selection": flexible_box_selection,
        "boxes_ready": boxes_ready,
        "all_boxes_complete": all_boxes_complete,
        "expected_units_total": expected_units_total,
        "scanned_units_total": scanned_units_total,
        "current_step": current_step,
        "expected_scan": expected_scan,
        "prompt": prompt,
        "task_status": _task_payload_status(task, payload),
    }


def build_mobile_execution_snapshot(legacy_order_id: str) -> dict:
    task = _load_task(legacy_order_id)
    if not task:
        return {}
    payload = dict(task.payload or {})
    if _pallet_choice_pending(payload):
        candidate_locations = _candidate_location_groups(payload)
        return {
            "source_code": "",
            "source_label": "\u041f\u043e\u0434\u0445\u043e\u0434\u044f\u0449\u0438\u0435 \u043c\u0435\u0441\u0442\u0430",
            "source_confirmed": False,
            "pallet_code": "",
            "pallet_confirmed": False,
            "destination_code": _location_scan_code(payload.get("to_location") or {}),
            "destination_label": str(payload.get("to_label") or "").strip(),
            "destination_confirmed": False,
            "boxes": [],
            "boxes_pending": [],
            "boxes_found": [],
            "boxes_total": 0,
            "boxes_completed": 0,
            "boxes_scanned_count": 0,
            "box_selection_complete": False,
            "flexible_box_selection": False,
            "boxes_ready": False,
            "all_boxes_complete": False,
            "expected_units_total": 0,
            "scanned_units_total": 0,
            "current_step": "pallet",
            "expected_scan": "QR \u043f\u043e\u0434\u0445\u043e\u0434\u044f\u0449\u0435\u0439 \u043f\u0430\u043b\u043b\u0435\u0442\u044b",
            "prompt": "\u0412\u044b\u0431\u0435\u0440\u0438 \u043c\u0435\u0441\u0442\u043e \u0438\u0437 \u0441\u043f\u0438\u0441\u043a\u0430 \u0438 \u043e\u0442\u0441\u043a\u0430\u043d\u0438\u0440\u0443\u0439 QR \u043f\u043e\u0434\u0445\u043e\u0434\u044f\u0449\u0435\u0439 \u043f\u0430\u043b\u043b\u0435\u0442\u044b.",
            "candidate_locations": candidate_locations,
            "candidate_pallets": _candidate_pallet_options(payload),
            "task_status": _task_payload_status(task, payload),
        }

    payload, placement_payload, _placement_source, payload_changed, error = _ensure_mobile_runtime_payload(task, payload)
    if error:
        return {}
    if payload_changed:
        task.payload = payload
        task.save(update_fields=["payload", "updated_at"])
    return _build_mobile_execution_snapshot_payload(task, payload, placement_payload)


@transaction.atomic
def scan_move_task_step(
    *,
    legacy_order_id: str,
    scan_value: str,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    task = _load_task(legacy_order_id)
    if not task:
        return MoveTaskCommandResult(ok=False, error="Р—Р°РґР°РЅРёРµ РЅРµ РЅР°Р№РґРµРЅРѕ.")
    if not employee_id:
        return MoveTaskCommandResult(ok=False, error="РџСЂРѕС„РёР»СЊ СЃРѕС‚СЂСѓРґРЅРёРєР° РЅРµ РЅР°Р№РґРµРЅ.")

    payload = dict(task.payload or {})
    status = _task_payload_status(task, payload)
    assigned_to_id = _task_assignee_id(task, payload)
    status, assigned_to_id, payload = _promote_created_assigned_task(
        task,
        payload,
        status=status,
        assigned_to_id=assigned_to_id,
        user=user,
        employee_id=employee_id,
        employee_name=employee_name,
    )
    scan_code = str(scan_value or "").strip()
    pallet_code = str(payload.get("pallet_code") or "").strip()
    pallet_code_display = _task_pallet_display(payload, fallback=scan_code)
    source_code = _task_source_code(payload)
    destination_code = _task_destination_code(payload)
    from_location = payload.get("from_location") or {}
    to_location = payload.get("to_location") or {}
    if status == MoveTask.STATUS_DONE:
        if _same_location_scan(scan_code, to_location):
            return MoveTaskCommandResult(
                ok=True,
                message=(
                    f"РњРµСЃС‚Рѕ {destination_code} СѓР¶Рµ РїРѕРґС‚РІРµСЂР¶РґРµРЅРѕ. "
                    f"РџР°Р»Р»РµС‚Р° {pallet_code_display} СѓР¶Рµ СЂР°Р·РјРµС‰РµРЅР°."
                ),
                completed=True,
            )
        if _same_pallet_code_scan(scan_code, pallet_code):
            return MoveTaskCommandResult(
                ok=False,
                error=f"РџР°Р»Р»РµС‚Р° {pallet_code_display} СѓР¶Рµ РґРѕСЃС‚Р°РІР»РµРЅР° РІ {destination_code}.",
            )
        return MoveTaskCommandResult(ok=False, error="Р—Р°РґР°РЅРёРµ СѓР¶Рµ РІС‹РїРѕР»РЅРµРЅРѕ.")
    if status != MoveTask.STATUS_IN_PROGRESS:
        return MoveTaskCommandResult(ok=False, error="РЎРЅР°С‡Р°Р»Р° РІРѕР·СЊРјРёС‚Рµ Р·Р°РґР°РЅРёРµ РІ СЂР°Р±РѕС‚Сѓ.")
    if assigned_to_id and assigned_to_id != employee_id:
        return MoveTaskCommandResult(ok=False, error="Р—Р°РґР°РЅРёРµ РЅР°Р·РЅР°С‡РµРЅРѕ РґСЂСѓРіРѕРјСѓ РІРѕРґРёС‚РµР»СЋ.")

    if _pallet_choice_pending(payload):
        option = _candidate_pallet_for_scan(payload, scan_code)
        if option:
            return _bind_flexible_pallet_choice(
                task=task,
                payload=payload,
                option=option,
                scan_code=scan_code,
                user=user,
                employee_id=employee_id,
                employee_name=employee_name,
            )
        return MoveTaskCommandResult(
            ok=False,
            error=(
                f"\u041f\u0430\u043b\u043b\u0435\u0442\u0430 {display_scan_text(scan_code)} "
                "\u043d\u0435 \u043f\u043e\u0434\u0445\u043e\u0434\u0438\u0442. "
                "\u041e\u0442\u0441\u043a\u0430\u043d\u0438\u0440\u0443\u0439\u0442\u0435 QR \u043e\u0434\u043d\u043e\u0439 "
                "\u0438\u0437 \u043f\u043e\u0434\u0445\u043e\u0434\u044f\u0449\u0438\u0445 \u043f\u0430\u043b\u043b\u0435\u0442 \u043d\u0430 \u044d\u043a\u0440\u0430\u043d\u0435."
            ),
        )

    payload, placement_payload, _placement_source, payload_changed, error = _ensure_mobile_runtime_payload(task, payload)
    if error:
        return MoveTaskCommandResult(ok=False, error=error)
    if payload_changed:
        task.payload = payload
        task.save(update_fields=["payload", "updated_at"])
    execution = dict(payload.get("mobile_execution") or {})
    execution.setdefault("boxes_scanned", [])
    execution.setdefault("units_scanned", {})
    if not scan_code:
        return MoveTaskCommandResult(ok=False, error="РћС‚СЃРєР°РЅРёСЂСѓР№С‚Рµ РєРѕРґ.")

    snapshot = _build_mobile_execution_snapshot_payload(task, payload, placement_payload)
    source_code = snapshot["source_code"]
    destination_code = snapshot["destination_code"]
    source_prompt_label = _task_source_prompt_label(payload)

    if snapshot["destination_confirmed"]:
        complete_result = complete_move_task(
            legacy_order_id=legacy_order_id,
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
        )
        if not complete_result.ok:
            return complete_result
        return MoveTaskCommandResult(
            ok=True,
            task=complete_result.task or task,
            payload=complete_result.payload or payload,
            message=complete_result.message or f"РњРµСЃС‚Рѕ {destination_code} РїРѕРґС‚РІРµСЂР¶РґРµРЅРѕ.",
            completed=True,
        )

    if not snapshot["pallet_confirmed"]:
        if _same_location_scan(scan_code, from_location):
            return MoveTaskCommandResult(
                ok=True,
                task=task,
                payload=payload,
                message=(
                    f"РђРґСЂРµСЃ {source_prompt_label} РїРѕРєР°Р·Р°РЅ РЅР° СЌРєСЂР°РЅРµ. "
                    f"РўРµРїРµСЂСЊ Р¶РґРµРј QR РїР°Р»Р»РµС‚С‹ {pallet_code_display}."
                ),
            )
        if _same_location_scan(scan_code, to_location):
            return MoveTaskCommandResult(
                ok=False,
                error=(
                    f"РћС‚СЃРєР°РЅРёСЂРѕРІР°РЅРѕ РјРµСЃС‚Рѕ РЅР°Р·РЅР°С‡РµРЅРёСЏ {destination_code}. "
                    f"РЎРµР№С‡Р°СЃ Р¶РґРµРј QR РїР°Р»Р»РµС‚С‹ {pallet_code_display}."
                ),
            )
        if not _same_pallet_code_scan(scan_code, pallet_code):
            return MoveTaskCommandResult(
                ok=False,
                error=(
                    f"РџР°Р»Р»РµС‚Р° {display_scan_text(scan_code)} РЅРµ РїРѕРґС…РѕРґРёС‚. "
                    f"Р–РґРµРј QR РїР°Р»Р»РµС‚С‹ {pallet_code_display}."
                ),
            )
        execution["pallet_confirmed"] = True
        execution["last_scan"] = scan_code
        payload["mobile_execution"] = execution
        task.payload = payload
        task.save(update_fields=["payload", "updated_at"])
        return MoveTaskCommandResult(ok=True, task=task, payload=payload, message="РџР°Р»Р»РµС‚Р° РїРѕРґС‚РІРµСЂР¶РґРµРЅР°.")

    if snapshot["all_boxes_complete"] and not snapshot["destination_confirmed"]:
        if _same_pallet_code_scan(scan_code, pallet_code):
            return MoveTaskCommandResult(
                ok=False,
                error=(
                    f"РџР°Р»Р»РµС‚Р° {pallet_code_display} СѓР¶Рµ РїРѕРґС‚РІРµСЂР¶РґРµРЅР°. "
                    f"РўРµРїРµСЂСЊ РѕС‚СЃРєР°РЅРёСЂСѓР№С‚Рµ РјРµСЃС‚Рѕ {destination_code}."
                ),
            )
        if _same_location_scan(scan_code, from_location):
            return MoveTaskCommandResult(
                ok=False,
                error=(
                    f"Р’С‹ СЃРЅРѕРІР° РѕС‚СЃРєР°РЅРёСЂРѕРІР°Р»Рё РёСЃС…РѕРґРЅРѕРµ РјРµСЃС‚Рѕ {source_code}. "
                    f"РќСѓР¶РЅРѕ РїРѕРґС‚РІРµСЂРґРёС‚СЊ РјРµСЃС‚Рѕ РЅР°Р·РЅР°С‡РµРЅРёСЏ {destination_code}."
                ),
            )
        if not _same_location_scan(scan_code, to_location):
            return MoveTaskCommandResult(
                ok=False,
                error=(
                    f"РњРµСЃС‚Рѕ {display_scan_text(scan_code)} РЅРµ РїРѕРґС…РѕРґРёС‚ РґР»СЏ СЌС‚РѕР№ РїР°Р»Р»РµС‚С‹. "
                    f"РћР¶РёРґР°РµС‚СЃСЏ РјРµСЃС‚Рѕ {destination_code}."
                ),
            )
        execution = dict(payload.get("mobile_execution") or {})
        execution["destination_confirmed"] = True
        execution["destination_override_pending"] = False
        execution["last_scan"] = scan_code
        payload["mobile_execution"] = execution
        task.payload = payload
        task.save(update_fields=["payload", "updated_at"])
        complete_result = complete_move_task(
            legacy_order_id=legacy_order_id,
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
        )
        if not complete_result.ok:
            return complete_result
        return MoveTaskCommandResult(
            ok=True,
            task=task,
            payload=payload,
            message=complete_result.message or f"РњРµСЃС‚Рѕ {destination_code} РїРѕРґС‚РІРµСЂР¶РґРµРЅРѕ.",
            completed=True,
        )

    pending_boxes = [row for row in snapshot["boxes"] if not row["box_complete"]]
    scanned_boxes = {
        _normalize_box_code(code).lower()
        for code in (execution.get("boxes_scanned") or [])
        if _normalize_box_code(code)
    }
    selection_mode = _requested_box_selection_mode(payload)
    units_scanned = dict(execution.get("units_scanned") or {})
    for row in pending_boxes:
        if _same_box_code_scan(scan_code, row["box_code"]):
            box_key = row["box_code"].lower()
            if box_key not in scanned_boxes:
                if selection_mode == BOX_SELECTION_PATTERN_MATCHING:
                    prospective_scans = list(execution.get("boxes_scanned") or []) + [row["box_code"]]
                    resolved_boxes = _assign_requested_patterns_to_boxes(
                        payload,
                        placement_payload,
                        pallet_code,
                        prospective_scans,
                        allow_partial=True,
                    )
                    if not resolved_boxes or row["box_code"].lower() not in {
                        _normalize_box_code(code).lower() for code in resolved_boxes
                    }:
                        return MoveTaskCommandResult(
                            ok=False,
                            error=(
                                f"РљРѕСЂРѕР± {row['box_code']} РЅРµ РїРѕРґС…РѕРґРёС‚ РїРѕРґ РѕСЃС‚Р°РІС€СѓСЋСЃСЏ РєРѕСЂРѕР±РѕС‡РЅСѓСЋ СЃС…РµРјСѓ. "
                                "РћС‚СЃРєР°РЅРёСЂСѓР№С‚Рµ РґСЂСѓРіРѕР№ РїРѕРґС…РѕРґСЏС‰РёР№ РєРѕСЂРѕР±."
                            ),
                        )
                expected_barcodes = set((row.get("unit_barcode_qty") or {}).keys())
                if not expected_barcodes:
                    expected_barcodes = _mobile_selector_sets(payload)[0]
                try:
                    validate_scanned_box_identity(
                        code=row["box_code"],
                        agency_id=(
                            _as_int(payload.get("agency_id"))
                            or _as_int(getattr(getattr(task, "request", None), "agency_id", None))
                        ),
                        pallet_code=pallet_code,
                        barcodes=expected_barcodes,
                    )
                except ValidationError as exc:
                    return MoveTaskCommandResult(ok=False, error="; ".join(exc.messages))
                try:
                    claim_boxes_for_task(
                        task,
                        [row["box_code"]],
                        claimed_by=user,
                        claim_kind=(
                            BoxClaim.KIND_PARTIAL
                            if _normalize_move_mode(payload.get("move_mode"), payload.get("pick_mode")) == MOVE_MODE_BOX_PARTIAL
                            else BoxClaim.KIND_BOX
                        ),
                        payload={
                            "legacy_order_id": task.legacy_order_id,
                            "shipping_order_id": str(payload.get("shipping_order_id") or "").strip(),
                            "scan": scan_code,
                        },
                    )
                except ValueError as exc:
                    return MoveTaskCommandResult(ok=False, error=str(exc))
                execution["boxes_scanned"] = list(execution.get("boxes_scanned") or []) + [row["box_code"]]
                execution["last_scan"] = row["box_code"]
                payload["mobile_execution"] = execution
                task.payload = payload
                task.save(update_fields=["payload", "updated_at"])
                if row["requires_unit_scan"]:
                    return MoveTaskCommandResult(
                        ok=True,
                        task=task,
                        payload=payload,
                        message=(
                            f"РљРѕСЂРѕР± {row['box_code']} РЅР°Р№РґРµРЅ. РЎРєР°РЅРёСЂСѓР№С‚Рµ С‚РѕРІР°СЂ: "
                            f"{row['unit_scanned_total']} РёР· {row['units_required']}."
                        ),
                    )
                return MoveTaskCommandResult(
                    ok=True,
                    task=task,
                    payload=payload,
                    message=f"РљРѕСЂРѕР± {row['box_code']} РїРѕРґС‚РІРµСЂР¶РґРµРЅ.",
                )
            break

    scanned_box_specs = [
        row for row in pending_boxes if row["box_code"].lower() in scanned_boxes and row["requires_unit_scan"]
    ]
    for row in scanned_box_specs:
        box_key = row["box_code"].lower()
        allowed = row["unit_barcode_qty"]
        if scan_code not in allowed:
            continue
        scanned_for_box = {
            str(barcode).strip(): _as_int(qty)
            for barcode, qty in dict(units_scanned.get(row["box_code"]) or units_scanned.get(box_key) or {}).items()
            if str(barcode or "").strip()
        }
        if _as_int(scanned_for_box.get(scan_code)) >= _as_int(allowed.get(scan_code)):
            return MoveTaskCommandResult(
                ok=False,
                error=f"РўРѕРІР°СЂ {scan_code} СѓР¶Рµ РЅР°Р±СЂР°РЅ РїРѕР»РЅРѕСЃС‚СЊСЋ РґР»СЏ РєРѕСЂРѕР±Р° {row['box_code']}.",
            )
        scanned_for_box[scan_code] = _as_int(scanned_for_box.get(scan_code)) + 1
        units_scanned[row["box_code"]] = scanned_for_box
        execution["units_scanned"] = units_scanned
        execution["last_scan"] = scan_code
        payload["mobile_execution"] = execution
        task.payload = payload
        task.save(update_fields=["payload", "updated_at"])
        scanned_total = _barcode_qty_total(scanned_for_box)
        message = f"РўРѕРІР°СЂ {scan_code} РїРѕРґС‚РІРµСЂР¶РґРµРЅ: {scanned_total} РёР· {row['units_required']}."
        if scanned_total >= row["units_required"] and row["return_required"]:
            message += f" Р’РµСЂРЅРё РєРѕСЂРѕР± {row['box_code']} РЅР° РјРµСЃС‚Рѕ."
        return MoveTaskCommandResult(ok=True, task=task, payload=payload, message=message)

    expected = snapshot.get("expected_scan") or "СЃР»РµРґСѓСЋС‰РёР№ С€Р°Рі Р·Р°РґР°РЅРёСЏ"
    return MoveTaskCommandResult(
        ok=False,
        error=(
            f"РљРѕРґ {display_scan_text(scan_code)} СЃРµР№С‡Р°СЃ РЅРµ РЅСѓР¶РµРЅ. "
            f"РћР¶РёРґР°РµС‚СЃСЏ: {expected}."
        ),
    )


def _find_placement_entry_for_pallet(
    pallet_code: str,
    *,
    receiving_order_id: str | None = None,
    processing_order_id: str | None = None,
    agency_id: int | None = None,
) -> SimpleNamespace | None:
    target = str(pallet_code or "").strip()
    if not target:
        return None
    stock_tree = OperationalStockService.get_pallet_tree(target, agency_id=agency_id)
    if stock_tree is not None:
        return SimpleNamespace(
            order_id=stock_tree.context_order_id,
            order_type=stock_tree.context_order_type,
            agency=stock_tree.agency,
            agency_id=int(stock_tree.agency.id or 0),
            payload=stock_tree.payload,
            stock_tree=stock_tree,
        )
    candidate_order_ids: list[str] = []
    seen_order_ids: set[str] = set()
    for raw_order_id in (processing_order_id, receiving_order_id):
        order_id = str(raw_order_id or "").strip()
        if not order_id or order_id in seen_order_ids:
            continue
        seen_order_ids.add(order_id)
        candidate_order_ids.append(order_id)
    if candidate_order_ids:
        blocked_orders: set[tuple[str, str]] = set()
        entries = OrderAuditEntry.objects.filter(
            order_type__in=("receiving", "processing"),
            order_id__in=candidate_order_ids,
        ).order_by("-created_at", "-id")
        if agency_id:
            entries = entries.filter(agency_id=int(agency_id))
        for entry in entries:
            order_key = (str(entry.order_type or "").strip(), str(entry.order_id or "").strip())
            if order_key in blocked_orders:
                continue
            payload = entry.payload or {}
            if payload.get("act") != "placement":
                continue
            state = str(payload.get("act_state") or "closed").strip().lower()
            if state != "closed":
                blocked_orders.add(order_key)
                continue
            for pallet in payload.get("act_pallets") or []:
                if not isinstance(pallet, dict):
                    continue
                if str(pallet.get("code") or "").strip() != target:
                    continue
                return SimpleNamespace(
                    order_id=str(entry.order_id or "").strip(),
                    order_type=str(entry.order_type or "").strip() or "receiving",
                    agency=entry.agency,
                    agency_id=int(entry.agency_id or 0),
                    payload=payload,
                    stock_tree=None,
                )
    return None


def _delivered_processing_specs_from_payload(payload: dict) -> list[dict]:
    specs: list[dict] = []
    requested_rows = _requested_partial_rows(payload)
    if requested_rows:
        payload_goods_type = _normalize_goods_type(payload.get("requested_goods_type"))
        for row in requested_rows:
            barcode_qty = _normalize_barcode_qty_map(row.get("barcode_qty"))
            if barcode_qty:
                for barcode, qty in barcode_qty.items():
                    specs.append(
                        {
                            "barcode": barcode,
                            "sku": "",
                            "goods_type": payload_goods_type,
                            "qty": qty,
                        }
                    )
                continue
            row_qty = _as_int(row.get("qty"))
            if row_qty <= 0:
                continue
            specs.append(
                {
                    "barcode": "",
                    "sku": str(payload.get("requested_sku") or "").strip(),
                    "goods_type": payload_goods_type,
                    "qty": row_qty,
                }
            )
        return specs

    requested_barcode_qty = _requested_barcode_qty(payload)
    if requested_barcode_qty:
        payload_goods_type = _normalize_goods_type(payload.get("requested_goods_type"))
        for barcode, qty in requested_barcode_qty.items():
            specs.append(
                {
                    "barcode": barcode,
                    "sku": "",
                    "goods_type": payload_goods_type,
                    "qty": qty,
                }
            )
        return specs

    request_items = payload.get("request_items") or []
    if isinstance(request_items, list):
        for item in request_items:
            if not isinstance(item, dict):
                continue
            item_qty = _as_int(item.get("requested_qty"))
            if item_qty <= 0:
                continue
            item_goods_type = _normalize_goods_type(item.get("requested_goods_type"))
            item_barcodes = [
                str(value).strip()
                for value in (item.get("requested_barcodes") or [])
                if str(value or "").strip()
            ]
            if len(item_barcodes) == 1:
                specs.append(
                    {
                        "barcode": item_barcodes[0],
                        "sku": "",
                        "goods_type": item_goods_type,
                        "qty": item_qty,
                    }
                )
            else:
                specs.append(
                    {
                        "barcode": "",
                        "sku": str(item.get("requested_article") or "").strip(),
                        "goods_type": item_goods_type,
                        "qty": item_qty,
                    }
                )
    if specs:
        return specs

    requested_qty = _as_int(payload.get("requested_qty"))
    if requested_qty > 0:
        specs.append(
            {
                "barcode": "",
                "sku": str(payload.get("requested_sku") or "").strip(),
                "goods_type": _normalize_goods_type(payload.get("requested_goods_type")),
                "qty": requested_qty,
            }
        )
    return specs


@transaction.atomic
def take_move_task(
    *,
    legacy_order_id: str,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    task = _load_task(legacy_order_id)
    if not task:
        return MoveTaskCommandResult(ok=False, error="Р—Р°РґР°РЅРёРµ РЅРµ РЅР°Р№РґРµРЅРѕ.")
    if not employee_id:
        return MoveTaskCommandResult(ok=False, error="РџСЂРѕС„РёР»СЊ СЃРѕС‚СЂСѓРґРЅРёРєР° РЅРµ РЅР°Р№РґРµРЅ.")

    payload = dict(task.payload or {})
    status = _task_payload_status(task, payload)
    assigned_to_id = _task_assignee_id(task, payload)
    if status == MoveTask.STATUS_DONE:
        return MoveTaskCommandResult(ok=False, error="Р—Р°РґР°РЅРёРµ СѓР¶Рµ РІС‹РїРѕР»РЅРµРЅРѕ.")
    if status == MoveTask.STATUS_IN_PROGRESS and assigned_to_id and assigned_to_id != employee_id:
        return MoveTaskCommandResult(ok=False, error="Р—Р°РґР°РЅРёРµ СѓР¶Рµ РІР·СЏС‚Рѕ РґСЂСѓРіРёРј РІРѕРґРёС‚РµР»РµРј.")
    if not _pallet_choice_pending(payload):
        try:
            lock_pallet_for_task(
                task,
                locked_by=user,
                payload={
                    "legacy_order_id": task.legacy_order_id,
                    "shipping_order_id": str(payload.get("shipping_order_id") or "").strip(),
                    "locked_on": "take_move_task",
                },
            )
        except ValueError as exc:
            return MoveTaskCommandResult(ok=False, error=str(exc))

    payload["status"] = MoveTask.STATUS_IN_PROGRESS
    payload["status_label"] = "\u0412 \u0440\u0430\u0431\u043e\u0442\u0435"
    payload["assigned_to_id"] = employee_id
    payload["assigned_to_name"] = employee_name
    payload["taken_at"] = timezone.localtime().isoformat()

    authenticated_user = user if getattr(user, "is_authenticated", False) else None
    agency = getattr(task.request, "agency", None)
    log_order_action(
        "status",
        order_id=task.legacy_order_id,
        order_type="stock_move",
        user=authenticated_user,
        agency=agency,
        description=f"Р—Р°РґР°РЅРёРµ {task.legacy_order_id} РІР·СЏС‚Рѕ РІ СЂР°Р±РѕС‚Сѓ",
        payload=payload,
    )
    log_stock_move(
        "update",
        user=authenticated_user,
        agency=agency,
        description=f"Р—Р°РґР°РЅРёРµ {task.legacy_order_id} РІР·СЏС‚Рѕ РІ СЂР°Р±РѕС‚Сѓ",
        snapshot={
            "move_id": task.legacy_order_id,
            "pallet_code": payload.get("pallet_code"),
            "from_location": payload.get("from_location"),
            "to_location": payload.get("to_location"),
            "from_label": payload.get("from_label"),
            "to_label": payload.get("to_label"),
            "receiving_order_id": payload.get("receiving_order_id"),
            "status": MoveTask.STATUS_IN_PROGRESS,
            "assigned_to": employee_name,
        },
    )
    sync_task_status_by_legacy_order_id(
        task.legacy_order_id,
        status=MoveTask.STATUS_IN_PROGRESS,
        assigned_to=authenticated_user,
        assigned_to_name=employee_name,
    )
    task.refresh_from_db()
    payload, _placement_payload, _placement_source, _payload_changed, _error = _ensure_mobile_runtime_payload(task, payload)
    task.payload = payload
    task.save(update_fields=["payload", "updated_at"])
    return MoveTaskCommandResult(ok=True, task=task, payload=payload)


@transaction.atomic
def take_move_request(
    *,
    legacy_order_ids,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    tasks = _load_request_tasks(legacy_order_ids)
    if not tasks:
        return MoveTaskCommandResult(ok=False, error="Р—Р°СЏРІРєР° СЂРёС‡С‚СЂР°РєР° РЅРµ РЅР°Р№РґРµРЅР°.")
    if not employee_id:
        return MoveTaskCommandResult(ok=False, error="РџСЂРѕС„РёР»СЊ СЃРѕС‚СЂСѓРґРЅРёРєР° РЅРµ РЅР°Р№РґРµРЅ.")

    authenticated_user = user if getattr(user, "is_authenticated", False) else None
    taken_count = 0
    for task in tasks:
        payload = dict(task.payload or {})
        status = _task_payload_status(task, payload)
        assigned_to_id = _task_assignee_id(task, payload)
        if status == MoveTask.STATUS_DONE:
            continue
        if status == MoveTask.STATUS_IN_PROGRESS and assigned_to_id and assigned_to_id != employee_id:
            return MoveTaskCommandResult(ok=False, error="Р§Р°СЃС‚СЊ РїР°Р»Р»РµС‚ Р·Р°СЏРІРєРё СѓР¶Рµ РІР·СЏС‚Р° РґСЂСѓРіРёРј РІРѕРґРёС‚РµР»РµРј.")
        if not _pallet_choice_pending(payload):
            try:
                lock_pallet_for_task(
                    task,
                    locked_by=user,
                    payload={
                        "legacy_order_id": task.legacy_order_id,
                        "shipping_order_id": str(payload.get("shipping_order_id") or "").strip(),
                        "locked_on": "take_move_request",
                    },
                )
            except ValueError as exc:
                transaction.set_rollback(True)
                return MoveTaskCommandResult(ok=False, error=str(exc))

    for task in tasks:
        payload = dict(task.payload or {})
        status = _task_payload_status(task, payload)
        assigned_to_id = _task_assignee_id(task, payload)
        if status == MoveTask.STATUS_DONE:
            continue
        execution = dict(payload.get("mobile_execution") or {})
        if status != MoveTask.STATUS_IN_PROGRESS or assigned_to_id != employee_id:
            payload["status"] = MoveTask.STATUS_IN_PROGRESS
            payload["status_label"] = "\u0412 \u0440\u0430\u0431\u043e\u0442\u0435"
            payload["assigned_to_id"] = employee_id
            payload["assigned_to_name"] = employee_name
            payload["taken_at"] = timezone.localtime().isoformat()
            task.status = MoveTask.STATUS_IN_PROGRESS
            task.assigned_to = authenticated_user
            task.assigned_to_name = employee_name
            task.started_at = task.started_at or timezone.now()
            taken_count += 1
        payload["mobile_request_batch_mode"] = True
        execution.setdefault("pallet_confirmed", False)
        execution.setdefault("destination_confirmed", False)
        payload["mobile_execution"] = execution
        task.save(
            update_fields=[
                "status",
                "assigned_to",
                "assigned_to_name",
                "started_at",
                "updated_at",
            ]
        )
        sync_task_status_by_legacy_order_id(
            task.legacy_order_id,
            status=MoveTask.STATUS_IN_PROGRESS,
            assigned_to=authenticated_user,
            assigned_to_name=employee_name,
        )
        task.refresh_from_db()
        payload, _placement_payload, _placement_source, _payload_changed, _error = _ensure_mobile_runtime_payload(task, payload)
        task.payload = payload
        task.started_at = task.started_at or timezone.now()
        task.save(update_fields=["payload", "started_at", "updated_at"])
        agency = getattr(task.request, "agency", None)
        log_order_action(
            "status",
            order_id=task.legacy_order_id,
            order_type="stock_move",
            user=authenticated_user,
            agency=agency,
            description=f"Р—Р°РґР°РЅРёРµ {task.legacy_order_id} РІР·СЏС‚Рѕ РІ СЂР°Р±РѕС‚Сѓ РІ СЃРѕСЃС‚Р°РІРµ Р·Р°СЏРІРєРё",
            payload=payload,
        )
        log_stock_move(
            "update",
            user=authenticated_user,
            agency=agency,
            description=f"Р—Р°РґР°РЅРёРµ {task.legacy_order_id} РІР·СЏС‚Рѕ РІ СЂР°Р±РѕС‚Сѓ РІ СЃРѕСЃС‚Р°РІРµ Р·Р°СЏРІРєРё",
            snapshot={
                "move_id": task.legacy_order_id,
                "pallet_code": payload.get("pallet_code"),
                "from_location": payload.get("from_location"),
                "to_location": payload.get("to_location"),
                "from_label": payload.get("from_label"),
                "to_label": payload.get("to_label"),
                "status": MoveTask.STATUS_IN_PROGRESS,
                "assigned_to": employee_name,
                "request_batch_mode": True,
            },
        )
    message = "Р—Р°СЏРІРєР° РІР·СЏС‚Р° РІ СЂР°Р±РѕС‚Сѓ." if taken_count else "Р—Р°СЏРІРєР° СѓР¶Рµ РІ СЂР°Р±РѕС‚Рµ."
    return MoveTaskCommandResult(ok=True, message=message)


@transaction.atomic
def scan_move_request_step(
    *,
    legacy_order_ids,
    scan_value: str,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    tasks = _load_request_tasks(legacy_order_ids)
    if not tasks:
        return MoveTaskCommandResult(ok=False, error="Р—Р°СЏРІРєР° СЂРёС‡С‚СЂР°РєР° РЅРµ РЅР°Р№РґРµРЅР°.")
    if not employee_id:
        return MoveTaskCommandResult(ok=False, error="РџСЂРѕС„РёР»СЊ СЃРѕС‚СЂСѓРґРЅРёРєР° РЅРµ РЅР°Р№РґРµРЅ.")

    scan_code = str(scan_value or "").strip()
    if not scan_code:
        return MoveTaskCommandResult(ok=False, error="РћС‚СЃРєР°РЅРёСЂСѓР№С‚Рµ РєРѕРґ.")

    snapshot = build_mobile_request_execution_snapshot(legacy_order_ids, employee_id=employee_id)
    if not snapshot:
        return MoveTaskCommandResult(ok=False, error="РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕРґРіРѕС‚РѕРІРёС‚СЊ Р·Р°СЏРІРєСѓ Рє СЃРєР°РЅРёСЂРѕРІР°РЅРёСЋ.")
    active_order_id = str(snapshot.get("active_order_id") or "").strip()
    active_destination_override_pending = bool(snapshot.get("destination_override_pending"))
    duplicate_destination_result = (
        None
        if active_destination_override_pending
        else _request_duplicate_destination_result(tasks, scan_code)
    )
    if not snapshot["can_scan"]:
        if duplicate_destination_result:
            return duplicate_destination_result
        return MoveTaskCommandResult(ok=False, error="РЎРЅР°С‡Р°Р»Р° РІРѕР·СЊРјРёС‚Рµ РІСЃСЋ Р·Р°СЏРІРєСѓ РІ СЂР°Р±РѕС‚Сѓ.")

    task_map = {str(task.legacy_order_id or "").strip(): task for task in tasks}
    authenticated_user = user if getattr(user, "is_authenticated", False) else None
    if active_order_id:
        active_task = task_map.get(active_order_id)
        if not active_task:
            return MoveTaskCommandResult(ok=False, error="РђРєС‚РёРІРЅР°СЏ РїР°Р»Р»РµС‚Р° Р·Р°СЏРІРєРё РЅРµ РЅР°Р№РґРµРЅР°.")
        active_payload = dict(active_task.payload or {})
        expected_code = str(snapshot.get("active_destination_code") or "").strip()
        active_pallet = _task_pallet_display(active_payload, fallback=scan_code)
        source_code = _task_source_code(active_payload)
        if active_destination_override_pending:
            if _same_pallet_code_scan(scan_code, active_payload.get("pallet_code")):
                return MoveTaskCommandResult(
                    ok=False,
                    error=(
                        f"РџР°Р»Р»РµС‚Р° {active_pallet} СѓР¶Рµ РїРѕРґС‚РІРµСЂР¶РґРµРЅР°. "
                        "РЎРµР№С‡Р°СЃ Р¶РґРµРј РЅРѕРІРѕРµ РјРµСЃС‚Рѕ РЅР°Р·РЅР°С‡РµРЅРёСЏ."
                    ),
                )
            if _same_location_scan(scan_code, active_payload.get("from_location") or {}):
                return MoveTaskCommandResult(
                    ok=False,
                    error=(
                        f"Р’С‹ СЃРЅРѕРІР° РѕС‚СЃРєР°РЅРёСЂРѕРІР°Р»Рё РёСЃС…РѕРґРЅРѕРµ РјРµСЃС‚Рѕ {source_code}. "
                        "РЎРµР№С‡Р°СЃ Р¶РґРµРј РЅРѕРІРѕРµ РјРµСЃС‚Рѕ РЅР°Р·РЅР°С‡РµРЅРёСЏ."
                    ),
                )
            new_destination = _destination_from_scan_value(scan_code)
            if not new_destination:
                return MoveTaskCommandResult(
                    ok=False,
                    error=(
                        f"РњРµСЃС‚Рѕ {display_scan_text(scan_code)} РЅРµ СЂР°СЃРїРѕР·РЅР°РЅРѕ. "
                        "РћС‚СЃРєР°РЅРёСЂСѓР№С‚Рµ РєРѕСЂСЂРµРєС‚РЅС‹Р№ Р°РґСЂРµСЃ СЃРєР»Р°РґР°."
                    ),
                )
            execution = dict(active_payload.get("mobile_execution") or {})
            current_destination = (
                execution.get("destination_override_candidate")
                if isinstance(execution.get("destination_override_candidate"), dict)
                else {}
            )
            if current_destination and (
                _same_location_scan(scan_code, current_destination)
                or _location_scan_code(new_destination) == _location_scan_code(current_destination)
            ):
                return confirm_move_request_destination_override(
                    legacy_order_ids,
                    user=user,
                    employee_id=employee_id,
                    employee_name=employee_name,
                )
            return _store_request_destination_override_candidate(
                task=active_task,
                destination=new_destination,
                scan_code=scan_code,
            )
        if _same_pallet_code_scan(scan_code, active_payload.get("pallet_code")):
            return MoveTaskCommandResult(
                ok=False,
                error=(
                    f"РџР°Р»Р»РµС‚Р° {active_pallet} СѓР¶Рµ РїРѕРґС‚РІРµСЂР¶РґРµРЅР°. "
                    f"РўРµРїРµСЂСЊ РѕС‚СЃРєР°РЅРёСЂСѓР№С‚Рµ РјРµСЃС‚Рѕ {expected_code}."
                ),
            )
        if _same_location_scan(scan_code, active_payload.get("from_location") or {}):
            return MoveTaskCommandResult(
                ok=False,
                error=(
                    f"Р’С‹ СЃРЅРѕРІР° РѕС‚СЃРєР°РЅРёСЂРѕРІР°Р»Рё РёСЃС…РѕРґРЅРѕРµ РјРµСЃС‚Рѕ {source_code}. "
                    f"Р”Р»СЏ РїР°Р»Р»РµС‚С‹ {active_pallet} РЅСѓР¶РЅРѕ РјРµСЃС‚Рѕ {expected_code}."
                ),
            )
        if not _same_location_scan(scan_code, active_payload.get("to_location") or {}):
            return MoveTaskCommandResult(
                ok=False,
                error=(
                    f"РњРµСЃС‚Рѕ {display_scan_text(scan_code)} РЅРµ РїРѕРґС…РѕРґРёС‚ РґР»СЏ РїР°Р»Р»РµС‚С‹ {active_pallet}. "
                    f"РћР¶РёРґР°РµС‚СЃСЏ РјРµСЃС‚Рѕ {expected_code}."
                ),
            )
        payload = active_payload
        execution = dict(payload.get("mobile_execution") or {})
        execution["destination_confirmed"] = True
        execution["destination_override_pending"] = False
        execution["last_scan"] = scan_code
        payload["mobile_execution"] = execution
        active_task.payload = payload
        active_task.save(update_fields=["payload", "updated_at"])
        complete_result = complete_move_task(
            legacy_order_id=active_order_id,
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
        )
        if not complete_result.ok:
            return complete_result
        for task in tasks:
            task.refresh_from_db(fields=["status", "payload", "updated_at"])
        remaining_count = sum(1 for task in tasks if task.status != MoveTask.STATUS_DONE)
        if remaining_count <= 0:
            return MoveTaskCommandResult(
                ok=True,
                message="РњРµСЃС‚Рѕ РїРѕРґС‚РІРµСЂР¶РґРµРЅРѕ. Р’СЃРµ РїР°Р»Р»РµС‚С‹ РїРѕ Р·Р°СЏРІРєРµ РґРѕСЃС‚Р°РІР»РµРЅС‹.",
                completed=True,
            )
        return MoveTaskCommandResult(
            ok=True,
            message="РњРµСЃС‚Рѕ РїРѕРґС‚РІРµСЂР¶РґРµРЅРѕ. РџР°Р»Р»РµС‚Р° РґРѕСЃС‚Р°РІР»РµРЅР°, СЃРєР°РЅРёСЂСѓР№С‚Рµ СЃР»РµРґСѓСЋС‰СѓСЋ.",
            completed=False,
        )

    if duplicate_destination_result:
        return duplicate_destination_result

    selected_pallet_codes = {
        str((task.payload or {}).get("pallet_code") or task.pallet_code or "").strip().lower()
        for task in tasks
        if str((task.payload or {}).get("pallet_code") or task.pallet_code or "").strip()
    }
    for task in tasks:
        payload = dict(task.payload or {})
        status = _task_payload_status(task, payload)
        assigned_to_id = _task_assignee_id(task, payload)
        if status != MoveTask.STATUS_IN_PROGRESS:
            continue
        if assigned_to_id and assigned_to_id != employee_id:
            continue
        if not _pallet_choice_pending(payload):
            continue
        for option in _candidate_pallet_options(payload):
            if _same_location_scan(scan_code, option.get("from_location") or {}):
                source_code = _location_scan_code(option.get("from_location") or {})
                return MoveTaskCommandResult(
                    ok=True,
                    task=task,
                    payload=payload,
                    message=f"\u0410\u0434\u0440\u0435\u0441 {source_code} \u043f\u043e\u043a\u0430\u0437\u0430\u043d \u043d\u0430 \u044d\u043a\u0440\u0430\u043d\u0435. \u0422\u0435\u043f\u0435\u0440\u044c \u043e\u0442\u0441\u043a\u0430\u043d\u0438\u0440\u0443\u0439\u0442\u0435 QR \u043f\u043e\u0434\u0445\u043e\u0434\u044f\u0449\u0435\u0439 \u043f\u0430\u043b\u043b\u0435\u0442\u044b.",
                )
        option = _candidate_pallet_for_scan(payload, scan_code)
        if not option:
            continue
        option_code = str(option.get("pallet_code") or "").strip().lower()
        if option_code and option_code in selected_pallet_codes:
            continue
        return _bind_flexible_pallet_choice(
            task=task,
            payload=payload,
            option=option,
            scan_code=scan_code,
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
        )

    remaining_pallets = _request_remaining_pallet_labels(tasks)
    candidate = None
    for task in tasks:
        payload = dict(task.payload or {})
        status = _task_payload_status(task, payload)
        assigned_to_id = _task_assignee_id(task, payload)
        if status != MoveTask.STATUS_IN_PROGRESS:
            continue
        if assigned_to_id and assigned_to_id != employee_id:
            continue
        if not _same_pallet_code_scan(scan_code, payload.get("pallet_code")):
            continue
        candidate = task
        break
    if not candidate:
        for task in tasks:
            payload = dict(task.payload or {})
            pallet_display = _task_pallet_display(payload, fallback=scan_code)
            destination_code = _task_destination_code(payload)
            status = _task_payload_status(task, payload)
            execution = dict(payload.get("mobile_execution") or {})
            if _same_pallet_code_scan(scan_code, payload.get("pallet_code")):
                if status == MoveTask.STATUS_DONE or execution.get("destination_confirmed"):
                    return MoveTaskCommandResult(
                        ok=False,
                        error=f"РџР°Р»Р»РµС‚Р° {pallet_display} СѓР¶Рµ РґРѕСЃС‚Р°РІР»РµРЅР° РІ {destination_code}.",
                    )
                if execution.get("pallet_confirmed"):
                    return MoveTaskCommandResult(
                        ok=False,
                        error=(
                            f"РџР°Р»Р»РµС‚Р° {pallet_display} СѓР¶Рµ РїРѕРґС‚РІРµСЂР¶РґРµРЅР°. "
                            f"РўРµРїРµСЂСЊ РѕС‚СЃРєР°РЅРёСЂСѓР№С‚Рµ РјРµСЃС‚Рѕ {destination_code}."
                        ),
                    )
            if _same_location_scan(scan_code, payload.get("to_location") or {}):
                return MoveTaskCommandResult(
                    ok=False,
                    error=(
                        f"РћС‚СЃРєР°РЅРёСЂРѕРІР°РЅРѕ РјРµСЃС‚Рѕ {destination_code}, РЅРѕ СЃРµР№С‡Р°СЃ РЅСѓР¶РЅРѕ СЃРєР°РЅРёСЂРѕРІР°С‚СЊ РїР°Р»Р»РµС‚Сѓ. "
                        f"РћСЃС‚Р°Р»РѕСЃСЊ РїРµСЂРµРІРµР·С‚Рё: {', '.join(remaining_pallets) or 'РЅРµС‚ РїР°Р»Р»РµС‚'}."
                    ),
                )
            if _same_location_scan(scan_code, payload.get("from_location") or {}):
                return MoveTaskCommandResult(
                    ok=False,
                    error=(
                        "РћС‚СЃРєР°РЅРёСЂРѕРІР°РЅРѕ РјРµСЃС‚Рѕ РѕС‚Р±РѕСЂР°, РЅРѕ СЃРµР№С‡Р°СЃ РЅСѓР¶РЅРѕ СЃРєР°РЅРёСЂРѕРІР°С‚СЊ РїР°Р»Р»РµС‚Сѓ. "
                        f"РћСЃС‚Р°Р»РѕСЃСЊ РїРµСЂРµРІРµР·С‚Рё: {', '.join(remaining_pallets) or 'РЅРµС‚ РїР°Р»Р»РµС‚'}."
                    ),
                )
        return MoveTaskCommandResult(
            ok=False,
            error=(
                f"РџР°Р»Р»РµС‚Р° {display_scan_text(scan_code)} РЅРµ РѕС‚РЅРѕСЃРёС‚СЃСЏ Рє СЌС‚РѕР№ Р·Р°СЏРІРєРµ. "
                f"РћСЃС‚Р°Р»РѕСЃСЊ РїРµСЂРµРІРµР·С‚Рё: {', '.join(remaining_pallets) or 'РЅРµС‚ РїР°Р»Р»РµС‚'}."
            ),
        )

    payload = dict(candidate.payload or {})
    execution = dict(payload.get("mobile_execution") or {})
    execution["source_confirmed"] = True
    execution["pallet_confirmed"] = True
    execution["destination_override_pending"] = False
    execution["last_scan"] = scan_code
    payload["mobile_request_batch_mode"] = True
    payload["mobile_execution"] = execution
    candidate.payload = payload
    candidate.save(update_fields=["payload", "updated_at"])
    pallet_code = display_scan_text(payload.get("pallet_code") or scan_code)
    destination_code = _location_scan_code(payload.get("to_location") or {})
    agency = getattr(candidate.request, "agency", None)
    log_stock_move(
        "update",
        user=authenticated_user,
        agency=agency,
        description=f"Р”Р»СЏ Р·Р°СЏРІРєРё РІС‹Р±СЂР°РЅР° РїР°Р»Р»РµС‚Р° {payload.get('pallet_code')}",
        snapshot={
            "move_id": candidate.legacy_order_id,
            "pallet_code": payload.get("pallet_code"),
            "to_label": payload.get("to_label"),
            "request_batch_mode": True,
        },
    )
    return MoveTaskCommandResult(
        ok=True,
        task=candidate,
        payload=payload,
        message=f"РџР°Р»Р»РµС‚Р° {pallet_code} РїРѕРґС‚РІРµСЂР¶РґРµРЅР°. РћС‚РІРµР·Рё -> {destination_code}.",
        completed=False,
    )


@transaction.atomic
def complete_move_task(
    *,
    legacy_order_id: str,
    user,
    employee_id: int | None,
    employee_name: str,
    require_scan_confirmation: bool = True,
    desktop_selected_boxes: list[str] | None = None,
) -> MoveTaskCommandResult:
    task = _load_task(legacy_order_id)
    if not task:
        return MoveTaskCommandResult(ok=False, error="Р—Р°РґР°РЅРёРµ РЅРµ РЅР°Р№РґРµРЅРѕ.")
    if not employee_id:
        return MoveTaskCommandResult(ok=False, error="РџСЂРѕС„РёР»СЊ СЃРѕС‚СЂСѓРґРЅРёРєР° РЅРµ РЅР°Р№РґРµРЅ.")

    payload = dict(task.payload or {})
    status = _task_payload_status(task, payload)
    assigned_to_id = _task_assignee_id(task, payload)
    status, assigned_to_id, payload = _promote_created_assigned_task(
        task,
        payload,
        status=status,
        assigned_to_id=assigned_to_id,
        user=user,
        employee_id=employee_id,
        employee_name=employee_name,
    )
    if status != MoveTask.STATUS_IN_PROGRESS:
        return MoveTaskCommandResult(ok=False, error="Р—Р°РґР°РЅРёРµ РµС‰Рµ РЅРµ РІР·СЏС‚Рѕ РІ СЂР°Р±РѕС‚Сѓ.")
    if assigned_to_id and assigned_to_id != employee_id:
        return MoveTaskCommandResult(ok=False, error="Р—Р°РґР°РЅРёРµ РЅР°Р·РЅР°С‡РµРЅРѕ РґСЂСѓРіРѕРјСѓ РІРѕРґРёС‚РµР»СЋ.")

    pallet_code = str(payload.get("pallet_code") or "").strip()
    pallet_code_display = display_scan_text(pallet_code)
    if not pallet_code:
        return MoveTaskCommandResult(ok=False, error="РќРµ РЅР°Р№РґРµРЅ РєРѕРґ РїР°Р»Р»РµС‚С‹ РІ Р·Р°РґР°РЅРёРё.")

    preloaded_otg_delivered_boxes = otg_already_delivered_box_codes(payload, task.request.agency)
    payload, placement_payload, placement_source, _payload_changed, error = _ensure_mobile_runtime_payload(task, payload)
    if error:
        if preloaded_otg_delivered_boxes:
            placement_payload = {"act": "placement", "act_state": "closed", "act_pallets": [], "act_boxes": []}
            placement_source = {
                "order_id": str(payload.get("receiving_order_id") or payload.get("shipping_order_id") or task.legacy_order_id),
                "order_type": "receiving" if payload.get("receiving_order_id") else "stock_move",
                "agency_id": int(task.request.agency_id) if task.request and task.request.agency_id else 0,
            }
        else:
            return MoveTaskCommandResult(ok=False, error=error)
    authenticated_user = user if getattr(user, "is_authenticated", False) else None
    placement_order_id = str(placement_source.get("order_id") or "").strip()
    placement_order_type = str(placement_source.get("order_type") or "").strip() or "receiving"
    placement_agency = getattr(task.request, "agency", None)
    pallets = placement_payload.get("act_pallets") or []
    boxes = placement_payload.get("act_boxes") or []
    to_location = payload.get("to_location") or {}
    to_zone = _normalize_zone_code(to_location.get("zone") or "")
    pick_mode = str(payload.get("pick_mode") or "full").strip().lower()
    move_mode = _normalize_move_mode(payload.get("move_mode"), pick_mode)
    placement_description = f"РџРµСЂРµРјРµС‰РµРЅРёРµ РїР°Р»Р»РµС‚С‹ {pallet_code}"
    done_status_label = "РџРµСЂРµРјРµС‰РµРЅРѕ"
    shipping_reserve_rebind: dict | None = None
    skip_shipping_otg_operation = False
    shipping_arrived_box_codes: list[str] = []
    processing_arrived_box_codes: list[str] = []
    processing_partial_pick_rows: list[dict] = []
    processing_direct_arrival_pending = False

    if to_zone == "OTG":
        from_location = payload.get("from_location") or {}
        return_location = _build_location(
            from_location.get("zone"),
            _as_int(from_location.get("row")),
            _as_int(from_location.get("section")),
            _as_int(from_location.get("tier")),
            _as_int(from_location.get("cell")),
        )
        otg_location = _build_location(
            to_location.get("zone") or "OTG",
            _as_int(to_location.get("row")),
            _as_int(to_location.get("section")),
            _as_int(to_location.get("tier")),
            _as_int(to_location.get("cell")),
        )
        already_delivered_boxes = preloaded_otg_delivered_boxes or otg_already_delivered_box_codes(
            payload,
            task.request.agency,
        )
        loose_picked_rows: list[dict] = []
        full_box_pick_rows: list[dict] = []
        if (
            move_mode == MOVE_MODE_BOX_PARTIAL
            and bool(payload.get("ship_as_loose_units"))
            and not already_delivered_boxes
        ):
            whole_box_codes: list[str] = []
            if _is_any_matching_box_selection(payload):
                requested_boxes, resolve_error = _resolve_any_matching_boxes_for_completion(
                    payload,
                    placement_payload,
                    pallet_code,
                    require_scan_confirmation=require_scan_confirmation,
                    desktop_selected_boxes=desktop_selected_boxes,
                )
                if resolve_error:
                    return MoveTaskCommandResult(ok=False, error=resolve_error)
                if (
                    _requested_box_selection_mode(payload) == BOX_SELECTION_PATTERN_MATCHING
                    and _partial_pick_patterns(payload)
                    and _requested_box_count(payload) > len(_expand_partial_pick_patterns(payload))
                ):
                    whole_box_codes, requested_rows = _split_mixed_partial_box_selection(
                        payload,
                        placement_payload,
                        pallet_code,
                        requested_boxes,
                    )
                else:
                    requested_rows = _partial_pick_rows_for_boxes(
                        payload,
                        placement_payload,
                        pallet_code,
                        requested_boxes,
                    )
            else:
                requested_rows = _mobile_plan_partial_rows(payload, placement_payload, pallet_code)
                requested_boxes = [
                    _normalize_box_code(row.get("box_code"))
                    for row in requested_rows
                    if _normalize_box_code(row.get("box_code"))
                ]
            if not requested_rows:
                return MoveTaskCommandResult(ok=False, error="РќРµ СѓРґР°Р»РѕСЃСЊ РѕРїСЂРµРґРµР»РёС‚СЊ СЃС‚СЂРѕРєРё С‡Р°СЃС‚РёС‡РЅРѕРіРѕ РѕС‚Р±РѕСЂР°.")
            try:
                if whole_box_codes:
                    claim_boxes_for_task(
                        task,
                        whole_box_codes,
                        claimed_by=user,
                        claim_kind=BoxClaim.KIND_BOX,
                        payload={
                            "legacy_order_id": task.legacy_order_id,
                            "shipping_order_id": str(payload.get("shipping_order_id") or "").strip(),
                            "move_mode": move_mode,
                        },
                    )
                partial_box_codes = [
                    _normalize_box_code(row.get("box_code"))
                    for row in requested_rows
                    if _normalize_box_code(row.get("box_code"))
                ]
                claim_boxes_for_task(
                    task,
                    partial_box_codes,
                    claimed_by=user,
                    claim_kind=BoxClaim.KIND_PARTIAL,
                    payload={
                        "legacy_order_id": task.legacy_order_id,
                        "shipping_order_id": str(payload.get("shipping_order_id") or "").strip(),
                        "move_mode": move_mode,
                    },
                )
            except ValueError as exc:
                return MoveTaskCommandResult(ok=False, error=str(exc))
            reserved_box_codes = [
                str(code).strip()
                for code in (payload.get("reserved_box_codes") or [])
                if str(code or "").strip()
            ]
            shipping_order_id = str(payload.get("shipping_order_id") or "").strip()
            if shipping_order_id and reserved_box_codes and requested_boxes:
                try:
                    WarehouseWritePathService.rebind_shipping_reserve_boxes(
                        agency=task.request.agency,
                        order_id=shipping_order_id,
                        reserved_box_codes=reserved_box_codes,
                        actual_box_codes=list(requested_boxes),
                        performed_by=authenticated_user,
                        source_document_id=shipping_order_id,
                    )
                except ValueError as exc:
                    return MoveTaskCommandResult(ok=False, error=str(exc))
            for code in whole_box_codes:
                box_qty, _barcode_qty = _placement_box_signature(placement_payload, pallet_code, code)
                full_box_pick_rows.append({"box_code": code, "qty": _as_int(box_qty)})
            picked_qty = 0
            for row_plan in requested_rows:
                row_box = _normalize_box_code(row_plan.get("box_code"))
                row_qty = _as_int(row_plan.get("qty"))
                row_barcode_qty = _normalize_barcode_qty_map(row_plan.get("barcode_qty"))
                if not row_box or row_qty <= 0:
                    return MoveTaskCommandResult(ok=False, error="РќРµРєРѕСЂСЂРµРєС‚РЅР°СЏ СЃС‚СЂРѕРєР° С‡Р°СЃС‚РёС‡РЅРѕРіРѕ РѕС‚Р±РѕСЂР°.")
                row_picked = 0
                if row_barcode_qty:
                    for barcode, barcode_qty in row_barcode_qty.items():
                        consumed_ok, consume_error, picked_part = _consume_box_qty(
                            placement_payload,
                            pallet_code,
                            row_box,
                            barcode_qty,
                            {barcode},
                            set(),
                            set(),
                        )
                        if not consumed_ok:
                            return MoveTaskCommandResult(
                                ok=False,
                                error=consume_error or f"РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РѕР±СЂР°С‚СЊ С‚РѕРІР°СЂ {barcode}.",
                            )
                        row_picked += picked_part
                else:
                    barcode_values, sku_values, goods_type_values, _requested_qty = _mobile_selector_sets(payload)
                    consumed_ok, consume_error, row_picked = _consume_box_qty(
                        placement_payload,
                        pallet_code,
                        row_box,
                        row_qty,
                        barcode_values,
                        sku_values,
                        goods_type_values,
                    )
                    if not consumed_ok:
                        return MoveTaskCommandResult(ok=False, error=consume_error or "РќРµ СѓРґР°Р»РѕСЃСЊ РІС‹РїРѕР»РЅРёС‚СЊ РѕС‚Р±РѕСЂ.")
                picked_qty += row_picked
                loose_picked_rows.append(
                    {
                        "box_code": row_box,
                        "qty": row_qty,
                        "picked_qty": row_picked,
                        "barcode_qty": row_barcode_qty,
                    }
                )
            try:
                WarehouseWritePathService.complete_partial_shipping_pick_to_otg(
                    agency=task.request.agency,
                    order_id=_shipping_order_key_from_payload(payload),
                    source_pallet_code=pallet_code,
                    picked_rows=loose_picked_rows,
                    destination_row_no=_as_int(otg_location.get("row")),
                    destination_section_no=_as_int(otg_location.get("section")),
                    destination_tier_no=_as_int(otg_location.get("tier")),
                    destination_cell_no=_as_int(otg_location.get("cell")),
                    destination_location_code=str(otg_location.get("code") or "").strip(),
                    allow_legacy_generic_destination=not bool(
                        payload.get("concrete_location_required")
                    ),
                    performed_by=authenticated_user,
                )
            except ValueError as exc:
                return MoveTaskCommandResult(ok=False, error=str(exc))
            if whole_box_codes:
                moved_ok, moved_error, moved_meta = _move_boxes_to_otg(
                    placement_payload,
                    pallet_code,
                    whole_box_codes,
                    otg_location=otg_location,
                    return_location=return_location,
                )
                if not moved_ok:
                    return MoveTaskCommandResult(
                        ok=False,
                        error=moved_error or "РќРµ СѓРґР°Р»РѕСЃСЊ РґРѕСЃС‚Р°РІРёС‚СЊ РєРѕСЂРѕР±Р° РІ OTG.",
                    )
            else:
                pallets_after = placement_payload.get("act_pallets") or []
                source_pallet_exists = any(
                    isinstance(item, dict) and str(item.get("code") or "").strip() == pallet_code
                    for item in pallets_after
                )
                moved_meta = {
                    "moved_boxes": [],
                    "pallet_deleted": not source_pallet_exists,
                    "pallet_returned": source_pallet_exists,
                }
            moved_boxes = list(moved_meta.get("moved_boxes") or [])
            moved_meta["loose_units"] = True
            payload["picked_loose_units"] = True
            skip_shipping_otg_operation = not bool(moved_boxes)
        elif already_delivered_boxes:
            moved_boxes = already_delivered_boxes
            moved_meta = {
                "moved_boxes": moved_boxes,
                "pallet_deleted": False,
                "pallet_returned": False,
                "already_in_otg": True,
            }
        else:
            if _is_any_matching_box_selection(payload):
                requested_boxes, resolve_error = _resolve_any_matching_boxes_for_completion(
                    payload,
                    placement_payload,
                    pallet_code,
                    require_scan_confirmation=require_scan_confirmation,
                    desktop_selected_boxes=desktop_selected_boxes,
                )
            else:
                requested_boxes, resolve_error = _resolve_otg_box_codes(
                    placement_payload,
                    pallet_code,
                    payload,
                )
            if resolve_error:
                return MoveTaskCommandResult(ok=False, error=resolve_error)
            try:
                claim_boxes_for_task(
                    task,
                    requested_boxes,
                    claimed_by=user,
                    claim_kind=BoxClaim.KIND_BOX,
                    payload={
                        "legacy_order_id": task.legacy_order_id,
                        "shipping_order_id": str(payload.get("shipping_order_id") or "").strip(),
                        "move_mode": move_mode,
                    },
                )
            except ValueError as exc:
                return MoveTaskCommandResult(ok=False, error=str(exc))
            moved_ok, moved_error, moved_meta = _move_boxes_to_otg(
                placement_payload,
                pallet_code,
                requested_boxes,
                otg_location=otg_location,
                return_location=return_location,
            )
            if not moved_ok:
                return MoveTaskCommandResult(
                    ok=False,
                    error=moved_error or "РќРµ СѓРґР°Р»РѕСЃСЊ РґРѕСЃС‚Р°РІРёС‚СЊ РєРѕСЂРѕР±Р° РІ OTG.",
                )
            moved_boxes = moved_meta.get("moved_boxes") or []
        payload["picked_boxes"] = moved_boxes
        requested_qty_value = _as_int(payload.get("requested_qty"))
        if loose_picked_rows:
            loose_qty = sum(_as_int(row.get("picked_qty") or row.get("qty")) for row in loose_picked_rows)
            full_box_qty = sum(_as_int(row.get("qty")) for row in full_box_pick_rows)
            payload["picked_qty"] = full_box_qty + loose_qty
            payload["picked_rows"] = [*full_box_pick_rows, *loose_picked_rows]
        else:
            payload["picked_qty"] = requested_qty_value if requested_qty_value > 0 else len(moved_boxes)
            payload["picked_rows"] = [{"box_code": code} for code in moved_boxes]
        payload["source_pallet_deleted"] = bool(moved_meta.get("pallet_deleted"))
        payload["source_pallet_returned"] = bool(moved_meta.get("pallet_returned"))
        placement_payload["act_items_removed"] = True
        shipping_order_id = str(payload.get("shipping_order_id") or "").strip()
        reserved_box_codes = [
            str(code).strip()
            for code in (payload.get("reserved_box_codes") or [])
            if str(code or "").strip()
        ]
        if shipping_order_id and reserved_box_codes and not moved_meta.get("already_in_otg"):
            shipping_reserve_rebind = {
                "shipping_order_id": shipping_order_id,
                "reserved_box_codes": reserved_box_codes,
                "actual_box_codes": list(moved_boxes),
            }
        if shipping_order_id and (skip_shipping_otg_operation or moved_meta.get("already_in_otg")):
            shipping_arrived_box_codes = list(moved_boxes)
        if moved_meta.get("loose_units") and moved_boxes:
            placement_description = (
                f"РљРѕСЂРѕР±Р° ({', '.join(moved_boxes)}) Рё С€С‚СѓС‡РЅС‹Р№ С‚РѕРІР°СЂ "
                f"{sum(_as_int(row.get('picked_qty') or row.get('qty')) for row in loose_picked_rows)} С€С‚. "
                f"РґРѕСЃС‚Р°РІР»РµРЅС‹ РІ OTG; РїР°Р»Р»РµС‚Р° {pallet_code} РІРѕР·РІСЂР°С‰РµРЅР° РЅР° РёСЃС…РѕРґРЅРѕРµ РјРµСЃС‚Рѕ"
            )
            done_status_label = "РљРѕСЂРѕР±Р° Рё С€С‚СѓС‡РЅС‹Р№ С‚РѕРІР°СЂ РґРѕСЃС‚Р°РІР»РµРЅС‹ РІ OTG"
        elif moved_meta.get("loose_units"):
            placement_description = (
                f"Р§Р°СЃС‚РёС‡РЅС‹Р№ РѕС‚Р±РѕСЂ {payload.get('picked_qty') or 0} С€С‚. РёР· РєРѕСЂРѕР±РѕРІ "
                f"({', '.join(moved_boxes)}) РґРѕСЃС‚Р°РІР»РµРЅ РІ OTG; РїР°Р»Р»РµС‚Р° {pallet_code} РІРѕР·РІСЂР°С‰РµРЅР° РЅР° РёСЃС…РѕРґРЅРѕРµ РјРµСЃС‚Рѕ"
            )
            done_status_label = "РЁС‚СѓС‡РЅС‹Р№ С‚РѕРІР°СЂ РґРѕСЃС‚Р°РІР»РµРЅ РІ OTG"
        elif moved_meta.get("already_in_otg"):
            placement_description = (
                f"РљРѕСЂРѕР±Р° ({', '.join(moved_boxes)}) СѓР¶Рµ РЅР°С…РѕРґСЏС‚СЃСЏ РІ OTG; "
                f"Р·Р°РґР°С‡Р° СЂРёС‡С‚СЂР°РєР° Р·Р°РєСЂС‹С‚Р° Р±РµР· РїРѕРІС‚РѕСЂРЅРѕРіРѕ РїРµСЂРµРјРµС‰РµРЅРёСЏ"
            )
            done_status_label = "РљРѕСЂРѕР±Р° СѓР¶Рµ РІ OTG"
        elif moved_meta.get("pallet_deleted"):
            placement_description = (
                f"РљРѕСЂРѕР±Р° ({', '.join(moved_boxes)}) РґРѕСЃС‚Р°РІР»РµРЅС‹ РІ OTG; "
                f"РёСЃС…РѕРґРЅР°СЏ РїР°Р»Р»РµС‚Р° {pallet_code} РїСѓСЃС‚Р° Рё СѓРґР°Р»РµРЅР°"
            )
            done_status_label = "РљРѕСЂРѕР±Р° РґРѕСЃС‚Р°РІР»РµРЅС‹ РІ OTG, РїР°Р»Р»РµС‚Р° СѓРґР°Р»РµРЅР°"
        else:
            placement_description = (
                f"РљРѕСЂРѕР±Р° ({', '.join(moved_boxes)}) РґРѕСЃС‚Р°РІР»РµРЅС‹ РІ OTG; "
                f"РїР°Р»Р»РµС‚Р° {pallet_code} СЃ РѕСЃС‚Р°С‚РєРѕРј РІРѕР·РІСЂР°С‰РµРЅР° РЅР° РёСЃС…РѕРґРЅРѕРµ РјРµСЃС‚Рѕ"
            )
            done_status_label = "РљРѕСЂРѕР±Р° РґРѕСЃС‚Р°РІР»РµРЅС‹ РІ OTG, РїР°Р»Р»РµС‚Р° РІРѕР·РІСЂР°С‰РµРЅР°"
    elif to_zone == "OBR" and move_mode == MOVE_MODE_BOX_PARTIAL:
        requested_qty = _as_int(payload.get("requested_qty"))
        requested_sku = str(payload.get("requested_sku") or "").strip()
        requested_barcode_qty = _requested_barcode_qty(payload)
        requested_rows = _requested_partial_rows(payload)
        requested_barcodes_raw = payload.get("requested_barcodes")
        if isinstance(requested_barcodes_raw, list):
            requested_barcodes = requested_barcodes_raw
        else:
            requested_barcodes = _parse_json_list(requested_barcodes_raw)
        barcode_values = {
            str(value).strip()
            for value in requested_barcodes
            if str(value or "").strip()
        }
        requested_goods_type = _normalize_goods_type(payload.get("requested_goods_type"))
        goods_type_values = {requested_goods_type} if requested_goods_type else set()
        sku_values = {requested_sku} if requested_sku else set()
        if barcode_values:
            sku_values.update(
                SKUBarcode.objects.filter(
                    agency_id=task.request.agency_id,
                    value__in=barcode_values,
                )
                .values_list("sku__sku_code", flat=True)
            )
            sku_values.discard(None)
        if requested_barcode_qty:
            barcode_values.update(requested_barcode_qty.keys())
        if not (barcode_values or sku_values):
            return MoveTaskCommandResult(ok=False, error="Р’ Р·Р°РґР°РЅРёРё РЅРµ СѓРєР°Р·Р°РЅ С‚РѕРІР°СЂ РґР»СЏ РѕС‚Р±РѕСЂР°.")
        picked_qty = 0
        if requested_rows:
            rows_total = sum(_as_int(row.get("qty")) for row in requested_rows)
            if rows_total <= 0:
                return MoveTaskCommandResult(ok=False, error="Р’ Р·Р°РґР°РЅРёРё РЅРµ СѓРєР°Р·Р°РЅРѕ РєРѕР»РёС‡РµСЃС‚РІРѕ Рє РѕС‚Р±РѕСЂСѓ.")
            if requested_qty > 0 and rows_total != requested_qty:
                return MoveTaskCommandResult(
                    ok=False,
                    error=(
                        f"РЎСѓРјРјР° РѕС‚Р±РѕСЂР° РїРѕ РєРѕСЂРѕР±Р°Рј ({rows_total}) РЅРµ СЃРѕРІРїР°РґР°РµС‚ "
                        f"СЃ РєРѕР»РёС‡РµСЃС‚РІРѕРј РІ Р·Р°РґР°РЅРёРё ({requested_qty})."
                    ),
                )
            requested_qty = rows_total
            picked_rows = []
            for row_plan in requested_rows:
                row_box = _normalize_box_code(row_plan.get("box_code"))
                row_qty = _as_int(row_plan.get("qty"))
                row_barcode_qty = _normalize_barcode_qty_map(row_plan.get("barcode_qty"))
                if not row_box or row_qty <= 0:
                    return MoveTaskCommandResult(
                        ok=False,
                        error="РќРµРєРѕСЂСЂРµРєС‚РЅС‹Рµ РґР°РЅРЅС‹Рµ РїРѕ СЃС‚СЂРѕРєР°Рј РѕС‚Р±РѕСЂР° РІ Р·Р°РґР°РЅРёРё.",
                    )
                row_picked = 0
                if row_barcode_qty:
                    row_total = _barcode_qty_total(row_barcode_qty)
                    if row_total != row_qty:
                        return MoveTaskCommandResult(
                            ok=False,
                            error=(
                                f"Р’ РєРѕСЂРѕР±Рµ {row_box} СЂР°Р·Р±РёРІРєР° РЁРљ ({row_total}) "
                                f"РЅРµ СЃРѕРІРїР°РґР°РµС‚ СЃ РєРѕР»РёС‡РµСЃС‚РІРѕРј ({row_qty})."
                            ),
                        )
                    for barcode, barcode_qty in row_barcode_qty.items():
                        consumed_ok, consume_error, picked_part = _consume_box_qty(
                            placement_payload,
                            pallet_code,
                            row_box,
                            barcode_qty,
                            {barcode},
                            set(),
                            goods_type_values,
                        )
                        if not consumed_ok:
                            return MoveTaskCommandResult(
                                ok=False,
                                error=consume_error or f"РќРµ СѓРґР°Р»РѕСЃСЊ РІС‹РїРѕР»РЅРёС‚СЊ РѕС‚Р±РѕСЂ РїРѕ РЁРљ {barcode}.",
                            )
                        row_picked += picked_part
                else:
                    consumed_ok, consume_error, row_picked = _consume_box_qty(
                        placement_payload,
                        pallet_code,
                        row_box,
                        row_qty,
                        barcode_values,
                        sku_values,
                        goods_type_values,
                    )
                    if not consumed_ok:
                        return MoveTaskCommandResult(
                            ok=False,
                            error=consume_error or "РќРµ СѓРґР°Р»РѕСЃСЊ РІС‹РїРѕР»РЅРёС‚СЊ РѕС‚Р±РѕСЂ.",
                        )
                picked_qty += row_picked
                picked_rows.append(
                    {
                        "box_code": row_box,
                        "qty": row_qty,
                        "picked_qty": row_picked,
                        "barcode_qty": row_barcode_qty,
                        "sku_code": requested_sku,
                        "barcodes": list(barcode_values),
                    }
                )
            payload["picked_rows"] = picked_rows
        else:
            if requested_qty <= 0:
                if requested_barcode_qty:
                    requested_qty = _barcode_qty_total(requested_barcode_qty)
                if requested_qty <= 0:
                    return MoveTaskCommandResult(ok=False, error="Р’ Р·Р°РґР°РЅРёРё РЅРµ СѓРєР°Р·Р°РЅРѕ РєРѕР»РёС‡РµСЃС‚РІРѕ Рє РѕС‚Р±РѕСЂСѓ.")
            if requested_barcode_qty:
                plan_total = _barcode_qty_total(requested_barcode_qty)
                if requested_qty > 0 and plan_total != requested_qty:
                    return MoveTaskCommandResult(
                        ok=False,
                        error=(
                            f"Р Р°Р·Р±РёРІРєР° РїРѕ РЁРљ ({plan_total}) РЅРµ СЃРѕРІРїР°РґР°РµС‚ "
                            f"СЃ РєРѕР»РёС‡РµСЃС‚РІРѕРј РІ Р·Р°РґР°РЅРёРё ({requested_qty})."
                        ),
                    )
            requested_box = _single_requested_box(payload)
            use_pallet_level_pick = not requested_box and not _payload_box_codes(payload)
            if requested_barcode_qty:
                for barcode, barcode_qty in requested_barcode_qty.items():
                    if use_pallet_level_pick:
                        consumed_ok, consume_error, picked_part = _consume_pallet_qty(
                            placement_payload,
                            pallet_code,
                            barcode_qty,
                            {barcode},
                            set(),
                            goods_type_values,
                        )
                    else:
                        consumed_ok, consume_error, picked_part = _consume_box_qty(
                            placement_payload,
                            pallet_code,
                            requested_box,
                            barcode_qty,
                            {barcode},
                            set(),
                            goods_type_values,
                        )
                    if not consumed_ok:
                        return MoveTaskCommandResult(
                            ok=False,
                            error=consume_error or f"РќРµ СѓРґР°Р»РѕСЃСЊ РІС‹РїРѕР»РЅРёС‚СЊ РѕС‚Р±РѕСЂ РїРѕ РЁРљ {barcode}.",
                        )
                    picked_qty += picked_part
                if picked_qty <= 0:
                    return MoveTaskCommandResult(
                        ok=False,
                        error="РќРµ СѓРґР°Р»РѕСЃСЊ РІС‹РїРѕР»РЅРёС‚СЊ РѕС‚Р±РѕСЂ РїРѕ Р·Р°СЏРІР»РµРЅРЅРѕР№ СЂР°Р·Р±РёРІРєРµ РЁРљ.",
                    )
            else:
                if use_pallet_level_pick:
                    consumed_ok, consume_error, picked_qty = _consume_pallet_qty(
                        placement_payload,
                        pallet_code,
                        requested_qty,
                        barcode_values,
                        sku_values,
                        goods_type_values,
                    )
                else:
                    consumed_ok, consume_error, picked_qty = _consume_box_qty(
                        placement_payload,
                        pallet_code,
                        requested_box,
                        requested_qty,
                        barcode_values,
                        sku_values,
                        goods_type_values,
                    )
                if not consumed_ok:
                    return MoveTaskCommandResult(
                        ok=False,
                        error=consume_error or "РќРµ СѓРґР°Р»РѕСЃСЊ РІС‹РїРѕР»РЅРёС‚СЊ РѕС‚Р±РѕСЂ.",
                    )
        payload["picked_qty"] = picked_qty
        if not requested_rows:
            row_barcode_qty = dict(requested_barcode_qty or {})
            if not row_barcode_qty and len(barcode_values) == 1:
                row_barcode_qty[next(iter(barcode_values))] = picked_qty
            payload["picked_rows"] = [
                {
                    "box_code": _single_requested_box(payload),
                    "qty": picked_qty,
                    "picked_qty": picked_qty,
                    "barcode_qty": row_barcode_qty,
                    "sku_code": requested_sku,
                    "barcodes": list(barcode_values),
                }
            ]
        processing_partial_pick_rows = [
            dict(row)
            for row in (payload.get("picked_rows") or [])
            if isinstance(row, dict)
        ]
        processing_direct_arrival_pending = bool(processing_partial_pick_rows)
        placement_payload["act_items_removed"] = True
        if requested_rows and len(requested_rows) > 1:
            placement_description = (
                f"Р§Р°СЃС‚РёС‡РЅС‹Р№ РѕС‚Р±РѕСЂ {picked_qty} С€С‚. РёР· {len(requested_rows)} РєРѕСЂРѕР±РѕРІ "
                f"РЅР° РїР°Р»Р»РµС‚Рµ {pallet_code}"
            )
            done_status_label = "РћС‚Р±РѕСЂ РёР· РєРѕСЂРѕР±РѕРІ РІС‹РїРѕР»РЅРµРЅ"
        else:
            requested_box = _single_requested_box(payload)
            if requested_box:
                placement_description = (
                    f"Р§Р°СЃС‚РёС‡РЅС‹Р№ РѕС‚Р±РѕСЂ {picked_qty} С€С‚. РёР· РєРѕСЂРѕР±Р° {requested_box} "
                    f"РЅР° РїР°Р»Р»РµС‚Рµ {pallet_code}"
                )
                done_status_label = "РћС‚Р±РѕСЂ РёР· РєРѕСЂРѕР±Р° РІС‹РїРѕР»РЅРµРЅ"
            else:
                placement_description = f"Р§Р°СЃС‚РёС‡РЅС‹Р№ РѕС‚Р±РѕСЂ {picked_qty} С€С‚. СЃ РїР°Р»Р»РµС‚С‹ {pallet_code}"
                done_status_label = "РћС‚Р±РѕСЂ СЃ РїР°Р»Р»РµС‚С‹ РІС‹РїРѕР»РЅРµРЅ"
    elif to_zone == "OBR" and move_mode == MOVE_MODE_BOX_FULL:
        requested_boxes = _payload_box_codes(payload)
        if _is_any_matching_box_selection(payload):
            requested_boxes, resolve_error = _resolve_any_matching_boxes_for_completion(
                payload,
                placement_payload,
                pallet_code,
                require_scan_confirmation=require_scan_confirmation,
                desktop_selected_boxes=desktop_selected_boxes,
            )
            if resolve_error:
                return MoveTaskCommandResult(ok=False, error=resolve_error)
        removed_ok, remove_error, removed_count = _remove_boxes_from_pallet(
            placement_payload,
            pallet_code,
            requested_boxes,
        )
        if not removed_ok:
            return MoveTaskCommandResult(
                ok=False,
                error=remove_error or "РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РѕР±СЂР°С‚СЊ РєРѕСЂРѕР±Р°.",
            )
        payload["picked_boxes"] = requested_boxes
        processing_arrived_box_codes = list(requested_boxes)
        processing_direct_arrival_pending = bool(processing_arrived_box_codes)
        payload["picked_qty"] = _as_int(payload.get("requested_qty")) or removed_count
        placement_payload["act_items_removed"] = True
        if _is_any_matching_box_selection(payload):
            placement_description = (
                f"РћС‚РѕР±СЂР°РЅС‹ РїРѕРґС…РѕРґСЏС‰РёРµ РєРѕСЂРѕР±Р° ({', '.join(requested_boxes)}) СЃ РїР°Р»Р»РµС‚С‹ {pallet_code}"
            )
            done_status_label = (
                f"РћС‚РѕР±СЂР°РЅС‹ РїРѕРґС…РѕРґСЏС‰РёРµ РєРѕСЂРѕР±Р° ({removed_count})"
                if removed_count
                else "РџРѕРґС…РѕРґСЏС‰РёРµ РєРѕСЂРѕР±Р° РїРµСЂРµРґР°РЅС‹ РІ РѕР±СЂР°Р±РѕС‚РєСѓ"
            )
        else:
            placement_description = (
                f"РћС‚РѕР±СЂР°РЅС‹ РєРѕСЂРѕР±Р° ({', '.join(requested_boxes)}) СЃ РїР°Р»Р»РµС‚С‹ {pallet_code}"
            )
            done_status_label = (
                f"РћС‚РѕР±СЂР°РЅС‹ РєРѕСЂРѕР±Р° ({removed_count})"
                if removed_count
                else "РљРѕСЂРѕР±Р° РїРµСЂРµРґР°РЅС‹ РІ РѕР±СЂР°Р±РѕС‚РєСѓ"
            )
    elif to_zone == "OBR":
        updated = False
        removed_boxes = set()
        updated_pallets = []
        for pallet in pallets:
            if not isinstance(pallet, dict):
                continue
            if str(pallet.get("code") or "").strip() == pallet_code:
                updated = True
                for box_code in pallet.get("boxes") or []:
                    code = str(box_code or "").strip()
                    if code:
                        removed_boxes.add(code)
                continue
            updated_pallets.append(pallet)
        if removed_boxes:
            boxes = [
                box
                for box in boxes
                if str((box or {}).get("code") or "").strip() not in removed_boxes
            ]
        if not updated:
            return MoveTaskCommandResult(ok=False, error="РќРµ СѓРґР°Р»РѕСЃСЊ СѓРґР°Р»РёС‚СЊ РїР°Р»Р»РµС‚Сѓ РёР· СЂР°Р·РјРµС‰РµРЅРёСЏ.")
        placement_payload["act_pallets"] = updated_pallets
        placement_payload["act_boxes"] = boxes
        placement_payload["act_items_removed"] = True
        placement_description = f"РџР°Р»Р»РµС‚Р° {pallet_code} РїРµСЂРµРґР°РЅР° РІ Р·РѕРЅСѓ РѕР±СЂР°Р±РѕС‚РєРё"
        done_status_label = "РџР°Р»Р»РµС‚Р° РїРµСЂРµРґР°РЅР° РІ РѕР±СЂР°Р±РѕС‚РєСѓ"
    else:
        updated = False
        for pallet in pallets:
            if not isinstance(pallet, dict):
                continue
            if str(pallet.get("code") or "").strip() == pallet_code:
                pallet["location"] = _build_location(
                    to_location.get("zone"),
                    _as_int(to_location.get("row")),
                    _as_int(to_location.get("section")),
                    _as_int(to_location.get("tier")),
                    _as_int(to_location.get("cell")),
                )
                updated = True
                break
        if not updated:
            return MoveTaskCommandResult(ok=False, error="РќРµ СѓРґР°Р»РѕСЃСЊ РѕР±РЅРѕРІРёС‚СЊ Р»РѕРєР°С†РёСЋ РїР°Р»Р»РµС‚С‹.")
        placement_payload["act_pallets"] = pallets

    placement_payload["act"] = "placement"
    placement_payload["act_state"] = "closed"
    move_agency = getattr(task.request, "agency", None)
    warehouse_operation_id = int(payload.get("warehouse_operation_id") or 0)
    warehouse_operation_handled = False
    shipping_order_id = _shipping_order_key_from_payload(payload)
    processing_order_id = str(payload.get("processing_order_id") or "").strip()
    if shipping_reserve_rebind and to_zone == "OTG":
        try:
            WarehouseWritePathService.rebind_shipping_reserve_boxes(
                agency=task.request.agency,
                order_id=str(shipping_reserve_rebind.get("shipping_order_id") or "").strip(),
                reserved_box_codes=list(shipping_reserve_rebind.get("reserved_box_codes") or []),
                actual_box_codes=list(shipping_reserve_rebind.get("actual_box_codes") or []),
                performed_by=authenticated_user,
                source_document_id=str(shipping_reserve_rebind.get("shipping_order_id") or "").strip(),
            )
            shipping_reserve_rebind = None
        except ValueError as exc:
            return MoveTaskCommandResult(ok=False, error=str(exc))
    if to_zone == "OTG" and not warehouse_operation_id and shipping_order_id and not skip_shipping_otg_operation:
        shipping_operation_statuses = [
            WarehouseOperation.STATUS_CREATED,
            WarehouseOperation.STATUS_PLANNED,
            WarehouseOperation.STATUS_IN_PROGRESS,
            WarehouseOperation.STATUS_PARTIAL,
            WarehouseOperation.STATUS_BLOCKED,
        ]
        if payload.get("picked_boxes") and otg_already_delivered_box_codes(payload, task.request.agency):
            shipping_operation_statuses.append(WarehouseOperation.STATUS_DONE)
        shipping_move_operation = (
            WarehouseOperation.objects.filter(
                agency=task.request.agency,
                operation_type=WarehouseOperation.TYPE_MOVE_TO_OTG,
                context_type="shipping",
                context_id=shipping_order_id,
                status__in=shipping_operation_statuses,
            )
            .order_by("-id")
            .first()
        )
        if shipping_move_operation is None:
            use_container_scoped_otg_move = move_mode in {MOVE_MODE_BOX_FULL, MOVE_MODE_BOX_PARTIAL}
            try:
                if use_container_scoped_otg_move:
                    shipping_move_operation = WarehouseWritePathService.request_move_to_otg_for_containers(
                        agency=task.request.agency,
                        order_id=shipping_order_id,
                        container_codes=_otg_operation_container_codes(payload, pallet_code),
                        destination_row_no=_as_int(to_location.get("row")),
                        destination_section_no=_as_int(to_location.get("section")),
                        destination_tier_no=_as_int(to_location.get("tier")),
                        destination_cell_no=_as_int(to_location.get("cell")),
                        destination_location_code=str(to_location.get("code") or "").strip(),
                        allow_legacy_generic_destination=not bool(
                            payload.get("concrete_location_required")
                        ),
                        requested_by=authenticated_user,
                        requested_by_role="super_car",
                    )
                else:
                    shipping_move_operation = WarehouseWritePathService.request_move_to_otg(
                        agency=task.request.agency,
                        order_id=shipping_order_id,
                        destination_row_no=_as_int(to_location.get("row")),
                        destination_section_no=_as_int(to_location.get("section")),
                        destination_tier_no=_as_int(to_location.get("tier")),
                        destination_cell_no=_as_int(to_location.get("cell")),
                        destination_location_code=str(to_location.get("code") or "").strip(),
                        allow_legacy_generic_destination=not bool(
                            payload.get("concrete_location_required")
                        ),
                        requested_by=authenticated_user,
                        requested_by_role="super_car",
                    )
            except ValueError as first_exc:
                if use_container_scoped_otg_move:
                    return MoveTaskCommandResult(ok=False, error=str(first_exc))
                try:
                    shipping_move_operation = WarehouseWritePathService.request_move_to_otg_for_containers(
                        agency=task.request.agency,
                        order_id=shipping_order_id,
                        container_codes=_otg_operation_container_codes(payload, pallet_code),
                        destination_row_no=_as_int(to_location.get("row")),
                        destination_section_no=_as_int(to_location.get("section")),
                        destination_tier_no=_as_int(to_location.get("tier")),
                        destination_cell_no=_as_int(to_location.get("cell")),
                        destination_location_code=str(to_location.get("code") or "").strip(),
                        allow_legacy_generic_destination=not bool(
                            payload.get("concrete_location_required")
                        ),
                        requested_by=authenticated_user,
                        requested_by_role="super_car",
                    )
                except ValueError as container_exc:
                    error_text = str(container_exc) or str(first_exc)
                    return MoveTaskCommandResult(ok=False, error=error_text)
        warehouse_operation_id = int(shipping_move_operation.id or 0)
        payload["warehouse_operation_id"] = warehouse_operation_id
    if warehouse_operation_id:
        if to_zone == "OTG":
            shipping_move_operation = (
                WarehouseOperation.objects.filter(
                    id=warehouse_operation_id,
                    operation_type=WarehouseOperation.TYPE_MOVE_TO_OTG,
                )
                .order_by("id")
                .first()
            )
            if shipping_move_operation and shipping_move_operation.status == WarehouseOperation.STATUS_DONE:
                warehouse_operation_handled = True
            elif shipping_move_operation:
                try:
                    if shipping_move_operation.status != WarehouseOperation.STATUS_IN_PROGRESS:
                        WarehouseWritePathService.start_move_to_otg(
                            operation=shipping_move_operation,
                            performed_by=authenticated_user,
                        )
                    WarehouseWritePathService.complete_move_to_otg(
                        operation=shipping_move_operation,
                        performed_by=authenticated_user,
                        allow_legacy_generic_destination=not bool(
                            payload.get("concrete_location_required")
                        ),
                    )
                    warehouse_operation_handled = True
                except ValueError:
                    pass
        putaway_operation = (
            WarehouseOperation.objects.filter(
                id=warehouse_operation_id,
                operation_type=WarehouseOperation.TYPE_PUTAWAY,
            )
            .order_by("id")
            .first()
        )
        if putaway_operation and putaway_operation.status != WarehouseOperation.STATUS_DONE:
            try:
                WarehouseWritePathService.complete_putaway_operation(
                    operation=putaway_operation,
                    performed_by=authenticated_user,
                )
                warehouse_operation_handled = True
            except ValueError:
                pass
        if to_zone == "OBR" and not processing_direct_arrival_pending:
            processing_move_operation = (
                WarehouseOperation.objects.filter(
                    id=warehouse_operation_id,
                    operation_type=WarehouseOperation.TYPE_MOVE_TO_PROCESSING,
                )
                .order_by("id")
                .first()
            )
            if processing_move_operation and processing_move_operation.status != WarehouseOperation.STATUS_DONE:
                try:
                    if processing_move_operation.status != WarehouseOperation.STATUS_IN_PROGRESS:
                        WarehouseWritePathService.start_move_to_processing(
                            operation=processing_move_operation,
                            performed_by=authenticated_user,
                        )
                    WarehouseWritePathService.complete_move_to_processing(
                        operation=processing_move_operation,
                        performed_by=authenticated_user,
                    )
                    warehouse_operation_handled = True
                except ValueError:
                    pass
            processing_order_id = str(
                payload.get("processing_order_id")
                or getattr(processing_move_operation, "context_id", "")
                or ""
            ).strip()
            if processing_order_id and processing_move_operation and processing_move_operation.agency:
                latest_processing_entry = (
                    OrderAuditEntry.objects.filter(order_id=processing_order_id, order_type="processing")
                    .order_by("-created_at", "-id")
                    .first()
                )
                status_payload = latest_processing_entry.payload or {} if latest_processing_entry else {}
                status_value = str(
                    status_payload.get("status") or status_payload.get("submit_action") or ""
                ).strip().lower()
                status_label = str(status_payload.get("status_label") or "").strip().lower()
                if status_value == "processing_in_work" or "РІР·СЏС‚Р°" in status_label:
                    WarehouseWritePathService.start_processing_if_ready(
                        agency=processing_move_operation.agency,
                        order_id=processing_order_id,
                        started_by=authenticated_user,
                        started_by_role="super_car",
                    )
    from_zone = _normalize_zone_code((payload.get("from_location") or {}).get("zone") or "")
    processing_order_id = str(payload.get("processing_order_id") or "").strip()
    if (
        processing_order_id
        and from_zone == "OBR"
        and to_zone
        and to_zone not in {"OBR", "OTG", "LOAD", "VEH"}
        and pallet_code
        and move_agency
        and not warehouse_operation_handled
    ):
        try:
            output_return_operation = WarehouseWritePathService.complete_processing_output_return_to_storage(
                agency=move_agency,
                order_id=processing_order_id,
                pallet_code=pallet_code,
                destination_zone_code=to_zone,
                destination_row_no=_as_int(to_location.get("row")),
                destination_section_no=_as_int(to_location.get("section")),
                destination_tier_no=_as_int(to_location.get("tier")),
                destination_cell_no=_as_int(to_location.get("cell")),
                performed_by=authenticated_user,
            )
            if output_return_operation:
                warehouse_operation_id = int(output_return_operation.id or 0)
                payload["warehouse_operation_id"] = warehouse_operation_id
                warehouse_operation_handled = True
        except ValueError:
            pass
    placement_source_kind = str(placement_source.get("source_kind") or "").strip()
    if not warehouse_operation_handled and placement_source_kind != "warehouse_stock":
        OperationalStockService.replace_order_placement(
            placement_agency,
            placement_order_type,
            placement_order_id,
            placement_payload,
            performed_by=authenticated_user,
        )
    if to_zone == "OBR" and processing_order_id and processing_partial_pick_rows:
        try:
            created_snapshot_ids = WarehouseWritePathService.complete_partial_processing_pick_to_obr(
                agency=task.request.agency,
                order_id=processing_order_id,
                source_pallet_code=pallet_code,
                picked_rows=processing_partial_pick_rows,
                destination_row_no=_as_int(to_location.get("row")),
                destination_section_no=_as_int(to_location.get("section")),
                destination_tier_no=_as_int(to_location.get("tier")),
                destination_cell_no=_as_int(to_location.get("cell")),
                destination_location_code=str(to_location.get("code") or "").strip(),
                allow_legacy_generic_destination=not bool(
                    payload.get("concrete_location_required")
                ),
                performed_by=authenticated_user,
                source_document_id=str(task.legacy_order_id or "").strip(),
            )
        except ValueError as exc:
            return MoveTaskCommandResult(ok=False, error=str(exc))
        payload["processing_direct_arrival_written"] = True
        payload["processing_arrival"] = {
            "partial_snapshot_ids": created_snapshot_ids,
            "picked_rows": processing_partial_pick_rows,
        }
        warehouse_operation_handled = True
    elif to_zone == "OBR" and processing_order_id and processing_arrived_box_codes:
        try:
            marked_qty = WarehouseWritePathService.mark_processing_boxes_arrived_to_obr(
                agency=task.request.agency,
                order_id=processing_order_id,
                box_codes=processing_arrived_box_codes,
                destination_row_no=_as_int(to_location.get("row")),
                destination_section_no=_as_int(to_location.get("section")),
                destination_tier_no=_as_int(to_location.get("tier")),
                destination_cell_no=_as_int(to_location.get("cell")),
                destination_location_code=str(to_location.get("code") or "").strip(),
                allow_legacy_generic_destination=not bool(
                    payload.get("concrete_location_required")
                ),
                performed_by=authenticated_user,
                source_document_id=str(task.legacy_order_id or "").strip(),
            )
        except ValueError as exc:
            return MoveTaskCommandResult(ok=False, error=str(exc))
        if _as_int(marked_qty) <= 0:
            return MoveTaskCommandResult(ok=False, error="warehouse_obr_arrival_not_written")
        payload["processing_direct_arrival_written"] = True
        payload["processing_arrival"] = {
            "box_codes": list(processing_arrived_box_codes),
            "marked_qty": _as_int(marked_qty),
        }
        warehouse_operation_handled = True
    if to_zone == "OTG" and shipping_order_id and shipping_arrived_box_codes:
        marked_qty = WarehouseWritePathService.mark_shipping_boxes_arrived_to_otg(
            agency=task.request.agency,
            order_id=shipping_order_id,
            box_codes=shipping_arrived_box_codes,
            performed_by=authenticated_user,
            destination_location_code=str(to_location.get("code") or "").strip(),
            allow_legacy_generic_destination=not bool(
                payload.get("concrete_location_required")
            ),
        )
        if _as_int(marked_qty) <= 0:
            return MoveTaskCommandResult(ok=False, error="Не удалось записать доставленные короба отгрузки в warehouse.")
        mark_reservation_units_arrived_to_otg(
            order=shipping_order,
            box_codes=shipping_arrived_box_codes,
            user=authenticated_user,
        )
    if shipping_reserve_rebind:
        try:
            WarehouseWritePathService.rebind_shipping_reserve_boxes(
                agency=task.request.agency,
                order_id=str(shipping_reserve_rebind.get("shipping_order_id") or "").strip(),
                reserved_box_codes=list(shipping_reserve_rebind.get("reserved_box_codes") or []),
                actual_box_codes=list(shipping_reserve_rebind.get("actual_box_codes") or []),
                performed_by=authenticated_user,
                source_document_id=str(shipping_reserve_rebind.get("shipping_order_id") or "").strip(),
            )
        except ValueError as exc:
            return MoveTaskCommandResult(ok=False, error=str(exc))
    if _should_log_source_order_event(payload=payload, to_zone=to_zone):
        log_order_action(
            "status",
            order_id=placement_order_id,
            order_type=placement_order_type,
            user=authenticated_user,
            agency=placement_agency,
            description=placement_description,
            payload=placement_payload,
        )

    payload["mobile_placement_payload"] = copy.deepcopy(placement_payload)
    payload["status"] = MoveTask.STATUS_DONE
    payload["status_label"] = done_status_label
    payload["completed_at"] = timezone.localtime().isoformat()
    payload["completed_by_name"] = employee_name
    log_order_action(
        "status",
        order_id=task.legacy_order_id,
        order_type="stock_move",
        user=authenticated_user,
        agency=move_agency,
        description=f"Р—Р°РґР°РЅРёРµ {task.legacy_order_id} РІС‹РїРѕР»РЅРµРЅРѕ",
        payload=payload,
    )
    log_stock_move(
        "update",
        user=authenticated_user,
        agency=move_agency,
        description=f"Р—Р°РґР°РЅРёРµ {task.legacy_order_id} РІС‹РїРѕР»РЅРµРЅРѕ",
        snapshot={
            "move_id": task.legacy_order_id,
            "pallet_code": pallet_code,
            "from_location": payload.get("from_location"),
            "to_location": payload.get("to_location"),
            "from_label": payload.get("from_label"),
            "to_label": payload.get("to_label"),
            "receiving_order_id": placement_order_id,
            "status": MoveTask.STATUS_DONE,
            "completed_by": employee_name,
        },
    )
    sync_task_status_by_legacy_order_id(
        task.legacy_order_id,
        status=MoveTask.STATUS_DONE,
        assigned_to=authenticated_user,
        assigned_to_name=employee_name,
        qty_done=_as_int(payload.get("picked_qty") or payload.get("requested_qty")),
    )
    processing_order_id = str(payload.get("processing_order_id") or "").strip()
    from_zone = _normalize_zone_code((payload.get("from_location") or {}).get("zone") or "")
    if processing_order_id and to_zone == "OBR":
        latest_processing_entry = (
            OrderAuditEntry.objects.filter(order_id=processing_order_id, order_type="processing")
            .select_related("agency")
            .order_by("-created_at", "-id")
            .first()
        )
        log_processing_stage(
            order_id=processing_order_id,
            payload=latest_processing_entry.payload or {} if latest_processing_entry else {},
            stage=PROCESSING_STAGE_OBR_ARRIVED,
            user=authenticated_user,
            agency=move_agency,
            description="РўРѕРІР°СЂ РїСЂРёР±С‹Р» РІ OBR",
        )
    elif (
        processing_order_id
        and from_zone == "OBR"
        and to_zone
        and to_zone != "OBR"
        and processing_return_to_stock_completed(processing_order_id)
    ):
        latest_processing_entry = (
            OrderAuditEntry.objects.filter(order_id=processing_order_id, order_type="processing")
            .select_related("agency")
            .order_by("-created_at", "-id")
            .first()
        )
        log_processing_stage(
            order_id=processing_order_id,
            payload=latest_processing_entry.payload or {} if latest_processing_entry else {},
            stage=PROCESSING_STAGE_RETURNED_TO_STOCK,
            user=authenticated_user,
            agency=move_agency,
            description="РўРѕРІР°СЂ РІРѕР·РІСЂР°С‰РµРЅ РЅР° СЃРєР»Р°Рґ",
        )
    task.refresh_from_db()
    task.payload = payload
    task.save(update_fields=["payload", "updated_at"])
    if warehouse_operation_id and not warehouse_operation_handled:
        operation = (
            WarehouseOperation.objects.filter(
                id=warehouse_operation_id,
                operation_type=WarehouseOperation.TYPE_PUTAWAY,
            )
            .order_by("id")
            .first()
        )
        if operation and operation.status != WarehouseOperation.STATUS_DONE:
            try:
                WarehouseWritePathService.complete_putaway_operation(
                    operation=operation,
                    performed_by=authenticated_user,
                )
            except ValueError:
                pass
    if to_zone == "OBR" and warehouse_operation_id and not payload.get("processing_direct_arrival_written"):
        operation = (
            WarehouseOperation.objects.filter(
                id=warehouse_operation_id,
                operation_type=WarehouseOperation.TYPE_MOVE_TO_PROCESSING,
            )
            .order_by("id")
            .first()
        )
        if operation and operation.status != WarehouseOperation.STATUS_DONE:
            try:
                if operation.status != WarehouseOperation.STATUS_IN_PROGRESS:
                    WarehouseWritePathService.start_move_to_processing(
                        operation=operation,
                        performed_by=authenticated_user,
                    )
                WarehouseWritePathService.complete_move_to_processing(
                    operation=operation,
                    performed_by=authenticated_user,
                )
            except ValueError:
                pass
        processing_order_id = str(
            payload.get("processing_order_id")
            or (operation.context_id if operation else "")
            or ""
        ).strip()
        if processing_order_id and operation and operation.agency:
            latest_processing_entry = (
                OrderAuditEntry.objects.filter(order_id=processing_order_id, order_type="processing")
                .order_by("-created_at", "-id")
                .first()
            )
            status_payload = latest_processing_entry.payload or {} if latest_processing_entry else {}
            status_value = str(
                status_payload.get("status") or status_payload.get("submit_action") or ""
            ).strip().lower()
            status_label = str(status_payload.get("status_label") or "").strip().lower()
            if status_value == "processing_in_work" or "РІР·СЏС‚Р°" in status_label:
                WarehouseWritePathService.start_processing_if_ready(
                    agency=operation.agency,
                    order_id=processing_order_id,
                    started_by=authenticated_user,
                    started_by_role="super_car",
                )
    return MoveTaskCommandResult(ok=True, task=task, payload=payload)
