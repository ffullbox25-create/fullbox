from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, time as dt_time
from pathlib import Path

import requests
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.core.mail import send_mail
from django.http import (
    FileResponse,
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseNotFound,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.html import escape

from employees.access import get_employee_for_user, get_request_role, resolve_cabinet_url
from employees.models import Employee
from sku.models import Agency


DEV_USERS = [
    ("admin", "Администратор"),
    ("director", "Директор"),
    ("accountant", "Бухгалтер"),
    ("hr", "Отдел кадров"),
    ("head_manager", "Главный менеджер"),
    ("processing_head", "Руководитель участка обработки"),
    ("processing_worker", "Обработчик: Илья Соколов"),
    ("processing_worker2", "Обработчик: Алина Морозова"),
    ("manager", "Менеджер"),
    ("storekeeper", "Кладовщик"),
    ("logistician", "Логист"),
    ("reachtruck_driver", "Водитель ричтрака"),
    ("picker", "Сборщик"),
    ("developer", "Разработчик"),
]
ROLE_TITLES = dict(DEV_USERS)
_REMOTE_JOURNAL_CACHE = {"ts": 0.0, "data": None}
_JOURNAL_ENTRY_RE = re.compile(r"^-\s*(\d{4}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}))?:")
FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<rect width='64' height='64' rx='12' fill='#c79a1c'/>"
    "<path d='M20 46V18h24v8H30v4h12v8H30v8h-10z' fill='#1f2328'/>"
    "</svg>"
)


def _set_employee_session(request, employee: Employee) -> None:
    request.session["employee_id"] = employee.id
    request.session["employee_name"] = employee.full_name
    request.session["employee_role"] = employee.role


def _clear_employee_session(request) -> None:
    request.session.pop("employee_id", None)
    request.session.pop("employee_name", None)
    request.session.pop("employee_role", None)


def _resolve_client_portal_agency(user):
    if not user or not getattr(user, "is_authenticated", False):
        return None
    agency = Agency.objects.filter(portal_user=user).first()
    if agency:
        return agency
    from client_cabinet.portal_members import get_portal_membership

    membership = get_portal_membership(user)
    return membership.agency if membership else None


def login_menu_response(request):
    if not settings.DEBUG:
        return HttpResponseNotFound()
    usernames = [u[0] for u in DEV_USERS]
    existing = (
        get_user_model()
        .objects.filter(username__in=usernames)
        .values_list("username", flat=True)
    )
    items = [
        {"username": username, "label": label}
        for username, label in DEV_USERS
        if username in existing
    ]
    agencies = Agency.objects.order_by("agn_name")
    return render(request, "login_menu.html", {"users": items, "agencies": agencies})


def dev_login_response(request, username: str):
    if not settings.DEBUG:
        return HttpResponseNotFound()
    user_model = get_user_model()
    try:
        user = user_model.objects.get(username=username)
    except user_model.DoesNotExist:
        return HttpResponseRedirect("/login-menu/")

    backend = "django.contrib.auth.backends.ModelBackend"
    user.backend = backend
    login(request, user, backend=backend)
    next_url = request.GET.get("next")
    if next_url and not next_url.startswith("/"):
        next_url = None

    employee_role = None
    if username == "manager":
        employee_role = "manager"
    elif username == "hr":
        employee_role = "hr"
    elif username == "storekeeper":
        employee_role = "storekeeper"
    elif username == "logistician":
        employee_role = "logistician"
    elif username == "reachtruck_driver":
        employee_role = "reachtruck_driver"
    elif username in {"processing_worker", "processing_worker2"}:
        employee_role = "processing_worker"

    employee = None
    if employee_role == "processing_worker" and username in {"processing_worker", "processing_worker2"}:
        workers = list(
            Employee.objects.filter(role="processing_worker", is_active=True)
            .order_by("full_name")[:2]
        )
        if workers:
            if username == "processing_worker2" and len(workers) > 1:
                employee = workers[1]
            else:
                employee = workers[0]
    elif employee_role:
        employee = (
            Employee.objects.filter(role=employee_role, is_active=True)
            .order_by("full_name")
            .first()
        )

    if employee:
        _set_employee_session(request, employee)
    else:
        _clear_employee_session(request)

    if next_url:
        target = next_url
    elif employee_role:
        target = resolve_cabinet_url(employee_role)
    elif username == "developer":
        target = next_url or "/dev/"
    elif username == "admin":
        target = next_url or "/admin/"
    else:
        target = f"/cabinet/{username}/"
    return redirect(target)


