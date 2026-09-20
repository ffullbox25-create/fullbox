from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db.models import Q

from audit.models import OrderAuditEntry
from reachtruck.models import BoxClaim, MoveTask, PalletLock
from reachtruck.services.task_commands import (
    _same_box_code_scan,
    _same_pallet_code_scan,
    _scan_compare_variants,
    display_scan_text,
    display_pallet_scan_text,
)
from reachtruck.services.putaway_planner import (
    parse_int_value,
    putaway_location_label,
    putaway_location_scan_code,
)
from orders.services import ReceivingWorkflowService
from sklad.location_occupancy import active_os_physical_containers, os_location_occupancy_message
from sklad.models import WarehouseContainer, WarehouseLocation, WarehouseOperation, WarehouseStockSnapshot
from sklad.services.warehouse_stock_rows import normalize_stock_row_from_snapshot
from sklad.services.operational_locations import (
    normalize_operational_location_scan,
    require_concrete_movement_location,
    resolve_operational_location_scan,
)
from sklad.services.warehouse_transitions import WarehouseStateCode
from sklad.services.warehouse_write_path import WarehouseWritePathService
from shipping.reservation_units import active_reserved_pallet_codes_for_agency


SESSION_MOVE_KEY = "reachtruck_free_active_move"
SESSION_INSPECTION_KEY = "reachtruck_free_last_scan"

ALLOWED_ROLES = (
    "reachtruck_driver",
    "super_car",
    "storekeeper",
    "manager",
    "head_manager",
    "director",
    "admin",
)
MOVE_ROLES = {"reachtruck_driver", "super_car", "admin"}
DESTINATION_ZONES = {"OS", "MR"}
FREE_SOURCE_STATES = {
    WarehouseStateCode.STORED.value,
    WarehouseStateCode.PLACED_IN_RECEIVING.value,
    WarehouseStateCode.IN_PROCESSING_ZONE.value,
    WarehouseStateCode.PLACED_AFTER_PROCESSING.value,
}
NON_OCCUPYING_LOCATION_STATES = {
    WarehouseStateCode.PROCESSING_CONSUMED.value,
    WarehouseStateCode.SHIPPED.value,
    WarehouseStateCode.CANCELED.value,
}
PALLET_CONTAINER_TYPES = {
    WarehouseContainer.TYPE_PALLET,
    WarehouseContainer.TYPE_MIXED_PALLET,
}

_FINAL_TASK_STATUSES = {
    MoveTask.STATUS_DONE,
    MoveTask.STATUS_CANCELED,
    MoveTask.STATUS_FAILED,
}
_FINAL_OPERATION_STATUSES = {
    WarehouseOperation.STATUS_DONE,
    WarehouseOperation.STATUS_CANCELED,
}
_DASH_TRANSLATION = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\ufe63": "-",
        "\uff0d": "-",
    }
)
_OS_SCAN_RE = re.compile(r"^(?P<line>[0-9A-Z]+)-(?P<row>\d+)/(?P<tier>\d+)-(?P<cell>\d+)$")
_LEGACY_OS_SCAN_RE = re.compile(r"^OS-(?P<row>\d+)-(?P<section>\d+)-(?P<tier>\d+)-(?P<cell>\d+)$")
_MR_SCAN_RE = re.compile(r"^MR(?:-(?P<row>\d+))?$")
_AIM_SYMBOLOGY_PREFIX_RE = re.compile(r"^\][A-Z0-9]{2}")
_CYRILLIC_B_OS_LINE_RE = re.compile(r"^В(?=-\d+/)")
_OS_SECTION_BY_LABEL = {
    "0": 1,
    "A": 2,
    "B": 3,
    "C": 4,
    "D": 5,
    "E": 6,
    "F": 7,
    "G": 8,
    "I": 9,
}


def _is_pallet_container(container) -> bool:
    return bool(container and container.container_type in PALLET_CONTAINER_TYPES)


@dataclass(frozen=True)
class FreeMoveResult:
    operation: WarehouseOperation
    message: str


def clean_code(value) -> str:
    return str(value or "").strip()


def readable_pallet_scan_text(scan_value: str, *, resolved_code: str = "") -> str:
    resolved = clean_code(resolved_code)
    if resolved:
        return resolved
    return clean_code(display_pallet_scan_text(scan_value))


def readable_scan_text(scan_value: str, *, resolved_code: str = "") -> str:
    resolved = clean_code(resolved_code)
    if resolved:
        return resolved
    return clean_code(display_scan_text(scan_value))


def normalize_scan_text(value) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.translate(_DASH_TRANSLATION).replace("\u00a0", " ")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    text = "".join(text.split()).upper()
    text = _AIM_SYMBOLOGY_PREFIX_RE.sub("", text)
    return _CYRILLIC_B_OS_LINE_RE.sub("B", text)


def _snapshot_base_query():
    return WarehouseStockSnapshot.objects.filter(is_archived=False, qty__gt=0).select_related(
        "agency",
        "sku_ref",
        "container",
        "parent_container",
        "location",
        "active_operation",
    )


def location_to_dict(location: WarehouseLocation | None) -> dict:
    if location is None:
        return {"zone": "", "row": "", "section": "", "tier": "", "cell": ""}
    return {
        "zone": str(location.zone_code or "").strip().upper(),
        "row": int(location.row_no or 0) or "",
        "section": int(location.section_no or 0) or "",
        "tier": int(location.tier_no or 0) or "",
        "cell": int(location.cell_no or 0) or "",
    }


def parse_destination_scan(scan_value: str) -> tuple[dict | None, str]:
    scan = normalize_scan_text(scan_value)
    if not scan:
        return None, "Отсканируйте новое место."

    match = _OS_SCAN_RE.match(scan)
    if match:
        line = match.group("line")
        section = _OS_SECTION_BY_LABEL.get(line)
        if not section and line.isdigit():
            section = int(line)
        if not section:
            return None, "Не удалось распознать линию OS."
        return (
            {
                "zone": "OS",
                "row": int(match.group("row")),
                "section": section,
                "tier": int(match.group("tier")),
                "cell": int(match.group("cell")),
            },
            "",
        )

    match = _LEGACY_OS_SCAN_RE.match(scan)
    if match:
        return (
            {
                "zone": "OS",
                "row": int(match.group("row")),
                "section": int(match.group("section")),
                "tier": int(match.group("tier")),
                "cell": int(match.group("cell")),
            },
            "",
        )

    match = _MR_SCAN_RE.match(scan)
    if match:
        row = int(match.group("row") or 0)
        if not row:
            return None, "Для зоны MR отсканируйте место с номером ряда."
        return {"zone": "MR", "row": row, "section": "", "tier": "", "cell": ""}, ""

    if scan in {"PR", "OTG"}:
        return None, (
            f"Общая зона {scan} запрещена как место назначения. "
            "Отсканируйте QR конкретного места."
        )
    if scan == "OBR":
        return None, "Зона OBR недоступна для свободного перемещения."
    if scan == "OS":
        return None, "Для зоны OS отсканируйте полную ячейку."
    return None, "Место не распознано. Ожидается QR конкретного места OS или MR."


def parse_location_scan(scan_value: str) -> tuple[dict | None, str]:
    scan = normalize_scan_text(scan_value)
    if not scan:
        return None, ""

    match = _OS_SCAN_RE.match(scan)
    if match:
        line = match.group("line")
        section = _OS_SECTION_BY_LABEL.get(line)
        if not section and line.isdigit():
            section = int(line)
        if not section:
            return None, ""
        return (
            {
                "zone": "OS",
                "row": int(match.group("row")),
                "section": section,
                "tier": int(match.group("tier")),
                "cell": int(match.group("cell")),
            },
            "",
        )

    match = _LEGACY_OS_SCAN_RE.match(scan)
    if match:
        return (
            {
                "zone": "OS",
                "row": int(match.group("row")),
                "section": int(match.group("section")),
                "tier": int(match.group("tier")),
                "cell": int(match.group("cell")),
            },
            "",
        )

    match = _MR_SCAN_RE.match(scan)
    if match:
        row = int(match.group("row") or 0)
        return {"zone": "MR", "row": row or "", "section": "", "tier": "", "cell": ""}, ""

    if scan in {"PR", "OBR", "OTG"}:
        return {"zone": scan, "row": "", "section": "", "tier": "", "cell": ""}, ""

    return None, ""


def _location_from_dict(location: dict) -> WarehouseLocation | None:
    zone = clean_code(location.get("zone")).upper()
    if not zone:
        return None
    return (
        WarehouseLocation.objects.filter(
            zone_code__iexact=zone,
            row_no=parse_int_value(location.get("row")),
            section_no=parse_int_value(location.get("section")),
            tier_no=parse_int_value(location.get("tier")),
            cell_no=parse_int_value(location.get("cell")),
        )
        .order_by("warehouse_code", "id")
        .first()
    )


def _snapshot_query(pallet_code: str, *, agency_id: int | None = None):
    normalized = clean_code(pallet_code)
    qs = _snapshot_base_query().filter(
        Q(container_code__iexact=normalized)
        | Q(container__container_code__iexact=normalized)
        | Q(parent_container__container_code__iexact=normalized)
    )
    if agency_id:
        qs = qs.filter(agency_id=int(agency_id))
    return qs.order_by("agency_id", "id")


def _pallet_lookup_tokens(scan_value: str) -> list[str]:
    tokens: set[str] = set()
    for variant in _scan_compare_variants(scan_value):
        parts = [part for part in re.split(r"[-_/\s]+", variant) if part]
        max_tail_parts = min(4, len(parts))
        for tail_size in range(2, max_tail_parts + 1):
            tail = "-".join(parts[-tail_size:])
            if len(tail) >= 5:
                tokens.add(tail)
        for digits in re.findall(r"\d{4,}", variant):
            tokens.add(digits)
    return sorted(tokens, key=lambda item: (-len(item), item))[:12]


