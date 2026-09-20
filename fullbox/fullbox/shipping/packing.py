from __future__ import annotations

import re

from django.db import transaction
from django.utils import timezone

from audit.models import OrderAuditEntry, log_order_action
from reachtruck.models import MoveTask
from sklad.models import WarehouseContainer, WarehouseEvent, WarehouseStockSnapshot
from sklad.services.warehouse_transitions import WarehouseStateCode
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sku.models import Agency
from fullbox.container_codes import issue_pallet_code

from .models import ShippingOrder
from .services import close_storekeeper_task, ensure_logistician_task, order_payload
from .truth import ShippingTruthService

_SHIPPING_PALLET_LABEL_RE = re.compile(r"^SHIP-\d+-PAL-(\d+)-(.*)$")
_SHIPPING_ITEM_KEY_SEPARATOR = "\x1f"


def _to_int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _shipping_task_matches_order(order: ShippingOrder, payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    order_number = str(order.number or "").strip()
    payload_number = str(payload.get("shipping_order_id") or payload.get("order_id") or "").strip()
    if payload_number and payload_number == order_number:
        return True
    payload_pk = payload.get("shipping_order_pk") or payload.get("order_pk")
    return str(payload_pk or "").strip() == str(order.pk)


def _shipping_packing_entry(order: ShippingOrder) -> OrderAuditEntry | None:
    return (
        OrderAuditEntry.objects.filter(
            order_id=order.number,
            order_type="shipping",
            payload__act="shipping_packing",
        )
        .order_by("-created_at")
        .first()
    )


_PACKING_PALLET_REMOVAL_ACT = "shipping_packing_pallet_removal"


def _shipping_packing_pallet_removal_entries(order: ShippingOrder):
    return OrderAuditEntry.objects.filter(
        order_id=order.number,
        order_type="shipping",
        payload__act=_PACKING_PALLET_REMOVAL_ACT,
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
            return draft
    return None


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
        snapshot.active_operation = None
        snapshot.active_operation_type = ""
        snapshot.warehouse_state_code = WarehouseStateCode.IN_OTG.value
        snapshot.zone_code = str(snapshot.zone_code or "OTG").strip() or "OTG"
        snapshot.save(
            update_fields=[
                "parent_container",
                "last_event",
                "available_qty",
                "shipping_reserved_qty",
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
            ],
        )
        .order_by("id")
    )


def _shipping_flow_snapshots(order: ShippingOrder) -> list[WarehouseStockSnapshot]:
    cache_key = "_shipping_flow_snapshots_cache"
    cached = getattr(order, cache_key, None)
    if cached is None:
        cached = list(_shipping_flow_snapshot_queryset(order))
        setattr(order, cache_key, cached)
    return cached


def _shipping_loose_snapshots(order: ShippingOrder) -> list[WarehouseStockSnapshot]:
    return [
        snapshot
        for snapshot in _shipping_flow_snapshots(order)
        if _shipping_snapshot_is_loose(snapshot) and _warehouse_shipping_snapshot_qty(snapshot) > 0
    ]


def _shipping_loose_items(order: ShippingOrder) -> list[dict]:
    rows: dict[str, dict] = {}
    for snapshot in _shipping_loose_snapshots(order):
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
            },
        )
        row["qty"] = int(row.get("qty") or 0) + item_qty
        row["snapshot_ids"].append(int(snapshot.id))
    return sorted(
        rows.values(),
        key=lambda item: (
            str(item.get("sku_code") or "").lower(),
            str(item.get("name") or "").lower(),
            str(item.get("size") or "").lower(),
            str(item.get("barcode") or "").lower(),
        ),
    )


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

    boxes: dict[str, dict] = {}
    for snapshot in snapshots:
        if _shipping_snapshot_is_loose(snapshot):
            continue
        box_code = _shipping_snapshot_box_code(order, snapshot)
        if not box_code:
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


