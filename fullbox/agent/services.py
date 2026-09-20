import json
import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.utils import timezone

from employees.access import get_request_role

from .auth import device_token_matches
from .models import AgentCommand, AgentContext, AgentEvent, DeviceAgent
from .network import legacy_agent_request_source_allowed
from .realtime import realtime_agent_allowed


ALLOWED_AGENT_ROLES = {
    "storekeeper",
    "processing_head",
    "processing_worker",
    "head_manager",
    "director",
    "admin",
    "manager",
}
_CLIENT_EVENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,63}\Z")


def _parse_json_body(request):
    try:
        body = request.body.decode("utf-8")
    except (AttributeError, UnicodeDecodeError):
        return None
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _get_agent_id(data):
    if not isinstance(data, dict):
        return ""
    value = data.get("agent_id")
    if value is None:
        value = data.get("agentId")
    return str(value or "").strip()


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _normalize_printer_names(printers) -> list[str]:
    if isinstance(printers, str):
        printers = [printers]
    if not isinstance(printers, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in printers:
        text = str(item or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _merge_agent_meta(existing_meta, incoming_meta, *, now) -> dict:
    existing = dict(existing_meta or {}) if isinstance(existing_meta, dict) else {}
    incoming = dict(incoming_meta or {}) if isinstance(incoming_meta, dict) else {}
    merged = dict(existing)

    for key, value in incoming.items():
        if key in {"printers", "printer_details", "printer_details_updated_at", "last_known_printers", "last_known_printers_at"}:
            continue
        merged[key] = value

    existing_printers = _normalize_printer_names(existing.get("printers"))
    incoming_printers = _normalize_printer_names(incoming.get("printers"))
    last_known_printers = _normalize_printer_names(existing.get("last_known_printers"))

    if incoming_printers:
        merged["printers"] = incoming_printers
        merged["last_known_printers"] = incoming_printers
        merged["last_known_printers_at"] = now.isoformat()
        merged.pop("printers_last_empty_at", None)
    elif "printers" in incoming:
        fallback_printers = existing_printers or last_known_printers
        merged["printers"] = fallback_printers
        if fallback_printers:
            merged["last_known_printers"] = fallback_printers
            merged["printers_last_empty_at"] = now.isoformat()
    elif existing_printers:
        merged["printers"] = existing_printers

    if isinstance(incoming.get("printer_details"), list):
        merged["printer_details"] = incoming.get("printer_details") or []
    elif "printer_details" in existing:
        merged["printer_details"] = existing.get("printer_details")

    if incoming.get("printer_details_updated_at"):
        merged["printer_details_updated_at"] = incoming.get("printer_details_updated_at")
    elif "printer_details_updated_at" in existing:
        merged["printer_details_updated_at"] = existing.get("printer_details_updated_at")

    return merged


def _extract_token(request):
    header = request.headers.get("X-Agent-Token") or request.META.get("HTTP_X_AGENT_TOKEN")
    if header:
        return header.strip()
    auth = request.headers.get("Authorization") or request.META.get("HTTP_AUTHORIZATION")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def agent_allowed(request) -> bool:
    provided = _extract_token(request)
    device_agent_id = str(
        request.headers.get("X-Agent-ID") or request.META.get("HTTP_X_AGENT_ID") or ""
    ).strip()
    if device_agent_id and provided and device_token_matches(device_agent_id, provided):
        request.fullbox_authenticated_agent_id = device_agent_id
        return True

    if not legacy_agent_request_source_allowed(request):
        return False

    shared = getattr(settings, "AGENT_SHARED_TOKEN", "") or ""
    print_token = getattr(settings, "PRINT_AGENT_TOKEN", "") or ""
    if not shared:
        if print_token:
            if not provided:
                return False
            return secrets.compare_digest(print_token, provided)
        return bool(getattr(settings, "DEBUG", False))
    if not provided:
        return False
    if secrets.compare_digest(shared, provided):
        return True
    if print_token and secrets.compare_digest(print_token, provided):
        return True
    return False


def _authenticated_device_agent_id(request) -> str:
    return str(getattr(request, "fullbox_authenticated_agent_id", "") or "").strip()


def _device_identity_matches(request, claimed_agent_id: str) -> bool:
    authenticated_agent_id = _authenticated_device_agent_id(request)
    if not authenticated_agent_id:
        return True
    return bool(claimed_agent_id) and secrets.compare_digest(authenticated_agent_id, claimed_agent_id)


def agent_forbidden():
    return HttpResponseForbidden("Доступ запрещен")


def _touch_agent(agent_id: str, request):
    if not agent_id:
        return
    now = timezone.now()
    updated = DeviceAgent.objects.filter(agent_id=agent_id).update(
        last_seen=now,
        last_ip=_client_ip(request),
    )
    if not updated:
        DeviceAgent.objects.create(
            agent_id=agent_id,
            name="",
            host="",
            version="",
            last_seen=now,
            last_ip=_client_ip(request),
            meta={},
        )


def _context_ttl_seconds() -> int:
    try:
        ttl = int(getattr(settings, "AGENT_CONTEXT_TTL", 30))
    except (TypeError, ValueError):
        ttl = 30
    return max(5, min(ttl, 300))


def _command_delivery_lease_seconds() -> int:
    try:
        ttl = int(getattr(settings, "AGENT_COMMAND_DELIVERY_LEASE_SECONDS", 60))
    except (TypeError, ValueError):
        ttl = 60
    return max(10, min(ttl, 3600))


def _agent_online_state(agent_id: str) -> tuple[bool, str, dict]:
    if not agent_id:
        return False, "", {}
    agent = DeviceAgent.objects.filter(agent_id=agent_id).first()
    if not agent or not agent.last_seen:
        return False, "", {}
    online_threshold = timezone.now() - timedelta(seconds=30)
    return agent.last_seen >= online_threshold, agent.last_seen.isoformat(), agent.meta or {}


def _extract_scanner_state(meta: dict) -> dict:
    if not isinstance(meta, dict):
        return {"ready": None, "reason": "", "port": "", "error": ""}
    com_health = meta.get("com_health")
    if isinstance(com_health, dict):
        return {
            "ready": com_health.get("ready") if isinstance(com_health.get("ready"), bool) else None,
            "reason": str(com_health.get("reason") or ""),
            "port": str(com_health.get("port") or ""),
            "error": str(com_health.get("error") or ""),
        }
    com_status = meta.get("com_status")
    if isinstance(com_status, dict):
        connected = com_status.get("connected") if isinstance(com_status.get("connected"), bool) else None
        error = str(com_status.get("error") or "")
        return {
            "ready": connected if isinstance(connected, bool) else None,
            "reason": "connected" if connected else "not_connected",
            "port": "",
            "error": error,
        }
    return {"ready": None, "reason": "", "port": "", "error": ""}


def _get_active_context(agent_id: str):
    if not agent_id:
        return None
    now = timezone.now()
    return (
        AgentContext.objects.filter(agent_id=agent_id, active=True, expires_at__gt=now)
        .order_by("-last_seen", "-updated_at")
        .first()
    )


def _context_owner_payload(ctx: AgentContext) -> dict:
    if not ctx:
        return {}
    user_label = ""
    if ctx.user_id:
        user = ctx.user
        if user:
            user_label = user.get_full_name() or user.username or ""
    return {
        "user_id": ctx.user_id,
        "user": user_label,
        "role": ctx.role or "",
        "order_id": ctx.order_id,
        "box_id": ctx.box_id or "",
        "updated_at": ctx.updated_at.isoformat() if ctx.updated_at else "",
    }


def _role_allowed(request) -> str | None:
    role = get_request_role(request)
    if role not in ALLOWED_AGENT_ROLES:
        return None
    return role


def agent_status_response(request):
    role = _role_allowed(request)
    if not role:
        return HttpResponseForbidden("Доступ запрещен")
    agent_id = str(request.GET.get("agent_id") or "").strip()
    if not agent_id:
        return JsonResponse({"ok": False, "error": "missing_agent_id"}, status=400)
    agent_online, agent_last_seen, agent_meta = _agent_online_state(agent_id)
    scanner_state = _extract_scanner_state(agent_meta)
    realtime_allowed = realtime_agent_allowed(agent_id)
    return JsonResponse(
        {
            "ok": True,
            "agent_online": agent_online,
            "agent_last_seen": agent_last_seen,
            "scanner_ready": scanner_state.get("ready"),
            "scanner_reason": scanner_state.get("reason"),
            "scanner_port": scanner_state.get("port"),
            "scanner_error": scanner_state.get("error"),
        }
    )


def agent_context_claim_response(request):
    role = _role_allowed(request)
    if not role:
        return HttpResponseForbidden("Доступ запрещен")
    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    agent_id = _get_agent_id(data or {})
    if not agent_id:
        return JsonResponse({"ok": False, "error": "missing_agent_id"}, status=400)
    context_id = str((data or {}).get("context_id") or "").strip()
    force = bool((data or {}).get("force"))
    try:
        order_id = int((data or {}).get("order_id") or 0)
    except (TypeError, ValueError):
        order_id = 0
    box_id = str((data or {}).get("box_id") or "").strip()
    now = timezone.now()
    ttl_seconds = _context_ttl_seconds()
    expires_at = now + timedelta(seconds=ttl_seconds)
    agent_online, agent_last_seen, agent_meta = _agent_online_state(agent_id)
    scanner_state = _extract_scanner_state(agent_meta)
    realtime_allowed = realtime_agent_allowed(agent_id)

    def _last_scan_event_id(context_value: str) -> int:
        if not context_value:
            return 0
        last_event = (
            AgentEvent.objects.filter(
                context_id=context_value,
                event_type=AgentEvent.EVENT_SCAN,
            )
            .order_by("-id")
            .values_list("id", flat=True)
            .first()
        )
        return int(last_event or 0)

    AgentContext.objects.filter(agent_id=agent_id, active=True, expires_at__lte=now).update(active=False)
    if context_id:
        try:
            ctx = AgentContext.objects.get(context_id=context_id)
        except AgentContext.DoesNotExist:
            ctx = None
        if not ctx:
            return JsonResponse({"ok": False, "error": "context_not_found"}, status=404)
        if ctx.agent_id != agent_id:
            return JsonResponse({"ok": False, "error": "agent_mismatch"}, status=403)
        if ctx.user_id and ctx.user_id != request.user.id:
            return JsonResponse({"ok": False, "error": "context_forbidden"}, status=403)
        ctx.user = request.user
        ctx.role = role or ctx.role
        ctx.order_id = order_id or ctx.order_id
        ctx.box_id = box_id
        ctx.session_key = request.session.session_key or ""
        ctx.last_seen = now
        ctx.expires_at = expires_at
        ctx.active = True
        ctx.save(
            update_fields=[
                "user",
                "role",
                "order_id",
                "box_id",
                "session_key",
                "last_seen",
                "expires_at",
                "active",
                "updated_at",
            ]
        )
        return JsonResponse(
            {
                "ok": True,
                "context_id": ctx.context_id,
                "expires_at": ctx.expires_at.isoformat(),
                "agent_online": agent_online,
                "agent_last_seen": agent_last_seen,
                "scanner_ready": scanner_state.get("ready"),
                "scanner_reason": scanner_state.get("reason"),
                "scanner_port": scanner_state.get("port"),
                "scanner_error": scanner_state.get("error"),
                "last_scan_event_id": _last_scan_event_id(ctx.context_id),
                "realtime_enabled": realtime_allowed,
            }
        )
    existing = (
        AgentContext.objects.filter(agent_id=agent_id, active=True, expires_at__gt=now)
        .order_by("-last_seen", "-updated_at")
        .first()
    )
    if existing and existing.user_id == request.user.id:
        existing.user = request.user
        existing.role = role or existing.role
        existing.order_id = order_id or existing.order_id
        existing.box_id = box_id
        existing.session_key = request.session.session_key or ""
        existing.last_seen = now
        existing.expires_at = expires_at
        existing.save(
            update_fields=[
                "user",
                "role",
                "order_id",
                "box_id",
                "session_key",
                "last_seen",
                "expires_at",
                "updated_at",
            ]
        )
        return JsonResponse(
            {
                "ok": True,
                "context_id": existing.context_id,
                "expires_at": existing.expires_at.isoformat(),
                "agent_online": agent_online,
                "agent_last_seen": agent_last_seen,
                "scanner_ready": scanner_state.get("ready"),
                "scanner_reason": scanner_state.get("reason"),
                "scanner_port": scanner_state.get("port"),
                "scanner_error": scanner_state.get("error"),
                "last_scan_event_id": _last_scan_event_id(existing.context_id),
                "realtime_enabled": realtime_allowed,
            }
        )
    if existing and not force:
        return JsonResponse(
            {
                "ok": False,
                "error": "busy",
                "context_id": existing.context_id,
                "owner": _context_owner_payload(existing),
                "agent_online": agent_online,
                "agent_last_seen": agent_last_seen,
                "scanner_ready": scanner_state.get("ready"),
                "scanner_reason": scanner_state.get("reason"),
                "scanner_port": scanner_state.get("port"),
                "scanner_error": scanner_state.get("error"),
                "realtime_enabled": realtime_allowed,
            },
            status=409,
        )
    if existing and force:
        existing.active = False
        existing.save(update_fields=["active", "updated_at"])
    context_id = secrets.token_urlsafe(16)
    ctx = AgentContext.objects.create(
        agent_id=agent_id,
        context_id=context_id,
        user=request.user,
        role=role or "",
        order_id=order_id or None,
        box_id=box_id,
        session_key=request.session.session_key or "",
        last_seen=now,
        expires_at=expires_at,
        active=True,
    )
    return JsonResponse(
        {
            "ok": True,
            "context_id": ctx.context_id,
            "expires_at": ctx.expires_at.isoformat(),
            "agent_online": agent_online,
            "agent_last_seen": agent_last_seen,
            "scanner_ready": scanner_state.get("ready"),
            "scanner_reason": scanner_state.get("reason"),
            "scanner_port": scanner_state.get("port"),
            "scanner_error": scanner_state.get("error"),
            "last_scan_event_id": 0,
            "realtime_enabled": realtime_allowed,
        }
    )


def agent_context_release_response(request):
    role = _role_allowed(request)
    if not role:
        return HttpResponseForbidden("Доступ запрещен")
    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    context_id = str((data or {}).get("context_id") or "").strip()
    if not context_id:
        return JsonResponse({"ok": False, "error": "missing_context_id"}, status=400)
    try:
        ctx = AgentContext.objects.get(context_id=context_id)
    except AgentContext.DoesNotExist:
        return JsonResponse({"ok": False, "error": "context_not_found"}, status=404)
    if ctx.user_id and ctx.user_id != request.user.id and role not in {"admin", "director"}:
        return HttpResponseForbidden("Доступ запрещен")
    ctx.active = False
    ctx.expires_at = timezone.now()
    ctx.save(update_fields=["active", "expires_at", "updated_at"])
    return JsonResponse({"ok": True})


def agent_ping_response(request):
    if not agent_allowed(request):
        return agent_forbidden()
    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    agent_id = _get_agent_id(data or {}) or _authenticated_device_agent_id(request)
    if not agent_id:
        return JsonResponse({"ok": False, "error": "missing_agent_id"}, status=400)
    if not _device_identity_matches(request, agent_id):
        return JsonResponse({"ok": False, "error": "agent_mismatch"}, status=403)
    now = timezone.now()
    incoming_meta = (data or {}).get("meta") if isinstance((data or {}).get("meta"), dict) else {}
    agent = DeviceAgent.objects.filter(agent_id=agent_id).first()
    if agent is None:
        DeviceAgent.objects.create(
            agent_id=agent_id,
            name=str((data or {}).get("name") or "").strip(),
            host=str((data or {}).get("host") or "").strip(),
            version=str((data or {}).get("version") or "").strip(),
            last_seen=now,
            last_ip=_client_ip(request),
            meta=_merge_agent_meta({}, incoming_meta, now=now),
        )
    else:
        incoming_name = str((data or {}).get("name") or "").strip()
        incoming_host = str((data or {}).get("host") or "").strip()
        incoming_version = str((data or {}).get("version") or "").strip()
        agent.name = incoming_name or agent.name
        agent.host = incoming_host or agent.host
        agent.version = incoming_version or agent.version
        agent.last_seen = now
        agent.last_ip = _client_ip(request)
        agent.meta = _merge_agent_meta(agent.meta, incoming_meta, now=now)
        agent.save(update_fields=["name", "host", "version", "last_seen", "last_ip", "meta", "updated_at"])
    return JsonResponse({"ok": True})


def agent_commands_response(request):
    if not agent_allowed(request):
        return agent_forbidden()
    agent_id = (request.GET.get("agent_id") or "").strip() or _authenticated_device_agent_id(request)
    if not agent_id:
        return JsonResponse({"ok": False, "error": "missing_agent_id"}, status=400)
    if not _device_identity_matches(request, agent_id):
        return JsonResponse({"ok": False, "error": "agent_mismatch"}, status=403)
    _touch_agent(agent_id, request)
    try:
        limit = int(request.GET.get("limit") or 5)
    except ValueError:
        limit = 5
    limit = max(1, min(limit, 20))
    now = timezone.now()
    delivery_cutoff = now - timedelta(seconds=_command_delivery_lease_seconds())
    AgentCommand.objects.filter(
        status=AgentCommand.STATUS_DELIVERED,
        delivered_at__lte=delivery_cutoff,
        agent_id__in=[agent_id, ""],
    ).update(
        status=AgentCommand.STATUS_PENDING,
        delivered_at=None,
        updated_at=now,
    )
    qs = AgentCommand.objects.filter(status=AgentCommand.STATUS_PENDING).filter(agent_id__in=[agent_id, ""])
    commands = list(qs.order_by("created_at")[:limit])
    if commands:
        AgentCommand.objects.filter(id__in=[cmd.id for cmd in commands]).update(
            status=AgentCommand.STATUS_DELIVERED,
            delivered_at=now,
        )
    payload = [
        {
            "id": cmd.id,
            "command": cmd.command,
            "payload": cmd.payload,
            "created_at": cmd.created_at.isoformat(),
        }
        for cmd in commands
    ]
    return JsonResponse({"ok": True, "commands": payload})


def agent_command_ack_response(request, command_id: int):
    if not agent_allowed(request):
        return agent_forbidden()
    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    try:
        cmd = AgentCommand.objects.get(id=command_id)
    except AgentCommand.DoesNotExist:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    agent_id = _get_agent_id(data or {}) or _authenticated_device_agent_id(request)
    if not _device_identity_matches(request, agent_id):
        return JsonResponse({"ok": False, "error": "agent_mismatch"}, status=403)
    if agent_id:
        _touch_agent(agent_id, request)
    if agent_id and cmd.agent_id and cmd.agent_id != agent_id:
        return JsonResponse({"ok": False, "error": "agent_mismatch"}, status=403)
    if cmd.status in {AgentCommand.STATUS_DONE, AgentCommand.STATUS_FAILED}:
        return JsonResponse({"ok": True, "already_acked": True})
    ok = bool((data or {}).get("ok"))
    cmd.status = AgentCommand.STATUS_DONE if ok else AgentCommand.STATUS_FAILED
    cmd.acked_at = timezone.now()
    cmd.result = (data or {}).get("result") if isinstance((data or {}).get("result"), dict) else {}
    cmd.error = str((data or {}).get("error") or "").strip()
    cmd.save(update_fields=["status", "acked_at", "result", "error", "updated_at"])
    return JsonResponse({"ok": True})


def record_agent_event(
    agent_id: str,
    event_type: str,
    payload: dict,
    *,
    client_event_id: str = "",
) -> AgentEvent:
    ctx = _get_active_context(agent_id)
    context_fields = {}
    if ctx:
        now = timezone.now()
        ctx.last_seen = now
        ctx.expires_at = now + timedelta(seconds=_context_ttl_seconds())
        ctx.save(update_fields=["last_seen", "expires_at", "updated_at"])
        context_fields = {
            "context_id": ctx.context_id,
            "context_user": ctx.user,
            "context_role": ctx.role or "",
            "context_order_id": ctx.order_id,
            "context_box_id": ctx.box_id or "",
        }
    return AgentEvent.objects.create(
        agent_id=agent_id,
        client_event_id=client_event_id,
        event_type=event_type,
        payload=payload,
        **context_fields,
    )


def agent_event_response(request):
    if not agent_allowed(request):
        return agent_forbidden()
    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    agent_id = _get_agent_id(data or {}) or _authenticated_device_agent_id(request)
    if not agent_id:
        return JsonResponse({"ok": False, "error": "missing_agent_id"}, status=400)
    if not _device_identity_matches(request, agent_id):
        return JsonResponse({"ok": False, "error": "agent_mismatch"}, status=403)
    _touch_agent(agent_id, request)
    event_type = str(
        (data or {}).get("event_type")
        or (data or {}).get("eventType")
        or (data or {}).get("type")
        or ""
    ).strip().lower()
    if event_type not in {
        AgentEvent.EVENT_SCAN,
        AgentEvent.EVENT_PRINT,
        AgentEvent.EVENT_STATUS,
        AgentEvent.EVENT_ERROR,
    }:
        return JsonResponse({"ok": False, "error": "invalid_event_type"}, status=400)
    payload = (data or {}).get("payload")
    if not isinstance(payload, dict):
        payload = {}
    raw_client_event_id = (data or {}).get("client_event_id")
    if raw_client_event_id is None:
        raw_client_event_id = (data or {}).get("clientEventId")
    if raw_client_event_id is None:
        raw_client_event_id = payload.get("client_event_id")
    if raw_client_event_id is None:
        raw_client_event_id = payload.get("clientEventId")
    client_event_id = ""
    if raw_client_event_id is not None and raw_client_event_id != "":
        client_event_id = str(raw_client_event_id).strip()
        if not _CLIENT_EVENT_ID_RE.fullmatch(client_event_id):
            return JsonResponse({"ok": False, "error": "invalid_client_event_id"}, status=400)

    if client_event_id:
        existing = AgentEvent.objects.filter(
            agent_id=agent_id,
            client_event_id=client_event_id,
        ).first()
        if existing is not None:
            return JsonResponse(
                {"ok": True, "event_id": existing.id, "duplicate": True}
            )
    try:
        with transaction.atomic():
            event = record_agent_event(
                agent_id,
                event_type,
                payload,
                client_event_id=client_event_id,
            )
    except IntegrityError:
        if not client_event_id:
            raise
        existing = AgentEvent.objects.filter(
            agent_id=agent_id,
            client_event_id=client_event_id,
        ).first()
        if existing is None:
            raise
        return JsonResponse(
            {"ok": True, "event_id": existing.id, "duplicate": True}
        )
    return JsonResponse({"ok": True, "event_id": event.id, "duplicate": False})


def agent_events_poll_response(request):
    role = _role_allowed(request)
    if not role:
        return HttpResponseForbidden("Доступ запрещен")
    try:
        since_id = int(request.GET.get("since") or 0)
    except (TypeError, ValueError):
        since_id = 0
    event_type = str(request.GET.get("type") or AgentEvent.EVENT_SCAN).strip().lower()
    agent_id = str(request.GET.get("agent_id") or "").strip()
    context_id = str(request.GET.get("context_id") or "").strip()
    qs = AgentEvent.objects.filter(id__gt=since_id)
    if event_type:
        qs = qs.filter(event_type=event_type)
    if context_id:
        try:
            ctx = AgentContext.objects.get(context_id=context_id)
        except AgentContext.DoesNotExist:
            return JsonResponse({"ok": True, "events": []})
        if ctx.user_id and ctx.user_id != request.user.id and role not in {"admin", "director"}:
            return HttpResponseForbidden("Доступ запрещен")
        if ctx.expires_at and ctx.expires_at <= timezone.now():
            return JsonResponse({"ok": True, "events": []})
        qs = qs.filter(context_id=context_id)
    elif agent_id:
        qs = qs.filter(agent_id=agent_id)
    events = list(qs.order_by("id")[:50])
    payload = [
        {
            "id": ev.id,
            "agent_id": ev.agent_id,
            "context_id": ev.context_id,
            "event_type": ev.event_type,
            "payload": ev.payload,
            "created_at": ev.created_at.isoformat(),
        }
        for ev in events
    ]
    return JsonResponse({"ok": True, "events": payload})
