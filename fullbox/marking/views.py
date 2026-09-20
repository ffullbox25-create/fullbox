import json
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from agent.models import DeviceAgent
from audit.models import OrderAuditEntry
from employees.access import get_request_effective_role, get_request_role, request_has_any_role
from labels.utils import agent_printer_names_from_meta, split_printers_by_kind
from processing_app.agent_selection import deduplicate_scanner_device_agents
from sku.models import SKU
from .services import (
    free_marking_batch_status_response,
    free_marking_candidates_response,
    free_marking_confirm_printed_response,
    free_marking_import_response,
    free_marking_print_page_context,
    free_marking_queue_response,
    honest_sign_duplicate_print_response,
    honest_sign_duplicate_validate_response,
    honest_sign_status_check_response,
    is_true_api_configured,
    processing_marking_import_response,
    processing_marking_batch_status_response,
    processing_marking_confirm_printed_response,
    processing_marking_print_response,
    processing_marking_queue_response,
    processing_marking_reset_printed_response,
    processing_marking_scan_response,
    processing_marking_summary_response,
    receiving_marking_scan_response,
    return_printed_marking_response,
)
from .reporting import marking_report_context, marking_report_export_response

ALLOWED_PROCESSING_ROLES = {"storekeeper", "processing_head", "head_manager", "director", "admin"}
ALLOWED_RECEIVING_ROLES = {"storekeeper", "manager", "head_manager", "director", "admin"}
ALLOWED_STATUS_CHECK_ROLES = {
    "storekeeper",
    "processing_head",
    "head_manager",
    "director",
    "admin",
}
ALLOWED_DUPLICATE_ROLES = ALLOWED_STATUS_CHECK_ROLES
ALLOWED_FREE_PRINT_ROLES = {"processing_head", "head_manager", "manager", "director", "admin"}
ALLOWED_RETURN_ROLES = {"processing_head", "head_manager", "director", "admin"}
ALLOWED_REPORT_ROLES = {"processing_head", "head_manager", "manager", "director", "admin"}


def _parse_json_body(request):
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _normalize_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    return text


def _get_processing_order(order_id: str):
    if not order_id:
        return None, None, None
    latest = (
        OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
        .select_related("agency")
        .order_by("-created_at")
        .first()
    )
    if not latest:
        return None, None, None
    return latest, latest.payload or {}, latest.agency


def _get_receiving_order(order_id: str):
    if not order_id:
        return None, None, None
    latest = (
        OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
        .select_related("agency")
        .order_by("-created_at")
        .first()
    )
    if not latest:
        return None, None, None
    return latest, latest.payload or {}, latest.agency


def _resolve_sku(agency, sku_code: str):
    if not sku_code:
        return None
    qs = SKU.objects.filter(sku_code=sku_code)
    if agency:
        sku = qs.filter(agency=agency).first()
        if sku:
            return sku
        return qs.filter(agency__isnull=True).first()
    return qs.first()


def _require_processing_role(request):
    role = get_request_role(request)
    if role not in ALLOWED_PROCESSING_ROLES:
        return False, HttpResponseForbidden("Доступ запрещен")
    return True, None


def _require_receiving_role(request):
    role = get_request_role(request)
    if role not in ALLOWED_RECEIVING_ROLES:
        return False, HttpResponseForbidden("Доступ запрещен")
    return True, None


def _require_status_check_role(request):
    role = get_request_role(request)
    if role not in ALLOWED_STATUS_CHECK_ROLES:
        return False, HttpResponseForbidden("Доступ запрещен")
    return True, None


def _require_duplicate_role(request):
    role = get_request_role(request)
    if role not in ALLOWED_DUPLICATE_ROLES:
        return False, HttpResponseForbidden("Доступ запрещен")
    return True, None


def _require_free_print_role(request):
    role = get_request_role(request)
    if role not in ALLOWED_FREE_PRINT_ROLES:
        return False, HttpResponseForbidden("Доступ запрещен")
    return True, None


def _require_return_role(request):
    role = get_request_role(request)
    if role not in ALLOWED_RETURN_ROLES:
        return False, HttpResponseForbidden("Доступ запрещен")
    return True, None


def _require_report_role(request):
    if not request_has_any_role(request, ALLOWED_REPORT_ROLES):
        return False, HttpResponseForbidden("Доступ запрещен")
    return True, None


def _duplicate_print_agents() -> list[dict]:
    online_since = timezone.now() - timedelta(seconds=60)
    agents = DeviceAgent.objects.all().order_by("-last_seen", "-updated_at")
    result = []
    for entry in deduplicate_scanner_device_agents(agents):
        agent = entry["agent"]
        printers, _virtual = split_printers_by_kind(
            agent_printer_names_from_meta(agent.meta if isinstance(agent.meta, dict) else {})
        )
        if not printers:
            continue
        result.append(
            {
                "id": str(agent.agent_id or "").strip(),
                "label": str(agent.name or agent.host or agent.agent_id or "").strip(),
                "printers": printers,
                "is_online": bool(agent.last_seen and agent.last_seen >= online_since),
                "aliases": list(entry.get("aliases") or []),
            }
        )
    return result


