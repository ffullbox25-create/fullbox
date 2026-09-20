from __future__ import annotations

import json

from sklad.services.stock_availability import StockAvailabilityService


def parse_int_value(raw) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


def normalize_zone_code(raw: str | None) -> str:
    text = str(raw or "").strip().upper()
    if not text:
        return "PR"
    if text in {"PR", "OTG", "MR", "OS", "OBR"}:
        return text
    return text


_OS_LINE_DISPLAY_LABELS = {
    1: "0",
    2: "A",
    3: "B",
    4: "C",
    5: "D",
    6: "E",
    7: "F",
    8: "G",
    9: "I",
}


def unassigned_putaway_location() -> dict:
    return {"zone": "", "row": "", "section": "", "tier": "", "cell": "", "unassigned": True}


def is_unassigned_putaway_location(location: dict | None) -> bool:
    if not isinstance(location, dict):
        return False
    return bool(location.get("unassigned")) or not str(location.get("zone") or "").strip()


def _os_line_display_label(section: int) -> str:
    return _OS_LINE_DISPLAY_LABELS.get(parse_int_value(section), str(parse_int_value(section) or ""))


def putaway_location_key(location: dict | None) -> tuple[str, int, int, int, int]:
    if is_unassigned_putaway_location(location):
        return ("", 0, 0, 0, 0)
    normalized = normalize_putaway_location(location)
    return (
        normalize_zone_code(normalized.get("zone") or ""),
        parse_int_value(normalized.get("row")),
        parse_int_value(normalized.get("section")),
        parse_int_value(normalized.get("tier")),
        parse_int_value(normalized.get("cell")),
    )


def normalize_putaway_location(raw_location, fallback_payload=None) -> dict:
    source = raw_location if isinstance(raw_location, dict) else {}
    fallback = fallback_payload if isinstance(fallback_payload, dict) else {}
    zone = normalize_zone_code(
        source.get("zone")
        or source.get("location")
        or fallback.get("zone")
        or fallback.get("location")
        or ""
    )
    row = parse_int_value(source.get("row") or fallback.get("row")) if zone in {"MR", "OS"} else 0
    section = parse_int_value(source.get("section") or fallback.get("section")) if zone == "OS" else 0
    tier = parse_int_value(source.get("tier") or fallback.get("tier")) if zone == "OS" else 0
    cell = parse_int_value(source.get("cell") or fallback.get("cell")) if zone == "OS" else 0
    return {
        "zone": zone,
        "row": row if zone in {"MR", "OS"} else "",
        "section": section if zone == "OS" else "",
        "tier": tier if zone == "OS" else "",
        "cell": cell if zone == "OS" else "",
    }


def putaway_location_label(location: dict | None) -> str:
    source = location if isinstance(location, dict) else {}
    if is_unassigned_putaway_location(source):
        return "Место не выбрано"
    zone = normalize_zone_code(source.get("zone") or "") or "PR"
    row = parse_int_value(source.get("row"))
    section = parse_int_value(source.get("section"))
    tier = parse_int_value(source.get("tier"))
    cell = parse_int_value(source.get("cell"))
    if zone == "PR":
        return "PR · Зона приемки"
    if zone == "OBR":
        return "OBR · Зона обработки"
    if zone == "OTG":
        return "OTG · Зона отгрузки"
    if zone == "MR":
        return f"MR · Между рядами · Ряд {row}" if row else "MR · Между рядами"
    if zone == "OS":
        line_label = _os_line_display_label(section)
        if line_label and row and tier and cell:
            return f"OS · Линия {line_label} · Стеллаж {row} · Этаж {tier} · Ячейка {cell}"
        if line_label and row:
            return f"OS · Линия {line_label} · Стеллаж {row}"
        return "OS · Основной склад"
    return zone