def _shipping_sync_warehouse_packing(
    order: ShippingOrder,
    *,
    act_data: dict,
    user,
) -> bool:
    snapshots = _shipping_warehouse_snapshots(order)
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
            started_by=user,
        )
        snapshots = _shipping_warehouse_snapshots(order)

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
        pallet_container, _ = WarehouseContainer.objects.get_or_create(
            agency=order.agency,
            container_code=pallet_code,
            defaults={
                "container_type": WarehouseContainer.TYPE_MIXED_PALLET,
                "current_location": location,
                "created_by": user if getattr(user, "is_authenticated", False) else None,
                "source_context_type": "shipping",
                "source_context_id": str(order.number or "").strip(),
            },
        )
        updates: list[str] = []
        if pallet_container.container_type != WarehouseContainer.TYPE_MIXED_PALLET:
            pallet_container.container_type = WarehouseContainer.TYPE_MIXED_PALLET
            updates.append("container_type")
        if location is not None and pallet_container.current_location_id != location.id:
            pallet_container.current_location = location
            updates.append("current_location")
        if str(pallet_container.source_context_type or "").strip() != "shipping":
            pallet_container.source_context_type = "shipping"
            updates.append("source_context_type")
        if str(pallet_container.source_context_id or "").strip() != str(order.number or "").strip():
            pallet_container.source_context_id = str(order.number or "").strip()
            updates.append("source_context_id")
        if updates:
            pallet_container.save(update_fields=updates + ["updated_at"])
        pallet_containers[pallet_code.lower()] = pallet_container

    target_pallet_by_box_code = {
        str(box.get("code") or "").strip().lower(): str(box.get("pallet_code") or "").strip().lower()
        for box in (act_data.get("act_boxes") or [])
        if isinstance(box, dict) and str(box.get("code") or "").strip() and str(box.get("pallet_code") or "").strip()
    }
    if not target_pallet_by_box_code:
        return False

    for snapshot in snapshots:
        container = snapshot.container
        box_code = _shipping_snapshot_box_code(order, snapshot)
        target_code = target_pallet_by_box_code.get(box_code.lower())
        target_pallet = pallet_containers.get(str(target_code or "").strip().lower())
        if not target_pallet or not box_code:
            continue
        if container is None and box_code:
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
            container_updates: list[str] = []
            if container.container_type != WarehouseContainer.TYPE_BOX:
                container.container_type = WarehouseContainer.TYPE_BOX
                container_updates.append("container_type")
            if container.parent_container_id != target_pallet.id:
                container.parent_container = target_pallet
                container_updates.append("parent_container")
            if target_pallet.current_location_id and container.current_location_id != target_pallet.current_location_id:
                container.current_location = target_pallet.current_location
                container_updates.append("current_location")
            if str(container.source_context_type or "").strip() != "shipping":
                container.source_context_type = "shipping"
                container_updates.append("source_context_type")
            if str(container.source_context_id or "").strip() != str(order.number or "").strip():
                container.source_context_id = str(order.number or "").strip()
                container_updates.append("source_context_id")
            if container_updates:
                container.save(update_fields=container_updates + ["updated_at"])
            snapshot.container = container
            snapshot.container_code = box_code
        if container is not None and str(container.container_type or "").strip() == WarehouseContainer.TYPE_BOX:
            box_updates: list[str] = []
            if container.parent_container_id != target_pallet.id:
                container.parent_container = target_pallet
                box_updates.append("parent_container")
            if target_pallet.current_location_id and container.current_location_id != target_pallet.current_location_id:
                container.current_location = target_pallet.current_location
                box_updates.append("current_location")
            if box_updates:
                container.save(update_fields=box_updates + ["updated_at"])
        snapshot_updates: list[str] = []
        if snapshot.container_id != getattr(container, "id", None):
            snapshot.container = container
            snapshot_updates.append("container")
        if str(snapshot.container_code or "").strip() != box_code:
            snapshot.container_code = box_code
            snapshot_updates.append("container_code")
        if snapshot.parent_container_id != target_pallet.id:
            snapshot.parent_container = target_pallet
            snapshot_updates.append("parent_container")
        if target_pallet.current_location_id and snapshot.location_id != target_pallet.current_location_id:
            snapshot.location = target_pallet.current_location
            snapshot.zone_code = str(target_pallet.current_location.zone_code or "").strip()
            snapshot.zone_kind = str(target_pallet.current_location.zone_kind or "").strip()
            snapshot_updates.extend(["location", "zone_code", "zone_kind"])
        if snapshot_updates:
            snapshot.save(update_fields=snapshot_updates + ["updated_at"])

    if operation is not None and str(operation.status or "").strip() != "done":
        WarehouseWritePathService.complete_palletization(
            operation=operation,
            performed_by=user,
        )
    return True


