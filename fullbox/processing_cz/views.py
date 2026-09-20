import json
from urllib.parse import urlencode

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.views import View
from django.views.decorators.http import require_POST

from employees.access import get_request_role, resolve_cabinet_url
from labels.utils import load_label_settings
from orders.services import ReceivingWorkflowService
from processing_app.services import ProcessingWorkflowService
from processing_app.stages import PROCESSING_STAGE_UNBOXING_OPENED, log_processing_stage
from processing_app.web_ui import ProcessingFlowView
from stockmap.views import _OS_CELLS_PER_TIER, _OS_ROW_SECTIONS, _OS_TIERS

from .services import (
    accepted_units,
    complete_flow,
    context_payload,
    issue_container_for_order,
    load_order_context,
    reopen_flow,
    save_flow_draft,
    scan_unit,
    serialize_unit,
    delete_unit,
    delete_units_for_box,
)


ALLOWED_ROLES = {"storekeeper", "processing_head", "processing_worker", "head_manager", "director", "admin", "manager"}
FINISH_ROLES = {"processing_head", "processing_worker"}


def _parse_json_body(request) -> dict | None:
    if not request.body:
        return {}
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _require_role(request, roles=ALLOWED_ROLES):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    role = get_request_role(request)
    if role not in roles:
        return HttpResponseForbidden("Доступ запрещен")
    return None


class ProcessingCzFlowView(LoginRequiredMixin, View):
    template_name = "orders/receiving_flow.html"

    def _context(self, order_id: str):
        context = load_order_context(order_id)
        if context is None:
            raise Http404("Заявка на обработку не найдена")
        return context

    def _flow_view(self, order_id: str) -> ProcessingFlowView:
        flow_view = ProcessingFlowView()
        flow_view.kwargs = {"order_id": str(order_id or "")}
        return flow_view

    def get(self, request, order_id: str):
        denied = _require_role(request)
        if denied:
            return denied
        context = self._context(order_id)
        flow_view = self._flow_view(context.order_id)
        if not flow_view._placement_act_entry(context.entries) and not flow_view._can_start(context.entries):
            query = urlencode(
                {
                    "error": (
                        "Нельзя открыть раскоробовку с ЧЗ: сначала завершите все карты обработки "
                        "и заполните результаты."
                    )
                }
            )
            return redirect(f"/orders/processing/{context.order_id}/work/?{query}")
        if not flow_view._placement_act_entry(context.entries):
            logged, _next_payload = log_processing_stage(
                order_id=context.order_id,
                payload=context.payload,
                stage=PROCESSING_STAGE_UNBOXING_OPENED,
                user=request.user if request.user.is_authenticated else None,
                agency=context.agency,
                description="Открыта раскоробовка с ЧЗ",
            )
            if logged:
                context = self._context(order_id)
        role = get_request_role(request)
        page_context = ProcessingWorkflowService.build_processing_flow_page_context(
            order_id=context.order_id,
            entries=context.entries,
            request=request,
            can_finish_flow=role in FINISH_ROLES,
            can_finish_flow_mismatch=role == "processing_head",
            can_reassign_boxes=role == "processing_head",
            is_directional_unboxing_payload=flow_view._is_directional_unboxing_payload,
            items_from_placement_act=flow_view._items_from_placement_act,
            normalize_flow_state=flow_view._normalize_flow_state,
            find_flow_state=flow_view._find_flow_state,
            placement_act_entry=flow_view._placement_act_entry,
            ok=request.GET.get("ok") == "1",
            error=request.GET.get("error") or "",
        )
        page_context.update(context_payload(context))
        page_context.update(
            {
                "flow_title": "Раскоробовка с ЧЗ",
                "flow_order_type": "processing",
                "flow_back_url": f"/orders/processing/{context.order_id}/work/",
                "flow_back_label": "К обработке",
                "flow_finish_label": "Завершить раскоробовку с ЧЗ",
                "flow_closed_label": "Раскоробовка закрыта",
                "flow_date_label": "Дата раскоробовки",
                "flow_reopen_label": "Внести изменения в раскоробовку?",
                "act_print_label": "Акт размещения",
                "receiving_mode": "cz",
                "receiving_mode_label": "Раскоробовка с ЧЗ",
                "cz_unit_scan_mode": True,
                "cz_accepted_units": [
                    serialize_unit(unit)
                    for unit in accepted_units(context.order_id).order_by("accepted_at", "id")
                ],
                "marking_scan_url": f"/orders/processing/{context.order_id}/cz-flow/scan/",
                "cz_unit_delete_url": f"/orders/processing/{context.order_id}/cz-flow/unit/delete/",
                "cz_box_units_delete_url": f"/orders/processing/{context.order_id}/cz-flow/box/delete-units/",
                "container_code_url": f"/orders/processing/{context.order_id}/cz-flow/container-code/",
                "box_code_rebind_url": "",
                "box_action_url": f"/orders/processing/{context.order_id}/flow/box-action/",
                "item_weight_url": "",
                "cabinet_url": resolve_cabinet_url(role),
                "current_agency_id": context.agency.id if context.agency else 0,
                "can_change_goods_type": False,
                "goods_type_choices": list(ReceivingWorkflowService.RECEIVING_GOODS_TYPE_LABELS.items()),
                "can_send_to_warehouse_action": False,
                "show_warehouse_move_action": False,
                "warehouse_move_status": "",
                "warehouse_move_rows": [],
                "warehouse_move_error": "",
                "warehouse_move_created_count": 0,
                "warehouse_move_skipped_count": 0,
                "warehouse_move_missing_count": 0,
                "act_print_url": "",
                "label_settings": page_context.get("label_settings") or load_label_settings(),
                "preferred_agent_id": "",
                "preferred_agent_locked": False,
                "os_config": {
                    "row_sections": _OS_ROW_SECTIONS,
                    "tiers": _OS_TIERS,
                    "cells_per_tier": _OS_CELLS_PER_TIER,
                    "mr_rows": [1, 2, 3, 4],
                },
                "ok": request.GET.get("ok") == "1",
                "error": request.GET.get("error") or "",
                "goods_type_updated": False,
                "goods_type_error": False,
            }
        )
        return render(request, self.template_name, page_context)

    def post(self, request, order_id: str):
        denied = _require_role(request)
        if denied:
            return denied
        context = self._context(order_id)
        flow_action = (request.POST.get("flow_action") or "").strip().lower()
        if flow_action == "draft":
            result = save_flow_draft(context=context, request=request)
            if result.status == "forbidden":
                return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
            if result.status in {"not_allowed", "invalid_json", "closed", "box_move_head_only"}:
                return JsonResponse({"ok": False, "error": result.status}, status=400)
            if result.status == "session_not_found":
                return JsonResponse({"ok": False, "error": result.status}, status=404)
            return JsonResponse({"ok": True, "session_id": result.session_id})
        if flow_action == "reopen":
            result = reopen_flow(context=context, request=request)
            if result.status == "forbidden":
                return HttpResponseForbidden("Доступ запрещен")
            return redirect(f"/orders/processing/{context.order_id}/cz-flow/?reopen=1")
        if flow_action:
            return redirect(f"/orders/processing/{context.order_id}/cz-flow/")

        role = get_request_role(request) or ""
        if role not in FINISH_ROLES:
            return HttpResponseForbidden("Доступ запрещен")
        action = (request.POST.get("action") or "close").strip().lower()
        if action != "close":
            return redirect(f"/orders/processing/{context.order_id}/cz-flow/?error=unknown_action")
        status, _message = complete_flow(context=context, request=request, role=role)
        if status == "ok":
            return redirect(f"/orders/processing/{context.order_id}/cz-flow/?ok=1")
        return redirect(f"/orders/processing/{context.order_id}/cz-flow/?error={status}")


