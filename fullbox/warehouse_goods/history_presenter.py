from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sklad.models import WarehouseEvent, WarehouseLocation
from sklad.topology import os_location_code


EVENT_LABELS = {
    "receiving_arrived": "Принято на склад",
    "placement_started": "Размещение начато",
    "placement_completed": "Размещено на складе",
    "putaway_requested": "Запрошено размещение",
    "putaway_completed": "Размещено в ячейке",
    "processing_requested": "Передача в обработку",
    "processing_reserved": "Резерв для обработки",
    "processing_reserve_released": "Резерв обработки снят",
    "processing_zone_arrived": "Передано в обработку",
    "processing_started": "Обработка начата",
    "processing_completed": "Обработка завершена",
    "processing_consumed": "Списано в обработке",
    "shipping_reserved": "Резерв для отгрузки",
    "shipping_reserve_released": "Резерв отгрузки снят",
    "otg_requested": "Запрошена передача в отгрузку",
    "otg_arrived": "Передано в зону отгрузки",
    "palletization_started": "Паллетирование начато",
    "palletization_completed": "Паллетирование завершено",
    "ready_for_loading": "Готово к погрузке",
    "assigned_to_trip": "Назначено в рейс",
    "loading_started": "Погрузка начата",
    "loaded_to_vehicle": "Загружено в автомобиль",
    "shipped": "Отгружено",
    "movement_requested": "Запрошено перемещение",
    "movement_task_created": "Создано задание на перемещение",
    "movement_started": "Перемещение начато",
    "movement_completed": "Перемещено",
    "movement_canceled": "Перемещение отменено",
    "stock_returned_to_storage": "Возвращено на хранение",
    "stock_corrected": "Остаток скорректирован",
    "warehouse_context_canceled": "Складская операция отменена",
    "fbs_replenishment_reserved": "Резерв для подсорта FBS",
    "fbs_replenishment_destination_selected": "Выбрано место подсорта FBS",
    "fbs_replenishment_box_printed": "Распечатана этикетка короба FBS",
    "fbs_replenishment_staged": "Товар подготовлен к подсорту FBS",
    "fbs_replenishment_box_closed": "Короб подсорта FBS закрыт",
    "fbs_replenishment_collected": "Подсорт FBS собран",
    "fbs_replenishment_box_prepared": "Короб подсорта FBS подготовлен",
    "fbs_replenishment_completed": "Подсорт FBS выполнен",
    "fbs_replenishment_canceled": "Подсорт FBS отменен",
    "fbs_box_placement_completed": "Короб FBS размещен",
    "fbs_inventory_adjusted": "Остаток FBS скорректирован",
    "shipping_packing_pallet_removed_from_order": "Паллета исключена из отгрузки",
}


@dataclass(frozen=True)
class GoodsHistoryRow:
    occurred_at: datetime | None
    action: str
    from_location: str
    to_location: str
    qty: int
    user_name: str
    information: str


def history_action_label(event_type: str) -> str:
    return EVENT_LABELS.get(str(event_type or "").strip(), "Складская операция")


def history_location_label(location: WarehouseLocation | None, fallback_zone: str = "") -> str:
    zone = str(getattr(location, "zone_code", "") or fallback_zone or "").strip().upper()
    if location is not None and zone == "OS" and all(
        (
            int(location.row_no or 0),
            int(location.section_no or 0),
            int(location.tier_no or 0),
            int(location.cell_no or 0),
        )
    ):
        return os_location_code(
            row=location.row_no,
            section=location.section_no,
            tier=location.tier_no,
            cell=location.cell_no,
        )
    if location is not None and zone == "MR" and int(location.row_no or 0):
        return f"MR-{int(location.row_no)}"
    if zone:
        return zone
    if location is not None:
        return str(location.location_code or location.display_name or "").strip() or "—"
    return "—"


def _unique_codes(values) -> list[str]:
    result = []
    seen = set()
    for value in values:
        code = str(value or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        result.append(code)
    return result


def history_information(event: WarehouseEvent) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    box_values = []
    raw_box_codes = payload.get("box_codes")
    if isinstance(raw_box_codes, (list, tuple)):
        box_values.extend(raw_box_codes)
    for key in ("box_code", "target_box_code", "source_box_code", "rebound_from_box_code"):
        box_values.append(payload.get(key))
    boxes = _unique_codes(box_values)

    pallet_values = []
    raw_pallet_codes = payload.get("pallet_codes")
    if isinstance(raw_pallet_codes, (list, tuple)):
        pallet_values.extend(raw_pallet_codes)
    for key in ("pallet_code", "target_pallet_code", "source_pallet_code"):
        pallet_values.append(payload.get(key))
    pallets = _unique_codes(pallet_values)

    parts = []
    if boxes:
        parts.append(f"{'Короб' if len(boxes) == 1 else 'Короба'}: {', '.join(boxes)}")
    if pallets:
        parts.append(f"{'Паллета' if len(pallets) == 1 else 'Паллеты'}: {', '.join(pallets)}")
    return " · ".join(parts) or "—"


def present_history_event(event: WarehouseEvent) -> GoodsHistoryRow:
    user_name = "—"
    if event.performed_by is not None:
        user_name = event.performed_by.get_full_name() or event.performed_by.get_username() or "—"
    return GoodsHistoryRow(
        occurred_at=event.occurred_at,
        action=history_action_label(event.event_type),
        from_location=history_location_label(event.from_location, event.from_zone_code),
        to_location=history_location_label(event.to_location, event.to_zone_code),
        qty=int(event.qty or 0),
        user_name=user_name,
        information=history_information(event),
    )


def present_history_events(events) -> list[GoodsHistoryRow]:
    return [present_history_event(event) for event in events]
