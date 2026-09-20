import json

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.views import View
from django.views.decorators.http import require_POST
from urllib.parse import urlencode, unquote_plus

from employees.access import get_request_role, resolve_cabinet_url
from orders.services import ReceivingWorkflowService
from orders.web_ui import export_receiving_flow_current_xlsx
from stockmap.views import _OS_CELLS_PER_TIER, _OS_ROW_SECTIONS, _OS_TIERS

from .services import (
    accepted_units,
    complete_flow,
    delete_unit,
    delete_units_for_box,
    duplicate_marking_usage,
    issue_container_for_order,
    load_order_context,
    reconcile_flow_state_with_units,
    scan_unit,
    serialize_unit,
)


def _parse_json_body(request) -> dict | None:
    if not request.body:
        return {}
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _require_storekeeper(request):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    role = get_request_role(request)
    if role != "storekeeper":
        return HttpResponseForbidden("Доступ запрещен")
    return None


def _receiving_owner_lock_response(request, context, *, json_response: bool = False):
    access = ReceivingWorkflowService.receiving_work_access(
        entries=context.entries,
        user=request.user if request.user.is_authenticated else None,
    )
    if access.status == "allowed":
        return None
    owner_name = str(access.payload.get("storekeeper_name") or "другого кладовщика")
    message = f"Заявка уже в работе у {owner_name}."
    if json_response:
        return JsonResponse(
            {
                "ok": False,
                "error": "assigned_to_other_storekeeper",
                "storekeeper_name": owner_name,
            },
            status=409,
        )
    return HttpResponseForbidden(message)


def _parse_flow_client_version(value) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _flow_client_version_from_entries(entries) -> int:
    for entry in reversed(entries or []):
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        if isinstance(payload.get("flow_state"), dict):
            return _parse_flow_client_version(payload.get("flow_client_version"))
    return 0


