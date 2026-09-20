from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import TemplateView

from audit.models import OrderAuditEntry
from employees.access import RoleRequiredMixin
from fullbox.order_numbers import format_order_number
from super_car.services import (
    _mobile_request_route_summary,
    _mobile_route_detail,
    _short_agency_name,
    _task_count_label,
    _task_kind_label,
    build_dashboard_context,
    collect_moves as _collect_moves,
    create_move_request_response,
    handle_dashboard_post,
    lookup_item_pallets_response,
    lookup_pallet_location_response,
    mobile_category_key as _mobile_category_key,
    mobile_category_label as _mobile_category_label,
    mobile_number_label as _mobile_number_label,
    mobile_request_identity as _mobile_request_identity,
    mobile_request_key as _mobile_request_key,
    mobile_request_url as _mobile_request_url,
    mobile_task_url as _mobile_task_url,
)
from super_car.services.move_requests import _move_instruction
from super_car.services.task_commands import _repair_human_mojibake_text
from super_car.services.pallet_ops import (
    MOVE_MODE_BOX_FULL,
    MOVE_MODE_BOX_PARTIAL,
    MOVE_MODE_PALLET_FULL,
    _barcode_qty_total,
    _box_execution_plan,
    _move_boxes_to_otg,
    _normalize_move_mode,
    _pallet_box_plan,
    _parse_box_codes,
    _partial_request_covers_full_pallet,
    _payload_box_codes,
    _resolve_box_partial_codes,
    _resolve_otg_box_codes,
)

ALLOWED_ZONES = {"PR", "OTG", "MR", "OS", "OBR"}
ALLOWED_ROLES = (
    "super_car",
    "director",
    "admin",
)


