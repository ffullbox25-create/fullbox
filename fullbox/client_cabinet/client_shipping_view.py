"""Read-only client view of a shipping order (6-step scale + aggregates).

Does not write warehouse state. Maps existing ShippingOrder + trip + packing
summaries into a safe client-facing presentation for the LK detail card.
"""

from __future__ import annotations

import re
from typing import Any

from django.utils import timezone


CLIENT_SHIPPING_STAGES: list[tuple[str, str]] = [
    ("confirmed", "Подтверждена"),
    ("assembling", "Комплектуется"),
    ("ready", "Готова"),
    ("in_transit", "В пути"),
    ("delivered", "Сдана на МП"),
    ("closed", "Закрыта"),
]

_STAGE_INDEX = {key: idx for idx, (key, _) in enumerate(CLIENT_SHIPPING_STAGES)}

_ASSEMBLY_OPS = {
    "reserved": "Резервирование товара",
    "storekeeper_accepted": "Принята в работу складом",
    "picking": "Подбор и доставка в зону отгрузки",
}


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _text(value) -> str:
    return str(value or "").strip()


_PARTIAL_BOX_SPLIT_TOKEN_RE = re.compile(r"split_box:b64:[A-Za-z0-9_-]+={0,2}")


def _client_item_comment(value) -> str:
    """Remove internal source-box references from the client presentation only."""
    visible_parts: list[str] = []
    for raw_part in _text(value).split(";"):
        part = _PARTIAL_BOX_SPLIT_TOKEN_RE.sub("", raw_part).strip()
        lowered = part.casefold()
        if lowered.startswith("исходные короба:") or lowered.startswith("короба:"):
            continue
        if part:
            visible_parts.append(part)
    return "; ".join(visible_parts)


def _marketplace_short_label(marketplace_name: str = "") -> str:
    raw = _text(marketplace_name).lower()
    if not raw:
        return "МП"
    if "wildberries" in raw or raw in {"wb", "вайлдберриз", "вайлдбериз"}:
        return "WB"
    if "ozon" in raw or "озон" in raw:
        return "Ozon"
    if "yandex" in raw or "яндекс" in raw or "market" in raw:
        return "Яндекс"
    return _text(marketplace_name)[:24] or "МП"


def _delivered_stage_label(marketplace_name: str = "") -> str:
    return f"Сдана на {_marketplace_short_label(marketplace_name)}"


def _fmt_dt(value) -> str:
    if not value:
        return ""
    try:
        local = timezone.localtime(value) if timezone.is_aware(value) else value
        return local.strftime("%d.%m.%Y, %H:%M")
    except Exception:
        try:
            return value.strftime("%d.%m.%Y")
        except Exception:
            return _text(value)


def resolve_client_shipping_stage(
    *,
    order_status: str,
    trip_status: str = "",
    routing_status: str = "",
    marketplace_name: str = "",
) -> tuple[str, str]:
    """Map warehouse/trip/routing state → client stage key + label."""
    st = _text(order_status).lower()
    trip = _text(trip_status).lower()
    routing = _text(routing_status).lower()

    if st == "canceled":
        return "canceled", "Отменена"

    if routing == "delivery_failed":
        return "not_delivered", "Не сдана на МП"

    if trip == "completed" or routing == "delivered":
        if st in {"shipped", "partial_shipped"} and routing != "delivery_failed":
            return "closed", "Закрыта"
        return "delivered", _delivered_stage_label(marketplace_name)

    if trip == "departed" or routing == "in_transit":
        return "in_transit", "В пути"

    if st in {"shipped", "partial_shipped"}:
        return "in_transit", "В пути"

    if st == "packed":
        return "ready", "Готова"

    if st in {"reserved", "storekeeper_accepted", "picking"}:
        return "assembling", "Комплектуется"

    if st == "submitted":
        return "confirmed", "Подтверждена"

    if st == "draft":
        return "draft", "Черновик"

    return "confirmed", "Подтверждена"


