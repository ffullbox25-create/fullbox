from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.models import OrderAuditEntry, log_order_action
from employees.models import Employee
from todo.models import Task

from .item_binding import (
    _supplement_pick_tasks_are_done,
    marketplace_binding_mismatch_rows,
    marketplace_binding_snapshot,
    shipping_supplement_fact_is_complete,
    validate_marketplace_item_binding,
)
from .models import ShippingOrder, ShippingOrderItem
from .packing import _shipping_delivered_boxes, _shipping_loose_items
from .truth import ShippingTruthService


STOREKEEPER_ROLES = {"storekeeper", "admin", "director", "developer"}
MANAGER_ROLES = {"manager", "head_manager", "director", "admin", "developer", None}
BOX_CODE_RE = re.compile(r"\b[A-ZА-ЯЁ]{2,5}-\d{4}-\d{6,}-[A-ZА-ЯЁ0-9]+", re.IGNORECASE)
BOX_COUNT_RE = re.compile(r"Коробов:\s*(\d+)", re.IGNORECASE)
BOX_QTY_RE = re.compile(r"кратность:\s*(\d+)", re.IGNORECASE)
TECHNICAL_ACTOR_LABELS = {"admin", "administrator", "администратор", "разработчик", "developer", "system", "система"}
DISCREPANCY_WORKFLOW_VERSION = 2
WORKFLOW_STAGE_AWAITING_SUPPLEMENT_PACKING = "awaiting_supplement_packing"
WORKFLOW_STAGE_RESOLVED = "resolved"
CORRECTION_MODE_WHOLE_BOX = "whole_box"
CORRECTION_MODE_PIECE = "piece"
CORRECTION_MODES = {
    CORRECTION_MODE_WHOLE_BOX,
    CORRECTION_MODE_PIECE,
}


def _to_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _norm(value) -> str:
    return str(value or "").strip()


def _actor_label(user) -> str:
    if not getattr(user, "is_authenticated", False):
        return "-"
    return (user.get_full_name() or user.username or "-").strip()


def _is_technical_actor_label(value) -> bool:
    return _norm(value).lower() in TECHNICAL_ACTOR_LABELS


def _audit_actor_label(entry: OrderAuditEntry) -> str:
    user = getattr(entry, "user", None)
    if user:
        label = _actor_label(user)
        if label and label != "-":
            return label
    payload = entry.payload if isinstance(entry.payload, dict) else {}
    for key in ("approved_by", "actor", "user", "manager"):
        label = _norm(payload.get(key))
        if label:
            return label
    return "-"


def _business_discrepancy_approver(order: ShippingOrder, fallback: str) -> str:
    entries = (
        OrderAuditEntry.objects.filter(
            order_type="shipping",
            order_id=order.number,
            action="shipping_discrepancy_approved",
        )
        .select_related("user")
        .order_by("created_at", "id")
    )
    first_business_actor = ""
    for entry in entries:
        actor = _audit_actor_label(entry)
        if not actor or actor == "-" or _is_technical_actor_label(actor):
            continue
        first_business_actor = actor
        break
    if first_business_actor:
        return first_business_actor
    if fallback and not _is_technical_actor_label(fallback):
        return fallback
    return ""


def _box_codes_from_text(value) -> list[str]:
    text = _norm(value)
    if not text:
        return []
    codes: list[str] = []
    seen: set[str] = set()
    for match in BOX_CODE_RE.findall(text):
        code = _norm(match)
        key = code.lower()
        if code and key not in seen:
            seen.add(key)
            codes.append(code)
    return codes


def _first_int_match(pattern: re.Pattern, value) -> int:
    match = pattern.search(_norm(value))
    if not match:
        return 0
    return _to_int(match.group(1))


def _missing_rows(order: ShippingOrder) -> list[dict]:
    rows: list[dict] = []
    for row in ShippingTruthService.for_order(order).quantity_mismatch_rows:
        missing_qty = _to_int(row.get("missing_qty"))
        if missing_qty <= 0:
            continue
        rows.append(
            {
                "shipping_item_id": _to_int(row.get("shipping_item_id")),
                "sku_code": _norm(row.get("sku_code")),
                "name": _norm(row.get("name")),
                "size": _norm(row.get("size")),
                "barcode": _norm(row.get("barcode")),
                "goods_type": _norm(row.get("goods_type")),
                "requested_qty": _to_int(row.get("requested_qty")),
                "marketplace_qty": _to_int(row.get("requested_qty")),
                "fact_qty": _to_int(row.get("fact_qty")),
                "missing_qty": missing_qty,
                "excess_qty": 0,
                "mismatch_label": _norm(row.get("mismatch_label")),
            }
        )
    return rows


def _marketplace_discrepancy(order: ShippingOrder) -> dict:
    boxes = _shipping_delivered_boxes(order)
    result = validate_marketplace_item_binding(order, boxes=boxes)
    if not result.get("applies"):
        return {}
    rows = marketplace_binding_mismatch_rows(result)
    if not rows:
        return {
            "binding_snapshot": marketplace_binding_snapshot(result),
            "rows": [],
            "kind": str(result.get("kind") or ""),
        }

    items_by_barcode: dict[str, ShippingOrderItem] = {}
    for item in order.items.order_by("id"):
        key = _norm(item.barcode).casefold()
        if key and key not in items_by_barcode:
            items_by_barcode[key] = item
    enriched_rows: list[dict] = []
    for row in rows:
        enriched = dict(row)
        item = items_by_barcode.get(_norm(row.get("barcode")).casefold())
        if item is not None:
            enriched.update(
                {
                    "shipping_item_id": int(item.id or 0),
                    "sku_code": _norm(item.sku_code),
                    "name": _norm(item.name),
                    "goods_type": _norm(item.goods_type),
                    "size": _norm(item.size),
                }
            )
        enriched_rows.append(enriched)
    return {
        "binding_snapshot": marketplace_binding_snapshot(result),
        "rows": enriched_rows,
        "kind": str(result.get("kind") or ""),
    }


