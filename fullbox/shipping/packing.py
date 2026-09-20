from __future__ import annotations

from collections import Counter
import re

from django.db import transaction
from django.utils import timezone

from audit.models import OrderAuditEntry, log_order_action
from marking.codes import (
    MarkingCodeFormatError,
    marking_code_identity,
    normalize_marking_code,
    validate_import_marking_code,
)
from sklad.models import WarehouseContainer, WarehouseEvent, WarehouseReserve, WarehouseStockSnapshot
from sklad.services.warehouse_transitions import WarehouseStateCode
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sku.models import Agency
from fullbox.container_codes import issue_pallet_code

from .item_binding import (
    enforced_marketplace_item_binding_errors,
    shipping_discrepancy_allows_current_mismatch,
    validate_marketplace_item_binding,
)
from .models import ShippingOrder
from .services import close_storekeeper_task, ensure_logistician_task, order_payload
from .truth import ShippingTruthService

_SHIPPING_PALLET_LABEL_RE = re.compile(r"^SHIP-\d+-PAL-(\d+)-(.*)$")
_SHIPPING_UI_PALLET_KEY_RE = re.compile(r"^PAL-\d+$", re.IGNORECASE)
_SHIPPING_ITEM_KEY_SEPARATOR = "\x1f"


def _to_int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _shipping_packing_entry(order: ShippingOrder) -> OrderAuditEntry | None:
    cache_key = "_shipping_packing_entry_cache"
    if hasattr(order, cache_key):
        return getattr(order, cache_key)
    return (
        OrderAuditEntry.objects.filter(
            order_id=order.number,
            order_type="shipping",
            payload__act__in=["shipping_packing", "shipping_loose_packing"],
        )
        .order_by("-created_at")
        .first()
    )


_PACKING_PALLET_REMOVAL_ACT = "shipping_packing_pallet_removal"
_PACKING_DRAFT_ACT = "shipping_packing_draft"


def _shipping_packing_pallet_removal_entries(order: ShippingOrder):
    return OrderAuditEntry.objects.filter(
        order_id=order.number,
        order_type="shipping",
        payload__act=_PACKING_PALLET_REMOVAL_ACT,
    ).order_by("created_at", "id")


def _shipping_packing_draft_entries(order: ShippingOrder):
    return OrderAuditEntry.objects.filter(
        order_id=order.number,
        order_type="shipping",
        payload__act=_PACKING_DRAFT_ACT,
    ).order_by("created_at", "id")


def shipping_packing_pending_pallet_removal(order: ShippingOrder) -> dict | None:
    entries = list(_shipping_packing_pallet_removal_entries(order))
    resolved_request_ids = {
        _to_int((entry.payload or {}).get("request_entry_id"))
        for entry in entries
        if str((entry.payload or {}).get("removal_status") or "").strip() in {"approved", "rejected"}
    }
    for entry in reversed(entries):
        payload = dict(entry.payload or {})
        if str(payload.get("removal_status") or "").strip() != "pending":
            continue
        if int(entry.id or 0) in resolved_request_ids:
            continue
        payload["entry_id"] = int(entry.id or 0)
        return payload
    return None


def _shipping_packing_draft_state(boxes_state: list, pallets_state: list) -> dict:
    boxes: list[dict] = []
    for index, raw_box in enumerate(boxes_state if isinstance(boxes_state, list) else [], start=1):
        if not isinstance(raw_box, dict):
            continue
        box_code = str(raw_box.get("code") or raw_box.get("box_code") or "").strip()
        if not box_code:
            continue
        items = [dict(item) for item in (raw_box.get("items") or []) if isinstance(item, dict)]
        boxes.append(
            {
                "row_key": str(raw_box.get("row_key") or "").strip(),
                "code": box_code,
                "qty": _to_int(raw_box.get("qty")) or _shipping_box_qty(items),
                "barcode_preview": str(raw_box.get("barcode_preview") or _shipping_box_items_preview(items) or "-"),
                "items": items,
                "pallet_code": str(raw_box.get("pallet_code") or "").strip(),
                "ui_index": _to_int(raw_box.get("ui_index")) or index,
            }
        )
    pallets: list[dict] = []
    for index, raw_pallet in enumerate(pallets_state if isinstance(pallets_state, list) else [], start=1):
        if not isinstance(raw_pallet, dict):
            continue
        code = str(raw_pallet.get("code") or "").strip()
        if not code:
            continue
        pallets.append(
            {
                "code": code,
                "label": str(raw_pallet.get("label") or code or f"PAL-{index}").strip(),
                "sealed": bool(raw_pallet.get("sealed")),
            }
        )
    return {"boxes": boxes, "pallets": pallets}


def _shipping_packing_draft_after_removal(draft_state: dict | None, removed_box_codes: list[str]) -> dict:
    draft = dict(draft_state or {})
    removed = {str(code or "").strip().lower() for code in removed_box_codes or [] if str(code or "").strip()}
    boxes = [
        dict(box)
        for box in draft.get("boxes") or []
        if isinstance(box, dict) and str(box.get("code") or "").strip().lower() not in removed
    ]
    used_pallets = {str(box.get("pallet_code") or "").strip() for box in boxes if str(box.get("pallet_code") or "").strip()}
    pallets = [
        dict(pallet)
        for pallet in draft.get("pallets") or []
        if isinstance(pallet, dict) and str(pallet.get("code") or "").strip() in used_pallets
    ]
    return {"boxes": boxes, "pallets": pallets}


