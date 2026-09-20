"""Broadcast chat events via Django Channels (Redis)."""

from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


def thread_group(thread_id: int) -> str:
    return f"chat.thread.{int(thread_id)}"


def agency_group(agency_id: int, *, client_side: bool) -> str:
    side = "client" if client_side else "staff"
    return f"chat.agency.{int(agency_id)}.{side}"


def broadcast_thread_event(thread_id: int, event_type: str, payload: dict[str, Any]) -> None:
    if not thread_id:
        return
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(
            thread_group(thread_id),
            {"type": "chat.event", "event": event_type, "payload": payload},
        )
    except Exception as exc:
        logger.warning("chat broadcast skipped thread=%s: %s", thread_id, exc)


def broadcast_agency_event(agency_id: int, *, client_side: bool, event_type: str, payload: dict[str, Any]) -> None:
    if not agency_id:
        return
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(
            agency_group(agency_id, client_side=client_side),
            {"type": "chat.event", "event": event_type, "payload": payload},
        )
    except Exception as exc:
        logger.warning("chat agency broadcast skipped agency=%s: %s", agency_id, exc)
