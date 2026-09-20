from __future__ import annotations

from types import SimpleNamespace

from django.utils import timezone

from fullbox.order_numbers import format_order_number

ORDER_TITLE_KEY = "order_title"
ORDER_DISPLAY_TITLE_KEY = "order_display_title"
_STATUS_ONLY_KEYS = {"comment", "message", "status", "status_label", "submit_action"}
_RECEIVING_RUNTIME_IGNORED_KEYS = _STATUS_ONLY_KEYS | {
    ORDER_TITLE_KEY,
    ORDER_DISPLAY_TITLE_KEY,
    "flow_state",
    "flow_boxes",
    "flow_pallets",
    "flow_active_box",
    "flow_active_pallet",
    "packing_assignee",
    "packing_assignee_id",
    "packing_assignee_role",
}


def _normalize_payload(payload: dict | None) -> dict:
    return dict(payload) if isinstance(payload, dict) else {}


def _sorted_entries(entries) -> list:
    return sorted(
        list(entries or []),
        key=lambda entry: (
            getattr(entry, "created_at", None) or timezone.localtime(),
            getattr(entry, "id", 0),
        ),
    )


def _parse_qty_value(raw: object | None) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _has_receiving_items(payload: dict) -> bool:
    items = payload.get("items") or []
    for item in items:
        for key in ("sku_code", "name", "qty", "size"):
            if str(item.get(key) or "").strip():
                return True
    return False


def _receiving_has_mismatch(payload: dict | None) -> bool:
    payload = payload or {}
    if payload.get("act_mismatch") is True:
        return True
    act_label = str(payload.get("act_label") or "").strip().lower()
    status_label = str(payload.get("status_label") or "").strip().lower()
    if "расхожд" in act_label or "расхожд" in status_label:
        return True
    act_items = payload.get("act_items") or []
    for item in act_items:
        planned_qty = _parse_qty_value(item.get("planned_qty"))
        if planned_qty is None:
            planned_qty = _parse_qty_value(item.get("qty")) or 0
        actual_qty = _parse_qty_value(item.get("actual_qty"))
        if actual_qty is None:
            actual_qty = _parse_qty_value(item.get("qty")) or 0
        if actual_qty != planned_qty:
            return True
    return False


def _is_receiving_fact_payload(payload: dict | None) -> bool:
    payload = payload or {}
    if not payload:
        return False
    if "act_items" in payload or "act_mismatch" in payload:
        return True
    act_value = str(payload.get("act") or "").strip().lower()
    if act_value == "receiving":
        return True
    act_label = str(payload.get("act_label") or "").strip().lower()
    status_label = str(payload.get("status_label") or "").strip().lower()
    return "акт приемки" in act_label or "товар принят" in status_label


def _latest_receiving_fact_payload(entries) -> dict:
    for entry in reversed(_sorted_entries(entries)):
        payload = _normalize_payload(getattr(entry, "payload", None))
        if _is_receiving_fact_payload(payload):
            return payload
    return {}


def _latest_significant_payload(entries) -> dict:
    for entry in reversed(_sorted_entries(entries)):
        payload = _normalize_payload(getattr(entry, "payload", None))
        if not payload:
            continue
        significant_keys = set(payload.keys()) - _STATUS_ONLY_KEYS
        if significant_keys:
            return payload
    return {}


def _planned_receiving_payload_from_entries(entries) -> dict:
    for entry in _sorted_entries(entries):
        payload = _normalize_payload(getattr(entry, "payload", None))
        if "items" in payload:
            return payload
    return _latest_significant_payload(entries)


def _receiving_runtime_payload_from_entries(entries) -> dict:
    merged = {}
    for entry in _sorted_entries(entries):
        payload = _normalize_payload(getattr(entry, "payload", None))
        if not payload:
            continue
        for key, value in payload.items():
            if key in _RECEIVING_RUNTIME_IGNORED_KEYS:
                continue
            merged[key] = value
    return merged


