"""Клиентский UI: выбор типа приёмки + форма распределения."""
from __future__ import annotations

import json
import logging
import uuid

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from client_cabinet import web_ui as client_web_ui
from client_cabinet.services import build_client_cabinet_url
from sku.models import Agency, SKU, SKUBarcode

from .models import ReceivingDistributionPlan
from .services.create import create_receiving_distribution
from .services.draft_ui import attach_check_to_plan, plan_to_form_context, save_ui_draft
from .services.excel import (
    MAX_UPLOAD_BYTES,
    assert_template_healthy,
    build_error_report_response,
    build_template_response,
    ensure_template_file,
    parse_distribution_excel,
)
from .services.validate import DistributionDraft, ValidationIssue

logger = logging.getLogger(__name__)


def _agency_or_403(request, pk: int) -> Agency | None:
    agency = get_object_or_404(Agency, pk=pk)
    if not request.user.is_authenticated:
        return None
    if not client_web_ui._check_agency_access(request, agency):
        return None
    return agency


def _known_sku_codes(agency: Agency) -> set[str]:
    return {
        str(c).casefold()
        for c in SKU.objects.filter(agency=agency).values_list("sku_code", flat=True)
        if c
    }


def _known_barcodes(agency: Agency) -> set[str]:
    return {
        str(c).casefold()
        for c in SKUBarcode.objects.filter(sku__agency=agency).values_list("value", flat=True)
        if c
    }


def _shell_context(agency: Agency) -> dict:
    return {
        "agency": agency,
        "cabinet_url": build_client_cabinet_url(agency.id),
        "choose_url": f"/client/{agency.id}/receiving/choose/",
        "sku_url": f"/client/{agency.id}/sku/",
        "template_url": f"/client/{agency.id}/receiving/distribution/template.xlsx",
        "check_url": f"/client/{agency.id}/receiving/distribution/check/",
        "error_report_url": f"/client/{agency.id}/receiving/distribution/error-report/",
    }


@require_http_methods(["GET"])
def receiving_type_choice(request, pk: int):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    agency = _agency_or_403(request, pk)
    if agency is None:
        return HttpResponseForbidden("Доступ запрещен")
    ctx = _shell_context(agency)
    ctx.update(
        {
            "ordinary_url": f"/orders/receiving/?client={agency.id}",
            "distribution_url": f"/client/{agency.id}/receiving/distribution/new/",
        }
    )
    return render(request, "receiving_distribution/type_choice.html", ctx)