def _stage_scale(current_key: str) -> list[dict[str, Any]]:
    if current_key == "canceled":
        return [
            {"key": key, "label": label, "state": "muted"}
            for key, label in CLIENT_SHIPPING_STAGES
        ]
    if current_key == "not_delivered":
        rows = []
        for key, label in CLIENT_SHIPPING_STAGES:
            if key in {"delivered", "closed"}:
                continue
            if key == "in_transit":
                rows.append({"key": key, "label": label, "state": "done"})
            elif _STAGE_INDEX.get(key, 0) < _STAGE_INDEX.get("in_transit", 0):
                rows.append({"key": key, "label": label, "state": "done"})
            else:
                rows.append({"key": key, "label": label, "state": "muted"})
        rows.append({"key": "not_delivered", "label": "Не сдана на МП", "state": "current"})
        return rows
    if current_key == "draft":
        return [
            {"key": key, "label": label, "state": "upcoming"}
            for key, label in CLIENT_SHIPPING_STAGES
        ]
    current_idx = _STAGE_INDEX.get(current_key, 0)
    rows = []
    for idx, (key, label) in enumerate(CLIENT_SHIPPING_STAGES):
        if idx < current_idx:
            state = "done"
        elif idx == current_idx:
            state = "current"
        else:
            state = "upcoming"
        rows.append({"key": key, "label": label, "state": state})
    return rows


def _line_status(*, stage_key: str, qty_requested: int, qty_reserved: int, qty_shipped: int) -> str:
    if stage_key == "canceled":
        return "Отменена"
    if qty_shipped > 0 and qty_shipped >= qty_requested > 0:
        return "Отгружено"
    if qty_shipped > 0 and qty_shipped < qty_requested:
        return "Частично"
    if stage_key in {"ready", "in_transit", "delivered", "closed"}:
        return "Подготовлено" if qty_reserved or qty_shipped else "Ожидает"
    if stage_key == "assembling":
        if qty_reserved >= qty_requested > 0:
            return "В работе"
        if qty_reserved > 0:
            return "Резерв частичный"
        return "В работе"
    if stage_key == "confirmed":
        return "Подтверждена"
    return "Ожидает"


def _metric_display(value, *, empty: str = "0") -> str:
    if value is None:
        return "Нет данных"
    if isinstance(value, str):
        return value
    return str(int(value)) if value or empty == "0" else empty


def _shipping_discrepancy_display(order, *, warehouse_status: str) -> dict[str, str]:
    payload = getattr(order, "shipping_discrepancy_payload", None)
    if not isinstance(payload, dict):
        payload = {}
    status = _text(getattr(order, "shipping_discrepancy_status", "") or payload.get("status")).lower()
    if not status:
        return {}
    additional_pick_ids = payload.get("additional_pick_move_ids") or []
    has_pick = bool(additional_pick_ids)
    has_substitution = bool(payload.get("added_items") or payload.get("added_box_count"))
    warehouse_done = warehouse_status in {"packed", "shipped", "partial_shipped"}
    final_box_count = _as_int(payload.get("final_otg_box_count"))
    if status == "pending":
        return {
            "state": "pending",
            "status_label": "Расхождение на согласовании",
            "description": "Ожидается решение менеджера.",
        }
    if status == "resolved" or final_box_count:
        return {
            "state": "done",
            "status_label": "Расхождение закрыто по складу",
            "description": "Итоговый складской факт учтён.",
        }
    if status == "pickup_required" and warehouse_done:
        return {
            "state": "done",
            "status_label": "Добор учтён, заявка упакована",
            "description": "Склад уже учёл замену/добор и сформировал отгрузку.",
        }
    if status == "pickup_required" and has_pick:
        return {
            "state": "pickup",
            "status_label": "Добор создан",
            "description": "Создано задание ричтраку на добор по согласованному расхождению.",
        }
    if status == "pickup_required" and has_substitution:
        return {
            "state": "pickup",
            "status_label": "Добор согласован",
            "description": "Менеджер согласовал замену/добор по складскому факту.",
        }
    if status == "approved":
        return {
            "state": "approved",
            "status_label": "Расхождение согласовано",
            "description": "Складской факт согласован менеджером.",
        }
    return {
        "state": status,
        "status_label": "Есть складское расхождение",
        "description": "Состояние расхождения обновляется по факту склада.",
    }


def _box_status_for_stage(stage_key: str) -> str:
    if stage_key in {"ready", "in_transit", "delivered", "closed"}:
        return "Закрыт"
    if stage_key == "assembling":
        return "Формируется"
    if stage_key == "canceled":
        return "Отменён"
    return "Ожидает"