def role_cabinet_response(request, role: str):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    current_role = get_request_role(request)
    # Portal clients must not land on the old role cabinet shell.
    if role == "client" or (current_role == "client"):
        agency = _resolve_client_portal_agency(request.user)
        if agency:
            return redirect(f"/client/dashboard/lk/?client={agency.id}")
        return redirect("/client/dashboard/lk/")
    if role == "manager":
        return redirect(resolve_cabinet_url(role))
    if current_role != role:
        return HttpResponseForbidden("Доступ запрещен")
    title = ROLE_TITLES.get(role, "Кабинет")
    if role == "director":
        from .director_dashboard import build_director_dashboard_context

        ctx = {
            "role": role,
            "title": title,
        }
        ctx.update(build_director_dashboard_context(request))
        return render(request, "director/dashboard.html", ctx)
    return render(request, "role_cabinet.html", {"role": role, "title": title})


def sign_in_response(request):
    if request.user.is_authenticated:
        employee = get_employee_for_user(request.user)
        if employee:
            _set_employee_session(request, employee)
            return redirect(resolve_cabinet_url(employee.role))
        agency = _resolve_client_portal_agency(request.user)
        if agency:
            _clear_employee_session(request)
            return redirect(f"/client/dashboard/lk/?client={agency.id}")
        logout(request)
        request.session.flush()
    error = None
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        password = request.POST.get("password") or ""
        user = authenticate(request, username=username, password=password)
        if not user:
            error = "Неверный логин или пароль"
        else:
            login(request, user)
            employee = get_employee_for_user(user)
            agency = _resolve_client_portal_agency(user)
            if employee:
                _set_employee_session(request, employee)
                return redirect(resolve_cabinet_url(employee.role))
            if agency:
                _clear_employee_session(request)
                return redirect(f"/client/dashboard/lk/?client={agency.id}")
            _clear_employee_session(request)
            return redirect("/")
    from employees.qr_login import qr_login_available

    return render(
        request,
        "login.html",
        {
            "error": error,
            # Клиентам блок бейджа не показываем: вне сети склада он всё равно
            # не работает, а страница входа у них должна остаться прежней.
            "qr_login_available": qr_login_available(request),
        },
    )


def sign_out_response(request):
    logout(request)
    request.session.flush()
    return redirect("/login/")


def favicon_response():
    response = HttpResponse(FAVICON_SVG, content_type="image/svg+xml")
    response["Cache-Control"] = "public, max-age=86400"
    return response


def landing_submit_response(request):
    if request.method != "POST":
        return HttpResponseNotFound()

    ignored_keys = {
        "csrfmiddlewaretoken",
        "formservices[]",
        "tildaspec-formname",
        "_redirect",
        "_page",
        "_form_id",
    }
    payload = {}
    for key, values in request.POST.lists():
        if key in ignored_keys:
            continue
        cleaned = [value.strip() for value in values if value.strip()]
        if not cleaned:
            continue
        payload[key] = cleaned[0] if len(cleaned) == 1 else cleaned

    if not payload:
        error = {"ok": False, "error": "Пустая форма"}
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse(error, status=400)
        return HttpResponse("Пустая форма", status=400)

    landing_dir = Path(settings.MEDIA_ROOT) / "landing"
    landing_dir.mkdir(parents=True, exist_ok=True)
    submission_path = landing_dir / "submissions.jsonl"
    record = {
        "timestamp": timezone.now().isoformat(),
        "path": request.POST.get("_page") or request.path,
        "form_id": request.POST.get("_form_id") or "",
        "form_name": (request.POST.get("tildaspec-formname") or "").strip(),
        "ip": request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        or request.META.get("REMOTE_ADDR", ""),
        "user_agent": request.META.get("HTTP_USER_AGENT", ""),
        "fields": payload,
    }
    with submission_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    recipient = os.environ.get("LANDING_LEADS_EMAIL", "").strip()
    if recipient:
        lines = [
            f"Время: {record['timestamp']}",
            f"Путь: {record['path']}",
            f"Форма: {record['form_name'] or record['form_id'] or 'landing'}",
            f"IP: {record['ip']}",
            "",
            "Данные:",
        ]
        for key, value in payload.items():
            if isinstance(value, list):
                lines.append(f"- {key}: {', '.join(value)}")
            else:
                lines.append(f"- {key}: {value}")
        send_mail(
            subject="Новая заявка с landing Fullbox",
            message="\n".join(lines),
            from_email=os.environ.get("DEFAULT_FROM_EMAIL", "fullbox@localhost"),
            recipient_list=[recipient],
            fail_silently=True,
        )

    redirect_url = request.POST.get("_redirect") or "/spasibo"
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "redirect": redirect_url})
    return redirect(redirect_url)


