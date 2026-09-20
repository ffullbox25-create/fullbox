from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal


SKU_AUDIT_FIELDS = (
    ("sku_code", "Артикул", ""),
    ("name", "Наименование", ""),
    ("brand", "Бренд", ""),
    ("agency_name", "Клиент", ""),
    ("market_name", "Маркетплейс", ""),
    ("color", "Цвет", ""),
    ("color_ref_name", "Цвет из справочника", ""),
    ("size", "Размер", ""),
    ("name_print", "Название для печати", ""),
    ("code", "Основной штрихкод", ""),
    ("barcodes", "Штрихкоды", ""),
    ("img", "Ссылка на фото", ""),
    ("img_comment", "Комментарий к фото", ""),
    ("gender", "Пол", ""),
    ("season", "Сезон", ""),
    ("additional_name", "Дополнительное наименование", ""),
    ("composition", "Состав", ""),
    ("made_in", "Страна производства", ""),
    ("cr_product_date", "Дата производства", ""),
    ("end_product_date", "Срок годности", ""),
    ("sign_akciz", "Акциз", ""),
    ("tovar_category", "Категория", ""),
    ("use_nds", "НДС", ""),
    ("vid_tovar", "Вид товара", ""),
    ("type_tovar", "Тип товара", ""),
    ("stor_unit_name", "Склад хранения", ""),
    ("weight_kg", "Вес", "кг"),
    ("weight_net_kg", "Вес нетто", "кг"),
    ("weight_gross_kg", "Вес брутто", "кг"),
    ("volume", "Объём", "м³"),
    ("length_mm", "Длина", "мм"),
    ("width_mm", "Ширина", "мм"),
    ("height_mm", "Высота", "мм"),
    ("honest_sign", "Честный знак", ""),
    ("description", "Описание", ""),
    ("source", "Источник", ""),
    ("source_reference", "Внешний ID", ""),
    ("deleted", "Удалён", ""),
)


def _json_value(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _related_name(obj, attr: str) -> str | None:
    related = getattr(obj, attr, None)
    if related is None:
        return None
    return str(related)


def sku_audit_snapshot(sku) -> dict:
    """Return a JSON-safe SKU snapshot suitable for field-level history."""
    if not sku:
        return {}
    snapshot = {
        "schema_version": 2,
        "id": sku.pk,
        "agency_id": getattr(sku, "agency_id", None),
        "market_id": getattr(sku, "market_id", None),
        "color_ref_id": getattr(sku, "color_ref_id", None),
        "stor_unit_id": getattr(sku, "stor_unit_id", None),
        "agency_name": _related_name(sku, "agency"),
        "market_name": _related_name(sku, "market"),
        "color_ref_name": _related_name(sku, "color_ref"),
        "stor_unit_name": _related_name(sku, "stor_unit"),
    }
    direct_fields = {
        field
        for field, _label, _unit in SKU_AUDIT_FIELDS
        if field not in {"agency_name", "market_name", "color_ref_name", "stor_unit_name", "barcodes"}
    }
    for field in direct_fields:
        snapshot[field] = _json_value(getattr(sku, field, None))
    snapshot["barcodes"] = [
        {
            "value": str(barcode.value or "").strip(),
            "size": str(barcode.size or "").strip(),
            "is_primary": bool(barcode.is_primary),
        }
        for barcode in sku.barcodes.all().order_by("-is_primary", "value", "id")
    ]
    snapshot["created_at"] = _json_value(getattr(sku, "created_at", None))
    snapshot["updated_at"] = _json_value(getattr(sku, "updated_at", None))
    return snapshot


def _display_value(value, unit: str = "") -> str:
    if value is None or value == "":
        return "не заполнено"
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if isinstance(value, list):
        values = []
        for item in value:
            if not isinstance(item, dict):
                values.append(str(item))
                continue
            text = str(item.get("value") or "").strip()
            size = str(item.get("size") or "").strip()
            if size:
                text = f"{text} ({size})"
            if item.get("is_primary"):
                text = f"{text} [основной]"
            if text:
                values.append(text)
        return ", ".join(values) or "не заполнено"
    text = str(value)
    if unit and text != "не заполнено":
        return f"{text} {unit}"
    return text


def sku_audit_changes(before: dict | None, after: dict | None) -> list[dict]:
    before = before or {}
    after = after or {}
    changes = []
    for field, label, unit in SKU_AUDIT_FIELDS:
        old_value = before.get(field)
        new_value = after.get(field)
        if old_value == new_value:
            continue
        changes.append(
            {
                "field": field,
                "label": label,
                "before": old_value,
                "after": new_value,
                "before_display": _display_value(old_value, unit),
                "after_display": _display_value(new_value, unit),
            }
        )
    return changes


def build_sku_audit_snapshot(
    sku,
    *,
    before: dict | None = None,
    source: str = "",
    marketplace: str = "",
) -> dict:
    snapshot = sku_audit_snapshot(sku)
    snapshot["_audit"] = {
        "source": str(source or "").strip(),
        "marketplace": str(marketplace or "").strip().upper(),
        "changes": sku_audit_changes(before, snapshot) if before is not None else [],
    }
    return snapshot


def sku_audit_change_rows(snapshot: dict | None) -> list[dict]:
    if not isinstance(snapshot, dict):
        return []
    audit_data = snapshot.get("_audit")
    if not isinstance(audit_data, dict):
        return []
    rows = audit_data.get("changes")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def sku_audit_description(action: str, snapshot: dict, *, fallback: str = "") -> str:
    audit_data = snapshot.get("_audit") if isinstance(snapshot, dict) else {}
    audit_data = audit_data if isinstance(audit_data, dict) else {}
    marketplace = str(audit_data.get("marketplace") or "").strip().upper()
    source = str(audit_data.get("source") or "").strip().lower()
    prefix = f"Синхронизация {marketplace}" if marketplace else "Изменение через интерфейс"
    if action == "create":
        return f"{prefix}: карточка SKU создана."
    changes = sku_audit_change_rows(snapshot)
    if not changes:
        return fallback or f"{prefix}: поля карточки не изменились."
    labels = [str(row.get("label") or row.get("field") or "поле") for row in changes]
    visible = ", ".join(labels[:8])
    if len(labels) > 8:
        visible += f" и ещё {len(labels) - 8}"
    if source == "marketplace" and not marketplace:
        prefix = "Синхронизация маркетплейса"
    return f"{prefix}: изменены {visible}."
