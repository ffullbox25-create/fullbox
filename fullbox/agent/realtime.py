from __future__ import annotations

import re
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.db import transaction


AGENT_BROADCAST_GROUP = "fullbox.agent.broadcast"
_GROUP_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def realtime_enabled() -> bool:
    return bool(getattr(settings, "FULLBOX_REALTIME_ENABLED", True))


def realtime_agent_allowed(agent_id: str = "") -> bool:
    if not realtime_enabled():
        return False
    raw_allowed_ids = getattr(settings, "FULLBOX_REALTIME_AGENT_IDS", set()) or set()
    if isinstance(raw_allowed_ids, str):
        allowed_ids = {value.strip() for value in raw_allowed_ids.split(",") if value.strip()}
    else:
        allowed_ids = {str(value).strip() for value in raw_allowed_ids if str(value).strip()}
    if not allowed_ids:
        return True
    value = str(agent_id or "").strip()
    if not value:
        return True
    return value in allowed_ids


def _safe_group_part(value: str, *, fallback: str) -> str:
    cleaned = _GROUP_SAFE_RE.sub("_", str(value or "").strip())[:72].strip("._-")
    return cleaned or fallback


def agent_group(agent_id: str) -> str:
    return f"fullbox.agent.{_safe_group_part(agent_id, fallback='unknown')}"


def context_group(context_id: str) -> str:
    return f"fullbox.context.{_safe_group_part(context_id, fallback='unknown')}"


def serialize_agent_event(event) -> dict[str, Any]:
    return {
        "id": event.id,
        "agent_id": event.agent_id,
        "context_id": event.context_id,
        "event_type": event.event_type,
        "payload": event.payload,
        "created_at": event.created_at.isoformat() if event.created_at else "",
    }


def _group_send(group: str, message: dict[str, Any]) -> None:
    if not realtime_enabled():
        return
    layer = get_channel_layer()
    if layer is None:
        return
    async_to_sync(layer.group_send)(group, message)


def notify_agent_command(command) -> None:
    if not realtime_agent_allowed(command.agent_id):
        return
    payload = {
        "type": "agent.command_available",
        "command_id": int(command.id),
        "command": command.command,
        "agent_id": command.agent_id or "",
    }
    target_group = agent_group(command.agent_id) if command.agent_id else AGENT_BROADCAST_GROUP
    _group_send(target_group, payload)


def notify_print_job_available(job) -> None:
    if not realtime_agent_allowed(job.agent):
        return
    payload = {
        "type": "print.job_available",
        "job_id": int(job.id),
        "agent_id": job.agent or "",
        "printer_name": job.printer_name or "",
        "copies_count": int(job.copies_count or 1),
    }
    target_group = agent_group(job.agent) if job.agent else AGENT_BROADCAST_GROUP
    _group_send(target_group, payload)


def notify_agent_event(event) -> None:
    if not event.context_id or not realtime_agent_allowed(event.agent_id):
        return
    _group_send(
        context_group(event.context_id),
        {
            "type": "agent.event_available",
            "event": serialize_agent_event(event),
        },
    )


def on_commit_notify_agent_command(command) -> None:
    transaction.on_commit(lambda: notify_agent_command(command))


def on_commit_notify_print_job_available(job) -> None:
    transaction.on_commit(lambda: notify_print_job_available(job))


def on_commit_notify_agent_event(event) -> None:
    transaction.on_commit(lambda: notify_agent_event(event))
