"""Client-facing request titles for the LK.

The helpers are read-only: they format already available request data into a
human title so clients can understand the request without decoding PR/OTG/OBR.
"""

from __future__ import annotations

import re
from typing import Any


_GENERIC_TITLES = {
    "",
    "заявка",
    "заявка на приёмку",
    "заявка на приемку",
    "заявка на отгрузку",
    "заявка на обработку",
    "прочая заявка",
}

_DELIVERY_LABELS = {
    "marketplace": "Маркетплейс",
    "courier": "Курьер",
    "pickup": "Самовывоз",
    "transfer": "Перемещение",
    "other": "Другая отгрузка",
}

_PLACE_LABELS = {
    "box": "Короба",
    "boxes": "Короба",
    "короб": "Короба",
    "короба": "Короба",
    "pallet": "Палеты",
    "pallets": "Палеты",
    "палета": "Палеты",
    "палеты": "Палеты",
    "mixed": "Смешанная поставка",
    "mix": "Смешанная поставка",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, str):
        match = re.search(r"-?\d+", value.replace("\xa0", " "))
        if not match:
            return 0
        value = match.group(0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _short(value: Any, *, limit: int = 48) -> str:
    text = " ".join(_text(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _first(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text and text != "—" and text != "-":
            return text
    return ""


def _base_title(value: Any) -> str:
    title = _short(value, limit=72)
    low = title.lower()
    if low in _GENERIC_TITLES or low.startswith("заявка "):
        return ""
    return title


def _label_count(value: Any, one: str, few: str, many: str) -> str:
    count = _as_int(value)
    if count <= 0:
        return ""
    tail = count % 100
    if 11 <= tail <= 14:
        word = many
    else:
        last = count % 10
        if last == 1:
            word = one
        elif 2 <= last <= 4:
            word = few
        else:
            word = many
    return f"{count} {word}"


def _summary_value(view: dict[str, Any] | None, key: str, label: str = "") -> str:
    for row in (view or {}).get("summary") or []:
        if not isinstance(row, dict):
            continue
        if _text(row.get("key")) == key or (label and _text(row.get("label")) == label):
            return _text(row.get("value"))
    return ""


def _declared_value(view: dict[str, Any] | None, label: str) -> str:
    for row in (view or {}).get("declared") or []:
        if isinstance(row, dict) and _text(row.get("label")) == label:
            return _text(row.get("value"))
    return ""


def _meta_value(body: dict[str, Any] | None, label: str) -> str:
    for row in (body or {}).get("meta") or []:
        if isinstance(row, dict) and _text(row.get("label")) == label:
            return _text(row.get("value"))
    return ""


def _metric_value(view: dict[str, Any] | None, key: str) -> str:
    for row in (view or {}).get("metrics") or []:
        if isinstance(row, dict) and _text(row.get("key")) == key:
            value = _text(row.get("value"))
            unit = _text(row.get("unit"))
            return f"{value} {unit}".strip() if value else ""
    return ""


def _lines_count(lines: list[dict[str, Any]] | None) -> int:
    seen: set[str] = set()
    count = 0
    for row in lines or []:
        if not isinstance(row, dict):
            continue
        key = _text(row.get("sku_code") or row.get("article") or row.get("barcode") or row.get("name"))
        if key:
            low = key.lower()
            if low in seen:
                continue
            seen.add(low)
        count += 1
    return count


def _lines_qty(lines: list[dict[str, Any]] | None, *keys: str) -> int:
    total = 0
    for row in lines or []:
        if not isinstance(row, dict):
            continue
        for key in keys:
            value = _as_int(row.get(key))
            if value:
                total += value
                break
    return total


def _payload_items(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    payload = payload if isinstance(payload, dict) else {}
    for key in ("items", "stock_rows", "size_rows", "products", "lines"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _first_line_name(lines: list[dict[str, Any]] | None, payload: dict[str, Any] | None = None) -> str:
    for row in lines or []:
        if not isinstance(row, dict):
            continue
        name = _first(row.get("name"), row.get("product_name"), row.get("sku_code"), row.get("article"))
        if name:
            return _short(name, limit=54)
    for row in _payload_items(payload):
        name = _first(row.get("name"), row.get("product_name"), row.get("sku_code"), row.get("article"), row.get("sku"))
        if name:
            return _short(name, limit=54)
    return ""


def _marketplace_name(order: Any = None, payload: dict[str, Any] | None = None) -> str:
    if order is not None:
        marketplace = getattr(order, "marketplace", None)
        name = _text(getattr(marketplace, "name", ""))
        if name:
            return name
    payload = payload if isinstance(payload, dict) else {}
    return _first(payload.get("marketplace_name"), payload.get("marketplace"), payload.get("marketplace_label"))


def _destination_from_order(order: Any) -> str:
    if order is None:
        return ""
    delivery_type = _text(getattr(order, "delivery_type", "")).lower()
    if delivery_type == "transfer":
        return _first(getattr(order, "destination_address", ""), getattr(order, "destination_warehouse", ""))
    try:
        destinations = list(order.destinations.all())
    except Exception:
        destinations = []
    if destinations:
        names = [_text(getattr(dest, "destination_warehouse", "")) for dest in destinations[:2]]
        names = [name for name in names if name]
        if names:
            suffix = f" +{len(destinations) - 2}" if len(destinations) > 2 else ""
            return ", ".join(names) + suffix
    return _first(
        getattr(order, "destination_warehouse", ""),
        getattr(order, "transit_address", ""),
        getattr(order, "destination_address", ""),
    )


def _destination_from_payload(payload: dict[str, Any] | None, body: dict[str, Any] | None = None) -> str:
    payload = payload if isinstance(payload, dict) else {}
    body = body if isinstance(body, dict) else {}
    for label in ("Назначение перемещения", "Склад назначения", "Конечные склады Ozon", "Транзитный склад"):
        value = _meta_value(body, label)
        if value:
            return value
    destinations = payload.get("destination_warehouses")
    if isinstance(destinations, list):
        names = [_text(value) for value in destinations[:2] if _text(value)]
        if names:
            suffix = f" +{len(destinations) - 2}" if len(destinations) > 2 else ""
            return ", ".join(names) + suffix
    return _first(
        payload.get("destination_warehouse"),
        payload.get("transit_address"),
        payload.get("destination_address"),
    )


def _receiving_display_title(
    *,
    payload: dict[str, Any] | None,
    base_title: str,
    body: dict[str, Any] | None,
    receiving_view: dict[str, Any] | None,
) -> str:
    payload = payload if isinstance(payload, dict) else {}
    lines = (receiving_view or {}).get("lines") or (body or {}).get("lines") or _payload_items(payload)
    place = _PLACE_LABELS.get(_text(payload.get("place_type")).lower()) or _summary_value(
        receiving_view, "place_type", "Тип поставки"
    )
    if place:
        place = _PLACE_LABELS.get(place.lower(), place)
    sku_count = _as_int((receiving_view or {}).get("sku_count")) or _lines_count(lines)
    planned_units = _as_int((receiving_view or {}).get("planned_units")) or _lines_qty(lines, "qty_planned", "qty")
    boxes = _as_int(_summary_value(receiving_view, "expected_boxes", "Коробов") or payload.get("expected_boxes"))
    parts = []
    if place:
        parts.append(place)
    if sku_count:
        parts.append(f"{sku_count} SKU")
    if boxes:
        parts.append(_label_count(boxes, "короб", "короба", "коробов"))
    if planned_units:
        parts.append(_label_count(planned_units, "ед.", "ед.", "ед."))
    title = "Приёмка"
    if parts:
        title += ": " + ", ".join(parts[:4])
    tail = _first_line_name(lines, payload) or _base_title(base_title)
    if tail:
        title += " — " + tail
    return title


def _processing_display_title(
    *,
    payload: dict[str, Any] | None,
    base_title: str,
    body: dict[str, Any] | None,
    processing_view: dict[str, Any] | None,
) -> str:
    payload = payload if isinstance(payload, dict) else {}
    lines = (body or {}).get("lines") or _payload_items(payload)
    article_from = _first(payload.get("article"), _meta_value(body, "Артикул"))
    article_to = _first(
        payload.get("article_change_target_article"),
        payload.get("target_article"),
        payload.get("article_to"),
    )
    if article_from and article_to and article_from != article_to:
        head = f"Обработка: {article_from} → {article_to}"
    else:
        service = _first(payload.get("processing_type_label"), payload.get("service_name"), payload.get("category_label"))
        head = f"Обработка: {service}" if service else "Обработка"
    qty = _lines_qty(lines, "qty", "qty_requested", "factual_qty", "recount_qty")
    sku_count = _lines_count(lines)
    parts = []
    if sku_count:
        parts.append(f"{sku_count} SKU")
    if qty:
        parts.append(_label_count(qty, "шт.", "шт.", "шт."))
    name = _first_line_name(lines, payload) or _first(payload.get("product_name"), _meta_value(body, "Товар"))
    if name:
        parts.append(_short(name, limit=46))
    if not parts:
        fallback = _base_title(base_title)
        if fallback:
            parts.append(fallback)
    return head + (": " + ", ".join(parts[:3]) if parts else "")


def _shipping_display_title(
    *,
    payload: dict[str, Any] | None,
    base_title: str,
    body: dict[str, Any] | None,
    shipping_view: dict[str, Any] | None,
    shipping_order: Any = None,
) -> str:
    payload = payload if isinstance(payload, dict) else {}
    delivery_type = _text(
        getattr(shipping_order, "delivery_type", "") if shipping_order is not None else payload.get("delivery_type")
    ).lower()
    delivery_label = _DELIVERY_LABELS.get(delivery_type, "")
    if shipping_order is not None and hasattr(shipping_order, "get_delivery_type_display"):
        try:
            delivery_label = _text(shipping_order.get_delivery_type_display()) or delivery_label
        except Exception:
            pass
    marketplace = _marketplace_name(shipping_order, payload)
    destination = _destination_from_order(shipping_order) if shipping_order is not None else ""
    destination = destination or _declared_value(shipping_view, "Склад назначения") or _destination_from_payload(payload, body)
    if delivery_type == "transfer":
        head = "Перемещение"
    elif delivery_type == "pickup":
        head = "Самовывоз"
    elif delivery_type == "courier":
        head = "Курьер"
    elif marketplace:
        head = f"Отгрузка: {marketplace}"
    elif delivery_label:
        head = f"Отгрузка: {delivery_label}"
    else:
        head = "Отгрузка"
    if destination:
        head += f" → {_short(destination, limit=42)}"
    boxes = _as_int(getattr(shipping_order, "expected_boxes", 0) if shipping_order is not None else 0)
    if not boxes:
        boxes = _as_int(_metric_value(shipping_view, "boxes") or payload.get("expected_boxes"))
    units = _as_int(getattr(shipping_order, "title_units", 0) if shipping_order is not None else 0)
    if not units:
        units = _as_int(_metric_value(shipping_view, "requested") or _declared_value(shipping_view, "Заявлено единиц"))
    if not units:
        units = _lines_qty((body or {}).get("lines"), "qty_requested", "qty")
    parts = []
    if boxes:
        parts.append(_label_count(boxes, "короб", "короба", "коробов"))
    if units:
        parts.append(_label_count(units, "шт.", "шт.", "шт."))
    if parts:
        head += " · " + ", ".join(parts[:2])
    fallback = _base_title(base_title)
    if not parts and not destination and fallback:
        head += f" · {fallback}"
    return head


def _other_display_title(*, payload: dict[str, Any] | None, base_title: str) -> str:
    payload = payload if isinstance(payload, dict) else {}
    category = _first(payload.get("category_label"), payload.get("category"))
    description = _first(payload.get("description"), base_title)
    if category and description and category.lower() not in description.lower():
        return f"{category}: {_short(description, limit=58)}"
    if description:
        return _short(description, limit=70)
    return category or "Другая заявка"


def build_request_display_title(
    *,
    order_type: str,
    order_id: Any = "",
    payload: dict[str, Any] | None = None,
    base_title: str = "",
    body: dict[str, Any] | None = None,
    receiving_view: dict[str, Any] | None = None,
    processing_view: dict[str, Any] | None = None,
    shipping_view: dict[str, Any] | None = None,
    shipping_order: Any = None,
) -> str:
    """Return a human title for the client request list/detail."""
    raw = _text(order_type).lower()
    if raw == "packing":
        raw = "processing"
    if raw == "receiving":
        return _receiving_display_title(
            payload=payload,
            base_title=base_title,
            body=body,
            receiving_view=receiving_view,
        )
    if raw == "shipping":
        return _shipping_display_title(
            payload=payload,
            base_title=base_title,
            body=body,
            shipping_view=shipping_view,
            shipping_order=shipping_order,
        )
    if raw == "processing":
        return _processing_display_title(
            payload=payload,
            base_title=base_title,
            body=body,
            processing_view=processing_view,
        )
    if raw == "other":
        return _other_display_title(payload=payload, base_title=base_title)
    return _base_title(base_title) or f"Заявка {order_id}"