class ReceivingCzFlowView(LoginRequiredMixin, View):
    template_name = "orders/receiving_flow.html"

    def _context(self, order_id: str):
        context = load_order_context(order_id)
        if context is None:
            raise Http404("Заявка не найдена")
        return context

    def get(self, request, order_id: str):
        denied = _require_storekeeper(request)
        if denied:
            return denied
        context = self._context(order_id)
        start_result = ReceivingWorkflowService.start_receiving_work(
            order_id=context.order_id,
            entries=context.entries,
            role=get_request_role(request) or "",
            user=request.user if request.user.is_authenticated else None,
        )
        if start_result.status == "assigned_to_other_storekeeper":
            owner_name = str(start_result.payload.get("storekeeper_name") or "другого кладовщика")
            return HttpResponseForbidden(f"Заявка уже в работе у {owner_name}.")
        context = self._context(order_id)
        role = get_request_role(request)
        page_context = ReceivingWorkflowService.build_receiving_flow_page_context(
            order_id=context.order_id,
            entries=context.entries,
            role=role or "",
            user=request.user,
            session_key=request.session.session_key or "",
            query_params={
                "warehouse_move": request.GET.get("warehouse_move"),
                "warehouse_created": request.GET.get("warehouse_created"),
                "warehouse_skipped": request.GET.get("warehouse_skipped"),
                "warehouse_missing": request.GET.get("warehouse_missing"),
                "warehouse_error": unquote_plus(str(request.GET.get("warehouse_error") or "").strip()),
            },
            row_sections=_OS_ROW_SECTIONS,
            tiers=_OS_TIERS,
            cells_per_tier=_OS_CELLS_PER_TIER,
        )
        page_context.update(
            {
                "receiving_mode": "cz",
                "receiving_mode_label": "Потоковая приемка с ЧЗ",
                "cz_unit_scan_mode": True,
                "cz_accepted_units": [
                    serialize_unit(unit)
                    for unit in accepted_units(context.order_id).order_by("accepted_at", "id")
                ],
                "marking_scan_url": f"/orders/receiving/{context.order_id}/cz-flow/scan/",
                "cz_unit_delete_url": f"/orders/receiving/{context.order_id}/cz-flow/unit/delete/",
                "cz_box_units_delete_url": f"/orders/receiving/{context.order_id}/cz-flow/box/delete-units/",
                "container_code_url": f"/orders/receiving/{context.order_id}/cz-flow/container-code/",
                "flow_current_export_url": f"/orders/receiving/{context.order_id}/cz-flow/export/",
                "box_code_rebind_url": "",
                "cabinet_url": resolve_cabinet_url(role),
                "can_change_goods_type": False,
                "can_capture_receiving_services": bool(
                    role == "storekeeper" and not page_context.get("flow_locked", False)
                ),
                "os_config": {
                    "row_sections": _OS_ROW_SECTIONS,
                    "tiers": _OS_TIERS,
                    "cells_per_tier": _OS_CELLS_PER_TIER,
                    "mr_rows": [1, 2, 3, 4],
                },
                "ok": request.GET.get("ok") == "1",
                "error": request.GET.get("error") or "",
                "warehouse_error": str(request.GET.get("warehouse_error") or "").strip(),
                "services_error": str(request.GET.get("services_error") or "").strip(),
                "goods_type_updated": request.GET.get("goods_type_updated") == "1",
                "goods_type_error": request.GET.get("goods_type_error") == "1",
            }
        )
        units = list(accepted_units(context.order_id).order_by("accepted_at", "id"))
        source_flow_state = page_context.get("flow_state") or {}
        reconciled_flow_state = reconcile_flow_state_with_units(source_flow_state, units)
        page_context["flow_state"] = reconciled_flow_state
        page_context["cz_accepted_units"] = [serialize_unit(unit) for unit in units]
        page_context["cz_flow_state_reconciled"] = reconciled_flow_state != source_flow_state
        page_context["flow_client_version"] = _flow_client_version_from_entries(context.entries)

        return render(request, self.template_name, page_context)

    def post(self, request, order_id: str):
        denied = _require_storekeeper(request)
        if denied:
            return denied
        context = self._context(order_id)
        role = get_request_role(request) or ""
        flow_action = (request.POST.get("flow_action") or "").strip().lower()
        owner_lock = _receiving_owner_lock_response(
            request,
            context,
            json_response=flow_action == "draft",
        )
        if owner_lock:
            return owner_lock
        if flow_action == "draft":
            boxes_raw = request.POST.get("boxes_json") or "[]"
            pallets_raw = request.POST.get("pallets_json") or "[]"
            active_box = request.POST.get("active_box") or ""
            active_pallet = request.POST.get("active_pallet") or ""
            try:
                boxes_data = json.loads(boxes_raw)
                pallets_data = json.loads(pallets_raw)
            except json.JSONDecodeError:
                boxes_data = None
                pallets_data = None
            if isinstance(boxes_data, list) and isinstance(pallets_data, list):
                reconciled = reconcile_flow_state_with_units(
                    {
                        "boxes": boxes_data,
                        "pallets": pallets_data,
                        "activeBox": active_box,
                        "activePallet": active_pallet,
                    },
                    list(accepted_units(context.order_id).order_by("accepted_at", "id")),
                    active_box=active_box,
                    active_pallet=active_pallet,
                )
                boxes_raw = json.dumps(reconciled["boxes"], ensure_ascii=False)
                pallets_raw = json.dumps(reconciled["pallets"], ensure_ascii=False)
                active_box = reconciled["activeBox"]
                active_pallet = reconciled["activePallet"]
            result = ReceivingWorkflowService.save_receiving_flow_draft(
                order_id=context.order_id,
                entries=context.entries,
                role=role,
                boxes_raw=boxes_raw,
                pallets_raw=pallets_raw,
                active_box=active_box,
                active_pallet=active_pallet,
                received_at=request.POST.get("received_at") or "",
                draft_version=request.POST.get("flow_client_version") or "",
                draft_base_version=request.POST.get("flow_base_version") or "",
                user=request.user if request.user.is_authenticated else None,
            )
            if result.status == "forbidden":
                return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
            if result.status in {"closed", "not_allowed", "invalid_json"}:
                return JsonResponse({"ok": False, "error": result.status}, status=400)
            if result.status == "pallet_composition_locked":
                return JsonResponse(
                    {
                        "ok": False,
                        "error": result.status,
                        "pallet_codes": result.meta.get("pallet_codes") or [],
                        "message": (
                            "Состав паллеты нельзя менять после передачи на размещение. "
                            "Для изменения выполните складскую корректировку."
                        ),
                    },
                    status=409,
                )
            if result.status == "goods_type_locked":
                return JsonResponse(
                    {
                        "ok": False,
                        "error": result.status,
                        "container_codes": result.meta.get("container_codes") or [],
                        "message": "Тип приемки зафиксирован при начале работы и не может быть изменен.",
                    },
                    status=409,
                )
            if result.status == "assigned_to_other_storekeeper":
                return JsonResponse(
                    {
                        "ok": False,
                        "error": result.status,
                        "storekeeper_name": result.payload.get("storekeeper_name") or "другого кладовщика",
                    },
                    status=409,
                )
            if result.status == "stale_draft" or result.meta.get("stale_ignored"):
                current_context = self._context(order_id)
                return JsonResponse(
                    {
                        "ok": False,
                        "error": "stale_draft",
                        "flow_client_version": _flow_client_version_from_entries(
                            current_context.entries
                        ),
                    },
                    status=409,
                )
            if result.status != "saved":
                return JsonResponse({"ok": False, "error": result.status}, status=400)
            return JsonResponse(
                {
                    "ok": True,
                    "flow_client_version": result.payload.get("flow_client_version", 0),
                }
            )
        if flow_action == "set_goods_type":
            return redirect(f"/orders/receiving/{context.order_id}/cz-flow/?goods_type_error=1")
        if flow_action == "reopen":
            result = ReceivingWorkflowService.reopen_receiving_flow(
                order_id=context.order_id,
                entries=context.entries,
                role=role,
                user=request.user if request.user.is_authenticated else None,
            )
            if result.status == "denied":
                return HttpResponseForbidden("Доступ запрещен")
            return redirect(f"/orders/receiving/{context.order_id}/cz-flow/?ok=1")
        if flow_action:
            return redirect(f"/orders/receiving/{context.order_id}/cz-flow/")
        action = (request.POST.get("action") or "close").strip().lower()
        if action != "close":
            return redirect(f"/orders/receiving/{context.order_id}/cz-flow/?error=unknown_action")
        try:
            status, completion_message = complete_flow(
                context=context,
                user=request.user if request.user.is_authenticated else None,
                received_at=request.POST.get("received_at") or "",
                receiving_location_code=request.POST.get("receiving_location_code") or "",
            )
        except ValidationError as exc:
            message = "; ".join(str(item) for item in getattr(exc, "messages", []) if str(item)) or str(exc)
            query = urlencode({"error": "services_required", "services_error": message})
            return redirect(f"/orders/receiving/{context.order_id}/cz-flow/?{query}")
        except ValueError as exc:
            query = urlencode({"error": "warehouse_locked", "warehouse_error": str(exc)})
            return redirect(f"/orders/receiving/{context.order_id}/cz-flow/?{query}")
        if status in {"ok", "closed"}:
            return redirect(f"/orders/receiving/{context.order_id}/cz-flow/?ok=1")
        if completion_message:
            query = urlencode({"error": "warehouse_locked", "warehouse_error": completion_message})
            return redirect(f"/orders/receiving/{context.order_id}/cz-flow/?{query}")
        return redirect(f"/orders/receiving/{context.order_id}/cz-flow/?error={status}")


