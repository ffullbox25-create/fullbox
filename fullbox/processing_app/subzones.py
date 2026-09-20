from __future__ import annotations

from collections.abc import Iterable

from .stages import (
    PROCESSING_STAGE_DONE,
    PROCESSING_STAGE_OBR_ARRIVED,
    PROCESSING_STAGE_QUALITY_APPROVED,
    PROCESSING_STAGE_QUALITY_CONTROL,
    PROCESSING_STAGE_REWORK,
    PROCESSING_STAGE_RETURNED_TO_STOCK,
    PROCESSING_STAGE_RETURN_TO_STOCK_SENT,
    PROCESSING_STAGE_TAKEN_INTO_PROCESSING,
    PROCESSING_STAGE_UNBOXING_COMPLETED,
    PROCESSING_STAGE_UNBOXING_OPENED,
    processing_stage_rank,
)

PROCESSING_SUBZONE_IN = "obr_in"
PROCESSING_SUBZONE_WORK = "obr_work"
PROCESSING_SUBZONE_QC = "obr_qc"
PROCESSING_SUBZONE_PACK = "obr_pack"
PROCESSING_SUBZONE_OUT = "obr_out"

PROCESSING_SUBZONES: tuple[dict[str, str], ...] = (
    {
        "key": PROCESSING_SUBZONE_IN,
        "code": "OBR-IN",
        "label": "Ожидает обработки",
        "description": "Товар доставлен в OBR и ожидает назначения.",
    },
    {
        "key": PROCESSING_SUBZONE_WORK,
        "code": "OBR-WORK",
        "label": "Рабочее место",
        "description": "Товар находится у назначенного обработчика.",
    },
    {
        "key": PROCESSING_SUBZONE_QC,
        "code": "OBR-QC",
        "label": "Проверка качества",
        "description": "Обработка завершена, требуется проверка результата.",
    },
    {
        "key": PROCESSING_SUBZONE_PACK,
        "code": "OBR-PACK",
        "label": "Формирование коробов",
        "description": "Товар передан на формирование коробов и палет.",
    },
    {
        "key": PROCESSING_SUBZONE_OUT,
        "code": "OBR-OUT",
        "label": "Готово к выводу",
        "description": "Короба сформированы, товар готов к возврату или отгрузке.",
    },
)

_PROCESSING_SUBZONE_MAP = {item["key"]: item for item in PROCESSING_SUBZONES}
_PROCESSING_SUBZONE_KEYS = frozenset(_PROCESSING_SUBZONE_MAP)
_OUT_STAGES = {
    PROCESSING_STAGE_QUALITY_APPROVED,
    PROCESSING_STAGE_RETURN_TO_STOCK_SENT,
    PROCESSING_STAGE_RETURNED_TO_STOCK,
    PROCESSING_STAGE_DONE,
}
_QC_STAGES = {
    PROCESSING_STAGE_UNBOXING_COMPLETED,
    PROCESSING_STAGE_QUALITY_CONTROL,
}


def processing_subzone_meta(subzone: str | None) -> dict[str, str]:
    return dict(_PROCESSING_SUBZONE_MAP.get(str(subzone or "").strip(), {}))


def apply_processing_card_subzone(payload: dict, card_id: str, subzone: str) -> dict:
    card_key = str(card_id or "").strip()
    subzone_key = str(subzone or "").strip()
    if not card_key or subzone_key not in _PROCESSING_SUBZONE_KEYS:
        return payload
    raw_map = payload.get("processing_card_subzones")
    subzone_map = dict(raw_map) if isinstance(raw_map, dict) else {}
    subzone_map[card_key] = subzone_key
    payload["processing_card_subzones"] = subzone_map
    payload["processing_subzone"] = subzone_key
    return payload


def apply_processing_cards_subzone(
    payload: dict,
    card_ids: Iterable[str],
    subzone: str,
) -> dict:
    for card_id in card_ids:
        apply_processing_card_subzone(payload, card_id, subzone)
    return payload


def build_processing_subzone_state(
    *,
    card_ids: Iterable[str],
    processed_card_ids: Iterable[str] = (),
    assigned_card_ids: Iterable[str] = (),
    stage: str = "",
    packaging_active: bool = False,
    placement_completed: bool = False,
) -> dict:
    ordered_card_ids = []
    seen_card_ids = set()
    for raw_card_id in card_ids:
        card_id = str(raw_card_id or "").strip()
        if not card_id or card_id in seen_card_ids:
            continue
        seen_card_ids.add(card_id)
        ordered_card_ids.append(card_id)

    processed = {
        str(card_id or "").strip()
        for card_id in processed_card_ids
        if str(card_id or "").strip()
    }
    assigned = {
        str(card_id or "").strip()
        for card_id in assigned_card_ids
        if str(card_id or "").strip()
    }
    stage_value = str(stage or "").strip()
    stage_is_out = stage_value in _OUT_STAGES
    stage_is_pack = packaging_active or stage_value == PROCESSING_STAGE_UNBOXING_OPENED
    stage_is_qc = stage_value in _QC_STAGES or (
        placement_completed and stage_value != PROCESSING_STAGE_REWORK
    )
    stage_is_in_obr = (
        processing_stage_rank(stage_value)
        >= processing_stage_rank(PROCESSING_STAGE_OBR_ARRIVED)
    )

    card_subzones: dict[str, dict[str, str]] = {}
    counts = {item["key"]: 0 for item in PROCESSING_SUBZONES}
    for card_id in ordered_card_ids:
        subzone = ""
        if stage_is_out:
            subzone = PROCESSING_SUBZONE_OUT
        elif stage_is_pack:
            subzone = PROCESSING_SUBZONE_PACK
        elif stage_is_qc:
            subzone = PROCESSING_SUBZONE_QC
        elif card_id in processed:
            subzone = PROCESSING_SUBZONE_QC
        elif card_id in assigned:
            subzone = PROCESSING_SUBZONE_WORK
        elif stage_is_in_obr:
            subzone = PROCESSING_SUBZONE_IN
        if not subzone:
            continue
        card_subzones[card_id] = processing_subzone_meta(subzone)
        counts[subzone] += 1

    current_subzone = ""
    if stage_is_out:
        current_subzone = PROCESSING_SUBZONE_OUT
    elif stage_is_pack:
        current_subzone = PROCESSING_SUBZONE_PACK
    elif stage_is_qc:
        current_subzone = PROCESSING_SUBZONE_QC
    elif counts[PROCESSING_SUBZONE_WORK]:
        current_subzone = PROCESSING_SUBZONE_WORK
    elif ordered_card_ids and counts[PROCESSING_SUBZONE_QC] == len(ordered_card_ids):
        current_subzone = PROCESSING_SUBZONE_QC
    elif counts[PROCESSING_SUBZONE_IN]:
        current_subzone = PROCESSING_SUBZONE_IN
    elif (
        processing_stage_rank(stage_value)
        >= processing_stage_rank(PROCESSING_STAGE_TAKEN_INTO_PROCESSING)
    ):
        current_subzone = PROCESSING_SUBZONE_WORK

    steps = []
    for item in PROCESSING_SUBZONES:
        step = dict(item)
        step["count"] = counts[item["key"]]
        step["is_current"] = item["key"] == current_subzone
        steps.append(step)

    return {
        "current": processing_subzone_meta(current_subzone),
        "cards": card_subzones,
        "counts": counts,
        "steps": steps,
        "is_in_obr": bool(current_subzone),
    }