def _snapshot_pallet_codes(snapshot: WarehouseStockSnapshot) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()

    def add(raw_value) -> None:
        code = clean_code(raw_value)
        key = code.casefold()
        if not code or key in seen:
            return
        seen.add(key)
        codes.append(code)

    parent_container = getattr(snapshot, "parent_container", None)
    container = getattr(snapshot, "container", None)
    if _is_pallet_container(parent_container):
        add(getattr(parent_container, "container_code", ""))
    if _is_pallet_container(container):
        add(snapshot.container_code)
        add(getattr(container, "container_code", ""))
    return codes


def _resolve_pallet_scan(scan_value: str, *, agency_id: int | None = None) -> tuple[str, list[WarehouseStockSnapshot], list[str]]:
    normalized = clean_code(scan_value)
    exact_snapshots = list(_snapshot_query(normalized, agency_id=agency_id))
    if exact_snapshots:
        matched_codes = sorted(
            {
                candidate_code
                for snapshot in exact_snapshots
                for candidate_code in _snapshot_pallet_codes(snapshot)
                if _same_pallet_code_scan(scan_value, candidate_code)
                or candidate_code.casefold() == normalized.casefold()
            },
            key=lambda item: item.casefold(),
        )
        if len(matched_codes) > 1:
            return normalized, [], matched_codes
        if matched_codes:
            resolved = matched_codes[0]
            return resolved, list(_snapshot_query(resolved, agency_id=agency_id)), []
        return normalized, [], []

    lookup_tokens = _pallet_lookup_tokens(scan_value)
    if not lookup_tokens:
        return normalized, [], []

    token_filter = Q()
    for token in lookup_tokens:
        token_filter |= (
            Q(container_code__icontains=token)
            | Q(container__container_code__icontains=token)
            | Q(parent_container__container_code__icontains=token)
        )
    qs = _snapshot_base_query().filter(token_filter)
    if agency_id:
        qs = qs.filter(agency_id=int(agency_id))

    matched_codes: set[str] = set()
    for snapshot in qs.order_by("agency_id", "id")[:300]:
        for candidate_code in _snapshot_pallet_codes(snapshot):
            if _same_pallet_code_scan(scan_value, candidate_code):
                matched_codes.add(candidate_code)

    if not matched_codes:
        return normalized, [], []
    if len(matched_codes) > 1:
        return normalized, [], sorted(matched_codes, key=lambda item: item.casefold())

    resolved = next(iter(matched_codes))
    return resolved, list(_snapshot_query(resolved, agency_id=agency_id)), []


def _state_label(value: str) -> str:
    labels = {
        WarehouseStateCode.STORED.value: "На хранении",
        WarehouseStateCode.PLACED_IN_RECEIVING.value: "В зоне приемки",
        WarehouseStateCode.RESERVED_FOR_PROCESSING.value: "Зарезервировано под обработку",
        WarehouseStateCode.RESERVED_FOR_SHIPPING.value: "Зарезервировано под отгрузку",
        WarehouseStateCode.MOVING_TO_PROCESSING.value: "Едет в обработку",
        WarehouseStateCode.MOVING_TO_OTG.value: "Едет в OTG",
        WarehouseStateCode.IN_PROCESSING_ZONE.value: "В зоне обработки",
        WarehouseStateCode.PROCESSING_IN_PROGRESS.value: "В обработке",
        WarehouseStateCode.PROCESSING_CONSUMED.value: "Списано обработкой",
        WarehouseStateCode.PLACED_AFTER_PROCESSING.value: "После обработки",
        WarehouseStateCode.IN_OTG.value: "В OTG",
        WarehouseStateCode.PALLETIZING.value: "Паллетизация",
        WarehouseStateCode.READY_FOR_LOADING.value: "Готово к погрузке",
        WarehouseStateCode.ASSIGNED_TO_TRIP.value: "Назначено в рейс",
        WarehouseStateCode.LOADING_IN_PROGRESS.value: "Погрузка",
        WarehouseStateCode.LOADED_TO_VEHICLE.value: "Загружено в машину",
        WarehouseStateCode.SHIPPED.value: "Отгружено",
        WarehouseStateCode.PARTIALLY_SHIPPED.value: "Частично отгружено",
        WarehouseStateCode.CANCELED.value: "Отменено",
    }
    return labels.get(str(value or "").strip(), str(value or "").strip() or "-")


def _context_label(context_type: str, context_id: str) -> str:
    labels = {
        "receiving": "Приемка",
        "processing": "Обработка",
        "shipping": "Отгрузка",
        "reachtruck_free": "Ричтракер Free",
    }
    prefix = labels.get(str(context_type or "").strip(), str(context_type or "").strip() or "Контекст")
    value = str(context_id or "").strip()
    source = f"{prefix} {value}" if value else prefix
    return f"Источник: {source}"


def _build_lines(rows: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str, str, str, str], dict] = {}
    for row in rows:
        key = (
            str(row.get("sku") or ""),
            str(row.get("name") or ""),
            str(row.get("size") or ""),
            str(row.get("barcode") or ""),
            str(row.get("goods_type") or ""),
        )
        bucket = grouped.setdefault(
            key,
            {
                "sku": key[0] or "-",
                "name": key[1],
                "size": key[2],
                "barcode": key[3],
                "goods_type": key[4],
                "qty": 0,
                "boxes": [],
            },
        )
        bucket["qty"] += int(row.get("qty") or 0)
        box_code = clean_code(row.get("box_code"))
        if box_code and box_code not in bucket["boxes"]:
            bucket["boxes"].append(box_code)
    return sorted(grouped.values(), key=lambda item: (item["sku"], item["barcode"], item["size"]))


def _agency_display_name(agency) -> str:
    return (
        clean_code(getattr(agency, "short_name", ""))
        or clean_code(getattr(agency, "agn_name", ""))
        or clean_code(getattr(agency, "name", ""))
        or (str(agency) if agency else "")
    )


def _single_value(values, default: str = "-") -> str:
    cleaned = [clean_code(value) for value in values if clean_code(value)]
    unique = list(dict.fromkeys(cleaned))
    if not unique:
        return default
    if len(unique) == 1:
        return unique[0]
    return "Несколько"


def _summary_item(label: str, value) -> dict:
    text = clean_code(value)
    return {"label": label, "value": text or "-"}


def _placement_location_label(row: dict) -> str:
    zone = clean_code(row.get("zone")).upper()
    if zone:
        return putaway_location_label(
            {
                "zone": zone,
                "row": parse_int_value(row.get("row")),
                "section": parse_int_value(row.get("section")),
                "tier": parse_int_value(row.get("tier")),
                "cell": parse_int_value(row.get("cell")),
            }
        )
    return clean_code(row.get("location")) or "-"


def _snapshot_location_summary(snapshots: list[WarehouseStockSnapshot]) -> tuple[str, str, int]:
    if not snapshots:
        return "-", "-", 0
    location_ids = {int(snapshot.location_id or 0) for snapshot in snapshots}
    if len(location_ids) > 1:
        return "Несколько мест", "-", len(location_ids)
    first = snapshots[0]
    location_dict = location_to_dict(first.location)
    if not clean_code(location_dict.get("zone")) and clean_code(first.zone_code):
        location_dict["zone"] = clean_code(first.zone_code).upper()
    return putaway_location_label(location_dict), putaway_location_scan_code(location_dict), len(location_ids)


def _stock_contexts(rows: list[dict]) -> list[str]:
    return sorted(
        {
            _context_label(row.get("source_order_type"), row.get("source_order_id"))
            for row in rows
            if clean_code(row.get("source_order_type")) or clean_code(row.get("source_order_id"))
        }
    )


def _stock_binding_labels(
    snapshots: list[WarehouseStockSnapshot],
    *,
    pallet_code: str = "",
    box_codes: list[str] | None = None,
) -> list[str]:
    bindings: list[str] = []
    for snapshot in snapshots:
        if snapshot.active_operation_id:
            operation = snapshot.active_operation
            if operation and clean_code(operation.status) in _FINAL_OPERATION_STATUSES:
                continue
            label = f"#{snapshot.active_operation_id}"
            if operation:
                label = f"{operation.operation_type} {label}"
            bindings.append(f"Активная складская операция {label}")
            break
    if any(int(snapshot.processing_reserved_qty or 0) > 0 for snapshot in snapshots):
        bindings.append("Есть резерв под обработку.")
    if any(int(snapshot.shipping_reserved_qty or 0) > 0 for snapshot in snapshots):
        bindings.append("Есть резерв под отгрузку.")
    if any(int(snapshot.other_reserved_qty or 0) > 0 for snapshot in snapshots):
        bindings.append("Есть ручной резерв.")
    first = snapshots[0] if snapshots else None
    if first and first.agency_id and clean_code(pallet_code):
        if active_reserved_pallet_codes_for_agency(int(first.agency_id), [clean_code(pallet_code)]):
            bindings.append("Есть резерв под отгрузку.")
    if first and first.agency_id and (clean_code(pallet_code) or box_codes):
        bindings.extend(
            _active_reachtruck_blockers(
                agency_id=int(first.agency_id),
                pallet_code=clean_code(pallet_code),
                box_codes=box_codes or [],
            )
        )
    return list(dict.fromkeys(bindings))


def _build_placements(rows: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str, str, str], dict] = {}
    for row in rows:
        key = (
            _placement_location_label(row),
            clean_code(row.get("pallet_code")),
            clean_code(row.get("box_code")),
            clean_code(row.get("warehouse_state_code")),
        )
        bucket = grouped.setdefault(
            key,
            {
                "location": key[0] or "-",
                "pallet_code": key[1],
                "box_code": key[2],
                "state_label": _state_label(key[3]),
                "qty": 0,
            },
        )
        bucket["qty"] += int(row.get("qty") or 0)
    return sorted(
        grouped.values(),
        key=lambda item: (item["location"], item["pallet_code"], item["box_code"], item["state_label"]),
    )