def shipping_packing_saved_draft_state(order: ShippingOrder) -> dict | None:
    entries = list(_shipping_packing_pallet_removal_entries(order))
    resolved_request_ids = {
        _to_int((entry.payload or {}).get("request_entry_id"))
        for entry in entries
        if str((entry.payload or {}).get("removal_status") or "").strip() in {"approved", "rejected"}
    }
    candidates: list[tuple] = []
    for entry in reversed(entries):
        payload = dict(entry.payload or {})
        status = str(payload.get("removal_status") or "").strip()
        if status == "pending":
            if int(entry.id or 0) in resolved_request_ids:
                continue
            draft = payload.get("draft_state")
        elif status == "rejected":
            draft = payload.get("draft_state")
        elif status == "approved":
            draft = payload.get("draft_state_after_removal")
        else:
            continue
        if isinstance(draft, dict) and isinstance(draft.get("boxes"), list):
            candidates.append((entry.created_at, int(entry.id or 0), draft))
    latest_draft_entry = _shipping_packing_draft_entries(order).last()
    if latest_draft_entry:
        payload = dict(latest_draft_entry.payload or {})
        draft = payload.get("draft_state")
        if isinstance(draft, dict) and isinstance(draft.get("boxes"), list):
            candidates.append((latest_draft_entry.created_at, int(latest_draft_entry.id or 0), draft))
    if not candidates:
        return None
    latest = sorted(candidates, key=lambda item: (item[0], item[1]))[-1]
    latest_packing_entry = (
        OrderAuditEntry.objects.filter(
            order_id=order.number,
            order_type="shipping",
            payload__act="shipping_packing",
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if latest_packing_entry and (latest[0], latest[1]) <= (latest_packing_entry.created_at, int(latest_packing_entry.id or 0)):
        return None
    return latest[2]


def save_shipping_packing_draft(
    order: ShippingOrder,
    *,
    boxes_state: list,
    pallets_state: list,
    user,
) -> dict:
    if not isinstance(boxes_state, list):
        boxes_state = []
    if not isinstance(pallets_state, list):
        pallets_state = []
    draft_state = _shipping_packing_draft_state(boxes_state, pallets_state)
    if not draft_state.get("boxes"):
        return {"saved": False, "draft_state": draft_state}
    log_order_action(
        action="shipping_packing_draft",
        order_id=order.number,
        order_type="shipping",
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=order.agency,
        description="\u0427\u0435\u0440\u043d\u043e\u0432\u0438\u043a \u043f\u0430\u043b\u043b\u0435\u0442\u0438\u0437\u0430\u0446\u0438\u0438 \u0441\u043e\u0445\u0440\u0430\u043d\u0435\u043d.",
        payload=order_payload(
            order,
            extra={
                "act": _PACKING_DRAFT_ACT,
                "act_state": "draft",
                "draft_state": draft_state,
            },
        ),
    )
    return {"saved": True, "draft_state": draft_state}


def _shipping_removed_box_codes(order: ShippingOrder) -> set[str]:
    cache_key = "_shipping_removed_box_codes_cache"
    cached = getattr(order, cache_key, None)
    if cached is not None:
        return cached
    removed: set[str] = set()
    for entry in _shipping_packing_pallet_removal_entries(order):
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        if str(payload.get("removal_status") or "").strip() != "approved":
            continue
        for code in payload.get("box_codes") or []:
            normalized = str(code or "").strip().lower()
            if normalized:
                removed.add(normalized)
        for box in payload.get("boxes") or []:
            if not isinstance(box, dict):
                continue
            normalized = str(box.get("code") or box.get("box_code") or "").strip().lower()
            if normalized:
                removed.add(normalized)
    setattr(order, cache_key, removed)
    return removed


def _shipping_filter_removed_boxes(order: ShippingOrder, boxes: list[dict]) -> list[dict]:
    removed = _shipping_removed_box_codes(order)
    if not removed:
        return boxes
    return [
        box
        for box in boxes
        if str((box or {}).get("box_code") or (box or {}).get("code") or "").strip().lower() not in removed
    ]


def _packing_pallet_removal_task_route(order: ShippingOrder) -> str:
    return f"/shipping/{int(order.pk or 0)}/packing/pallet-removal/review/"


def _shipping_packing_box_state_rows(boxes_state: list, pallet_code: str) -> list[dict]:
    target = str(pallet_code or "").strip()
    result: list[dict] = []
    if not target or not isinstance(boxes_state, list):
        return result
    for raw_box in boxes_state:
        if not isinstance(raw_box, dict):
            continue
        if str(raw_box.get("pallet_code") or "").strip() != target:
            continue
        box_code = str(raw_box.get("code") or "").strip()
        if not box_code:
            continue
        items = [dict(item) for item in raw_box.get("items") or [] if isinstance(item, dict)]
        qty = _to_int(raw_box.get("qty")) or _shipping_box_qty(items)
        result.append(
            {
                "row_key": str(raw_box.get("row_key") or "").strip(),
                "code": box_code,
                "qty": qty,
                "barcode_preview": str(raw_box.get("barcode_preview") or _shipping_box_items_preview(items) or "-"),
                "items": items,
            }
        )
    return result


def _shipping_packing_pallet_label(pallets_state: list, pallet_code: str) -> str:
    target = str(pallet_code or "").strip()
    for raw_pallet in pallets_state or []:
        if not isinstance(raw_pallet, dict):
            continue
        if str(raw_pallet.get("code") or "").strip() == target:
            return str(raw_pallet.get("label") or target).strip()
    return target


def _shipping_packing_removal_manager():
    from employees.models import Employee

    return (
        Employee.objects.filter(role="manager", is_active=True).order_by("full_name").first()
        or Employee.objects.filter(role__in=["head_manager", "director", "admin"], is_active=True).order_by("full_name").first()
    )


def _create_shipping_pallet_removal_manager_task(order: ShippingOrder, payload: dict, user) -> None:
    from todo.models import Task

    manager = _shipping_packing_removal_manager()
    if manager is None:
        return
    route = _packing_pallet_removal_task_route(order)
    Task.objects.filter(route=route).exclude(status="done").update(status="done")
    pallet_label = str(payload.get("pallet_label") or payload.get("pallet_code") or "-").strip()
    box_count = _to_int(payload.get("box_count"))
    Task.objects.create(
        title=(
            "\u0421\u0420\u041e\u0427\u041d\u041e: "
            f"\u0441\u043e\u0433\u043b\u0430\u0441\u043e\u0432\u0430\u0442\u044c \u0438\u0437\u044a\u044f\u0442\u0438\u0435 \u043f\u0430\u043b\u043b\u0435\u0442\u044b {pallet_label}"
        ),
        description=(
            f"\u0417\u0430\u044f\u0432\u043a\u0430: {order.number}. "
            f"\u041f\u0430\u043b\u043b\u0435\u0442\u0430: {pallet_label}. "
            f"\u041a\u043e\u0440\u043e\u0431\u043e\u0432: {box_count}. "
            f"\u041f\u0440\u0438\u0447\u0438\u043d\u0430: {str(payload.get('reason') or '-').strip()}"
        ),
        route=route,
        assigned_to=manager,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        status="in_progress",
        priority="urgent",
        due_date=timezone.localtime(),
    )


def request_shipping_packing_pallet_removal(
    order: ShippingOrder,
    *,
    pallet_code: str,
    reason: str,
    boxes_state: list,
    pallets_state: list,
    user,
) -> dict:
    if shipping_packing_pending_pallet_removal(order):
        return {
            "ok": False,
            "error": "\u041f\u043e \u044d\u0442\u043e\u0439 \u0437\u0430\u044f\u0432\u043a\u0435 \u0443\u0436\u0435 \u0435\u0441\u0442\u044c \u0437\u0430\u043f\u0440\u043e\u0441 \u043d\u0430 \u0438\u0437\u044a\u044f\u0442\u0438\u0435 \u043f\u0430\u043b\u043b\u0435\u0442\u044b.",
        }
    normalized_pallet_code = str(pallet_code or "").strip()
    normalized_reason = str(reason or "").strip()
    if not normalized_pallet_code:
        return {"ok": False, "error": "\u041f\u0430\u043b\u043b\u0435\u0442\u0430 \u043d\u0435 \u0432\u044b\u0431\u0440\u0430\u043d\u0430."}
    if not normalized_reason:
        return {"ok": False, "error": "\u0423\u043a\u0430\u0436\u0438\u0442\u0435 \u043f\u0440\u0438\u0447\u0438\u043d\u0443 \u043e\u0442\u043c\u0435\u043d\u044b."}
    boxes = _shipping_packing_box_state_rows(boxes_state, normalized_pallet_code)
    if not boxes:
        return {
            "ok": False,
            "error": "\u0412 \u043f\u0430\u043b\u043b\u0435\u0442\u0435 \u043d\u0435\u0442 \u043a\u043e\u0440\u043e\u0431\u043e\u0432 \u0434\u043b\u044f \u0438\u0437\u044a\u044f\u0442\u0438\u044f.",
        }
    payload = order_payload(
        order,
        extra={
            "act": _PACKING_PALLET_REMOVAL_ACT,
            "removal_status": "pending",
            "pallet_code": normalized_pallet_code,
            "pallet_label": _shipping_packing_pallet_label(pallets_state, normalized_pallet_code),
            "reason": normalized_reason,
            "boxes": boxes,
            "box_codes": [str(box.get("code") or "").strip() for box in boxes],
            "box_count": len(boxes),
            "qty": sum(_to_int(box.get("qty")) for box in boxes),
            "draft_state": _shipping_packing_draft_state(boxes_state, pallets_state),
            "requested_at": timezone.localtime().isoformat(),
        },
    )
    with transaction.atomic():
        log_order_action(
            action="status",
            order_id=order.number,
            order_type="shipping",
            user=user if getattr(user, "is_authenticated", False) else None,
            agency=order.agency,
            description="\u0417\u0430\u043f\u0440\u043e\u0448\u0435\u043d\u043e \u0441\u043e\u0433\u043b\u0430\u0441\u043e\u0432\u0430\u043d\u0438\u0435 \u0438\u0437\u044a\u044f\u0442\u0438\u044f \u043f\u0430\u043b\u043b\u0435\u0442\u044b \u0438\u0437 \u043e\u0442\u0433\u0440\u0443\u0437\u043a\u0438",
            payload=payload,
        )
        entry = _shipping_packing_pallet_removal_entries(order).last()
        if entry is not None:
            payload["entry_id"] = int(entry.id or 0)
        _create_shipping_pallet_removal_manager_task(order, payload, user)
    return {"ok": True, "payload": payload}


def _close_shipping_pallet_removal_manager_tasks(order: ShippingOrder) -> None:
    from todo.models import Task

    Task.objects.filter(route=_packing_pallet_removal_task_route(order)).exclude(status="done").update(status="done")


def _issue_removed_shipping_pallet_code(order: ShippingOrder, boxes: list[dict]) -> str:
    goods_types = {
        str(item.get("goods_type") or "").strip()
        for box in boxes or []
        for item in box.get("items") or []
        if isinstance(item, dict) and str(item.get("goods_type") or "").strip()
    }
    goods_type = next(iter(goods_types)) if len(goods_types) == 1 else "gv"
    issued = issue_pallet_code(
        agency=order.agency,
        goods_type=goods_type,
        order_type="shipping",
        order_id=order.number,
    )
    return str(issued.get("code") or "").strip()


def _unlink_removed_shipping_pallet_from_order(order: ShippingOrder, payload: dict, *, user) -> str:
    box_codes = [str(code or "").strip() for code in payload.get("box_codes") or [] if str(code or "").strip()]
    if not box_codes:
        return ""
    pallet_code = _issue_removed_shipping_pallet_code(order, list(payload.get("boxes") or []))
    first_snapshot = (
        WarehouseStockSnapshot.objects.select_related("location")
        .filter(agency=order.agency, container_code__in=box_codes, is_archived=False)
        .order_by("id")
        .first()
    )
    location = first_snapshot.location if first_snapshot and first_snapshot.location_id else None
    pallet_container, _created = WarehouseContainer.objects.get_or_create(
        agency=order.agency,
        container_code=pallet_code,
        defaults={
            "container_type": WarehouseContainer.TYPE_MIXED_PALLET,
            "current_location": location,
            "created_by": user if getattr(user, "is_authenticated", False) else None,
            "source_context_type": "",
            "source_context_id": "",
        },
    )
    container_updates: list[str] = []
    if pallet_container.container_type != WarehouseContainer.TYPE_MIXED_PALLET:
        pallet_container.container_type = WarehouseContainer.TYPE_MIXED_PALLET
        container_updates.append("container_type")
    if location is not None and pallet_container.current_location_id != location.id:
        pallet_container.current_location = location
        container_updates.append("current_location")
    if str(pallet_container.source_context_type or "").strip():
        pallet_container.source_context_type = ""
        container_updates.append("source_context_type")
    if str(pallet_container.source_context_id or "").strip():
        pallet_container.source_context_id = ""
        container_updates.append("source_context_id")
    if container_updates:
        pallet_container.save(update_fields=container_updates + ["updated_at"])

    snapshots = list(
        WarehouseStockSnapshot.objects.select_related("container", "location")
        .filter(agency=order.agency, container_code__in=box_codes, is_archived=False)
        .order_by("id")
    )
    for snapshot in snapshots:
        box_container = snapshot.container
        if box_container is not None:
            box_updates: list[str] = []
            if box_container.parent_container_id != pallet_container.id:
                box_container.parent_container = pallet_container
                box_updates.append("parent_container")
            if location is not None and box_container.current_location_id != location.id:
                box_container.current_location = location
                box_updates.append("current_location")
            if str(box_container.source_context_type or "").strip() == "shipping":
                box_container.source_context_type = ""
                box_updates.append("source_context_type")
            if str(box_container.source_context_id or "").strip() == str(order.number or "").strip():
                box_container.source_context_id = ""
                box_updates.append("source_context_id")
            if box_updates:
                box_container.save(update_fields=box_updates + ["updated_at"])
        event = WarehouseEvent.objects.create(
            agency=order.agency,
            event_type="shipping_packing_pallet_removed_from_order",
            stock_context_type="",
            stock_context_id="",
            container=box_container,
            source_document_type="shipping",
            source_document_id=str(order.number or "").strip(),
            to_location=snapshot.location,
            to_zone_code=str(snapshot.zone_code or "").strip(),
            qty=max(_to_int(snapshot.qty), 0),
            payload={
                "request_entry_id": _to_int(payload.get("entry_id")),
                "shipping_order_id": str(order.number or "").strip(),
                "box_code": str(snapshot.container_code or "").strip(),
                "removed_pallet_code": pallet_code,
                "screen_pallet_code": str(payload.get("pallet_code") or "").strip(),
                "reason": str(payload.get("reason") or "").strip(),
            },
            performed_by=user if getattr(user, "is_authenticated", False) else None,
            occurred_at=timezone.localtime(),
        )
        snapshot.parent_container = pallet_container
        snapshot.last_event = event
        snapshot.available_qty = max(_to_int(snapshot.qty), 0)
        snapshot.shipping_reserved_qty = 0
        if (
            str(snapshot.source_context_type or "").strip() == "shipping"
            and str(snapshot.source_context_id or "").strip() == str(order.number or "").strip()
        ):
            snapshot.source_context_type = ""
            snapshot.source_context_id = ""
        snapshot.active_operation = None
        snapshot.active_operation_type = ""
        snapshot.warehouse_state_code = WarehouseStateCode.STORED.value
        snapshot.zone_code = str(snapshot.zone_code or "OTG").strip() or "OTG"
        snapshot.save(
            update_fields=[
                "parent_container",
                "last_event",
                "available_qty",
                "shipping_reserved_qty",
                "source_context_type",
                "source_context_id",
                "active_operation",
                "active_operation_type",
                "warehouse_state_code",
                "zone_code",
                "updated_at",
            ]
        )
    return pallet_code


def resolve_shipping_packing_pallet_removal(order: ShippingOrder, *, decision: str, user) -> dict:
    pending = shipping_packing_pending_pallet_removal(order)
    if not pending:
        return {
            "ok": False,
            "error": "\u041d\u0435\u0442 \u043e\u0442\u043a\u0440\u044b\u0442\u043e\u0433\u043e \u0437\u0430\u043f\u0440\u043e\u0441\u0430 \u043d\u0430 \u0438\u0437\u044a\u044f\u0442\u0438\u0435 \u043f\u0430\u043b\u043b\u0435\u0442\u044b.",
        }
    decision = str(decision or "").strip()
    if decision not in {"approved", "rejected"}:
        return {"ok": False, "error": "\u041d\u0435\u0432\u0435\u0440\u043d\u043e\u0435 \u0440\u0435\u0448\u0435\u043d\u0438\u0435."}
    with transaction.atomic():
        resolved_payload = dict(pending)
        resolved_payload["act"] = _PACKING_PALLET_REMOVAL_ACT
        resolved_payload["request_entry_id"] = _to_int(pending.get("entry_id"))
        resolved_payload["removal_status"] = decision
        resolved_payload["resolved_at"] = timezone.localtime().isoformat()
        resolved_payload["resolved_by"] = getattr(user, "get_username", lambda: "")() if getattr(user, "is_authenticated", False) else ""
        if decision == "approved":
            resolved_payload["detached_pallet_code"] = _unlink_removed_shipping_pallet_from_order(
                order,
                resolved_payload,
                user=user,
            )
            resolved_payload["draft_state_after_removal"] = _shipping_packing_draft_after_removal(
                pending.get("draft_state"),
                pending.get("box_codes") or [],
            )
            description = "\u0418\u0437\u044a\u044f\u0442\u0438\u0435 \u043f\u0430\u043b\u043b\u0435\u0442\u044b \u0438\u0437 \u0437\u0430\u044f\u0432\u043a\u0438 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u043e \u043c\u0435\u043d\u0435\u0434\u0436\u0435\u0440\u043e\u043c"
        else:
            description = "\u0418\u0437\u044a\u044f\u0442\u0438\u0435 \u043f\u0430\u043b\u043b\u0435\u0442\u044b \u0438\u0437 \u0437\u0430\u044f\u0432\u043a\u0438 \u043e\u0442\u043a\u043b\u043e\u043d\u0435\u043d\u043e \u043c\u0435\u043d\u0435\u0434\u0436\u0435\u0440\u043e\u043c"
        log_order_action(
            action="status",
            order_id=order.number,
            order_type="shipping",
            user=user if getattr(user, "is_authenticated", False) else None,
            agency=order.agency,
            description=description,
            payload=order_payload(order, extra=resolved_payload),
        )
        _close_shipping_pallet_removal_manager_tasks(order)
    return {"ok": True, "payload": resolved_payload}


def _normalize_shipping_pallet_label(value: str | None) -> str:
    return str(value or "").strip()


def _shipping_packing_label_number(value: str | None) -> int:
    raw = str(value or "").strip()
    match = re.fullmatch("(?:\u041f\u0430\u043b\u043b\u0435\u0442\u0430|Pallet|PAL)\\s*(\\d+)", raw, flags=re.IGNORECASE)
    return _to_int(match.group(1)) if match else 0


def _shipping_box_row_key(box: dict | None, index: int | None = None) -> str:
    if isinstance(box, dict):
        explicit = str(box.get("row_key") or "").strip()
        if explicit:
            return explicit
        box_code = str(box.get("box_code") or box.get("code") or "").strip() or "BOX"
        receiving_order_id = str(box.get("receiving_order_id") or "").strip() or "NA"
        suffix = int(index or box.get("ui_index") or 0)
        if suffix > 0:
            return f"{receiving_order_id}::{box_code}::{suffix}"
        return f"{receiving_order_id}::{box_code}"
    suffix = int(index or 0)
    return f"BOX::{suffix}" if suffix > 0 else "BOX"


def _shipping_item_goods_types(items: list[dict] | None) -> list[str]:
    values: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("goods_type") or "").strip()
        if raw:
            values.append(raw)
    return values


def _shipping_single_goods_type(values: list[str] | set[str] | tuple[str, ...] | None) -> str:
    normalized: dict[str, str] = {}
    for value in values or []:
        raw = str(value or "").strip()
        if not raw:
            continue
        normalized.setdefault(raw.lower(), raw)
    if len(normalized) == 1:
        return next(iter(normalized.values()))
    return ""


def _shipping_loose_box_code(order: ShippingOrder, index: int = 1) -> str:
    return f"SHIP-{int(order.pk or 0)}-BOX-LOOSE-{int(index or 1)}"


def _shipping_snapshot_box_code(order: ShippingOrder, snapshot: WarehouseStockSnapshot) -> str:
    container = snapshot.container
    if container is not None and str(container.container_type or "").strip() == WarehouseContainer.TYPE_BOX:
        return str(container.container_code or "").strip()
    if str(snapshot.container_code or "").strip():
        return str(snapshot.container_code or "").strip()
    return _shipping_loose_box_code(order, 1)


def _shipping_snapshot_is_loose(snapshot: WarehouseStockSnapshot) -> bool:
    return snapshot.container_id is None and not str(snapshot.container_code or "").strip()


def _shipping_item_key_from_values(
    sku_code: str | None,
    name: str | None,
    size: str | None,
    barcode: str | None,
    goods_type: str | None,
) -> str:
    return _SHIPPING_ITEM_KEY_SEPARATOR.join(
        [
            str(sku_code or "").strip(),
            str(name or "").strip(),
            str(size or "").strip(),
            str(barcode or "").strip(),
            str(goods_type or "").strip(),
        ]
    )


def _shipping_item_key_from_payload(item: dict | None) -> str:
    if not isinstance(item, dict):
        return ""
    return _shipping_item_key_from_values(
        item.get("sku_code") or item.get("sku"),
        item.get("name"),
        item.get("size"),
        item.get("barcode"),
        item.get("goods_type"),
    )


def _shipping_item_key_from_snapshot(snapshot: WarehouseStockSnapshot) -> str:
    return _shipping_item_key_from_values(
        snapshot.sku_code,
        snapshot.name,
        snapshot.size,
        snapshot.barcode,
        snapshot.goods_type,
    )


def _shipping_loose_box_code_base(order: ShippingOrder) -> str:
    return f"SHIP-{int(order.pk or 0)}-BOX"


def _shipping_box_items_preview(items: list[dict] | None) -> str:
    prepared = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        barcode = str(item.get("barcode") or item.get("sku_code") or item.get("sku") or "").strip() or "-"
        qty = _to_int(item.get("qty") or item.get("actual_qty") or item.get("count"))
        if qty <= 0:
            continue
        prepared.append(f"{barcode} - {qty} шт.")
    return "; ".join(prepared[:4]) + ("; ..." if len(prepared) > 4 else "")


def _shipping_box_qty(items: list[dict] | None) -> int:
    total = 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        total += _to_int(item.get("qty") or item.get("actual_qty") or item.get("count"))
    return int(max(total, 0))


def _warehouse_shipping_snapshot_qty(snapshot: WarehouseStockSnapshot) -> int:
    state_code = str(snapshot.warehouse_state_code or "").strip()
    if state_code in {
        WarehouseStateCode.IN_OTG.value,
        WarehouseStateCode.PALLETIZING.value,
        WarehouseStateCode.READY_FOR_LOADING.value,
        WarehouseStateCode.ASSIGNED_TO_TRIP.value,
        WarehouseStateCode.LOADING_IN_PROGRESS.value,
        WarehouseStateCode.LOADED_TO_VEHICLE.value,
        WarehouseStateCode.SHIPPED.value,
    }:
        return int(snapshot.qty or 0)
    return int(snapshot.shipping_reserved_qty or snapshot.qty or 0)


def _shipping_flow_snapshot_queryset(order: ShippingOrder):
    order_key = str(order.number or "").strip()
    if not order_key:
        return WarehouseStockSnapshot.objects.none()
    return (
        WarehouseStockSnapshot.objects.select_related("container", "parent_container", "location", "active_operation", "last_event")
        .filter(
            agency=order.agency,
            is_archived=False,
            last_event__stock_context_type="shipping",
            last_event__stock_context_id=order_key,
            warehouse_state_code__in=[
                WarehouseStateCode.IN_OTG.value,
                WarehouseStateCode.PALLETIZING.value,
                WarehouseStateCode.READY_FOR_LOADING.value,
                WarehouseStateCode.ASSIGNED_TO_TRIP.value,
                WarehouseStateCode.LOADING_IN_PROGRESS.value,
                WarehouseStateCode.LOADED_TO_VEHICLE.value,
                WarehouseStateCode.SHIPPED.value,
            ],
        )
        .order_by("id")
    )


def _shipping_flow_snapshots(order: ShippingOrder) -> list[WarehouseStockSnapshot]:
    cache_key = "_shipping_flow_snapshots_cache"
    cached = getattr(order, cache_key, None)
    if cached is None:
        if order.status == ShippingOrder.STATUS_CANCELED:
            # A newly created shipping order may reserve released stock before
            # the old canceled order is physically returned. Its reserve event
            # becomes last_event, so keep finding the boxes through their
            # delivered BoxClaim association until they leave the OTG flow.
            cached = list(ShippingTruthService.for_order(order).warehouse_snapshots)
        else:
            cached = list(_shipping_flow_snapshot_queryset(order))
        setattr(order, cache_key, cached)
    return cached


def _shipping_loose_snapshots(order: ShippingOrder) -> list[WarehouseStockSnapshot]:
    return [
        snapshot
        for snapshot in _shipping_flow_snapshots(order)
        if _shipping_snapshot_is_loose(snapshot) and _warehouse_shipping_snapshot_qty(snapshot) > 0
    ]


def _trusted_reachtruck_marking_identity(snapshot: WarehouseStockSnapshot) -> str:
    """Return a reusable KIZ only for a one-unit scan recorded by reachtruck."""
    if _warehouse_shipping_snapshot_qty(snapshot) != 1:
        return ""
    last_event = getattr(snapshot, "last_event", None)
    payload = getattr(last_event, "payload", None)
    payload = payload if isinstance(payload, dict) else {}
    if not (
        payload.get("partial_shipping_pick") is True
        and payload.get("marking_scan_recorded") is True
    ):
        return ""
    try:
        marking_code = validate_import_marking_code(
            getattr(snapshot, "marking_code", ""),
            product_barcode=getattr(snapshot, "barcode", ""),
        )
    except MarkingCodeFormatError:
        return ""
    return marking_code_identity(marking_code)


def _shipping_items_from_snapshots(
    order: ShippingOrder,
    snapshots: list[WarehouseStockSnapshot],
) -> list[dict]:
    rows: dict[str, dict] = {}
    trusted_marking_identity_by_snapshot_id = {
        int(snapshot.id): _trusted_reachtruck_marking_identity(snapshot)
        for snapshot in snapshots
    }
    trusted_marking_identity_counts = Counter(
        identity
        for identity in trusted_marking_identity_by_snapshot_id.values()
        if identity
    )
    source_barcodes = {
        str(snapshot.barcode or "").strip()
        for snapshot in snapshots
        if str(snapshot.barcode or "").strip()
    }
    if order.status == ShippingOrder.STATUS_CANCELED:
        # This is a warehouse return, not an outbound verification. Preserve
        # existing marking codes on stock rows, but do not require operators to
        # scan Data Matrix again while repacking canceled goods for placement.
        marking_required_barcodes = set()
    else:
        marking_required_barcodes = WarehouseWritePathService._required_loose_packing_marking_barcodes(
            agency=order.agency,
            order_id=order.number,
            barcodes=source_barcodes,
        )
    for snapshot in snapshots:
        item_qty = _warehouse_shipping_snapshot_qty(snapshot)
        if item_qty <= 0:
            continue
        key = _shipping_item_key_from_snapshot(snapshot)
        row = rows.setdefault(
            key,
            {
                "key": key,
                "sku_code": str(snapshot.sku_code or "").strip(),
                "name": str(snapshot.name or "").strip(),
                "size": str(snapshot.size or "").strip(),
                "barcode": str(snapshot.barcode or "").strip(),
                "goods_type": str(snapshot.goods_type or "").strip(),
                "qty": 0,
                "snapshot_ids": [],
                "preverified_marking_qty": 0,
                "requires_marking_scan": False,
                "marking_preverified": False,
            },
        )
        row["qty"] = int(row.get("qty") or 0) + item_qty
        row["snapshot_ids"].append(int(snapshot.id))
        trusted_identity = trusted_marking_identity_by_snapshot_id.get(int(snapshot.id), "")
        if trusted_identity and trusted_marking_identity_counts[trusted_identity] == 1:
            row["preverified_marking_qty"] = int(row.get("preverified_marking_qty") or 0) + item_qty
    for row in rows.values():
        barcode_requires_marking = str(row.get("barcode") or "").strip() in marking_required_barcodes
        fully_preverified = bool(
            barcode_requires_marking
            and int(row.get("qty") or 0) > 0
            and int(row.get("preverified_marking_qty") or 0) == int(row.get("qty") or 0)
        )
        row["marking_preverified"] = fully_preverified
        row["requires_marking_scan"] = bool(barcode_requires_marking and not fully_preverified)
    return sorted(
        rows.values(),
        key=lambda item: (
            str(item.get("sku_code") or "").lower(),
            str(item.get("name") or "").lower(),
            str(item.get("size") or "").lower(),
            str(item.get("barcode") or "").lower(),
        ),
    )


def _shipping_loose_items(order: ShippingOrder) -> list[dict]:
    return _shipping_items_from_snapshots(order, _shipping_loose_snapshots(order))


def _shipping_boxed_repack_snapshots(order: ShippingOrder) -> list[WarehouseStockSnapshot]:
    """Current OTG stock that can be physically rebuilt into Ozon GM boxes."""
    return [
        snapshot
        for snapshot in _shipping_flow_snapshots(order)
        if not _shipping_snapshot_is_loose(snapshot)
        and str(snapshot.warehouse_state_code or "").strip() == WarehouseStateCode.IN_OTG.value
        and _warehouse_shipping_snapshot_qty(snapshot) > 0
    ]


def _shipping_boxed_repack_items(order: ShippingOrder) -> list[dict]:
    return _shipping_items_from_snapshots(order, _shipping_boxed_repack_snapshots(order))


def _shipping_repack_snapshots(order: ShippingOrder) -> list[WarehouseStockSnapshot]:
    snapshots = list(_shipping_boxed_repack_snapshots(order))
    snapshots.extend(_shipping_loose_snapshots(order))
    return list({int(snapshot.pk): snapshot for snapshot in snapshots}.values())


def _shipping_repack_items(order: ShippingOrder) -> list[dict]:
    return _shipping_items_from_snapshots(order, _shipping_repack_snapshots(order))


def _shipping_receiving_placement_box_map(agency: Agency | None, receiving_order_id: str) -> dict[str, dict]:
    receiving_key = str(receiving_order_id or "").strip()
    if not agency or not receiving_key:
        return {}
    cache_attr = "_shipping_receiving_placement_box_map_cache"
    cache = getattr(agency, cache_attr, None)
    if cache is None:
        cache = {}
        setattr(agency, cache_attr, cache)
    cache_key = (int(getattr(agency, "pk", 0) or 0), receiving_key)
    if cache_key in cache:
        return cache[cache_key]
    entries = (
        OrderAuditEntry.objects.filter(
            agency=agency,
            order_type="receiving",
            order_id=receiving_key,
            payload__act="placement",
        )
        .order_by("-created_at", "-id")
    )
    box_map: dict[str, dict] = {}
    for entry in entries:
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        for raw_box in payload.get("act_boxes") or []:
            if not isinstance(raw_box, dict):
                continue
            box_code = str(raw_box.get("code") or "").strip()
            if not box_code:
                continue
            box_map.setdefault(box_code.lower(), dict(raw_box))
    cache[cache_key] = box_map
    return box_map


def _shipping_delivered_boxes_from_warehouse(order: ShippingOrder) -> list[dict]:
    snapshots = _shipping_flow_snapshots(order)
    if not snapshots:
        return []

    removed_box_codes = _shipping_removed_box_codes(order)
    boxes: dict[str, dict] = {}
    for snapshot in snapshots:
        if _shipping_snapshot_is_loose(snapshot):
            continue
        box_code = _shipping_snapshot_box_code(order, snapshot)
        if not box_code:
            continue
        if box_code.lower() in removed_box_codes:
            continue
        receiving_order_id = (
            str(snapshot.source_context_id or "").strip()
            if str(snapshot.source_context_type or "").strip() == "receiving"
            else ""
        )
        parent_pallet = snapshot.parent_container
        container_source_type = str(getattr(snapshot.container, "source_context_type", "") or "").strip()
        container_source_id = str(getattr(snapshot.container, "source_context_id", "") or "").strip()
        is_loose_stage_box = (
            container_source_type == "shipping"
            and container_source_id == str(order.number or "").strip()
        )
        parent_pallet_code = (
            str(parent_pallet.container_code or "").strip()
            if is_loose_stage_box
            and parent_pallet is not None
            and str(parent_pallet.source_context_type or "").strip() == "shipping"
            and str(parent_pallet.source_context_id or "").strip() == str(order.number or "").strip()
            else ""
        )
        parent_pallet_label = _shipping_pallet_label_from_code(parent_pallet_code) if parent_pallet_code else ""
        entry = boxes.setdefault(
            box_code.lower(),
            {
                "box_code": box_code,
                "qty": 0,
                "items": [],
                "receiving_order_id": receiving_order_id,
                "pallet_code": parent_pallet_code,
                "pallet_label": parent_pallet_label,
            },
        )
        if parent_pallet_code and not str(entry.get("pallet_code") or "").strip():
            entry["pallet_code"] = parent_pallet_code
            entry["pallet_label"] = parent_pallet_label
        item_qty = _warehouse_shipping_snapshot_qty(snapshot)
        if item_qty <= 0:
            continue
        entry["qty"] += item_qty
        entry["items"].append(
            {
                "sku_code": str(snapshot.sku_code or "").strip(),
                "name": str(snapshot.name or "").strip(),
                "size": str(snapshot.size or "").strip(),
                "barcode": str(snapshot.barcode or "").strip(),
                "goods_type": str(snapshot.goods_type or "").strip(),
                "qty": item_qty,
            }
        )

    delivered: list[dict] = []
    for index, box in enumerate(sorted(boxes.values(), key=lambda item: str(item.get("box_code") or "").lower()), start=1):
        items = list(box.get("items") or [])
        delivered.append(
            {
                "row_key": _shipping_box_row_key(
                    {
                        "box_code": box.get("box_code"),
                        "receiving_order_id": box.get("receiving_order_id") or "",
                    },
                    index,
                ),
                "box_code": str(box.get("box_code") or "").strip(),
                "qty": int(box.get("qty") or 0),
                "items": items,
                "barcode_preview": _shipping_box_items_preview(items),
                "receiving_order_id": str(box.get("receiving_order_id") or "").strip(),
                "pallet_code": str(box.get("pallet_code") or "").strip(),
                "pallet_label": str(box.get("pallet_label") or "").strip(),
            }
        )
    return [row for row in delivered if row["box_code"] and int(row.get("qty") or 0) > 0]


def _shipping_warehouse_snapshots(order: ShippingOrder) -> list[WarehouseStockSnapshot]:
    return list(_shipping_flow_snapshots(order))


def _shipping_owned_pallet_container(
    order: ShippingOrder,
    *,
    container_code: str,
    location,
    user,
) -> WarehouseContainer:
    code = str(container_code or "").strip()
    if not code or _SHIPPING_UI_PALLET_KEY_RE.fullmatch(code):
        raise ValueError("Внутренний ключ паллеты нельзя сохранить как складской код.")

    container = (
        WarehouseContainer.objects.select_for_update()
        .filter(agency=order.agency, container_code=code)
        .first()
    )
    if container is None:
        return WarehouseContainer.objects.create(
            agency=order.agency,
            container_code=code,
            container_type=WarehouseContainer.TYPE_MIXED_PALLET,
            current_location=location,
            created_by=user if getattr(user, "is_authenticated", False) else None,
            source_context_type="shipping",
            source_context_id=str(order.number or "").strip(),
        )

    expected_order_id = str(order.number or "").strip()
    owner_type = str(container.source_context_type or "").strip().lower()
    owner_id = str(container.source_context_id or "").strip()
    if owner_type != "shipping" or owner_id != expected_order_id:
        raise ValueError(
            f"Паллета {code} уже принадлежит другой заявке. "
            "Раскладка не сохранена, складские данные не изменены."
        )
    if container.container_type != WarehouseContainer.TYPE_MIXED_PALLET:
        raise ValueError(f"Код {code} уже используется не как паллета.")
    if location is not None and container.current_location_id != location.id:
        container.current_location = location
        container.save(update_fields=["current_location", "updated_at"])
    return container


def _shipping_sync_warehouse_packing(
    order: ShippingOrder,
    *,
    act_data: dict,
    user,
) -> bool:
    removed_box_codes = _shipping_removed_box_codes(order)
    snapshots = [
        snapshot
        for snapshot in _shipping_warehouse_snapshots(order)
        if _shipping_snapshot_box_code(order, snapshot).lower() not in removed_box_codes
    ]
    if not snapshots:
        return False

    operation = next(
        (
            snapshot.active_operation
            for snapshot in snapshots
            if snapshot.active_operation is not None
            and str(snapshot.active_operation.operation_type or "").strip() == "palletization"
            and str(snapshot.active_operation.status or "").strip() in {"created", "planned", "in_progress", "partial"}
        ),
        None,
    )
    if operation is None and any(snapshot.warehouse_state_code == WarehouseStateCode.IN_OTG.value for snapshot in snapshots):
        operation = WarehouseWritePathService.start_palletization(
            agency=order.agency,
            order_id=order.number,
            box_codes=[
                str(box.get("code") or box.get("box_code") or "").strip()
                for box in (act_data.get("act_boxes") or [])
                if isinstance(box, dict)
                and str(box.get("code") or box.get("box_code") or "").strip()
            ],
            started_by=user,
        )
        snapshots = [
            snapshot
            for snapshot in _shipping_warehouse_snapshots(order)
            if _shipping_snapshot_box_code(order, snapshot).lower() not in removed_box_codes
        ]

    pallet_specs = {
        str(pallet.get("label") or "").strip(): dict(pallet)
        for pallet in (act_data.get("act_pallets") or [])
        if isinstance(pallet, dict) and str(pallet.get("label") or "").strip() and str(pallet.get("code") or "").strip()
    }
    if not pallet_specs:
        return False

    pallet_containers: dict[str, WarehouseContainer] = {}
    for label, pallet_spec in pallet_specs.items():
        pallet_code = str(pallet_spec.get("code") or "").strip()
        location = next((snapshot.location for snapshot in snapshots if snapshot.location_id), None)
        pallet_container = _shipping_owned_pallet_container(
            order,
            container_code=pallet_code,
            location=location,
            user=user,
        )
        pallet_containers[pallet_code.lower()] = pallet_container

    target_pallet_by_box_code = {
        str(box.get("code") or "").strip().lower(): str(box.get("pallet_code") or "").strip().lower()
        for box in (act_data.get("act_boxes") or [])
        if isinstance(box, dict) and str(box.get("code") or "").strip() and str(box.get("pallet_code") or "").strip()
    }
    if not target_pallet_by_box_code:
        return False

    assigned_snapshot_ids: set[int] = set()
    containers_by_id: dict[int, WarehouseContainer] = {}
    containers_by_box_code: dict[str, WarehouseContainer] = {}
    containers_to_update: dict[int, WarehouseContainer] = {}
    snapshots_to_update: dict[int, WarehouseStockSnapshot] = {}
    snapshot_container_fields_changed: set[int] = set()
    now = timezone.now()
    for snapshot in snapshots:
        container = snapshot.container
        box_code = _shipping_snapshot_box_code(order, snapshot)
        normalized_box_code = box_code.lower()
        target_code = target_pallet_by_box_code.get(normalized_box_code)
        target_pallet = pallet_containers.get(str(target_code or "").strip().lower())
        if not target_pallet or not box_code:
            continue
        assigned_snapshot_ids.add(int(snapshot.id))
        container_was_missing = container is None
        if container is not None:
            container = containers_by_id.setdefault(int(container.id), container)
            containers_by_box_code.setdefault(normalized_box_code, container)
        else:
            container = containers_by_box_code.get(normalized_box_code)
            if container is None:
                container, _created = WarehouseContainer.objects.get_or_create(
                    agency=order.agency,
                    container_code=box_code,
                    defaults={
                        "container_type": WarehouseContainer.TYPE_BOX,
                        "current_location": target_pallet.current_location,
                        "parent_container": target_pallet,
                        "created_by": user if getattr(user, "is_authenticated", False) else None,
                        "source_context_type": "shipping",
                        "source_context_id": str(order.number or "").strip(),
                    },
                )
                container = containers_by_id.setdefault(int(container.id), container)
                containers_by_box_code[normalized_box_code] = container
        container_changed = False
        if container_was_missing:
            if container.container_type != WarehouseContainer.TYPE_BOX:
                container.container_type = WarehouseContainer.TYPE_BOX
                container_changed = True
            if str(container.source_context_type or "").strip() != "shipping":
                container.source_context_type = "shipping"
                container_changed = True
            if str(container.source_context_id or "").strip() != str(order.number or "").strip():
                container.source_context_id = str(order.number or "").strip()
                container_changed = True
        if container is not None and str(container.container_type or "").strip() == WarehouseContainer.TYPE_BOX:
            if container.parent_container_id != target_pallet.id:
                container.parent_container = target_pallet
                container_changed = True
            if target_pallet.current_location_id and container.current_location_id != target_pallet.current_location_id:
                container.current_location = target_pallet.current_location
                container_changed = True
        if container_changed:
            container.updated_at = now
            containers_to_update[int(container.id)] = container
        snapshot_changed = False
        if snapshot.container_id != getattr(container, "id", None):
            snapshot.container = container
            snapshot_changed = True
            snapshot_container_fields_changed.add(int(snapshot.id))
        if str(snapshot.container_code or "").strip() != box_code:
            snapshot.container_code = box_code
            snapshot_changed = True
            snapshot_container_fields_changed.add(int(snapshot.id))
        if snapshot.parent_container_id != target_pallet.id:
            snapshot.parent_container = target_pallet
            snapshot_changed = True
        if target_pallet.current_location_id and snapshot.location_id != target_pallet.current_location_id:
            snapshot.location = target_pallet.current_location
            snapshot.zone_code = str(target_pallet.current_location.zone_code or "").strip()
            snapshot.zone_kind = str(target_pallet.current_location.zone_kind or "").strip()
            snapshot_changed = True
        if snapshot_changed:
            snapshot.updated_at = now
            snapshots_to_update[int(snapshot.id)] = snapshot

    if containers_to_update:
        WarehouseContainer.objects.bulk_update(
            list(containers_to_update.values()),
            [
                "container_type",
                "parent_container",
                "current_location",
                "source_context_type",
                "source_context_id",
                "updated_at",
            ],
            batch_size=500,
        )
    grouped_snapshot_ids: dict[tuple, list[int]] = {}
    snapshots_with_container_changes: list[WarehouseStockSnapshot] = []
    for snapshot_id, snapshot in snapshots_to_update.items():
        if snapshot_id in snapshot_container_fields_changed:
            snapshots_with_container_changes.append(snapshot)
            continue
        group_key = (
            snapshot.parent_container_id,
            snapshot.location_id,
            str(snapshot.zone_code or ""),
            str(snapshot.zone_kind or ""),
        )
        grouped_snapshot_ids.setdefault(group_key, []).append(snapshot_id)
    for (parent_container_id, location_id, zone_code, zone_kind), snapshot_ids in grouped_snapshot_ids.items():
        WarehouseStockSnapshot.objects.filter(id__in=snapshot_ids).update(
            parent_container_id=parent_container_id,
            location_id=location_id,
            zone_code=zone_code,
            zone_kind=zone_kind,
            updated_at=now,
        )
    if snapshots_with_container_changes:
        WarehouseStockSnapshot.objects.bulk_update(
            snapshots_with_container_changes,
            [
                "container",
                "container_code",
                "parent_container",
                "location",
                "zone_code",
                "zone_kind",
                "updated_at",
            ],
            batch_size=500,
        )

    if operation is not None and str(operation.status or "").strip() != "done":
        if assigned_snapshot_ids:
            _shipping_detach_unassigned_palletization_snapshots(
                operation=operation,
                keep_snapshot_ids=assigned_snapshot_ids,
                user=user,
            )
        WarehouseWritePathService.complete_palletization(
            operation=operation,
            performed_by=user,
        )
    return True


def _shipping_release_canceled_palletized_reserves(order: ShippingOrder, *, user) -> None:
    order_key = str(order.number or "").strip()
    if not order_key:
        return

    released_by = user if getattr(user, "is_authenticated", False) else None
    reserves = (
        WarehouseReserve.objects.select_for_update()
        .filter(
            agency=order.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id=order_key,
        )
        .exclude(status__in=[WarehouseReserve.STATUS_RELEASED, WarehouseReserve.STATUS_CANCELED])
        .order_by("id")
    )
    for reserve in reserves:
        reserve.status = WarehouseReserve.STATUS_RELEASED
        # Keep the historical reserve quantities. The warehouse schema requires
        # qty_reserved > 0 even for released rows; availability is controlled by
        # the status and by the stock snapshot fields.
        update_fields = ["status"]
        if released_by is not None:
            reserve.released_by = released_by
            update_fields.append("released_by")
        reserve.save(update_fields=update_fields + ["updated_at"])

    # Availability is restored at cancellation time. Do not alter snapshot
    # quantities here: meanwhile the same goods may already be reserved by a
    # different shipping order.
    order.items.filter(qty_reserved__gt=0).update(qty_reserved=0)


def _shipping_create_canceled_return_tasks(
    order: ShippingOrder,
    *,
    act_data: dict,
    user,
) -> list[str]:
    from reachtruck.models import MoveTask
    from reachtruck.services.move_requests import create_batch_move_tasks

    order_key = str(order.number or "").strip()
    pallets = [
        dict(pallet)
        for pallet in (act_data.get("act_pallets") or [])
        if isinstance(pallet, dict) and str(pallet.get("code") or "").strip()
    ]
    if not order_key or not pallets:
        return []

    box_codes_by_pallet: dict[str, list[str]] = {}
    for raw_box in act_data.get("act_boxes") or []:
        if not isinstance(raw_box, dict):
            continue
        return_pallet_code = str(raw_box.get("pallet_code") or "").strip()
        box_code = str(raw_box.get("code") or raw_box.get("box_code") or "").strip()
        if not return_pallet_code or not box_code:
            continue
        box_codes_by_pallet.setdefault(return_pallet_code.lower(), []).append(box_code)

    existing_tasks = MoveTask.objects.filter(
        request__agency=order.agency,
        payload__canceled_shipping_return_v1=True,
        payload__shipping_order_id=order_key,
        status__in=[MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS, MoveTask.STATUS_DONE],
    )
    existing_by_pallet = {
        str(task.pallet_code or "").strip().lower(): str(task.legacy_order_id or "").strip()
        for task in existing_tasks
        if str(task.pallet_code or "").strip()
    }

    actor = user if getattr(user, "is_authenticated", False) else None
    actor_name = ""
    if actor is not None:
        actor_name = str(actor.get_full_name() or actor.get_username() or actor).strip()
    destination = {"zone": "OS", "row": "", "section": "", "tier": "", "cell": ""}
    task_specs: list[dict] = []
    move_ids = [move_id for move_id in existing_by_pallet.values() if move_id]
    for pallet in pallets:
        pallet_code = str(pallet.get("code") or "").strip()
        if pallet_code.lower() in existing_by_pallet:
            continue
        qty = max(_to_int(pallet.get("qty")), 0)
        payload = {
            "status": "created",
            "status_label": "Ожидает возврата из OTG",
            "canceled_shipping_return_v1": True,
            "shipping_order_id": order_key,
            "shipping_order_pk": int(order.pk or 0),
            "pallet_code": pallet_code,
            "box_codes": box_codes_by_pallet.get(pallet_code.lower(), []),
            "from_location": {"zone": "OTG", "row": "", "section": "", "tier": "", "cell": ""},
            "to_location": destination,
            "from_label": "OTG · Зона отгрузки",
            "to_label": "Основной склад · отсканируйте ячейку хранения",
            "pick_mode": "full",
            "move_mode": "pallet_full",
            "requested_qty": qty,
            "requested_sku": "",
            "requested_barcodes": [],
            "requested_goods_type": "",
            "instruction": (
                f"Заявка {order_key} отменена. Возьмите возвратную паллету {pallet_code} в OTG, "
                "доставьте её в основной склад и отсканируйте фактическую ячейку хранения."
            ),
        }
        task_specs.append(
            {
                "description": f"Возврат паллеты {pallet_code} из OTG по отмененной заявке {order_key}",
                "payload": payload,
            }
        )

    if task_specs:
        _move_request, created_ids = create_batch_move_tasks(
            context_type="shipping_return",
            context_id=order_key,
            agency=order.agency,
            user=actor,
            requested_by_name=actor_name,
            requested_by_role="storekeeper",
            destination=destination,
            comment=f"Возврат из OTG по отмененной заявке {order_key}",
            task_specs=task_specs,
        )
        move_ids.extend(created_ids)
    return move_ids


def _shipping_delivered_boxes(order: ShippingOrder) -> list[dict]:
    cache_key = "_shipping_delivered_boxes_cache"
    cached = getattr(order, cache_key, None)
    if cached is not None:
        return cached
    removed_box_codes = _shipping_removed_box_codes(order)
    warehouse_boxes = _shipping_filter_removed_boxes(order, _shipping_delivered_boxes_from_warehouse(order))
    setattr(order, cache_key, warehouse_boxes)
    return warehouse_boxes


def _shipping_boxes_from_packing_payload(payload: dict | None) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    prepared: list[dict] = []
    for index, raw_box in enumerate(payload.get("act_boxes") or [], start=1):
        if not isinstance(raw_box, dict):
            continue
        box_code = str(raw_box.get("code") or "").strip()
        if not box_code:
            continue
        items = [dict(item) for item in (raw_box.get("items") or []) if isinstance(item, dict)]
        prepared.append(
            {
                "row_key": _shipping_box_row_key(raw_box, index),
                "box_code": box_code,
                "qty": _to_int(raw_box.get("qty")) or _shipping_box_qty(items),
                "items": items,
                "barcode_preview": str(raw_box.get("barcode_preview") or _shipping_box_items_preview(items) or "-"),
                "receiving_order_id": str(raw_box.get("receiving_order_id") or "").strip(),
            }
        )
    return [row for row in prepared if row["box_code"] and int(row.get("qty") or 0) > 0]


def _shipping_build_packing_act(
    order: ShippingOrder,
    delivered_boxes: list[dict],
    assignments: dict[str, str],
    source_codes_by_label: dict[str, str] | None = None,
) -> dict:
    pallet_order: list[str] = []
    pallet_codes: dict[str, str] = {}
    pallet_goods_types: dict[str, list[str]] = {}
    pallets_by_label: dict[str, dict] = {}
    act_boxes: list[dict] = []
    item_totals: dict[tuple[str, str, str, str, str], dict] = {}

    for index, box in enumerate(delivered_boxes, start=1):
        row_key = _shipping_box_row_key(box, index)
        label = _normalize_shipping_pallet_label(assignments.get(row_key))
        if not label:
            continue
        pallet_goods_types.setdefault(label, []).extend(_shipping_item_goods_types(box.get("items")))

    for index, box in enumerate(delivered_boxes, start=1):
        row_key = _shipping_box_row_key(box, index)
        box_code = str(box.get("box_code") or "").strip()
        if not box_code:
            continue
        label = _normalize_shipping_pallet_label(assignments.get(row_key))
        if not label:
            continue
        ui_key = str((source_codes_by_label or {}).get(label) or "").strip()
        if label not in pallet_codes:
            pallet_order.append(label)
            issued = issue_pallet_code(
                agency=order.agency,
                goods_type=_shipping_single_goods_type(pallet_goods_types.get(label)),
                order_type="shipping",
                order_id=order.number,
            )
            pallet_codes[label] = str(issued.get("code") or "").strip()
        pallet_code = pallet_codes[label]
        if not ui_key:
            ui_key = label
        items = [dict(item) for item in (box.get("items") or []) if isinstance(item, dict)]
        box_qty = _shipping_box_qty(items) or _to_int(box.get("qty"))
        act_box = {
            "row_key": row_key,
            "code": box_code,
            "qty": int(max(box_qty, 0)),
            "items": items,
            "barcode_preview": _shipping_box_items_preview(items) or str(box.get("barcode_preview") or ""),
            "pallet_code": pallet_code,
            "pallet_label": label,
            "pallet_source_code": pallet_code,
            "pallet_ui_key": ui_key,
            "location": {"zone": "OTG"},
            "sealed": True,
        }
        act_boxes.append(act_box)
        pallet_entry = pallets_by_label.setdefault(
            label,
            {
                "code": pallet_code,
                "label": label,
                "source_code": pallet_code,
                "ui_key": ui_key,
                "boxes": [],
                "items": [],
                "location": {"zone": "OTG"},
                "sealed": True,
                "qty": 0,
            },
        )
        pallet_entry["boxes"].append(row_key)
        pallet_entry["qty"] = int(pallet_entry.get("qty") or 0) + int(max(box_qty, 0))
        for item in items:
            key = (
                str(item.get("sku_code") or item.get("sku") or "").strip(),
                str(item.get("name") or "").strip(),
                str(item.get("size") or "").strip(),
                str(item.get("barcode") or "").strip(),
                str(item.get("goods_type") or "").strip(),
            )
            row = item_totals.setdefault(
                key,
                {
                    "sku_code": key[0],
                    "name": key[1],
                    "size": key[2],
                    "barcode": key[3],
                    "goods_type": key[4],
                    "qty": 0,
                },
            )
            row["qty"] += _to_int(item.get("qty") or item.get("actual_qty") or item.get("count"))

    act_pallets: list[dict] = []
    for label in pallet_order:
        pallet_entry = pallets_by_label[label]
        pallet_entry["box_count"] = len(pallet_entry.get("boxes") or [])
        act_pallets.append(pallet_entry)

    return {
        "act": "shipping_packing",
        "act_state": "closed",
        "act_boxes": act_boxes,
        "act_pallets": act_pallets,
        "act_items": list(item_totals.values()),
        "delivered_box_count": len(act_boxes),
        "pallet_count": len(act_pallets),
    }


def _shipping_detach_unassigned_palletization_snapshots(
    *,
    operation,
    keep_snapshot_ids: set[int],
    user,
) -> int:
    keep_ids = {int(value) for value in keep_snapshot_ids if int(value or 0) > 0}
    if not keep_ids:
        return 0

    from sklad.services.warehouse_transitions import WarehouseEventType

    now = timezone.now()
    detached_qty = 0
    snapshots = list(
        WarehouseStockSnapshot.objects.select_for_update()
        .filter(active_operation=operation, is_archived=False)
        .exclude(id__in=keep_ids)
        .order_by("id")
    )
    for snapshot in snapshots:
        previous_event = snapshot.last_event
        event = WarehouseEvent.objects.create(
            agency_id=snapshot.agency_id,
            event_type=WarehouseEventType.WAREHOUSE_CONTEXT_CANCELED.value,
            stock_context_type="manual_repair",
            stock_context_id=f"detach_palletization_{operation.context_id}_{operation.id}",
            container_id=snapshot.container_id,
            operation=operation,
            source_document_type=snapshot.source_context_type,
            source_document_id=str(snapshot.source_context_id or "").strip(),
            from_location_id=snapshot.location_id,
            to_location_id=snapshot.location_id,
            from_zone_code=str(snapshot.zone_code or "").strip(),
            to_zone_code=str(snapshot.zone_code or "").strip(),
            qty=int(snapshot.qty or 0),
            performed_by=user if getattr(user, "is_authenticated", False) else None,
            performed_by_role="storekeeper",
            occurred_at=now,
            payload={
                "reason": "snapshot was not included in shipping packing act",
                "operation_context_type": str(operation.context_type or "").strip(),
                "operation_context_id": str(operation.context_id or "").strip(),
                "previous_last_event_id": previous_event.id if previous_event else None,
                "previous_last_event_type": previous_event.event_type if previous_event else "",
            },
        )
        if str(snapshot.warehouse_state_code or "").strip() == WarehouseStateCode.PALLETIZING.value:
            snapshot.warehouse_state_code = WarehouseStateCode.IN_OTG.value
        snapshot.active_operation = None
        snapshot.active_operation_type = ""
        snapshot.last_event = event
        snapshot.snapshot_version = int(snapshot.snapshot_version or 0) + 1
        snapshot.updated_at = now
        snapshot.save(
            update_fields=[
                "warehouse_state_code",
                "active_operation",
                "active_operation_type",
                "last_event",
                "snapshot_version",
                "updated_at",
            ]
        )
        detached_qty += int(snapshot.qty or 0)

    if detached_qty > 0:
        operation.planned_qty = max(int(operation.planned_qty or 0) - detached_qty, 0)
        operation.save(update_fields=["planned_qty", "updated_at"])

    return len(snapshots)


def _shipping_pallet_label_from_code(container_code: str | None) -> str:
    raw = str(container_code or "").strip()
    if not raw:
        return "-"
    match = _SHIPPING_PALLET_LABEL_RE.match(raw)
    if not match:
        return raw
    suffix = str(match.group(2) or "").strip("-")
    if suffix:
        return suffix
    return str(match.group(1) or "").strip() or raw


def _shipping_packing_summary_from_warehouse(order: ShippingOrder) -> dict | None:
    order_key = str(order.number or "").strip()
    if not order_key:
        return None
    summary_states = {
        WarehouseStateCode.READY_FOR_LOADING.value,
        WarehouseStateCode.ASSIGNED_TO_TRIP.value,
        WarehouseStateCode.LOADING_IN_PROGRESS.value,
        WarehouseStateCode.LOADED_TO_VEHICLE.value,
        WarehouseStateCode.SHIPPED.value,
    }
    snapshots = sorted(
        (
            snapshot
            for snapshot in _shipping_flow_snapshots(order)
            if str(snapshot.warehouse_state_code or "").strip() in summary_states
            and (
                order.status == ShippingOrder.STATUS_CANCELED
                or (
                    snapshot.last_event is not None
                    and str(snapshot.last_event.stock_context_type or "").strip() == "shipping"
                    and str(snapshot.last_event.stock_context_id or "").strip() == order_key
                )
            )
            and snapshot.parent_container is not None
            and str(snapshot.parent_container.source_context_type or "").strip() == "shipping"
            and str(snapshot.parent_container.source_context_id or "").strip() == order_key
        ),
        key=lambda snapshot: (
            str(snapshot.parent_container.container_code or ""),
            int(snapshot.id or 0),
        ),
    )
    if not snapshots:
        return None

    pallets_by_code: dict[str, dict] = {}
    box_keys: set[tuple[str, str]] = set()
    for snapshot in snapshots:
        pallet = snapshot.parent_container
        if pallet is None:
            continue
        pallet_code = str(pallet.container_code or "").strip()
        if not pallet_code:
            continue
        pallet_entry = pallets_by_code.setdefault(
            pallet_code,
            {
                "label": _shipping_pallet_label_from_code(pallet_code),
                "code": pallet_code,
                "source_code": pallet_code,
                "box_count": 0,
                "qty": 0,
                "boxes": [],
            },
        )
        container = snapshot.container
        box_code = ""
        if container is not None and str(container.container_type or "").strip() == WarehouseContainer.TYPE_BOX:
            box_code = str(container.container_code or "").strip()
        elif str(snapshot.container_code or "").strip():
            box_code = str(snapshot.container_code or "").strip()
        item_qty = _warehouse_shipping_snapshot_qty(snapshot)
        pallet_entry["qty"] += item_qty
        if box_code:
            box_key = (pallet_code, box_code.lower())
            if box_key not in box_keys:
                box_keys.add(box_key)
                pallet_entry["boxes"].append(
                    {
                        "row_key": _shipping_box_row_key(
                            {
                                "code": box_code,
                                "receiving_order_id": str(snapshot.source_context_id or "").strip(),
                            },
                            len(pallet_entry["boxes"]) + 1,
                        ),
                        "box_code": box_code,
                        "qty": 0,
                        "barcode_preview": "",
                        "pallet_label": pallet_entry["label"],
                    }
                )
                pallet_entry["box_count"] += 1
            for box in pallet_entry["boxes"]:
                if str(box.get("box_code") or "").strip().lower() == box_code.lower():
                    box["qty"] = int(box.get("qty") or 0) + item_qty
                    break

    pallets = list(pallets_by_code.values())
    if not pallets:
        return None
    return {
        "pallet_count": len(pallets),
        "box_count": sum(int(pallet.get("box_count") or 0) for pallet in pallets),
        "pallets": pallets,
        "entry": None,
    }


def _shipping_packing_summary(order: ShippingOrder) -> dict | None:
    warehouse_summary = _shipping_packing_summary_from_warehouse(order)
    if warehouse_summary:
        return warehouse_summary
    if _shipping_flow_snapshots(order):
        return None
    entry = _shipping_packing_entry(order)
    if not entry or not isinstance(entry.payload, dict):
        return None
    payload = dict(entry.payload or {})
    raw_pallets = payload.get("act_pallets") or []
    raw_boxes = payload.get("act_boxes") or []
    if not isinstance(raw_pallets, list) or not isinstance(raw_boxes, list):
        return None
    boxes_by_key: dict[str, dict] = {}
    boxes_by_code: dict[str, list[dict]] = {}
    for index, box in enumerate(raw_boxes, start=1):
        if not isinstance(box, dict):
            continue
        code = str(box.get("code") or "").strip()
        if not code:
            continue
        items = [dict(item) for item in (box.get("items") or []) if isinstance(item, dict)]
        box_qty = _to_int(box.get("qty")) or _shipping_box_qty(items)
        if box_qty <= 0:
            continue
        row_key = _shipping_box_row_key(box, index)
        box_data = {
            "row_key": row_key,
            "box_code": code,
            "qty": box_qty,
            "barcode_preview": str(box.get("barcode_preview") or _shipping_box_items_preview(items)),
            "pallet_label": str(box.get("pallet_label") or "").strip(),
        }
        boxes_by_key[row_key] = box_data
        boxes_by_code.setdefault(code, []).append(box_data)
    pallets = []
    for pallet in raw_pallets:
        if not isinstance(pallet, dict):
            continue
        label = str(pallet.get("label") or pallet.get("code") or "").strip()
        code = str(pallet.get("container_code") or pallet.get("code") or "").strip()
        pallet_boxes = []
        legacy_box_map = {key: list(rows) for key, rows in boxes_by_code.items()}
        for box_ref in pallet.get("boxes") or []:
            ref = str(box_ref or "").strip()
            box_data = boxes_by_key.get(ref)
            if not box_data:
                fallback_rows = legacy_box_map.get(ref) or []
                box_data = fallback_rows.pop(0) if fallback_rows else None
            if box_data:
                pallet_boxes.append(box_data)
        pallet_qty = sum(int(box.get("qty") or 0) for box in pallet_boxes)
        if not pallet_boxes or pallet_qty <= 0:
            continue
        pallets.append(
            {
                "label": label or code or "-",
                "code": code,
                "source_code": str(pallet.get("source_code") or pallet.get("code") or "").strip(),
                "box_count": len(pallet_boxes),
                "qty": pallet_qty,
                "boxes": pallet_boxes,
            }
        )
    return {
        "pallet_count": len(pallets),
        "box_count": len(boxes_by_key),
        "pallets": pallets,
        "entry": entry,
    }



def _shipping_act_box_qty_by_code(act_data: dict) -> dict[str, int]:
    qty_by_code: dict[str, int] = {}
    for raw_box in (act_data.get("act_boxes") or []) if isinstance(act_data, dict) else []:
        if not isinstance(raw_box, dict):
            continue
        code = str(raw_box.get("code") or "").strip().lower()
        if not code:
            continue
        qty = _to_int(raw_box.get("qty")) or _shipping_box_qty(raw_box.get("items") or [])
        qty_by_code[code] = qty_by_code.get(code, 0) + int(max(qty, 0))
    return qty_by_code


def _shipping_task_box_codes(payload: dict | None) -> list[str]:
    if not isinstance(payload, dict):
        return []
    codes: list[str] = []

    def add_code(value) -> None:
        if isinstance(value, dict):
            add_code(value.get("box_code") or value.get("code"))
            return
        code = str(value or "").strip()
        if code and code not in codes:
            codes.append(code)

    for key in (
        "selected_box_codes",
        "reserved_box_codes",
        "planned_box_codes",
        "requested_box_codes",
        "picked_boxes",
        "boxes",
        "planned_boxes",
        "reserved_boxes",
        "requested_boxes",
    ):
        value = payload.get(key)
        if isinstance(value, (list, tuple)):
            for item in value:
                add_code(item)
        else:
            add_code(value)
    for key in (
        "selected_box_code",
        "reserved_box_code",
        "planned_box_code",
        "requested_box_code",
        "box_code",
    ):
        add_code(payload.get(key))
    return codes


def _shipping_auto_complete_otg_reachtruck_tasks(
    order: ShippingOrder,
    *,
    act_data: dict,
    user,
) -> list[int]:
    qty_by_code = _shipping_act_box_qty_by_code(act_data)
    packed_codes = set(qty_by_code)
    if not packed_codes:
        return []

    from reachtruck.models import BoxClaim, MoveRequest, MoveTask, PalletLock
    from reachtruck.services.move_requests import _recompute_request_status

    now = timezone.now()
    order_ids = {
        str(order.number or "").strip(),
        str(getattr(order, "display_number", "") or "").strip(),
        str(order.pk or "").strip(),
    }
    order_ids = {value for value in order_ids if value}
    final_statuses = {MoveTask.STATUS_DONE, MoveTask.STATUS_CANCELED, MoveTask.STATUS_FAILED}
    completed_task_ids: list[int] = []
    request_ids: set[int] = set()

    tasks = (
        MoveTask.objects.select_for_update()
        .select_related("request")
        .filter(request__agency=order.agency, to_zone__iexact="OTG")
        .exclude(status__in=final_statuses)
        .order_by("id")
    )
    for task in tasks:
        payload = dict(task.payload or {})
        if payload.get("otg_scan_fact_mode") == "scan_facts_v1":
            continue
        payload_order_ids = {
            str(getattr(task.request, "context_id", "") or "").strip(),
            str(task.legacy_order_id or "").strip(),
            str(payload.get("shipping_order_id") or "").strip(),
            str(payload.get("shipping_order_number") or "").strip(),
            str(payload.get("source_document_id") or "").strip(),
            str(payload.get("order_id") or "").strip(),
            str(payload.get("order_pk") or "").strip(),
            str(payload.get("shipping_order_pk") or "").strip(),
        }
        payload_order_ids = {value for value in payload_order_ids if value}
        has_order_claim = BoxClaim.objects.filter(
            move_task=task,
        ).filter(
            shipping_order_pk=order.pk,
        ).exists() or BoxClaim.objects.filter(
            move_task=task,
            shipping_order_id__in=order_ids,
        ).exists()
        if not (payload_order_ids & order_ids or has_order_claim):
            continue

        task_codes = _shipping_task_box_codes(payload)
        task_code_keys = {code.lower() for code in task_codes if code}
        if not task_code_keys or not task_code_keys.issubset(packed_codes):
            continue

        picked_rows = [
            {"box_code": code, "qty": int(qty_by_code.get(code.lower(), 0) or 0)}
            for code in task_codes
            if code.lower() in packed_codes
        ]
        picked_qty = sum(int(row.get("qty") or 0) for row in picked_rows)
        planned_qty = max(int(task.qty_planned or 0), 0)
        if planned_qty > 0 and picked_qty < planned_qty:
            continue

        picked_box_codes = [row["box_code"] for row in picked_rows]
        payload["picked_boxes"] = picked_box_codes
        payload["picked_rows"] = picked_rows
        payload["picked_qty"] = picked_qty or planned_qty
        payload["auto_completed_by_shipping_packing"] = True
        payload["auto_completed_reason"] = "box already packed on OTG pallet"
        payload["auto_completed_at"] = now.isoformat()
        payload["otg_completion_fact_mode"] = "shipping_packing_v1"
        payload["shipping_packing_fact"] = {
            "shipping_order_id": int(order.pk),
            "shipping_order_number": str(order.number or "").strip(),
            "requested_box_codes": list(task_codes),
            "box_codes": picked_box_codes,
            "packed_qty_by_code": {
                row["box_code"]: int(row.get("qty") or 0)
                for row in picked_rows
            },
            "picked_qty": int(payload["picked_qty"] or 0),
            "confirmed_at": now.isoformat(),
        }
        execution = dict(payload.get("mobile_execution") or {})
        execution.setdefault("source_confirmed", True)
        execution["pallet_confirmed"] = True
        execution["destination_confirmed"] = True
        payload["mobile_execution"] = execution

        if task.started_at is None:
            task.started_at = now
        task.completed_at = now
        task.status = MoveTask.STATUS_DONE
        task.qty_done = int(task.qty_planned or 0) or int(payload["picked_qty"] or 0)
        task.payload = payload
        task.save(update_fields=["status", "qty_done", "payload", "started_at", "completed_at", "updated_at"])

        BoxClaim.objects.filter(move_task=task, status=BoxClaim.STATUS_CLAIMED).update(
            status=BoxClaim.STATUS_DELIVERED,
            delivered_at=now,
            updated_at=now,
        )
        PalletLock.objects.filter(move_task=task, status=PalletLock.STATUS_ACTIVE).update(
            status=PalletLock.STATUS_RELEASED,
            released_at=now,
            updated_at=now,
        )
        completed_task_ids.append(int(task.id))
        request_ids.add(int(task.request_id))

    for request_id in sorted(request_ids):
        move_request = MoveRequest.objects.get(pk=request_id)
        _recompute_request_status(move_request)

    return completed_task_ids


def _shipping_packing_barcode_order_number(order: ShippingOrder) -> str:
    display_number = str(getattr(order, "display_number", "") or "").strip()
    if not display_number:
        raw_number = str(order.number or "").strip()
        match = re.fullmatch(r"SO-0*(\d+)", raw_number, flags=re.IGNORECASE)
        display_number = f"{int(match.group(1))}_OTG" if match else raw_number
    display_number = re.sub(r"[^A-Z0-9_-]+", "", display_number.upper())
    if display_number and not display_number.endswith("_OTG"):
        display_number = f"{display_number}_OTG"
    return display_number or "OTG"


def shipping_order_generated_packing_barcode(order: ShippingOrder) -> str:
    agency_prefix = re.sub(
        r"[^A-Z0-9]+",
        "",
        str(getattr(order.agency, "pref", "") or "").upper(),
    ) or "FB"
    return f"{agency_prefix}-{_shipping_packing_barcode_order_number(order)}-000000"


def shipping_order_effective_packing_barcode(order: ShippingOrder, marketplace_name: str | None = None) -> str:
    marketplace_label = str(
        marketplace_name
        if marketplace_name is not None
        else getattr(getattr(order, "marketplace", None), "name", "")
        or ""
    ).strip()
    delivery_type = str(getattr(order, "delivery_type", "") or "").strip()
    raw_barcode = str(getattr(order, "shipping_barcode", "") or "").strip()
    if (
        raw_barcode
        and raw_barcode != "-"
        and "ozon" not in marketplace_label.casefold()
        and delivery_type != ShippingOrder.DELIVERY_PICKUP
    ):
        return raw_barcode
    return shipping_order_generated_packing_barcode(order)


def _shipping_packing_barcode(order: ShippingOrder, marketplace_name: str) -> str:
    return shipping_order_effective_packing_barcode(order, marketplace_name)


def _shipping_packing_supplier_label(value: str) -> str:
    label = re.sub(r"\s+", " ", str(value or "").strip())
    if not label:
        return ""
    label = re.sub(r"^Индивидуальный предприниматель\b", "ИП", label, flags=re.IGNORECASE)
    label = re.sub(r"^Общество с ограниченной ответственностью\b", "ООО", label, flags=re.IGNORECASE)
    ip_match = re.match(r"^ИП\s+([^\s]+)(?:\s+([^\s.]+))?(?:\s+([^\s.]+))?", label, flags=re.IGNORECASE)
    if ip_match:
        surname, first_name, middle_name = ip_match.groups()
        initials = "".join(f" {part[0].upper()}." for part in (first_name, middle_name) if part)
        return f"ИП {surname}{initials}".strip()
    return label


def _shipping_packing_supplier_name(order: ShippingOrder) -> str:
    agency = order.agency
    for value in (
        getattr(agency, "short_name", ""),
        getattr(agency, "agn_name", ""),
    ):
        label = _shipping_packing_supplier_label(str(value or ""))
        if label:
            return label
    return "-"


def shipping_packing_slip_meta(order: ShippingOrder) -> dict:
    delivery_date = order.slot_date.strftime("%d.%m.%Y") if order.slot_date else "-"
    marketplace_name = str(getattr(order.marketplace, "name", "") or "").strip() or "-"
    return {
        "order_number": str(order.number or "").strip(),
        "marketplace_name": marketplace_name,
        "supply_type": order.get_supply_type_display() or "-",
        "shipping_barcode": _shipping_packing_barcode(order, marketplace_name),
        "supply_number": str(order.wb_supply_barcode or "").strip() or "-",
        "destination_warehouse": str(order.destination_warehouse or "").strip() or "-",
        "transit_address": str(order.transit_address or "").strip() if order.wb_transit_warehouse else "",
        "supplier_name": _shipping_packing_supplier_name(order),
        "delivery_date": delivery_date,
    }


def shipping_packing_slips_data(
    order: ShippingOrder,
    packing_summary: dict | None = None,
) -> list[dict]:
    summary = packing_summary if isinstance(packing_summary, dict) else _shipping_packing_summary(order)
    if not summary:
        return []
    meta = shipping_packing_slip_meta(order)
    pallets = list(summary.get("pallets") or [])
    total_pallets = len(pallets)
    slips: list[dict] = []
    for index, pallet in enumerate(pallets, start=1):
        pallet_data = dict(pallet or {})
        pallet_label = str(pallet_data.get("label") or pallet_data.get("code") or index).strip() or str(index)
        pallet_code = str(pallet_data.get("code") or "").strip()
        boxes = list(pallet_data.get("boxes") or [])
        slips.append(
            {
                "slip_key": pallet_code or f"PAL-{index}",
                "pallet_index": str(index),
                "pallet_label": pallet_label,
                "pallet_counter": f"{index} из {total_pallets}",
                "pallet_code": pallet_code,
                "box_count": str(len(boxes)),
                "total_pallets": str(total_pallets),
                "marketplace_name": str(meta.get("marketplace_name") or "-").upper(),
                "supply_type": str(meta.get("supply_type") or "-"),
                "supply_number": str(meta.get("supply_number") or "-"),
                "shipping_barcode": str(meta.get("shipping_barcode") or "-"),
                "destination_warehouse": str(meta.get("destination_warehouse") or "-"),
                "transit_address": str(meta.get("transit_address") or "-"),
                "supplier_name": str(meta.get("supplier_name") or "-"),
                "delivery_date": str(meta.get("delivery_date") or "-"),
            }
        )
    return slips


def _shipping_packing_initial_state_from_draft(delivered_boxes: list[dict], draft_state: dict | None) -> dict | None:
    if not isinstance(draft_state, dict):
        return None
    delivered_by_code = {
        str(box.get("box_code") or box.get("code") or "").strip().lower(): box
        for box in delivered_boxes or []
        if str(box.get("box_code") or box.get("code") or "").strip()
    }
    initial_boxes: list[dict] = []
    for index, raw_box in enumerate(draft_state.get("boxes") or [], start=1):
        if not isinstance(raw_box, dict):
            continue
        box_code = str(raw_box.get("code") or raw_box.get("box_code") or "").strip()
        delivered_box = delivered_by_code.get(box_code.lower())
        if not delivered_box:
            continue
        items = list(delivered_box.get("items") or raw_box.get("items") or [])
        initial_boxes.append(
            {
                "row_key": str(raw_box.get("row_key") or _shipping_box_row_key(delivered_box, index)).strip(),
                "code": box_code,
                "qty": int(delivered_box.get("qty") or raw_box.get("qty") or 0),
                "barcode_preview": str(delivered_box.get("barcode_preview") or raw_box.get("barcode_preview") or "-"),
                "items": items,
                "pallet_code": str(raw_box.get("pallet_code") or "").strip(),
                "ui_index": _to_int(raw_box.get("ui_index")) or index,
            }
        )
    if not initial_boxes:
        return None

    restored_box_codes = {
        str(box.get("code") or "").strip().lower()
        for box in initial_boxes
        if str(box.get("code") or "").strip()
    }
    for delivered_box in delivered_boxes or []:
        box_code = str(delivered_box.get("box_code") or delivered_box.get("code") or "").strip()
        if not box_code or box_code.lower() in restored_box_codes:
            continue
        index = len(initial_boxes) + 1
        initial_boxes.append(
            {
                "row_key": _shipping_box_row_key(delivered_box, index),
                "code": box_code,
                "qty": int(delivered_box.get("qty") or 0),
                "barcode_preview": str(delivered_box.get("barcode_preview") or "-"),
                "items": list(delivered_box.get("items") or []),
                "pallet_code": "",
                "ui_index": index,
            }
        )
        restored_box_codes.add(box_code.lower())

    used_pallets: list[str] = []
    for box in initial_boxes:
        pallet_code = str(box.get("pallet_code") or "").strip()
        if pallet_code and pallet_code not in used_pallets:
            used_pallets.append(pallet_code)

    canonical_codes = {
        pallet_code: f"PAL-{index}"
        for index, pallet_code in enumerate(used_pallets, start=1)
        if _SHIPPING_UI_PALLET_KEY_RE.fullmatch(pallet_code)
    }
    for box in initial_boxes:
        pallet_code = str(box.get("pallet_code") or "").strip()
        if pallet_code in canonical_codes:
            box["pallet_code"] = canonical_codes[pallet_code]

    draft_pallets_by_code = {
        str(raw_pallet.get("code") or "").strip(): raw_pallet
        for raw_pallet in draft_state.get("pallets") or []
        if isinstance(raw_pallet, dict) and str(raw_pallet.get("code") or "").strip()
    }
    initial_pallets: list[dict] = []
    for index, source_code in enumerate(used_pallets, start=1):
        raw_pallet = draft_pallets_by_code.get(source_code) or {}
        code = canonical_codes.get(source_code, source_code)
        label = str(raw_pallet.get("label") or source_code).strip()
        if source_code in canonical_codes:
            label = f"Паллета {index}"
        initial_pallets.append(
            {
                "code": code,
                "label": label,
                "sealed": bool(raw_pallet.get("sealed")),
            }
        )
    return {"initial_boxes": initial_boxes, "initial_pallets": initial_pallets}


def _shipping_manageable_packing_boxes(
    order: ShippingOrder,
    packing_summary: dict | None = None,
) -> list[dict]:
    cache_key = "_shipping_manageable_packing_boxes_cache"
    cached = getattr(order, cache_key, None)
    if cached is not None:
        return cached
    delivered_boxes = _shipping_delivered_boxes(order)
    setattr(order, cache_key, delivered_boxes)
    return delivered_boxes


def _shipping_packing_initial_state(
    order: ShippingOrder,
    packing_summary: dict | None = None,
) -> dict:
    summary = packing_summary if isinstance(packing_summary, dict) else _shipping_packing_summary(order)
    delivered_boxes = _shipping_manageable_packing_boxes(order, summary)
    draft_state = _shipping_packing_initial_state_from_draft(delivered_boxes, shipping_packing_saved_draft_state(order))
    if draft_state:
        return {
            "packing_summary": summary,
            "delivered_boxes": delivered_boxes,
            "initial_boxes": draft_state["initial_boxes"],
            "initial_pallets": draft_state["initial_pallets"],
        }

    existing_assignments_by_row: dict[str, str] = {}
    existing_assignments_by_code: dict[str, str] = {}
    if summary:
        for pallet in summary.get("pallets") or []:
            label = str((pallet or {}).get("label") or "").strip()
            for box_index, box in enumerate((pallet or {}).get("boxes") or [], start=1):
                box_code = str((box or {}).get("box_code") or "").strip()
                if not box_code or not label:
                    continue
                row_key = _shipping_box_row_key(box, box_index)
                if row_key:
                    existing_assignments_by_row[row_key] = label
                existing_assignments_by_code.setdefault(box_code, label)

    initial_boxes: list[dict] = []
    for index, box in enumerate(delivered_boxes, start=1):
        row_key = _shipping_box_row_key(box, index)
        box_code = str(box.get("box_code") or "").strip()
        source_pallet_code = str(box.get("pallet_code") or "").strip()
        assigned_label = (
            existing_assignments_by_row.get(row_key)
            or existing_assignments_by_code.get(box_code, "")
            or str(box.get("pallet_label") or "").strip()
            or (_shipping_pallet_label_from_code(source_pallet_code) if source_pallet_code else "")
        )
        initial_boxes.append(
            {
                "row_key": row_key,
                "code": box_code,
                "qty": int(box.get("qty") or 0),
                "barcode_preview": str(box.get("barcode_preview") or "-"),
                "items": list(box.get("items") or []),
                "pallet_code": assigned_label,
                "ui_index": index,
            }
        )

    initial_pallets: list[dict] = []
    seen_labels: set[str] = set()
    for box in initial_boxes:
        label = _normalize_shipping_pallet_label(box.get("pallet_code"))
        if not label or label in seen_labels:
            continue
        seen_labels.add(label)
        initial_pallets.append({"code": label, "label": label})

    return {
        "packing_summary": summary,
        "delivered_boxes": delivered_boxes,
        "initial_boxes": initial_boxes,
        "initial_pallets": initial_pallets,
    }


def _shipping_loose_packing_initial_state(
    order: ShippingOrder,
    *,
    repack_existing_boxes: bool = False,
) -> dict:
    loose_items = (
        _shipping_repack_items(order)
        if repack_existing_boxes
        else _shipping_loose_items(order)
    )
    source_box_codes = [
        str(row.get("box_code") or "").strip()
        for row in _shipping_delivered_boxes(order)
        if str(row.get("box_code") or "").strip()
    ]
    existing_codes = {
        code
        for code in source_box_codes
    }
    existing_codes.update(
        str(code or "").strip()
        for code in WarehouseContainer.objects.filter(agency=order.agency).values_list("container_code", flat=True)
        if str(code or "").strip()
    )
    return {
        "loose_items": loose_items,
        "existing_box_codes": sorted(existing_codes, key=lambda value: value.lower()),
        "box_code_base": _shipping_loose_box_code_base(order),
        "repack_existing_boxes": bool(repack_existing_boxes),
        "source_box_codes": source_box_codes if repack_existing_boxes else [],
    }


def _shipping_build_loose_packing_act(order: ShippingOrder, boxes: list[dict], pallets: list[dict]) -> dict:
    act_boxes: list[dict] = []
    pallets_by_code = {
        str(pallet.get("code") or "").strip(): dict(pallet)
        for pallet in pallets
        if isinstance(pallet, dict) and str(pallet.get("code") or "").strip()
    }
    act_pallets_by_code: dict[str, dict] = {}
    item_totals: dict[str, dict] = {}
    for raw_box in boxes:
        if not isinstance(raw_box, dict):
            continue
        box_code = str(raw_box.get("code") or "").strip()
        pallet_code = str(raw_box.get("pallet_code") or "").strip()
        pallet = pallets_by_code.get(pallet_code) if pallet_code else None
        items = [dict(item) for item in (raw_box.get("items") or []) if isinstance(item, dict)]
        box_qty = _shipping_box_qty(items)
        if not box_code or box_qty <= 0:
            continue
        pallet_label = str((pallet or {}).get("label") or pallet_code).strip()
        pallet_container_code = str((pallet or {}).get("container_code") or "").strip()
        act_pallet_code = pallet_container_code or pallet_code
        act_boxes.append(
            {
                "code": box_code,
                "gm_barcode": str(raw_box.get("gm_barcode") or "").strip(),
                "gm_supply_id": str(raw_box.get("gm_supply_id") or "").strip(),
                "gm_bundle_id": str(raw_box.get("gm_bundle_id") or "").strip(),
                "qty": box_qty,
                "items": items,
                "barcode_preview": _shipping_box_items_preview(items),
                "pallet_code": act_pallet_code,
                "pallet_label": pallet_label,
                "pallet_source_code": pallet_code,
                "pallet_container_code": pallet_container_code,
                "location": {"zone": "OTG"},
                "source": "loose_goods",
                "sealed": True,
            }
        )
        if pallet_code:
            pallet_entry = act_pallets_by_code.setdefault(
                pallet_code,
                {
                    "code": act_pallet_code,
                    "label": pallet_label,
                    "source_code": pallet_code,
                    "container_code": pallet_container_code,
                    "boxes": [],
                    "qty": 0,
                    "box_count": 0,
                    "sealed": bool((pallet or {}).get("sealed")),
                    "location": {"zone": "OTG"},
                },
            )
            pallet_entry["boxes"].append(box_code)
            pallet_entry["qty"] = int(pallet_entry.get("qty") or 0) + box_qty
            pallet_entry["box_count"] = len(pallet_entry.get("boxes") or [])
        for item in items:
            key = _shipping_item_key_from_payload(item)
            if not key:
                continue
            row = item_totals.setdefault(
                key,
                {
                    "sku_code": str(item.get("sku_code") or "").strip(),
                    "name": str(item.get("name") or "").strip(),
                    "size": str(item.get("size") or "").strip(),
                    "barcode": str(item.get("barcode") or "").strip(),
                    "goods_type": str(item.get("goods_type") or "").strip(),
                    "qty": 0,
                },
            )
            row["qty"] = int(row.get("qty") or 0) + _to_int(item.get("qty"))
    return {
        "act": "shipping_loose_packing",
        "act_state": "closed",
        "act_boxes": act_boxes,
        "act_pallets": list(act_pallets_by_code.values()),
        "act_items": list(item_totals.values()),
        "box_count": len(act_boxes),
        "pallet_count": len(act_pallets_by_code),
        "qty": sum(int(box.get("qty") or 0) for box in act_boxes),
    }


def _shipping_normalize_loose_packing_boxes(
    boxes_state: list,
    expected_by_key: dict[str, dict],
    *,
    gm_targets: list[dict] | None = None,
) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    prepared_boxes: list[dict] = []
    seen_box_codes: set[str] = set()
    seen_gm_barcodes: set[str] = set()
    packed_totals: dict[str, int] = {}
    seen_marking_snapshot_ids: set[int] = set()
    seen_marking_codes: set[str] = set()
    seen_marking_identities: set[str] = set()
    gm_required = gm_targets is not None
    gm_targets_by_code = {
        str(target.get("gm_barcode") or "").strip().casefold(): dict(target)
        for target in (gm_targets or [])
        if isinstance(target, dict) and str(target.get("gm_barcode") or "").strip()
    }
    if not isinstance(boxes_state, list):
        boxes_state = []

    for raw_box in boxes_state:
        if not isinstance(raw_box, dict):
            continue
        box_code = str(raw_box.get("code") or "").strip()
        if not box_code:
            errors.append("Укажите код для каждого нового короба.")
            continue
        if box_code.lower() in seen_box_codes:
            errors.append(f"Код короба {box_code} указан несколько раз.")
            continue
        seen_box_codes.add(box_code.lower())
        if not raw_box.get("sealed"):
            errors.append(f"Короб {box_code} нужно закрыть перед завершением упаковки.")
        gm_barcode = str(raw_box.get("gm_barcode") or "").strip()
        gm_target = gm_targets_by_code.get(gm_barcode.casefold()) if gm_barcode else None
        existing_box_code = str((gm_target or {}).get("existing_box_code") or "").strip()
        if gm_required:
            if not gm_barcode:
                errors.append(f"Для короба {box_code} не указан ШК ГМ Ozon.")
            elif gm_target is None:
                errors.append(f"ШК ГМ {gm_barcode} не входит в план этой заявки.")
            elif gm_barcode.casefold() in seen_gm_barcodes:
                errors.append(f"ШК ГМ {gm_barcode} назначен нескольким коробам.")
            else:
                seen_gm_barcodes.add(gm_barcode.casefold())
            if existing_box_code and box_code.casefold() != existing_box_code.casefold():
                errors.append(
                    f"ШК ГМ {gm_barcode} нужно дополнить в существующем коробе "
                    f"{existing_box_code}, а не в коробе {box_code}."
                )
        prepared_items: list[dict] = []
        for raw_item in raw_box.get("items") or []:
            if not isinstance(raw_item, dict):
                continue
            item_qty = _to_int(raw_item.get("qty") or raw_item.get("actual_qty") or raw_item.get("count"))
            if item_qty <= 0:
                continue
            item_key = _shipping_item_key_from_payload(raw_item)
            expected = expected_by_key.get(item_key)
            if expected is None:
                errors.append(f"В коробе {box_code} указан товар, которого нет среди товара без короба.")
                continue
            prepared_item = {
                "key": item_key,
                "sku_code": str(expected.get("sku_code") or "").strip(),
                "name": str(expected.get("name") or "").strip(),
                "size": str(expected.get("size") or "").strip(),
                "barcode": str(expected.get("barcode") or "").strip(),
                "goods_type": str(expected.get("goods_type") or "").strip(),
                "qty": item_qty,
                "requires_marking_scan": bool(expected.get("requires_marking_scan")),
                "marking_preverified": bool(expected.get("marking_preverified")),
            }
            if prepared_item["marking_preverified"]:
                if prepared_item["requires_marking_scan"]:
                    errors.append(
                        f"Для товара {prepared_item['barcode'] or prepared_item['sku_code']} "
                        "получены противоречивые правила Честного знака. Обновите страницу."
                    )
                prepared_item["preverified_snapshot_ids"] = [
                    _to_int(snapshot_id)
                    for snapshot_id in expected.get("snapshot_ids") or []
                    if _to_int(snapshot_id) > 0
                ]
            if prepared_item["requires_marking_scan"]:
                marking_units: list[dict] = []
                for raw_unit in raw_item.get("marking_units") or []:
                    if not isinstance(raw_unit, dict):
                        continue
                    snapshot_id = _to_int(raw_unit.get("snapshot_id"))
                    unit_barcode = str(raw_unit.get("barcode") or "").strip()
                    marking_code = normalize_marking_code(raw_unit.get("marking_code"))
                    marking_identity = marking_code_identity(marking_code)
                    if (
                        unit_barcode != prepared_item["barcode"]
                        or not marking_code
                        or not marking_identity
                    ):
                        errors.append(
                            f"В коробе {box_code} есть некорректный парный скан для товара "
                            f"{prepared_item['barcode'] or prepared_item['sku_code']}."
                        )
                        continue
                    if (
                        (snapshot_id > 0 and snapshot_id in seen_marking_snapshot_ids)
                        or marking_code in seen_marking_codes
                        or marking_identity in seen_marking_identities
                    ):
                        errors.append(f"Data Matrix {marking_code} использован повторно.")
                        continue
                    if snapshot_id > 0:
                        seen_marking_snapshot_ids.add(snapshot_id)
                    seen_marking_codes.add(marking_code)
                    seen_marking_identities.add(marking_identity)
                    marking_units.append(
                        {
                            "snapshot_id": snapshot_id,
                            "barcode": unit_barcode,
                            "marking_code": marking_code,
                            "marking_identity": marking_identity,
                        }
                    )
                prepared_item["marking_units"] = marking_units
                if len(marking_units) != item_qty:
                    errors.append(
                        f"Для товара {prepared_item['barcode'] or prepared_item['sku_code']} "
                        f"в коробе {box_code} отсканировано Data Matrix: "
                        f"{len(marking_units)} из {item_qty}."
                    )
            prepared_items.append(prepared_item)
            packed_totals[item_key] = int(packed_totals.get(item_key) or 0) + item_qty
        if not prepared_items:
            errors.append(f"Короб {box_code} пустой.")
        if gm_required and gm_target is not None:
            actual_signature = tuple(
                sorted(
                    (str(item.get("key") or ""), int(item.get("qty") or 0))
                    for item in prepared_items
                    if str(item.get("key") or "") and int(item.get("qty") or 0) > 0
                )
            )
            target_signature = tuple(
                sorted(
                    (str(item.get("key") or ""), int(item.get("qty") or 0))
                    for item in gm_target.get("items") or []
                    if isinstance(item, dict)
                    and str(item.get("key") or "")
                    and int(item.get("qty") or 0) > 0
                )
            )
            if actual_signature != target_signature:
                errors.append(
                    f"Состав короба {box_code} не совпадает с ШК ГМ {gm_barcode}. "
                    "Автоматическая замена запрещена."
                )
        prepared_boxes.append(
            {
                "code": box_code,
                "gm_barcode": gm_barcode if gm_target is not None else "",
                "gm_supply_id": str((gm_target or {}).get("supply_id") or "").strip(),
                "gm_bundle_id": str((gm_target or {}).get("bundle_id") or "").strip(),
                "existing_box_code": existing_box_code,
                "items": prepared_items,
                "pallet_code": str(raw_box.get("pallet_code") or "").strip(),
                "sealed": bool(raw_box.get("sealed")),
            }
        )

    if not prepared_boxes:
        errors.append("Нет закрытых коробов с товаром без короба.")
    if gm_required:
        missing_gm = [
            str(target.get("gm_barcode") or "").strip()
            for target in (gm_targets or [])
            if isinstance(target, dict)
            and str(target.get("gm_barcode") or "").strip().casefold() not in seen_gm_barcodes
        ]
        if missing_gm:
            errors.append(f"Не сформированы короба для ШК ГМ: {', '.join(missing_gm)}.")
    for item_key, expected in expected_by_key.items():
        expected_qty = _to_int(expected.get("qty"))
        packed_qty = int(packed_totals.get(item_key) or 0)
        if packed_qty != expected_qty:
            label = str(expected.get("barcode") or expected.get("sku_code") or "-").strip() or "-"
            errors.append(f"По товару {label} упаковано {packed_qty} из {expected_qty}.")
    return prepared_boxes, errors


def _shipping_normalize_loose_packing_pallets(pallets_state: list, boxes: list[dict]) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    if not isinstance(pallets_state, list):
        pallets_state = []
    used_pallet_codes = {
        str(box.get("pallet_code") or "").strip()
        for box in boxes
        if str(box.get("code") or "").strip() and str(box.get("pallet_code") or "").strip()
    }
    prepared: list[dict] = []
    seen_codes: set[str] = set()
    for raw_pallet in pallets_state:
        if not isinstance(raw_pallet, dict):
            continue
        code = str(raw_pallet.get("code") or "").strip()
        label = str(raw_pallet.get("label") or code).strip()
        if not code or code not in used_pallet_codes:
            continue
        if code.lower() in seen_codes:
            errors.append(f"Паллета {label or code} указана несколько раз.")
            continue
        seen_codes.add(code.lower())
        if not label:
            errors.append("Укажите название каждой паллеты.")
            continue
        if not raw_pallet.get("sealed"):
            errors.append(f"Паллету {label} нужно закрыть перед завершением упаковки.")
        prepared.append(
            {
                "code": code,
                "label": label,
                "sealed": bool(raw_pallet.get("sealed")),
                "container_code": "",
            }
        )
    prepared_codes = {str(pallet.get("code") or "").strip() for pallet in prepared}
    for box in boxes:
        box_code = str(box.get("code") or "").strip()
        pallet_code = str(box.get("pallet_code") or "").strip()
        if not box_code:
            continue
        if not pallet_code:
            continue
        elif pallet_code not in prepared_codes:
            errors.append(f"Для короба {box_code} выбрана неизвестная паллета.")
    return prepared, errors


def _shipping_ensure_box_container(
    order: ShippingOrder,
    *,
    box_code: str,
    location,
    parent_container: WarehouseContainer | None = None,
    user,
) -> WarehouseContainer:
    box, _created = WarehouseContainer.objects.get_or_create(
        agency=order.agency,
        container_code=box_code,
        defaults={
            "container_type": WarehouseContainer.TYPE_BOX,
            "current_location": location,
            "parent_container": parent_container,
            "created_by": user if getattr(user, "is_authenticated", False) else None,
            "source_context_type": "shipping",
            "source_context_id": str(order.number or "").strip(),
        },
    )
    updates: list[str] = []
    if box.container_type != WarehouseContainer.TYPE_BOX:
        box.container_type = WarehouseContainer.TYPE_BOX
        updates.append("container_type")
    if box.parent_container_id != getattr(parent_container, "id", None):
        box.parent_container = parent_container
        updates.append("parent_container")
    if location is not None and box.current_location_id != location.id:
        box.current_location = location
        updates.append("current_location")
    if str(box.source_context_type or "").strip() != "shipping":
        box.source_context_type = "shipping"
        updates.append("source_context_type")
    if str(box.source_context_id or "").strip() != str(order.number or "").strip():
        box.source_context_id = str(order.number or "").strip()
        updates.append("source_context_id")
    if updates:
        box.save(update_fields=updates + ["updated_at"])
    return box


def _shipping_ensure_loose_pallet_containers(
    order: ShippingOrder,
    *,
    pallets: list[dict],
    location,
    user,
) -> dict[str, WarehouseContainer]:
    containers: dict[str, WarehouseContainer] = {}
    for pallet in pallets:
        code = str(pallet.get("code") or "").strip()
        label = _normalize_shipping_pallet_label(pallet.get("label") or code)
        if not code or not label:
            continue
        container_code = str(pallet.get("container_code") or code).strip()
        if not container_code or _SHIPPING_UI_PALLET_KEY_RE.fullmatch(container_code):
            issued = issue_pallet_code(
                agency=order.agency,
                goods_type="mixed",
                order_type="shipping",
                order_id=order.number,
            )
            container_code = str(issued.get("code") or "").strip()
        pallet["container_code"] = container_code
        container = _shipping_owned_pallet_container(
            order,
            container_code=container_code,
            location=location,
            user=user,
        )
        containers[code] = container
    return containers


def _shipping_assign_loose_snapshots_to_boxes(
    order: ShippingOrder,
    *,
    boxes: list[dict],
    pallets_by_code: dict[str, WarehouseContainer],
    loose_snapshots: list[WarehouseStockSnapshot],
    user,
) -> None:
    snapshots_by_key: dict[str, list[WarehouseStockSnapshot]] = {}
    for snapshot in loose_snapshots:
        snapshots_by_key.setdefault(_shipping_item_key_from_snapshot(snapshot), []).append(snapshot)

    marking_sources = {
        key: [(row.id, row.container_id, row.container_code) for row in rows]
        for key, rows in snapshots_by_key.items()
    }

    for box in boxes:
        box_code = str(box.get("code") or "").strip()
        if not box_code:
            continue
        pallet_container = pallets_by_code.get(str(box.get("pallet_code") or "").strip())
        first_location = next((snapshot.location for snapshot in loose_snapshots if snapshot.location_id), None)
        box_container = _shipping_ensure_box_container(
            order,
            box_code=box_code,
            location=first_location,
            parent_container=pallet_container,
            user=user,
        )
        for item in box.get("items") or []:
            item_key = _shipping_item_key_from_payload(item)
            remaining = _to_int(item.get("qty"))
            if item.get("marking_preverified"):
                WarehouseWritePathService.assign_preverified_marked_shipping_units_to_box(
                    agency=order.agency,
                    order_id=order.number,
                    snapshot_ids=item.get("preverified_snapshot_ids") or [],
                    barcode=str(item.get("barcode") or "").strip(),
                    qty=remaining,
                    destination_container=box_container,
                    performed_by=user,
                )
                continue
            if item.get("requires_marking_scan"):
                for marked_unit in item.get("marking_units") or []:
                    WarehouseWritePathService.assign_marked_shipping_unit_to_box(
                        agency=order.agency,
                        order_id=order.number,
                        snapshot_id=_to_int(marked_unit.get("snapshot_id")),
                        barcode=str(marked_unit.get("barcode") or "").strip(),
                        marking_code=str(marked_unit.get("marking_code") or "").strip(),
                        destination_container=box_container,
                        performed_by=user,
                        source_bindings=marking_sources.get(item_key, []),
                    )
                continue
            source_snapshots = snapshots_by_key.get(item_key) or []
            while remaining > 0 and source_snapshots:
                snapshot = source_snapshots[0]
                snapshot_qty = _warehouse_shipping_snapshot_qty(snapshot)
                if snapshot_qty <= 0:
                    source_snapshots.pop(0)
                    continue
                take_qty = min(remaining, snapshot_qty)
                if take_qty >= snapshot_qty:
                    snapshot.container = box_container
                    snapshot.container_code = box_code
                    snapshot.parent_container = pallet_container
                    snapshot.save(update_fields=["container", "container_code", "parent_container", "updated_at"])
                    source_snapshots.pop(0)
                else:
                    snapshot.qty = snapshot_qty - take_qty
                    snapshot.save(update_fields=["qty", "updated_at"])
                    WarehouseStockSnapshot.objects.create(
                        agency=snapshot.agency,
                        stock_unit_type=snapshot.stock_unit_type or "item",
                        source_context_type=snapshot.source_context_type,
                        source_context_id=snapshot.source_context_id,
                        sku_ref=snapshot.sku_ref,
                        sku_code=snapshot.sku_code,
                        name=snapshot.name,
                        size=snapshot.size,
                        barcode=snapshot.barcode,
                        goods_type=snapshot.goods_type,
                        marking_code=snapshot.marking_code,
                        qty=take_qty,
                        available_qty=0,
                        processing_reserved_qty=0,
                        shipping_reserved_qty=0,
                        other_reserved_qty=0,
                        container=box_container,
                        container_code=box_code,
                        parent_container=pallet_container,
                        location=snapshot.location,
                        zone_code=snapshot.zone_code,
                        zone_kind=snapshot.zone_kind,
                        warehouse_state_code=snapshot.warehouse_state_code,
                        active_operation=snapshot.active_operation,
                        active_operation_type=snapshot.active_operation_type,
                        current_trip_id=snapshot.current_trip_id,
                        is_in_vehicle=snapshot.is_in_vehicle,
                        last_event=snapshot.last_event,
                    )
                remaining -= take_qty
            if remaining > 0:
                label = str(item.get("barcode") or item.get("sku_code") or "-").strip() or "-"
                raise ValueError(
                    f"Не удалось распределить {remaining} шт. товара {label}. "
                    "Упаковка отменена, складские данные не изменены."
                )


def save_shipping_loose_packing(
    order: ShippingOrder,
    *,
    boxes_state: list,
    pallets_state: list,
    loose_items: list[dict],
    gm_targets: list[dict] | None = None,
    repack_existing_boxes: bool = False,
    repack_source_box_codes: list[str] | None = None,
    user,
) -> dict:
    source_snapshots = (
        _shipping_repack_snapshots(order)
        if repack_existing_boxes
        else _shipping_loose_snapshots(order)
    )
    current_loose_items = _shipping_items_from_snapshots(order, source_snapshots)
    from .services import _ozon_loose_packing_gm_plan

    live_gm_plan = _ozon_loose_packing_gm_plan(
        order,
        loose_items=current_loose_items,
        repack_all_delivered=repack_existing_boxes,
    )
    if live_gm_plan["required"] and not live_gm_plan["ready"] and live_gm_plan["errors"]:
        return {
            "saved": False,
            "errors": [
                "Упаковка пока недоступна: " + str(error)
                for error in live_gm_plan["errors"][:4]
            ],
        }

    expected_by_key = {
        _shipping_item_key_from_payload(item): dict(item)
        for item in loose_items
        if isinstance(item, dict) and _shipping_item_key_from_payload(item) and _to_int(item.get("qty")) > 0
    }
    prepared_boxes, errors = _shipping_normalize_loose_packing_boxes(
        boxes_state,
        expected_by_key,
        gm_targets=gm_targets,
    )
    # Упаковка штучного товара создает только самостоятельные короба.
    # Паллетизация выполняется следующим отдельным этапом.
    prepared_pallets: list[dict] = []
    for box in prepared_boxes:
        box["pallet_code"] = ""
    box_codes = [str(box.get("code") or "").strip() for box in prepared_boxes if str(box.get("code") or "").strip()]
    existing_codes = set(
        WarehouseContainer.objects.filter(agency=order.agency, container_code__in=box_codes).values_list("container_code", flat=True)
    )
    allowed_existing_codes = {
        str(target.get("existing_box_code") or "").strip().casefold()
        for target in (gm_targets or [])
        if isinstance(target, dict) and str(target.get("existing_box_code") or "").strip()
    }
    delivered_box_codes = {
        str(box.get("box_code") or box.get("code") or "").strip().casefold()
        for box in _shipping_delivered_boxes(order)
        if str(box.get("box_code") or box.get("code") or "").strip()
    }
    unavailable_existing_codes = allowed_existing_codes - delivered_box_codes
    for box_code in sorted(unavailable_existing_codes):
        errors.append(f"Короб {box_code} больше не доступен для дополнения. Обновите страницу.")
    for box_code in sorted(existing_codes):
        if box_code.casefold() not in allowed_existing_codes:
            errors.append(f"Код короба {box_code} уже используется.")

    current_expected = {
        _shipping_item_key_from_payload(item): (
            _to_int(item.get("qty")),
            bool(item.get("requires_marking_scan")),
            bool(item.get("marking_preverified")),
        )
        for item in current_loose_items
        if isinstance(item, dict) and _shipping_item_key_from_payload(item)
    }
    submitted_expected = {
        item_key: (
            _to_int(item.get("qty")),
            bool(item.get("requires_marking_scan")),
            bool(item.get("marking_preverified")),
        )
        for item_key, item in expected_by_key.items()
    }
    if current_expected != submitted_expected:
        errors.append(
            "Состав товара для переупаковки изменился. Обновите страницу и повторите операцию."
            if repack_existing_boxes
            else "Товар без короба изменился. Обновите страницу и повторите упаковку."
        )

    if repack_existing_boxes:
        source_box_codes = {
            _shipping_snapshot_box_code(order, snapshot).casefold()
            for snapshot in _shipping_boxed_repack_snapshots(order)
            if _shipping_snapshot_box_code(order, snapshot)
        }
        submitted_source_box_codes = {
            str(code or "").strip().casefold()
            for code in repack_source_box_codes or []
            if str(code or "").strip()
        }
        if order.status != ShippingOrder.STATUS_PICKING:
            errors.append("Переупаковка Ozon доступна только после доставки товара в OTG.")
        if not source_box_codes:
            errors.append("Не найдены исходные короба в OTG для переупаковки.")
        if submitted_source_box_codes != source_box_codes:
            missing = sorted(source_box_codes - submitted_source_box_codes)
            extra = sorted(submitted_source_box_codes - source_box_codes)
            if missing:
                errors.append("Отсканируйте все исходные короба: " + ", ".join(missing) + ".")
            if extra:
                errors.append("Эти короба не входят в переупаковку: " + ", ".join(extra) + ".")
        if len(prepared_boxes) != len(gm_targets or []):
            errors.append("Количество новых коробов должно совпадать с количеством ШК ГМ Ozon.")

    if errors:
        return {"saved": False, "errors": errors}

    with transaction.atomic():
        locked_order = ShippingOrder.objects.select_for_update().get(pk=order.pk)
        if repack_existing_boxes and locked_order.status != ShippingOrder.STATUS_PICKING:
            return {
                "saved": False,
                "errors": ["Статус заявки изменился. Обновите страницу перед переупаковкой."],
            }
        live_source_snapshots = (
            _shipping_repack_snapshots(locked_order)
            if repack_existing_boxes
            else _shipping_loose_snapshots(locked_order)
        )
        locked_source_snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("container", "location", "last_event")
            .filter(id__in=[int(snapshot.id) for snapshot in live_source_snapshots])
            .order_by("id")
        )
        locked_expected = {
            _shipping_item_key_from_payload(item): _to_int(item.get("qty"))
            for item in _shipping_items_from_snapshots(order, locked_source_snapshots)
            if isinstance(item, dict) and _shipping_item_key_from_payload(item)
        }
        submitted_quantities = {
            item_key: _to_int(item.get("qty"))
            for item_key, item in expected_by_key.items()
        }
        if locked_expected != submitted_quantities:
            return {
                "saved": False,
                "errors": ["Складской факт изменился во время упаковки. Обновите страницу."],
            }
        live_locked_plan = _ozon_loose_packing_gm_plan(
            order,
            loose_items=_shipping_items_from_snapshots(order, locked_source_snapshots),
            repack_all_delivered=repack_existing_boxes,
        )
        if live_locked_plan["required"] and not live_locked_plan["ready"]:
            return {
                "saved": False,
                "errors": [
                    "Упаковка пока недоступна: " + str(error)
                    for error in (live_locked_plan.get("errors") or ["состав ШК ГМ изменился"])[0:4]
                ],
            }
        locked_prepared_boxes, locked_errors = _shipping_normalize_loose_packing_boxes(
            boxes_state,
            {
                _shipping_item_key_from_payload(item): dict(item)
                for item in _shipping_items_from_snapshots(order, locked_source_snapshots)
                if isinstance(item, dict)
                and _shipping_item_key_from_payload(item)
                and _to_int(item.get("qty")) > 0
            },
            gm_targets=live_locked_plan.get("targets") if live_locked_plan.get("required") else None,
        )
        if locked_errors:
            return {"saved": False, "errors": list(dict.fromkeys(locked_errors))}
        prepared_boxes = locked_prepared_boxes
        for box in prepared_boxes:
            box["pallet_code"] = ""
        boxed_locked_snapshots = [
            snapshot
            for snapshot in locked_source_snapshots
            if not _shipping_snapshot_is_loose(snapshot)
        ]
        source_container_ids = {
            int(snapshot.container_id)
            for snapshot in boxed_locked_snapshots
            if snapshot.container_id
        }
        if repack_existing_boxes:
            locked_source_box_codes = {
                _shipping_snapshot_box_code(order, snapshot).casefold()
                for snapshot in boxed_locked_snapshots
                if _shipping_snapshot_box_code(order, snapshot)
            }
            if locked_source_box_codes != submitted_source_box_codes:
                return {
                    "saved": False,
                    "errors": ["Состав исходных коробов изменился. Обновите страницу и отсканируйте их заново."],
                }
            foreign_snapshot_exists = (
                WarehouseStockSnapshot.objects.select_for_update()
                .filter(
                    agency=order.agency,
                    container_id__in=source_container_ids,
                    is_archived=False,
                    qty__gt=0,
                )
                .exclude(pk__in=[snapshot.pk for snapshot in boxed_locked_snapshots])
                .exists()
            )
            if foreign_snapshot_exists:
                return {
                    "saved": False,
                    "errors": [
                        "В исходных коробах найден товар вне этой отгрузки. "
                        "Переупаковка остановлена без изменений."
                    ],
                }
        source_box_codes_for_archive = sorted(
            {
                _shipping_snapshot_box_code(order, snapshot)
                for snapshot in boxed_locked_snapshots
                if _shipping_snapshot_box_code(order, snapshot)
            }
        )
        pallets_by_code: dict[str, WarehouseContainer] = {}
        act_data = _shipping_build_loose_packing_act(order, prepared_boxes, [])
        if repack_existing_boxes:
            act_data["act"] = "shipping_ozon_gm_repacking"
            act_data["repacked_existing_boxes"] = True
            act_data["source_box_codes"] = source_box_codes_for_archive
        _shipping_assign_loose_snapshots_to_boxes(
            order,
            boxes=prepared_boxes,
            pallets_by_code=pallets_by_code,
            loose_snapshots=locked_source_snapshots,
            user=user,
        )
        if repack_existing_boxes:
            if WarehouseStockSnapshot.objects.filter(
                agency=order.agency,
                container_id__in=source_container_ids,
                is_archived=False,
                qty__gt=0,
            ).exists():
                raise ValueError(
                    "Не весь товар перенесён из исходных коробов. "
                    "Переупаковка отменена, складские данные не изменены."
                )
            WarehouseWritePathService._archive_empty_shipping_source_containers(
                agency=order.agency,
                box_codes=source_box_codes_for_archive,
            )
        for cache_attr in ("_shipping_delivered_boxes_cache", "_shipping_manageable_packing_boxes_cache"):
            if hasattr(order, cache_attr):
                delattr(order, cache_attr)
        loose_box_codes = {
            str(box.get("code") or "").strip().lower()
            for box in prepared_boxes
            if str(box.get("code") or "").strip()
        }
        # Закрытие коробов завершает только упаковку штучного товара.
        # Общая паллетизация заявки всегда выполняется следующим шагом.
        completed = False
        log_order_action(
            action="status",
            order_id=order.number,
            order_type="shipping",
            user=user if getattr(user, "is_authenticated", False) else None,
            agency=order.agency,
            description=(
                "Кладовщик переупаковал исходные короба в грузоместа Ozon"
                if repack_existing_boxes
                else "Кладовщик упаковал товар без короба в короба для отгрузки"
            ),
            payload=order_payload(order, extra=act_data),
        )
    return {"saved": True, "errors": [], "act_data": act_data, "completed": completed}


