from __future__ import annotations

import re
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from audit.models import OrderAuditEntry, get_order_external_number
from employees.models import Employee
from employees.access import resolve_cabinet_url
from reachtruck.models import MoveRequest, MoveTask
from sklad.models import WarehouseStockSnapshot
from sklad.services import WarehouseGoodsStateResolver, WarehouseStateCode

from .dispatch import (
    format_shipping_datetime,
    shipping_dispatch_employee,
    shipping_dispatch_stage,
)
from .models import ShippingOrder, ShippingOrderAttachment
from .packing import _shipping_packing_summary
from .truth import ShippingTruthService
from .workflow import is_logistician_role, is_manager_role, is_order_returned_for_rework


def _to_int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _display_shipping_number(number: str) -> str:
    return get_order_external_number("shipping", number)


def shipping_expected_box_count(order: ShippingOrder) -> int:
    """Return the current authoritative box count for warehouse workflow.

    Ozon cargo composition can be reconciled after a request was created (for
    example, when a product and its GM barcode are removed together).  The
    persisted ``expected_boxes`` field may therefore contain the older count.
    A complete immutable Ozon snapshot is safer for packing readiness; all
    other shipments continue to use the persisted field.
    """
    discrepancy_payload = (
        order.shipping_discrepancy_payload
        if isinstance(getattr(order, "shipping_discrepancy_payload", None), dict)
        else {}
    )
    discrepancy_status = str(
        getattr(order, "shipping_discrepancy_status", "")
        or discrepancy_payload.get("status")
        or ""
    ).strip()
    final_otg_box_count = _to_int(discrepancy_payload.get("final_otg_box_count"))
    if discrepancy_status in {"approved", "resolved"} and final_otg_box_count > 0:
        return final_otg_box_count

    try:
        from .item_binding import _latest_ozon_gm_payload

        payload = _latest_ozon_gm_payload(order)
        cargoes = [
            row
            for row in payload.get("gm_cargoes") or []
            if isinstance(row, dict)
        ]
        boxes_total = _to_int(payload.get("ozon_boxes_total"))
        is_complete_ozon_snapshot = bool(
            payload.get("ozon_snapshot_complete")
            and (
                payload.get("ozon_snapshot_source") == "ozon_api"
                or payload.get("is_ozon_api_supply")
                or payload.get("mass_ozon_submit")
            )
        )
        if (
            is_complete_ozon_snapshot
            and boxes_total > 0
            and len(cargoes) == boxes_total
        ):
            return boxes_total
    except Exception:
        # Keep legacy/non-Ozon requests usable even if their audit history is
        # incomplete. Strict Ozon validation is performed separately.
        pass
    return max(_to_int(getattr(order, "expected_boxes", 0)), 0)


def _box_count_from_move_payload(payload: dict) -> int:
    if not isinstance(payload, dict):
        return 0
    picked_rows = payload.get("picked_rows")
    if isinstance(picked_rows, list):
        codes = {
            str(row.get("box_code") or "").strip()
            for row in picked_rows
            if isinstance(row, dict) and str(row.get("box_code") or "").strip()
        }
        if codes:
            return len(codes)

    requested_boxes = payload.get("requested_boxes")
    if isinstance(requested_boxes, list) and requested_boxes:
        total = 0
        has_qty = False
        codes: set[str] = set()
        for entry in requested_boxes:
            if isinstance(entry, dict):
                qty = _to_int(entry.get("boxes") or entry.get("qty_boxes") or entry.get("qty"))
                if qty > 0:
                    total += qty
                    has_qty = True
                    continue
                code = str(entry.get("box_code") or "").strip()
                if code:
                    codes.add(code)
                    continue
            else:
                code = str(entry or "").strip()
                if code:
                    codes.add(code)
        if has_qty and total > 0:
            return total
        if codes:
            return len(codes)
        return len(requested_boxes)

    requested_box = str(payload.get("requested_box") or "").strip()
    if requested_box:
        return 1
    return 0


