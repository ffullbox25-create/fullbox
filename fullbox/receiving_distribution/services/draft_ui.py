"""Сохранение/открытие UI-черновика плана с файлом и результатом проверки."""
from __future__ import annotations

import logging
from typing import Any

from django.core.files.base import ContentFile
from django.utils import timezone

from sku.models import Agency

from ..models import ReceivingDistributionEvent, ReceivingDistributionPlan
from .create import create_receiving_distribution
from .validate import DistributionDraft

logger = logging.getLogger(__name__)


def _sanitize_filename(name: str) -> str:
    raw = (name or "distribution.xlsx").split("/")[-1].split("\\")[-1]
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw)
    return (safe or "distribution.xlsx")[:200]


def attach_check_to_plan(
    plan: ReceivingDistributionPlan,
    *,
    check_payload: dict[str, Any],
    check_status: str,
    user=None,
    uploaded_file=None,
) -> ReceivingDistributionPlan:
    plan.check_payload = check_payload or {}
    plan.check_status = check_status
    plan.checked_at = timezone.now()
    if user and getattr(user, "is_authenticated", False):
        plan.updated_by = user
    if uploaded_file is not None:
        filename = _sanitize_filename(getattr(uploaded_file, "name", "") or "distribution.xlsx")
        plan.source_filename = filename
        if hasattr(uploaded_file, "seek"):
            try:
                uploaded_file.seek(0)
            except Exception:
                pass
        content = uploaded_file.read() if hasattr(uploaded_file, "read") else uploaded_file
        plan.source_file.save(filename, ContentFile(content), save=False)
    plan.save()
    ReceivingDistributionEvent.objects.create(
        plan=plan,
        action="file_checked",
        user=user if getattr(user, "is_authenticated", False) else None,
        new_value={"check_status": check_status, "ok": bool(check_payload.get("ok"))},
        source="client_ui",
    )
    return plan


def save_ui_draft(
    *,
    agency: Agency,
    draft: DistributionDraft,
    user=None,
    client_request_id: str = "",
    check_payload: dict[str, Any] | None = None,
    check_status: str = "",
    uploaded_file=None,
) -> ReceivingDistributionPlan:
    """Создаёт/обновляет черновик PR+план и сохраняет файл/проверку."""
    from django.core.exceptions import ValidationError

    if not draft.items:
        raise ValidationError("Загрузите и проверьте шаблон перед сохранением черновика.")
    result = create_receiving_distribution(
        agency=agency,
        draft=draft,
        user=user,
        submit=False,
        client_request_id=client_request_id,
        allow_incomplete=True,
    )
    plan = result.plan
    meta = draft.meta or {}
    plan.comment = str(meta.get("comment") or plan.comment or "")
    plan.vehicle_number = str(meta.get("vehicle_number") or plan.vehicle_number or "")
    plan.driver_name = str(meta.get("driver_name") or plan.driver_name or "")
    plan.driver_phone = str(meta.get("driver_phone") or plan.driver_phone or "")
    try:
        plan.expected_boxes = int(meta.get("expected_boxes") or plan.expected_boxes or 0)
    except (TypeError, ValueError):
        pass
    try:
        plan.expected_pallets = int(meta.get("expected_pallets") or plan.expected_pallets or 0)
    except (TypeError, ValueError):
        pass
    plan.save()
    if check_payload is not None or uploaded_file is not None:
        attach_check_to_plan(
            plan,
            check_payload=check_payload or plan.check_payload or {},
            check_status=check_status or plan.check_status or "uploaded",
            user=user,
            uploaded_file=uploaded_file,
        )
    logger.info(
        "receiving_distribution draft saved plan=%s agency=%s user=%s",
        plan.receiving_order_id,
        agency.id,
        getattr(user, "id", None),
    )
    ReceivingDistributionEvent.objects.create(
        plan=plan,
        action="draft_saved",
        user=user if getattr(user, "is_authenticated", False) else None,
        new_value={"receiving_order_id": plan.receiving_order_id},
        source="client_ui",
    )
    return plan


def plan_to_form_context(plan: ReceivingDistributionPlan) -> dict[str, Any]:
    return {
        "plan_id": plan.id,
        "receiving_order_id": plan.receiving_order_id,
        "client_request_id": plan.client_request_id,
        "meta": {
            "eta_at": plan.eta_at.isoformat(timespec="minutes") if plan.eta_at else "",
            "expected_boxes": plan.expected_boxes or "",
            "expected_pallets": plan.expected_pallets or "",
            "vehicle_number": plan.vehicle_number,
            "driver_name": plan.driver_name,
            "driver_phone": plan.driver_phone,
            "comment": plan.comment,
        },
        "source_filename": plan.source_filename,
        "check_status": plan.check_status,
        "check_payload": plan.check_payload or {},
        "checked_at": plan.checked_at.isoformat() if plan.checked_at else "",
        "needs_recheck": plan.check_status in {"stale", "uploaded", "errors", ""} or not plan.check_payload,
    }
