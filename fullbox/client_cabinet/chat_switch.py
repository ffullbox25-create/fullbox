"""Runtime switch for the LK chats module."""

from __future__ import annotations

from django.conf import settings


def chats_enabled() -> bool:
    return bool(getattr(settings, "FULLBOX_CHATS_ENABLED", True))
