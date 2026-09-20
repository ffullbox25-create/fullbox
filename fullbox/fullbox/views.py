from django.conf import settings
from django.contrib.auth import authenticate
from django.core.paginator import Paginator
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

from employees.access import role_required
from employees.models import Employee

from .web_ui import (
    build_development_journal_context,
    build_project_text_context,
    dev_login_response,
    development_journal_file_response,
    favicon_response,
    file_response,
    landing_submit_response,
    login_menu_response,
    role_cabinet_response,
    sign_in_response,
    sign_out_response,
)


INTERNAL_EMPLOYEE_ROLES = tuple(role for role, _label in Employee.ROLE_CHOICES)
INTERNAL_DOCUMENTATION_ROLES = ("developer", "admin")


def _resolve_portal_member_username_by_email(request):
    if request.method != "POST" or request.user.is_authenticated:
        return None, None

    raw_username = (request.POST.get("username") or "").strip()
    password = request.POST.get("password") or ""
    if "@" not in raw_username or not password:
        return None, None

    from client_cabinet.models import AgencyPortalMember

    members = list(
        AgencyPortalMember.objects.select_related("agency", "user")
        .filter(
            email__iexact=raw_username,
            is_active=True,
            agency__archived=False,
            user__isnull=False,
            user__is_active=True,
        )
        .order_by("agency_id", "id")
    )
    if not members:
        return None, None

    matched_usernames = [
        member.user.username
        for member in members
        if authenticate(request, username=member.user.username, password=password)
    ]
    if len(matched_usernames) == 1:
        return matched_usernames[0], None
    if len(matched_usernames) > 1:
        return None, "Email привязан к нескольким клиентским кабинетам. Войдите по логину сотрудника."
    return None, None


def login_menu(request):
    return login_menu_response(request)


def dev_login(request, username):
    return dev_login_response(request, username)


def role_cabinet(request, role):
    return role_cabinet_response(request, role)


@ensure_csrf_cookie
def sign_in(request):
    resolved_username, email_error = _resolve_portal_member_username_by_email(request)
    if email_error:
        from employees.qr_login import qr_login_available

        return render(
            request,
            "login.html",
            {
                "error": email_error,
                "qr_login_available": qr_login_available(request),
            },
        )
    if resolved_username:
        post_data = request.POST.copy()
        post_data["username"] = resolved_username
        request.POST = post_data
    return sign_in_response(request)


def sign_out(request):
    return sign_out_response(request)


def favicon(request):
    return favicon_response()


@ensure_csrf_cookie
def csrf_failure(request, reason=""):
    from employees.qr_login import qr_login_available

    return render(
        request,
        "login.html",
        {
            "error": "Сессия входа устарела. Попробуйте войти ещё раз.",
            "qr_login_available": qr_login_available(request),
        },
        status=403,
    )


@csrf_exempt
def landing_submit(request):
    return landing_submit_response(request)


@role_required(*INTERNAL_DOCUMENTATION_ROLES)
def project_description(request):
    return render(
        request,
        "project_text.html",
        build_project_text_context(
            title="Описание проекта",
            subtitle="Актуальное описание Fullbox из README.md",
            path=settings.BASE_DIR.parent / "README.md",
        ),
    )


@role_required(*INTERNAL_DOCUMENTATION_ROLES)
def project_structure(request):
    return render(
        request,
        "project_text.html",
        build_project_text_context(
            title="Описание структуры проекта",
            subtitle="Архитектура, границы модулей, рекомендации и риски",
            path=settings.BASE_DIR.parent / "architecture.md",
            action_buttons=[
                {"label": "Блок-схема проекта", "url": "/project-structure/diagram/", "variant": "primary"},
                {"label": "Журнал разработки", "url": "/development-journal/"},
            ],
        ),
    )


@role_required(*INTERNAL_DOCUMENTATION_ROLES)
def project_block_diagram(request):
    return render(
        request,
        "project_block_diagram.html",
        {
            "title": "Блок-схема проекта",
            "subtitle": "Связи между ролями, бизнес-процессами и системными модулями",
        },
    )


@role_required(*INTERNAL_DOCUMENTATION_ROLES)
def development_journal(request):
    return render(request, "project_text.html", build_development_journal_context())


@role_required(*INTERNAL_DOCUMENTATION_ROLES)
def project_description_file(request):
    return file_response(settings.BASE_DIR.parent / "README.md", "README.md")


@role_required(*INTERNAL_DOCUMENTATION_ROLES)
def development_journal_file(request):
    return development_journal_file_response()


@role_required(*INTERNAL_EMPLOYEE_ROLES)
def scanner_test(request):
    return render(request, "scanner_test.html")


@role_required(*INTERNAL_EMPLOYEE_ROLES)
def scanner_settings(request):
    return render(request, "scanner_settings.html")


@role_required(*INTERNAL_EMPLOYEE_ROLES)
def scanner_ims_2290hd(request):
    return render(request, "scanner_ims_2290hd.html")


def manager_billing_alias(request, subpath: str = ""):
    """ТЗ `/manager/billing/` → существующий кабинет `/team-manager/billing/`."""
    target = "/team-manager/billing/"
    if subpath:
        target = f"/team-manager/billing/{subpath.lstrip('/')}"
    qs = request.META.get("QUERY_STRING") or ""
    if qs:
        target = f"{target}?{qs}" if "?" not in target else f"{target}&{qs}"
    return HttpResponseRedirect(target)


def director_inventory(request):
    from django.http import HttpResponseForbidden

    from employees.access import get_request_role
    from sklad.ui_services import build_inventory_journal_page
    from sklad.views import _export_inventory_excel, inventory_pagination_links

    role = get_request_role(request)
    if role not in {"director", "admin", "developer"}:
        return HttpResponseForbidden("Доступ запрещен")

    page = build_inventory_journal_page(request=request)
    if isinstance(page, HttpResponseForbidden):
        return page
    if str(request.GET.get("export") or "").strip().lower() == "xlsx":
        return _export_inventory_excel(page)

    context = page["context"]
    paginator = Paginator(context.get("rows") or [], 100)
    page_obj = paginator.get_page(request.GET.get("page"))
    query_params = request.GET.copy()
    query_params.pop("page", None)
    context["rows"] = list(page_obj.object_list)
    context["page_obj"] = page_obj
    context["page_query"] = query_params.urlencode()
    context["page_links"] = inventory_pagination_links(page_obj)
    context["active_nav"] = "inventory"
    context["inventory_home_url"] = "/cabinet/director/inventory/"
    context["inventory_page_title"] = "Складские остатки"
    context["inventory_cabinet_url"] = "/cabinet/director/"
    context["inventory_hide_internal_stock_columns"] = True
    export_params = request.GET.copy()
    export_params.pop("page", None)
    export_params["export"] = "xlsx"
    context["export_excel_url"] = f"{request.path}?{export_params.urlencode()}"
    return render(request, "sklad/inventory_journal.html", context)