def _pallet_status_for_stage(stage_key: str) -> str:
    if stage_key in {"ready", "in_transit", "delivered", "closed"}:
        return "Сформирована"
    if stage_key == "assembling":
        return "Формируется"
    if stage_key == "canceled":
        return "Отменена"
    return "Ожидает"


def _build_boxes_and_pallets(packing: dict | None, *, stage_key: str) -> tuple[list[dict], list[dict]]:
    boxes: list[dict[str, Any]] = []
    pallets: list[dict[str, Any]] = []
    if not packing or not isinstance(packing.get("pallets"), list):
        return boxes, pallets
    box_status = _box_status_for_stage(stage_key)
    pallet_status = _pallet_status_for_stage(stage_key)
    for pallet in packing["pallets"]:
        if not isinstance(pallet, dict):
            continue
        label = _text(pallet.get("label") or pallet.get("code")) or "—"
        code = _text(pallet.get("code") or pallet.get("source_code"))
        pallet_boxes = [b for b in (pallet.get("boxes") or []) if isinstance(b, dict)]
        pallets.append(
            {
                "code": code,
                "label": label,
                "box_count": _as_int(pallet.get("box_count")) or len(pallet_boxes),
                "qty": _as_int(pallet.get("qty")),
                "status": pallet_status,
                "boxes": [
                    {
                        "code": _text(b.get("box_code") or b.get("code")),
                        "qty": _as_int(b.get("qty")),
                    }
                    for b in pallet_boxes
                ],
            }
        )
        for box in pallet_boxes:
            boxes.append(
                {
                    "code": _text(box.get("box_code") or box.get("code")),
                    "qty": _as_int(box.get("qty")),
                    "barcode_preview": _text(box.get("barcode_preview")),
                    "pallet_label": _text(box.get("pallet_label")) or label,
                    "pallet_code": code,
                    "status": box_status,
                }
            )
    return boxes, pallets


def _load_trip_details(order) -> dict[str, Any]:
    """Read-only trip fields for client logistics tab."""
    try:
        from logistics.models import LogisticsTrip, LogisticsTripOrder

        link = (
            LogisticsTripOrder.objects.select_related("trip", "trip__carrier")
            .filter(shipping_order_id=order.pk)
            .exclude(trip__status=LogisticsTrip.STATUS_CANCELED)
            .order_by("-id")
            .first()
        )
    except Exception:
        return {}
    if not link or not link.trip:
        return {}
    trip = link.trip
    carrier_name = ""
    try:
        carrier_name = _text(getattr(trip.carrier, "name", "") or getattr(trip.carrier, "title", ""))
    except Exception:
        carrier_name = ""
    vehicle_type = ""
    if hasattr(trip, "get_vehicle_type_display"):
        vehicle_type = _text(trip.get_vehicle_type_display())
    return {
        "trip_number": _text(trip.number),
        "trip_status": _text(trip.status),
        "trip_status_label": (
            dict(LogisticsTrip.STATUS_CHOICES).get(trip.status, "") if hasattr(LogisticsTrip, "STATUS_CHOICES") else ""
        ),
        "trip_date": trip.trip_date.strftime("%d.%m.%Y") if getattr(trip, "trip_date", None) else "",
        "vehicle_name": _text(trip.vehicle_name),
        "vehicle_number": _text(trip.vehicle_number),
        "vehicle_type": vehicle_type,
        "driver_name": _text(trip.driver_name),
        "driver_phone": _text(trip.driver_phone),
        "carrier_name": carrier_name,
        "route_comment": _text(trip.route_comment),
    }