def _receiving_title_from_entries(entries) -> str:
    sorted_entries = _sorted_entries(entries)
    planned_payload = _planned_receiving_payload_from_entries(sorted_entries)
    if planned_payload and not _has_receiving_items(planned_payload):
        return "Заявка на приемку без указания товара"
    fact_payload = _latest_receiving_fact_payload(sorted_entries)
    if fact_payload and _receiving_has_mismatch(fact_payload):
        return "Заявка на приемку с расхождениями"
    return "Заявка на приемку"


def _first_created_at(entries, fallback=None):
    sorted_entries = _sorted_entries(entries)
    if sorted_entries:
        return getattr(sorted_entries[0], "created_at", None) or fallback
    return fallback


def _format_receiving_date(value) -> str:
    if not value:
        return ""
    dt_value = value
    if timezone.is_naive(dt_value):
        dt_value = timezone.make_aware(dt_value, timezone.get_current_timezone())
    return timezone.localtime(dt_value).strftime("%d.%m.%Y")


def _payload_title_data(payload: dict | None) -> dict:
    data = _normalize_payload(payload)
    base = str(data.get(ORDER_TITLE_KEY) or "").strip()
    display = str(data.get(ORDER_DISPLAY_TITLE_KEY) or "").strip()
    return {
        ORDER_TITLE_KEY: base,
        ORDER_DISPLAY_TITLE_KEY: display,
    }


def _latest_persisted_title_data(entries) -> dict:
    for entry in reversed(_sorted_entries(entries)):
        data = _payload_title_data(getattr(entry, "payload", None))
        if data[ORDER_TITLE_KEY] or data[ORDER_DISPLAY_TITLE_KEY]:
            return data
    return {
        ORDER_TITLE_KEY: "",
        ORDER_DISPLAY_TITLE_KEY: "",
    }


def derive_order_title(order_type: str, order_id: str, *, payload: dict | None = None, entries=None) -> str:
    normalized_type = str(order_type or "").strip().lower()
    data = _normalize_payload(payload)
    if normalized_type == "receiving":
        history = _sorted_entries(entries)
        if history:
            return _receiving_title_from_entries(history)
        if not data:
            return "Заявка на приемку"
        if not _has_receiving_items(data):
            return "Заявка на приемку без указания товара"
        if _receiving_has_mismatch(data):
            return "Заявка на приемку с расхождениями"
        return "Заявка на приемку"
    if normalized_type == "packing":
        return "Заявка на упаковку"
    if normalized_type == "processing":
        status_value = str(data.get("status") or data.get("submit_action") or "").strip().lower()
        status_label = str(data.get("status_label") or "").strip().lower()
        if status_value == "draft" or "черновик" in status_label or str(order_id or "").startswith("draft-"):
            return "Черновик заявки на обработку"
        return "Заявка на обработку"
    if normalized_type == "shipping":
        return "Заявка на отгрузку"
    if normalized_type == "stock_move":
        return "Складское задание"
    return "Заявка"


