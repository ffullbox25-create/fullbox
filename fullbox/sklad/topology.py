from __future__ import annotations

from collections.abc import Iterator


OS_ROW_SECTIONS = {
    1: 9,
    2: 9,
    3: 9,
    4: 9,
    5: 9,
    6: 8,
    7: 6,
    8: 6,
    9: 6,
    10: 6,
}
OS_TIERS = 4
OS_CELLS_PER_TIER = 3
OS_TIER_OVERRIDES = {
    (4, 9): 5,
    (5, 9): 5,
}
OS_PASSAGE_POSITIONS = {
    (4, section_number, tier_number)
    for section_number in range(1, 7)
    for tier_number in (1, 2)
}
OS_LINE_DISPLAY_LABELS = {
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


def os_tiers_for_position(row: int, section: int) -> int:
    row_no = int(row or 0)
    section_no = int(section or 0)
    if row_no <= 0 or section_no <= 0:
        return 0
    if section_no > int(OS_ROW_SECTIONS.get(row_no, 0)):
        return 0
    return int(OS_TIER_OVERRIDES.get((row_no, section_no), OS_TIERS))


def os_is_passage_position(row: int, section: int, tier: int) -> bool:
    return (int(row or 0), int(section or 0), int(tier or 0)) in OS_PASSAGE_POSITIONS


def iter_os_slot_keys() -> Iterator[tuple[int, int, int, int]]:
    for row in sorted(OS_ROW_SECTIONS):
        for section in range(1, int(OS_ROW_SECTIONS[row]) + 1):
            for tier in range(1, os_tiers_for_position(row, section) + 1):
                if os_is_passage_position(row, section, tier):
                    continue
                for cell in range(1, OS_CELLS_PER_TIER + 1):
                    yield row, section, tier, cell


def os_line_display_label(section: int) -> str:
    section_no = int(section or 0)
    return OS_LINE_DISPLAY_LABELS.get(section_no, str(section_no or ""))


def os_location_code(*, row: int, section: int, tier: int, cell: int) -> str:
    line_label = os_line_display_label(section)
    if line_label and row and tier and cell:
        return f"{line_label}-{int(row)}/{int(tier)}-{int(cell)}"
    return "OS"


def os_location_label(*, row: int, section: int, tier: int, cell: int) -> str:
    line_label = os_line_display_label(section)
    if line_label and row and tier and cell:
        return (
            f"OS · Линия {line_label} · Стеллаж {int(row)} · "
            f"Этаж {int(tier)} · Ячейка {int(cell)}"
        )
    return "OS · Основной склад"


def suggest_os_slot_for_agency(
    *,
    agency_id: int | None,
    occupied_keys: set[tuple[int, int, int, int]],
    section_agencies: dict[tuple[int, int], set[int]],
) -> tuple[int, int, int, int] | None:
    current_agency_id = int(agency_id or 0)
    slots_by_section: dict[tuple[int, int], list[tuple[int, int, int, int]]] = {}
    for key in iter_os_slot_keys():
        if key in occupied_keys:
            continue
        slots_by_section.setdefault((key[0], key[1]), []).append(key)

    section_buckets: dict[str, list[tuple[int, int]]] = {
        "same": [],
        "empty": [],
        "other": [],
    }
    for section_key in sorted(slots_by_section):
        agencies = {int(value) for value in section_agencies.get(section_key, set()) if int(value) > 0}
        if current_agency_id and current_agency_id in agencies:
            bucket = "same"
        elif not agencies:
            bucket = "empty"
        else:
            bucket = "other"
        section_buckets[bucket].append(section_key)

    for bucket in ("same", "empty", "other"):
        for section_key in section_buckets[bucket]:
            slots = slots_by_section[section_key]
            if slots:
                return slots[0]
    return None