@require_POST
def receiving_cz_flow_export(request, order_id: str):
    denied = _require_storekeeper(request)
    if denied:
        return denied
    return export_receiving_flow_current_xlsx(request, order_id)


@require_POST
def receiving_cz_container_code(request, order_id: str):
    denied = _require_storekeeper(request)
    if denied:
        return denied
    context = load_order_context(order_id)
    if context is None:
        return JsonResponse({"ok": False, "error": "order_not_found"}, status=404)
    owner_lock = _receiving_owner_lock_response(request, context, json_response=True)
    if owner_lock:
        return owner_lock
    payload = _parse_json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    result = issue_container_for_order(
        context,
        kind=payload.get("kind") or "box",
        received_at=payload.get("received_at") or "",
    )
    status = 200 if result.get("ok") else 400
    return JsonResponse(result, status=status)


@require_POST
def receiving_cz_scan(request, order_id: str):
    denied = _require_storekeeper(request)
    if denied:
        return denied
    context = load_order_context(order_id)
    if context is None:
        return JsonResponse({"ok": False, "error": "order_not_found"}, status=404)
    owner_lock = _receiving_owner_lock_response(request, context, json_response=True)
    if owner_lock:
        return owner_lock
    payload = _parse_json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    try:
        result = scan_unit(
            context=context,
            barcode=payload.get("barcode") or "",
            marking_code=payload.get("marking_code") or payload.get("code") or "",
            box_code=payload.get("box_code") or "",
            pallet_code=payload.get("pallet_code") or "",
            user=request.user if request.user.is_authenticated else None,
            confirm_gtin_mismatch=payload.get("confirm_gtin_mismatch") is True,
        )
    except IntegrityError:
        details = duplicate_marking_usage(
            payload.get("marking_code") or payload.get("code") or "",
        )
        return JsonResponse(
            {
                "ok": False,
                "error": details.pop("error", "Код ЧЗ уже принят."),
                "status": "duplicate",
                "reason_code": "duplicate",
                **details,
            },
            status=409,
        )
    if result.status != "ok":
        retry_reconcile = payload.get("retry_reconcile") is True
        target_box_code = str(payload.get("box_code") or "").strip()
        target_pallet_code = str(payload.get("pallet_code") or "").strip()
        if (
            retry_reconcile
            and result.status == "duplicate"
            and result.unit is not None
            and result.unit.order_id == context.order_id
            and str(result.unit.box_code or "").strip() == target_box_code
            and str(result.unit.pallet_code or "").strip() == target_pallet_code
        ):
            order_units = accepted_units(context.order_id)
            return JsonResponse(
                {
                    "ok": True,
                    "reconciled": True,
                    "unit": serialize_unit(result.unit),
                    "item_count": order_units.filter(
                        sku_code=result.unit.sku_code,
                        size=result.unit.size,
                    ).count(),
                    "total_count": order_units.count(),
                }
            )
        status = 409 if result.status in {"duplicate", "conflict"} else 400
        if result.status == "closed":
            status = 400
        return JsonResponse(
            {
                "ok": False,
                "error": result.error,
                "status": result.status,
                "reason_code": result.status,
                **result.details,
            },
            status=status,
        )
    return JsonResponse(
        {
            "ok": True,
            "unit": serialize_unit(result.unit),
            "item_count": result.item_count,
            "total_count": result.total_count,
            **result.details,
        }
    )