def _flow_item_rows(*, items, box_code: str = "", pallet_code: str = "") -> list[dict]:
    rows: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        qty = int(item.get("qty") or 0)
        if qty <= 0:
            continue
        rows.append(
            {
                "sku": clean_code(item.get("sku_code")) or clean_code(item.get("sku")),
                "name": clean_code(item.get("name")),
                "size": clean_code(item.get("size")),
                "barcode": clean_code(item.get("barcode")),
                "goods_type": clean_code(item.get("goods_type")),
                "qty": qty,
                "box_code": clean_code(box_code),
                "pallet_code": clean_code(pallet_code),
                "zone": "PR",
                "location": "PR",
                "warehouse_state_code": WarehouseStateCode.PLACED_IN_RECEIVING.value,
                "source_order_type": "receiving",
            }
        )
    return rows


def _flow_match_for_scan(scan_value: str, *, object_type: str) -> dict | None:
    normalized = clean_code(scan_value)
    if not normalized:
        return None
    entries = (
        OrderAuditEntry.objects.filter(
            order_type="receiving",
            action="update",
            payload__has_key="flow_state",
        )
        .select_related("agency")
        .order_by("-created_at", "-id")
    )
    seen_orders: set[str] = set()
    for entry in entries:
        order_id = clean_code(entry.order_id)
        if not order_id:
            continue
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        flow_state = payload.get("flow_state")
        if not isinstance(flow_state, dict):
            continue
        if order_id in seen_orders:
            continue
        seen_orders.add(order_id)
        boxes = flow_state.get("boxes") if isinstance(flow_state.get("boxes"), list) else []
        pallets = flow_state.get("pallets") if isinstance(flow_state.get("pallets"), list) else []
        if object_type == "pallet":
            for pallet in pallets:
                if not isinstance(pallet, dict):
                    continue
                pallet_code = clean_code(pallet.get("code"))
                if pallet_code and (
                    pallet_code.casefold() == normalized.casefold()
                    or _same_pallet_code_scan(scan_value, pallet_code)
                ):
                    order_entries = list(
                        OrderAuditEntry.objects.filter(order_type="receiving", order_id=order_id)
                        .order_by("created_at", "id")
                    )
                    if ReceivingWorkflowService._flow_closed(order_entries):
                        break
                    return {
                        "entry": entry,
                        "order_id": order_id,
                        "agency": entry.agency,
                        "flow_state": flow_state,
                        "pallet": pallet,
                    }
        if object_type == "box":
            box_by_code = {
                clean_code(box.get("code")): box
                for box in boxes
                if isinstance(box, dict) and clean_code(box.get("code"))
            }
            for box_code, box in box_by_code.items():
                if not (
                    box_code.casefold() == normalized.casefold()
                    or _same_box_code_scan(scan_value, box_code)
                ):
                    continue
                order_entries = list(
                    OrderAuditEntry.objects.filter(order_type="receiving", order_id=order_id)
                    .order_by("created_at", "id")
                )
                if ReceivingWorkflowService._flow_closed(order_entries):
                    break
                parent_pallet = None
                for pallet in pallets:
                    if not isinstance(pallet, dict):
                        continue
                    pallet_box_codes = {clean_code(code) for code in (pallet.get("boxes") or [])}
                    if box_code in pallet_box_codes:
                        parent_pallet = pallet
                        break
                return {
                    "entry": entry,
                    "order_id": order_id,
                    "agency": entry.agency,
                    "flow_state": flow_state,
                    "box": box,
                    "pallet": parent_pallet,
                }
    return None


def _materialize_flow_pallet_match(match: dict | None, *, user=None) -> str:
    if not match:
        return ""
    pallet = match.get("pallet") if isinstance(match.get("pallet"), dict) else None
    pallet_code = clean_code(pallet.get("code")) if pallet else ""
    if not pallet_code or not bool(pallet.get("sealed")):
        return ""
    takeout_state = ReceivingWorkflowService.receiving_flow_pallet_takeout_state(
        order_id=clean_code(match.get("order_id")),
        pallet_code=pallet_code,
    )
    if not takeout_state.get("can_take", False):
        return ""
    agency = match.get("agency")
    if not agency:
        return ""
    ReceivingWorkflowService.materialize_receiving_flow_pallets(
        order_id=clean_code(match.get("order_id")),
        agency=agency,
        flow_state=match.get("flow_state") if isinstance(match.get("flow_state"), dict) else {},
        pallet_codes=[pallet_code],
        user=user,
    )
    return pallet_code


def _flow_pallet_info(match: dict) -> dict:
    pallet = match.get("pallet") if isinstance(match.get("pallet"), dict) else {}
    flow_state = match.get("flow_state") if isinstance(match.get("flow_state"), dict) else {}
    boxes = flow_state.get("boxes") if isinstance(flow_state.get("boxes"), list) else []
    box_by_code = {
        clean_code(box.get("code")): box
        for box in boxes
        if isinstance(box, dict) and clean_code(box.get("code"))
    }
    pallet_code = clean_code(pallet.get("code"))
    box_codes = [clean_code(code) for code in (pallet.get("boxes") or []) if clean_code(code)]
    rows = _flow_item_rows(items=pallet.get("items") or [], pallet_code=pallet_code)
    for box_code in box_codes:
        box = box_by_code.get(box_code) or {}
        rows.extend(_flow_item_rows(items=box.get("items") or [], box_code=box_code, pallet_code=pallet_code))
    sealed = bool(pallet.get("sealed"))
    total_qty = sum(int(row.get("qty") or 0) for row in rows)
    takeout_state = ReceivingWorkflowService.receiving_flow_pallet_takeout_state(
        order_id=clean_code(match.get("order_id")),
        pallet_code=pallet_code,
    )
    can_take = bool(takeout_state.get("can_take"))
    blockers = [] if can_take else [
        clean_code(takeout_state.get("reason"))
        or f"Приемка {clean_code(match.get('order_id'))}: размещение паллеты недоступно."
    ]
    agency_name = _agency_display_name(match.get("agency"))
    return {
        "ok": True,
        "found": True,
        "object_type": "pallet",
        "title": f"Паллета {pallet_code}",
        "code": pallet_code,
        "pallet_code": pallet_code,
        "agency_id": int(getattr(match.get("agency"), "id", 0) or 0),
        "agency_name": agency_name,
        "location_label": "PR · Зона приемки",
        "location_scan_code": "PR",
        "state_labels": [_state_label(WarehouseStateCode.PLACED_IN_RECEIVING.value)],
        "total_qty": total_qty,
        "box_count": len(box_codes),
        "box_codes": box_codes,
        "contexts": [_context_label("receiving", clean_code(match.get("order_id")))],
        "lines": _build_lines(rows),
        "placements": _build_placements(rows),
        "blockers": blockers,
        "is_free": can_take and not blockers,
        "can_move": False,
        "status_label": "Готова к забору" if can_take else "Размещение недоступно",
        "status_kind": "free" if can_take else "blocked",
        "summary": [
            _summary_item("Клиент", agency_name),
            _summary_item("QR места", "PR"),
            _summary_item("Количество", f"{total_qty} шт."),
            _summary_item("Короба", len(box_codes)),
            _summary_item("Статус", _state_label(WarehouseStateCode.PLACED_IN_RECEIVING.value)),
        ],
    }


def _flow_box_info(match: dict) -> dict:
    box = match.get("box") if isinstance(match.get("box"), dict) else {}
    pallet = match.get("pallet") if isinstance(match.get("pallet"), dict) else {}
    box_code = clean_code(box.get("code"))
    pallet_code = clean_code(pallet.get("code"))
    rows = _flow_item_rows(items=box.get("items") or [], box_code=box_code, pallet_code=pallet_code)
    total_qty = sum(int(row.get("qty") or 0) for row in rows)
    agency_name = _agency_display_name(match.get("agency"))
    return {
        "ok": True,
        "found": True,
        "object_type": "box",
        "title": f"Короб {box_code}",
        "code": box_code,
        "box_code": box_code,
        "pallet_code": pallet_code,
        "agency_id": int(getattr(match.get("agency"), "id", 0) or 0),
        "agency_name": agency_name,
        "location_label": "PR · Зона приемки",
        "location_scan_code": "PR",
        "state_labels": [_state_label(WarehouseStateCode.PLACED_IN_RECEIVING.value)],
        "total_qty": total_qty,
        "contexts": [_context_label("receiving", clean_code(match.get("order_id")))],
        "lines": _build_lines(rows),
        "placements": _build_placements(rows),
        "blockers": [],
        "can_move": False,
        "status_label": "В приемке",
        "status_kind": "blocked",
        "summary": [
            _summary_item("Клиент", agency_name),
            _summary_item("Паллета", pallet_code),
            _summary_item("QR места", "PR"),
            _summary_item("Количество", f"{total_qty} шт."),
            _summary_item("Статус", _state_label(WarehouseStateCode.PLACED_IN_RECEIVING.value)),
        ],
    }


def _scan_lookup_values(scan_value: str) -> list[str]:
    values: list[str] = []
    for value in (
        clean_code(scan_value),
        clean_code(display_scan_text(scan_value)),
        normalize_scan_text(scan_value),
    ):
        key = value.casefold()
        if value and key not in {item.casefold() for item in values}:
            values.append(value)
    return values


def _exact_container_type_for_scan(scan_value: str) -> str:
    lookup_values = _scan_lookup_values(scan_value)
    if not lookup_values:
        return ""
    query = Q()
    for value in lookup_values:
        query |= Q(container_code__iexact=value)
    containers = list(
        WarehouseContainer.objects.filter(query, status=WarehouseContainer.STATUS_ACTIVE)
        .only("container_type", "container_code")
        .order_by("id")[:20]
    )
    types = {clean_code(container.container_type) for container in containers if clean_code(container.container_type)}
    if len(types) == 1:
        return next(iter(types))
    return ""