def save_shipping_packing(
    order: ShippingOrder,
    *,
    boxes_state: list,
    pallets_state: list,
    delivered_boxes: list[dict],
    initial_boxes: list[dict],
    user,
    replace_existing: bool = False,
) -> dict:
    errors: list[str] = []
    if shipping_packing_pending_pallet_removal(order):
        return {
            "saved": False,
            "errors": [
                "\u041d\u0435\u043b\u044c\u0437\u044f \u0437\u0430\u0432\u0435\u0440\u0448\u0438\u0442\u044c \u043f\u0430\u043b\u043b\u0435\u0442\u0438\u0437\u0430\u0446\u0438\u044e: "
                "\u043e\u0436\u0438\u0434\u0430\u0435\u0442\u0441\u044f \u0440\u0435\u0448\u0435\u043d\u0438\u0435 \u043c\u0435\u043d\u0435\u0434\u0436\u0435\u0440\u0430 \u043f\u043e \u0438\u0437\u044a\u044f\u0442\u0438\u044e \u043f\u0430\u043b\u043b\u0435\u0442\u044b."
            ],
        }
    if not isinstance(boxes_state, list):
        boxes_state = []
    if not isinstance(pallets_state, list):
        pallets_state = []

    delivered_map: dict[str, dict] = {}
    delivered_by_code: dict[str, dict] = {}
    delivered_key_by_code: dict[str, str] = {}
    for index, box in enumerate(delivered_boxes, start=1):
        row_key = _shipping_box_row_key(box, index)
        if row_key:
            delivered_map[row_key] = box
        box_code = str(box.get("box_code") or box.get("code") or "").strip().lower()
        if box_code:
            delivered_by_code.setdefault(box_code, box)
            if row_key:
                delivered_key_by_code.setdefault(box_code, row_key)

    used_pallet_codes: set[str] = set()
    for raw_box in boxes_state:
        if not isinstance(raw_box, dict):
            continue
        pallet_code = str(raw_box.get("pallet_code") or "").strip()
        row_key = _shipping_box_row_key(raw_box)
        box_code = str(raw_box.get("code") or raw_box.get("box_code") or "").strip().lower()
        if (row_key in delivered_map or box_code in delivered_by_code) and pallet_code:
            used_pallet_codes.add(pallet_code)

    pallet_labels: dict[str, str] = {}
    for raw_pallet in pallets_state:
        if not isinstance(raw_pallet, dict):
            continue
        code = str(raw_pallet.get("code") or "").strip()
        label = _normalize_shipping_pallet_label(raw_pallet.get("label") or code)
        if not code:
            continue
        if not label:
            if code in used_pallet_codes:
                errors.append("Укажите название для каждой новой паллеты.")
            continue
        pallet_labels[code] = label

    assignments: dict[str, str] = {}
    assigned_box_codes: set[str] = set()
    seen_row_keys: set[str] = set()
    seen_box_codes: set[str] = set()
    for raw_box in boxes_state:
        if not isinstance(raw_box, dict):
            continue
        row_key = _shipping_box_row_key(raw_box)
        box_code = str(raw_box.get("code") or "").strip()
        pallet_code = str(raw_box.get("pallet_code") or "").strip()
        delivered_box = delivered_map.get(row_key)
        canonical_row_key = row_key
        if delivered_box is None and box_code:
            fallback_key = box_code.lower()
            delivered_box = delivered_by_code.get(fallback_key)
            canonical_row_key = delivered_key_by_code.get(fallback_key, row_key)
        if not box_code or delivered_box is None:
            continue
        seen_row_keys.add(row_key)
        if canonical_row_key:
            seen_row_keys.add(canonical_row_key)
        seen_box_codes.add(box_code.lower())
        if not pallet_code:
            errors.append(f"Укажите паллету для короба {box_code}.")
            continue
        label = pallet_labels.get(pallet_code) or _normalize_shipping_pallet_label(pallet_code)
        if not label:
            errors.append(f"Для короба {box_code} выбрана неизвестная паллета.")
            continue
        assignments[canonical_row_key] = label
        assigned_code = str(delivered_box.get("box_code") or box_code or "").strip().lower()
        if assigned_code:
            assigned_box_codes.add(assigned_code)

    used_label_numbers = sorted(
        _shipping_packing_label_number(pallet_labels.get(code) or code)
        for code in used_pallet_codes
        if _shipping_packing_label_number(pallet_labels.get(code) or code) > 0
    )
    if used_label_numbers:
        missing_label_numbers = [
            number
            for number in range(1, max(used_label_numbers) + 1)
            if number not in set(used_label_numbers)
        ]
        if missing_label_numbers:
            errors.append(
                "Нельзя завершить паллетизацию: в нумерации паллет есть пропуск. "
                f"Проверьте паллеты: не хватает №{', №'.join(str(number) for number in missing_label_numbers)}."
            )

    missing_boxes = [
        box
        for box in initial_boxes
        if _shipping_box_row_key(box) not in seen_row_keys
        and str(box.get("code") or box.get("box_code") or "").strip().lower() not in seen_box_codes
    ]
    for box in missing_boxes:
        errors.append(f"Короб {box.get('code') or '-'} отсутствует в раскладке.")

    not_assigned_codes: list[str] = []
    if order.status != ShippingOrder.STATUS_CANCELED:
        not_assigned_codes = sorted(ShippingTruthService.for_order(order).unassigned_for_packing(assigned_box_codes))
    if not_assigned_codes:
        errors.append(
            "\u041d\u0435\u043b\u044c\u0437\u044f \u0437\u0430\u0432\u0435\u0440\u0448\u0438\u0442\u044c \u043f\u0430\u043b\u043b\u0435\u0442\u0438\u0437\u0430\u0446\u0438\u044e: "
            "\u043d\u0435 \u0432\u0441\u0435 \u043a\u043e\u0440\u043e\u0431\u0430 \u0438\u0437 OTG \u0440\u0430\u0437\u043c\u0435\u0449\u0435\u043d\u044b \u043f\u043e \u043f\u0430\u043b\u043b\u0435\u0442\u0430\u043c. "
            f"\u041d\u0435 \u0440\u0430\u0437\u043c\u0435\u0449\u0435\u043d\u043e: {len(not_assigned_codes)}."
        )

    if order.status != ShippingOrder.STATUS_CANCELED:
        discrepancy_payload = (
            order.shipping_discrepancy_payload
            if isinstance(order.shipping_discrepancy_payload, dict)
            else {}
        )
        if (
            _to_int(discrepancy_payload.get("workflow_version")) >= 2
            and order.shipping_discrepancy_status
            in {"pending", "pick_confirmation", "pickup_required"}
        ):
            binding_result = validate_marketplace_item_binding(
                order,
                boxes=delivered_boxes,
            )
            if (
                binding_result.get("applies")
                and not binding_result.get("ready")
                and not shipping_discrepancy_allows_current_mismatch(
                    order,
                    binding_result,
                )
            ):
                errors.append(
                    "Нельзя завершить паллетизацию: расхождение ожидает решения "
                    "или подтвержденного сканами добора."
                )
        errors.extend(
            enforced_marketplace_item_binding_errors(
                order,
                boxes=delivered_boxes,
            )
        )

    if errors:
        return {"saved": False, "errors": errors}

    source_codes_by_label = {
        str(label or "").strip(): str(code or "").strip()
        for code, label in pallet_labels.items()
        if str(label or "").strip()
    }
    with transaction.atomic():
        locked_order = (
            ShippingOrder.objects.select_for_update(of=("self",))
            .select_related("agency", "marketplace")
            .get(pk=order.pk)
        )
        existing_summary = None
        if locked_order.status in {ShippingOrder.STATUS_PACKED, ShippingOrder.STATUS_CANCELED}:
            existing_summary = _shipping_packing_summary(locked_order)
        replacing_existing = bool(
            existing_summary
            and replace_existing
            and locked_order.status == ShippingOrder.STATUS_PACKED
        )
        if existing_summary and not replacing_existing:
            return {
                "saved": True,
                "errors": [],
                "act_data": existing_summary,
                "already_saved": True,
            }
        order = locked_order
        was_canceled = order.status == ShippingOrder.STATUS_CANCELED
        act_data = _shipping_build_packing_act(
            order,
            delivered_boxes,
            assignments,
            source_codes_by_label=source_codes_by_label,
        )
        warehouse_synced = _shipping_sync_warehouse_packing(
            order,
            act_data=act_data,
            user=user,
        )
        if not warehouse_synced:
            raise ValueError(
                "Не удалось записать паллетизацию в warehouse. "
                "Раскладка и складские данные не изменены."
            )
        completed_reachtruck_tasks = _shipping_auto_complete_otg_reachtruck_tasks(
            order,
            act_data=act_data,
            user=user,
        )
        if completed_reachtruck_tasks:
            act_data["auto_completed_reachtruck_tasks"] = completed_reachtruck_tasks
        if was_canceled:
            _shipping_release_canceled_palletized_reserves(order, user=user)
            return_move_ids = _shipping_create_canceled_return_tasks(order, act_data=act_data, user=user)
            close_storekeeper_task(order)
            act_data["canceled_palletization"] = True
            act_data["reserves_released"] = True
            act_data["return_move_ids"] = return_move_ids
            action_description = (
                "Кладовщик разложил короба отмененной заявки по паллетам; "
                "созданы задания на возврат товара в основной склад"
            )
        else:
            order.status = ShippingOrder.STATUS_PACKED
            order.save(update_fields=["status", "updated_at"])
            close_storekeeper_task(order)
            ensure_logistician_task(order, user)
            if replacing_existing:
                act_data["replaced_existing_packing"] = True
                action_description = "\u041a\u043b\u0430\u0434\u043e\u0432\u0449\u0438\u043a \u0438\u0437\u043c\u0435\u043d\u0438\u043b \u0440\u0430\u0441\u043a\u043b\u0430\u0434\u043a\u0443 \u043a\u043e\u0440\u043e\u0431\u043e\u0432 \u043f\u043e \u043d\u043e\u0432\u044b\u043c \u043f\u0430\u043b\u043b\u0435\u0442\u0430\u043c \u0434\u043b\u044f \u043e\u0442\u0433\u0440\u0443\u0437\u043a\u0438"
            else:
                action_description = "Кладовщик разложил короба по новым паллетам для отгрузки"
        log_order_action(
            action="status",
            order_id=order.number,
            order_type="shipping",
            user=user if getattr(user, "is_authenticated", False) else None,
            agency=order.agency,
            description=action_description,
            payload=order_payload(order, extra=act_data),
        )
    return {"saved": True, "errors": [], "act_data": act_data}
