from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass

from django.http import JsonResponse
from django.utils import timezone

from .auth import device_token_matches
from .models import DeviceAgent


_DESKTOP_AGENT_ID_RE = re.compile(r"desktop-auth-[A-Za-z0-9._-]{8,51}\Z")
_FALSE_VALUES = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class DesktopAuthentication:
    ok: bool
    mode: str = ""
    agent_id: str = ""
    error: str = ""


def desktop_legacy_auth_enabled() -> bool:
    value = str(os.getenv("FULLBOX_DESKTOP_LEGACY_AUTH_ENABLED", "true") or "").strip().lower()
    return value not in _FALSE_VALUES


def _client_ip(request) -> str | None:
    forwarded = str(request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",", 1)[0].strip()
    candidate = forwarded or str(request.META.get("REMOTE_ADDR") or "").strip()
    try:
        return str(ipaddress.ip_address(candidate)) if candidate else None
    except ValueError:
        return None


def authenticate_desktop_request(
    request,
    workstation_id: str,
    *,
    allow_legacy: bool = True,
) -> DesktopAuthentication:
    """Authenticate Fullbox Desktop and bind its token to one workstation ID.

    The legacy marker remains available only while 1.0.23 workstations are being
    upgraded.  Supplying any individual credential disables legacy fallback, so
    an invalid or partially configured 1.0.24 client cannot silently downgrade.
    """

    if str(request.headers.get("X-Fullbox-Desktop") or "").strip() != "1":
        return DesktopAuthentication(False, error="desktop_required")

    agent_id = str(request.headers.get("X-Fullbox-Desktop-ID") or "").strip()
    token = str(request.headers.get("X-Fullbox-Desktop-Token") or "").strip()
    has_individual_credentials = bool(agent_id or token)
    if has_individual_credentials:
        if not agent_id or not token or not _DESKTOP_AGENT_ID_RE.fullmatch(agent_id):
            return DesktopAuthentication(False, error="invalid_desktop_credentials")
        if not device_token_matches(agent_id, token):
            return DesktopAuthentication(False, error="invalid_desktop_credentials")
        agent = DeviceAgent.objects.filter(agent_id=agent_id).only("id", "name").first()
        if agent is None or str(agent.name or "").strip() != str(workstation_id or "").strip():
            return DesktopAuthentication(False, error="desktop_workstation_mismatch")

        update_fields = {
            "last_seen": timezone.now(),
            "last_ip": _client_ip(request),
        }
        version = str(request.headers.get("X-Fullbox-Desktop-Version") or "").strip()[:32]
        if version:
            update_fields["version"] = version
        DeviceAgent.objects.filter(pk=agent.pk).update(**update_fields)
        request.fullbox_desktop_auth_mode = "device"
        request.fullbox_desktop_agent_id = agent_id
        return DesktopAuthentication(True, mode="device", agent_id=agent_id)

    if allow_legacy and desktop_legacy_auth_enabled():
        request.fullbox_desktop_auth_mode = "legacy"
        request.fullbox_desktop_agent_id = ""
        return DesktopAuthentication(True, mode="legacy")
    return DesktopAuthentication(False, error="desktop_activation_required")


def desktop_auth_error_response(auth: DesktopAuthentication) -> JsonResponse:
    messages = {
        "desktop_required": "Fullbox Desktop required",
        "invalid_desktop_credentials": "Привязка Fullbox Desktop недействительна.",
        "desktop_workstation_mismatch": "Fullbox Desktop привязан к другому рабочему месту.",
        "desktop_activation_required": "Требуется индивидуальная привязка Fullbox Desktop.",
    }
    response = JsonResponse(
        {
            "ok": False,
            "error": messages.get(auth.error, "Доступ Fullbox Desktop запрещён."),
            "error_code": auth.error or "desktop_forbidden",
        },
        status=403,
    )
    response["Cache-Control"] = "no-store"
    return response
