from __future__ import annotations

import ipaddress
from datetime import timedelta

from django.contrib.auth import login, logout
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST, require_http_methods

from .access import get_employee_for_user, resolve_cabinet_url
from .badges import BADGE_PREFIX, WAREHOUSE_QR_ROLES, find_active_badge
from .models import EmployeeBadgeEvent


GENERIC_BADGE_ERROR = "Бейдж не подошёл"
FULLBOX_DESKTOP_USER_AGENT_MARKER = "FullboxDesktop/"
PFBS_TSD_ENTRY_PATH = "/tsd/"
PFBS_TSD_PICKING_PATH = "/fbs/tsd/picking/"
PFBS_TSD_ROLE = "picker"
RECEIVING_TSD_ENTRY_PATH = "/tsd/receiving/"
RECEIVING_TSD_QUEUE_PATH = "/orders/receiving/tsd/"
RECEIVING_TSD_ROLE = "storekeeper"
MAX_ATTEMPTS_PER_MINUTE = 10
LOGIN_EVENT_TYPES = (
    EmployeeBadgeEvent.EVENT_LOGIN_SUCCESS,
    EmployeeBadgeEvent.EVENT_LOGIN_FAILURE,
    EmployeeBadgeEvent.EVENT_RATE_LIMITED,
)


def _client_ip(request) -> str:
    raw_ip = str(
        request.META.get("HTTP_X_REAL_IP")
        or request.META.get("REMOTE_ADDR")
        or ""
    ).strip()
    try:
        return str(ipaddress.ip_address(raw_ip))
    except ValueError:
        return ""


def _is_fullbox_desktop(request) -> bool:
    user_agent = str(request.META.get("HTTP_USER_AGENT") or "")
    return FULLBOX_DESKTOP_USER_AGENT_MARKER in user_agent


def _is_pfbs_tsd_entry(request) -> bool:
    return str(getattr(request, "path_info", "") or "") == PFBS_TSD_ENTRY_PATH


def _is_receiving_tsd_entry(request) -> bool:
    return str(getattr(request, "path_info", "") or "") == RECEIVING_TSD_ENTRY_PATH


def _login_context(request, *, qr_error: str = "") -> dict:
    receiving_tsd_login_mode = _is_receiving_tsd_entry(request)
    tsd_login_mode = _is_pfbs_tsd_entry(request) or receiving_tsd_login_mode
    return {
        "qr_error": qr_error,
        "qr_login_available": qr_login_available(request),
        "qr_login_action": (
            RECEIVING_TSD_ENTRY_PATH
            if receiving_tsd_login_mode
            else PFBS_TSD_ENTRY_PATH if tsd_login_mode else "/login/qr/"
        ),
        "tsd_login_mode": tsd_login_mode,
        "receiving_tsd_login_mode": receiving_tsd_login_mode,
    }


def _render_login(request, *, qr_error: str = "", status: int = 200):
    response = render(
        request,
        "login.html",
        _login_context(request, qr_error=qr_error),
        status=status,
    )
    response["Cache-Control"] = "no-store, private"
    return response


def qr_login_available(request) -> bool:
    """Показывать QR в Fullbox Desktop и на коротких входах складских TSD."""
    return (
        _is_fullbox_desktop(request)
        or _is_pfbs_tsd_entry(request)
        or _is_receiving_tsd_entry(request)
    )


def _record_event(*, event_type: str, ip_address: str, employee=None, details=None) -> None:
    EmployeeBadgeEvent.objects.create(
        event_type=event_type,
        employee=employee,
        ip_address=ip_address or None,
        details=details or {},
    )


