"""Client-facing request progress for LK home (display-only)."""

from __future__ import annotations

from typing import Any


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def request_progress(
    *,
    order_type: str,
    bucket: str = "",
    status: str = "",
    status_label: str = "",
    is_draft: bool = False,
) -> dict[str, Any]:
    """Map real request state to percent + stage label for home active list."""
    otype = _norm(order_type)
    if otype == "packing":
        otype = "processing"
    bucket_key = _norm(bucket)
    filter_status = _norm(status)
    label = _norm(status_label)

    if is_draft or "черновик" in label or bucket_key == "client" and "черновик" in label:
        return {"percent": 10, "stage_label": "Черновик"}
    if filter_status in {"cancelled", "canceled"} or ("отмен" in label and "согласован" not in label):
        return {"percent": 0, "stage_label": "Отменена"}
    if filter_status == "completed" or bucket_key == "done" or _has(label, "выполн", "заверш", "закрыт"):
        return {"percent": 100, "stage_label": "Выполнена"}
    if "отмена на согласовании" in label or filter_status == "cancel_requested":
        return {"percent": 50, "stage_label": "Отмена на согласовании"}
    if _has(label, "уточн") or filter_status == "clarification":
        return {"percent": 35, "stage_label": "Требует уточнения"}
    if _has(label, "ожидает подписи", "акт отправлен"):
        return {"percent": 85, "stage_label": "Ожидает подтверждения"}
    if _has(label, "ждет подтверждения", "ждёт подтверждения", "на согласовании"):
        return {"percent": 20, "stage_label": "На согласовании"}

    if otype == "receiving":
        return _receiving_progress(bucket_key, label)
    if otype == "processing":
        return _processing_progress(bucket_key, label)
    if otype == "shipping":
        return _shipping_progress(bucket_key, label, filter_status)
    return _generic_progress(bucket_key, label)


def _has(label: str, *tokens: str) -> bool:
    return any(token in label for token in tokens)


def _receiving_progress(bucket: str, label: str) -> dict[str, Any]:
    if bucket == "warehouse" or _has(label, "в работе", "ожидании поставки", "принята", "склад"):
        if _has(label, "размещен", "размещён"):
            return {"percent": 90, "stage_label": "Размещена"}
        return {"percent": 65, "stage_label": "В работе"}
    if bucket == "manager":
        return {"percent": 20, "stage_label": "На согласовании"}
    return {"percent": 40, "stage_label": "В работе"}


def _processing_progress(bucket: str, label: str) -> dict[str, Any]:
    if _has(label, "готов", "заверш"):
        return {"percent": 90, "stage_label": "Готова"}
    if bucket == "warehouse" or _has(label, "в работе", "обработ", "сборк", "упаков", "маркир"):
        if _has(label, "новая", "ожида"):
            return {"percent": 25, "stage_label": "Новая"}
        return {"percent": 40, "stage_label": "В обработке"}
    if bucket == "manager":
        return {"percent": 20, "stage_label": "На согласовании"}
    return {"percent": 30, "stage_label": "В обработке"}


def _shipping_progress(bucket: str, label: str, status: str) -> dict[str, Any]:
    if status in {"shipped", "partial_shipped"} or _has(label, "отгружен", "рейс завершен", "загружено"):
        return {"percent": 95, "stage_label": "Отгружается"}
    if _has(label, "готов", "ожидает логист", "подготовлена"):
        return {"percent": 80, "stage_label": "Готова к отгрузке"}
    if _has(label, "комплект", "отбор", "ричтрак", "сборк", "упаков"):
        return {"percent": 55, "stage_label": "Комплектация"}
    if bucket == "warehouse" or _has(label, "в работе", "принята", "склад"):
        return {"percent": 50, "stage_label": "В работе"}
    if bucket == "manager" or status in {"submitted"}:
        return {"percent": 20, "stage_label": "На согласовании"}
    return {"percent": 30, "stage_label": "В работе"}


def _generic_progress(bucket: str, label: str) -> dict[str, Any]:
    if bucket == "warehouse" or _has(label, "в работе"):
        return {"percent": 50, "stage_label": "В работе"}
    if bucket == "manager":
        return {"percent": 20, "stage_label": "На согласовании"}
    if bucket == "client":
        return {"percent": 10, "stage_label": "У клиента"}
    return {"percent": 30, "stage_label": "В работе"}


def enrich_request_row(row: dict[str, Any]) -> dict[str, Any]:
    """Attach progress fields to an existing request list row."""
    progress = request_progress(
        order_type=row.get("order_type") or row.get("type") or "",
        bucket=row.get("bucket") or row.get("stage") or "",
        status=row.get("status") or "",
        status_label=row.get("status_label") or row.get("subtitle") or "",
        is_draft=bool(row.get("is_draft")),
    )
    out = dict(row)
    out["progress_percent"] = progress["percent"]
    out["progress_stage"] = progress["stage_label"]
    return out
