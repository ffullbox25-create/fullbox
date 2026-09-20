from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings


MARKETPLACE_WAREHOUSE_KEYS = ("wb", "ozon", "yandex", "sber")
MARKETPLACE_KEY_ALIASES = {
    "wb": "wb",
    "wildberries": "wb",
    "wildberries (wb)": "wb",
    "ozon": "ozon",
    "yandex": "yandex",
    "yandex market": "yandex",
    "yandex.market": "yandex",
    "яндекс": "yandex",
    "яндекс маркет": "yandex",
    "сбер": "sber",
    "сбермегамаркет": "sber",
    "sber": "sber",
    "sbermegamarket": "sber",
}


def marketplace_warehouses_path() -> Path:
    return settings.BASE_DIR.parent / "marketplace_warehouses.json"


def normalize_marketplace_key(value) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return MARKETPLACE_KEY_ALIASES.get(text, "")


def marketplace_key_for_market(market) -> str:
    if market is None:
        return ""
    name = getattr(market, "name", market)
    return normalize_marketplace_key(name)


def load_marketplace_warehouses() -> dict[str, list[str]]:
    data = {key: [] for key in MARKETPLACE_WAREHOUSE_KEYS}
    path = marketplace_warehouses_path()
    if not path.exists():
        return data
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return data
    if not isinstance(payload, dict):
        return data

    for key in MARKETPLACE_WAREHOUSE_KEYS:
        values = payload.get(key) or []
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            continue
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            if isinstance(value, dict):
                warehouse_type = str(value.get("type") or "").strip()
                name = str(value.get("name") or "").strip()
                address = str(value.get("address") or "").strip()
                left = " · ".join(part for part in (warehouse_type, name) if part)
                text = f"{left} — {address}" if left and address else (left or address)
            else:
                text = str(value or "").strip()
            if not text:
                continue
            dedupe_key = text.lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            cleaned.append(text)
        data[key] = cleaned
    return data


def load_marketplace_warehouse_catalog() -> dict[str, list[dict[str, str]]]:
    data = {key: [] for key in MARKETPLACE_WAREHOUSE_KEYS}
    path = marketplace_warehouses_path()
    if not path.exists():
        return data
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return data
    if not isinstance(payload, dict):
        return data

    for key in MARKETPLACE_WAREHOUSE_KEYS:
        values = payload.get(key) or []
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            continue
        cleaned: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for value in values:
            if isinstance(value, dict):
                row = {
                    "type": str(value.get("type") or "").strip(),
                    "name": str(value.get("name") or "").strip(),
                    "address": str(value.get("address") or "").strip(),
                }
            else:
                text = str(value or "").strip()
                if not text:
                    continue
                name = text
                address = ""
                for separator in (" — ", " - ", " – "):
                    if separator in text:
                        left, right = text.split(separator, 1)
                        if left.strip() and right.strip():
                            name = left.strip()
                            address = right.strip()
                            break
                row = {"type": "", "name": name, "address": address}
            if not row["name"] and not row["address"]:
                continue
            dedupe_key = (row["type"].lower(), row["name"].lower(), row["address"].lower())
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            cleaned.append(row)
        data[key] = cleaned
    return data