def build_project_text_context(*, title: str, subtitle: str, path: Path, action_buttons: list[dict] | None = None):
    return {
        "title": title,
        "subtitle": subtitle,
        "sections": load_sections(path),
        "action_buttons": action_buttons or [],
    }


def build_development_journal_context():
    local_path = settings.BASE_DIR.parent / "journal.md"
    remote_url = getattr(settings, "JOURNAL_REMOTE_URL", "")
    cache_seconds = getattr(settings, "JOURNAL_REMOTE_CACHE_SECONDS", 300)
    remote_data = load_remote_text(remote_url, cache_seconds)
    sections = load_journal_sections_from_text(remote_data) if remote_data else load_journal_sections(local_path)
    return {
        "title": "Журнал разработки",
        "subtitle": "Хронология изменений проекта",
        "sections": sections,
        "action_buttons": [
            {"label": "Описание структуры проекта", "url": "/project-structure/", "variant": "primary"},
            {"label": "Блок-схема проекта", "url": "/project-structure/diagram/"},
        ],
    }


def development_journal_file_response():
    local_path = settings.BASE_DIR.parent / "journal.md"
    remote_url = getattr(settings, "JOURNAL_REMOTE_URL", "")
    cache_seconds = getattr(settings, "JOURNAL_REMOTE_CACHE_SECONDS", 300)
    remote_data = load_remote_text(remote_url, cache_seconds)
    if remote_data is not None:
        response = HttpResponse(remote_data, content_type="text/markdown; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="journal.md"'
        return response
    return file_response(local_path, "journal.md")


def load_text_file(path: Path) -> str:
    try:
        data = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "Файл не найден."
    except OSError:
        return "Не удалось прочитать файл."
    return escape(data)


def load_sections(path: Path) -> list[dict]:
    try:
        data = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [{"title": "Ошибка", "body": "Файл не найден."}]
    except OSError:
        return [{"title": "Ошибка", "body": "Не удалось прочитать файл."}]
    return load_sections_from_text(data)


def split_sections_from_text(data: str) -> list[dict]:
    lines = data.splitlines()
    sections = []
    current = {"title": "Документ", "body": []}
    for line in lines:
        if line.lstrip().startswith("#"):
            if current["body"] or current["title"] != "Документ":
                sections.append(current)
            title = line.lstrip("#").strip() or "Раздел"
            current = {"title": title, "body": []}
        else:
            current["body"].append(line)
    if current["body"] or current["title"] != "Документ":
        sections.append(current)
    return sections


def decorate_sections(sections: list[dict]) -> list[dict]:
    accents = ["#d6a300", "#2dd4bf", "#60a5fa", "#f97316", "#34d399", "#f43f5e"]
    decorated = []
    for idx, section in enumerate(sections):
        decorated.append(
            {
                "title": escape(section["title"]),
                "body": escape("\n".join(section["body"]).strip()),
                "accent": accents[idx % len(accents)],
            }
        )
    return decorated


def load_sections_from_text(data: str) -> list[dict]:
    return decorate_sections(split_sections_from_text(data))


def parse_section_date(title: str):
    try:
        return datetime.strptime(str(title or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_journal_entry_datetime(line: str):
    match = _JOURNAL_ENTRY_RE.match(str(line or "").strip())
    if not match:
        return None
    raw_date, raw_time = match.groups()
    try:
        if raw_time:
            return datetime.strptime(f"{raw_date} {raw_time}", "%Y-%m-%d %H:%M")
        return datetime.combine(datetime.strptime(raw_date, "%Y-%m-%d").date(), dt_time.min)
    except ValueError:
        return None


def load_journal_sections(path: Path) -> list[dict]:
    try:
        data = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [{"title": "Ошибка", "body": "Файл не найден."}]
    except OSError:
        return [{"title": "Ошибка", "body": "Не удалось прочитать файл."}]
    return load_journal_sections_from_text(data)


def load_journal_sections_from_text(data: str) -> list[dict]:
    raw_sections = split_sections_from_text(data)
    grouped: dict[str, list[dict]] = {}
    undated_sections: list[dict] = []
    order_counter = 0

    def add_grouped(section_title: str, lines: list[str], sort_key: datetime, priority: int):
        nonlocal order_counter
        cleaned_lines = list(lines)
        while cleaned_lines and not cleaned_lines[-1].strip():
            cleaned_lines.pop()
        while cleaned_lines and not cleaned_lines[0].strip():
            cleaned_lines.pop(0)
        if not cleaned_lines:
            return
        grouped.setdefault(section_title, []).append(
            {
                "lines": cleaned_lines,
                "sort_key": sort_key,
                "priority": priority,
                "order": order_counter,
            }
        )
        order_counter += 1

    for section in raw_sections:
        section_date = parse_section_date(section["title"])
        section_title = section_date.isoformat() if section_date else section["title"]
        buffer: list[str] = []

        for line in section["body"]:
            entry_dt = parse_journal_entry_datetime(line)
            if entry_dt is not None:
                if buffer:
                    if section_date is not None:
                        add_grouped(
                            section_title,
                            buffer,
                            datetime.combine(section_date, dt_time.min),
                            priority=0,
                        )
                    else:
                        undated_sections.append({"title": section["title"], "body": list(buffer)})
                        order_counter += 1
                    buffer = []
                add_grouped(entry_dt.date().isoformat(), [line], entry_dt, priority=1)
                continue
            buffer.append(line)

        if buffer:
            if section_date is not None:
                add_grouped(
                    section_title,
                    buffer,
                    datetime.combine(section_date, dt_time.min),
                    priority=0,
                )
            else:
                undated_sections.append({"title": section["title"], "body": list(buffer)})
                order_counter += 1

    ordered_sections: list[dict] = []
    for section_title, items in sorted(grouped.items(), key=lambda item: item[0], reverse=True):
        items_sorted = sorted(
            items,
            key=lambda item: (item["sort_key"], item["priority"], item["order"]),
            reverse=True,
        )
        body: list[str] = []
        for item in items_sorted:
            if body and body[-1].strip() and item["lines"] and item["lines"][0].strip():
                body.append("")
            body.extend(item["lines"])
        ordered_sections.append({"title": section_title, "body": body})

    ordered_sections.extend(undated_sections)
    return decorate_sections(ordered_sections)


def load_remote_text(url: str, cache_seconds: int):
    if not url:
        return None
    now = time.time()
    cached = _REMOTE_JOURNAL_CACHE.get("data")
    if cached and now - _REMOTE_JOURNAL_CACHE.get("ts", 0) < cache_seconds:
        return cached
    try:
        response = requests.get(url, timeout=4)
        response.raise_for_status()
        data = response.text
    except requests.RequestException:
        return cached
    _REMOTE_JOURNAL_CACHE["ts"] = now
    _REMOTE_JOURNAL_CACHE["data"] = data
    return data


def file_response(path: Path, filename: str):
    try:
        return FileResponse(open(path, "rb"), as_attachment=True, filename=filename)
    except FileNotFoundError:
        return HttpResponse("Файл не найден.", status=404)
    except OSError:
        return HttpResponse("Не удалось прочитать файл.", status=500)
