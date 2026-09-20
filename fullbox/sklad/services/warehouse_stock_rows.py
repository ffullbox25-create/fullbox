from __future__ import annotations

from django.db import models

from sklad.models import WarehouseContainer, WarehouseStockSnapshot

_PALLET_CONTAINER_TYPES = {
    WarehouseContainer.TYPE_PALLET,
    WarehouseContainer.TYPE_MIXED_PALLET,
}
_PROCESSING_SOURCE_CONTAINER_STATES = {
    "moving_to_processing",
    "in_processing_zone",
    "processing_in_progress",
}


def _clean_text(value) -> str:
    return str(value or "").strip()


def _box_dimensions_label(container) -> str:
    if container is None:
        return ""
    width = getattr(container, "width_mm", None)
    height = getattr(container, "height_mm", None)
    depth = getattr(container, "depth_mm", None)
    if not width and not height and not depth:
        return ""
    return "/".join(str(value or "-") for value in (width, height, depth))


def _box_weight_label(container) -> str:
    if container is None:
        return ""
    weight = getattr(container, "gross_weight_g", None)
    if not weight:
        return ""
    return str(weight)


def _int_value(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_zone(value: str) -> str:
    text = _clean_text(value).upper()
    if not text:
        return ""
    if text in {"PR", "OBR", "OTG", "MR", "OS"}:
        return text
    if "ПРИЕМ" in text:
        return "PR"
    if "ОБРАБОТ" in text:
        return "OBR"
    if "ОТГРУЗ" in text:
        return "OTG"
    if "МЕЖДУ" in text:
        return "MR"
    if "ОСНОВ" in text:
        return "OS"
    return text


def _location_label(location: dict | None) -> str:
    location = location or {}
    zone = _normalize_zone(location.get("zone") or "") or "PR"
    row = _int_value(location.get("row"))
    section = _int_value(location.get("section"))
    tier = _int_value(location.get("tier"))
    cell = _int_value(location.get("cell"))
    if zone == "PR":
        return "PR · Зона приемки"
    if zone == "OBR":
        return "OBR · Зона обработки"
    if zone == "OTG":
        return "OTG · Зона отгрузки"
    if zone == "MR":
        return f"MR · Между рядами · Ряд {row}" if row else "MR · Между рядами"
    if zone == "OS":
        if row and section and tier and cell:
            return f"OS · Ряд {row} · Секция {section} · Ярус {tier} · Ячейка {cell}"
        if row:
            return f"OS · Ряд {row}"
        return "OS · Основной склад"
    return zone


def _location_parts_from_snapshot(snapshot: WarehouseStockSnapshot) -> tuple[str, int, int, int, int, str]:
    location = snapshot.location
    if location is not None:
        zone = _normalize_zone(location.zone_code or snapshot.zone_code or "")
        row = _int_value(location.row_no)
        section = _int_value(location.section_no)
        tier = _int_value(location.tier_no)
        cell = _int_value(location.cell_no)
        location_label = (
            _clean_text(location.display_name)
            or _clean_text(location.location_code)
            or _location_label(
                {
                    "zone": zone,
                    "row": row,
                    "section": section,
                    "tier": tier,
                    "cell": cell,
                }
            )
        )
        return zone, row, section, tier, cell, location_label
    zone = _normalize_zone(snapshot.zone_code or "")
    return zone, 0, 0, 0, 0, _location_label({"zone": zone})


def normalize_stock_row_from_snapshot(snapshot: WarehouseStockSnapshot) -> dict | None:
    container = snapshot.container
    parent_container = snapshot.parent_container
    active_operation = snapshot.active_operation
    last_event = snapshot.last_event
    sku_ref = snapshot.sku_ref
    sku_code = _clean_text(getattr(sku_ref, "sku_code", "") or snapshot.sku_code)
    pallet_code = ""
    box_code = ""
    if container is not None and container.container_type == WarehouseContainer.TYPE_BOX:
        box_code = _clean_text(container.container_code)
        if parent_container is not None and parent_container.container_type in _PALLET_CONTAINER_TYPES:
            pallet_code = _clean_text(parent_container.container_code)
    elif parent_container is not None and _clean_text(parent_container.container_code):
        pallet_code = _clean_text(parent_container.container_code)
        if container is not None and _clean_text(container.container_code):
            box_code = _clean_text(container.container_code)
    elif container is not None and container.container_type in _PALLET_CONTAINER_TYPES:
        pallet_code = _clean_text(container.container_code)
    elif _clean_text(snapshot.container_code):
        pallet_code = _clean_text(snapshot.container_code)
    state_code = _clean_text(snapshot.warehouse_state_code).lower()
    if state_code in _PROCESSING_SOURCE_CONTAINER_STATES:
        event_payload = getattr(last_event, "payload", None) if last_event is not None else None
        if isinstance(event_payload, dict):
            if not box_code:
                box_code = _clean_text(event_payload.get("source_box_code"))
        pallet_code = ""
    zone, row, section, tier, cell, location_label = _location_parts_from_snapshot(snapshot)
    box_size = _box_dimensions_label(container)
    box_weight = _box_weight_label(container)
    return {
        "id": int(snapshot.pk or 0),
        "agency": snapshot.agency,
        "agency_id": int(snapshot.agency_id or 0),
        "container_id": int(snapshot.container_id or 0),
        "parent_container_id": int(snapshot.parent_container_id or 0),
        "location_id": int(snapshot.location_id or 0),
        "source_order_type": _clean_text(snapshot.source_context_type),
        "source_order_id": _clean_text(snapshot.source_context_id),
        "order_type": _clean_text(snapshot.source_context_type),
        "order_id": _clean_text(snapshot.source_context_id),
        "active_context_type": _clean_text(getattr(active_operation, "context_type", "")),
        "active_context_id": _clean_text(getattr(active_operation, "context_id", "")),
        "active_context_status": _clean_text(getattr(active_operation, "status", "")),
        "last_event_type": _clean_text(getattr(last_event, "event_type", "")),
        "last_event_context_type": _clean_text(getattr(last_event, "stock_context_type", "")),
        "last_event_context_id": _clean_text(getattr(last_event, "stock_context_id", "")),
        "sku": sku_code,
        "sku_ref_id": int(snapshot.sku_ref_id or 0),
        "name": _clean_text(snapshot.name),
        "size": _clean_text(snapshot.size),
        "box_size": box_size,
        "box_weight": box_weight,
        "barcode": _clean_text(snapshot.barcode),
        "goods_type": _clean_text(snapshot.goods_type),
        "warehouse_state_code": _clean_text(snapshot.warehouse_state_code),
        "qty": int(snapshot.qty or 0),
        "available_qty": int(snapshot.available_qty or 0),
        "processing_reserved_qty": int(snapshot.processing_reserved_qty or 0),
        "shipping_reserved_qty": int(snapshot.shipping_reserved_qty or 0),
        "pallet_code": pallet_code,
        "box_code": box_code,
        "zone": zone,
        "row": row,
        "section": section,
        "tier": tier,
        "cell": cell,
        "location": location_label,
        "created_at": snapshot.created_at,
        "updated_at": snapshot.updated_at,
    }


def snapshot_stock_rows(
    *,
    agency=None,
    agency_id: int | None = None,
    zone: str | None = None,
    row_number: int | None = None,
    sku_values: set[str] | None = None,
    barcode_values: set[str] | None = None,
    require_pallet: bool = False,
    require_box: bool = False,
) -> list[dict]:
    qs = (
        WarehouseStockSnapshot.objects.filter(is_archived=False, qty__gt=0)
        .select_related("agency", "sku_ref", "container", "parent_container", "location", "active_operation", "last_event")
        .order_by("created_at", "id")
    )
    if agency_id:
        qs = qs.filter(agency_id=agency_id)
    elif agency is not None:
        qs = qs.filter(agency=agency)
    normalized_zone = _normalize_zone(zone or "")
    if normalized_zone:
        qs = qs.filter(zone_code__iexact=normalized_zone)
    if row_number:
        qs = qs.filter(location__row_no=row_number)

    sku_values = {_clean_text(value).lower() for value in (sku_values or set()) if _clean_text(value)}
    barcode_values = {_clean_text(value).lower() for value in (barcode_values or set()) if _clean_text(value)}

    rows: list[dict] = []
    for snapshot in qs:
        row = normalize_stock_row_from_snapshot(snapshot)
        if row is None:
            continue
        if require_pallet and not row["pallet_code"]:
            continue
        if require_box and not row["box_code"]:
            continue
        if normalized_zone and row["zone"] != normalized_zone:
            continue
        if row_number and row["row"] != row_number:
            continue
        row_sku = str(row["sku"]).lower()
        row_barcode = str(row["barcode"]).lower()
        if sku_values or barcode_values:
            if sku_values and barcode_values:
                if row_sku not in sku_values and row_barcode not in barcode_values:
                    continue
            elif sku_values and row_sku not in sku_values:
                continue
            elif barcode_values and row_barcode not in barcode_values:
                continue
        rows.append(row)
    return rows
