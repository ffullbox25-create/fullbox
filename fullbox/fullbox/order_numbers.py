from __future__ import annotations

import re
from collections.abc import Iterable


_ORDER_TYPE_SUFFIXES = {
    "receiving": "PR",
    "processing": "OBR",
    "shipping": "OTG",
    "logistics_trip": "LOG",
    "other": "OTH",
}

_PUBLIC_ORDER_PREFIXES = {
    "receiving": "PR",
    "processing": "OBR",
    "packing": "OBR",
    "shipping": "OTG",
    "other": "OTH",
}

_LEGACY_PREFIXES_BY_TYPE = {
    "shipping": ("SO", "OTG"),
    "receiving": ("PR",),
    "processing": ("OBR",),
    "packing": ("OBR",),
    "other": ("OTH",),
}

_PUBLIC_ORDER_STARTS = {
    "receiving": 122,
    "processing": 501,
    "packing": 501,
    "shipping": 91,
    "other": 15,
}

_PREFIXED_NUMBER_RE = re.compile(r"^(?P<prefix>[A-Z]{2,4})[-_]?0*(?P<number>\d+)$", re.IGNORECASE)
_LEGACY_SHIPPING_RE = re.compile(r"^0*(?P<number>\d+)_OTG$", re.IGNORECASE)


def normalize_order_type_for_number(order_type: str | None) -> str:
    normalized = str(order_type or "").strip().lower()
    if normalized == "packing":
        return "processing"
    return normalized


def public_order_prefix(order_type: str | None) -> str:
    normalized_type = str(order_type or "").strip().lower()
    return _PUBLIC_ORDER_PREFIXES.get(normalized_type) or _PUBLIC_ORDER_PREFIXES.get(
        normalize_order_type_for_number(normalized_type),
        "",
    )


def order_number_sequence(order_type: str | None, order_id: str | None) -> int | None:
    raw = str(order_id or "").strip()
    if not raw or raw.lower().startswith("draft-"):
        return None

    if raw.isdigit():
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    normalized_type = normalize_order_type_for_number(order_type)
    if normalized_type == "shipping":
        legacy_match = _LEGACY_SHIPPING_RE.fullmatch(raw)
        if legacy_match:
            try:
                return int(legacy_match.group("number"))
            except (TypeError, ValueError):
                return None

    match = _PREFIXED_NUMBER_RE.fullmatch(raw)
    if not match:
        return None

    prefix = match.group("prefix").upper()
    allowed_prefixes = _LEGACY_PREFIXES_BY_TYPE.get(normalized_type, ())
    if allowed_prefixes and prefix not in allowed_prefixes:
        return None
    try:
        return int(match.group("number"))
    except (TypeError, ValueError):
        return None


def build_public_order_number(order_type: str | None, sequence: int) -> str:
    prefix = public_order_prefix(order_type)
    try:
        number = int(sequence)
    except (TypeError, ValueError):
        number = 0
    if not prefix:
        return str(max(number, 0))
    return f"{prefix}-{max(number, 0):06d}"


def next_public_order_number(order_type: str | None, existing_order_ids: Iterable[str | None]) -> str:
    seen = {str(order_id or "").strip() for order_id in existing_order_ids if str(order_id or "").strip()}
    normalized_type = normalize_order_type_for_number(order_type)
    start_number = _PUBLIC_ORDER_STARTS.get(str(order_type or "").strip().lower())
    if start_number is None:
        start_number = _PUBLIC_ORDER_STARTS.get(normalized_type, 1)
    max_number = max(int(start_number or 1) - 1, 0)
    for order_id in seen:
        number = order_number_sequence(order_type, order_id)
        if number is not None and number > max_number:
            max_number = number

    next_number = max_number + 1
    candidate = build_public_order_number(order_type, next_number)
    while candidate in seen:
        next_number += 1
        candidate = build_public_order_number(order_type, next_number)
    return candidate


def format_order_number(order_type: str | None, order_id: str | None) -> str:
    raw = str(order_id or "").strip()
    if not raw:
        return "-"

    normalized_type = str(order_type or "").strip().lower()
    suffix = _ORDER_TYPE_SUFFIXES.get(normalized_type)
    if raw.lower().startswith("draft-"):
        draft_key = raw[6:].strip() or raw
        return f"{suffix} · черновик {draft_key}" if suffix else f"черновик {draft_key}"

    if suffix and raw.upper().endswith(f"_{suffix}"):
        number_raw = raw[: -(len(suffix) + 1)]
        number = str(int(number_raw)) if number_raw.isdigit() else number_raw
        return f"{number}_{suffix}"

    number = order_number_sequence(normalized_type, raw)
    if suffix and number is not None:
        return f"{number}_{suffix}"
    if suffix and raw.isdigit():
        return f"{raw}_{suffix}"
    return raw


def format_container_order_number(order_type: str | None, order_id: str | None) -> str:
    raw = str(order_id or "").strip()
    normalized_type = str(order_type or "").strip().lower()
    if normalized_type == "shipping" and raw:
        number = order_number_sequence("shipping", raw)
        if number is not None:
            return f"{number}OTG"
    display = format_order_number(order_type, order_id)
    text = str(display or order_id or "").strip().upper()
    cleaned = "".join(ch for ch in text if ch.isalnum())
    return cleaned or "ORD"


def replace_order_number_in_title(
    title: str | None,
    order_type: str | None,
    order_id: str | None,
    *,
    default_title: str | None = None,
) -> str:
    base_title = str(title or "").strip()
    display_id = format_order_number(order_type, order_id)
    if not base_title:
        return default_title or f"Заявка №{display_id}"

    raw = str(order_id or "").strip()
    if raw:
        if f"№{raw}" in base_title:
            return base_title.replace(f"№{raw}", f"№{display_id}")
        if raw in base_title:
            return base_title.replace(raw, display_id)
    return base_title