def _badge_login_response(
    request,
    *,
    tsd_login_mode: bool,
    tsd_required_role: str = PFBS_TSD_ROLE,
    tsd_success_path: str = PFBS_TSD_PICKING_PATH,
):
    if request.user.is_authenticated:
        employee = get_employee_for_user(request.user)
        if tsd_login_mode and employee and employee.role == tsd_required_role:
            return redirect(tsd_success_path)
        return redirect(resolve_cabinet_url(employee.role) if employee else "/")

    ip_address = _client_ip(request)
    recent_attempts = EmployeeBadgeEvent.objects.filter(
        event_type__in=LOGIN_EVENT_TYPES,
        ip_address=ip_address or None,
        created_at__gte=timezone.now() - timedelta(minutes=1),
    ).count()
    if recent_attempts >= MAX_ATTEMPTS_PER_MINUTE:
        _record_event(
            event_type=EmployeeBadgeEvent.EVENT_RATE_LIMITED,
            ip_address=ip_address,
        )
        return _render_login(request, qr_error=GENERIC_BADGE_ERROR, status=401)

    if not tsd_login_mode and not _is_fullbox_desktop(request):
        _record_event(
            event_type=EmployeeBadgeEvent.EVENT_LOGIN_FAILURE,
            ip_address=ip_address,
            details={"reason": "desktop_required"},
        )
        return _render_login(request, qr_error=GENERIC_BADGE_ERROR, status=401)

    raw_token = request.POST.get("badge_token") or ""
    badge = find_active_badge(raw_token)
    employee = badge.employee if badge else None
    user = employee.user if employee else None

    # Причину отказа записываем точную: раньше все шесть проверок сливались
    # в одну и в журнал шло "badge", из-за чего «галочка выключена» и
    # «код не подошёл» выглядели одинаково и не различались при разборе.
    # Оператору по-прежнему показывается один и тот же текст — подсказывать,
    # что именно не сошлось, нельзя.
    reason = None
    if badge is None:
        reason = "not_found"
    elif not employee.qr_login_enabled:
        reason = "disabled"
    elif not employee.is_active:
        reason = "employee_inactive"
    elif tsd_login_mode and employee.role != tsd_required_role:
        reason = (
            "tsd_storekeeper_required"
            if tsd_required_role == RECEIVING_TSD_ROLE
            else "tsd_picker_required"
        )
    elif employee.role not in WAREHOUSE_QR_ROLES:
        reason = "role_not_allowed"
    elif user is None:
        reason = "no_user"
    elif not user.is_active:
        reason = "user_inactive"

    if reason is not None:
        details = {"reason": reason}
        if reason == "not_found":
            # Сам код не пишем, но форму записываем: по ней сразу видно,
            # подменил ли сканер регистр, раскладку или обрезал строку.
            probe = str(raw_token).strip()
            details["token_len"] = len(probe)
            details["token_prefix_ok"] = probe.startswith(BADGE_PREFIX)
            details["token_is_ascii"] = probe.isascii()
            details["token_has_lower"] = any(char.islower() for char in probe)
        _record_event(
            event_type=EmployeeBadgeEvent.EVENT_LOGIN_FAILURE,
            ip_address=ip_address,
            employee=employee,
            details=details,
        )
        return _render_login(request, qr_error=GENERIC_BADGE_ERROR, status=401)

    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session["employee_id"] = employee.id
    request.session["employee_name"] = employee.full_name
    request.session["employee_role"] = employee.role
    now = timezone.now()
    badge.last_used_at = now
    badge.last_used_ip = ip_address
    badge.save(update_fields=("last_used_at", "last_used_ip"))
    _record_event(
        event_type=EmployeeBadgeEvent.EVENT_LOGIN_SUCCESS,
        ip_address=ip_address,
        employee=employee,
    )

    if tsd_login_mode:
        response = redirect(tsd_success_path)
    else:
        next_url = str(request.POST.get("next") or "").strip()
        if not url_has_allowed_host_and_scheme(
            url=next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            next_url = ""
        response = redirect(next_url or resolve_cabinet_url(employee.role))
    response["Cache-Control"] = "no-store, private"
    return response


@require_POST
def employee_qr_login(request):
    return _badge_login_response(request, tsd_login_mode=False)


@require_http_methods(["GET", "POST"])
def tsd_qr_login(request):
    """Короткий браузерный вход подборщика PFBS: lk.fullbox.ru/tsd/."""
    if request.method == "POST":
        return _badge_login_response(request, tsd_login_mode=True)
    if request.user.is_authenticated:
        employee = get_employee_for_user(request.user)
        if employee and employee.role == PFBS_TSD_ROLE:
            return redirect(PFBS_TSD_PICKING_PATH)
        return redirect(resolve_cabinet_url(employee.role) if employee else "/")
    return _render_login(request)


@require_http_methods(["GET", "POST"])
def receiving_tsd_qr_login(request):
    """Личный вход кладовщика сразу в упрощённую приёмку на TSD."""
    if request.method == "POST":
        return _badge_login_response(
            request,
            tsd_login_mode=True,
            tsd_required_role=RECEIVING_TSD_ROLE,
            tsd_success_path=RECEIVING_TSD_QUEUE_PATH,
        )
    if request.GET.get("switch") == "1" and request.user.is_authenticated:
        logout(request)
        request.session.flush()
    if request.user.is_authenticated:
        employee = get_employee_for_user(request.user)
        if employee and employee.role == RECEIVING_TSD_ROLE:
            return redirect(RECEIVING_TSD_QUEUE_PATH)
        # На общем TSD не открываем чужой кабинет: следующий сотрудник должен
        # авторизоваться своим личным бейджем именно в контуре приёмки.
        logout(request)
        request.session.flush()
    return _render_login(request)