@require_http_methods(["GET", "POST"])
def distribution_create(request, pk: int):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    agency = _agency_or_403(request, pk)
    if agency is None:
        return HttpResponseForbidden("Доступ запрещен")

    ensure_template_file()
    ctx = _shell_context(agency)
    ctx["client_request_id"] = str(uuid.uuid4())
    ctx["form_error"] = ""
    ctx["initial_json"] = ""
    ctx["max_upload_mb"] = MAX_UPLOAD_BYTES // (1024 * 1024)

    draft_id = (request.GET.get("draft") or "").strip()
    if request.method == "GET" and draft_id.isdigit():
        plan = (
            ReceivingDistributionPlan.objects.filter(
                pk=int(draft_id),
                agency=agency,
                status=ReceivingDistributionPlan.STATUS_DRAFT,
            )
            .first()
        )
        if plan:
            form_state = plan_to_form_context(plan)
            ctx["client_request_id"] = form_state["client_request_id"] or ctx["client_request_id"]
            ctx["initial_json"] = json.dumps(form_state, ensure_ascii=False)
            if plan.check_status == "ok" and plan.check_payload:
                # файл/схема могли устареть — помечаем как stale при reopen
                plan.check_status = "stale"
                plan.save(update_fields=["check_status", "updated_at"])
                form_state["check_status"] = "stale"
                form_state["needs_recheck"] = True
                ctx["initial_json"] = json.dumps(form_state, ensure_ascii=False)

    if request.method == "GET":
        return render(request, "receiving_distribution/create_form.html", ctx)

    action = (request.POST.get("action") or "save_draft").strip()
    client_request_id = (request.POST.get("client_request_id") or "").strip() or str(uuid.uuid4())
    raw_json = (request.POST.get("draft_json") or "").strip()
    check_payload_raw = (request.POST.get("check_payload_json") or "").strip()

    if raw_json:
        try:
            data = json.loads(raw_json)
            draft = DistributionDraft(
                meta=data.get("meta") or _meta_from_post(request),
                items=data.get("items") or [],
                directions=data.get("directions") or [],
                allocations=data.get("allocations") or [],
            )
        except json.JSONDecodeError:
            messages.error(request, "Некорректные данные формы.")
            return redirect(request.path)
    else:
        draft = _draft_from_manual_post(request)

    check_payload = {}
    if check_payload_raw:
        try:
            check_payload = json.loads(check_payload_raw)
        except json.JSONDecodeError:
            check_payload = {}

    if action == "save_draft":
        try:
            plan = save_ui_draft(
                agency=agency,
                draft=draft,
                user=request.user,
                client_request_id=client_request_id,
                check_payload=check_payload or None,
                check_status=str(check_payload.get("status") or ("ok" if check_payload.get("ok") else "uploaded")),
                uploaded_file=request.FILES.get("distribution_file"),
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(str(m) for m in exc.messages))
            ctx["client_request_id"] = client_request_id
            ctx["form_error"] = "; ".join(str(m) for m in exc.messages)
            return render(request, "receiving_distribution/create_form.html", ctx)
        messages.success(request, f"Черновик сохранён: {plan.receiving_order_id}.")
        return redirect(f"/client/{agency.id}/receiving/distribution/new/?draft={plan.id}")

    submit = action == "submit"
    try:
        result = create_receiving_distribution(
            agency=agency,
            draft=draft,
            user=request.user,
            submit=submit,
            client_request_id=client_request_id,
            allow_incomplete=not submit,
        )
        if check_payload or request.FILES.get("distribution_file"):
            attach_check_to_plan(
                result.plan,
                check_payload=check_payload,
                check_status="ok" if check_payload.get("ok") else result.plan.check_status or "ok",
                user=request.user,
                uploaded_file=request.FILES.get("distribution_file"),
            )
    except ValidationError as exc:
        messages.error(request, "; ".join(str(m) for m in exc.messages))
        ctx["client_request_id"] = client_request_id
        ctx["form_error"] = "; ".join(str(m) for m in exc.messages)
        return render(request, "receiving_distribution/create_form.html", ctx)

    if result.created:
        messages.success(
            request,
            f"Создана приёмка {result.receiving_order_id}"
            + (f", отгрузки: {', '.join(result.shipping_numbers)}" if result.shipping_numbers else "")
            + ("." if submit else " (черновик)."),
        )
    else:
        messages.info(request, f"Заявка уже создана ранее: {result.receiving_order_id}.")
    logger.info(
        "receiving_distribution created pr=%s shipping=%s user=%s agency=%s",
        result.receiving_order_id,
        result.shipping_numbers,
        request.user.id,
        agency.id,
    )
    return redirect(f"/orders/receiving/{result.receiving_order_id}/?client={agency.id}")