def _build_logistics_rows(*, order, snap: dict, trip: dict, stage_key: str) -> list[dict[str, str]]:
    slot = "—"
    if getattr(order, "slot_date", None):
        slot = order.slot_date.strftime("%d.%m.%Y")
        if getattr(order, "slot_time", None):
            slot = f"{slot} {order.slot_time.strftime('%H:%M')}"

    routing_label = _text(snap.get("status_label"))
    trip_status_label = _text(trip.get("trip_status_label")) or _text(snap.get("trip_status")) or "—"

    rows = [
        {"label": "Склад назначения", "value": _text(getattr(order, "destination_warehouse", "")) or "—"},
        {"label": "Транзитный склад", "value": _text(getattr(order, "transit_address", "")) or "—"},
        {"label": "Слот маркетплейса", "value": slot},
        {"label": "Статус маршрутизации", "value": routing_label or "Ещё не назначена"},
        {"label": "Номер рейса", "value": _text(trip.get("trip_number") or snap.get("trip_number")) or "—"},
        {"label": "Статус рейса", "value": trip_status_label},
        {"label": "Дата рейса", "value": _text(trip.get("trip_date")) or "—"},
        {"label": "Перевозчик", "value": _text(trip.get("carrier_name")) or "—"},
        {"label": "Тип транспорта", "value": _text(trip.get("vehicle_type")) or "—"},
        {"label": "Автомобиль", "value": _text(trip.get("vehicle_name")) or "—"},
        {
            "label": "Гос. номер",
            "value": _text(trip.get("vehicle_number") or snap.get("vehicle_number") or getattr(order, "vehicle_number", ""))
            or "—",
        },
        {
            "label": "Водитель",
            "value": _text(trip.get("driver_name") or snap.get("driver_name")) or "—",
        },
        {
            "label": "Телефон водителя",
            "value": _text(trip.get("driver_phone") or getattr(order, "driver_phone", "")) or "—",
        },
        {"label": "Комментарий логиста", "value": _text(trip.get("route_comment")) or "—"},
        {
            "label": "Дата отгрузки",
            "value": _fmt_dt(getattr(order, "shipped_at", None)) or ("Ещё не отгружено" if stage_key not in {"in_transit", "delivered", "closed"} else "—"),
        },
    ]
    if stage_key in {"in_transit", "delivered", "closed"}:
        # Highlight key transit fields first for client glance.
        highlight = {"Гос. номер", "Водитель", "Склад назначения", "Слот маркетплейса", "Номер рейса"}
        rows.sort(key=lambda r: 0 if r["label"] in highlight else 1)
    return rows


def _build_warehouse_info(
    *,
    stage_key: str,
    stage_label: str,
    current_operation: str,
    progress_text: str,
    planned_ready: str,
    box_fact: int,
    box_plan: int,
    pallet_fact: int,
    total_reserved: int,
    total_requested: int,
    attention_items: list,
) -> list[dict[str, str]]:
    return [
        {"label": "Текущий статус", "value": stage_label or "—"},
        {"label": "Текущая операция", "value": current_operation or "—"},
        {"label": "Прогресс", "value": progress_text or "—"},
        {"label": "Плановая готовность", "value": planned_ready or "—"},
        {
            "label": "Короба",
            "value": f"{box_fact} из {box_plan}" if box_plan else (str(box_fact) if box_fact else "Не начато"),
        },
        {"label": "Палеты", "value": str(pallet_fact) if pallet_fact else "Не начато"},
        {
            "label": "Резерв",
            "value": f"{total_reserved} из {total_requested} шт." if total_requested else str(total_reserved),
        },
        {
            "label": "Открытые вопросы",
            "value": str(len(attention_items)) if attention_items else "Нет",
        },
    ]


def _build_documents(*, order, stage_key: str, agency=None) -> list[dict[str, Any]]:
    number = _text(getattr(order, "number", ""))
    pk = getattr(order, "pk", None)
    agency_id = getattr(agency, "id", None) or getattr(order, "agency_id", None)
    docs: list[dict[str, Any]] = []
    if number:
        docs.append(
            {
                "title": "Состав коробов (Excel)",
                "doc_type": "excel",
                "status": "Доступен",
                "source": "Личный кабинет",
                "url": f"/client/api/v1/requests/shipping/{number}/export/",
                "open_label": "Скачать",
            }
        )
        if pk:
            docs.append(
                {
                    "title": "Упаковочный лист (Excel)",
                    "doc_type": "packing_list",
                    "status": "Доступен",
                    "source": "Личный кабинет",
                    "url": f"/shipping/{pk}/packing-list.xlsx",
                    "open_label": "Скачать",
                    "download": True,
                    "download_name": f"packing-list-{number}.xlsx",
                }
            )
        docs.append(
            {
                "title": "Честный знак по отгрузке (Excel)",
                "doc_type": "marking",
                "status": "Доступен",
                "source": "Складской реестр",
                "url": f"/client/api/v1/requests/shipping/{number}/marking/export/",
                "open_label": "Скачать ЧЗ",
                "download": True,
                "download_name": f"chestny-znak-{number}.xlsx",
            }
        )
    if pk and number and stage_key in {"ready", "in_transit", "delivered", "closed"}:
        docs.append(
            {
                "title": "Акт МХ-3",
                "doc_type": "act",
                "status": "Доступен",
                "source": "Склад",
                "url": f"/client/api/v1/requests/shipping/{number}/return-act/docx/",
                "open_label": "Скачать МХ-3",
                "download": True,
                "download_name": f"mx-3-{number or int(pk)}.docx",
            }
        )
    return docs