def shipping_discrepancy_snapshot(order: ShippingOrder, *, warnings: list[str] | None = None) -> dict:
    truth = ShippingTruthService.for_order(order)
    marketplace = _marketplace_discrepancy(order)
    marketplace_quantity_rows = list(marketplace.get("rows") or [])
    quantity_rows = marketplace_quantity_rows or list(truth.quantity_mismatch_rows)
    missing_rows = [
        dict(row)
        for row in quantity_rows
        if _to_int(row.get("missing_qty")) > 0
    ]
    if not quantity_rows:
        missing_rows = _missing_rows(order)
    expected_boxes = _to_int(order.expected_boxes)
    actual_boxes = len(truth.warehouse_box_codes)
    if marketplace_quantity_rows:
        missing_text = "; ".join(
            (
                f"ШК {row.get('barcode') or '-'}: заявлено "
                f"{_to_int(row.get('marketplace_qty'))} шт, в заявке "
                f"{_to_int(row.get('requested_qty'))} шт, факт OTG "
                f"{_to_int(row.get('fact_qty'))} шт ({row.get('mismatch_label') or 'расхождение'})"
            )
            for row in quantity_rows
        )
        reason = "Расхождение товарного ШК и количества: " + missing_text + "."
    elif quantity_rows:
        missing_text = "; ".join(
            (
                f"ШК {row.get('barcode') or '-'}: в заявке "
                f"{_to_int(row.get('requested_qty'))} шт, факт OTG "
                f"{_to_int(row.get('fact_qty'))} шт "
                f"({row.get('mismatch_label') or 'расхождение'})"
            )
            for row in quantity_rows
        )
        reason = "Расхождение товарного ШК и количества: " + missing_text + "."
    else:
        missing_text = ""
        reason = "Расхождений по товарному ШК и количеству нет."
    return {
        "expected_boxes": expected_boxes,
        "actual_boxes": actual_boxes,
        "missing_rows": missing_rows,
        "quantity_rows": quantity_rows,
        "has_quantity_mismatch": bool(quantity_rows),
        "binding_kind": marketplace.get("kind") or "",
        "binding_snapshot": marketplace.get("binding_snapshot") or {},
        "missing_text": missing_text,
        "reason": reason,
        "arrived_text": f"В OTG фактически привезено {actual_boxes} коробов.",
        "warehouse_box_codes": sorted(truth.warehouse_box_codes),
        "warnings": list(warnings or []),
    }


def shipping_discrepancy_context(order: ShippingOrder, *, warnings: list[str] | None = None) -> dict:
    payload = order.shipping_discrepancy_payload if isinstance(order.shipping_discrepancy_payload, dict) else {}
    workflow_version = _to_int(payload.get("workflow_version"))
    is_v2 = workflow_version >= DISCREPANCY_WORKFLOW_VERSION
    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
    if not snapshot:
        snapshot = shipping_discrepancy_snapshot(order, warnings=warnings)
    status = _norm(order.shipping_discrepancy_status) or _norm(payload.get("status"))
    added_items = payload.get("added_items") if isinstance(payload.get("added_items"), list) else []
    payload_added_box_count = _to_int(payload.get("added_box_count"))
    added_item_summaries: list[str] = []
    added_box_codes: list[str] = []
    for item in added_items:
        if not isinstance(item, dict):
            continue
        name = _norm(item.get("name")) or _norm(item.get("sku_code")) or "Товар"
        sku_code = _norm(item.get("sku_code")) or name
        barcode = _norm(item.get("barcode"))
        goods_type = _norm(item.get("goods_type"))
        qty = _to_int(item.get("qty_requested") or item.get("qty") or item.get("quantity"))
        comment = _norm(item.get("comment"))
        box_count = _first_int_match(BOX_COUNT_RE, comment) or _to_int(item.get("box_count"))
        if not box_count and payload_added_box_count and len(added_items) == 1:
            box_count = payload_added_box_count
        box_qty = _first_int_match(BOX_QTY_RE, comment) or _to_int(item.get("box_qty"))
        if not box_qty and box_count > 0 and qty > 0:
            box_qty = qty // box_count if qty % box_count == 0 else qty
        if not box_count and box_qty > 0 and qty > 0:
            box_count = qty // box_qty if qty % box_qty == 0 else 1
        bits = [sku_code]
        if barcode:
            bits.append(f"ШК {barcode}")
        if goods_type:
            bits.append(f"тип {goods_type}")
        summary = " / ".join(bits)
        if box_count and box_qty and qty:
            summary = f"{summary}: {box_count} короб x {box_qty} шт = {qty} шт"
        elif qty:
            summary = f"{summary}: {qty} шт"
        added_item_summaries.append(summary)
        added_box_codes.extend(_box_codes_from_text(comment))
        box_code = _norm(item.get("box_code"))
        if box_code:
            added_box_codes.append(box_code)
    if not added_box_codes:
        added_box_codes.extend(_box_codes_from_text(payload.get("added_box_codes")))
    added_box_codes = list(dict.fromkeys(added_box_codes))
    missing_text = _norm(payload.get("missing_text")) or _norm(snapshot.get("missing_text"))
    arrived_text = _norm(payload.get("arrived_text")) or _norm(snapshot.get("arrived_text"))
    default_pick_box_count = sum(_to_int(row.get("missing_boxes")) for row in list(snapshot.get("missing_rows") or []))
    additional_pick_move_ids = payload.get("additional_pick_move_ids") or []
    correction_plan = payload.get("correction_plan") if isinstance(payload.get("correction_plan"), dict) else {}
    correction_mode = _norm(payload.get("correction_mode"))
    decision = _norm(payload.get("decision"))
    workflow_stage = _norm(payload.get("workflow_stage"))
    is_awaiting_supplement_packing = bool(
        is_v2
        and decision == "supplement"
        and status == "pickup_required"
        and workflow_stage == WORKFLOW_STAGE_AWAITING_SUPPLEMENT_PACKING
    )
    supplement_packing_qty = _to_int(payload.get("supplement_packing_qty"))
    supplement_fact_complete = False
    if is_v2 and decision == "supplement" and status in {"pickup_required", "resolved"}:
        live_binding_result = validate_marketplace_item_binding(
            order,
            boxes=_shipping_delivered_boxes(order),
        )
        supplement_fact_complete = shipping_supplement_fact_is_complete(
            order,
            live_binding_result,
        )
    pickup_status_label = ""
    if is_awaiting_supplement_packing:
        pickup_status_label = "добор доставлен в OTG, требуется упаковка в короба"
    elif supplement_fact_complete:
        pickup_status_label = "добор подтвержден сканами, состав совпадает"
    elif status == "resolved":
        pickup_status_label = "доставлено в OTG"
    elif additional_pick_move_ids:
        pickup_status_label = "задание ричтраку создано"
    elif status == "pickup_required":
        pickup_status_label = "ожидает добор через ричтрак OTG"
    elif status == "pick_confirmation":
        pickup_status_label = "ожидает подтверждения кладовщиком"
    elif status == "approved":
        pickup_status_label = "согласовано"
    agreed_text = "; ".join(added_item_summaries)
    if not agreed_text:
        if status == "pending":
            agreed_text = (
                "Ожидает решения менеджера или начальника склада"
                if is_v2
                else "Ожидает решения менеджера или клиента"
            )
        elif status == "pick_confirmation":
            agreed_text = (
                "Назначен добор целыми коробами"
                if correction_mode == CORRECTION_MODE_WHOLE_BOX
                else "Назначен поштучный добор"
            )
        elif is_awaiting_supplement_packing:
            agreed_text = "Добор доставлен в OTG и ожидает обязательной упаковки в короба"
        elif supplement_fact_complete:
            agreed_text = "Согласованный добор подтвержден по фактическому ШК и количеству"
        elif status in {"approved", "pickup_required", "resolved"}:
            agreed_text = (
                "Согласована отгрузка по фактическому ШК и количеству"
                if is_v2 and decision == "approve_mismatch"
                else "Согласована отгрузка с расхождением"
            )
    has_history = bool(
        status
        or payload.get("requested_at")
        or payload.get("approved_at")
        or added_items
        or additional_pick_move_ids
    )
    approved_by = _business_discrepancy_approver(order, _norm(payload.get("approved_by")))
    warehouse_status = _norm(getattr(order, "status", "")).lower()
    warehouse_done = warehouse_status in {"packed", "shipped", "partial_shipped"}
    final_box_count = _to_int(payload.get("final_otg_box_count"))
    if status == "pending":
        summary_title = "Расхождение на согласовании"
        client_summary_title = "Расхождение на согласовании"
        client_status_text = "Ожидается решение менеджера."
    elif status == "pick_confirmation":
        summary_title = "Добор ожидает подтверждения кладовщика"
        client_summary_title = "Назначен корректирующий добор"
        client_status_text = "Менеджер назначил способ устранения расхождения."
    elif is_awaiting_supplement_packing:
        summary_title = "Добор доставлен — требуется упаковка"
        client_summary_title = "Добор доставлен на склад"
        client_status_text = "Добранный товар находится в OTG и ожидает упаковки в короба."
    elif supplement_fact_complete:
        summary_title = "Добор подтвержден по факту OTG"
        client_summary_title = "Добор подтвержден по факту OTG"
        client_status_text = "Согласованный состав полностью подтвержден сканами."
    elif status == "resolved" or final_box_count:
        summary_title = "Расхождение закрыто по складу"
        client_summary_title = "Расхождение закрыто по складу"
        client_status_text = "Итоговый складской факт учтён."
    elif status == "pickup_required" and warehouse_done:
        summary_title = "Добор учтён, заявка упакована"
        client_summary_title = "Добор учтён, заявка упакована"
        client_status_text = "Склад учёл замену/добор и сформировал отгрузку."
    elif status == "pickup_required" and additional_pick_move_ids:
        summary_title = "Добор создан"
        client_summary_title = "Добор создан"
        client_status_text = "Создано задание ричтраку на добор по согласованному расхождению."
    elif status == "approved":
        summary_title = "Расхождение согласовано"
        client_summary_title = "Расхождение согласовано"
        client_status_text = "Складской факт согласован менеджером."
    else:
        summary_title = "Складское расхождение"
        client_summary_title = "Складское расхождение"
        client_status_text = "Статус обновляется по факту склада."
    return {
        "status": status,
        "workflow_version": workflow_version,
        "is_v2": is_v2,
        "decision": decision,
        "workflow_stage": workflow_stage,
        "is_awaiting_supplement_packing": is_awaiting_supplement_packing,
        "supplement_packing_qty": supplement_packing_qty,
        "correction_mode": correction_mode,
        "correction_mode_label": (
            "Добор целыми коробами"
            if correction_mode == CORRECTION_MODE_WHOLE_BOX
            else "Поштучный добор"
            if correction_mode == CORRECTION_MODE_PIECE
            else ""
        ),
        "correction_plan": correction_plan,
        "can_offer_correction": bool(
            is_v2
            and any(
                _to_int(row.get("missing_qty")) > 0
                for row in list(snapshot.get("missing_rows") or [])
            )
        ),
        "payload": payload,
        "snapshot": snapshot,
        "has_history": has_history,
        "summary_title": summary_title,
        "client_summary_title": client_summary_title,
        "client_status_text": client_status_text,
        "is_pending": status == "pending",
        "is_pick_confirmation": status == "pick_confirmation",
        "is_pickup_required": status == "pickup_required",
        "is_approved": status == "approved",
        "is_resolved": status == "resolved",
        "supplement_fact_complete": supplement_fact_complete,
        "storekeeper_comment": _norm(payload.get("storekeeper_comment")),
        "requested_by": _norm(payload.get("requested_by")),
        "requested_at": _norm(payload.get("requested_at")),
        "approved_by": approved_by,
        "approved_at": _norm(payload.get("approved_at")),
        "substitution_by": _norm(payload.get("substitution_by")),
        "substitution_at": _norm(payload.get("substitution_at")),
        "missing_text": missing_text,
        "arrived_text": arrived_text,
        "agreed_text": agreed_text,
        "added_box_codes": added_box_codes,
        "additional_pick_move_ids": additional_pick_move_ids,
        "pickup_status_label": pickup_status_label,
        "default_pick_box_count": default_pick_box_count,
        "ready_for_palletization": (
            status == "resolved"
            or bool(payload.get("final_otg_box_count"))
            or bool(is_v2 and status == "approved" and decision == "approve_mismatch")
            or supplement_fact_complete
        ),
    }