@require_POST
def receiving_cz_delete_unit(request, order_id: str):
    denied = _require_storekeeper(request)
    if denied:
        return denied
    context = load_order_context(order_id)
    if context is None:
        return JsonResponse({"ok": False, "error": "order_not_found"}, status=404)
    owner_lock = _receiving_owner_lock_response(request, context, json_response=True)
    if owner_lock:
        return owner_lock
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
    return JsonResponse(
        {
            "ok": True,
            "unit": result.unit,
            "total_count": result.total_count,
        }
    )


@require_POST
def receiving_cz_delete_box_units(request, order_id: str):
    denied = _require_storekeeper(request)
    if denied:
        return denied
    context = load_order_context(order_id)
    if context is None:
        return JsonResponse({"ok": False, "error": "order_not_found"}, status=404)
    owner_lock = _receiving_owner_lock_response(request, context, json_response=True)
    if owner_lock:
        return owner_lock
    payload = _parse_json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    result = delete_units_for_box(context=context, box_code=payload.get("box_code") or "")
    if result.status != "ok":
        status = 400
        return JsonResponse({"ok": False, "error": result.error, "status": result.status}, status=status)
    return JsonResponse(
        {
            "ok": True,
            "units": result.units,
            "total_count": result.total_count,
        }
    )