def build_shipping_client_view(*, agency, order, trip_status: str = "") -> dict[str, Any] | None:
    """Build client shipping presentation (phases A+B) from a ShippingOrder."""
    if order is None:
        return None

    routing_status = ""
    snap: dict[str, Any] = {}
    try:
        from logistics.routing_services import routing_snapshot

        snap = routing_snapshot(order) or {}
        routing_status = _text(snap.get("status"))
        if not trip_status:
            trip_status = _text(snap.get("trip_status"))
    except Exception:
        snap = {}
        routing_status = ""

    marketplace = ""
    if getattr(order, "marketplace_id", None):
        marketplace = _text(getattr(getattr(order, "marketplace", None), "name", ""))

    stage_key, stage_label = resolve_client_shipping_stage(
        order_status=getattr(order, "status", "") or "",
        trip_status=trip_status,
        routing_status=routing_status,
        marketplace_name=marketplace,
    )

    packing = None
    try:
        from shipping.packing import _shipping_packing_summary

        packing = _shipping_packing_summary(order)
    except Exception:
        packing = None

    boxes, pallets = _build_boxes_and_pallets(packing, stage_key=stage_key)
    trip_details = _load_trip_details(order) if getattr(order, "pk", None) else {}

    box_fact = _as_int((packing or {}).get("box_count")) or len(boxes)
    pallet_fact = _as_int((packing or {}).get("pallet_count")) or len(pallets)
    box_plan = _as_int(getattr(order, "expected_boxes", 0))

    items = list(order.items.all()) if hasattr(order, "items") else []
    total_requested = sum(_as_int(i.qty_requested) for i in items)
    total_reserved = sum(_as_int(i.qty_reserved) for i in items)
    total_shipped = sum(_as_int(i.qty_shipped) for i in items)

    # Picked ≈ reserved once warehouse work started; formed ≈ units in packing boxes.
    st = _text(getattr(order, "status", "")).lower()
    if st in {"reserved", "storekeeper_accepted", "picking", "packed", "shipped", "partial_shipped"}:
        total_picked = total_reserved
    else:
        total_picked = 0

    formed_units = 0
    if packing and isinstance(packing.get("pallets"), list):
        for pallet in packing["pallets"]:
            if isinstance(pallet, dict):
                formed_units += _as_int(pallet.get("qty"))
    if not formed_units and packing and isinstance(packing.get("pallets"), list):
        pass
    if not formed_units and box_fact and st in {"packed", "shipped", "partial_shipped"}:
        # No unit totals in summary — leave 0 and show boxes separately.
        formed_units = 0

    delta = total_requested - total_shipped if total_shipped or st in {"shipped", "partial_shipped"} else 0
    if st not in {"shipped", "partial_shipped", "packed"} and total_shipped == 0:
        delta_display = 0
    else:
        delta_display = abs(delta) if total_shipped < total_requested else 0

    current_operation = ""
    progress_text = ""
    if stage_key == "assembling":
        if box_fact and st in {"picking", "storekeeper_accepted", "reserved"}:
            current_operation = "Формирование коробов"
        else:
            current_operation = _ASSEMBLY_OPS.get(st, "Комплектация")
        if box_plan > 0:
            progress_text = f"{box_fact} из {box_plan} коробов"
        elif total_reserved and total_requested:
            progress_text = f"Зарезервировано {total_reserved} из {total_requested} шт."
    elif stage_key == "ready":
        current_operation = "Готова к погрузке / передаче логисту"
        if box_fact:
            progress_text = f"{box_fact} коробов" + (f", {pallet_fact} палет" if pallet_fact else "")
    elif stage_key == "in_transit":
        current_operation = "Груз в пути"
    elif stage_key == "delivered":
        current_operation = "Сдана на склад маркетплейса"
    elif stage_key == "closed":
        current_operation = "Заявка закрыта"

    discrepancy_display = _shipping_discrepancy_display(order, warehouse_status=st)
    if discrepancy_display.get("state") in {"done", "pickup"} and stage_key in {"assembling", "ready"}:
        current_operation = discrepancy_display["status_label"]

    planned_ready = ""
    if getattr(order, "planned_ship_date", None):
        planned_ready = order.planned_ship_date.strftime("%d.%m.%Y")
        if getattr(order, "slot_time", None):
            planned_ready = f"{planned_ready} до {order.slot_time.strftime('%H:%M')}"
    elif getattr(order, "slot_date", None):
        planned_ready = order.slot_date.strftime("%d.%m.%Y")
        if getattr(order, "slot_time", None):
            planned_ready = f"{planned_ready} до {order.slot_time.strftime('%H:%M')}"

    supply_type = ""
    if hasattr(order, "get_supply_type_display"):
        supply_type = _text(order.get_supply_type_display())
    else:
        supply_type = _text(getattr(order, "supply_type", ""))

    badges = []
    if marketplace:
        badges.append(marketplace)
    if supply_type:
        badges.append(supply_type)
    if getattr(order, "delivery_type", "") == "marketplace":
        badges.append("FBO")

    lines: list[dict[str, Any]] = []
    discrepancies: list[dict[str, Any]] = []
    for item in items:
        qty_req = _as_int(item.qty_requested)
        qty_res = _as_int(item.qty_reserved)
        qty_ship = _as_int(item.qty_shipped)
        line_delta = qty_req - qty_ship if qty_ship or st in {"shipped", "partial_shipped"} else 0
        mismatch = bool(qty_ship and qty_req and qty_ship != qty_req)
        if mismatch:
            discrepancies.append(
                {
                    "sku_code": _text(item.sku_code),
                    "name": _text(item.name),
                    "size": _text(item.size),
                    "expected_qty": qty_req,
                    "factual_qty": qty_ship,
                    "delta_qty": qty_ship - qty_req,
                }
            )
        lines.append(
            {
                "sku_code": _text(item.sku_code),
                "name": _text(item.name),
                "size": _text(item.size),
                "barcode": _text(item.barcode),
                "qty_requested": qty_req,
                "qty_reserved": qty_res,
                "qty_picked": qty_res if total_picked else 0,
                "qty_processed": "Нет данных",
                "qty_formed": "Нет данных",
                "qty_shipped": qty_ship,
                "delta_qty": line_delta if (qty_ship or st in {"shipped", "partial_shipped"}) else 0,
                "line_status": _line_status(
                    stage_key=stage_key,
                    qty_requested=qty_req,
                    qty_reserved=qty_res,
                    qty_shipped=qty_ship,
                ),
                "mismatch": mismatch,
                "comment": _client_item_comment(item.comment),
            }
        )

    metrics = [
        {"key": "requested", "label": "Заявлено", "value": _metric_display(total_requested), "unit": "шт."},
        {"key": "shipped", "label": "Отгружено", "value": _metric_display(total_shipped), "unit": "шт."},
        {"key": "delta", "label": "Расхождение", "value": _metric_display(delta_display), "unit": "шт."},
        {
            "key": "boxes",
            "label": "Короба",
            "value": f"{box_fact} из {box_plan}" if box_plan else _metric_display(box_fact),
            "unit": "",
        },
        {
            "key": "pallets",
            "label": "Палеты",
            "value": _metric_display(pallet_fact if pallet_fact else None, empty="Не начато")
            if not pallet_fact and stage_key in {"confirmed", "draft", "assembling"}
            else _metric_display(pallet_fact),
            "unit": "",
        },
    ]

    declared = [
        {"label": "Номер заявки", "value": _text(order.number)},
        {"label": "Дата подачи", "value": _fmt_dt(getattr(order, "created_at", None))},
        {"label": "Маркетплейс", "value": marketplace or "—"},
        {"label": "Тип поставки", "value": supply_type or "—"},
        {"label": "Склад назначения", "value": _text(getattr(order, "destination_warehouse", "")) or "—"},
        {
            "label": "Слот",
            "value": (
                f"{order.slot_date.isoformat()}"
                + (f" {order.slot_time.strftime('%H:%M')}" if getattr(order, "slot_time", None) else "")
                if getattr(order, "slot_date", None)
                else "—"
            ),
        },
        {
            "label": "ШК / номер поставки",
            "value": _text(
                getattr(order, "wb_supply_barcode", "")
                or getattr(order, "shipping_barcode", "")
                or getattr(order, "supply_number", "")
            )
            or "—",
        },
        {"label": "Заявлено единиц", "value": str(total_requested)},
        {"label": "План коробов", "value": str(box_plan) if box_plan else "—"},
        {"label": "Комментарий клиента", "value": _text(getattr(order, "comment", "")) or "—"},
    ]

    warehouse_done = [
        {"label": "Сформировано палет", "value": str(pallet_fact) if pallet_fact else "Не начато"},
        {"label": "Отгружено", "value": f"{total_shipped} шт."},
        {
            "label": "Расхождение",
            "value": f"{delta_display} шт." if delta_display else "0 шт.",
        },
        {
            "label": "Дата отгрузки",
            "value": _fmt_dt(getattr(order, "shipped_at", None)) or "Ещё не отгружено",
        },
    ]

    attention_items: list[dict[str, Any]] = []
    if stage_key != "canceled":
        disc_state = _text(discrepancy_display.get("state")).lower()
        if discrepancies:
            attention_items.append(
                {
                    "type": "discrepancy",
                    "title": f"Расхождения по {len(discrepancies)} позициям",
                    "description": "Фактически отгружено меньше или больше заявленного.",
                }
            )
        if (
            box_plan
            and box_fact
            and box_fact < box_plan
            and stage_key in {"assembling", "ready"}
            and disc_state not in {"done", "resolved", "closed", "approved", "pickup"}
        ):
            attention_items.append(
                {
                    "type": "boxes",
                    "title": f"Не сформировано коробов: {box_plan - box_fact}",
                    "description": f"План {box_plan}, факт {box_fact}.",
                }
            )
        if disc_state and disc_state not in {"done", "resolved", "closed", "approved", "pickup"}:
            attention_items.append(
                {
                    "type": "warehouse_discrepancy",
                    "title": discrepancy_display.get("status_label") or "Есть складское расхождение",
                    "description": discrepancy_display.get("description") or "Ожидается решение менеджера.",
                }
            )

    logistics = _build_logistics_rows(order=order, snap=snap, trip=trip_details, stage_key=stage_key)
    warehouse_info = _build_warehouse_info(
        stage_key=stage_key,
        stage_label=stage_label,
        current_operation=current_operation,
        progress_text=progress_text,
        planned_ready=planned_ready,
        box_fact=box_fact,
        box_plan=box_plan,
        pallet_fact=pallet_fact,
        total_reserved=total_reserved,
        total_requested=total_requested,
        attention_items=attention_items,
    )
    if discrepancy_display:
        warehouse_info.append(
            {
                "label": "Расхождение",
                "value": discrepancy_display.get("status_label") or "Есть складское расхождение",
            }
        )
    documents = _build_documents(order=order, stage_key=stage_key, agency=agency)

    return {
        "enabled": True,
        "stage_key": stage_key,
        "status_label": stage_label,
        "scale": _stage_scale(stage_key),
        "current_operation": current_operation,
        "progress_text": progress_text,
        "planned_ready": planned_ready,
        "badges": badges,
        "metrics": metrics,
        "declared": declared,
        "warehouse_done": warehouse_done,
        "attention": attention_items,
        "lines": lines,
        "discrepancies": discrepancies,
        "has_mismatch": bool(discrepancies),
        "boxes": boxes,
        "pallets": pallets,
        "logistics": logistics,
        "warehouse_info": warehouse_info,
        "discrepancy": discrepancy_display,
        "documents": documents,
        "tabs": [
            {"key": "composition", "label": "Состав отгрузки"},
            {"key": "boxes", "label": "Короба и палеты"},
            {"key": "warehouse", "label": "Информация от склада"},
            {"key": "logistics", "label": "Логистика"},
            {"key": "documents", "label": "Документы"},
            {"key": "history", "label": "История"},
        ],
        "updated_at": _fmt_dt(timezone.now()),
        "meta_extra": {
            "trip_status": _text(trip_status),
            "routing_status": routing_status,
            "warehouse_status": st,
        },
    }