def putaway_location_scan_code(location: dict | None) -> str:
    if is_unassigned_putaway_location(location):
        return ""
    source = normalize_putaway_location(location)
    zone = normalize_zone_code(source.get("zone") or "") or "PR"
    row = parse_int_value(source.get("row"))
    section = parse_int_value(source.get("section"))
    tier = parse_int_value(source.get("tier"))
    cell = parse_int_value(source.get("cell"))
    if zone == "OS":
        line_label = _os_line_display_label(section)
        if line_label and row and tier and cell:
            return f"{line_label}-{row}/{tier}-{cell}"
        if line_label and row:
            return f"{line_label}-{row}"
        return "OS"
    if zone == "MR":
        return f"MR-{row}" if row else "MR"
    return zone


def parse_putaway_destinations(
    raw,
    *,
    allowed_zones: set[str] | tuple[str, ...] | list[str] = ("PR", "MR", "OS"),
    allowed_zones_label: str = "PR, MR или OS",
) -> tuple[dict[str, dict] | None, str]:
    if raw is None:
        return None, ""
    text = str(raw or "").strip()
    if not text:
        return {}, ""
    try:
        source = json.loads(text)
    except json.JSONDecodeError:
        return None, "Некорректный JSON мест назначения для палет."
    if not isinstance(source, list):
        return None, "Некорректный формат списка палет для перемещения."
    allowed = {normalize_zone_code(value) for value in allowed_zones}
    result: dict[str, dict] = {}
    seen_os: set[tuple[int, int, int, int]] = set()
    for item in source:
        if not isinstance(item, dict):
            continue
        pallet_code = str(item.get("pallet_code") or item.get("palletCode") or "").strip()
        if not pallet_code:
            continue
        destination = item.get("destination") if isinstance(item.get("destination"), dict) else item
        normalized = normalize_putaway_location(destination)
        zone = normalize_zone_code(normalized.get("zone") or "")
        if zone not in allowed:
            return None, f"Паллета {pallet_code}: выберите зону хранения {allowed_zones_label}."
        if zone == "MR" and not parse_int_value(normalized.get("row")):
            return None, f"Паллета {pallet_code}: для зоны MR укажите ряд."
        if zone == "OS":
            row = parse_int_value(normalized.get("row"))
            section = parse_int_value(normalized.get("section"))
            tier = parse_int_value(normalized.get("tier"))
            cell = parse_int_value(normalized.get("cell"))
            if not (row and section and tier and cell):
                return None, f"Паллета {pallet_code}: для зоны OS укажите ряд, секцию, ярус и ячейку."
            os_key = (row, section, tier, cell)
            if os_key in seen_os:
                return None, f"Паллета {pallet_code}: место OS уже выбрано для другой палеты."
            seen_os.add(os_key)
        result[pallet_code] = normalized
    if text and not result:
        return None, "Не выбрана ни одна палета для перемещения на склад."
    return result, ""


