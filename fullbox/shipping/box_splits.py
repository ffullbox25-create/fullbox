from __future__ import annotations

import base64
import json
import re
from typing import Any


PARTIAL_BOX_SPLIT_MARKER = "split_box:b64:"
_PARTIAL_BOX_SPLIT_RE = re.compile(r"split_box:b64:([A-Za-z0-9_-]+={0,2})")


def _b64_padding(value: str) -> str:
    return "=" * ((4 - len(value) % 4) % 4)


def encode_partial_box_split(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    token = base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")
    return f"{PARTIAL_BOX_SPLIT_MARKER}{token}"


def extract_partial_box_split(comment: str | None) -> dict[str, Any]:
    match = _PARTIAL_BOX_SPLIT_RE.search(str(comment or ""))
    if not match:
        return {}
    token = match.group(1)
    try:
        raw = base64.urlsafe_b64decode((token + _b64_padding(token)).encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def has_partial_box_split(comment: str | None) -> bool:
    return bool(extract_partial_box_split(comment))