def build_order_title_data(
    order_type: str,
    order_id: str,
    *,
    payload: dict | None = None,
    entries=None,
    created_at=None,
    force_recompute: bool = False,
) -> dict:
    normalized_type = str(order_type or "").strip().lower()
    payload_data = _normalize_payload(payload)
    if normalized_type != "receiving" and not force_recompute:
        direct = _payload_title_data(payload_data)
        if direct[ORDER_TITLE_KEY] and direct[ORDER_DISPLAY_TITLE_KEY]:
            base = direct[ORDER_TITLE_KEY]
            display = direct[ORDER_DISPLAY_TITLE_KEY]
            created_at_value = _first_created_at(entries, fallback=created_at)
            full = display
            if normalized_type == "receiving":
                created_at_label = _format_receiving_date(created_at_value)
                if created_at_label:
                    full = f"{display} от {created_at_label}"
            return {
                ORDER_TITLE_KEY: base,
                ORDER_DISPLAY_TITLE_KEY: display,
                "order_display_title_full": full,
            }
        from_entries = _latest_persisted_title_data(entries)
        if from_entries[ORDER_TITLE_KEY] and from_entries[ORDER_DISPLAY_TITLE_KEY]:
            created_at_value = _first_created_at(entries, fallback=created_at)
            full = from_entries[ORDER_DISPLAY_TITLE_KEY]
            if normalized_type == "receiving":
                created_at_label = _format_receiving_date(created_at_value)
                if created_at_label:
                    full = f"{full} от {created_at_label}"
            return {
                ORDER_TITLE_KEY: from_entries[ORDER_TITLE_KEY],
                ORDER_DISPLAY_TITLE_KEY: from_entries[ORDER_DISPLAY_TITLE_KEY],
                "order_display_title_full": full,
            }
    base_title = derive_order_title(
        normalized_type,
        order_id,
        payload=payload_data,
        entries=entries,
    )
    raw_order_id = str(order_id or "").strip()
    try:
        from audit.models import get_order_external_number

        display_id = get_order_external_number(normalized_type, raw_order_id)
    except Exception:
        display_id = format_order_number(normalized_type, raw_order_id)
    display_title = f"{base_title} №{display_id}" if display_id else base_title
    created_at_value = _first_created_at(entries, fallback=created_at)
    full_title = display_title
    if normalized_type == "receiving":
        created_at_label = _format_receiving_date(created_at_value)
        if created_at_label:
            full_title = f"{display_title} от {created_at_label}"
    return {
        ORDER_TITLE_KEY: base_title,
        ORDER_DISPLAY_TITLE_KEY: display_title,
        "order_display_title_full": full_title,
    }


def enrich_order_payload_with_title(
    order_type: str,
    order_id: str,
    *,
    payload: dict | None = None,
    entries=None,
    created_at=None,
    force_recompute: bool = False,
) -> dict:
    data = _normalize_payload(payload)
    title_data = build_order_title_data(
        order_type,
        order_id,
        payload=data,
        entries=entries,
        created_at=created_at,
        force_recompute=force_recompute,
    )
    data[ORDER_TITLE_KEY] = title_data[ORDER_TITLE_KEY]
    data[ORDER_DISPLAY_TITLE_KEY] = title_data[ORDER_DISPLAY_TITLE_KEY]
    return data


def resolve_order_title(
    order_type: str,
    order_id: str,
    *,
    payload: dict | None = None,
    entries=None,
    created_at=None,
    variant: str = "display",
) -> str:
    title_data = build_order_title_data(
        order_type,
        order_id,
        payload=payload,
        entries=entries,
        created_at=created_at,
    )
    normalized_variant = str(variant or "display").strip().lower()
    if normalized_variant == "base":
        return title_data[ORDER_TITLE_KEY]
    if normalized_variant == "full":
        return title_data["order_display_title_full"]
    return title_data[ORDER_DISPLAY_TITLE_KEY]


def build_order_runtime_payload(
    order_type: str,
    *,
    payload: dict | None = None,
    entries=None,
) -> dict:
    normalized_type = str(order_type or "").strip().lower()
    if normalized_type == "receiving":
        merged = _receiving_runtime_payload_from_entries(entries)
        if merged:
            return merged
        fallback = {}
        for key, value in _normalize_payload(payload).items():
            if key in _RECEIVING_RUNTIME_IGNORED_KEYS:
                continue
            fallback[key] = value
        return fallback
    history_payload = _latest_significant_payload(entries)
    if history_payload:
        return history_payload
    return _normalize_payload(payload)


def build_entry_title_payload(order_type: str, order_id: str, payload: dict | None, history_entries, *, created_at=None) -> dict:
    current_created_at = created_at or timezone.now()
    current_entry = SimpleNamespace(payload=_normalize_payload(payload), created_at=current_created_at, id=0)
    entries = [*list(history_entries or []), current_entry]
    return enrich_order_payload_with_title(
        order_type,
        order_id,
        payload=payload,
        entries=entries,
        force_recompute=True,
    )