def _extract_receiving_items(payload: dict) -> list[dict]:
    payload = payload or {}
    result = {}
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        sku_code = str(item.get("sku_code") or item.get("sku") or "").strip()
        if not sku_code:
            continue
        size = str(item.get("size") or "").strip()
        barcode = str(item.get("barcode") or "").strip()
        key = (sku_code, size)
        if key not in result:
            result[key] = {
                "sku_code": sku_code,
                "size": size,
                "barcode": barcode,
                "qty": 0,
            }
        qty_raw = item.get("qty") or item.get("actual_qty")
        qty = _normalize_cell(qty_raw)
        try:
            qty_value = int(qty) if qty != "" else 0
        except ValueError:
            qty_value = 0
        result[key]["qty"] += qty_value
        if not result[key]["barcode"] and barcode:
            result[key]["barcode"] = barcode
    return list(result.values())


@login_required
@require_GET
def honest_sign_status_page(request):
    ok, response = _require_status_check_role(request)
    if not ok:
        return response
    role = get_request_role(request)
    return render(
        request,
        "marking/status_check.html",
        {
            "true_api_configured": is_true_api_configured(),
            "status_check_role": role,
        },
    )


@login_required
@require_POST
def honest_sign_status_check(request):
    return honest_sign_status_check_response(request=request)


@login_required
@require_GET
def honest_sign_duplicate_page(request):
    ok, response = _require_duplicate_role(request)
    if not ok:
        return response
    role = get_request_role(request)
    return render(
        request,
        "marking/duplicate.html",
        {
            "duplicate_role": role,
            "print_agents": _duplicate_print_agents(),
            "true_api_configured": is_true_api_configured(),
        },
    )


@login_required
@require_POST
def honest_sign_duplicate_validate(request):
    return honest_sign_duplicate_validate_response(request=request)


@login_required
@require_POST
def honest_sign_duplicate_print(request):
    return honest_sign_duplicate_print_response(request=request)


@login_required
@require_GET
def free_marking_print_page(request):
    ok, response = _require_free_print_role(request)
    if not ok:
        return response
    context = free_marking_print_page_context(
        order_type=request.GET.get("order_type") or "processing",
        order_id=request.GET.get("order_id") or "",
    )
    role = get_request_effective_role(
        request,
        preferred_roles=("processing_head", "head_manager", "manager", "director", "admin"),
    ) or ""
    role_context = {
        "processing_head": ("Руководитель обработки", "/processing-head/"),
        "head_manager": ("Начальник склада", "/head-manager/"),
        "manager": ("Менеджер", "/team-manager/"),
        "director": ("Директор", "/cabinet/director/"),
        "admin": ("Администратор", "/admin/"),
    }
    role_label, home_url = role_context.get(role, ("Сотрудник", "/"))
    context.update(
        {
            "free_print_role": role,
            "free_print_role_label": role_label,
            "free_print_home_url": home_url,
            "print_agents": _duplicate_print_agents(),
        }
    )
    return render(request, "marking/free_print.html", context)


@login_required
@require_POST
def free_marking_import(request):
    return free_marking_import_response(request=request)


@login_required
@require_POST
def free_marking_candidates(request):
    return free_marking_candidates_response(request=request)


@login_required
@require_POST
def free_marking_queue(request):
    return free_marking_queue_response(request=request)


@login_required
@require_POST
def free_marking_batch_status(request):
    return free_marking_batch_status_response(request=request)


@login_required
@require_POST
def free_marking_confirm_printed(request):
    return free_marking_confirm_printed_response(request=request)


@login_required
@require_GET
def return_printed_marking_page(request):
    ok, response = _require_return_role(request)
    if not ok:
        return response
    return render(
        request,
        "marking/return.html",
        {"return_role": get_request_role(request)},
    )


@login_required
@require_POST
def return_printed_marking_scan(request):
    return return_printed_marking_response(request=request)


@login_required
@require_GET
def marking_report_page(request):
    ok, response = _require_report_role(request)
    if not ok:
        return response
    if str(request.GET.get("export") or "").strip().lower() == "xlsx":
        return marking_report_export_response(request)
    role = get_request_effective_role(
        request,
        preferred_roles=("processing_head", "head_manager", "manager", "director", "admin"),
    )
    return render(
        request,
        "marking/report.html",
        marking_report_context(request, role=role or ""),
    )


@login_required
@require_GET
def processing_marking_summary(request, order_id: str):
    return processing_marking_summary_response(request=request, order_id=order_id)


@login_required
@require_POST
def processing_marking_scan(request, order_id: str):
    return processing_marking_scan_response(request=request, order_id=order_id)


@login_required
@require_POST
def receiving_marking_scan(request, order_id: str):
    return receiving_marking_scan_response(request=request, order_id=order_id)


@login_required
@require_POST
def processing_marking_print(request, order_id: str):
    return processing_marking_print_response(request=request, order_id=order_id)


@login_required
@require_POST
def processing_marking_queue(request, order_id: str):
    return processing_marking_queue_response(request=request, order_id=order_id)


@login_required
@require_POST
def processing_marking_batch_status(request, order_id: str):
    return processing_marking_batch_status_response(request=request, order_id=order_id)


@login_required
@require_POST
def processing_marking_confirm_printed(request, order_id: str):
    return processing_marking_confirm_printed_response(request=request, order_id=order_id)


@login_required
@require_POST
def processing_marking_reset_printed(request, order_id: str):
    return processing_marking_reset_printed_response(request=request, order_id=order_id)


@login_required
@require_POST
def processing_marking_import(request, order_id: str):
    return processing_marking_import_response(request=request, order_id=order_id)