def _shipping_delivered_boxes(order: ShippingOrder) -> list[dict]:
    cache_key = "_shipping_delivered_boxes_cache"
    cached = getattr(order, cache_key, None)
    if cached is not None:
        return cached
    warehouse_boxes = _shipping_delivered_boxes_from_warehouse(order)
    done_tasks = (
        MoveTask.objects.filter(
            request__agency=order.agency,
            status=MoveTask.STATUS_DONE,
            to_zone="OTG",
        )
        .order_by("id")
    )
    placement_cache: dict[str, dict[str, dict]] = {}
    delivered: list[dict] = list(warehouse_boxes)
    seen_codes: set[str] = {
        str(row.get("box_code") or "").strip().lower()
        for row in delivered
        if str(row.get("box_code") or "").strip()
    }
    row_index = len(delivered)
    for task in done_tasks:
        payload = task.payload if isinstance(task.payload, dict) else {}
        if not _shipping_task_matches_order(order, payload):
            continue
        receiving_order_id = str(payload.get("receiving_order_id") or "").strip()
        placement_boxes = placement_cache.get(receiving_order_id)
        if placement_boxes is None:
            placement_boxes = _shipping_receiving_placement_box_map(order.agency, receiving_order_id)
            placement_cache[receiving_order_id] = placement_boxes
        mobile_payload = payload.get("mobile_placement_payload") if isinstance(payload, dict) else {}
        if isinstance(mobile_payload, dict):
            for raw_box in mobile_payload.get("act_boxes") or []:
                if not isinstance(raw_box, dict):
                    continue
                mobile_box_code = str(raw_box.get("code") or "").strip()
                if mobile_box_code:
                    placement_boxes.setdefault(mobile_box_code.lower(), dict(raw_box))
        raw_codes = payload.get("picked_boxes") or []
        if not isinstance(raw_codes, list):
            raw_codes = []
        if not raw_codes:
            raw_codes = [
                str(row.get("box_code") or "").strip()
                for row in (payload.get("picked_rows") or [])
                if isinstance(row, dict) and str(row.get("box_code") or "").strip()
            ]
        for raw_code in raw_codes:
            box_code = str(raw_code or "").strip()
            box_key = box_code.lower()
            if not box_code or box_key in seen_codes:
                continue
            seen_codes.add(box_key)
            row_index += 1
            box_payload = placement_boxes.get(box_key) if placement_boxes else None
            items = list(box_payload.get("items") or []) if isinstance(box_payload, dict) else []
            delivered.append(
                {
                    "row_key": _shipping_box_row_key(
                        {
                            "box_code": box_code,
                            "receiving_order_id": receiving_order_id,
                        },
                        row_index,
                    ),
                    "box_code": box_code,
                    "qty": _shipping_box_qty(items),
                    "items": items,
                    "barcode_preview": _shipping_box_items_preview(items),
                    "receiving_order_id": receiving_order_id,
                }
            )
    delivered = [row for row in delivered if row["box_code"] and int(row.get("qty") or 0) > 0]
    setattr(order, cache_key, delivered)
    return delivered


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
        source_code = str((source_codes_by_label or {}).get(label) or "").strip()
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
            "pallet_source_code": source_code,
            "location": {"zone": "OTG"},
            "sealed": True,
        }
        act_boxes.append(act_box)
        pallet_entry = pallets_by_label.setdefault(
            label,
            {
                "code": pallet_code,
                "label": label,
                "source_code": source_code,
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
    snapshots = list(
        WarehouseStockSnapshot.objects.select_related("container", "parent_container")
        .filter(
            agency=order.agency,
            is_archived=False,
            warehouse_state_code__in=[
                WarehouseStateCode.READY_FOR_LOADING.value,
                "assigned_to_trip",
                "loading_in_progress",
                "loaded_to_vehicle",
            ],
            last_event__stock_context_type="shipping",
            last_event__stock_context_id=order_key,
            parent_container__source_context_type="shipping",
            parent_container__source_context_id=order_key,
        )
        .order_by("parent_container__container_code", "id")
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
        code = str(pallet.get("code") or "").strip()
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
                "source_code": str(pallet.get("source_code") or "").strip(),
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


def shipping_packing_slip_meta(order: ShippingOrder) -> dict:
    delivery_date = "-"
    if order.slot_date:
        delivery_date = order.slot_date.strftime("%d.%m.%Y")
        if order.slot_time:
            delivery_date = f"{delivery_date} {order.slot_time.strftime('%H:%M')}"
    elif order.planned_ship_date:
        delivery_date = order.planned_ship_date.strftime("%d.%m.%Y")
    elif order.eta_at:
        eta_at = order.eta_at
        if timezone.is_naive(eta_at):
            eta_at = timezone.make_aware(eta_at, timezone.get_current_timezone())
        delivery_date = timezone.localtime(eta_at).strftime("%d.%m.%Y %H:%M")

    marketplace_name = str(getattr(order.marketplace, "name", "") or "").strip() or "-"
    agency_name = str(getattr(order.agency, "agn_name", "") or "").strip() or "-"
    return {
        "order_number": str(order.number or "").strip(),
        "marketplace_name": marketplace_name,
        "supply_type": order.get_supply_type_display() or "-",
        "shipping_barcode": str(order.shipping_barcode or "").strip() or "-",
        "supply_number": str(order.wb_supply_barcode or "").strip() or "-",
        "destination_warehouse": str(order.destination_warehouse or "").strip() or "-",
        "transit_address": str(order.transit_address or "").strip() if order.wb_transit_warehouse else "",
        "supplier_name": agency_name,
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
        qr_source = str(pallet_code or pallet_data.get("source_code") or pallet_label).strip()
        qr_value = f"{meta['order_number']}::{qr_source}".strip(":")
        slips.append(
            {
                "slip_key": pallet_code or f"PAL-{index}",
                "pallet_index": str(index),
                "pallet_label": pallet_label,
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
                "qr_value": qr_value or "-",
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
    used_pallets = {str(box.get("pallet_code") or "").strip() for box in initial_boxes if str(box.get("pallet_code") or "").strip()}
    initial_pallets: list[dict] = []
    seen: set[str] = set()
    for raw_pallet in draft_state.get("pallets") or []:
        if not isinstance(raw_pallet, dict):
            continue
        code = str(raw_pallet.get("code") or "").strip()
        if not code or code not in used_pallets or code in seen:
            continue
        seen.add(code)
        initial_pallets.append(
            {
                "code": code,
                "label": str(raw_pallet.get("label") or code).strip(),
                "sealed": bool(raw_pallet.get("sealed")),
            }
        )
    for pallet_code in sorted(used_pallets):
        if pallet_code not in seen:
            initial_pallets.append({"code": pallet_code, "label": pallet_code, "sealed": True})
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
    if delivered_boxes:
        setattr(order, cache_key, delivered_boxes)
        return delivered_boxes
    summary = packing_summary if isinstance(packing_summary, dict) else _shipping_packing_summary(order)
    entry = summary.get("entry") if isinstance(summary, dict) else None
    payload = entry.payload if entry and isinstance(entry.payload, dict) else {}
    boxes = _shipping_boxes_from_packing_payload(payload)
    setattr(order, cache_key, boxes)
    return boxes


def _shipping_packing_initial_state(
    order: ShippingOrder,
    packing_summary: dict | None = None,
) -> dict:
    summary = packing_summary if isinstance(packing_summary, dict) else _shipping_packing_summary(order)
    delivered_boxes = _shipping_manageable_packing_boxes(order, summary)
    if not summary:
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


def _shipping_loose_packing_initial_state(order: ShippingOrder) -> dict:
    loose_items = _shipping_loose_items(order)
    existing_codes = {
        str(row.get("box_code") or "").strip()
        for row in _shipping_delivered_boxes(order)
        if str(row.get("box_code") or "").strip()
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
        act_boxes.append(
            {
                "code": box_code,
                "qty": box_qty,
                "items": items,
                "barcode_preview": _shipping_box_items_preview(items),
                "pallet_code": pallet_code,
                "pallet_label": pallet_label,
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
                    "code": pallet_code,
                    "label": pallet_label,
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


def _shipping_normalize_loose_packing_boxes(boxes_state: list, expected_by_key: dict[str, dict]) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    prepared_boxes: list[dict] = []
    seen_box_codes: set[str] = set()
    packed_totals: dict[str, int] = {}
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
            }
            prepared_items.append(prepared_item)
            packed_totals[item_key] = int(packed_totals.get(item_key) or 0) + item_qty
        if not prepared_items:
            errors.append(f"Короб {box_code} пустой.")
        prepared_boxes.append(
            {
                "code": box_code,
                "items": prepared_items,
                "pallet_code": str(raw_box.get("pallet_code") or "").strip(),
                "sealed": bool(raw_box.get("sealed")),
            }
        )

    if not prepared_boxes:
        errors.append("Нет закрытых коробов с товаром без короба.")
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
            errors.append(f"Укажите паллету для короба {box_code}.")
        elif pallet_code not in prepared_codes:
            errors.append(f"Для короба {box_code} выбрана неизвестная паллета.")
    if boxes and not prepared:
        errors.append("Нет закрытых паллет с коробами.")
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
        if not container_code:
            continue
        pallet["container_code"] = container_code
        container, _created = WarehouseContainer.objects.get_or_create(
            agency=order.agency,
            container_code=container_code,
            defaults={
                "container_type": WarehouseContainer.TYPE_MIXED_PALLET,
                "current_location": location,
                "created_by": user if getattr(user, "is_authenticated", False) else None,
                "source_context_type": "shipping",
                "source_context_id": str(order.number or "").strip(),
            },
        )
        updates: list[str] = []
        if container.container_type != WarehouseContainer.TYPE_MIXED_PALLET:
            container.container_type = WarehouseContainer.TYPE_MIXED_PALLET
            updates.append("container_type")
        if location is not None and container.current_location_id != location.id:
            container.current_location = location
            updates.append("current_location")
        if str(container.source_context_type or "").strip() != "shipping":
            container.source_context_type = "shipping"
            updates.append("source_context_type")
        if str(container.source_context_id or "").strip() != str(order.number or "").strip():
            container.source_context_id = str(order.number or "").strip()
            updates.append("source_context_id")
        if updates:
            container.save(update_fields=updates + ["updated_at"])
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


def save_shipping_loose_packing(
    order: ShippingOrder,
    *,
    boxes_state: list,
    pallets_state: list,
    loose_items: list[dict],
    user,
) -> dict:
    expected_by_key = {
        _shipping_item_key_from_payload(item): dict(item)
        for item in loose_items
        if isinstance(item, dict) and _shipping_item_key_from_payload(item) and _to_int(item.get("qty")) > 0
    }
    prepared_boxes, errors = _shipping_normalize_loose_packing_boxes(boxes_state, expected_by_key)
    prepared_pallets, pallet_errors = _shipping_normalize_loose_packing_pallets(pallets_state, prepared_boxes)
    errors.extend(pallet_errors)
    box_codes = [str(box.get("code") or "").strip() for box in prepared_boxes if str(box.get("code") or "").strip()]
    existing_codes = set(
        WarehouseContainer.objects.filter(agency=order.agency, container_code__in=box_codes).values_list("container_code", flat=True)
    )
    for box_code in sorted(existing_codes):
        errors.append(f"Код короба {box_code} уже используется.")

    loose_snapshots = _shipping_loose_snapshots(order)
    current_expected = {
        _shipping_item_key_from_payload(item): _to_int(item.get("qty"))
        for item in _shipping_loose_items(order)
        if isinstance(item, dict) and _shipping_item_key_from_payload(item)
    }
    submitted_expected = {
        item_key: _to_int(item.get("qty"))
        for item_key, item in expected_by_key.items()
    }
    if current_expected != submitted_expected:
        errors.append("Товар без короба изменился. Обновите страницу и повторите упаковку.")

    if errors:
        return {"saved": False, "errors": errors}

    with transaction.atomic():
        first_location = next((snapshot.location for snapshot in loose_snapshots if snapshot.location_id), None)
        pallets_by_code = _shipping_ensure_loose_pallet_containers(
            order,
            pallets=prepared_pallets,
            location=first_location,
            user=user,
        )
        act_data = _shipping_build_loose_packing_act(order, prepared_boxes, prepared_pallets)
        _shipping_assign_loose_snapshots_to_boxes(
            order,
            boxes=prepared_boxes,
            pallets_by_code=pallets_by_code,
            loose_snapshots=loose_snapshots,
            user=user,
        )
        log_order_action(
            action="status",
            order_id=order.number,
            order_type="shipping",
            user=user if getattr(user, "is_authenticated", False) else None,
            agency=order.agency,
            description="Кладовщик упаковал товар без короба в короба для отгрузки",
            payload=order_payload(order, extra=act_data),
        )
    return {"saved": True, "errors": [], "act_data": act_data}


def save_shipping_packing(
    order: ShippingOrder,
    *,
    boxes_state: list,
    pallets_state: list,
    delivered_boxes: list[dict],
    initial_boxes: list[dict],
    user,
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
    for index, box in enumerate(delivered_boxes, start=1):
        row_key = _shipping_box_row_key(box, index)
        if row_key:
            delivered_map[row_key] = box

    used_pallet_codes: set[str] = set()
    for raw_box in boxes_state:
        if not isinstance(raw_box, dict):
            continue
        pallet_code = str(raw_box.get("pallet_code") or "").strip()
        row_key = _shipping_box_row_key(raw_box)
        if row_key in delivered_map and pallet_code:
            used_pallet_codes.add(pallet_code)

    pallet_labels: dict[str, str] = {}
    for raw_pallet in pallets_state:
        if not isinstance(raw_pallet, dict):
            continue
        code = str(raw_pallet.get("code") or "").strip()
        label = _normalize_shipping_pallet_label(raw_pallet.get("label") or code)
        if not code:
            continue
        if code not in used_pallet_codes:
            continue
        if not label:
            errors.append("Укажите название для каждой новой паллеты.")
            continue
        pallet_labels[code] = label

    assignments: dict[str, str] = {}
    assigned_box_codes: set[str] = set()
    seen_row_keys: set[str] = set()
    for raw_box in boxes_state:
        if not isinstance(raw_box, dict):
            continue
        row_key = _shipping_box_row_key(raw_box)
        box_code = str(raw_box.get("code") or "").strip()
        pallet_code = str(raw_box.get("pallet_code") or "").strip()
        delivered_box = delivered_map.get(row_key)
        if not box_code or delivered_box is None:
            continue
        seen_row_keys.add(row_key)
        if not pallet_code:
            errors.append(f"Укажите паллету для короба {box_code}.")
            continue
        label = pallet_labels.get(pallet_code)
        if not label:
            errors.append(f"Для короба {box_code} выбрана неизвестная паллета.")
            continue
        assignments[row_key] = label
        assigned_code = str(delivered_box.get("box_code") or box_code or "").strip().lower()
        if assigned_code:
            assigned_box_codes.add(assigned_code)

    missing_boxes = [box for box in initial_boxes if _shipping_box_row_key(box) not in seen_row_keys]
    for box in missing_boxes:
        errors.append(f"Короб {box.get('code') or '-'} отсутствует в раскладке.")

    not_assigned_codes = sorted(ShippingTruthService.for_order(order).unassigned_for_packing(assigned_box_codes))
    if not_assigned_codes:
        errors.append(
            "\u041d\u0435\u043b\u044c\u0437\u044f \u0437\u0430\u0432\u0435\u0440\u0448\u0438\u0442\u044c \u043f\u0430\u043b\u043b\u0435\u0442\u0438\u0437\u0430\u0446\u0438\u044e: "
            "\u043d\u0435 \u0432\u0441\u0435 \u043a\u043e\u0440\u043e\u0431\u0430 \u0438\u0437 OTG \u0440\u0430\u0437\u043c\u0435\u0449\u0435\u043d\u044b \u043f\u043e \u043f\u0430\u043b\u043b\u0435\u0442\u0430\u043c. "
            f"\u041d\u0435 \u0440\u0430\u0437\u043c\u0435\u0449\u0435\u043d\u043e: {len(not_assigned_codes)}."
        )

    if errors:
        return {"saved": False, "errors": errors}

    source_codes_by_label = {
        str(label or "").strip(): str(code or "").strip()
        for code, label in pallet_labels.items()
        if str(label or "").strip()
    }
    with transaction.atomic():
        act_data = _shipping_build_packing_act(
            order,
            delivered_boxes,
            assignments,
            source_codes_by_label=source_codes_by_label,
        )
        _shipping_sync_warehouse_packing(
            order,
            act_data=act_data,
            user=user,
        )
        order.status = ShippingOrder.STATUS_PACKED
        order.save(update_fields=["status", "updated_at"])
        close_storekeeper_task(order)
        ensure_logistician_task(order, user)
        log_order_action(
            action="status",
            order_id=order.number,
            order_type="shipping",
            user=user if getattr(user, "is_authenticated", False) else None,
            agency=order.agency,
            description="Кладовщик разложил короба по новым паллетам для отгрузки",
            payload=order_payload(order, extra=act_data),
        )
    return {"saved": True, "errors": [], "act_data": act_data}