def shipping_task_matches_order(order: ShippingOrder, payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    order_number = str(order.number or "").strip()
    task_order = str(payload.get("shipping_order_id") or "").strip()
    if task_order and task_order == order_number:
        return True
    return _to_int(payload.get("shipping_order_pk")) == int(order.pk or 0)


def shipping_reachtruck_metrics(order: ShippingOrder, *, order_box_count: int | None = None) -> dict[str, int]:
    truth = ShippingTruthService.for_order(order)
    pallets: set[str] = set()
    for snapshot in truth.warehouse_snapshots:
        parent = getattr(snapshot, "parent_container", None)
        pallet_code = str(getattr(parent, "container_code", "") or "").strip()
        if pallet_code and pallet_code != "-":
            pallets.add(pallet_code)
    return {"pallet_count": len(pallets), "box_count": len(truth.warehouse_box_codes)}


def shipping_otg_tasks_progress(order: ShippingOrder) -> dict[str, int]:
    tasks = (
        MoveTask.objects.filter(
            request__context_type=MoveRequest.CONTEXT_MANUAL,
            request__context_id=str(order.id),
            request__destination_zone="OTG",
            to_zone="OTG",
        )
        .order_by("id")
    )
    use_payload_fallback = not tasks.exists()
    if use_payload_fallback:
        tasks = (
            MoveTask.objects.filter(
                request__agency=order.agency,
                to_zone="OTG",
            )
            .order_by("id")
        )
    total = 0
    done = 0
    active = 0
    for task in tasks:
        payload = task.payload if isinstance(task.payload, dict) else {}
        if use_payload_fallback and not shipping_task_matches_order(order, payload):
            continue
        # A canceled task is no longer part of the delivery plan. It is often
        # replaced by a new task after a box substitution and must not keep the
        # request forever below 100% completion.
        if task.status == MoveTask.STATUS_CANCELED:
            continue
        total += 1
        if task.status == MoveTask.STATUS_DONE:
            done += 1
        elif task.status in {MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS}:
            active += 1
    return {"total": total, "done": done, "active": active}


def shipping_otg_delivery_completed(order: ShippingOrder) -> bool:
    expected_boxes = shipping_expected_box_count(order)
    truth = ShippingTruthService.for_order(order)
    delivered_box_codes = truth.warehouse_box_codes
    if expected_boxes > 0 and len(delivered_box_codes) >= expected_boxes:
        return True

    progress = shipping_otg_tasks_progress(order)
    total = int(progress.get("total") or 0)
    done = int(progress.get("done") or 0)
    active = int(progress.get("active") or 0)
    return bool(delivered_box_codes) and total > 0 and active == 0 and done >= total and not truth.quantity_mismatch_rows


def shipping_ui_status_label(order: ShippingOrder, *, dispatch_stage: dict | None = None) -> str:
    # Cancellation is terminal for the client, while the warehouse may still
    # have to palletize and return stock that had already reached OTG.
    if order.status == ShippingOrder.STATUS_CANCELED:
        truth = ShippingTruthService.for_order(order)
        if truth.warehouse_box_codes:
            if _shipping_packing_summary(order):
                return "Отменена — товар ожидает возврата на склад"
            return "Отменена — требуется паллетизация для возврата"
        return "Отменена"
    if is_order_returned_for_rework(order):
        return "На доработке"
    dispatch_stage = dispatch_stage or shipping_dispatch_stage(order)
    payload = dispatch_stage.get("payload") or {}
    trip_status = str(payload.get("trip_status") or "").strip()
    if dispatch_stage["warehouse_confirmed"]:
        if not dispatch_stage["logistician_signed"]:
            return "Погрузка подтверждена, акт ожидает логиста"
        if not dispatch_stage["manager_signed"]:
            return "Акт отгрузки на подписи менеджера"
        if dispatch_stage["act_sent"]:
            return "Акт отгрузки отправлен клиенту"
    try:
        from logistics.routing_services import routing_snapshot

        snap = routing_snapshot(order)
        if snap.get("needs_clarification"):
            return "Требуется уточнение для логистики"
        if snap.get("status_label") and order.status == ShippingOrder.STATUS_PACKED and snap.get("status") in {
            "ready_for_routing",
            "routed",
            "in_transit",
        }:
            if snap.get("status") == "ready_for_routing" and not snap.get("trip_number"):
                return "Готова к маршрутизации"
            if snap.get("status") == "routed":
                return f"Рейс назначен{(' · ' + snap['trip_number']) if snap.get('trip_number') else ''}"
            if snap.get("status") == "in_transit":
                return "В пути"
    except Exception:
        pass
    base_result = WarehouseGoodsStateResolver.resolve_for_shipping_order(order, trip_status=trip_status)
    if order.status == ShippingOrder.STATUS_PICKING and base_result.code == WarehouseStateCode.IN_OTG:
        progress = shipping_otg_tasks_progress(order)
        if int(progress.get("active") or 0) > 0:
            return "Доставка в зону отгрузки (ричтрак)"
    if order.status == ShippingOrder.STATUS_PACKED:
        if trip_status == "completed":
            return "Рейс завершен"
        if dispatch_stage["logistician_signed"] and not dispatch_stage["manager_signed"]:
            return "Акт отгрузки на подписи менеджера"
        if dispatch_stage["has_trip"] and dispatch_stage["requires_trip"] and trip_status not in {"departed", "loading"}:
            return "Логист сформировал рейс, ожидается погрузка"
    return base_result.label_for("default")


def active_shipping_attachments(order: ShippingOrder):
    cutoff = timezone.now() - timedelta(days=ShippingOrderAttachment.RETENTION_DAYS)
    return order.attachments.select_related("uploaded_by").filter(uploaded_at__gte=cutoff).order_by("-uploaded_at")


def shipping_attachment_names(order: ShippingOrder) -> list[str]:
    return [attachment.filename for attachment in active_shipping_attachments(order)]



_SHIPPING_DISCREPANCY_WARNING_PREFIX = "\u0420\u0430\u0441\u0445\u043e\u0436\u0434\u0435\u043d\u0438\u0435 \u043e\u0442\u0433\u0440\u0443\u0437\u043a\u0438:"
_OTG_PENDING_SOURCE_BLOCKED_STATES = {
    "ready_for_loading",
    "assigned_to_trip",
    "loading_in_progress",
    "loaded_to_vehicle",
    "shipped",
}


def _payload_box_codes(payload: dict) -> list[str]:
    if not isinstance(payload, dict):
        return []
    for key in ("planned_box_codes", "selected_box_codes", "reserved_box_codes", "requested_boxes"):
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        codes: list[str] = []
        seen: set[str] = set()
        for value in values:
            if isinstance(value, dict):
                raw_code = value.get("box_code") or value.get("code") or value.get("container_code")
            else:
                raw_code = value
            code = str(raw_code or "").strip()
            code_key = code.lower()
            if not code_key or code_key in seen:
                continue
            seen.add(code_key)
            codes.append(code)
        if codes:
            return codes
    return []


def _pending_otg_task_box_codes(order: ShippingOrder, warehouse_box_keys: set[str]) -> set[str]:
    order_pk = str(getattr(order, "pk", "") or getattr(order, "id", "") or "").strip()
    order_number = str(getattr(order, "number", "") or "").strip()
    if not order_pk and not order_number:
        return set()

    task_statuses = [MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS]
    tasks = MoveTask.objects.filter(
        status__in=task_statuses,
        request__context_type="manual",
        request__context_id=order_pk,
    ).order_by("id")
    if not tasks.exists() and order_number:
        tasks = MoveTask.objects.filter(
            status__in=task_statuses,
            payload__shipping_order_id=order_number,
        ).order_by("id")

    pending_codes: list[str] = []
    seen_pending: set[str] = set()
    for task in tasks:
        payload = task.payload if isinstance(task.payload, dict) else {}
        to_location = payload.get("to_location") if isinstance(payload.get("to_location"), dict) else {}
        if str(to_location.get("zone") or "").strip().upper() != "OTG":
            continue
        for code in _payload_box_codes(payload):
            code_key = code.lower()
            if code_key in warehouse_box_keys or code_key in seen_pending:
                continue
            seen_pending.add(code_key)
            pending_codes.append(code)

    if not pending_codes:
        return set()

    snapshots = WarehouseStockSnapshot.objects.filter(
        agency=order.agency,
        is_archived=False,
        container_code__in=pending_codes,
        qty__gt=0,
    ).order_by("container_code", "-id")
    usable_codes: set[str] = set()
    for snapshot in snapshots:
        code_key = str(snapshot.container_code or "").strip().lower()
        if not code_key or code_key in usable_codes:
            continue
        state = str(snapshot.warehouse_state_code or "").strip().lower()
        zone = str(getattr(snapshot, "zone_code", "") or "").strip().upper()
        if state in _OTG_PENDING_SOURCE_BLOCKED_STATES or zone == "OTG":
            continue
        if _to_int(snapshot.processing_reserved_qty) > 0 or _to_int(snapshot.other_reserved_qty) > 0:
            continue
        if _to_int(snapshot.available_qty) <= 0 and _to_int(snapshot.shipping_reserved_qty) <= 0:
            continue
        usable_codes.add(code_key)
    return usable_codes


def _missing_otg_boxes_covered_by_pending_tasks(order: ShippingOrder, truth: ShippingTruthService) -> bool:
    expected_count = _to_int(truth.expected_box_count)
    warehouse_box_keys = set(truth.warehouse_box_codes)
    if expected_count <= 0 or len(warehouse_box_keys) >= expected_count:
        return False
    pending_box_keys = _pending_otg_task_box_codes(order, warehouse_box_keys)
    return len(warehouse_box_keys | pending_box_keys) >= expected_count


def shipping_integrity_warnings(order: ShippingOrder, *, packing_summary: dict | None = None) -> list[str]:
    summary = packing_summary if isinstance(packing_summary, dict) else _shipping_packing_summary(order)
    truth = ShippingTruthService.for_order(order)
    warnings = truth.integrity_warnings(packing_summary=summary)
    if order.status == ShippingOrder.STATUS_CANCELED and truth.warehouse_box_codes:
        if summary:
            return_warning = (
                "Заявка отменена. Возвратная паллетизация завершена; "
                f"в зоне OTG остаются короба: {len(truth.warehouse_box_codes)}. "
                "Товар уже доступен клиенту; требуется выполнить задание ричтрака "
                "и вернуть его на хранение."
            )
        else:
            return_warning = (
                "Заявка отменена. В зоне OTG остаются короба: "
                f"{len(truth.warehouse_box_codes)}. "
                "Товар уже доступен клиенту; требуется разложить короба "
                "по возвратным паллетам."
            )
        warnings.insert(
            0,
            return_warning,
        )
    if not warnings or not _missing_otg_boxes_covered_by_pending_tasks(order, truth):
        return warnings
    return [
        warning
        for warning in warnings
        if not str(warning or "").startswith(_SHIPPING_DISCREPANCY_WARNING_PREFIX)
    ]


def _shipping_history_actor(entry: OrderAuditEntry) -> str:
    user = getattr(entry, "user", None)
    if user:
        full_name = str(user.get_full_name() or "").strip()
        if full_name:
            return _repair_shipping_history_text(full_name)
        username = str(user.get_username() or "").strip()
        if username:
            return _repair_shipping_history_text(username)
    payload = entry.payload if isinstance(entry.payload, dict) else {}
    for key in ("actor", "user", "employee", "manager"):
        value = str(payload.get(key) or "").strip()
        if value:
            repaired = _repair_shipping_history_text(value)
            if not _shipping_history_is_question_noise(repaired):
                return repaired
    actor_from_payload = _shipping_history_actor_from_payload(payload)
    if actor_from_payload:
        return actor_from_payload
    return "-"


_SHIPPING_HISTORY_MOJIBAKE_MARKERS = (
    "\u00d0",
    "\u00d1",
    "\u0420\u045f",
    "\u0420\u040e",
    "\u0420\u2019",
    "\u0420\u0452",
    "\u0420\u0405",
    "\u0420\u00b0",
    "\u0420\u00b5",
    "\u0420\u0451",
    "\u0420\u0455",
    "\u0420\u0491",
    "\u0420\u00bb",
    "\u0420\u0458",
    "\u0420\u0457",
    "\u0421\u201a",
    "\u0421\u0403",
    "\u0421\u040f",
    "\u0421\u040a",
    "\u0432\u0402",
    "\ufffd",
)


def _shipping_history_text_score(text: str) -> tuple[int, int, int]:
    cyrillic_count = sum(0x0400 <= ord(char) <= 0x04FF for char in text)
    mojibake_count = sum(text.count(marker) for marker in _SHIPPING_HISTORY_MOJIBAKE_MARKERS)
    question_noise = text.count("?") // 3
    return (cyrillic_count - mojibake_count * 4 - question_noise * 2, cyrillic_count, -mojibake_count)


def _repair_shipping_history_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    candidates = [text]
    for encoding in ("cp1251", "latin1"):
        try:
            repaired = text.encode(encoding).decode("utf-8").strip()
        except UnicodeError:
            continue
        if repaired and repaired not in candidates:
            candidates.append(repaired)

    return max(candidates, key=_shipping_history_text_score)


def _shipping_history_is_question_noise(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    question_count = text.count("?")
    visible_count = sum(1 for char in text if not char.isspace())
    return question_count >= 6 and question_count * 2 >= visible_count


def _shipping_history_actor_from_payload(payload: dict) -> str:
    actor_role = str(payload.get("actor_role") or "").strip().lower()
    if actor_role in {"reachtruck", "reachtruck_driver"}:
        return "Ричтрак"
    if actor_role == "storekeeper":
        return "Склад"
    if actor_role == "manager":
        return "Менеджер"
    if actor_role == "client":
        return "Клиент"
    if actor_role == "logistician":
        return "Логист"
    return ""


def _shipping_history_text_from_payload(payload: dict) -> str:
    event = str(payload.get("event") or "").strip()
    source = str(payload.get("source") or "").strip()
    if source == "codex_data_sync" and event.startswith("completed_delivery_"):
        parts = ["Доставка в OTG зафиксирована: товар доставлен в OTG, резервы сверены."]
        otg_boxes = payload.get("otg_boxes")
        if otg_boxes not in (None, ""):
            parts.append(f"Коробов в OTG: {otg_boxes}.")
        false_claims = payload.get("false_claims_cancelled")
        if isinstance(false_claims, list) and false_claims:
            parts.append(f"Отменено ложных претензий: {len(false_claims)}.")
        return " ".join(parts)
    if source == "codex_data_sync" and event.startswith("partial_delivery_"):
        parts = ["Частичная доставка в OTG:"]
        delivered_otg = payload.get("delivered_otg")
        requested = payload.get("requested")
        if delivered_otg not in (None, "") and requested not in (None, ""):
            parts.append(f"в OTG {delivered_otg}/{requested} коробов.")
        elif delivered_otg not in (None, ""):
            parts.append(f"в OTG {delivered_otg} коробов.")
        active_claimed = payload.get("active_claimed")
        if active_claimed not in (None, "", 0):
            parts.append(f"В активном отборе: {active_claimed}.")
        shortage = payload.get("shortage")
        if shortage not in (None, "", 0):
            parts.append(f"Не хватает: {shortage}.")
        canceled_plan_codes = payload.get("canceled_plan_codes")
        if isinstance(canceled_plan_codes, list) and canceled_plan_codes:
            parts.append(f"Отменено ошибочных планов: {len(canceled_plan_codes)}.")
        return " ".join(parts)
    return ""


def _shipping_history_text(entry: OrderAuditEntry) -> str:
    description = str(entry.description or "").strip()
    payload = entry.payload if isinstance(entry.payload, dict) else {}
    if description:
        repaired = _repair_shipping_history_text(description)
        if not _shipping_history_is_question_noise(repaired):
            return repaired
        payload_text = _shipping_history_text_from_payload(payload)
        if payload_text:
            return payload_text
        return repaired
    payload_text = _shipping_history_text_from_payload(payload)
    if payload_text:
        return payload_text
    for key in ("message", "comment", "status_label", "title", "detail"):
        value = str(payload.get(key) or "").strip()
        if value:
            return _repair_shipping_history_text(value)
    return ""


def _shipping_history_role_from_text(*values: str) -> str:
    text = " ".join(str(value or "").strip().lower() for value in values if str(value or "").strip())
    if not text:
        return ""
    if any(marker in text for marker in ("кладовщик", "storekeeper", "склад")):
        return "storekeeper"
    if any(marker in text for marker in ("главный менеджер", "менеджер", "manager", "согласован")):
        return "manager"
    if any(marker in text for marker in ("клиент", "client", "ип ", "индивидуальный предприниматель")):
        return "client"
    if any(marker in text for marker in ("логист", "logistician", "рейс")):
        return "logistician"
    if any(marker in text for marker in ("ричтрак", "reachtruck", "водитель ричтрака")):
        return "reachtruck"
    if any(marker in text for marker in ("администратор", "разработчик", "admin", "developer")):
        return "admin"
    return ""


def _shipping_history_actor_role(entry: OrderAuditEntry) -> str:
    user = getattr(entry, "user", None)
    if user:
        try:
            employee = user.employee_profile
        except Employee.DoesNotExist:
            employee = None
        if employee and getattr(employee, "role", ""):
            role = str(employee.role or "").strip()
            if role:
                return role
        if not bool(getattr(user, "is_staff", False)):
            return "client"
    payload = entry.payload if isinstance(entry.payload, dict) else {}
    for key in ("actor_role", "performed_by_role", "requested_by_role", "assigned_by_role", "role"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    inferred = _shipping_history_role_from_text(
        _shipping_history_actor(entry),
        entry.description,
        entry.get_action_display(),
        payload.get("title"),
        payload.get("message"),
        payload.get("comment"),
        payload.get("status_label"),
        payload.get("detail"),
        payload.get("actor"),
        payload.get("user"),
    )
    if inferred:
        return inferred
    return "system"


def _shipping_history_actor_color(actor_role: str) -> str:
    role = str(actor_role or "").strip()
    if role in {"manager", "head_manager", "director"}:
        return "manager"
    if role == "storekeeper":
        return "storekeeper"
    if role == "client":
        return "client"
    if role == "logistician":
        return "logistician"
    if role in {"reachtruck", "reachtruck_driver"}:
        return "reachtruck"
    if role in {"admin", "developer"}:
        return "admin"
    return "system"


def _shipping_history_role_label(actor_color: str) -> str:
    return {
        "storekeeper": "Склад",
        "manager": "Менеджер",
        "client": "Клиент",
        "logistician": "Логист",
        "reachtruck": "Ричтрак",
        "admin": "Админ",
        "system": "Система",
    }.get(str(actor_color or "").strip(), "Система")


def shipping_history_rows(order: ShippingOrder, *, limit: int = 30) -> list[dict]:
    entries = (
        OrderAuditEntry.objects.filter(order_type="shipping", order_id=order.number)
        .select_related("user", "user__employee_profile")
        .order_by("-created_at", "-id")[:limit]
    )
    rows: list[dict] = []
    for entry in entries:
        actor_label = _shipping_history_actor(entry)
        actor_role = _shipping_history_actor_role(entry)
        actor_color = _shipping_history_actor_color(actor_role)
        rows.append(
            {
                "created_at": entry.created_at,
                "action_label": entry.get_action_display(),
                "actor_label": actor_label,
                "actor_color": actor_color,
                "actor_role_label": _shipping_history_role_label(actor_color),
                "text": _shipping_history_text(entry),
            }
        )
    return rows


def build_shipping_detail_context(
    order: ShippingOrder,
    *,
    scope: str | None,
    role: str | None,
    stock_rows: list[dict],
    available_items: list[dict],
    order_box_count: int,
    can_write: bool,
    can_edit_items: bool,
    can_submit_for_approval: bool,
    can_manager_approve: bool,
    can_manager_reopen: bool,
    can_storekeeper_accept: bool,
    can_storekeeper_pick: bool,
    can_storekeeper_pack: bool,
    can_storekeeper_manage_packing: bool,
    can_cancel: bool,
    can_edit_order_form: bool,
) -> dict:
    shipping_metrics = {
        "pallet_count": 0,
        "box_count": int(order_box_count or 0),
    }
    packing_summary = _shipping_packing_summary(order)
    integrity_warnings = shipping_integrity_warnings(order, packing_summary=packing_summary)
    items = list(order.items.order_by("id"))
    truth = ShippingTruthService.for_order(order)
    _attach_otg_delivered_qty_to_items(items, truth)
    if packing_summary:
        shipping_metrics["pallet_count"] = int(packing_summary.get("pallet_count") or 0)
    shipping_metrics["box_count"] = int(order_box_count or 0)
    dispatch_stage = shipping_dispatch_stage(order, packing_summary=packing_summary)
    payload = dispatch_stage.get("payload") or {}
    shipping_metrics["shipped_pallet_count"] = (
        int(payload.get("pallet_count") or 0)
        if order.status in {ShippingOrder.STATUS_SHIPPED, ShippingOrder.STATUS_PARTIAL}
        else 0
    )
    order.ui_status_label = shipping_ui_status_label(order, dispatch_stage=dispatch_stage)
    order.display_number = getattr(order, "display_number", None) or _display_shipping_number(order.number)
    order.resolved_vehicle_number = (
        str(payload.get("trip_vehicle_number") or payload.get("vehicle_number") or order.vehicle_number or "").strip()
    )
    order.resolved_driver_name = (
        str(payload.get("trip_driver_name") or payload.get("driver_name") or "").strip()
    )
    order.resolved_driver_phone = (
        str(payload.get("trip_driver_phone") or payload.get("driver_phone") or order.driver_phone or "").strip()
    )
    routing = {}
    try:
        from logistics.routing_services import (
            CLARIFY_REASONS,
            can_resolve_clarification,
            can_return_for_clarification,
            routing_snapshot,
        )

        routing = routing_snapshot(order)
        routing["can_return"] = can_return_for_clarification(role, order) and scope == "staff"
        routing["can_resolve"] = can_resolve_clarification(role, order) and scope == "staff"
        routing["reason_choices"] = CLARIFY_REASONS
    except Exception:
        routing = {"status": "", "status_label": "", "can_return": False, "can_resolve": False, "reason_choices": []}
    return {
        "order": order,
        "attachments": list(active_shipping_attachments(order)),
        "items": items,
        "reserves": order.reserves.order_by("id"),
        "stock_rows": stock_rows,
        "available_items": available_items[:30],
        "shipping_metrics": shipping_metrics,
        "attachment_retention_days": ShippingOrderAttachment.RETENTION_DAYS,
        "can_write": can_write,
        "can_edit_items": can_edit_items,
        "can_submit_for_approval": can_submit_for_approval,
        "can_manager_approve": can_manager_approve,
        "can_manager_reopen": can_manager_reopen,
        "can_storekeeper_accept": can_storekeeper_accept,
        "can_storekeeper_pick": can_storekeeper_pick,
        "can_storekeeper_pack": can_storekeeper_pack,
        "can_storekeeper_manage_packing": can_storekeeper_manage_packing,
        "can_cancel": can_cancel,
        "can_edit_order_form": can_edit_order_form,
        "edit_order_url": f"/shipping/new/?order={order.pk}&edit=1",
        "packing_url": f"/shipping/{order.pk}/packing/",
        "packing_slips_url": f"/shipping/{order.pk}/packing-slips/",
        "packing_summary": packing_summary,
        "shipping_integrity_warnings": integrity_warnings,
        "shipping_history": shipping_history_rows(order) if scope != "client" else [],
        "shipping_history_visible": scope != "client",
        "can_view_packing_slips": bool(packing_summary) and scope == "staff" and role == "storekeeper",
        "dispatch_stage": dispatch_stage,
        "dispatch_trip_url": reverse("logistics:trip-detail", args=[dispatch_stage["trip_link"].trip_id])
        if dispatch_stage["trip_link"]
        else "",
        "dispatch_act_url": reverse("shipping:dispatch-act", args=[order.pk]),
        "can_view_dispatch_act": order.status in {
            ShippingOrder.STATUS_PACKED,
            ShippingOrder.STATUS_SHIPPED,
            ShippingOrder.STATUS_PARTIAL,
        },
        "routing": routing,
        "scope": scope,
        "role": role,
        "selected_client": order.agency,
        "cabinet_url": resolve_cabinet_url(
            role,
            client_id=order.agency_id if role == "client" else None,
        ),
    }


def _shipping_item_otg_key(row) -> tuple[str, str, str, str]:
    return (
        str(getattr(row, "barcode", "") or "").strip().lower(),
        str(getattr(row, "size", "") or "").strip().lower(),
        str(getattr(row, "goods_type", "") or "").strip().lower(),
        str(getattr(row, "sku_code", "") or "").strip().lower(),
    )


def _attach_otg_delivered_qty_to_items(items: list, truth: ShippingTruthService) -> None:
    qty_by_key: dict[tuple[str, str, str, str], int] = {}
    for snapshot in truth.warehouse_snapshots:
        key = _shipping_item_otg_key(snapshot)
        if not key[0] and not key[3]:
            continue
        qty_by_key[key] = qty_by_key.get(key, 0) + max(_to_int(getattr(snapshot, "qty", 0)), 0)

    remaining_by_key = dict(qty_by_key)
    for item in items:
        key = _shipping_item_otg_key(item)
        remaining = remaining_by_key.get(key, 0)
        requested = max(_to_int(getattr(item, "qty_requested", 0)), 0)
        delivered = min(remaining, requested) if requested > 0 else remaining
        setattr(item, "qty_otg_delivered", delivered)
        shipped = max(_to_int(getattr(item, "qty_shipped", 0)), 0)
        setattr(item, "qty_to_deliver_to_otg", max(requested - delivered - shipped, 0))
        remaining_by_key[key] = max(remaining - delivered, 0)


def build_shipping_dispatch_context(
    order: ShippingOrder,
    *,
    role: str | None,
    sign_status: str = "",
    sign_error: str = "",
    packing_summary: dict | None = None,
) -> dict:
    summary = packing_summary if isinstance(packing_summary, dict) else _shipping_packing_summary(order)
    dispatch_stage = shipping_dispatch_stage(order, packing_summary=summary)
    payload = dispatch_stage["payload"]
    trip_link = dispatch_stage["trip_link"]
    trip = trip_link.trip if trip_link else None
    logistician_employee = shipping_dispatch_employee(payload, "act_logistician_employee_id")
    manager_employee = shipping_dispatch_employee(payload, "act_manager_employee_id")
    signable_statuses = {
        ShippingOrder.STATUS_PACKED,
        ShippingOrder.STATUS_SHIPPED,
        ShippingOrder.STATUS_PARTIAL,
    }
    can_logistician_sign = (
        order.status in signable_statuses
        and is_logistician_role(role)
        and dispatch_stage["warehouse_confirmed"]
        and not dispatch_stage["logistician_signed"]
        and not dispatch_stage["manager_signed"]
        and (not dispatch_stage["requires_trip"] or dispatch_stage["has_trip"])
    )
    can_manager_sign = (
        order.status in signable_statuses
        and is_manager_role(role)
        and dispatch_stage["warehouse_confirmed"]
        and dispatch_stage["logistician_signed"]
        and not dispatch_stage["manager_signed"]
    )
    items = []
    for item in payload.get("act_items") or []:
        if not isinstance(item, dict):
            continue
        name_parts = [str(item.get("name") or "-").strip() or "-"]
        size = str(item.get("size") or "").strip()
        if size:
            name_parts.append(f"р-р {size}")
        items.append(
            {
                "sku_code": item.get("sku_code") or "-",
                "barcode": item.get("barcode") or "-",
                "name": ", ".join(name_parts),
                "qty_requested": int(item.get("qty_requested") or 0),
                "qty_reserved": int(item.get("qty_reserved") or 0),
                "qty_shipped": int(item.get("qty_shipped") or 0),
                "shortage_qty": int(item.get("shortage_qty") or 0),
                "excess_qty": int(item.get("excess_qty") or 0),
            }
        )

    return {
        "order": order,
        "payload": payload,
        "dispatch_stage": dispatch_stage,
        "trip": trip,
        "trip_link": trip_link,
        "items": items,
        "pallets": payload.get("act_pallets") or [],
        "logistician_employee": logistician_employee,
        "manager_employee": manager_employee,
        "logistician_signed_at": format_shipping_datetime(payload.get("act_logistician_signed_at")),
        "manager_signed_at": format_shipping_datetime(payload.get("act_manager_signed_at")),
        "act_sent_at": format_shipping_datetime(payload.get("act_sent_at")),
        "warehouse_confirmed_at": format_shipping_datetime(
            payload.get("act_warehouse_confirmed_at")
        ),
        "can_logistician_sign": can_logistician_sign,
        "can_manager_sign": can_manager_sign,
        "requires_trip": dispatch_stage["requires_trip"],
        "has_trip": dispatch_stage["has_trip"],
        "sign_status": sign_status,
        "sign_error": sign_error,
        "detail_url": reverse("shipping:detail", args=[order.pk]),
        "cabinet_url": resolve_cabinet_url(
            role,
            client_id=order.agency_id if role == "client" else None,
        ),
        "role": role,
    }