def _completed_supplement_state(order: ShippingOrder) -> dict:
    payload = order.shipping_discrepancy_payload if isinstance(order.shipping_discrepancy_payload, dict) else {}
    status = _norm(order.shipping_discrepancy_status) or _norm(payload.get("status"))
    if (
        _to_int(payload.get("workflow_version")) < DISCREPANCY_WORKFLOW_VERSION
        or status not in {"pickup_required", "resolved"}
        or _norm(payload.get("decision")) != "supplement"
    ):
        return {}

    delivered_boxes = _shipping_delivered_boxes(order)
    binding_result = validate_marketplace_item_binding(
        order,
        boxes=delivered_boxes,
    )
    if not shipping_supplement_fact_is_complete(order, binding_result):
        return {}

    final_box_count = len(delivered_boxes)
    if final_box_count <= 0:
        final_box_count = len(ShippingTruthService.for_order(order).warehouse_box_codes)
    if final_box_count <= 0:
        return {}

    return {
        "final_box_count": final_box_count,
        "binding_snapshot": marketplace_binding_snapshot(binding_result),
    }


@transaction.atomic
def resolve_completed_discrepancy_supplement(order: ShippingOrder, *, user=None) -> dict:
    order = ShippingOrder.objects.select_for_update().get(pk=order.pk)
    state = _completed_supplement_state(order)
    if not state:
        return {}

    payload = order.shipping_discrepancy_payload if isinstance(order.shipping_discrepancy_payload, dict) else {}
    payload = dict(payload)
    final_box_count = _to_int(state.get("final_box_count"))
    already_resolved = (
        _norm(order.shipping_discrepancy_status) == "resolved"
        and _to_int(payload.get("final_otg_box_count")) == final_box_count
        and _to_int(order.expected_boxes) == final_box_count
    )
    if already_resolved:
        _close_supplement_packing_task(order)
        return payload

    payload.setdefault("old_expected_boxes", _to_int(order.expected_boxes))
    payload["status"] = "resolved"
    payload["resolved_at"] = timezone.localtime().isoformat()
    payload["resolved_by"] = _actor_label(user)
    payload["resolution_reason"] = "supplement_fact_complete"
    payload["final_otg_box_count"] = final_box_count
    payload["new_expected_boxes"] = final_box_count
    payload["resolved_binding_snapshot"] = state.get("binding_snapshot") or {}
    if _norm(payload.get("workflow_stage")) == WORKFLOW_STAGE_AWAITING_SUPPLEMENT_PACKING:
        payload["workflow_stage"] = WORKFLOW_STAGE_RESOLVED
        payload["supplement_packing_completed_at"] = timezone.localtime().isoformat()
        payload["supplement_packing_completed_by"] = _actor_label(user)

    order.expected_boxes = final_box_count
    order.shipping_discrepancy_status = "resolved"
    order.shipping_discrepancy_payload = payload
    order.save(
        update_fields=[
            "expected_boxes",
            "shipping_discrepancy_status",
            "shipping_discrepancy_payload",
            "updated_at",
        ]
    )
    _close_discrepancy_decision_tasks(order)
    _close_discrepancy_storekeeper_task(order)
    _close_supplement_packing_task(order)
    _log_discrepancy(
        order,
        action="ship_disc_supp_resolved",
        user=user,
        description="Согласованный добор подтвержден фактом OTG.",
        payload=payload,
    )
    return payload