@require_http_methods(["POST"])
def distribution_check(request, pk: int):
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "Доступ запрещен"}, status=403)
    agency = _agency_or_403(request, pk)
    if agency is None:
        return JsonResponse({"ok": False, "error": "Доступ запрещен"}, status=403)

    uploaded = request.FILES.get("distribution_file")
    if not uploaded:
        return JsonResponse({"ok": False, "error": "Файл не загружен"}, status=400)

    logger.info(
        "receiving_distribution check start agency=%s user=%s file=%s size=%s",
        agency.id,
        request.user.id,
        getattr(uploaded, "name", ""),
        getattr(uploaded, "size", None),
    )
    preview = parse_distribution_excel(
        uploaded,
        known_sku_codes=_known_sku_codes(agency),
        known_barcodes=_known_barcodes(agency),
    )
    payload = preview.to_payload()
    payload["status"] = (
        "ok" if preview.ok and not preview.has_warnings else ("warnings" if preview.ok else "errors")
    )
    payload["filename"] = _safe_name(getattr(uploaded, "name", ""))
    payload["filesize"] = int(getattr(uploaded, "size", 0) or 0)
    logger.info(
        "receiving_distribution check done agency=%s ok=%s errors=%s warnings=%s",
        agency.id,
        payload["ok"],
        len(payload.get("issues") or []),
        len(payload.get("warnings") or []),
    )
    # кэш отчёта в сессии для скачивания
    request.session[f"rd_check_{agency.id}"] = {
        "issues": payload.get("issues") or [],
        "warnings": payload.get("warnings") or [],
    }
    return JsonResponse(payload)


@require_http_methods(["GET"])
def distribution_error_report(request, pk: int):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    agency = _agency_or_403(request, pk)
    if agency is None:
        return HttpResponseForbidden("Доступ запрещен")
    cached = request.session.get(f"rd_check_{agency.id}") or {}
    raw_issues = list(cached.get("issues") or []) + list(cached.get("warnings") or [])
    issues = [
        ValidationIssue(
            code=str(i.get("code") or ""),
            message=str(i.get("message") or ""),
            row_no=i.get("row_no"),
            sku_code=str(i.get("sku_code") or ""),
            severity=str(i.get("severity") or "error"),
            column=str(i.get("column") or ""),
            value=str(i.get("value") or ""),
            recommendation=str(i.get("recommendation") or ""),
        )
        for i in raw_issues
        if isinstance(i, dict)
    ]
    if not issues:
        messages.error(request, "Нет данных проверки для отчёта. Сначала проверьте файл.")
        return redirect(f"/client/{agency.id}/receiving/distribution/new/")
    return build_error_report_response(issues)


@require_http_methods(["GET"])
def distribution_template_download(request, pk: int):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    agency = _agency_or_403(request, pk)
    if agency is None:
        return HttpResponseForbidden("Доступ запрещен")
    try:
        ensure_template_file()
        ok, reason = assert_template_healthy()
        if not ok:
            logger.error("template unhealthy agency=%s reason=%s", agency.id, reason)
            messages.error(request, "Не удалось подготовить шаблон. Обратитесь к менеджеру Fullbox.")
            return redirect(f"/client/{agency.id}/receiving/distribution/new/")
        logger.info("receiving_distribution template download agency=%s user=%s", agency.id, request.user.id)
        return build_template_response()
    except Exception:
        logger.exception("template download failed agency=%s", agency.id)
        messages.error(request, "Не удалось подготовить шаблон. Обратитесь к менеджеру Fullbox.")
        return redirect(f"/client/{agency.id}/receiving/distribution/new/")


def _safe_name(name: str) -> str:
    raw = (name or "").split("/")[-1].split("\\")[-1]
    return raw[:200]


def _meta_from_post(request) -> dict:
    return {
        "eta_at": request.POST.get("eta_at") or "",
        "expected_boxes": request.POST.get("expected_boxes") or 0,
        "expected_pallets": request.POST.get("expected_pallets") or 0,
        "vehicle_number": request.POST.get("vehicle_number") or "",
        "driver_name": request.POST.get("driver_name") or "",
        "driver_phone": request.POST.get("driver_phone") or "",
        "comment": (request.POST.get("comment") or "")[:500],
    }


def _draft_from_manual_post(request) -> DistributionDraft:
    def _load(name: str) -> list:
        raw = (request.POST.get(name) or "[]").strip()
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

    return DistributionDraft(
        meta=_meta_from_post(request),
        items=_load("items_json"),
        directions=_load("directions_json"),
        allocations=_load("allocations_json"),
    )
