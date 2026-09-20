from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from .base import decimal_qty


def payload_of(application) -> dict[str, Any]:
    payload = application.source_payload or {}
    if not isinstance(payload, dict):
        return {}
    nested = payload.get("payload")
    if isinstance(nested, dict):
        merged = dict(payload)
        merged.update(nested)
        return merged
    return payload


def first_number(data: dict[str, Any], keys: Iterable[str], *, default: str = "0") -> Decimal:
    for key in keys:
        value = data.get(key)
        if isinstance(value, (int, float, str)) and str(value).strip() != "":
            qty = decimal_qty(value, default=default)
            if qty:
                return qty
    return Decimal(default)


def list_count(data: dict[str, Any], keys: Iterable[str]) -> Decimal:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list) and value:
            return Decimal(len(value))
    return Decimal("0")


def list_sum(data: dict[str, Any], keys: Iterable[str], qty_keys: Iterable[str]) -> Decimal:
    for key in keys:
        value = data.get(key)
        if not isinstance(value, list) or not value:
            continue
        total = Decimal("0")
        for row in value:
            if not isinstance(row, dict):
                continue
            qty = first_number(row, qty_keys, default="0")
            total += qty
        if total:
            return total
    return Decimal("0")


def at_least_one(value: Decimal) -> Decimal:
    return value if value > 0 else Decimal("1")