def can_request_shipping_discrepancy(scope: str | None, role: str | None, order: ShippingOrder, warnings: list[str]) -> bool:
    if order.is_closed() or _norm(order.shipping_discrepancy_status):
        return False
    if scope != "staff" or role not in STOREKEEPER_ROLES:
        return False
    if _shipping_loose_items(order):
        return False
    snapshot = shipping_discrepancy_snapshot(order, warnings=warnings)
    return bool(
        (warnings or snapshot.get("has_quantity_mismatch"))
        and _to_int(snapshot.get("actual_boxes")) > 0
    )


def can_approve_shipping_discrepancy(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    if order.is_closed() or order.shipping_discrepancy_status not in {"pending", "pickup_required"}:
        return False
    payload = order.shipping_discrepancy_payload if isinstance(order.shipping_discrepancy_payload, dict) else {}
    if _to_int(payload.get("workflow_version")) >= DISCREPANCY_WORKFLOW_VERSION:
        return (
            order.shipping_discrepancy_status == "pending"
            and scope == "staff"
            and role in MANAGER_ROLES
        )
    if scope == "client":
        return True
    return scope == "staff" and role in MANAGER_ROLES


def can_add_discrepancy_item(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    payload = order.shipping_discrepancy_payload if isinstance(order.shipping_discrepancy_payload, dict) else {}
    if _to_int(payload.get("workflow_version")) >= DISCREPANCY_WORKFLOW_VERSION:
        return False
    return can_approve_shipping_discrepancy(scope, role, order)


def can_confirm_shipping_discrepancy_pick(
    scope: str | None,
    role: str | None,
    order: ShippingOrder,
) -> bool:
    payload = order.shipping_discrepancy_payload if isinstance(order.shipping_discrepancy_payload, dict) else {}
    return bool(
        not order.is_closed()
        and scope == "staff"
        and role in STOREKEEPER_ROLES
        and order.shipping_discrepancy_status == "pick_confirmation"
        and _to_int(payload.get("workflow_version")) >= DISCREPANCY_WORKFLOW_VERSION
        and _norm(payload.get("correction_mode")) in CORRECTION_MODES
    )


def _log_discrepancy(order: ShippingOrder, *, action: str, user, description: str, payload: dict) -> None:
    log_order_action(
        action=action,
        order_id=order.number,
        order_type="shipping",
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=order.agency,
        description=description,
        payload=payload,
    )


def _decision_employees() -> list[Employee]:
    employees: list[Employee] = []
    for role in ("manager", "head_manager"):
        employee = (
            Employee.objects.filter(role=role, is_active=True, user__isnull=False)
            .order_by("full_name", "id")
            .first()
        )
        if employee is not None:
            employees.append(employee)
    if employees:
        return employees
    fallback = (
        Employee.objects.filter(
            role__in=["director", "admin"],
            is_active=True,
            user__isnull=False,
        )
        .order_by("full_name", "id")
        .first()
    )
    return [fallback] if fallback is not None else []


def _ensure_discrepancy_manager_task(order: ShippingOrder, *, user, payload: dict) -> None:
    employees = _decision_employees()
    if not employees:
        return
    route = f"/shipping/{order.pk}/"
    description = "\n".join(
        [
            f"Заявка: {order.number}",
            f"Клиент: {getattr(order.agency, 'agn_name', '') or order.agency_id}",
            f"Причина: {payload.get('reason') or '-'}",
            f"Комментарий кладовщика: {payload.get('storekeeper_comment') or '-'}",
        ]
    )
    for employee in employees:
        title = (
            f"СРОЧНО: согласовать расхождение {order.number} "
            f"[{employee.role}]"
        )
        existing = (
            Task.objects.filter(route=route, title=title)
            .exclude(status="done")
            .order_by("-created_at")
            .first()
        )
        if existing:
            existing.description = description
            existing.assigned_to = employee
            existing.priority = "urgent"
            existing.status = "in_progress"
            existing.due_date = timezone.localtime()
            existing.save(
                update_fields=[
                    "description",
                    "assigned_to",
                    "priority",
                    "status",
                    "due_date",
                    "updated_at",
                ]
            )
            continue
        Task.objects.create(
            title=title,
            description=description,
            route=route,
            assigned_to=employee,
            created_by=user if getattr(user, "is_authenticated", False) else None,
            status="in_progress",
            priority="urgent",
            due_date=timezone.localtime(),
        )


def _close_discrepancy_decision_tasks(order: ShippingOrder) -> None:
    Task.objects.filter(
        route=f"/shipping/{order.pk}/",
        title__icontains="согласовать",
    ).filter(
        title__icontains="расхожд"
    ).exclude(status="done").update(status="done")


@transaction.atomic
def attach_operational_supplement_to_pending_discrepancy(
    *,
    order: ShippingOrder,
    user,
    move_ids: list[str],
    preview: dict,
) -> dict:
    """Replace an identical pending shortage with the guarded OTG supplement."""

    order = ShippingOrder.objects.select_for_update().get(pk=order.pk)
    status = _norm(order.shipping_discrepancy_status)
    if not status:
        return {}
    payload = (
        dict(order.shipping_discrepancy_payload)
        if isinstance(order.shipping_discrepancy_payload, dict)
        else {}
    )
    if (
        status != "pending"
        or _to_int(payload.get("workflow_version")) < DISCREPANCY_WORKFLOW_VERSION
    ):
        raise ValidationError(
            "Текущее решение по расхождению несовместимо с автоматическим добором. "
            "Новое задание не создано."
        )

    live_snapshot = _assert_v2_snapshot_unchanged(order, payload)
    missing_by_item_id: dict[int, int] = {}
    for row in live_snapshot.get("missing_rows") or []:
        item_id = _to_int(row.get("shipping_item_id"))
        missing_qty = max(_to_int(row.get("missing_qty")), 0)
        if item_id > 0 and missing_qty > 0:
            missing_by_item_id[item_id] = (
                missing_by_item_id.get(item_id, 0) + missing_qty
            )
    requested_by_item_id = {
        _to_int(item_id): max(_to_int(qty), 0)
        for item_id, qty in dict(
            (preview or {}).get("requested_qty_by_item_id") or {}
        ).items()
        if _to_int(item_id) > 0 and _to_int(qty) > 0
    }
    if not requested_by_item_id or requested_by_item_id != missing_by_item_id:
        raise ValidationError(
            "Добор не совпадает с ожидающим расхождением по строкам и количеству. "
            "Новое задание не создано."
        )

    normalized_move_ids = list(
        dict.fromkeys(_norm(move_id) for move_id in move_ids if _norm(move_id))
    )
    if not normalized_move_ids:
        raise ValidationError("Ричтраку не создано ни одного задания.")

    correction_mode = (
        CORRECTION_MODE_PIECE
        if _norm((preview or {}).get("correction_mode")) == CORRECTION_MODE_PIECE
        else CORRECTION_MODE_WHOLE_BOX
    )
    correction_plan = {
        "can_create": True,
        "correction_mode": correction_mode,
        "requested_qty": max(_to_int((preview or {}).get("requested_qty")), 0),
        "source_box_count": max(
            _to_int((preview or {}).get("requested_boxes")),
            0,
        ),
        "plan_rows": list((preview or {}).get("plan_rows") or []),
    }
    now = timezone.localtime().isoformat()
    payload.update(
        {
            "status": "pickup_required",
            "decision": "supplement",
            "correction_mode": correction_mode,
            "correction_plan": correction_plan,
            "decision_at": now,
            "decision_by": _actor_label(user),
            "decision_source": "confirmed_no_stock_operational_supplement",
            "additional_pick_move_ids": normalized_move_ids,
            "additional_pick_created_at": now,
            "additional_pick_created_by": _actor_label(user),
            "operational_supplement_source_request_id": _to_int(
                (preview or {}).get("source_request_id")
            ),
        }
    )
    order.shipping_discrepancy_status = "pickup_required"
    order.shipping_discrepancy_payload = payload
    order.save(
        update_fields=[
            "shipping_discrepancy_status",
            "shipping_discrepancy_payload",
            "updated_at",
        ]
    )
    _close_discrepancy_decision_tasks(order)
    _log_discrepancy(
        order,
        action="ship_disc_operational_supp",
        user=user,
        description=(
            "Ожидающее расхождение заменено идентичным добором после "
            "подтвержденного отсутствия товара на исходной паллете."
        ),
        payload=payload,
    )
    return payload


def _ensure_discrepancy_storekeeper_task(order: ShippingOrder, *, user, payload: dict) -> Task:
    storekeeper = (
        Employee.objects.filter(
            role="storekeeper",
            is_active=True,
            user__isnull=False,
        )
        .order_by("full_name", "id")
        .first()
    )
    if storekeeper is None:
        raise ValidationError(
            "Не найден активный кладовщик. Добор не назначен, складские данные не изменены."
        )

    route = f"/shipping/{order.pk}/"
    title = f"СРОЧНО: подтвердить добор {order.number}"
    correction_mode = _norm(payload.get("correction_mode"))
    mode_label = "целыми коробами" if correction_mode == CORRECTION_MODE_WHOLE_BOX else "поштучно"
    correction_plan = payload.get("correction_plan") if isinstance(payload.get("correction_plan"), dict) else {}
    description = "\n".join(
        [
            f"Заявка: {order.number}",
            f"Клиент: {getattr(order.agency, 'agn_name', '') or order.agency_id}",
            f"Режим добора: {mode_label}",
            f"Количество к добору: {_to_int(correction_plan.get('requested_qty'))} шт.",
            "Откройте заявку и подтвердите задание. До подтверждения остатки и резервы не меняются.",
        ]
    )
    existing = (
        Task.objects.filter(route=route, title=title)
        .exclude(status="done")
        .order_by("-created_at", "-id")
        .first()
    )
    if existing:
        existing.description = description
        existing.assigned_to = storekeeper
        existing.priority = "urgent"
        existing.status = "in_progress"
        existing.due_date = timezone.localtime()
        existing.save(
            update_fields=[
                "description",
                "assigned_to",
                "priority",
                "status",
                "due_date",
                "updated_at",
            ]
        )
        return existing
    return Task.objects.create(
        title=title,
        description=description,
        route=route,
        assigned_to=storekeeper,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        status="in_progress",
        priority="urgent",
        due_date=timezone.localtime(),
    )


def _close_discrepancy_storekeeper_task(order: ShippingOrder) -> None:
    Task.objects.filter(
        route=f"/shipping/{order.pk}/",
        title=f"СРОЧНО: подтвердить добор {order.number}",
    ).exclude(status="done").update(status="done")


def _supplement_packing_task_title(order: ShippingOrder) -> str:
    return f"СРОЧНО: упаковать добор {order.number}"


def _supplement_packing_task_route(order: ShippingOrder) -> str:
    return f"/shipping/{order.pk}/packing/loose/"


def _ensure_supplement_packing_task(
    order: ShippingOrder,
    *,
    user,
    quantity: int,
) -> Task:
    storekeeper = (
        Employee.objects.filter(
            role="storekeeper",
            is_active=True,
            user__isnull=False,
        )
        .order_by("full_name", "id")
        .first()
    )
    if storekeeper is None:
        storekeeper = (
            Employee.objects.filter(role="storekeeper", is_active=True)
            .order_by("full_name", "id")
            .first()
        )
    route = _supplement_packing_task_route(order)
    title = _supplement_packing_task_title(order)
    description = "\n".join(
        [
            f"Заявка: {order.number}",
            f"Клиент: {getattr(order.agency, 'agn_name', '') or order.agency_id}",
            f"Количество к добору: {max(_to_int(quantity), 0)} шт.",
            "Добор доставлен в OTG. Упакуйте товар в новые короба; без упаковки расхождение не закроется.",
        ]
    )
    existing = (
        Task.objects.filter(route=route, title=title)
        .exclude(status="done")
        .order_by("-created_at", "-id")
        .first()
    )
    if existing:
        existing.description = description
        if storekeeper is not None:
            existing.assigned_to = storekeeper
        existing.priority = "urgent"
        existing.status = "in_progress"
        existing.due_date = timezone.localtime()
        update_fields = [
            "description",
            "priority",
            "status",
            "due_date",
            "updated_at",
        ]
        if storekeeper is not None:
            update_fields.append("assigned_to")
        existing.save(update_fields=update_fields)
        return existing
    return Task.objects.create(
        title=title,
        description=description,
        route=route,
        assigned_to=storekeeper,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        status="in_progress",
        priority="urgent",
        due_date=timezone.localtime(),
    )


def _close_supplement_packing_task(order: ShippingOrder) -> None:
    Task.objects.filter(
        route=_supplement_packing_task_route(order),
        title=_supplement_packing_task_title(order),
    ).exclude(status="done").update(status="done")


def close_loose_packing_task(order: ShippingOrder) -> None:
    """Close the operational packing task after loose stock was packed."""

    _close_supplement_packing_task(order)


@transaction.atomic
def mark_supplement_awaiting_packing(
    *,
    completed_move_id: str,
    order_pk: int | None = None,
    order_number: str = "",
    user=None,
) -> dict:
    move_id = _norm(completed_move_id)
    if not move_id:
        return {}
    from django.db.models import Q
    from reachtruck.models import MoveTask

    move_lookup = Q(legacy_order_id=move_id)
    if _to_int(move_id) > 0:
        move_lookup |= Q(pk=_to_int(move_id))
    if not MoveTask.objects.filter(
        move_lookup,
        status=MoveTask.STATUS_DONE,
        request__destination_zone="OTG",
    ).exists():
        return {}
    orders = ShippingOrder.objects.select_for_update().select_related("agency")
    order = orders.filter(pk=order_pk).first() if _to_int(order_pk) > 0 else None
    if order is None and _norm(order_number):
        order = orders.filter(number=_norm(order_number)).first()
    if order is None:
        return {}

    loose_items = _shipping_loose_items(order)
    quantity = sum(max(_to_int(item.get("qty")), 0) for item in loose_items)
    if quantity <= 0:
        return {}

    packing_task_existed = Task.objects.filter(
        route=_supplement_packing_task_route(order),
        title=_supplement_packing_task_title(order),
    ).exclude(status="done").exists()
    task = _ensure_supplement_packing_task(
        order,
        user=user,
        quantity=quantity,
    )
    if not packing_task_existed:
        _log_discrepancy(
            order,
            action="shipping_loose_packing_required",
            user=user,
            description=(
                f"В OTG доставлен товар без постоянного короба — {quantity} шт. "
                "Кладовщику создана срочная задача упаковки."
            ),
            payload={
                "loose_packing_qty": quantity,
                "loose_packing_task_id": task.id,
                "completed_move_id": move_id,
            },
        )
    payload = order.shipping_discrepancy_payload if isinstance(order.shipping_discrepancy_payload, dict) else {}
    payload = dict(payload)
    expected_move_ids = {
        _norm(value)
        for value in payload.get("additional_pick_move_ids") or []
        if _norm(value)
    }
    if (
        _to_int(payload.get("workflow_version")) < DISCREPANCY_WORKFLOW_VERSION
        or _norm(payload.get("decision")) != "supplement"
        or _norm(order.shipping_discrepancy_status) != "pickup_required"
        or move_id not in expected_move_ids
        or not _supplement_pick_tasks_are_done(order, payload)
    ):
        return {
            "loose_packing_required": True,
            "loose_packing_qty": quantity,
            "loose_packing_task_id": task.id,
        }

    first_transition = _norm(payload.get("workflow_stage")) != WORKFLOW_STAGE_AWAITING_SUPPLEMENT_PACKING
    payload["workflow_stage"] = WORKFLOW_STAGE_AWAITING_SUPPLEMENT_PACKING
    payload["workflow_stage_updated_at"] = timezone.localtime().isoformat()
    payload["workflow_stage_updated_by"] = _actor_label(user)
    payload["supplement_packing_qty"] = quantity
    payload["supplement_packing_task_id"] = task.id
    payload["supplement_packing_trigger_move_id"] = move_id
    order.shipping_discrepancy_payload = payload
    order.save(update_fields=["shipping_discrepancy_payload", "updated_at"])
    if first_transition:
        _log_discrepancy(
            order,
            action="ship_disc_supp_awaiting_packing",
            user=user,
            description=(
                "Согласованный добор доставлен в OTG и ожидает обязательной "
                "упаковки в короба."
            ),
            payload=payload,
        )
    return payload


@transaction.atomic
def request_shipping_discrepancy(
    order: ShippingOrder,
    *,
    user,
    storekeeper_comment: str,
    warnings: list[str] | None = None,
) -> dict:
    order = ShippingOrder.objects.select_for_update().get(pk=order.pk)
    if order.is_closed() or _norm(order.shipping_discrepancy_status):
        raise ValidationError("По заявке уже есть решение или активное согласование расхождения.")
    loose_items = _shipping_loose_items(order)
    loose_qty = sum(max(_to_int(item.get("qty")), 0) for item in loose_items)
    if loose_qty > 0:
        raise ValidationError(
            f"Сначала упакуйте {loose_qty} шт. товара без короба в постоянные FBS-короба. "
            "После упаковки проверка состава запустится по фактическим коробам."
        )
    comment = _norm(storekeeper_comment) or "Комментарий кладовщика не указан."
    snapshot = shipping_discrepancy_snapshot(order, warnings=warnings)
    if _to_int(snapshot.get("actual_boxes")) <= 0 or not (
        warnings
        or snapshot.get("missing_rows")
        or snapshot.get("has_quantity_mismatch")
    ):
        raise ValidationError("Нет расхождения, которое можно отправить на согласование.")
    payload = {
        "workflow_version": DISCREPANCY_WORKFLOW_VERSION,
        "status": "pending",
        "requested_at": timezone.localtime().isoformat(),
        "requested_by": _actor_label(user),
        "storekeeper_comment": comment,
        "reason": snapshot["reason"],
        "missing_text": snapshot["missing_text"],
        "arrived_text": snapshot["arrived_text"],
        "snapshot": snapshot,
        "binding_snapshot": snapshot.get("binding_snapshot") or {},
    }
    order.shipping_discrepancy_status = "pending"
    order.shipping_discrepancy_payload = payload
    order.save(update_fields=["shipping_discrepancy_status", "shipping_discrepancy_payload", "updated_at"])
    _ensure_discrepancy_manager_task(order, user=user, payload=payload)
    _log_discrepancy(
        order,
        action="shipping_discrepancy_requested",
        user=user,
        description="Отгрузка с расхождением отправлена на согласование.",
        payload=payload,
    )
    return payload


def _assert_v2_snapshot_unchanged(order: ShippingOrder, payload: dict) -> dict:
    live_snapshot = shipping_discrepancy_snapshot(order)
    stored_binding = payload.get("binding_snapshot")
    live_binding = live_snapshot.get("binding_snapshot")
    if isinstance(stored_binding, dict) and stored_binding:
        if stored_binding != live_binding:
            raise ValidationError(
                "Факт OTG изменился после запроса. Решение заблокировано; "
                "нужен повторный разбор расхождения."
            )
    else:
        stored_snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
        stored_codes = sorted(str(code or "") for code in stored_snapshot.get("warehouse_box_codes") or [])
        live_codes = sorted(str(code or "") for code in live_snapshot.get("warehouse_box_codes") or [])
        if (
            stored_codes != live_codes
            or _norm(stored_snapshot.get("missing_text"))
            != _norm(live_snapshot.get("missing_text"))
        ):
            raise ValidationError(
                "Факт OTG изменился после запроса. Решение заблокировано; "
                "нужен повторный разбор расхождения."
            )
    return live_snapshot


@transaction.atomic
def approve_shipping_discrepancy(order: ShippingOrder, *, user) -> dict:
    order = ShippingOrder.objects.select_for_update().get(pk=order.pk)
    current_status = str(order.shipping_discrepancy_status or "").strip()
    if current_status not in {"pending", "pickup_required", "approved"}:
        raise ValidationError("По заявке нет активного согласования расхождения.")
    payload = order.shipping_discrepancy_payload if isinstance(order.shipping_discrepancy_payload, dict) else {}
    payload = dict(payload)
    if _to_int(payload.get("workflow_version")) >= DISCREPANCY_WORKFLOW_VERSION:
        if current_status != "pending":
            raise ValidationError("Решение по расхождению уже принято.")
        live_snapshot = _assert_v2_snapshot_unchanged(order, payload)
        payload["status"] = "approved"
        payload["decision"] = "approve_mismatch"
        payload["approved_at"] = timezone.localtime().isoformat()
        payload["approved_by"] = _actor_label(user)
        payload["final_otg_box_count"] = _to_int(live_snapshot.get("actual_boxes"))
        order.shipping_discrepancy_status = "approved"
        order.shipping_discrepancy_payload = payload
        order.save(
            update_fields=[
                "shipping_discrepancy_status",
                "shipping_discrepancy_payload",
                "updated_at",
            ]
        )
        _close_discrepancy_decision_tasks(order)
        _log_discrepancy(
            order,
            action="shipping_discrepancy_approved",
            user=user,
            description=(
                "Расхождение согласовано по фактическому ШК и количеству "
                "без изменения исходной заявки."
            ),
            payload=payload,
        )
        return payload
    should_reduce_to_fact = current_status == "pending" and not payload.get("added_items")
    if should_reduce_to_fact:
        snapshot = shipping_discrepancy_snapshot(order)
        missing_rows = list(snapshot.get("missing_rows") or [])
        old_expected_boxes = _to_int(order.expected_boxes)
        reduced_boxes, reduced_qty = _reduce_missing_items(order, missing_rows)
        order.expected_boxes = max(old_expected_boxes - reduced_boxes, 0)
        payload["as_is_finalized_at"] = timezone.localtime().isoformat()
        payload["old_expected_boxes"] = old_expected_boxes
        payload["new_expected_boxes"] = _to_int(order.expected_boxes)
        payload["removed_missing_boxes"] = reduced_boxes
        payload["removed_missing_qty"] = reduced_qty
    # Актуальный факт OTG — чтобы UI показал готовность к паллетизации.
    live_snapshot = shipping_discrepancy_snapshot(order)
    payload["final_otg_box_count"] = _to_int(live_snapshot.get("actual_boxes"))
    payload["status"] = "approved"
    payload["approved_at"] = timezone.localtime().isoformat()
    payload["approved_by"] = _actor_label(user)
    order.shipping_discrepancy_status = "approved"
    order.shipping_discrepancy_payload = payload
    order.save(update_fields=["expected_boxes", "shipping_discrepancy_status", "shipping_discrepancy_payload", "updated_at"])
    _close_discrepancy_decision_tasks(order)
    _log_discrepancy(
        order,
        action="shipping_discrepancy_approved",
        user=user,
        description="Отгрузка с расхождением согласована.",
        payload=payload,
    )
    return payload


@transaction.atomic
def reject_shipping_discrepancy(
    order: ShippingOrder,
    *,
    user,
    correction_mode: str,
) -> dict:
    mode = _norm(correction_mode)
    if mode not in CORRECTION_MODES:
        raise ValidationError("Выберите добор целыми коробами или поштучный добор.")
    order = ShippingOrder.objects.select_for_update().get(pk=order.pk)
    payload = order.shipping_discrepancy_payload if isinstance(order.shipping_discrepancy_payload, dict) else {}
    payload = dict(payload)
    if (
        order.shipping_discrepancy_status != "pending"
        or _to_int(payload.get("workflow_version")) < DISCREPANCY_WORKFLOW_VERSION
    ):
        raise ValidationError("Решение по расхождению уже принято или запрос устарел.")
    live_snapshot = _assert_v2_snapshot_unchanged(order, payload)
    missing_rows = [
        dict(row)
        for row in live_snapshot.get("missing_rows") or []
        if _to_int(row.get("missing_qty")) > 0
    ]
    if not missing_rows:
        raise ValidationError(
            "Для расхождения нет положительной недостачи. Проверьте лишний или неверный товар в OTG."
        )

    from otg_reachtruck.services import get_otg_discrepancy_pick_preview

    correction_plan = get_otg_discrepancy_pick_preview(
        order=order,
        shortage_rows=missing_rows,
        correction_mode=mode,
    )
    if not correction_plan.get("can_create"):
        raise ValidationError(
            str(correction_plan.get("reason") or "Не удалось построить безопасный план добора.")
        )

    payload["status"] = "pick_confirmation"
    payload["decision"] = "supplement"
    payload["correction_mode"] = mode
    payload["correction_plan"] = correction_plan
    payload["decision_at"] = timezone.localtime().isoformat()
    payload["decision_by"] = _actor_label(user)
    order.shipping_discrepancy_status = "pick_confirmation"
    order.shipping_discrepancy_payload = payload
    order.save(
        update_fields=[
            "shipping_discrepancy_status",
            "shipping_discrepancy_payload",
            "updated_at",
        ]
    )
    _close_discrepancy_decision_tasks(order)
    _ensure_discrepancy_storekeeper_task(order, user=user, payload=payload)
    _log_discrepancy(
        order,
        action="shipping_discrepancy_correction",
        user=user,
        description=(
            "По расхождению назначен добор без изменения исходной заявки "
            "и складских остатков."
        ),
        payload=payload,
    )
    return payload


@transaction.atomic
def confirm_shipping_discrepancy_pick(
    order: ShippingOrder,
    *,
    user,
    requested_by_role: str,
) -> dict:
    order = ShippingOrder.objects.select_for_update().get(pk=order.pk)
    payload = order.shipping_discrepancy_payload if isinstance(order.shipping_discrepancy_payload, dict) else {}
    payload = dict(payload)
    if (
        order.shipping_discrepancy_status != "pick_confirmation"
        or _to_int(payload.get("workflow_version")) < DISCREPANCY_WORKFLOW_VERSION
    ):
        raise ValidationError("Нет добора, ожидающего подтверждения кладовщиком.")
    correction_mode = _norm(payload.get("correction_mode"))
    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
    missing_rows = [
        dict(row)
        for row in snapshot.get("missing_rows") or []
        if _to_int(row.get("missing_qty")) > 0
    ]
    if not missing_rows:
        raise ValidationError("В согласованном решении не найдена недостача для добора.")

    from otg_reachtruck.services import (
        create_otg_shipping_discrepancy_pick,
        get_otg_discrepancy_pick_preview,
    )

    current_plan = get_otg_discrepancy_pick_preview(
        order=order,
        shortage_rows=missing_rows,
        correction_mode=correction_mode,
    )
    if not current_plan.get("can_create"):
        raise ValidationError(
            str(current_plan.get("reason") or "Не удалось построить безопасный план добора.")
        )

    expected_plan = payload.get("correction_plan")
    expected_plan = expected_plan if isinstance(expected_plan, dict) else {}
    expected_fingerprint = _norm(expected_plan.get("plan_fingerprint"))
    current_fingerprint = _norm(current_plan.get("plan_fingerprint"))
    if expected_fingerprint and current_fingerprint and expected_fingerprint != current_fingerprint:
        refreshed_at = timezone.localtime().isoformat()
        payload["correction_plan"] = current_plan
        payload["correction_plan_refreshed_at"] = refreshed_at
        payload["correction_plan_refreshed_by"] = _actor_label(user)
        payload["correction_plan_previous_fingerprint"] = expected_fingerprint
        payload["correction_plan_current_fingerprint"] = current_fingerprint
        order.shipping_discrepancy_payload = payload
        order.save(update_fields=["shipping_discrepancy_payload", "updated_at"])
        _log_discrepancy(
            order,
            action="shipping_disc_plan_refreshed",
            user=user,
            description=(
                "План корректирующего добора обновлён по текущему складу. "
                "Задание ричтраку не создано; требуется повторное подтверждение кладовщика."
            ),
            payload=payload,
        )
        response_payload = dict(payload)
        response_payload["plan_refreshed"] = True
        return response_payload

    _move_request, move_ids, current_plan = create_otg_shipping_discrepancy_pick(
        order=order,
        shortage_rows=missing_rows,
        correction_mode=correction_mode,
        user=user,
        requested_by_name=_actor_label(user),
        requested_by_role=requested_by_role,
        expected_plan=expected_plan,
    )
    if not move_ids:
        raise ValidationError("Ричтраку не создано ни одного задания. Остатки не изменены.")
    payload["status"] = "pickup_required"
    payload["additional_pick_move_ids"] = move_ids
    payload["additional_pick_created_at"] = timezone.localtime().isoformat()
    payload["additional_pick_created_by"] = _actor_label(user)
    payload["correction_plan"] = current_plan
    order.shipping_discrepancy_status = "pickup_required"
    order.shipping_discrepancy_payload = payload
    order.save(
        update_fields=[
            "shipping_discrepancy_status",
            "shipping_discrepancy_payload",
            "updated_at",
        ]
    )
    _close_discrepancy_storekeeper_task(order)
    _log_discrepancy(
        order,
        action="shipping_disc_pick_confirmed",
        user=user,
        description=(
            "Кладовщик подтвердил корректирующий добор. "
            "Факт появится только после сканов ричтрака."
        ),
        payload=payload,
    )
    return payload


def _reduce_missing_items(order: ShippingOrder, missing_rows: list[dict]) -> tuple[int, int]:
    reduced_boxes = 0
    reduced_qty = 0
    for row in missing_rows:
        remaining_qty = _to_int(row.get("missing_qty"))
        if remaining_qty <= 0:
            continue
        items = list(
            order.items.filter(
                barcode__iexact=_norm(row.get("barcode")),
                goods_type__iexact=_norm(row.get("goods_type")),
            ).order_by("-id")
        )
        for item in items:
            if remaining_qty <= 0:
                break
            current_qty = _to_int(item.qty_requested)
            if current_qty <= 0:
                continue
            take_qty = min(current_qty, remaining_qty)
            remaining_qty -= take_qty
            reduced_qty += take_qty
            item.qty_requested = max(current_qty - take_qty, 0)
            item.qty_reserved = min(_to_int(item.qty_reserved), item.qty_requested)
            if item.qty_requested <= 0:
                item.delete()
            else:
                item.save(update_fields=["qty_requested", "qty_reserved", "updated_at"])
        reduced_boxes += _to_int(row.get("missing_boxes"))
    return reduced_boxes, reduced_qty


def apply_discrepancy_substitution(order: ShippingOrder, *, selected_items: list[dict], added_box_count: int, user) -> dict:
    if order.shipping_discrepancy_status not in {"pending", "pickup_required"}:
        raise ValidationError("Сначала отправьте расхождение на согласование.")
    current_payload = (
        order.shipping_discrepancy_payload
        if isinstance(order.shipping_discrepancy_payload, dict)
        else {}
    )
    if _to_int(current_payload.get("workflow_version")) >= DISCREPANCY_WORKFLOW_VERSION:
        raise ValidationError(
            "Для нового процесса добор назначается решением менеджера "
            "и подтверждается кладовщиком без изменения заявки."
        )
    if not selected_items or added_box_count <= 0:
        raise ValidationError("Выберите товар для добора целыми коробами.")
    snapshot = shipping_discrepancy_snapshot(order)
    missing_rows = list(snapshot.get("missing_rows") or [])
    if not missing_rows:
        raise ValidationError("Не найден недостающий товар для замены.")
    reduced_boxes, reduced_qty = _reduce_missing_items(order, missing_rows)
    created_items: list[dict] = []
    for row in selected_items:
        item = ShippingOrderItem(**row)
        item.order = order
        item.save()
        created_items.append(
            {
                "item_id": item.id,
                "sku_code": item.sku_code,
                "name": item.name,
                "barcode": item.barcode,
                "goods_type": item.goods_type,
                "qty_requested": item.qty_requested,
                "box_count": _first_int_match(BOX_COUNT_RE, item.comment),
                "box_qty": _first_int_match(BOX_QTY_RE, item.comment),
                "box_codes": _box_codes_from_text(item.comment),
                "comment": item.comment,
            }
        )
    order.expected_boxes = max(_to_int(order.expected_boxes) - reduced_boxes + _to_int(added_box_count), 0)
    payload = order.shipping_discrepancy_payload if isinstance(order.shipping_discrepancy_payload, dict) else {}
    payload = dict(payload)
    payload["status"] = "pickup_required"
    payload["substitution_at"] = timezone.localtime().isoformat()
    payload["substitution_by"] = _actor_label(user)
    payload["removed_missing_boxes"] = reduced_boxes
    payload["removed_missing_qty"] = reduced_qty
    payload["added_box_count"] = _to_int(added_box_count)
    payload["added_items"] = created_items
    order.shipping_discrepancy_status = "pickup_required"
    order.shipping_discrepancy_payload = payload
    order.save(update_fields=["expected_boxes", "shipping_discrepancy_status", "shipping_discrepancy_payload", "updated_at"])
    _log_discrepancy(
        order,
        action="shipping_disc_substitution",
        user=user,
        description="По расхождению добавлен товар на добор через ричтрак OTG.",
        payload=payload,
    )
    return payload


def mark_discrepancy_pick_created(order: ShippingOrder, *, user, move_ids: list[str]) -> dict:
    payload = order.shipping_discrepancy_payload if isinstance(order.shipping_discrepancy_payload, dict) else {}
    payload = dict(payload)
    payload["additional_pick_move_ids"] = list(move_ids or [])
    payload["additional_pick_created_at"] = timezone.localtime().isoformat()
    payload["additional_pick_created_by"] = _actor_label(user)
    order.shipping_discrepancy_payload = payload
    order.save(update_fields=["shipping_discrepancy_payload", "updated_at"])
    _log_discrepancy(
        order,
        action="shipping_disc_pick_created",
        user=user,
        description="Создан обязательный добор через ричтрак OTG.",
        payload=payload,
    )
    return payload
