import json
import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from employees.access import get_request_role, request_has_any_role

from .auth import generate_device_token, generate_enrollment_code, secret_digest
from .desktop_auth import authenticate_desktop_request, desktop_auth_error_response
from .models import AgentCommand, AgentContext, AgentEnrollmentCode, AgentEvent, DeviceAgent
from .services import (
    agent_allowed as agent_allowed_service,
    agent_command_ack_response,
    agent_commands_response,
    agent_context_claim_response,
    agent_context_release_response,
    agent_event_response,
    agent_events_poll_response,
    agent_forbidden as agent_forbidden_service,
    agent_ping_response,
    agent_status_response,
)


AGENT_ENROLLMENT_ROLES = {
    "admin",
    "director",
    "head_manager",
    "processing_head",
    "storekeeper",
}
_AGENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")


def _enrollment_ttl_seconds() -> int:
    try:
        value = int(getattr(settings, "AGENT_ENROLLMENT_TTL_SECONDS", 600))
    except (TypeError, ValueError):
        value = 600
    return max(60, min(value, 3600))


def _private_json(payload: dict, *, status: int = 200) -> JsonResponse:
    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response


@login_required
@require_POST
def agent_enrollment_create(request):
    if not request.user.is_superuser and not request_has_any_role(request, AGENT_ENROLLMENT_ROLES):
        return HttpResponseForbidden("Доступ запрещен")

    now = timezone.now()
    ttl_seconds = _enrollment_ttl_seconds()
    expires_at = now + timedelta(seconds=ttl_seconds)
    enrollment_code = generate_enrollment_code()
    AgentEnrollmentCode.objects.create(
        code_digest=secret_digest(enrollment_code),
        created_by=request.user,
        expires_at=expires_at,
    )
    return _private_json(
        {
            "ok": True,
            "enrollment_code": enrollment_code,
            "expires_at": expires_at.isoformat(),
            "ttl_seconds": ttl_seconds,
        }
    )


@csrf_exempt
@require_POST
def agent_enrollment_exchange(request):
    data = _parse_json_body(request)
    if data is None:
        return _private_json({"ok": False, "error": "invalid_json"}, status=400)

    enrollment_code = str((data or {}).get("enrollment_code") or "").strip()
    agent_id = _get_agent_id(data or {})
    if not enrollment_code or len(enrollment_code) > 128:
        return _private_json({"ok": False, "error": "missing_enrollment_code"}, status=400)
    if not _AGENT_ID_RE.fullmatch(agent_id):
        return _private_json({"ok": False, "error": "invalid_agent_id"}, status=400)

    name = str((data or {}).get("name") or "").strip()[:128]
    host = str((data or {}).get("host") or "").strip()[:128]
    version = str((data or {}).get("version") or "").strip()[:32]
    now = timezone.now()
    code_digest = secret_digest(enrollment_code)

    with transaction.atomic():
        enrollment = (
            AgentEnrollmentCode.objects.select_for_update()
            .filter(code_digest=code_digest, used_at__isnull=True, expires_at__gt=now)
            .first()
        )
        if enrollment is None:
            return _private_json({"ok": False, "error": "invalid_or_expired_code"}, status=403)
        claimed = AgentEnrollmentCode.objects.filter(
            pk=enrollment.pk,
            used_at__isnull=True,
            expires_at__gt=now,
        ).update(used_at=now)
        if claimed != 1:
            return _private_json({"ok": False, "error": "invalid_or_expired_code"}, status=403)

        plaintext_token = generate_device_token()
        agent, _created = DeviceAgent.objects.get_or_create(agent_id=agent_id, defaults={"meta": {}})
        agent.name = name or agent.name
        agent.host = host or agent.host
        agent.version = version or agent.version
        agent.token_digest = secret_digest(plaintext_token)
        agent.token_created_at = now
        agent.token_revoked_at = None
        agent.save(
            update_fields=[
                "name",
                "host",
                "version",
                "token_digest",
                "token_created_at",
                "token_revoked_at",
                "updated_at",
            ]
        )
        enrollment.used_by_agent = agent
        enrollment.save(update_fields=["used_by_agent"])

    return _private_json(
        {
            "ok": True,
            "agent_id": agent.agent_id,
            "device_token": plaintext_token,
            "token_type": "device",
        }
    )


@require_GET
def desktop_credential_verify(request):
    """Validate an individual Desktop credential without touching print queues."""
    workstation_id = str(request.GET.get("workstation_id") or "").strip()
    if not workstation_id or len(workstation_id) > 80 or not re.fullmatch(r"[A-Za-z0-9._-]+", workstation_id):
        return _private_json({"ok": False, "error": "invalid_workstation_id"}, status=400)
    authentication = authenticate_desktop_request(
        request,
        workstation_id,
        allow_legacy=False,
    )
    if not authentication.ok:
        return desktop_auth_error_response(authentication)
    return _private_json(
        {
            "ok": True,
            "workstation_id": workstation_id,
            "desktop_auth": authentication.mode,
        }
    )


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


def _extract_token(request):
    header = request.headers.get("X-Agent-Token") or request.META.get("HTTP_X_AGENT_TOKEN")
    if header:
        return header.strip()
    auth = request.headers.get("Authorization") or request.META.get("HTTP_AUTHORIZATION")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _agent_allowed(request) -> bool:
    return agent_allowed_service(request)


def _agent_forbidden():
    return agent_forbidden_service()


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


@login_required
@require_GET
def agent_status(request):
    return agent_status_response(request)


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


@login_required
@require_POST
def agent_context_claim(request):
    return agent_context_claim_response(request)


@login_required
@require_POST
def agent_context_release(request):
    return agent_context_release_response(request)


@csrf_exempt
@require_POST
def agent_ping(request):
    return agent_ping_response(request)


@csrf_exempt
@require_GET
def agent_commands(request):
    return agent_commands_response(request)


@csrf_exempt
@require_POST
def agent_command_ack(request, command_id: int):
    return agent_command_ack_response(request, command_id)


@csrf_exempt
@require_POST
def agent_event(request):
    return agent_event_response(request)


@login_required
@require_GET
def agent_events_poll(request):
    return agent_events_poll_response(request)
