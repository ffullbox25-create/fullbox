"""Read-only client view of a receiving request (timeline + summary).

Does not write warehouse state. Maps existing audit payload / composition
into a safe client-facing presentation for the LK detail card.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.utils import timezone

_MONTHS_RU = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

CLIENT_RECEIVING_STAGES: list[tuple[str, str]] = [
    ("created", "Заявка создана"),
    ("pending", "На подтверждении"),
    ("confirmed", "Подтверждена"),
    ("receiving", "Приёмка начата"),
    ("accepted", "Принята на склад"),
    ("done", "Завершена"),
]

_STAGE_INDEX = {key: idx for idx, (key, _) in enumerate(CLIENT_RECEIVING_STAGES)}

_PLACE_TYPE_LABELS = {
    "box": "Короба",
    "boxes": "Короба",
    "короб": "Короба",
    "короба": "Короба",
    "pallet": "Палеты",
    "pallets": "Палеты",
    "палета": "Палеты",
    "палеты": "Палеты",
    "паллета": "Палеты",
    "паллеты": "Палеты",
    "mixed": "Смешанная поставка",
    "mix": "Смешанная поставка",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse_dt(value: Any):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    raw = _text(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
            try:
                return datetime.strptime(raw[:19], fmt)
            except ValueError:
                continue
    return None


def format_client_datetime(value: Any) -> str:
    """Human-readable datetime for LK: «16 июля 2026, 11:20»."""
    dt = _parse_dt(value)
    if dt is None:
        return _text(value)
    try:
        if timezone.is_aware(dt):
            dt = timezone.localtime(dt)
        month = _MONTHS_RU[dt.month] if 1 <= dt.month <= 12 else ""
        if month:
            return f"{dt.day} {month} {dt.year}, {dt.hour:02d}:{dt.minute:02d}"
        return dt.strftime("%d.%m.%Y, %H:%M")
    except Exception:
        try:
            return dt.strftime("%d.%m.%Y, %H:%M")
        except Exception:
            return _text(value)


def localize_place_type(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    mapped = _PLACE_TYPE_LABELS.get(raw.lower())
    if mapped:
        return mapped
    # Already localized Russian labels pass through.
    return raw


def resolve_client_receiving_stage(
    *,
    status_value: str,
    status_label: str,
    bucket: str,
    has_fact: bool,
    payload: dict | None = None,
) -> tuple[str, str]:
    """Map system status → client stage key + primary label."""
    st = _text(status_value).lower()
    label = _text(status_label).lower()
    buck = _text(bucket).lower()
    data = payload if isinstance(payload, dict) else {}

    if st in {"cancelled", "canceled"} or "отмен" in label:
        return "canceled", "Отменена"
    if st == "draft" or "черновик" in label:
        return "created", "Черновик"
    if data.get("act_sent") and str(data.get("act_client_response") or "").lower() not in {
        "confirmed",
        "dispute",
    }:
        return "accepted", "Ожидает подтверждения акта"
    if buck == "done" or st in {"done", "completed", "closed", "finished"} or any(
        token in label for token in ("выполн", "заверш", "закрыт", "размещен", "размещён")
    ):
        return "done", "Завершена"
    if has_fact or any(token in label for token in ("приемк", "приёмк", "принят", "на склад")):
        if any(token in label for token in ("принят", "на склад", "размещ")):
            return "accepted", "Принята на склад"
        return "receiving", "Приёмка начата"
    if buck == "warehouse" or any(token in label for token in ("склад", "в работ", "в обработ", "ожидает поставк")):
        return "confirmed", "Подтверждена"
    if st in {"sent_unconfirmed", "send", "submitted"} or any(
        token in label for token in ("ждет подтверждения", "ждёт подтверждения", "на подтвержд", "согласован")
    ):
        return "pending", "Ожидает подтверждения менеджером"
    if buck == "manager":
        return "pending", "Ожидает подтверждения менеджером"
    if buck == "client":
        return "created", "Черновик"
    return "pending", "Ожидает подтверждения менеджером"


def _stage_scale(current_key: str) -> list[dict[str, Any]]:
    if current_key == "canceled":
        return [{"key": key, "label": label, "state": "muted"} for key, label in CLIENT_RECEIVING_STAGES]
    current_idx = _STAGE_INDEX.get(current_key, 1)
    rows = []
    for idx, (key, label) in enumerate(CLIENT_RECEIVING_STAGES):
        if idx < current_idx:
            state = "done"
        elif idx == current_idx:
            state = "current"
        else:
            state = "upcoming"
        rows.append({"key": key, "label": label, "state": state})
    return rows


def _status_hint(stage_key: str) -> str:
    return {
        "created": "Заполните состав и отправьте заявку менеджеру FullBox.",
        "pending": (
            "Заявка передана менеджеру FullBox. После проверки даты и состава "
            "поставки статус изменится на «Подтверждена»."
        ),
        "confirmed": "Менеджер подтвердил заявку. Ожидайте прибытия товара на склад FullBox.",
        "receiving": "Склад начал приёмку. По мере приёмки появятся фактические количества.",
        "accepted": "Товар принят на склад. Проверьте акт приёмки, если он отправлен вам.",
        "done": "Заявка завершена. Товар доступен в остатках личного кабинета.",
        "canceled": "Заявка отменена.",
    }.get(stage_key, "")


def _meta_map(meta: list[dict[str, Any]] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in meta or []:
        if not isinstance(row, dict):
            continue
        label = _text(row.get("label"))
        value = row.get("value")
        if label and value not in (None, ""):
            result[label] = _text(value)
    return result


def _summary_cards(
    *,
    meta_src: dict[str, Any],
    meta_rows: list[dict[str, Any]],
    lines: list[dict[str, Any]],
    show_fact: bool,
) -> list[dict[str, str]]:
    by_label = _meta_map(meta_rows)
    eta_raw = meta_src.get("eta_at") or by_label.get("Ожидаемая дата") or ""
    place_raw = meta_src.get("place_type") or by_label.get("Тип места") or ""
    expected_boxes = meta_src.get("expected_boxes") or by_label.get("Ожидаемые короба") or ""
    sku_count = len(lines)
    planned_units = sum(_as_int(row.get("qty_planned")) for row in lines)
    actual_units = sum(
        _as_int(row.get("qty_actual")) for row in lines if row.get("qty_actual") is not None
    )

    cards: list[dict[str, str]] = []
    eta_fmt = format_client_datetime(eta_raw) if eta_raw else ""
    if eta_fmt:
        cards.append({"key": "eta", "label": "Ожидаемая дата", "value": eta_fmt})
    place_label = localize_place_type(place_raw)
    if place_label:
        cards.append({"key": "place_type", "label": "Тип поставки", "value": place_label})
    if sku_count:
        cards.append({"key": "sku_count", "label": "Позиций", "value": f"{sku_count} SKU"})
    if planned_units:
        cards.append({"key": "planned_units", "label": "Количество", "value": f"{planned_units} ед."})
    boxes_val = _as_int(expected_boxes)
    if boxes_val > 0:
        cards.append({"key": "expected_boxes", "label": "Коробов", "value": str(boxes_val)})
    if show_fact and actual_units:
        cards.append({"key": "actual_units", "label": "Принято", "value": f"{actual_units} ед."})

    # Optional extras only when present in payload/meta.
    for key, label in (
        ("pallet_count", "Палет"),
        ("total_weight", "Общий вес"),
        ("total_volume", "Общий объём"),
        ("warehouse_zone", "Зона приёмки"),
        ("contact_name", "Контактное лицо"),
    ):
        value = meta_src.get(key) or by_label.get(label)
        if value not in (None, ""):
            cards.append({"key": key, "label": label, "value": _text(value)})
    return cards


def _enrich_lines(lines: list[dict[str, Any]], *, show_fact: bool) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for idx, row in enumerate(lines, start=1):
        item = dict(row)
        item["row_no"] = idx
        plan = _as_int(item.get("qty_planned"))
        actual = item.get("qty_actual")
        if not show_fact or actual is None:
            item["delta_label"] = "Не принято" if show_fact else ""
            item["delta_tone"] = "pending" if show_fact else ""
            item["qty_actual_display"] = ""
        else:
            actual_i = _as_int(actual)
            item["qty_actual_display"] = str(actual_i)
            delta = actual_i - plan
            if delta == 0:
                item["delta_label"] = "Совпадает"
                item["delta_tone"] = "ok"
            elif delta < 0:
                item["delta_label"] = f"Недостача: {abs(delta)}"
                item["delta_tone"] = "shortage"
            else:
                item["delta_label"] = f"Излишек: {delta}"
                item["delta_tone"] = "excess"
        if item.get("box_qty") in (None, "", "—"):
            item["box_qty_display"] = ""
        else:
            item["box_qty_display"] = _text(item.get("box_qty"))
        if not _text(item.get("size")):
            item["size"] = "—"
        enriched.append(item)
    return enriched


def _columns(*, show_fact: bool) -> list[dict[str, str]]:
    cols = [
        {"key": "row_no", "label": "№"},
        {"key": "barcode", "label": "Штрихкод"},
        {"key": "sku_code", "label": "Артикул"},
        {"key": "name", "label": "Наименование"},
        {"key": "size", "label": "Размер"},
        {"key": "qty_planned", "label": "План"},
    ]
    if show_fact:
        cols.extend(
            [
                {"key": "qty_actual_display", "label": "Факт"},
                {"key": "box_qty_display", "label": "Принято в коробах"},
                {"key": "delta_label", "label": "Расхождение"},
            ]
        )
    return cols


def _list_receiving_attachments(*, agency, order_id: str) -> list[dict[str, Any]]:
    try:
        from orders.models import ReceivingOrderAttachment
        from orders.web_ui import _active_receiving_attachments
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    try:
        attachments = list(_active_receiving_attachments(str(order_id), agency))
    except Exception:
        attachments = list(
            ReceivingOrderAttachment.objects.filter(order_id=str(order_id), agency=agency).order_by("-uploaded_at")
        )
    for attachment in attachments:
        size = ""
        try:
            size_bytes = int(getattr(attachment.file, "size", 0) or 0)
            if size_bytes >= 1024 * 1024:
                size = f"{size_bytes / (1024 * 1024):.1f} МБ"
            elif size_bytes >= 1024:
                size = f"{size_bytes / 1024:.0f} КБ"
            elif size_bytes:
                size = f"{size_bytes} Б"
        except Exception:
            size = ""
        filename = _text(getattr(attachment, "filename", "")) or "Файл"
        ext = ""
        if "." in filename:
            ext = filename.rsplit(".", 1)[-1].upper()
        rows.append(
            {
                "id": getattr(attachment, "id", None),
                "filename": filename,
                "format": ext,
                "size": size,
                "uploaded_at": format_client_datetime(getattr(attachment, "uploaded_at", None)),
                "url": f"/orders/receiving/{order_id}/attachments/{attachment.id}/",
            }
        )
    return rows


def build_receiving_client_view(
    *,
    agency,
    order_id: str,
    entries: list,
    payload: dict[str, Any],
    body: dict[str, Any],
    status_label: str,
    bucket: str,
    created_at=None,
) -> dict[str, Any]:
    """Assemble client-facing receiving card extras (read-only)."""
    lines = list(body.get("lines") or [])
    show_fact = any(row.get("qty_actual") is not None for row in lines)
    status_value = _text(payload.get("status") or payload.get("submit_action"))
    stage_key, client_status = resolve_client_receiving_stage(
        status_value=status_value,
        status_label=status_label,
        bucket=bucket,
        has_fact=show_fact,
        payload=payload,
    )
    # Prefer explicit friendly label for classic waiting state.
    if stage_key == "pending":
        client_status = "Ожидает подтверждения менеджером"

    meta_src = payload if isinstance(payload, dict) else {}
    summary = _summary_cards(
        meta_src=meta_src,
        meta_rows=list(body.get("meta") or []),
        lines=lines,
        show_fact=show_fact,
    )
    enriched_lines = _enrich_lines(lines, show_fact=show_fact)
    planned_units = sum(_as_int(row.get("qty_planned")) for row in enriched_lines)
    comment = _text(meta_src.get("comment"))
    first_created = created_at
    if first_created is None and entries:
        first_created = getattr(entries[0], "created_at", None)

    agency_name = ""
    if agency is not None:
        agency_name = _text(getattr(agency, "agn_name", None) or getattr(agency, "name", None))

    title = "Заявка на приёмку"
    # Avoid technical "без указания товара" when composition exists.
    raw_title = _text(meta_src.get("order_title") or meta_src.get("title") or "")
    if raw_title and "без указания" not in raw_title.lower():
        supply_name = raw_title
    else:
        supply_name = ""

    return {
        "enabled": True,
        "stage_key": stage_key,
        "status_label": client_status,
        "status_hint": _status_hint(stage_key),
        "scale": _stage_scale(stage_key),
        "summary": summary,
        "show_fact_columns": show_fact,
        "sku_count": len(enriched_lines),
        "planned_units": planned_units,
        "lines": enriched_lines,
        "columns": _columns(show_fact=show_fact),
        "comment": comment,
        "comment_empty_text": "Комментарий не указан.",
        "attachments": _list_receiving_attachments(agency=agency, order_id=str(order_id)),
        "agency_name": agency_name,
        "created_at": format_client_datetime(first_created),
        "created_at_raw": first_created.isoformat() if hasattr(first_created, "isoformat") else "",
        "page_title": title,
        "supply_name": supply_name,
        "lines_subtitle": f"{len(enriched_lines)} позиций · {planned_units} единиц"
        if enriched_lines
        else "Состав пока не заполнен",
    }
