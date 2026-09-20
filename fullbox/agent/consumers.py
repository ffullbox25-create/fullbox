from __future__ import annotations

import secrets
from datetime import timedelta
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
from django.utils import timezone

from employees.models import Employee

from .auth import device_token_matches
from .models import AgentCommand, AgentContext, AgentEvent, DeviceAgent
from .network import legacy_agent_scope_source_allowed
from .realtime import AGENT_BROADCAST_GROUP, agent_group, context_group, realtime_agent_allowed
from .services import _merge_agent_meta, record_agent_event


def _scope_header(headers, name: bytes) -> str:
    name = name.lower()
    for key, value in headers:
        if key.lower() == name:
            try:
                return value.decode("utf-8").strip()
            except UnicodeDecodeError:
                return ""
    return ""


def _agent_token_allowed(token: str, agent_id: str = "", scope=None) -> bool:
    if agent_id and token and device_token_matches(agent_id, token):
        return True
    if not legacy_agent_scope_source_allowed(scope or {}):
        return False
    shared = getattr(settings, "AGENT_SHARED_TOKEN", "") or ""
    print_token = getattr(settings, "PRINT_AGENT_TOKEN", "") or ""
    if not shared:
        if print_token:
            return bool(token) and secrets.compare_digest(print_token, token)
        return bool(getattr(settings, "DEBUG", False))
    if not token:
        return False
    if secrets.compare_digest(shared, token):
        return True
    return bool(print_token) and secrets.compare_digest(print_token, token)


def _extract_agent_token(scope) -> str:
    headers = scope.get("headers") or []
    token = _scope_header(headers, b"x-agent-token")
    if token:
        return token
    auth = _scope_header(headers, b"authorization")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    query = parse_qs((scope.get("query_string") or b"").decode("utf-8", errors="ignore"))
    return str((query.get("token") or [""])[0]).strip()


def _extract_query_value(scope, key: str) -> str:
    query = parse_qs((scope.get("query_string") or b"").decode("utf-8", errors="ignore"))
    return str((query.get(key) or [""])[0]).strip()


class AgentRealtimeConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        headers = self.scope.get("headers") or []
        self.agent_id = _scope_header(headers, b"x-agent-id") or _extract_query_value(self.scope, "agent_id")
        token = _extract_agent_token(self.scope)
        token_allowed = False
        if self.agent_id and token:
            token_allowed = await database_sync_to_async(_agent_token_allowed)(token, self.agent_id, self.scope)
        if not self.agent_id or not token_allowed or not realtime_agent_allowed(self.agent_id):
            await self.close(code=4403)
            return

        await self.channel_layer.group_add(agent_group(self.agent_id), self.channel_name)
        await self.channel_layer.group_add(AGENT_BROADCAST_GROUP, self.channel_name)
        await self._touch_agent(self.agent_id)
        await self.accept()
        await self.send_json({"type": "realtime.ready", "agent_id": self.agent_id})

    async def disconnect(self, code):
        agent_id = getattr(self, "agent_id", "")
        if agent_id:
            await self.channel_layer.group_discard(agent_group(agent_id), self.channel_name)
        await self.channel_layer.group_discard(AGENT_BROADCAST_GROUP, self.channel_name)

    async def receive_json(self, content, **kwargs):
        message_type = str((content or {}).get("type") or "").strip().lower()
        if message_type in {"agent.hello", "agent.ping"}:
            await self._upsert_agent(
                self.agent_id,
                name=str((content or {}).get("name") or "").strip(),
                host=str((content or {}).get("host") or "").strip(),
                version=str((content or {}).get("version") or "").strip(),
                meta=(content or {}).get("meta") if isinstance((content or {}).get("meta"), dict) else {},
            )
            await self.send_json({"type": "realtime.ready", "agent_id": self.agent_id})
            return

        if message_type in {"agent.event", "scan.event"}:
            event_type = str((content or {}).get("event_type") or (content or {}).get("eventType") or "").strip().lower()
            if message_type == "scan.event" and not event_type:
                event_type = AgentEvent.EVENT_SCAN
            payload = (content or {}).get("payload")
            if not isinstance(payload, dict):
                payload = {}
            if event_type not in {
                AgentEvent.EVENT_SCAN,
                AgentEvent.EVENT_PRINT,
                AgentEvent.EVENT_STATUS,
                AgentEvent.EVENT_ERROR,
            }:
                await self.send_json({"type": "agent.event_ack", "ok": False, "error": "invalid_event_type"})
                return
            event_id = await self._record_event(self.agent_id, event_type, payload)
            await self.send_json({"type": "agent.event_ack", "ok": True, "event_id": event_id})
            return

        if message_type == "agent.command_ack":
            command_id = int((content or {}).get("command_id") or (content or {}).get("commandId") or 0)
            if command_id:
                ok = bool((content or {}).get("ok"))
                result = (content or {}).get("result") if isinstance((content or {}).get("result"), dict) else {}
                error = str((content or {}).get("error") or "").strip()
                ack_ok = await self._ack_command(self.agent_id, command_id, ok, result, error)
                await self.send_json({"type": "agent.command_ack_result", "ok": ack_ok, "command_id": command_id})

    async def agent_command_available(self, event):
        await self.send_json(
            {
                "type": "agent.command_available",
                "command_id": event.get("command_id"),
                "command": event.get("command"),
                "agent_id": event.get("agent_id") or "",
            }
        )

    async def print_job_available(self, event):
        await self.send_json(
            {
                "type": "print.job_available",
                "job_id": event.get("job_id"),
                "agent_id": event.get("agent_id") or "",
                "printer_name": event.get("printer_name") or "",
                "copies_count": event.get("copies_count") or 1,
            }
        )

    @database_sync_to_async
    def _touch_agent(self, agent_id: str) -> None:
        now = timezone.now()
        updated = DeviceAgent.objects.filter(agent_id=agent_id).update(last_seen=now)
        if not updated:
            DeviceAgent.objects.create(agent_id=agent_id, last_seen=now, meta={})

    @database_sync_to_async
    def _upsert_agent(self, agent_id: str, *, name: str, host: str, version: str, meta: dict) -> None:
        now = timezone.now()
        agent, _ = DeviceAgent.objects.get_or_create(agent_id=agent_id, defaults={"meta": {}})
        if name:
            agent.name = name
        if host:
            agent.host = host
        if version:
            agent.version = version
        agent.last_seen = now
        if meta:
            current_meta = agent.meta if isinstance(agent.meta, dict) else {}
            agent.meta = _merge_agent_meta(current_meta, meta, now=now)
        agent.save(update_fields=["name", "host", "version", "last_seen", "meta", "updated_at"])

    @database_sync_to_async
    def _record_event(self, agent_id: str, event_type: str, payload: dict) -> int:
        event = record_agent_event(agent_id, event_type, payload)
        return int(event.id)

    @database_sync_to_async
    def _ack_command(self, agent_id: str, command_id: int, ok: bool, result: dict, error: str) -> bool:
        command = AgentCommand.objects.filter(pk=command_id).first()
        if not command:
            return False
        if command.agent_id and command.agent_id != agent_id:
            return False
        command.status = AgentCommand.STATUS_DONE if ok else AgentCommand.STATUS_FAILED
        command.acked_at = timezone.now()
        command.result = result
        command.error = error
        command.save(update_fields=["status", "acked_at", "result", "error", "updated_at"])
        return True


class BrowserAgentContextConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.context_id = str(self.scope.get("url_route", {}).get("kwargs", {}).get("context_id") or "").strip()
        user = self.scope.get("user")
        if not self.context_id or not user or not user.is_authenticated:
            await self.close(code=4401)
            return
        if not await self._context_allowed(self.context_id, user.id):
            await self.close(code=4403)
            return
        await self.channel_layer.group_add(context_group(self.context_id), self.channel_name)
        await self.accept()
        await self.send_json({"type": "realtime.ready", "context_id": self.context_id})

    async def disconnect(self, code):
        context_id = getattr(self, "context_id", "")
        if context_id:
            await self.channel_layer.group_discard(context_group(context_id), self.channel_name)

    async def receive_json(self, content, **kwargs):
        if str((content or {}).get("type") or "").strip().lower() == "ping":
            await self.send_json({"type": "pong"})

    async def agent_event_available(self, event):
        await self.send_json({"type": "agent.event", "event": event.get("event") or {}})

    @database_sync_to_async
    def _context_allowed(self, context_id: str, user_id: int) -> bool:
        ctx = AgentContext.objects.filter(context_id=context_id, active=True).first()
        if not ctx:
            return False
        if not realtime_agent_allowed(ctx.agent_id):
            return False
        if ctx.expires_at and ctx.expires_at <= timezone.now() - timedelta(seconds=5):
            return False
        if not ctx.user_id or ctx.user_id == user_id:
            return True
        role = Employee.objects.filter(user_id=user_id).values_list("role", flat=True).first()
        return role in {"admin", "director"}