def _inspect_fbs_physical_pallet(pallet_code: str) -> dict:
    """Show FBS balances still stored in a physical general-warehouse pallet."""
    from fbs.models import FbsBox, FbsStockBalance

    normalized = clean_code(pallet_code)
    if not normalized:
        return {"ok": False, "found": False, "object_type": "pallet"}

    physical_pallet = (
        WarehouseContainer.objects.select_related("agency", "current_location")
        .filter(
            container_code__iexact=normalized,
            container_type__in=PALLET_CONTAINER_TYPES,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        .order_by("id")
        .first()
    )
    if physical_pallet is None:
        return {"ok": False, "found": False, "object_type": "pallet"}

    balances = list(
        FbsStockBalance.objects.filter(
            box__status__in=(FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE),
            box__source_container__parent_container=physical_pallet,
            qty__gt=0,
        )
        .select_related(
            "agency",
            "box__pallet",
            "box__source_container__current_location",
            "sku_ref",
        )
        .order_by("agency_id", "box__box_code", "id")
    )
    if not balances:
        return {"ok": False, "found": False, "object_type": "pallet"}

    rows = [
        {
            "sku": clean_code(balance.sku_code),
            "name": clean_code(balance.name),
            "size": clean_code(balance.size),
            "barcode": clean_code(balance.barcode),
            "goods_type": clean_code(balance.goods_type),
            "qty": int(balance.qty or 0),
            "box_code": clean_code(balance.box.box_code),
            "pallet_code": physical_pallet.container_code,
            "warehouse_state_code": "fbs_stock",
            "source_order_type": "fbs",
        }
        for balance in balances
    ]
    box_codes = sorted(
        {
            clean_code(balance.box.box_code)
            for balance in balances
            if clean_code(balance.box.box_code)
        }
    )
    logical_pallet_codes = sorted(
        {
            clean_code(balance.box.pallet.pallet_code)
            for balance in balances
            if clean_code(balance.box.pallet.pallet_code)
        }
    )
    agency_names = sorted(
        {_agency_display_name(balance.agency) for balance in balances}
    )
    source_locations = {
        int(balance.box.source_container.current_location_id): balance.box.source_container.current_location
        for balance in balances
        if balance.box.source_container.current_location_id
    }
    location = physical_pallet.current_location
    if location is None and len(source_locations) == 1:
        location = next(iter(source_locations.values()))
    location_dict = location_to_dict(location)
    location_code = putaway_location_scan_code(location_dict) if location else ""
    location_label = putaway_location_label(location_dict) if location else "-"
    for row in rows:
        row.update(location_dict)
        row["location"] = location_label

    total_qty = sum(int(balance.qty or 0) for balance in balances)
    available_qty = sum(int(balance.available_qty or 0) for balance in balances)
    reserved_qty = sum(int(balance.reserved_qty or 0) for balance in balances)
    blockers = [
        (
            "На физической паллете находится FBS-остаток. "
            "Для перемещения используйте FBS-короб или штатное задание FBS."
        )
    ]
    if len(source_locations) > 1:
        blockers.append("Короба физической паллеты числятся в нескольких местах.")
    elif location is not None and source_locations and int(location.id) not in source_locations:
        blockers.append("Место физической паллеты не совпадает с местом её FBS-коробов.")
    if len(agency_names) > 1:
        blockers.append("На физической паллете найден FBS-остаток нескольких клиентов.")

    return {
        "ok": True,
        "found": True,
        "object_type": "pallet",
        "stock_contour": "fbs",
        "move_kind": "fbs_physical_pallet",
        "title": f"Паллета {physical_pallet.container_code}",
        "code": physical_pallet.container_code,
        "pallet_code": physical_pallet.container_code,
        "physical_pallet_code": physical_pallet.container_code,
        "fbs_pallet_codes": logical_pallet_codes,
        "agency_id": int(physical_pallet.agency_id or 0),
        "agency_name": _agency_display_name(physical_pallet.agency),
        "location": location_dict,
        "location_id": int(getattr(location, "id", 0) or 0),
        "location_zone_code": clean_code(getattr(location, "zone_code", "")).upper(),
        "location_label": location_label,
        "location_scan_code": location_code,
        "state_labels": ["FBS"],
        "total_qty": total_qty,
        "available_qty": available_qty,
        "reserved_qty": reserved_qty,
        "box_count": len(box_codes),
        "box_codes": box_codes,
        "contexts": [
            "Контур FBS",
            "Учётные FBS-паллеты: " + (", ".join(logical_pallet_codes) or "-"),
            f"Текущее место: {location_label}",
        ],
        "lines": _build_lines(rows),
        "placements": [
            {
                "location": location_label,
                "qty": total_qty,
                "pallet_code": physical_pallet.container_code,
                "box_code": "",
                "state_label": "FBS",
            }
        ],
        "blockers": blockers,
        "is_free": False,
        "can_move": False,
        "status_label": "FBS-остаток",
        "status_kind": "blocked",
        "summary": [
            _summary_item("Контур", "FBS"),
            _summary_item("Клиент", _agency_display_name(physical_pallet.agency)),
            _summary_item("QR места", location_code),
            _summary_item("Количество", f"{total_qty} шт."),
            _summary_item("Доступно", f"{available_qty} шт."),
            _summary_item("Резерв", f"{reserved_qty} шт."),
            _summary_item("Короба", len(box_codes)),
        ],
    }


def _registered_empty_pallet_info(pallet_code: str) -> dict | None:
    normalized = clean_code(pallet_code)
    if not normalized:
        return None
    pallet = (
        WarehouseContainer.objects.select_related("agency", "current_location")
        .filter(
            container_code__iexact=normalized,
            status=WarehouseContainer.STATUS_ACTIVE,
            container_type__in=PALLET_CONTAINER_TYPES,
        )
        .order_by("id")
        .first()
    )
    if pallet is None:
        return None

    location = location_to_dict(pallet.current_location)
    location_label = putaway_location_label(location) if pallet.current_location else "-"
    location_scan_code = putaway_location_scan_code(location) if pallet.current_location else ""
    return {
        "ok": True,
        "found": True,
        "object_type": "pallet",
        "title": f"Паллета {normalized}",
        "code": normalized,
        "pallet_code": normalized,
        "agency_id": int(pallet.agency_id or 0),
        "agency_name": _agency_display_name(pallet.agency),
        "location": location,
        "location_id": int(pallet.current_location_id or 0),
        "location_label": location_label,
        "location_scan_code": location_scan_code,
        "state_labels": [],
        "total_qty": 0,
        "box_count": 0,
        "box_codes": [],
        "contexts": [],
        "lines": [],
        "placements": [],
        "blockers": ["Паллета зарегистрирована, но на ней нет коробов."],
        "is_free": False,
        "can_move": False,
        "status_label": "Пустая",
        "status_kind": "blocked",
        "summary": [
            _summary_item("Клиент", _agency_display_name(pallet.agency)),
            _summary_item("QR места", location_scan_code),
            _summary_item("Количество", "0 шт."),
            _summary_item("Короба", 0),
            _summary_item("Статус", "Пустая"),
        ],
    }


def _snapshot_box_query(box_code: str, *, agency_id: int | None = None):
    normalized = clean_code(box_code)
    qs = _snapshot_base_query().filter(
        Q(container__container_type=WarehouseContainer.TYPE_BOX, container__container_code__iexact=normalized)
        | Q(container_code__iexact=normalized, parent_container__isnull=False)
    )
    if agency_id:
        qs = qs.filter(agency_id=int(agency_id))
    return qs.order_by("agency_id", "id")


def _snapshot_box_codes(snapshot: WarehouseStockSnapshot) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()

    def add(raw_value) -> None:
        code = clean_code(raw_value)
        key = code.casefold()
        if not code or key in seen:
            return
        seen.add(key)
        codes.append(code)

    container = getattr(snapshot, "container", None)
    if container is not None and container.container_type == WarehouseContainer.TYPE_BOX:
        add(getattr(container, "container_code", ""))
    if getattr(snapshot, "parent_container_id", None):
        add(snapshot.container_code)
    return codes


def _resolve_box_scan(scan_value: str, *, agency_id: int | None = None) -> tuple[str, list[WarehouseStockSnapshot], list[str]]:
    normalized = readable_scan_text(scan_value)
    for lookup_value in _scan_lookup_values(scan_value):
        exact_snapshots = list(_snapshot_box_query(lookup_value, agency_id=agency_id))
        if exact_snapshots:
            rows = [normalize_stock_row_from_snapshot(snapshot) for snapshot in exact_snapshots]
            box_code = _single_value(row.get("box_code") for row in rows if row)
            return box_code if box_code != "Несколько" else lookup_value, exact_snapshots, []

    lookup_tokens = _pallet_lookup_tokens(scan_value)
    if not lookup_tokens:
        return normalized, [], []

    token_filter = Q()
    for token in lookup_tokens:
        token_filter |= Q(container__container_code__icontains=token) | Q(container_code__icontains=token)
    qs = _snapshot_base_query().filter(token_filter).filter(
        Q(container__container_type=WarehouseContainer.TYPE_BOX) | Q(parent_container__isnull=False)
    )
    if agency_id:
        qs = qs.filter(agency_id=int(agency_id))

    matched_codes: set[str] = set()
    for snapshot in qs.order_by("agency_id", "id")[:300]:
        for candidate_code in _snapshot_box_codes(snapshot):
            if _same_box_code_scan(scan_value, candidate_code):
                matched_codes.add(candidate_code)

    if not matched_codes:
        return normalized, [], []
    if len(matched_codes) > 1:
        return normalized, [], sorted(matched_codes, key=lambda item: item.casefold())

    resolved = next(iter(matched_codes))
    return resolved, list(_snapshot_box_query(resolved, agency_id=agency_id)), []


def _snapshot_item_query(scan_value: str):
    query = Q()
    for value in _scan_lookup_values(scan_value):
        query |= (
            Q(sku_code__iexact=value)
            | Q(sku_ref__sku_code__iexact=value)
            | Q(sku_ref__code__iexact=value)
            | Q(sku_ref__barcodes__value__iexact=value)
            | Q(barcode__iexact=value)
            | Q(marking_code__iexact=value)
        )
    if not query:
        return WarehouseStockSnapshot.objects.none()
    return _snapshot_base_query().filter(query).distinct().order_by("agency_id", "sku_code", "id")


def _location_snapshot_query(location: dict):
    zone = clean_code(location.get("zone")).upper()
    if not zone:
        return WarehouseStockSnapshot.objects.none()
    # A consumed processing row is an audit trace, not stock that physically
    # occupies its old location.  Keep it available to history screens, but do
    # not let it make Reachtruck Free report an empty cell as occupied.
    snapshot_query = _snapshot_base_query().exclude(
        warehouse_state_code__in=NON_OCCUPYING_LOCATION_STATES
    )
    row = parse_int_value(location.get("row"))
    section = parse_int_value(location.get("section"))
    tier = parse_int_value(location.get("tier"))
    cell = parse_int_value(location.get("cell"))
    if zone == "OS":
        if not (row and section and tier and cell):
            return WarehouseStockSnapshot.objects.none()
        return snapshot_query.filter(
            location__zone_code__iexact=zone,
            location__row_no=row,
            location__section_no=section,
            location__tier_no=tier,
            location__cell_no=cell,
        )
    if zone == "MR" and row:
        return snapshot_query.filter(location__zone_code__iexact=zone, location__row_no=row)
    return snapshot_query.filter(Q(location__zone_code__iexact=zone) | Q(location__isnull=True, zone_code__iexact=zone))


def inspect_location(scan_value: str) -> dict:
    location_dict, _error = parse_location_scan(scan_value)
    if not location_dict:
        return {"ok": False, "found": False, "object_type": "location", "error": "Место не распознано."}

    location = _location_from_dict(location_dict)
    display_location = location_to_dict(location) if location is not None else location_dict
    location_label = putaway_location_label(display_location)
    location_scan_code = putaway_location_scan_code(display_location)
    snapshots = list(_location_snapshot_query(display_location).order_by("agency_id", "sku_code", "id")[:800])
    rows = [normalize_stock_row_from_snapshot(snapshot) for snapshot in snapshots]
    rows = [row for row in rows if row]
    pallet_codes = sorted({clean_code(row.get("pallet_code")) for row in rows if clean_code(row.get("pallet_code"))})
    box_codes = sorted({clean_code(row.get("box_code")) for row in rows if clean_code(row.get("box_code"))})
    sku_codes = sorted({clean_code(row.get("sku")) for row in rows if clean_code(row.get("sku"))})
    agencies = sorted({_agency_display_name(snapshot.agency) for snapshot in snapshots if _agency_display_name(snapshot.agency)})
    states = sorted({clean_code(snapshot.warehouse_state_code) for snapshot in snapshots if clean_code(snapshot.warehouse_state_code)})
    total_qty = sum(int(snapshot.qty or 0) for snapshot in snapshots)
    # A pallet can physically occupy a cell without any remaining product.
    # Show its owner as well as snapshot owners; do not call such a cell empty.
    if location is not None and str(location.zone_code or "").upper() == "OS":
        occupants = list(
            active_os_physical_containers().filter(current_location=location).select_related("agency")
        )
        agencies = sorted(set(agencies) | {
            _agency_display_name(container.agency) for container in occupants
            if _agency_display_name(container.agency)
        })
        pallet_codes = sorted(set(pallet_codes) | {
            container.container_code for container in occupants
            if container.container_type in PALLET_CONTAINER_TYPES
        })
        box_codes = sorted(set(box_codes) | {
            container.container_code for container in occupants
            if container.container_type == WarehouseContainer.TYPE_BOX
        })
    occupancy_message = os_location_occupancy_message(location) if location is not None else ""
    is_occupied = bool(total_qty or occupancy_message)
    return {
        "ok": True,
        "found": True,
        "object_type": "location",
        "title": f"Место {location_scan_code or clean_code(scan_value)}",
        "code": location_scan_code or clean_code(scan_value),
        "location_label": location_label,
        "location_scan_code": location_scan_code,
        "total_qty": total_qty,
        "lines": _build_lines(rows),
        "placements": _build_placements(rows),
        "contexts": _stock_contexts(rows),
        "blockers": [occupancy_message] if occupancy_message and not total_qty else [],
        "can_move": False,
        "status_label": "Занято" if is_occupied else "Пусто",
        "status_kind": "blocked" if is_occupied else "free",
        "summary": [
            _summary_item("Паллеты", len(pallet_codes)),
            _summary_item("Короба", len(box_codes)),
            _summary_item("Товары", len(sku_codes)),
            _summary_item("Количество", f"{total_qty} шт."),
            _summary_item("Клиенты", ", ".join(agencies)),
            _summary_item(
                "Статус",
                ", ".join(_state_label(state) for state in states)
                if states
                else ("Занято" if occupancy_message else "Пусто"),
            ),
        ],
    }


def _active_reachtruck_blockers(*, agency_id: int, pallet_code: str, box_codes: list[str]) -> list[str]:
    blockers: list[str] = []
    normalized = clean_code(pallet_code)
    if normalized:
        open_tasks = (
            MoveTask.objects.select_related("request")
            .filter(
                request__agency_id=agency_id,
                status__in=[MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS],
            )
            .filter(Q(pallet_code__iexact=normalized) | Q(payload__pallet_code=normalized))
            .exclude(request__status="canceled")
            .order_by("id")
        )
        for task in open_tasks[:3]:
            label = clean_code(task.legacy_order_id) or f"#{task.pk}"
            blockers.append(f"Активное задание ричтракера {label}")

        pallet_lock = (
            PalletLock.objects.filter(
                agency_id=agency_id,
                pallet_code__iexact=normalized,
                status=PalletLock.STATUS_ACTIVE,
            )
            .exclude(move_task__status__in=_FINAL_TASK_STATUSES)
            .select_related("locked_by")
            .order_by("id")
            .first()
        )
        if pallet_lock:
            who = clean_code(getattr(pallet_lock.locked_by, "username", "")) if pallet_lock.locked_by else ""
            blockers.append(f"Паллета уже взята в работу{f' пользователем {who}' if who else ''}")

    if box_codes:
        claims = (
            BoxClaim.objects.filter(
                agency_id=agency_id,
                status=BoxClaim.STATUS_CLAIMED,
                box_code__in=box_codes,
            )
            .exclude(move_task__status__in=_FINAL_TASK_STATUSES)
            .order_by("id")
        )
        for claim in claims[:3]:
            blockers.append(f"Короб {claim.box_code} уже взят в работу")
    return blockers


def inspect_pallet(pallet_code: str) -> dict:
    normalized = clean_code(pallet_code)
    if not normalized:
        return {"ok": False, "found": False, "error": "Отсканируйте паллету."}

    normalized, snapshots, ambiguous_codes = _resolve_pallet_scan(normalized)
    if ambiguous_codes:
        return {
            "ok": False,
            "found": False,
            "pallet_code": normalized,
            "error": "По этому QR найдено несколько паллет. Отсканируйте прямой код паллеты.",
            "ambiguous_codes": ambiguous_codes,
        }
    if not snapshots:
        flow_match = _flow_match_for_scan(normalized, object_type="pallet")
        materialized_pallet_code = _materialize_flow_pallet_match(flow_match)
        if materialized_pallet_code:
            normalized, snapshots, ambiguous_codes = _resolve_pallet_scan(materialized_pallet_code)
        if not snapshots:
            if flow_match:
                return _flow_pallet_info(flow_match)
            fbs_physical_pallet = _inspect_fbs_physical_pallet(normalized)
            if fbs_physical_pallet.get("found"):
                return fbs_physical_pallet
            registered_empty_pallet = _registered_empty_pallet_info(normalized)
            if registered_empty_pallet:
                return registered_empty_pallet
            return {
                "ok": False,
                "found": False,
                "pallet_code": normalized,
                "error": "Паллета не найдена на складе.",
            }

    agency_ids = {int(snapshot.agency_id or 0) for snapshot in snapshots if snapshot.agency_id}
    if len(agency_ids) > 1:
        return {
            "ok": True,
            "found": True,
            "object_type": "pallet",
            "title": f"Паллета {normalized}",
            "code": normalized,
            "pallet_code": normalized,
            "is_free": False,
            "can_move": False,
            "status_label": "Есть привязки",
            "status_kind": "blocked",
            "blockers": ["Паллета найдена у нескольких клиентов. Нужна ручная проверка."],
            "summary": [_summary_item("Клиент", "Несколько")],
            "lines": [],
            "placements": [],
        }

    rows = [normalize_stock_row_from_snapshot(snapshot) for snapshot in snapshots]
    rows = [row for row in rows if row]
    first = snapshots[0]
    location = first.location
    location_dict = location_to_dict(location)
    location_label, location_scan_code, _location_count = _snapshot_location_summary(snapshots)
    location_ids = {int(snapshot.location_id or 0) for snapshot in snapshots}
    states = sorted({str(snapshot.warehouse_state_code or "").strip() for snapshot in snapshots})
    box_codes = sorted({clean_code(row.get("box_code")) for row in rows if clean_code(row.get("box_code"))})
    contexts = _stock_contexts(rows)

    blockers: list[str] = []
    if len(location_ids) > 1:
        blockers.append("Паллета разбита по нескольким местам.")
    for snapshot in snapshots:
        if snapshot.active_operation_id:
            operation = snapshot.active_operation
            if operation and clean_code(operation.status) in _FINAL_OPERATION_STATUSES:
                continue
            label = f"#{snapshot.active_operation_id}"
            if operation:
                label = f"{operation.operation_type} {label}"
            blockers.append(f"Активная складская операция {label}")
            break
    non_free_states = sorted(
        {
            str(snapshot.warehouse_state_code or "").strip()
            for snapshot in snapshots
            if str(snapshot.warehouse_state_code or "").strip() not in FREE_SOURCE_STATES
        }
    )
    if non_free_states:
        state_label = ", ".join(_state_label(state) for state in non_free_states)
        location_note = f" (место {location_scan_code})" if location_scan_code else ""
        blockers.append(f"Не на свободном хранении: {state_label}{location_note}.")
    receiving_context_ids = sorted(
        {
            clean_code(snapshot.source_context_id)
            for snapshot in snapshots
            if clean_code(snapshot.source_context_type).lower() == "receiving"
            and clean_code(snapshot.warehouse_state_code) == WarehouseStateCode.PLACED_IN_RECEIVING.value
            and clean_code(snapshot.zone_code).upper() == "PR"
            and clean_code(snapshot.source_context_id)
        }
    )
    for receiving_order_id in receiving_context_ids:
        takeout_state = ReceivingWorkflowService.receiving_flow_pallet_takeout_state(
            order_id=receiving_order_id,
            pallet_code=normalized,
        )
        if not takeout_state.get("can_take", True):
            blockers.append(
                clean_code(takeout_state.get("reason"))
                or f"Приемка {receiving_order_id}: паллета еще открыта."
            )
    if any(int(snapshot.processing_reserved_qty or 0) > 0 for snapshot in snapshots):
        blockers.append("Есть резерв под обработку.")
    if any(int(snapshot.shipping_reserved_qty or 0) > 0 for snapshot in snapshots):
        blockers.append("Есть резерв под отгрузку.")
    if any(int(snapshot.other_reserved_qty or 0) > 0 for snapshot in snapshots):
        blockers.append("Есть ручной резерв.")
    if first.agency_id:
        if active_reserved_pallet_codes_for_agency(int(first.agency_id), [normalized]):
            blockers.append("Есть резерв под отгрузку.")
        blockers.extend(
            _active_reachtruck_blockers(
                agency_id=int(first.agency_id),
                pallet_code=normalized,
                box_codes=box_codes,
            )
        )
    blockers = list(dict.fromkeys(blockers))

    return {
        "ok": True,
        "found": True,
        "object_type": "pallet",
        "title": f"Паллета {normalized}",
        "code": normalized,
        "pallet_code": normalized,
        "agency_id": int(first.agency_id or 0),
        "agency_name": _agency_display_name(first.agency),
        "location": location_dict,
        "location_id": int(first.location_id or 0),
        "location_label": location_label,
        "location_scan_code": location_scan_code,
        "state_labels": [_state_label(state) for state in states],
        "total_qty": sum(int(snapshot.qty or 0) for snapshot in snapshots),
        "box_count": len(box_codes),
        "box_codes": box_codes,
        "contexts": contexts,
        "lines": _build_lines(rows),
        "placements": _build_placements(rows),
        "blockers": blockers,
        "is_free": not blockers,
        "can_move": not blockers,
        "status_label": "Свободна" if not blockers else "Не свободна",
        "status_kind": "free" if not blockers else "blocked",
        "summary": [
            _summary_item("Клиент", _agency_display_name(first.agency)),
            _summary_item("QR места", location_scan_code),
            _summary_item("Количество", f"{sum(int(snapshot.qty or 0) for snapshot in snapshots)} шт."),
            _summary_item("Короба", len(box_codes)),
            _summary_item("Статус", ", ".join(_state_label(state) for state in states)),
        ],
    }


def inspect_box(box_code: str) -> dict:
    normalized = clean_code(box_code)
    if not normalized:
        return {"ok": False, "found": False, "error": "Отсканируйте короб."}

    normalized, snapshots, ambiguous_codes = _resolve_box_scan(normalized)
    if ambiguous_codes:
        return {
            "ok": False,
            "found": False,
            "object_type": "box",
            "code": normalized,
            "title": "Короб не найден",
            "error": "По этому QR найдено несколько коробов. Отсканируйте прямой код короба.",
            "ambiguous_codes": ambiguous_codes,
        }
    if not snapshots:
        flow_match = _flow_match_for_scan(normalized, object_type="box")
        materialized_pallet_code = _materialize_flow_pallet_match(flow_match)
        if materialized_pallet_code:
            flow_box = flow_match.get("box") if isinstance(flow_match, dict) else None
            flow_box_code = clean_code(flow_box.get("code")) if isinstance(flow_box, dict) else normalized
            normalized, snapshots, ambiguous_codes = _resolve_box_scan(flow_box_code)
        if not snapshots:
            if flow_match:
                return _flow_box_info(flow_match)
            return {
                "ok": False,
                "found": False,
                "object_type": "box",
                "code": normalized,
                "title": "Короб не найден",
                "error": "Короб не найден на складе.",
            }

    rows = [normalize_stock_row_from_snapshot(snapshot) for snapshot in snapshots]
    rows = [row for row in rows if row]
    first = snapshots[0]
    location_label, location_scan_code, _location_count = _snapshot_location_summary(snapshots)
    states = sorted({str(snapshot.warehouse_state_code or "").strip() for snapshot in snapshots})
    pallet_code = _single_value(row.get("pallet_code") for row in rows)
    box_codes = sorted({clean_code(row.get("box_code")) for row in rows if clean_code(row.get("box_code"))})
    blockers = _stock_binding_labels(
        snapshots,
        pallet_code="" if pallet_code == "Несколько" else pallet_code,
        box_codes=box_codes,
    )
    location_ids = {int(snapshot.location_id or 0) for snapshot in snapshots}
    agency_ids = {int(snapshot.agency_id or 0) for snapshot in snapshots if snapshot.agency_id}
    parent_ids = {int(snapshot.parent_container_id or 0) for snapshot in snapshots}
    if len(location_ids) > 1:
        blockers.append("Короб разбит по нескольким местам.")
    if len(agency_ids) > 1:
        blockers.append("Короб найден у нескольких клиентов.")
    if len(parent_ids) > 1:
        blockers.append("Короб привязан к нескольким паллетам.")
    non_free_states = sorted(
        {
            str(snapshot.warehouse_state_code or "").strip()
            for snapshot in snapshots
            if str(snapshot.warehouse_state_code or "").strip() not in FREE_SOURCE_STATES
        }
    )
    if non_free_states:
        blockers.append(
            "Не на свободном хранении: "
            + ", ".join(_state_label(state) for state in non_free_states)
            + "."
        )
    if not any(
        snapshot.container_id
        and snapshot.container
        and snapshot.container.container_type == WarehouseContainer.TYPE_BOX
        for snapshot in snapshots
    ):
        blockers.append("У короба отсутствует физический складской контейнер.")
    blockers = list(dict.fromkeys(blockers))
    total_qty = sum(int(snapshot.qty or 0) for snapshot in snapshots)
    return {
        "ok": True,
        "found": True,
        "object_type": "box",
        "stock_contour": "fbo",
        "move_kind": "box",
        "title": f"Короб {normalized}",
        "code": normalized,
        "box_code": normalized,
        "pallet_code": "" if pallet_code == "-" else pallet_code,
        "agency_id": int(first.agency_id or 0),
        "agency_name": _agency_display_name(first.agency),
        "location_label": location_label,
        "location_scan_code": location_scan_code,
        "state_labels": [_state_label(state) for state in states],
        "total_qty": total_qty,
        "contexts": _stock_contexts(rows),
        "lines": _build_lines(rows),
        "placements": _build_placements(rows),
        "blockers": blockers,
        "is_free": not blockers,
        "can_move": not blockers,
        "status_label": "Свободен" if not blockers else "Не свободен",
        "status_kind": "free" if not blockers else "blocked",
        "summary": [
            _summary_item("Клиент", _agency_display_name(first.agency)),
            _summary_item("Паллета", pallet_code),
            _summary_item("QR места", location_scan_code),
            _summary_item("Количество", f"{total_qty} шт."),
            _summary_item("Статус", ", ".join(_state_label(state) for state in states)),
        ],
    }


def inspect_item(scan_value: str) -> dict:
    normalized = readable_scan_text(scan_value)
    if not normalized:
        return {"ok": False, "found": False, "error": "Отсканируйте товар."}

    snapshots = list(_snapshot_item_query(scan_value)[:500])
    if not snapshots:
        return {
            "ok": False,
            "found": False,
            "object_type": "item",
            "code": normalized,
            "title": "Товар не найден",
            "error": "Товар не найден на складе.",
        }

    rows = [normalize_stock_row_from_snapshot(snapshot) for snapshot in snapshots]
    rows = [row for row in rows if row]
    first = snapshots[0]
    location_label, location_scan_code, location_count = _snapshot_location_summary(snapshots)
    states = sorted({str(snapshot.warehouse_state_code or "").strip() for snapshot in snapshots})
    sku = _single_value(row.get("sku") for row in rows)
    name = _single_value((row.get("name") for row in rows), default="")
    barcode = _single_value(row.get("barcode") for row in rows)
    size = _single_value(row.get("size") for row in rows)
    pallet_codes = sorted({clean_code(row.get("pallet_code")) for row in rows if clean_code(row.get("pallet_code"))})
    box_codes = sorted({clean_code(row.get("box_code")) for row in rows if clean_code(row.get("box_code"))})
    blockers = _stock_binding_labels(snapshots, box_codes=box_codes)
    total_qty = sum(int(snapshot.qty or 0) for snapshot in snapshots)
    available_qty = sum(int(snapshot.available_qty or 0) for snapshot in snapshots)
    title_code = sku if sku not in {"", "-"} else normalized
    return {
        "ok": True,
        "found": True,
        "object_type": "item",
        "title": f"Товар {title_code}",
        "code": normalized,
        "agency_id": int(first.agency_id or 0),
        "agency_name": _single_value(_agency_display_name(snapshot.agency) for snapshot in snapshots),
        "location_label": location_label,
        "location_scan_code": location_scan_code,
        "state_labels": [_state_label(state) for state in states],
        "total_qty": total_qty,
        "available_qty": available_qty,
        "contexts": _stock_contexts(rows),
        "lines": _build_lines(rows),
        "placements": _build_placements(rows),
        "blockers": blockers,
        "can_move": False,
        "status_label": "Есть привязки" if blockers else "",
        "status_kind": "blocked" if blockers else "",
        "summary": [
            _summary_item("Клиент", _single_value(_agency_display_name(snapshot.agency) for snapshot in snapshots)),
            _summary_item("Артикул", sku),
            _summary_item("Наименование", name),
            _summary_item("ШК", barcode),
            _summary_item("Размер", size),
            _summary_item("Количество", f"{total_qty} шт."),
            _summary_item("Доступно", f"{available_qty} шт."),
            _summary_item("Место", location_label if location_count <= 1 else f"{location_label}: {location_count}"),
            _summary_item("QR места", location_scan_code),
            _summary_item("Паллеты", len(pallet_codes)),
            _summary_item("Короба", len(box_codes)),
            _summary_item("Статус", ", ".join(_state_label(state) for state in states)),
        ],
    }


def inspect_scan(scan_value: str) -> dict:
    normalized = clean_code(scan_value)
    if not normalized:
        return {
            "ok": False,
            "found": False,
            "object_type": "",
            "title": "Объект не найден",
            "error": "Отсканируйте место, товар, короб или паллету.",
        }

    from fbs.services.free_relocation import inspect_fbs_pallet
    from reachtruck_free.box_relocation import inspect_fbs_box

    fbs_pallet_info = inspect_fbs_pallet(normalized)
    if fbs_pallet_info.get("found"):
        return fbs_pallet_info

    fbs_box_info = inspect_fbs_box(normalized)
    if fbs_box_info.get("found"):
        return fbs_box_info

    exact_container_type = _exact_container_type_for_scan(normalized)
    if exact_container_type in PALLET_CONTAINER_TYPES:
        return inspect_pallet(normalized)
    if exact_container_type == WarehouseContainer.TYPE_BOX:
        return inspect_box(normalized)

    location_info = inspect_location(normalized)
    if location_info.get("found"):
        return location_info

    item_info = inspect_item(normalized)
    if item_info.get("found"):
        return item_info

    pallet_info = inspect_pallet(normalized)
    if pallet_info.get("found"):
        return pallet_info

    box_info = inspect_box(normalized)
    if box_info.get("found"):
        return box_info

    return {
        "ok": False,
        "found": False,
        "object_type": "",
        "title": "Объект не найден",
        "code": readable_scan_text(normalized),
        "error": "Объект не найден на складе.",
    }


def moving_context(request) -> dict:
    session = getattr(request, "session", None)
    if session is None:
        return {}
    raw = session.get(SESSION_MOVE_KEY) or {}
    operation_id = raw.get("operation_id") if isinstance(raw, dict) else None
    if not operation_id:
        return {}
    operation = (
        WarehouseOperation.objects.select_related("agency", "source_location", "destination_location")
        .filter(
            id=operation_id,
            operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
        )
        .first()
    )
    if operation is None:
        session.pop(SESSION_MOVE_KEY, None)
        return {}
    if operation.context_type == "fbs_free_box":
        from reachtruck_free.box_relocation import fbs_box_relocation_context

        context = fbs_box_relocation_context(operation)
        if context:
            return context
        session.pop(SESSION_MOVE_KEY, None)
        return {}
    if operation.context_type == "fbs_free_relocation":
        from fbs.services.free_relocation import fbs_relocation_context

        context = fbs_relocation_context(operation)
        if context:
            context.setdefault(
                "moving_title",
                f"Паллета {clean_code(context.get('pallet_code'))} в пути",
            )
            return context
        session.pop(SESSION_MOVE_KEY, None)
        return {}
    source = location_to_dict(operation.source_location)
    snapshots = list(
        WarehouseStockSnapshot.objects.filter(active_operation=operation, is_archived=False, qty__gt=0)
        .select_related("sku_ref", "container", "parent_container", "location")
        .order_by("id")
    )
    rows = [normalize_stock_row_from_snapshot(snapshot) for snapshot in snapshots]
    rows = [row for row in rows if row]
    if operation.context_type == "reachtruck_free_box":
        from reachtruck_free.box_relocation import general_box_relocation_context

        return general_box_relocation_context(operation, lines=_build_lines(rows))
    return {
        "moving": True,
        "operation": operation,
        "pallet_code": clean_code(operation.context_id),
        "moving_title": f"Паллета {clean_code(operation.context_id)} в пути",
        "source_label": putaway_location_label(source),
        "source_scan_code": putaway_location_scan_code(source),
        "total_qty": sum(int(snapshot.qty or 0) for snapshot in snapshots),
        "lines": _build_lines(rows),
    }


def resume_active_free_move(request, *, pallet_code: str, role: str) -> bool:
    if role not in MOVE_ROLES:
        return False
    normalized_pallet = clean_code(pallet_code)
    if not normalized_pallet:
        return False
    from reachtruck_free.box_relocation import active_box_relocation_for_scan

    box_operation = active_box_relocation_for_scan(normalized_pallet)
    if box_operation is not None:
        box_code = clean_code(getattr(box_operation, "free_box_code", "")) or clean_code(
            box_operation.context_id
        )
        request.session[SESSION_MOVE_KEY] = {
            "operation_id": int(box_operation.id),
            "object_code": box_code,
            "box_code": box_code,
            "move_kind": (
                "fbs_box"
                if box_operation.context_type == "fbs_free_box"
                else "box"
            ),
        }
        request.session.pop(SESSION_INSPECTION_KEY, None)
        request.session.modified = True
        return True
    from fbs.services.free_relocation import active_relocation_for_pallet_scan

    fbs_operation = active_relocation_for_pallet_scan(normalized_pallet)
    if fbs_operation is not None:
        request.session[SESSION_MOVE_KEY] = {
            "operation_id": int(fbs_operation.id),
            "pallet_code": clean_code(fbs_operation.fbs_pallet_code),
            "move_kind": "fbs",
        }
        request.session.pop(SESSION_INSPECTION_KEY, None)
        request.session.modified = True
        return True
    operation = (
        WarehouseOperation.objects.filter(
            operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
            context_type="reachtruck_free",
            context_id__iexact=normalized_pallet,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
        )
        .order_by("-id")
        .only("id", "context_id")
        .first()
    )
    if operation is None:
        return False
    request.session[SESSION_MOVE_KEY] = {
        "operation_id": int(operation.id),
        "pallet_code": clean_code(operation.context_id),
    }
    request.session.pop(SESSION_INSPECTION_KEY, None)
    request.session.modified = True
    return True


def active_move_same_pallet_scan(request, scan_value: str) -> bool:
    session = getattr(request, "session", None)
    raw = session.get(SESSION_MOVE_KEY) if session is not None else None
    if not isinstance(raw, dict) or not raw.get("operation_id"):
        return False
    scanned = clean_code(scan_value)
    if not scanned:
        return False
    operation = (
        WarehouseOperation.objects.filter(
            id=int(raw["operation_id"]),
            operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
        )
        .only("context_id")
        .first()
    )
    if operation is None:
        return False
    object_code = (
        clean_code(raw.get("object_code"))
        or clean_code(raw.get("box_code"))
        or clean_code(raw.get("pallet_code"))
        or clean_code(operation.context_id)
    )
    if not object_code:
        return False
    if operation.context_type in {"fbs_free_box", "reachtruck_free_box"}:
        return scanned.casefold() == object_code.casefold() or _same_box_code_scan(scanned, object_code)
    return scanned.casefold() == object_code.casefold() or _same_pallet_code_scan(scanned, object_code)


def start_free_move(request, *, pallet_code: str, role: str) -> FreeMoveResult:
    if role not in MOVE_ROLES:
        raise ValueError("Свободное перемещение доступно только водителю ричтрака.")
    from fbs.exceptions import FbsError
    from fbs.services.free_relocation import inspect_fbs_pallet, start_fbs_free_relocation
    from reachtruck_free.box_relocation import (
        inspect_fbs_box,
        start_fbs_box_relocation,
        start_general_box_relocation,
    )

    fbs_box_info = inspect_fbs_box(pallet_code)
    if fbs_box_info.get("found"):
        if not fbs_box_info.get("can_move"):
            blockers = fbs_box_info.get("blockers") or [
                fbs_box_info.get("error") or "FBS-короб недоступен для перемещения."
            ]
            raise ValueError("; ".join(str(item) for item in blockers if str(item).strip()))
        try:
            operation = start_fbs_box_relocation(
                box_id=int(fbs_box_info["fbs_box_id"]),
                performed_by=request.user,
                expected_location_id=int(fbs_box_info.get("location_id") or 0) or None,
            )
        except FbsError as exc:
            raise ValueError(str(exc)) from exc
        box_code = clean_code(fbs_box_info.get("box_code") or pallet_code)
        request.session[SESSION_MOVE_KEY] = {
            "operation_id": int(operation.id),
            "object_code": box_code,
            "box_code": box_code,
            "agency_id": int(fbs_box_info.get("agency_id") or 0),
            "move_kind": "fbs_box",
        }
        request.session.pop(SESSION_INSPECTION_KEY, None)
        request.session.modified = True
        return FreeMoveResult(
            operation=operation,
            message=(
                "FBS-короб взят в свободное перемещение. "
                "Сканируйте FBS-паллету или PR-стеллаж назначения."
            ),
        )

    fbs_info = inspect_fbs_pallet(pallet_code)
    if fbs_info.get("found"):
        if not fbs_info.get("can_move"):
            blockers = fbs_info.get("blockers") or [
                fbs_info.get("error") or "FBS-паллета недоступна для перемещения."
            ]
            raise ValueError(
                "; ".join(str(item) for item in blockers if str(item).strip())
            )
        try:
            operation = start_fbs_free_relocation(
                pallet_id=int(fbs_info["fbs_pallet_id"]),
                performed_by=request.user,
                expected_location_id=int(fbs_info.get("location_id") or 0) or None,
            )
        except FbsError as exc:
            raise ValueError(str(exc)) from exc
        request.session[SESSION_MOVE_KEY] = {
            "operation_id": int(operation.id),
            "pallet_code": clean_code(fbs_info.get("pallet_code") or pallet_code),
            "agency_id": int(fbs_info.get("agency_id") or 0),
            "move_kind": "fbs",
        }
        request.session.pop(SESSION_INSPECTION_KEY, None)
        request.session.modified = True
        return FreeMoveResult(
            operation=operation,
            message=(
                "FBS-паллета взята в свободное перемещение. "
                "Сканируйте новое точное место OS, PR или OTG."
            ),
        )

    box_info = inspect_box(pallet_code)
    if box_info.get("found"):
        if not box_info.get("can_move"):
            blockers = box_info.get("blockers") or [
                box_info.get("error") or "Короб недоступен для перемещения."
            ]
            raise ValueError("; ".join(str(item) for item in blockers if str(item).strip()))
        operation = start_general_box_relocation(
            box_code=clean_code(box_info.get("box_code") or pallet_code),
            performed_by=request.user,
            expected_location_id=int(box_info.get("location_id") or 0) or None,
        )
        box_code = clean_code(box_info.get("box_code") or pallet_code)
        request.session[SESSION_MOVE_KEY] = {
            "operation_id": int(operation.id),
            "object_code": box_code,
            "box_code": box_code,
            "agency_id": int(box_info.get("agency_id") or 0),
            "move_kind": "box",
        }
        request.session.pop(SESSION_INSPECTION_KEY, None)
        request.session.modified = True
        return FreeMoveResult(
            operation=operation,
            message=(
                "Короб взят в свободное перемещение. "
                "Сканируйте точное место OS, PR, ОТГ или паллету назначения."
            ),
        )

    info = inspect_pallet(pallet_code)
    if not info.get("can_move"):
        blockers = info.get("blockers") or [info.get("error") or "Паллета недоступна для перемещения."]
        raise ValueError("; ".join(str(item) for item in blockers if str(item).strip()))
    agency_id = int(info.get("agency_id") or 0)
    if not agency_id:
        raise ValueError("Не найден клиент паллеты.")
    operation = WarehouseWritePathService.start_free_pallet_relocation(
        agency=snapshots_agency(agency_id),
        pallet_code=clean_code(pallet_code),
        performed_by=request.user,
        expected_location_id=int(info.get("location_id") or 0) or None,
    )
    request.session[SESSION_MOVE_KEY] = {
        "operation_id": int(operation.id),
        "pallet_code": clean_code(pallet_code),
        "agency_id": agency_id,
    }
    request.session.pop(SESSION_INSPECTION_KEY, None)
    request.session.modified = True
    return FreeMoveResult(operation=operation, message="Паллета взята в свободное перемещение. Сканируйте новое место.")


def snapshots_agency(agency_id: int):
    from sku.models import Agency

    return Agency.objects.get(id=int(agency_id))


def complete_free_move(request, *, scan_value: str) -> FreeMoveResult:
    session = getattr(request, "session", None)
    raw = session.get(SESSION_MOVE_KEY) if session is not None else None
    if not isinstance(raw, dict) or not raw.get("operation_id"):
        raise ValueError("Нет активного свободного перемещения.")
    active_operation = (
        WarehouseOperation.objects.filter(
            id=int(raw["operation_id"]),
            operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
        )
        .only("context_type", "source_zone_code")
        .first()
    )
    operation_context = active_operation.context_type if active_operation else ""
    if operation_context == "fbs_free_box":
        from fbs.exceptions import FbsError
        from reachtruck_free.box_relocation import complete_fbs_box_relocation

        try:
            operation = complete_fbs_box_relocation(
                operation_id=int(raw["operation_id"]),
                destination_scan=scan_value,
                performed_by=request.user,
            )
        except FbsError as exc:
            raise ValueError(str(exc)) from exc
        if session is not None:
            session.pop(SESSION_MOVE_KEY, None)
            session.pop(SESSION_INSPECTION_KEY, None)
            session.modified = True
        destination_zone = str(operation.destination_zone_code or "").strip().upper()
        destination_label = str(
            getattr(operation.destination_location, "location_code", "") or "PR"
        ).strip()
        return FreeMoveResult(
            operation=operation,
            message=(
                f"FBS-короб размещен на PR-стеллаже {destination_label}. "
                "Дополнительное размещение внутри стеллажа не требуется."
                if destination_zone == "PR"
                else "FBS-короб переложен на паллету назначения. Резерв не создавался."
            ),
        )
    if operation_context == "reachtruck_free_box":
        from reachtruck_free.box_relocation import complete_general_box_relocation

        operation = complete_general_box_relocation(
            operation_id=int(raw["operation_id"]),
            destination_scan=scan_value,
            performed_by=request.user,
        )
        if session is not None:
            session.pop(SESSION_MOVE_KEY, None)
            session.pop(SESSION_INSPECTION_KEY, None)
            session.modified = True
        task_payload = (
            operation.tasks.order_by("id").values_list("payload", flat=True).first()
            or {}
        )
        destination_code = clean_code(task_payload.get("destination_location_code"))
        destination_is_exact = bool(task_payload.get("destination_exact_place"))
        return FreeMoveResult(
            operation=operation,
            message=(
                f"Короб перемещён на точное место {destination_code}. "
                "Количество товара и резервы не изменены."
                if destination_is_exact
                else "Короб переложен на паллету назначения. "
                "Количество товара и резервы не изменены."
            ),
        )
    if operation_context == "fbs_free_relocation":
        from fbs.exceptions import FbsError
        from fbs.services.free_relocation import (
            allowed_fbs_destination_zones,
            complete_fbs_free_relocation,
        )

        source_zone = clean_code(active_operation.source_zone_code).upper()
        allowed_destinations = allowed_fbs_destination_zones(source_zone)
        destination_location_id = None
        exact_location = WarehouseLocation.objects.filter(
            warehouse_code="MSK",
            location_code__iexact=normalize_operational_location_scan(scan_value),
            zone_code__in=("PR", "OTG"),
        ).first()
        if exact_location is not None:
            expected_zone = clean_code(exact_location.zone_code).upper()
            try:
                exact_location = resolve_operational_location_scan(
                    scan_value,
                    expected_zone=expected_zone,
                    require_fbs=True,
                )
                require_concrete_movement_location(
                    exact_location,
                    purpose="свободного перемещения FBS",
                )
            except ValidationError as exc:
                raise ValueError("; ".join(exc.messages)) from exc
            destination = location_to_dict(exact_location)
            zone = expected_zone
            destination_location_id = int(exact_location.id)
        else:
            destination, error = parse_destination_scan(scan_value)
            if error:
                raise ValueError(error)
            zone = str(destination.get("zone") or "").strip().upper()
            if zone not in allowed_destinations:
                expected = ", ".join(sorted(allowed_destinations)) or "недоступна"
                raise ValueError(
                    f"Из зоны {source_zone or 'неизвестно'} FBS-паллету можно "
                    f"переместить только в {expected}."
                )

        try:
            operation = complete_fbs_free_relocation(
                operation_id=int(raw["operation_id"]),
                destination_zone_code=zone,
                destination_row_no=parse_int_value(destination.get("row")),
                destination_section_no=parse_int_value(destination.get("section")),
                destination_tier_no=parse_int_value(destination.get("tier")),
                destination_cell_no=parse_int_value(destination.get("cell")),
                destination_location_id=destination_location_id,
                performed_by=request.user,
            )
        except FbsError as exc:
            raise ValueError(str(exc)) from exc
        if session is not None:
            session.pop(SESSION_MOVE_KEY, None)
            session.pop(SESSION_INSPECTION_KEY, None)
            session.modified = True
        return FreeMoveResult(
            operation=operation,
            message=(
                "FBS-паллета перемещена на точное место "
                f"{exact_location.location_code}. Резерв сохранён."
                if zone in {"PR", "OTG"}
                else "FBS-паллета перемещена на новое точное место OS."
            ),
        )
    exact_location = WarehouseLocation.objects.filter(
        warehouse_code="MSK",
        location_code__iexact=normalize_operational_location_scan(scan_value),
        zone_code__in=DESTINATION_ZONES,
    ).first()
    destination_location_code = ""
    if exact_location is not None:
        try:
            require_concrete_movement_location(
                exact_location,
                purpose="свободного перемещения паллеты",
            )
        except ValidationError as exc:
            raise ValueError("; ".join(exc.messages)) from exc
        destination = location_to_dict(exact_location)
        zone = str(exact_location.zone_code or "").strip().upper()
        destination_location_code = str(exact_location.location_code or "").strip()
    else:
        destination, error = parse_destination_scan(scan_value)
        if error:
            raise ValueError(error)
        zone = str(destination.get("zone") or "").strip().upper()
    if zone not in DESTINATION_ZONES:
        raise ValueError("Свободное перемещение разрешено только в OS или MR.")
    operation = WarehouseWritePathService.complete_free_pallet_relocation(
        operation_id=int(raw["operation_id"]),
        destination_zone_code=zone,
        destination_location_code=destination_location_code,
        destination_row_no=parse_int_value(destination.get("row")),
        destination_section_no=parse_int_value(destination.get("section")),
        destination_tier_no=parse_int_value(destination.get("tier")),
        destination_cell_no=parse_int_value(destination.get("cell")),
        performed_by=request.user,
    )
    if session is not None:
        session.pop(SESSION_MOVE_KEY, None)
        session.pop(SESSION_INSPECTION_KEY, None)
        session.modified = True
    return FreeMoveResult(
        operation=operation,
        message=(
            f"Паллета перемещена на точное место {destination_location_code}."
            if destination_location_code
            else "Паллета перемещена на новое место."
        ),
    )


def cancel_free_move(request) -> str:
    session = getattr(request, "session", None)
    raw = session.get(SESSION_MOVE_KEY) if session is not None else None
    if isinstance(raw, dict) and raw.get("operation_id"):
        operation_context = (
            WarehouseOperation.objects.filter(id=int(raw["operation_id"]))
            .values_list("context_type", flat=True)
            .first()
        )
        if operation_context == "fbs_free_box":
            from fbs.exceptions import FbsError
            from reachtruck_free.box_relocation import cancel_fbs_box_relocation

            try:
                cancel_fbs_box_relocation(
                    operation_id=int(raw["operation_id"]),
                    performed_by=request.user,
                )
            except FbsError as exc:
                raise ValueError(str(exc)) from exc
        elif operation_context == "fbs_free_relocation":
            from fbs.exceptions import FbsError
            from fbs.services.free_relocation import cancel_fbs_free_relocation

            try:
                cancel_fbs_free_relocation(
                    operation_id=int(raw["operation_id"]),
                    performed_by=request.user,
                )
            except FbsError as exc:
                raise ValueError(str(exc)) from exc
        else:
            WarehouseWritePathService.cancel_free_pallet_relocation(
                operation_id=int(raw["operation_id"]),
                performed_by=request.user,
            )
    if session is not None:
        session.pop(SESSION_MOVE_KEY, None)
        session.pop(SESSION_INSPECTION_KEY, None)
        session.modified = True
    return "Свободное перемещение отменено."