def suggest_putaway_destinations(
    pallets,
    *,
    exclude_order_type: str,
    exclude_order_id: str,
    agency_id: int | None = None,
    row_sections: dict | None = None,
    tiers: list | tuple | None = None,
    cells_per_tier: int = 0,
    fallback_mr_row: int | None = None,
    exclude_draft_token: str = "",
) -> dict[str, dict]:
    source = pallets if isinstance(pallets, list) else []
    occupied_os = set(
        StockAvailabilityService.occupied_os_cell_keys(
            exclude_order_type=exclude_order_type,
            exclude_order_id=exclude_order_id,
            exclude_draft_token=exclude_draft_token,
        )
    )
    section_agencies = StockAvailabilityService.occupied_os_section_agencies(
        exclude_order_type=exclude_order_type,
        exclude_order_id=exclude_order_id,
        exclude_draft_token=exclude_draft_token,
    )
    used_os: set[tuple[int, int, int, int]] = set()
    result: dict[str, dict] = {}
    pending_codes: list[str] = []
    for pallet in source:
        if not isinstance(pallet, dict):
            continue
        pallet_code = str(pallet.get("code") or "").strip()
        if not pallet_code:
            continue
        location = normalize_putaway_location(pallet.get("location"), pallet)
        zone = normalize_zone_code(location.get("zone") or "")
        if zone == "OS":
            os_key = (
                parse_int_value(location.get("row")),
                parse_int_value(location.get("section")),
                parse_int_value(location.get("tier")),
                parse_int_value(location.get("cell")),
            )
            if all(os_key) and os_key not in occupied_os and os_key not in used_os:
                used_os.add(os_key)
                if agency_id and os_key[0] and os_key[1]:
                    section_agencies.setdefault((os_key[0], os_key[1]), set()).add(int(agency_id))
                result[pallet_code] = location
                continue
        if zone == "MR" and parse_int_value(location.get("row")):
            result[pallet_code] = location
            continue
        pending_codes.append(pallet_code)
    for pallet_code in pending_codes:
        assigned = None
        if row_sections and tiers and cells_per_tier:
            assigned = StockAvailabilityService.suggest_os_cell_for_agency(
                agency_id=agency_id,
                row_sections=row_sections,
                tiers=tiers,
                cells_per_tier=cells_per_tier,
                occupied_keys=occupied_os,
                used_cell_keys=used_os,
                section_agencies=section_agencies,
            )
        if assigned:
            os_key = (
                parse_int_value(assigned.get("row")),
                parse_int_value(assigned.get("section")),
                parse_int_value(assigned.get("tier")),
                parse_int_value(assigned.get("cell")),
            )
            used_os.add(os_key)
            if agency_id and os_key[0] and os_key[1]:
                section_agencies.setdefault((os_key[0], os_key[1]), set()).add(int(agency_id))
        if not assigned and fallback_mr_row:
            assigned = {"zone": "MR", "row": fallback_mr_row, "section": "", "tier": "", "cell": ""}
        if assigned:
            result[pallet_code] = normalize_putaway_location(assigned)
    return result


def build_putaway_rows(
    pallets,
    *,
    latest_moves_by_pallet: dict[str, dict] | None,
    suggested_destinations: dict[str, dict] | None,
    source_label: str,
) -> list[dict]:
    source = pallets if isinstance(pallets, list) else []
    latest = latest_moves_by_pallet if isinstance(latest_moves_by_pallet, dict) else {}
    suggested = suggested_destinations if isinstance(suggested_destinations, dict) else {}
    rows: list[dict] = []
    for pallet in source:
        if not isinstance(pallet, dict):
            continue
        pallet_code = str(pallet.get("code") or "").strip()
        if not pallet_code:
            continue
        move_payload = latest.get(pallet_code) or {}
        move_status = str(move_payload.get("status") or "").strip().lower()
        move_status_label = str(move_payload.get("status_label") or "").strip()
        suggested_destination = suggested.get(pallet_code)
        if isinstance(suggested_destination, dict) and not is_unassigned_putaway_location(suggested_destination):
            default_destination = normalize_putaway_location(suggested_destination)
        else:
            default_destination = unassigned_putaway_location()
        if move_payload.get("to_zone"):
            destination = normalize_putaway_location(
                {
                    "zone": move_payload.get("to_zone"),
                    "row": move_payload.get("to_row"),
                    "section": move_payload.get("to_section"),
                    "tier": move_payload.get("to_tier"),
                    "cell": move_payload.get("to_cell"),
                }
            )
        else:
            destination = default_destination
        status_text = "Задание не создано"
        status_class = "pending"
        selectable = True
        if move_status == "done":
            status_text = "Доставлено на склад"
            status_class = "done"
            selectable = False
        elif move_status == "in_progress":
            status_text = move_status_label or "В работе у ричтракера"
            status_class = "in_progress"
            selectable = False
        elif move_status == "created":
            status_text = move_status_label or "Передано ричтракеру"
            status_class = "created"
            selectable = False
        elif move_status in {"canceled", "cancelled"}:
            status_text = move_status_label or "Задание отменено, можно создать заново"
            status_class = "canceled"
        rows.append(
            {
                "pallet_code": pallet_code,
                "from_label": source_label,
                "to_label": putaway_location_label(destination),
                "status": move_status,
                "status_label": status_text,
                "status_class": status_class,
                "move_order_id": str(move_payload.get("order_id") or "").strip(),
                "selectable": selectable,
                "destination": destination,
            }
        )
    return rows