def _repair_context_mojibake(value):
    if isinstance(value, str):
        return _repair_human_mojibake_text(value)
    if isinstance(value, dict):
        return {key: _repair_context_mojibake(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_repair_context_mojibake(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_repair_context_mojibake(item) for item in value)
    return value
CREATE_ROLES = (
    "super_car",
    "director",
    "admin",
)
MANUAL_CREATE_ROLES = ("admin",)
PROCESSING_MOVE_CREATE_ROLES = ("super_car", "director", "admin")

MOBILE_MOVE_CATEGORIES = (
    ("shipping", "Отгрузка"),
    ("movement", "Перемещения"),
    ("inventory", "Инвентаризация"),
    ("optimization", "Комплектовка"),
)


SUPER_CAR_CATEGORY_DEFINITIONS = (
    {
        "key": "placement_receiving",
        "section": "placement",
        "label": "\u0421 \u043f\u0440\u0438\u0435\u043c\u043a\u0438",
        "subtitle": "\u041f\u0430\u043b\u043b\u0435\u0442\u044b \u0438\u0437 \u0437\u043e\u043d\u044b PR",
    },
    {
        "key": "placement_processing",
        "section": "placement",
        "label": "\u0421 \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0438",
        "subtitle": "\u041f\u0430\u043b\u043b\u0435\u0442\u044b \u0438\u0437 \u0437\u043e\u043d\u044b OBR",
    },
    {
        "key": "assembly_shipping",
        "section": "assembly",
        "label": "\u041d\u0430 \u043e\u0442\u0433\u0440\u0443\u0437\u043a\u0443",
        "subtitle": "\u041f\u043e\u0434\u0431\u043e\u0440 \u0438 \u043f\u043e\u0434\u0430\u0447\u0430 \u0432 OTG",
    },
    {
        "key": "assembly_processing",
        "section": "assembly",
        "label": "\u041d\u0430 \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0443",
        "subtitle": "\u041f\u043e\u0434\u0431\u043e\u0440 \u0438 \u043f\u043e\u0434\u0430\u0447\u0430 \u0432 OBR",
    },
)

MOBILE_MOVE_CATEGORIES = tuple(
    (definition["key"], definition["label"])
    for definition in SUPER_CAR_CATEGORY_DEFINITIONS
)

SUPER_CAR_ROOT_SECTIONS = (
    {
        "key": "placement",
        "label": "\u0420\u0430\u0437\u043c\u0435\u0449\u0435\u043d\u0438\u0435",
        "subtitle": "\u041f\u0430\u043b\u043b\u0435\u0442\u044b \u043d\u0430 \u0445\u0440\u0430\u043d\u0435\u043d\u0438\u0435",
    },
    {
        "key": "assembly",
        "label": "\u0421\u0431\u043e\u0440\u043a\u0430",
        "subtitle": "\u041f\u043e\u0434\u0431\u043e\u0440 \u0438 \u043f\u043e\u0434\u0430\u0447\u0430 \u0433\u0440\u0443\u0437\u0430",
    },
    {
        "key": "extras",
        "label": "\u0414\u043e\u043f\u044b",
        "subtitle": "\u0411\u044b\u0441\u0442\u0440\u044b\u0435 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u044f",
    },
)

SUPER_CAR_EXTRA_ACTIONS = (
    {
        "section": "extras",
        "key": "locate_pallet",
        "label": "\u0423\u043a\u0430\u0437\u0430\u0442\u044c \u043c\u0435\u0441\u0442\u043e",
        "subtitle": "\u041e\u0442\u0441\u043a\u0430\u043d\u0438\u0440\u0443\u0439\u0442\u0435 \u043f\u0430\u043b\u043b\u0435\u0442\u0443 \u0438 \u0443\u0437\u043d\u0430\u0439\u0442\u0435, \u0433\u0434\u0435 \u043e\u043d\u0430 \u0441\u0442\u043e\u0438\u0442 \u0441\u0435\u0439\u0447\u0430\u0441.",
    },
)


def _latest_closed_placement_entries():
    entries = OrderAuditEntry.objects.filter(
        order_type__in=("receiving", "processing")
    ).order_by("-created_at")
    latest_by_order = {}
    blocked_orders = set()
    for entry in entries:
        order_key = (entry.order_type, str(entry.order_id))
        if order_key in latest_by_order or order_key in blocked_orders:
            continue
        payload = entry.payload or {}
        if payload.get("act") != "placement":
            continue
        state = (payload.get("act_state") or "closed").lower()
        if state != "closed":
            blocked_orders.add(order_key)
            continue
        latest_by_order[order_key] = entry
    return list(latest_by_order.values())


def _barcode_qty_preview(source: dict[str, int], limit: int = 4) -> str:
    entries = list((source or {}).items())
    if not entries:
        return ""
    preview = [f"{barcode} - {qty} шт." for barcode, qty in entries[: max(1, limit)]]
    if len(entries) > limit:
        preview.append("…")
    return "; ".join(preview)


@login_required
@require_GET
def lookup_pallet_location(request):
    return lookup_pallet_location_response(request)


@login_required
@require_GET
def lookup_item_pallets(request):
    return lookup_item_pallets_response(request)


@login_required
@require_POST
def create_move_request(request):
    return create_move_request_response(request)


class ReachtruckDashboardView(RoleRequiredMixin, TemplateView):
    template_name = "super_car/dashboard.html"
    allowed_roles = ALLOWED_ROLES

    def dispatch(self, request, *args, **kwargs):
        source = request.POST if request.method == "POST" else request.GET
        category = str(source.get("mobile_category") or "").strip().lower()
        request_key = str(source.get("mobile_request") or "").strip()
        is_shipping_request = request_key.startswith("shipping:")
        if category == "assembly_shipping" or is_shipping_request:
            if is_shipping_request:
                request_key = request_key.split(":", 1)[1].strip()
            target = "/otg-reachtruck/"
            if request_key:
                target = f"{target}?request={request_key}"
            return redirect(target)
        return super().dispatch(request, *args, **kwargs)

    def _render_error(self, message: str, *, status: int = 400):
        message = _repair_human_mojibake_text(message)
        request = getattr(self, "request", None)
        is_ajax = False
        if request is not None:
            requested_with = str(request.headers.get("X-Requested-With") or "").strip().lower()
            accepts = str(request.headers.get("Accept") or "").strip().lower()
            is_ajax = requested_with == "xmlhttprequest" or "application/json" in accepts
        if is_ajax:
            return JsonResponse({"ok": False, "error": message}, status=status)
        ctx = self.get_context_data(error=message)
        return self.render_to_response(ctx, status=status)

    def get_context_data(self, **kwargs):
        for key in ("error", "ok_message"):
            if key in kwargs:
                kwargs[key] = _repair_human_mojibake_text(kwargs.get(key))
        ctx = super().get_context_data(**kwargs)
        ctx.update(build_dashboard_context(self.request, **kwargs))
        for key in ("error", "ok_message"):
            if key in ctx:
                ctx[key] = _repair_human_mojibake_text(ctx.get(key))
        for key in (
            "mobile_selected_execution",
            "mobile_request_execution",
            "mobile_request_candidate_locations",
            "mobile_selected_task",
            "mobile_request_single_remaining",
        ):
            if key in ctx:
                ctx[key] = _repair_context_mojibake(ctx.get(key))
        return ctx

    def post(self, request, *args, **kwargs):
        return handle_dashboard_post(self, request, *args, **kwargs)


__all__ = [
    "ALLOWED_ROLES",
    "ALLOWED_ZONES",
    "CREATE_ROLES",
    "MANUAL_CREATE_ROLES",
    "MOBILE_MOVE_CATEGORIES",
    "MOVE_MODE_BOX_FULL",
    "MOVE_MODE_BOX_PARTIAL",
    "MOVE_MODE_PALLET_FULL",
    "PROCESSING_MOVE_CREATE_ROLES",
    "ReachtruckDashboardView",
    "_barcode_qty_preview",
    "_barcode_qty_total",
    "_box_execution_plan",
    "_collect_moves",
    "_latest_closed_placement_entries",
    "_mobile_category_key",
    "_mobile_category_label",
    "_mobile_number_label",
    "_mobile_request_identity",
    "_mobile_request_key",
    "_mobile_request_route_summary",
    "_mobile_request_url",
    "_mobile_route_detail",
    "_mobile_task_url",
    "_move_boxes_to_otg",
    "_move_instruction",
    "_normalize_move_mode",
    "_pallet_box_plan",
    "_parse_box_codes",
    "_partial_request_covers_full_pallet",
    "_payload_box_codes",
    "_resolve_box_partial_codes",
    "_resolve_otg_box_codes",
    "_short_agency_name",
    "_task_count_label",
    "_task_kind_label",
    "format_order_number",
    "create_move_request",
    "lookup_item_pallets",
    "lookup_pallet_location",
]
