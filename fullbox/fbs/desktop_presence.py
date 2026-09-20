"""Short-lived registry of Fullbox Desktop workstations polling print jobs."""

from __future__ import annotations

import re

from django.core.cache import cache
from django.utils import timezone


DESKTOP_PRESENCE_SECONDS = 45
_CACHE_TIMEOUT_SECONDS = 60
_CACHE_PREFIX = "fbs:desktop-print:presence:"
_DESKTOP_VERSION_RE = re.compile(r"(?:^|\s)FullboxDesktop/([^\s]+)")
_MAX_PRINTERS = 100
_MAX_PRINTER_NAME_LENGTH = 255


def _normalized_workstation_id(value: str) -> str:
    workstation_id = str(value or "").strip()
    if not workstation_id or len(workstation_id) > 80:
        return ""
    return workstation_id if re.fullmatch(r"[A-Za-z0-9._-]+", workstation_id) else ""


def desktop_presence_cache_key(workstation_id: str) -> str:
    normalized = _normalized_workstation_id(workstation_id)
    return f"{_CACHE_PREFIX}{normalized}" if normalized else ""


def _normalized_printer_names(values) -> list[str]:
    if not isinstance(values, list):
        return []
    names = []
    seen = set()
    for value in values[:_MAX_PRINTERS]:
        name = str(value or "").strip()[:_MAX_PRINTER_NAME_LENGTH]
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _normalized_printer_details(values, printer_names: list[str]) -> list[dict]:
    if not isinstance(values, list):
        return []
    allowed = {name.casefold() for name in printer_names}
    details = []
    seen = set()
    for value in values[:_MAX_PRINTERS]:
        if not isinstance(value, dict):
            continue
        name = str(value.get("name") or "").strip()[:_MAX_PRINTER_NAME_LENGTH]
        key = name.casefold()
        if not name or key not in allowed or key in seen:
            continue
        seen.add(key)
        try:
            jobs = int(value.get("jobs") or 0)
        except (TypeError, ValueError):
            jobs = 0
        details.append(
            {
                "name": name,
                "is_default": bool(value.get("is_default")),
                "is_paused": bool(value.get("is_paused")),
                "is_offline": bool(value.get("is_offline")),
                "is_busy": bool(value.get("is_busy")),
                "jobs": max(min(jobs, 10000), 0),
                "status": str(value.get("status") or "").strip()[:120],
            }
        )
    return details


def record_desktop_presence(request, workstation_id: str, data=None) -> dict | None:
    """Mark an authenticated Desktop poll as online for the TSD printer list."""

    normalized = _normalized_workstation_id(workstation_id)
    key = desktop_presence_cache_key(normalized)
    if not key:
        return None
    user_agent = str(request.headers.get("User-Agent") or "").strip()
    version_match = _DESKTOP_VERSION_RE.search(user_agent)
    previous = cache.get(key)
    payload = dict(previous) if isinstance(previous, dict) else {}
    now_iso = timezone.now().isoformat()
    payload.update(
        {
            "workstation_id": normalized,
            "last_seen": now_iso,
            "version": version_match.group(1) if version_match else str(payload.get("version") or ""),
        }
    )
    source = data if isinstance(data, dict) else {}
    if isinstance(source.get("printers"), list):
        printers = _normalized_printer_names(source.get("printers"))
        preferred_printer = str(source.get("preferred_printer") or "").strip()[:_MAX_PRINTER_NAME_LENGTH]
        if preferred_printer not in printers:
            preferred_printer = ""
        payload.update(
            {
                "printers": printers,
                "printer_details": _normalized_printer_details(
                    source.get("printer_details"), printers
                ),
                "preferred_printer": preferred_printer,
                "printers_last_seen": now_iso,
            }
        )
    cache.set(key, payload, timeout=_CACHE_TIMEOUT_SECONDS)
    return payload


def get_desktop_presences(workstation_ids) -> dict[str, dict]:
    """Return non-expired presence rows keyed by the exact polling identifier."""

    ids = {
        normalized
        for value in workstation_ids
        if (normalized := _normalized_workstation_id(value))
    }
    key_to_id = {
        desktop_presence_cache_key(workstation_id): workstation_id
        for workstation_id in ids
    }
    values = cache.get_many(key_to_id)
    result = {}
    for key, payload in values.items():
        workstation_id = key_to_id.get(key, "")
        if workstation_id and isinstance(payload, dict):
            result[workstation_id] = payload
    return result