@require_POST
def processing_cz_container_code(request, order_id: str):
    denied = _require_role(request)
    if denied:
        return denied
    context = load_order_context(order_id)
    if context is None:
        return JsonResponse({"ok": False, "error": "order_not_found"}, status=404)
    payload = _parse_json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    result = issue_container_for_order(
        context,
        kind=payload.get("kind") or "box",
        goods_type=payload.get("goods_type") or "",
    )
    status = 200 if result.get("ok") else 400
    return JsonResponse(result, status=status)


@require_POST
def processing_cz_scan(request, order_id: str):
    denied = _require_role(request)
    if denied:
        return denied
    context = load_order_context(order_id)
    if context is None:
        return JsonResponse({"ok": False, "error": "order_not_found"}, status=404)
    payload = _parse_json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    try:
        result = scan_unit(
            context=context,
            barcode=payload.get("barcode") or "",
            marking_code=payload.get("marking_code") or payload.get("code") or "",
            box_code=payload.get("box_code") or payload.get("box_barcode") or "",
            pallet_code=payload.get("pallet_code") or "",
            sku_code=payload.get("sku_code") or "",
            size=payload.get("size") or "",
            user=request.user if request.user.is_authenticated else None,
        )
    except IntegrityError:
        return JsonResponse({"ok": False, "error": "Код ЧЗ уже принят."}, status=409)
    if result.status != "ok":
        status = 409 if result.status in {"duplicate", "conflict"} else 400
        return JsonResponse({"ok": False, "error": result.error, "status": result.status}, status=status)
    return JsonResponse(
        {
            "ok": True,
            "unit": serialize_unit(result.unit),
            "item_count": result.item_count,
            "total_count": result.total_count,
        }
    )


@require_POST
def processing_cz_delete_unit(request, order_id: str):
    denied = _require_role(request)
    if denied:
        return denied
    context = load_order_context(order_id)
    if context is None:
        return JsonResponse({"ok": False, "error": "order_not_found"}, status=404)
    payload = _parse_json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    try:
        unit_id = int(payload.get("unit_id") or 0)
    except (TypeError, ValueError):
        unit_id = 0
    if unit_id <= 0:
        return JsonResponse({"ok": False, "error": "unit_not_found"}, status=404)
    result = delete_unit(context=context, unit_id=unit_id)
    if result.status != "ok":
        status = 404 if result.status == "not_found" else 400
        return JsonResponse({"ok": False, "error": result.error, "status": result.status}, status=status)
    return JsonResponse({"ok": True, "unit": result.unit, "total_count": result.total_count})


@require_POST
def processing_cz_delete_box_units(request, order_id: str):
    denied = _require_role(request)
    if denied:
        return denied
    context = load_order_context(order_id)
    if context is None:
        return JsonResponse({"ok": False, "error": "order_not_found"}, status=404)
    payload = _parse_json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    result = delete_units_for_box(context=context, box_code=payload.get("box_code") or "")
    if result.status != "ok":
        return JsonResponse({"ok": False, "error": result.error, "status": result.status}, status=400)
    return JsonResponse({"ok": True, "units": result.units, "total_count": result.total_count})
