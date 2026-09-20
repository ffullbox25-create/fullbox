from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import date
import hashlib
import json
import logging
import re
from types import SimpleNamespace
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Max
from django.utils import timezone

from audit.models import OrderAuditEntry
from reachtruck.models import BoxClaim, MoveRequest, MoveRequestItem, MoveTask
from reachtruck.services.claims import (
    active_box_claim_codes,
    active_pallet_lock_codes,
    unavailable_box_claim_codes,
)
from reachtruck.services.move_requests import (
    _active_pallet_codes_for_agency,
    _as_int,
    _build_location,
    _cancel_open_shipping_pick_tasks,
    _location_label,
    _move_instruction,
    _order_has_pool_shipping_reserves,
    _order_has_shipping_reserves,
    _row_value,
    _shipping_full_pallet_instruction,
    _shipping_remaining_qty_by_item_id,
    _shipping_reserved_rows_for_order,
    _warehouse_base_rows_for_planning,
    create_stock_move_task,
    sync_task_status_by_legacy_order_id,
)
from reachtruck.services.pallet_ops import (
    BOX_SELECTION_FIXED,
    BOX_SELECTION_PATTERN_MATCHING,
    MOVE_MODE_BOX_FULL,
    MOVE_MODE_BOX_PARTIAL,
    MOVE_MODE_PALLET_FULL,
    _all_stock_boxes_for_pallet,
    _payload_box_codes,
    _requested_box_selection_mode,
    _stock_box_matches_requested_pattern,
)
from shipping.box_splits import encode_partial_box_split, extract_partial_box_split
from shipping.item_binding import enforced_ozon_request_binding_errors
from shipping.models import ShippingOrder, ShippingOrderItem
from sklad.models import WarehouseOperation, WarehouseOperationTask, WarehouseStockSnapshot
from sklad.services.stock_operations import OperationalStockService
from sklad.services.stock_availability import StockAvailabilityService
from sklad.services.warehouse_transitions import WarehouseStateCode
from sklad.services.warehouse_write_path import WarehouseWritePathService
from todo.models import Task
from sku.models import Agency

from .models import OtgDeliveryDemand, OtgDeliveryRequest, OtgPalletPlan, OtgPlanningEvent


_BOX_COUNT_RE = re.compile(r"коробов:\s*(\d+)", re.IGNORECASE)
_BOX_QTY_RE = re.compile(r"кратность:\s*(\d+)", re.IGNORECASE)
_BOX_CODES_RE = re.compile(r"короба:\s*([^;]+)", re.IGNORECASE)
_PARTIAL_BOX_SPLIT_TOKEN_RE = re.compile(r"split_box:b64:[A-Za-z0-9_-]+={0,2}")
_ACTIVE_OTG_STATUSES = {
    OtgDeliveryRequest.STATUS_REQUESTED,
    OtgDeliveryRequest.STATUS_PLANNING,
    OtgDeliveryRequest.STATUS_PLANNED,
    OtgDeliveryRequest.STATUS_DISPATCHED,
    OtgDeliveryRequest.STATUS_IN_PROGRESS,
    OtgDeliveryRequest.STATUS_PARTIAL,
}
_OTG_DELIVERED_STATE_CODES = {
    WarehouseStateCode.IN_OTG.value,
    WarehouseStateCode.PALLETIZING.value,
    WarehouseStateCode.READY_FOR_LOADING.value,
    WarehouseStateCode.ASSIGNED_TO_TRIP.value,
    WarehouseStateCode.LOADING_IN_PROGRESS.value,
    WarehouseStateCode.LOADED_TO_VEHICLE.value,
    WarehouseStateCode.SHIPPED.value,
}
_FINAL_ACTIVE_OPERATION_STATUSES = {"done", "canceled", "cancelled", "failed"}
_OTG_SCAN_FACT_MODE = "scan_facts_v1"
_OTG_PACKING_FACT_MODE = "shipping_packing_v1"
_SUPPLEMENTAL_OTG_PICK_REASON = "shipping_supplement_pick"
_DISCREPANCY_OTG_PICK_REASON = "shipping_discrepancy_v2"
_WAITING_FOR_STOCK_KEY = "waiting_for_stock"
_NO_STOCK_BLOCKED_REASON = "no_stock_at_planned_pallet"
_ACTIVE_SOURCE_WAIT_MESSAGE = (
    "Паллеты с зарезервированным товаром уже заняты активными заданиями склада. "
    "Заявка ожидает освобождения; дефицит остатков не подтверждён."
)
_PREEMPTIBLE_OTG_SOURCE_ZONES = {"PR", "OBR", "OS"}
_SOURCE_BOX_HOLD_STATUSES = {
    ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
    ShippingOrder.STATUS_PICKING,
    ShippingOrder.STATUS_PACKED,
}


logger = logging.getLogger(__name__)


def _preemptible_storage_route_details(
    task: MoveTask,
    *,
    lock_warehouse_rows: bool = False,
) -> dict | None:
    """Return linked planning rows only when a storage route has no warehouse fact."""

    payload = dict(task.payload or {})
    from_location = payload.get("from_location") if isinstance(payload.get("from_location"), dict) else {}
    to_location = payload.get("to_location") if isinstance(payload.get("to_location"), dict) else {}
    from_zone = _normalize_text(task.from_zone or from_location.get("zone")).upper()
    to_zone = _normalize_text(task.to_zone or to_location.get("zone")).upper()
    request_destination = _normalize_text(getattr(task.request, "destination_zone", "")).upper()
    if (
        task.status != MoveTask.STATUS_CREATED
        or from_zone not in _PREEMPTIBLE_OTG_SOURCE_ZONES
        or to_zone != "OS"
        or request_destination != "OS"
        or task.assigned_to_id
        or _normalize_text(task.assigned_to_name)
        or task.started_at is not None
        or task.completed_at is not None
        or task.canceled_at is not None
        or _as_int(task.qty_done) > 0
        or payload.get("fbs_replenishment_bridge_v1")
        or _as_int(payload.get("assigned_to_id")) > 0
        or _normalize_text(payload.get("assigned_to_name"))
    ):
        return None

    execution = payload.get("mobile_execution")
    if isinstance(execution, dict):
        for field_name in (
            "source_confirmed",
            "pallet_confirmed",
            "destination_confirmed",
            "boxes_scanned",
            "units_scanned",
            "last_scan",
            "scanned_box_codes",
            "scanned_unit_codes",
        ):
            if execution.get(field_name):
                return None
    for field_name in (
        "picked_boxes",
        "picked_rows",
        "picked_qty",
        "moved_boxes",
        "actual_box_codes",
        "confirmed_box_codes",
    ):
        if payload.get(field_name):
            return None
    if task.box_claims.filter(status="claimed").exists():
        return None
    if task.pallet_locks.filter(status="active").exists():
        return None

    warehouse_task_id = _as_int(payload.get("warehouse_operation_task_id"))
    warehouse_operation_id = _as_int(payload.get("warehouse_operation_id"))
    warehouse_task = None
    operation = None
    snapshot_ids: list[int] = []
    if warehouse_task_id > 0:
        warehouse_task_qs = WarehouseOperationTask.objects.select_related("operation")
        if lock_warehouse_rows:
            warehouse_task_qs = warehouse_task_qs.select_for_update()
        warehouse_task = warehouse_task_qs.filter(pk=warehouse_task_id).first()
        if warehouse_task is None:
            return None
        operation = warehouse_task.operation
        if warehouse_operation_id > 0 and int(operation.id) != warehouse_operation_id:
            return None
        if (
            warehouse_task.status != WarehouseOperationTask.STATUS_CREATED
            or warehouse_task.assigned_to_id
            or _normalize_text(warehouse_task.assigned_to_name)
            or warehouse_task.started_at is not None
            or warehouse_task.completed_at is not None
            or _as_int(warehouse_task.qty_done) > 0
        ):
            return None
        snapshot_ids = [
            _as_int(snapshot_id)
            for snapshot_id in dict(warehouse_task.payload or {}).get("snapshot_ids", [])
            if _as_int(snapshot_id) > 0
        ]
    elif warehouse_operation_id > 0:
        operation_qs = WarehouseOperation.objects.all()
        if lock_warehouse_rows:
            operation_qs = operation_qs.select_for_update()
        operation = operation_qs.filter(pk=warehouse_operation_id).first()
        if operation is None or operation.tasks.exists():
            return None

    if operation is not None:
        if (
            operation.status
            not in {
                WarehouseOperation.STATUS_CREATED,
                WarehouseOperation.STATUS_PLANNED,
                WarehouseOperation.STATUS_BLOCKED,
            }
            or operation.started_at is not None
            or operation.completed_at is not None
            or _as_int(operation.done_qty) > 0
        ):
            return None
        if not snapshot_ids and operation.tasks.exclude(pk=getattr(warehouse_task, "pk", None)).exists():
            return None

    return {
        "from_zone": from_zone,
        "operation": operation,
        "warehouse_task": warehouse_task,
        "snapshot_ids": snapshot_ids,
    }


def _preemptible_storage_operation_ids(agency_id: int | None) -> set[int]:
    operation_ids: set[int] = set()
    tasks = MoveTask.objects.select_related("request").filter(status=MoveTask.STATUS_CREATED)
    if agency_id:
        tasks = tasks.filter(request__agency_id=agency_id)
    for task in tasks.exclude(pallet_code="").order_by("id"):
        details = _preemptible_storage_route_details(task)
        operation = details.get("operation") if details else None
        if operation is not None:
            operation_ids.add(int(operation.id))
    return operation_ids


def _unstarted_exact_box_route_codes(task: MoveTask) -> set[str]:
    """Return exact source boxes only while this box route has no execution fact."""

    payload = dict(task.payload or {})
    move_mode = _normalize_text(payload.get("move_mode") or task.move_mode).lower()
    if (
        task.status != MoveTask.STATUS_CREATED
        or move_mode != MOVE_MODE_BOX_FULL
        or task.assigned_to_id
        or _normalize_text(task.assigned_to_name)
        or task.started_at is not None
        or task.completed_at is not None
        or task.canceled_at is not None
        or _as_int(task.qty_done) > 0
        or _as_int(payload.get("assigned_to_id")) > 0
        or _normalize_text(payload.get("assigned_to_name"))
    ):
        return set()

    execution = payload.get("mobile_execution")
    if isinstance(execution, dict) and any(
        execution.get(field_name)
        for field_name in (
            "source_confirmed",
            "pallet_confirmed",
            "destination_confirmed",
            "boxes_scanned",
            "units_scanned",
            "last_scan",
            "scanned_box_codes",
            "scanned_unit_codes",
        )
    ):
        return set()
    if any(
        payload.get(field_name)
        for field_name in (
            "picked_boxes",
            "picked_rows",
            "picked_qty",
            "moved_boxes",
            "actual_box_codes",
            "confirmed_box_codes",
        )
    ):
        return set()
    if task.pallet_locks.select_for_update().filter(status="active").exists():
        return set()

    box_keys: set[str] = set()
    for field_name in (
        "planned_box_codes",
        "selected_box_codes",
        "reserved_box_codes",
        "requested_boxes",
    ):
        raw_codes = payload.get(field_name)
        if isinstance(raw_codes, str):
            raw_codes = _payload_box_codes({"requested_boxes": raw_codes})
        if not isinstance(raw_codes, (list, tuple, set)):
            continue
        box_keys.update(
            _normalize_text(code).lower()
            for code in raw_codes
            if _normalize_text(code)
        )
    for field_name in (
        "requested_box",
        "source_box_code",
        "selected_box_code",
        "reserved_box_code",
    ):
        code_key = _normalize_text(payload.get(field_name)).lower()
        if code_key:
            box_keys.add(code_key)
    box_keys.update(
        _normalize_text(code).lower()
        for code in task.box_claims.select_for_update()
        .filter(status=BoxClaim.STATUS_CLAIMED)
        .values_list("box_code", flat=True)
        if _normalize_text(code)
    )
    return box_keys


def _cancel_preemptible_storage_routes_for_otg(
    *,
    agency_id: int | None,
    pallet_codes: set[str],
    selected_box_keys_by_pallet: dict[str, set[str]] | None,
    order: ShippingOrder,
) -> list[str]:
    normalized_pallet_codes = {
        _normalize_text(code)
        for code in pallet_codes
        if _normalize_text(code)
    }
    if not normalized_pallet_codes:
        return []

    tasks = (
        MoveTask.objects.select_for_update()
        .select_related("request")
        .filter(
            pallet_code__in=sorted(normalized_pallet_codes),
            status__in=[MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS],
        )
        .exclude(pallet_code="")
        .order_by("id")
    )
    if agency_id:
        tasks = tasks.filter(request__agency_id=agency_id)

    canceled_ids: list[str] = []
    for task in tasks:
        details = _preemptible_storage_route_details(task, lock_warehouse_rows=True)
        if details is None:
            payload = dict(task.payload or {})
            move_mode = _normalize_text(payload.get("move_mode") or task.move_mode).lower()
            destination_zone = _normalize_text(
                task.to_zone or task.request.destination_zone
            ).upper()
            if (
                move_mode == MOVE_MODE_BOX_FULL
                and payload.get("otg_scan_fact_mode") == _OTG_SCAN_FACT_MODE
                and destination_zone == "OTG"
            ):
                # Several shipping requests may take different boxes from one
                # pallet. Exact provisional box guards are checked by the
                # planner; only a whole-pallet route must own the entire pallet.
                continue
            task_box_keys = _unstarted_exact_box_route_codes(task)
            otg_box_keys = {
                _normalize_text(code).lower()
                for code in (selected_box_keys_by_pallet or {}).get(
                    _normalize_text(task.pallet_code).lower(),
                    set(),
                )
                if _normalize_text(code)
            }
            if task_box_keys and otg_box_keys and task_box_keys.isdisjoint(otg_box_keys):
                # The pallet is shared only at planning time. Each task owns an
                # exact, non-overlapping set of boxes; once either driver starts,
                # the ordinary pallet lock serializes physical access.
                continue
            raise ValidationError(
                f"Паллета {task.pallet_code} уже взята в работу. Обновите заявку и повторите подбор."
            )
        legacy_order_id = _normalize_text(task.legacy_order_id)
        if not legacy_order_id:
            raise ValidationError(f"У задания паллеты {task.pallet_code} отсутствует складской номер.")
        updated = sync_task_status_by_legacy_order_id(
            legacy_order_id,
            status=MoveTask.STATUS_CANCELED,
        )
        if updated is None:
            raise ValidationError(f"Не удалось отменить старый маршрут паллеты {task.pallet_code}.")
        updated_payload = dict(updated.payload or {})
        updated_payload.update(
            {
                "status": MoveTask.STATUS_CANCELED,
                "status_label": "Отменено: паллета направлена сразу в OTG",
                "superseded_by_otg_shipping": {
                    "shipping_order_id": _normalize_text(order.number),
                    "shipping_order_pk": int(order.pk),
                    "at": timezone.now().isoformat(),
                },
            }
        )
        updated.payload = updated_payload
        updated.save(update_fields=["payload", "updated_at"])

        warehouse_task = details.get("warehouse_task")
        operation = details.get("operation")
        snapshot_ids = list(details.get("snapshot_ids") or [])
        if warehouse_task is not None:
            warehouse_task.status = WarehouseOperationTask.STATUS_CANCELED
            warehouse_task.payload = {
                **dict(warehouse_task.payload or {}),
                "status_label": "Отменено: паллета направлена сразу в OTG",
                "superseded_by_shipping_order": _normalize_text(order.number),
            }
            warehouse_task.save(update_fields=["status", "payload", "updated_at"])
        if operation is not None:
            if snapshot_ids:
                WarehouseStockSnapshot.objects.filter(
                    id__in=snapshot_ids,
                    active_operation=operation,
                ).update(active_operation=None, active_operation_type="")
            statuses = list(operation.tasks.values_list("status", flat=True))
            if not statuses or all(status == WarehouseOperationTask.STATUS_CANCELED for status in statuses):
                operation.status = WarehouseOperation.STATUS_CANCELED
                operation.save(update_fields=["status", "updated_at"])
                WarehouseStockSnapshot.objects.filter(active_operation=operation).update(
                    active_operation=None,
                    active_operation_type="",
                )
        canceled_ids.append(legacy_order_id)
    return canceled_ids


def _active_otg_source_guards(
    agency_id: int | None,
    *,
    exclude_move_request_ids: set[int] | None = None,
) -> tuple[set[str], set[str]]:
    excluded_request_ids = {
        int(request_id)
        for request_id in (exclude_move_request_ids or set())
        if int(request_id or 0) > 0
    }
    blocked_pallets = {
        _normalize_text(code)
        for code in active_pallet_lock_codes(agency_id=agency_id)
        if _normalize_text(code)
    }
    blocked_box_keys = {
        _normalize_text(code).lower()
        for code in active_box_claim_codes(agency_id=agency_id)
        if _normalize_text(code)
    }
    open_tasks = MoveTask.objects.filter(
        status__in=[MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS],
    ).exclude(pallet_code="")
    if agency_id:
        open_tasks = open_tasks.filter(request__agency_id=agency_id)
    if excluded_request_ids:
        open_tasks = open_tasks.exclude(request_id__in=excluded_request_ids)

    planned_box_keys_by_task: dict[int, set[str]] = {}
    for task_id, planned_codes in OtgPalletPlan.objects.filter(
        move_task_id__in=open_tasks.values("id"),
    ).values_list("move_task_id", "planned_box_codes"):
        task_box_keys = planned_box_keys_by_task.setdefault(int(task_id), set())
        for code in planned_codes or []:
            code_key = _normalize_text(code).lower()
            if code_key:
                task_box_keys.add(code_key)

    for task in open_tasks.select_related("request").only(
        "id",
        "request__destination_zone",
        "pallet_code",
        "from_zone",
        "to_zone",
        "move_mode",
        "qty_done",
        "status",
        "assigned_to_id",
        "assigned_to_name",
        "started_at",
        "completed_at",
        "canceled_at",
        "payload",
    ):
        if _preemptible_storage_route_details(task) is not None:
            continue
        payload = dict(task.payload or {})
        move_mode = _normalize_text(payload.get("move_mode") or task.move_mode).lower()
        if move_mode != MOVE_MODE_BOX_FULL:
            pallet_code = _normalize_text(task.pallet_code)
            if pallet_code:
                blocked_pallets.add(pallet_code)
            continue
        task_has_box_code = False
        for field_name in (
            "planned_box_codes",
            "selected_box_codes",
            "reserved_box_codes",
            "requested_boxes",
            "picked_boxes",
            "candidate_box_codes",
        ):
            for code in payload.get(field_name) or []:
                code_key = _normalize_text(code).lower()
                if code_key:
                    blocked_box_keys.add(code_key)
                    task_has_box_code = True
        if not task_has_box_code:
            planned_box_keys = planned_box_keys_by_task.get(int(task.id), set())
            if planned_box_keys:
                blocked_box_keys.update(planned_box_keys)
                task_has_box_code = True
        if not task_has_box_code:
            pallet_code = _normalize_text(task.pallet_code)
            if pallet_code:
                blocked_pallets.add(pallet_code)
    return blocked_pallets, blocked_box_keys


def _task_has_confirmed_otg_scan_fact(task: MoveTask) -> bool:
    if task.status != MoveTask.STATUS_DONE:
        return False
    payload = task.payload if isinstance(task.payload, dict) else {}
    if payload.get("otg_scan_fact_mode") != _OTG_SCAN_FACT_MODE:
        return False
    execution = payload.get("mobile_execution")
    if not isinstance(execution, dict) or not bool(execution.get("destination_confirmed")):
        return False
    picked_qty = int(payload.get("picked_qty") or task.qty_done or 0)
    if picked_qty < int(task.qty_planned or 0) or int(task.qty_done or 0) < int(task.qty_planned or 0):
        return False
    move_mode = _normalize_text(payload.get("move_mode") or task.move_mode).lower()
    if move_mode == MOVE_MODE_BOX_FULL:
        requested_box_count = max(_as_int(payload.get("requested_box_count")), 0)
        picked_boxes = {
            _normalize_text(code).lower()
            for code in (payload.get("picked_boxes") or [])
            if _normalize_text(code)
        }
        if requested_box_count > 0 and len(picked_boxes) != requested_box_count:
            return False
        if _requested_box_selection_mode(payload) == BOX_SELECTION_FIXED:
            requested_boxes = {code.lower() for code in _payload_box_codes(payload)}
            if requested_boxes and requested_boxes != picked_boxes:
                return False
    has_box_scan = bool(execution.get("boxes_scanned") or payload.get("picked_boxes"))
    unit_scans = execution.get("units_scanned") or {}
    has_unit_scan = bool(unit_scans)
    if isinstance(unit_scans, dict):
        has_unit_scan = any(
            any(_as_int(nested_qty) > 0 for nested_qty in qty.values())
            if isinstance(qty, dict)
            else _as_int(qty) > 0
            for qty in unit_scans.values()
        )
    return has_box_scan or has_unit_scan


def _move_request_has_confirmed_otg_scan_facts(move_request: MoveRequest | None) -> bool:
    if move_request is None or move_request.status != MoveRequest.STATUS_DONE:
        return False
    tasks = list(move_request.tasks.all())
    return bool(tasks) and all(_task_has_confirmed_otg_scan_fact(task) for task in tasks)


def _task_has_confirmed_otg_packing_fact(
    task: MoveTask,
    *,
    shipping_order_id: int,
) -> bool:
    if task.status != MoveTask.STATUS_DONE:
        return False
    payload = task.payload if isinstance(task.payload, dict) else {}
    if payload.get("otg_completion_fact_mode") != _OTG_PACKING_FACT_MODE:
        return False
    if not bool(payload.get("auto_completed_by_shipping_packing")):
        return False
    fact = payload.get("shipping_packing_fact")
    if not isinstance(fact, dict):
        return False
    if _as_int(fact.get("shipping_order_id")) != int(shipping_order_id or 0):
        return False
    execution = payload.get("mobile_execution")
    if not isinstance(execution, dict) or not bool(execution.get("destination_confirmed")):
        return False

    picked_boxes = {
        _normalize_text(code).lower()
        for code in (payload.get("picked_boxes") or [])
        if _normalize_text(code)
    }
    fact_boxes = {
        _normalize_text(code).lower()
        for code in (fact.get("box_codes") or [])
        if _normalize_text(code)
    }
    requested_boxes = {
        _normalize_text(code).lower()
        for code in (fact.get("requested_box_codes") or [])
        if _normalize_text(code)
    }
    if not picked_boxes or picked_boxes != fact_boxes or picked_boxes != requested_boxes:
        return False

    raw_qty_by_code = fact.get("packed_qty_by_code")
    if not isinstance(raw_qty_by_code, dict):
        return False
    qty_by_code = {
        _normalize_text(code).lower(): max(_as_int(qty), 0)
        for code, qty in raw_qty_by_code.items()
        if _normalize_text(code)
    }
    if set(qty_by_code) != picked_boxes:
        return False
    packed_qty = sum(qty_by_code.values())
    fact_qty = max(_as_int(fact.get("picked_qty")), 0)
    payload_qty = max(_as_int(payload.get("picked_qty")), 0)
    planned_qty = max(int(task.qty_planned or 0), 0)
    return (
        packed_qty == fact_qty == payload_qty
        and packed_qty >= planned_qty
        and int(task.qty_done or 0) >= planned_qty
    )


def _move_request_confirmed_otg_fact_modes(
    move_request: MoveRequest | None,
    *,
    shipping_order_id: int,
) -> set[str]:
    if move_request is None or move_request.status != MoveRequest.STATUS_DONE:
        return set()
    tasks = list(move_request.tasks.all())
    if not tasks:
        return set()
    modes: set[str] = set()
    for task in tasks:
        if _task_has_confirmed_otg_scan_fact(task):
            modes.add(_OTG_SCAN_FACT_MODE)
        elif _task_has_confirmed_otg_packing_fact(
            task,
            shipping_order_id=shipping_order_id,
        ):
            modes.add(_OTG_PACKING_FACT_MODE)
        else:
            return set()
    return modes


@transaction.atomic
def sync_completed_otg_delivery_request(move_request: MoveRequest) -> list[int]:
    """Close OTG requests backed by strict scans or a verified packing act."""
    otg_request = (
        OtgDeliveryRequest.objects.select_for_update()
        .filter(move_request_id=move_request.pk)
        .first()
    )
    if otg_request is None:
        return []
    completion_fact_modes = _move_request_confirmed_otg_fact_modes(
        move_request,
        shipping_order_id=int(otg_request.shipping_order_id),
    )
    if not completion_fact_modes:
        return []

    request_payload = otg_request.payload if isinstance(otg_request.payload, dict) else {}
    is_supplement = request_payload.get("request_reason") == _SUPPLEMENTAL_OTG_PICK_REASON
    if int(otg_request.shortage_boxes or 0) > 0 and not is_supplement:
        return []

    completed_ids: list[int] = []
    if otg_request.status != OtgDeliveryRequest.STATUS_DONE:
        completion_payload = dict(request_payload)
        completion_payload["completed_move_request_id"] = int(move_request.pk)
        completion_payload["completed_at"] = timezone.now().isoformat()
        if completion_fact_modes == {_OTG_SCAN_FACT_MODE}:
            completion_payload["completed_by_confirmed_scans"] = True
            event_type = "completed_by_confirmed_scans"
            event_message = "Все задания подтверждены сканами и доставлены в OTG."
        else:
            completion_payload["completed_by_confirmed_facts"] = True
            completion_payload["completion_fact_modes"] = sorted(completion_fact_modes)
            completion_payload["completed_by_confirmed_packing"] = (
                _OTG_PACKING_FACT_MODE in completion_fact_modes
            )
            event_type = (
                "completed_by_confirmed_packing"
                if completion_fact_modes == {_OTG_PACKING_FACT_MODE}
                else "completed_by_confirmed_facts"
            )
            event_message = "Все задания подтверждены фактами выполнения и доставлены в OTG."
        otg_request.status = OtgDeliveryRequest.STATUS_DONE
        otg_request.payload = completion_payload
        otg_request.save(update_fields=["status", "payload", "updated_at"])
        _make_event(
            otg_request,
            event_type,
            message=event_message,
            payload={"move_request_id": int(move_request.pk)},
        )
        completed_ids.append(int(otg_request.pk))

    source_request_id = int((request_payload.get("supplemental_pick") or {}).get("source_request_id") or 0)
    if source_request_id <= 0:
        return completed_ids

    source_request = (
        OtgDeliveryRequest.objects.select_for_update()
        .filter(pk=source_request_id, shipping_order_id=otg_request.shipping_order_id)
        .first()
    )
    source_fact_modes = (
        _move_request_confirmed_otg_fact_modes(
            source_request.move_request,
            shipping_order_id=int(source_request.shipping_order_id),
        )
        if source_request is not None
        else set()
    )
    if not source_fact_modes:
        return completed_ids

    supplements = []
    for candidate in OtgDeliveryRequest.objects.filter(shipping_order_id=otg_request.shipping_order_id).exclude(
        pk=source_request.pk
    ):
        payload = candidate.payload if isinstance(candidate.payload, dict) else {}
        linked_source_id = int((payload.get("supplemental_pick") or {}).get("source_request_id") or 0)
        if linked_source_id == source_request.pk:
            supplements.append(candidate)
    if not supplements or any(candidate.status != OtgDeliveryRequest.STATUS_DONE for candidate in supplements):
        return completed_ids

    if source_request.status != OtgDeliveryRequest.STATUS_DONE:
        source_payload = dict(source_request.payload or {})
        if source_fact_modes == {_OTG_SCAN_FACT_MODE}:
            source_payload["completed_by_supplemental_scan_facts"] = True
            source_event_type = "completed_by_supplemental_scan_facts"
        else:
            source_payload["completed_by_supplemental_facts"] = True
            source_payload["completion_fact_modes"] = sorted(source_fact_modes)
            source_event_type = "completed_by_supplemental_facts"
        source_payload["completed_supplement_request_ids"] = [int(candidate.pk) for candidate in supplements]
        source_payload["completed_at"] = timezone.now().isoformat()
        source_request.status = OtgDeliveryRequest.STATUS_DONE
        source_request.payload = source_payload
        source_request.save(update_fields=["status", "payload", "updated_at"])
        _make_event(
            source_request,
            source_event_type,
            message="Исходная недостача закрыта подтверждённым добором.",
            payload={"supplement_request_ids": source_payload["completed_supplement_request_ids"]},
        )
        completed_ids.append(int(source_request.pk))
    return completed_ids


_FINISHED_SHIPPING_STATUSES = frozenset(
    {
        ShippingOrder.STATUS_SHIPPED,
        ShippingOrder.STATUS_PARTIAL,
        ShippingOrder.STATUS_CANCELED,
    }
)
_OPEN_OTG_REQUEST_STATUSES = frozenset(
    _ACTIVE_OTG_STATUSES | {OtgDeliveryRequest.STATUS_BLOCKED}
)


def close_otg_delivery_requests_for_finished_order(shipping_order_id: int) -> list[int]:
    """Finish OTG delivery requests left open after the order reached a final state.

    A delivery request is closed by one all-or-nothing check taken at the moment
    its MoveRequest turns done (`sync_completed_otg_delivery_request`, reached
    only from `_recompute_request_status`).  When that predicate is false in
    that millisecond it is never re-evaluated, so the row stays `dispatched`
    for good: on 20.09.2026 there were 63 such rows and 52 of them belonged to
    orders that had already shipped.

    Once the order itself is finished the request cannot become any more true
    than it already is.  So re-run the real predicate first -- that closes the
    rows whose facts merely landed late, with their genuine reason -- and close
    whatever is still open explicitly, recording that the shipment closed it.
    """
    order = (
        ShippingOrder.objects.filter(pk=int(shipping_order_id or 0))
        .only("id", "number", "status")
        .first()
    )
    if order is None or order.status not in _FINISHED_SHIPPING_STATUSES:
        return []

    pending = list(
        OtgDeliveryRequest.objects.filter(
            shipping_order_id=order.pk,
            status__in=_OPEN_OTG_REQUEST_STATUSES,
        ).select_related("move_request")
    )
    if not pending:
        return []

    for request in pending:
        if request.move_request_id is None:
            continue
        try:
            sync_completed_otg_delivery_request(request.move_request)
        except Exception:
            logger.exception(
                "Failed to re-check OTG delivery request completion",
                extra={"otg_request_id": int(request.pk)},
            )

    closed_ids: list[int] = []
    with transaction.atomic():
        still_open = (
            OtgDeliveryRequest.objects.select_for_update()
            .filter(
                shipping_order_id=order.pk,
                status__in=_OPEN_OTG_REQUEST_STATUSES,
            )
            .order_by("id")
        )
        for request in still_open:
            payload = dict(request.payload or {})
            payload["closed_by_shipping_completion"] = True
            payload["closed_shipping_status"] = order.status
            payload["closed_from_status"] = request.status
            payload["completed_at"] = timezone.now().isoformat()
            request.status = OtgDeliveryRequest.STATUS_DONE
            request.payload = payload
            request.save(update_fields=["status", "payload", "updated_at"])
            _make_event(
                request,
                "closed_by_shipping_completion",
                message=(
                    f"Заявка {order.number} закрыта ({order.status}); "
                    "задание подвоза в OTG больше не может быть выполнено."
                ),
                payload={
                    "shipping_status": order.status,
                    "closed_from_status": payload["closed_from_status"],
                },
            )
            closed_ids.append(int(request.pk))
    return closed_ids


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_goods_type(value: Any) -> str:
    return StockAvailabilityService.normalize_goods_type(value)


def _parse_box_count(comment: str | None) -> int:
    match = _BOX_COUNT_RE.search(str(comment or ""))
    return max(_as_int(match.group(1)) if match else 0, 0)


def _parse_box_qty(comment: str | None) -> int:
    match = _BOX_QTY_RE.search(str(comment or ""))
    return max(_as_int(match.group(1)) if match else 0, 0)


def _parse_box_codes(comment: str | None) -> list[str]:
    match = _BOX_CODES_RE.search(str(comment or ""))
    if not match:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw_code in str(match.group(1) or "").split(","):
        code = _normalize_text(raw_code)
        key = code.lower()
        if not code or key in seen:
            continue
        seen.add(key)
        result.append(code)
    return result


def _line_from_item(item, *, qty_per_box: int) -> dict:
    return {
        "shipping_item_id": int(getattr(item, "id", 0) or 0),
        "sku": _normalize_text(getattr(item, "sku_code", "")),
        "name": _normalize_text(getattr(item, "name", "")),
        "size": _normalize_text(getattr(item, "size", "")),
        "barcode": _normalize_text(getattr(item, "barcode", "")),
        "goods_type": _normalize_text(getattr(item, "goods_type", "")),
        "qty_per_box": max(int(qty_per_box or 0), 0),
    }


def _line_from_split_payload(row: dict, *, qty_key: str = "qty") -> dict:
    qty = max(_as_int((row or {}).get(qty_key)), 0)
    return {
        "row_key": _normalize_text((row or {}).get("row_key")),
        "sku": _normalize_text((row or {}).get("sku_code") or (row or {}).get("sku")),
        "name": _normalize_text((row or {}).get("name")),
        "size": _normalize_text((row or {}).get("size")),
        "barcode": _normalize_text((row or {}).get("barcode")),
        "goods_type": _normalize_text((row or {}).get("goods_type")),
        "qty_per_box": qty,
    }


def _composition_key(composition: list[dict]) -> tuple:
    return tuple(
        sorted(
            (
                _normalize_text(line.get("sku")).lower(),
                _normalize_text(line.get("barcode")).lower(),
                _normalize_text(line.get("size")).lower(),
                _normalize_goods_type(line.get("goods_type")),
                max(_as_int(line.get("qty_per_box")), 0),
            )
            for line in composition
            if max(_as_int(line.get("qty_per_box")), 0) > 0
        )
    )


def _demand_key(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _composition_box_qty(composition: list[dict]) -> int:
    return sum(max(_as_int(line.get("qty_per_box")), 0) for line in composition)


def _composition_barcode_qty(composition: list[dict]) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in composition or []:
        barcode = _normalize_text(line.get("barcode"))
        qty = max(_as_int(line.get("qty_per_box")), 0)
        if not barcode or qty <= 0:
            continue
        result[barcode] = result.get(barcode, 0) + qty
    return result


def _pattern_for_composition(composition: list[dict], *, boxes: int) -> dict:
    barcode_qty = _composition_barcode_qty(composition)
    sku_values = sorted({_normalize_text(line.get("sku")) for line in composition or [] if _normalize_text(line.get("sku"))})
    goods_types = sorted({_normalize_goods_type(line.get("goods_type")) for line in composition or [] if _normalize_goods_type(line.get("goods_type"))})
    requested_barcodes = sorted(barcode_qty.keys())
    return {
        "box_qty": _composition_box_qty(composition),
        "requested_box_count": max(int(boxes or 0), 0),
        "barcode_qty": barcode_qty,
        "requested_article": sku_values[0] if len(sku_values) == 1 else "",
        "requested_goods_type": goods_types[0] if len(goods_types) == 1 else "",
        "requested_barcodes": requested_barcodes,
    }


def _normalize_pattern_barcode_qty(value: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    if not isinstance(value, dict):
        return result
    for raw_barcode, raw_qty in value.items():
        barcode = _normalize_text(raw_barcode)
        qty = max(_as_int(raw_qty), 0)
        if barcode and qty > 0:
            result[barcode] = result.get(barcode, 0) + qty
    return result


def _merge_requested_box_patterns(patterns: list[dict]) -> list[dict]:
    merged: dict[tuple, dict] = {}
    for pattern in patterns or []:
        if not isinstance(pattern, dict):
            continue
        barcode_qty = _normalize_pattern_barcode_qty(pattern.get("barcode_qty"))
        requested_barcodes = tuple(sorted(_normalize_text(value) for value in pattern.get("requested_barcodes") or [] if _normalize_text(value)))
        key = (
            max(_as_int(pattern.get("box_qty")), 0),
            tuple(sorted(barcode_qty.items())),
            _normalize_text(pattern.get("requested_article")),
            _normalize_goods_type(pattern.get("requested_goods_type")),
            requested_barcodes,
        )
        entry = merged.setdefault(
            key,
            {
                **pattern,
                "box_qty": key[0],
                "requested_box_count": 0,
                "barcode_qty": barcode_qty,
                "requested_barcodes": list(requested_barcodes),
            },
        )
        entry["requested_box_count"] = max(_as_int(entry.get("requested_box_count")), 0) + max(
            _as_int(pattern.get("requested_box_count")),
            0,
        )
    return [entry for entry in merged.values() if max(_as_int(entry.get("requested_box_count")), 0) > 0]


def _barcode_qty_for_patterns(patterns: list[dict]) -> dict[str, int]:
    result: dict[str, int] = {}
    for pattern in patterns or []:
        box_count = max(_as_int(pattern.get("requested_box_count")), 0)
        if box_count <= 0:
            continue
        for barcode, qty in _normalize_pattern_barcode_qty(pattern.get("barcode_qty")).items():
            result[barcode] = result.get(barcode, 0) + qty * box_count
    return result


def _box_matches_composition(box: dict, composition: list[dict]) -> bool:
    pattern = _pattern_for_composition(composition, boxes=1)
    return _stock_box_matches_requested_pattern(box, pattern)


def _box_contains_pick_composition(box: dict, composition: list[dict]) -> bool:
    if not composition:
        return False

    stock_lines = [line for line in box.get("items") or [] if isinstance(line, dict)]
    if not stock_lines:
        return False

    for requested_line in composition or []:
        requested_qty = max(_as_int(requested_line.get("qty_per_box")), 0)
        if requested_qty <= 0:
            continue
        matched_qty = 0
        for stock_line in stock_lines:
            if _stock_line_matches_requested_line(stock_line, requested_line):
                matched_qty += max(_as_int(stock_line.get("qty") or stock_line.get("qty_per_box")), 0)
        if matched_qty < requested_qty:
            return False
    return True


def _box_composition(box: dict) -> list[dict]:
    result: list[dict] = []
    for item in box.get("items") or []:
        if not isinstance(item, dict):
            continue
        qty = max(_as_int(item.get("qty") or item.get("qty_per_box")), 0)
        if qty <= 0:
            continue
        result.append(
            {
                "sku": _normalize_text(item.get("sku") or item.get("sku_code")),
                "name": _normalize_text(item.get("name")),
                "size": _normalize_text(item.get("size")),
                "barcode": _normalize_text(item.get("barcode")),
                "goods_type": _normalize_text(item.get("goods_type")),
                "qty_per_box": qty,
            }
        )
    return result


def _single_line_flexible_demand(demand: OtgDeliveryDemand, composition: list[dict]) -> tuple[dict, int] | None:
    if demand.demand_type != OtgDeliveryDemand.TYPE_FULL_BOX:
        return None
    if _as_int(dict(demand.payload or {}).get("supplemental_pick_source_request_id")) > 0:
        return None
    positive_lines = [
        line
        for line in composition or []
        if max(_as_int(line.get("qty_per_box")), 0) > 0
    ]
    if len(positive_lines) != 1:
        return None
    positive_item_ids = [
        _as_int(item_id)
        for item_id, qty in (demand.item_quantities_per_box or {}).items()
        if _as_int(item_id) > 0 and max(_as_int(qty), 0) > 0
    ]
    if len(positive_item_ids) != 1:
        return None
    return positive_lines[0], positive_item_ids[0]


def _stock_line_matches_requested_line(stock_line: dict, requested_line: dict) -> bool:
    requested_barcode = _normalize_text(requested_line.get("barcode")).lower()
    stock_barcode = _normalize_text(stock_line.get("barcode")).lower()
    if requested_barcode:
        if stock_barcode != requested_barcode:
            return False
    else:
        requested_sku = _normalize_text(requested_line.get("sku")).lower()
        stock_sku = _normalize_text(stock_line.get("sku")).lower()
        if requested_sku and stock_sku != requested_sku:
            return False
        requested_size = _normalize_text(requested_line.get("size")).lower()
        stock_size = _normalize_text(stock_line.get("size")).lower()
        if requested_size and stock_size and stock_size != requested_size:
            return False
    requested_goods_type = _normalize_goods_type(requested_line.get("goods_type"))
    stock_goods_type = _normalize_goods_type(stock_line.get("goods_type"))
    return not requested_goods_type or not stock_goods_type or stock_goods_type == requested_goods_type


def _flexible_box_match(box: dict, requested_line: dict) -> tuple[int, list[dict]] | None:
    box_composition = _box_composition(box)
    if box_composition:
        if any(not _stock_line_matches_requested_line(line, requested_line) for line in box_composition):
            return None
        actual_qty = sum(max(_as_int(line.get("qty_per_box")), 0) for line in box_composition)
        if actual_qty <= 0:
            return None
        normalized_composition = []
        for line in box_composition:
            normalized_line = {**requested_line, **line}
            normalized_line["qty_per_box"] = max(_as_int(line.get("qty_per_box")), 0)
            normalized_composition.append(normalized_line)
        return actual_qty, normalized_composition

    requested_barcode = _normalize_text(requested_line.get("barcode"))
    barcode_qty = _normalize_pattern_barcode_qty(box.get("barcode_qty"))
    if requested_barcode:
        matching_qty = barcode_qty.get(requested_barcode, 0)
        if matching_qty <= 0 or any(barcode != requested_barcode for barcode in barcode_qty):
            return None
        line = {**requested_line, "qty_per_box": matching_qty}
        return matching_qty, [line]

    fallback_qty = max(_as_int(box.get("qty")), 0)
    if fallback_qty <= 0:
        return None
    line = {**requested_line, "qty_per_box": fallback_qty}
    return fallback_qty, [line]


def _flexible_box_candidates(
    *,
    catalog: dict[str, dict],
    requested_line: dict,
    used_box_keys: set[tuple[str, str]],
    used_full_pallets: set[str],
) -> list[dict]:
    candidates: list[dict] = []
    for pallet_code, pallet in catalog.items():
        if pallet_code in used_full_pallets:
            continue
        for box in pallet.get("available_boxes") or []:
            box_code = _normalize_text(box.get("code"))
            if not box_code or (pallet_code, box_code.lower()) in used_box_keys:
                continue
            match = _flexible_box_match(box, requested_line)
            if match is None:
                continue
            actual_qty, actual_composition = match
            candidates.append(
                {
                    "pallet_code": pallet_code,
                    "pallet": pallet,
                    "box": box,
                    "actual_qty": actual_qty,
                    "actual_composition": actual_composition,
                    "pattern": _pattern_for_composition(actual_composition, boxes=1),
                }
            )
    candidates.sort(
        key=lambda item: (
            _as_int(item.get("actual_qty")),
            _normalize_text(item.get("pallet_code")),
            _normalize_text((item.get("box") or {}).get("code")),
        )
    )
    return candidates


def _select_exact_flexible_candidates(candidates: list[dict], target_qty: int) -> list[dict]:
    target = max(_as_int(target_qty), 0)
    if target <= 0:
        return []

    def route_key(candidate: dict) -> tuple:
        location = dict((candidate.get("pallet") or {}).get("from_location") or {})
        return (
            max(_as_int(location.get("tier")), 0) or 999,
            max(_as_int(location.get("row")), 0) or 999,
            max(_as_int(location.get("section")), 0) or 999,
            max(_as_int(location.get("cell")), 0) or 999,
            _normalize_text(candidate.get("pallet_code")).casefold(),
            _normalize_text((candidate.get("box") or {}).get("code")).casefold(),
        )

    def selection_key(indexes: tuple[int, ...]) -> tuple:
        selected = [candidates[index] for index in indexes]
        pallet_count = len(
            {
                _normalize_text(candidate.get("pallet_code")).casefold()
                for candidate in selected
                if _normalize_text(candidate.get("pallet_code"))
            }
        )
        return (
            pallet_count,
            len(selected),
            tuple(sorted(route_key(candidate) for candidate in selected)),
        )

    # Keep only exact sums. Planning must never turn a request for N units
    # into a route that brings more or fewer than N units to OTG.
    best_by_qty: dict[int, tuple[int, ...]] = {0: ()}
    for index, candidate in enumerate(candidates or []):
        candidate_qty = max(_as_int(candidate.get("actual_qty")), 0)
        if candidate_qty <= 0 or candidate_qty > target:
            continue
        for subtotal, selected_indexes in list(best_by_qty.items()):
            total = subtotal + candidate_qty
            if total > target:
                continue
            proposed = (*selected_indexes, index)
            current = best_by_qty.get(total)
            if current is None or selection_key(proposed) < selection_key(current):
                best_by_qty[total] = proposed

    return [candidates[index] for index in best_by_qty.get(target, ())]


def _append_flexible_plans(
    *,
    plans: list[dict],
    demand: OtgDeliveryDemand,
    selected: list[dict],
    item_id: int,
    requested_line: dict,
    used_box_keys: set[tuple[str, str]],
) -> int:
    selected_qty = sum(max(_as_int(candidate.get("actual_qty")), 0) for candidate in selected or [])
    if selected_qty <= 0:
        return 0
    by_pallet: dict[str, list[dict]] = defaultdict(list)
    for candidate in selected:
        pallet_code = _normalize_text(candidate.get("pallet_code"))
        if pallet_code:
            by_pallet[pallet_code].append(candidate)
    for pallet_code, candidates in by_pallet.items():
        pallet = candidates[0].get("pallet") or {}
        selected_boxes = [candidate.get("box") or {} for candidate in candidates]
        selected_patterns = _merge_requested_box_patterns([candidate.get("pattern") or {} for candidate in candidates])
        plan_qty = sum(max(_as_int(candidate.get("actual_qty")), 0) for candidate in candidates)
        plans.append(
            {
                "demand": demand,
                "plan_type": OtgPalletPlan.TYPE_PICK_BOXES,
                "pallet_code": pallet_code,
                "boxes_planned": len(selected_boxes),
                "qty_planned": plan_qty,
                "from_location": pallet.get("from_location") or _build_location("PR", 0, 0, 0, 0),
                "receiving_order_id": pallet.get("receiving_order_id") or "",
                "planned_box_codes": [_normalize_text(box.get("code")) for box in selected_boxes],
                "planned_item_quantities": {str(item_id): plan_qty},
                "payload": {
                    "flexible_box_qty_match": True,
                    "composition_override": [{**requested_line, "qty_per_box": plan_qty}],
                    "requested_box_patterns_override": selected_patterns,
                    "planned_barcode_qty": _barcode_qty_for_patterns(selected_patterns),
                    "planned_item_quantities": {str(item_id): plan_qty},
                },
            }
        )
        for box in selected_boxes:
            box_code = _normalize_text(box.get("code"))
            if box_code:
                used_box_keys.add((pallet_code, box_code.lower()))
    return selected_qty


def _merge_box_compositions(boxes: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str, str, str], dict] = {}
    for box in boxes or []:
        for line in _box_composition(box):
            key = (
                _normalize_text(line.get("sku")),
                _normalize_text(line.get("size")),
                _normalize_text(line.get("barcode")),
                _normalize_goods_type(line.get("goods_type")),
            )
            entry = merged.setdefault(key, {**line, "qty_per_box": 0})
            entry["qty_per_box"] = _as_int(entry.get("qty_per_box")) + max(_as_int(line.get("qty_per_box")), 0)
            if not entry.get("name") and line.get("name"):
                entry["name"] = line.get("name")
    return list(merged.values())


def _combined_full_pallet_plan(
    *,
    pallet_code: str,
    pallet: dict,
    demands: list[OtgDeliveryDemand],
    remaining_by_demand_id: dict[int, int],
    used_box_keys: set[tuple[str, str]],
) -> dict | None:
    all_boxes = [
        box
        for box in pallet.get("all_boxes") or []
        if _normalize_text(box.get("code"))
    ]
    if not all_boxes:
        return None
    if any(demand.source_box_codes for demand in demands):
        return None
    available_box_keys = {
        _normalize_text(box.get("code")).lower()
        for box in pallet.get("available_boxes") or []
        if _normalize_text(box.get("code"))
    }
    assignments: list[tuple[dict, OtgDeliveryDemand]] = []
    temp_remaining = dict(remaining_by_demand_id)
    for box in sorted(all_boxes, key=lambda item: _normalize_text(item.get("code")).lower()):
        box_code = _normalize_text(box.get("code"))
        box_key = box_code.lower()
        if not box_code or box_key not in available_box_keys or (pallet_code, box_key) in used_box_keys:
            return None
        matched_demand = None
        for demand in demands:
            if demand.demand_type == OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT:
                continue
            demand_id = int(demand.id or 0)
            if temp_remaining.get(demand_id, 0) <= 0:
                continue
            if _box_matches_composition(box, list(demand.composition or [])):
                matched_demand = demand
                break
        if matched_demand is None:
            return None
        temp_remaining[int(matched_demand.id or 0)] = temp_remaining.get(int(matched_demand.id or 0), 0) - 1
        assignments.append((box, matched_demand))

    if len(assignments) != len(all_boxes):
        return None

    boxes = [box for box, _demand in assignments]
    planned_item_quantities: dict[str, int] = defaultdict(int)
    assigned_demand_counts: dict[str, int] = defaultdict(int)
    assigned_demand_ids: list[int] = []
    seen_demand_ids: set[int] = set()
    for _box, demand in assignments:
        demand_id = int(demand.id or 0)
        if demand_id and demand_id not in seen_demand_ids:
            seen_demand_ids.add(demand_id)
            assigned_demand_ids.append(demand_id)
        if demand_id:
            assigned_demand_counts[str(demand_id)] += 1
        for raw_item_id, raw_qty in (demand.item_quantities_per_box or {}).items():
            item_id = _as_int(raw_item_id)
            qty = max(_as_int(raw_qty), 0)
            if item_id > 0 and qty > 0:
                planned_item_quantities[str(item_id)] += qty

    return {
        "demand": assignments[0][1],
        "plan_type": OtgPalletPlan.TYPE_FULL_PALLET,
        "pallet_code": pallet_code,
        "boxes_planned": len(boxes),
        "qty_planned": sum(max(_as_int(box.get("qty")), 0) for box in boxes),
        "from_location": pallet.get("from_location") or _build_location("PR", 0, 0, 0, 0),
        "receiving_order_id": pallet.get("receiving_order_id") or "",
        "planned_box_codes": [_normalize_text(box.get("code")) for box in boxes],
        "planned_item_quantities": dict(planned_item_quantities),
        "assigned_demand_ids": assigned_demand_ids,
        "assigned_demand_counts": dict(assigned_demand_counts),
        "payload": {
            "combined_full_pallet": True,
            "composition_override": _merge_box_compositions(boxes),
            "planned_item_quantities": dict(planned_item_quantities),
            "assigned_demand_ids": assigned_demand_ids,
            "assigned_demand_counts": dict(assigned_demand_counts),
        },
    }


def _shipping_item_box_numbers(item) -> tuple[int, int]:
    comment = str(getattr(item, "comment", "") or "")
    box_count = _parse_box_count(comment)
    box_qty = _parse_box_qty(comment)
    qty_requested = max(_as_int(getattr(item, "qty_requested", 0)), 0)
    if box_count <= 0 and box_qty > 0 and qty_requested % box_qty == 0:
        box_count = qty_requested // box_qty
    if box_qty <= 0 and box_count > 0 and qty_requested % box_count == 0:
        box_qty = qty_requested // box_count
    return max(box_count, 0), max(box_qty, 0)


def _explicit_source_box_requires_partial_pick(
    *,
    line: dict,
    box_codes: list[str],
    box_qty: int,
    agency_id: int | None,
) -> bool:
    normalized_codes = [_normalize_text(code) for code in box_codes or [] if _normalize_text(code)]
    if not normalized_codes or box_qty <= 0 or agency_id is None:
        return False

    requested_barcode = _normalize_text((line or {}).get("barcode"))
    requested_sku = _normalize_text((line or {}).get("sku"))
    requested_goods_type = _normalize_goods_type((line or {}).get("goods_type"))

    requested_found: set[str] = set()
    boxes_with_other_content: set[str] = set()

    for snapshot in (
        WarehouseStockSnapshot.objects.filter(
            agency_id=agency_id,
            container_code__in=normalized_codes,
            is_archived=False,
        )
        .order_by("container_code", "-id")
    ):
        code_key = _normalize_text(getattr(snapshot, "container_code", "")).lower()
        if not code_key:
            continue

        snapshot_qty = max(_as_int(getattr(snapshot, "qty", 0)), 0)
        if snapshot_qty <= 0:
            continue

        snapshot_barcode = _normalize_text(getattr(snapshot, "barcode", ""))
        snapshot_sku = _normalize_text(getattr(snapshot, "sku_code", ""))
        snapshot_goods_type = _normalize_goods_type(getattr(snapshot, "goods_type", ""))
        same_item = (
            (requested_barcode and snapshot_barcode == requested_barcode)
            or (requested_sku and snapshot_sku == requested_sku)
        ) and (not requested_goods_type or not snapshot_goods_type or snapshot_goods_type == requested_goods_type)

        if same_item:
            if snapshot_qty >= box_qty and _snapshot_available_for_otg_source(snapshot):
                requested_found.add(code_key)
        else:
            boxes_with_other_content.add(code_key)

    if len(requested_found) < len({code.lower() for code in normalized_codes}):
        return False
    return bool(boxes_with_other_content)


def _explicit_source_partial_demand(
    item,
    *,
    line: dict,
    box_codes: list[str],
    box_count: int,
    box_qty: int,
    agency_id: int | None,
) -> dict | None:
    normalized_codes = [_normalize_text(code) for code in box_codes or [] if _normalize_text(code)]
    if not normalized_codes or box_count <= 0 or box_qty <= 0:
        return None
    if len(normalized_codes) != box_count:
        return None
    if not _explicit_source_box_requires_partial_pick(
        line=line,
        box_codes=normalized_codes,
        box_qty=box_qty,
        agency_id=agency_id,
    ):
        return None

    group_key = f"explicit_partial:{int(getattr(item, 'id', 0) or 0)}:{','.join(code.lower() for code in normalized_codes)}"
    return {
        "demand_type": OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT,
        "boxes_required": box_count,
        "composition": [line],
        "pick_composition": [line],
        "item_quantities_per_box": {str(item.id): box_qty},
        "source_box_codes": normalized_codes,
        "payload": {
            "group_key": group_key,
            "source_meta": {
                "kind": "explicit_partial_box_pick",
                "source_box_codes": normalized_codes,
            },
        },
    }


def _demand_line_identity(line: dict) -> tuple[str, str, str, str]:
    return (
        _normalize_text(line.get("sku")).lower(),
        _normalize_text(line.get("barcode")).lower(),
        _normalize_text(line.get("size")).lower(),
        _normalize_goods_type(line.get("goods_type")),
    )


def _merge_physical_box_lines(entries: list[dict], *, requested: bool) -> list[dict]:
    merged: dict[tuple[str, str, str, str], dict] = {}
    for entry in entries:
        lines = list(entry.get("pick_composition") or []) if requested else []
        if not lines:
            lines = list(entry.get("composition") or [])
        for line in lines:
            qty = max(_as_int(line.get("qty_per_box")), 0)
            if qty <= 0:
                continue
            key = _demand_line_identity(line)
            current = merged.get(key)
            if current is None:
                merged[key] = {**line, "qty_per_box": qty}
                continue
            if requested:
                current["qty_per_box"] = max(_as_int(current.get("qty_per_box")), 0) + qty
            else:
                # A partial split may repeat the full source-box pattern on
                # several order lines. The physical content must be counted once.
                current["qty_per_box"] = max(_as_int(current.get("qty_per_box")), qty)
            if not current.get("name") and line.get("name"):
                current["name"] = line.get("name")
    return list(merged.values())


def _merge_demands_for_shared_source_boxes(entries: list[dict]) -> list[dict]:
    code_occurrences: dict[str, int] = defaultdict(int)
    entry_codes: list[list[str]] = []
    for entry in entries:
        codes: list[str] = []
        seen: set[str] = set()
        for raw_code in entry.get("source_box_codes") or []:
            code = _normalize_text(raw_code)
            key = code.lower()
            if not code or key in seen:
                continue
            seen.add(key)
            codes.append(code)
            code_occurrences[key] += 1
        entry_codes.append(codes)

    duplicated_codes = {key for key, count in code_occurrences.items() if count > 1}
    if not duplicated_codes:
        return entries

    expanded: list[dict] = []
    for entry, codes in zip(entries, entry_codes):
        boxes_required = max(_as_int(entry.get("boxes_required")), 0)
        touches_duplicate = any(code.lower() in duplicated_codes for code in codes)
        if not touches_duplicate or len(codes) != boxes_required:
            expanded.append(entry)
            continue
        for code in codes:
            expanded.append(
                {
                    **deepcopy(entry),
                    "boxes_required": 1,
                    "source_box_codes": [code],
                }
            )

    grouped: dict[str, list[dict]] = defaultdict(list)
    for entry in expanded:
        codes = [
            _normalize_text(code)
            for code in entry.get("source_box_codes") or []
            if _normalize_text(code)
        ]
        if len(codes) == 1 and codes[0].lower() in duplicated_codes:
            grouped[codes[0].lower()].append(entry)

    result: list[dict] = []
    emitted: set[str] = set()
    for entry in expanded:
        codes = [
            _normalize_text(code)
            for code in entry.get("source_box_codes") or []
            if _normalize_text(code)
        ]
        key = codes[0].lower() if len(codes) == 1 else ""
        group = grouped.get(key) if key else None
        if not group or len(group) <= 1:
            result.append(entry)
            continue
        if key in emitted:
            continue
        emitted.add(key)

        source_composition = _merge_physical_box_lines(group, requested=False)
        pick_composition = _merge_physical_box_lines(group, requested=True)
        is_partial = _composition_key(source_composition) != _composition_key(pick_composition)
        item_quantities: dict[str, int] = defaultdict(int)
        for part in group:
            for item_id, raw_qty in dict(part.get("item_quantities_per_box") or {}).items():
                item_quantities[str(item_id)] += max(_as_int(raw_qty), 0)

        source_code = codes[0]
        result.append(
            {
                "demand_type": (
                    OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT
                    if is_partial
                    else OtgDeliveryDemand.TYPE_MIXED_BOX
                ),
                "boxes_required": 1,
                "composition": source_composition,
                "pick_composition": pick_composition if is_partial else [],
                "item_quantities_per_box": dict(item_quantities),
                "source_box_codes": [source_code],
                "payload": {
                    "group_key": f"physical_source_box:{key}",
                    "source_meta": {
                        "kind": "merged_physical_source_box",
                        "source_box_codes": [source_code],
                        "merged_demand_count": len(group),
                    },
                },
            }
        )
    return result


def build_box_demands(order: ShippingOrder, *, items=None) -> list[dict]:
    items = list(items) if items is not None else list(order.items.order_by("id"))
    regular: dict[tuple, dict] = {}
    mixed: dict[str, dict] = {}
    partial: dict[tuple[str, tuple[str, ...]], dict] = {}
    unsupported: list[str] = []

    for item in items:
        comment = str(getattr(item, "comment", "") or "")
        split_meta = extract_partial_box_split(comment)
        if str(split_meta.get("kind") or "").strip() == "partial_box_split":
            source_boxes = max(_as_int(split_meta.get("source_boxes")), 0)
            source_pattern = [
                _line_from_split_payload(row)
                for row in (split_meta.get("source_box_pattern") or [])
                if isinstance(row, dict)
            ]
            pick_pattern = [
                _line_from_split_payload(row)
                for row in (split_meta.get("pick_pattern") or [])
                if isinstance(row, dict)
            ]
            item_pick_qty = max(_as_int(split_meta.get("item_pick_qty")), 0)
            if source_boxes <= 0 or not source_pattern or not pick_pattern or item_pick_qty <= 0:
                unsupported.append(str(item))
                continue
            group_key = _normalize_text(split_meta.get("group_key") or split_meta.get("row_key") or f"item:{item.id}")
            source_box_codes = list(
                dict.fromkeys(
                    _normalize_text(code)
                    for code in (split_meta.get("source_box_codes") or [])
                    if _normalize_text(code)
                )
            )
            # The parser's group_key identifies the product row, not a physical
            # split operation. Two order lines may therefore share group_key but
            # point to different source boxes. Keep those demands separate while
            # still allowing rows from the same physical mixed box to merge below.
            partial_key = (
                group_key,
                tuple(sorted(code.lower() for code in source_box_codes)),
            )
            entry = partial.setdefault(
                partial_key,
                {
                    "demand_type": OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT,
                    "boxes_required": source_boxes,
                    "composition": source_pattern,
                    "pick_composition": pick_pattern,
                    "item_quantities_per_box": {},
                    "source_box_codes": source_box_codes,
                    "payload": {"group_key": group_key, "source_meta": split_meta},
                },
            )
            entry["boxes_required"] = max(_as_int(entry.get("boxes_required")), source_boxes)
            entry["item_quantities_per_box"][str(item.id)] = item_pick_qty
            continue

        box_count, box_qty = _shipping_item_box_numbers(item)
        if box_count <= 0 or box_qty <= 0:
            unsupported.append(str(item))
            continue

        line = _line_from_item(item, qty_per_box=box_qty)
        box_codes = _parse_box_codes(comment)
        explicit_partial = _explicit_source_partial_demand(
            item,
            line=line,
            box_codes=box_codes,
            box_count=box_count,
            box_qty=box_qty,
            agency_id=getattr(order, "agency_id", None),
        )
        if explicit_partial is not None:
            group_key = _normalize_text((explicit_partial.get("payload") or {}).get("group_key"))
            partial[group_key] = explicit_partial
            continue
        is_mixed = "микс-короб" in comment.lower()
        if is_mixed:
            group_key = "mixed:" + (",".join(code.lower() for code in box_codes) if box_codes else f"no-codes:{box_count}")
            entry = mixed.setdefault(
                group_key,
                {
                    "demand_type": OtgDeliveryDemand.TYPE_MIXED_BOX,
                    "boxes_required": box_count,
                    "composition": [],
                    "pick_composition": [],
                    "item_quantities_per_box": {},
                    "source_box_codes": box_codes,
                    "payload": {"group_key": group_key},
                },
            )
            if _as_int(entry.get("boxes_required")) != box_count:
                raise ValidationError("Для одного микс-короба найдено разное количество коробов в строках заявки.")
            entry["composition"].append(line)
            entry["item_quantities_per_box"][str(item.id)] = box_qty
            continue

        comp_key = _composition_key([line])
        # Identical product rows may still point to different physical boxes.
        # Keep their shipping-item allocation separate; route plans can merge
        # the resulting demands later when the boxes are on the same pallet.
        regular_key = (comp_key, int(item.id or 0))
        entry = regular.setdefault(
            regular_key,
            {
                "demand_type": OtgDeliveryDemand.TYPE_FULL_BOX,
                "boxes_required": 0,
                "composition": [line],
                "pick_composition": [],
                "item_quantities_per_box": {},
                "source_box_codes": [],
                "payload": {},
            },
        )
        entry["boxes_required"] += box_count
        entry["item_quantities_per_box"][str(item.id)] = (
            _as_int(entry["item_quantities_per_box"].get(str(item.id))) + box_qty
        )
        entry["source_box_codes"].extend(code for code in box_codes if code not in entry["source_box_codes"])

    if unsupported:
        raise ValidationError(
            "Не удалось построить коробочную потребность OTG для строк без коробов/кратности: "
            + "; ".join(unsupported)
        )

    result: list[dict] = []
    demand_entries = _merge_demands_for_shared_source_boxes(
        [*regular.values(), *mixed.values(), *partial.values()]
    )
    for entry in demand_entries:
        composition = list(entry.get("composition") or [])
        boxes_required = max(_as_int(entry.get("boxes_required")), 0)
        if boxes_required <= 0 or not composition:
            continue
        key_payload = {
            "type": entry.get("demand_type"),
            "composition": _composition_key(composition),
            "pick_composition": _composition_key(list(entry.get("pick_composition") or [])),
            "source_box_codes": sorted(_normalize_text(code).lower() for code in entry.get("source_box_codes") or []),
        }
        result.append(
            {
                **entry,
                "demand_key": _demand_key(key_payload),
                "box_qty": _composition_box_qty(composition),
                "boxes_required": boxes_required,
            }
        )
    if not result:
        raise ValidationError("Не удалось построить коробочную потребность OTG по заявке.")
    return result


def _candidate_values(demands: list[dict]) -> tuple[set[str], set[str]]:
    sku_values: set[str] = set()
    barcode_values: set[str] = set()
    for demand in demands:
        for line in list(demand.get("composition") or []) + list(demand.get("pick_composition") or []):
            sku = _normalize_text(line.get("sku"))
            barcode = _normalize_text(line.get("barcode"))
            if sku:
                sku_values.add(sku)
            if barcode:
                barcode_values.add(barcode)
    return sku_values, barcode_values


def _merge_base_rows(*row_groups: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple] = set()
    for rows in row_groups:
        for row in rows or []:
            row_id = _as_int(_row_value(row, "id", 0))
            if row_id > 0:
                key = ("id", row_id)
            else:
                key = (
                    "row",
                    _normalize_text(_row_value(row, "pallet_code", "") or _row_value(row, "parent_container_code", "")).lower(),
                    _normalize_text(_row_value(row, "box_code", "") or _row_value(row, "container_code", "")).lower(),
                    _normalize_text(_row_value(row, "sku", "") or _row_value(row, "sku_code", "")).lower(),
                    _normalize_text(_row_value(row, "barcode", "")).lower(),
                    _as_int(_row_value(row, "qty", 0)),
                )
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)
    return merged


def _available_rows_for_order_demands(order: ShippingOrder, demands: list[dict]) -> list[dict]:
    sku_values, barcode_values = _candidate_values(demands)
    allowed_active_operation_ids = _preemptible_storage_operation_ids(order.agency_id)
    return _warehouse_base_rows_for_planning(
        agency_id=order.agency_id,
        sku_values=sku_values or None,
        barcode_values=barcode_values or None,
        use_reserve_truth=True,
        exclude_shipping_order_id=order.number,
        allow_receiving=True,
        allowed_active_operation_ids=allowed_active_operation_ids,
    )


def _base_rows_for_order(order: ShippingOrder, demands: list[dict]) -> list[dict]:
    base_rows = _shipping_reserved_rows_for_order(order)
    available_rows = _available_rows_for_order_demands(order, demands)
    if base_rows:
        return _exclude_otg_rows(_merge_base_rows(base_rows, available_rows))
    return _exclude_otg_rows(available_rows)


def _row_is_otg_delivered(row: dict) -> bool:
    zone = _normalize_text(_row_value(row, "zone", "") or _row_value(row, "zone_code", "")).upper()
    state = _normalize_text(_row_value(row, "warehouse_state_code", "") or _row_value(row, "state", "")).lower()
    return zone == "OTG" or state in _OTG_DELIVERED_STATE_CODES


def _exclude_otg_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows or [] if not _row_is_otg_delivered(row)]


def _delivered_otg_boxes_for_order(order: ShippingOrder) -> list[dict]:
    order_number = _normalize_text(getattr(order, "number", ""))
    if not order_number:
        return []
    snapshots = (
        WarehouseStockSnapshot.objects.select_related("container")
        .filter(
            agency=order.agency,
            is_archived=False,
        )
        .filter(Q(zone_code="OTG") | Q(warehouse_state_code__in=sorted(_OTG_DELIVERED_STATE_CODES)))
        .filter(
            Q(last_event__stock_context_type="shipping", last_event__stock_context_id=order_number)
            | Q(source_context_type="shipping", source_context_id=order_number)
        )
        .exclude(container_code="")
        .order_by("container_code", "id")
    )
    grouped: dict[str, dict] = {}
    for snapshot in snapshots:
        box_code = _normalize_text(
            getattr(getattr(snapshot, "container", None), "container_code", "")
            or getattr(snapshot, "container_code", "")
        )
        if not box_code:
            continue
        entry = grouped.setdefault(box_code, {"code": box_code, "qty": 0, "barcode_qty": {}, "items": []})
        qty = max(_as_int(getattr(snapshot, "qty", 0)), 0)
        entry["qty"] += qty
        barcode = _normalize_text(getattr(snapshot, "barcode", ""))
        if barcode and qty > 0:
            entry["barcode_qty"][barcode] = entry["barcode_qty"].get(barcode, 0) + qty
        entry["items"].append(
            {
                "sku": _normalize_text(getattr(snapshot, "sku_code", "")),
                "barcode": barcode,
                "size": _normalize_text(getattr(snapshot, "size", "")),
                "goods_type": _normalize_text(getattr(snapshot, "goods_type", "")),
                "qty": qty,
            }
        )
    return list(grouped.values())


def _snapshot_has_shipping_context(snapshot: WarehouseStockSnapshot) -> bool:
    last_event = getattr(snapshot, "last_event", None)
    if last_event is not None and _normalize_text(getattr(last_event, "stock_context_type", "")).lower() == "shipping":
        return True
    if _normalize_text(getattr(snapshot, "source_context_type", "")).lower() == "shipping":
        return True
    container = getattr(snapshot, "container", None)
    if container is not None and _normalize_text(getattr(container, "source_context_type", "")).lower() == "shipping":
        return True
    return False


def _free_otg_boxes_for_order(order: ShippingOrder, demand_payloads: list[dict]) -> list[dict]:
    sku_values, barcode_values = _candidate_values(demand_payloads)
    if not sku_values and not barcode_values:
        return []

    item_filter = Q()
    if sku_values:
        item_filter |= Q(sku_code__in=sku_values)
    if barcode_values:
        item_filter |= Q(barcode__in=barcode_values)

    snapshots = (
        WarehouseStockSnapshot.objects.select_related("container", "last_event", "active_operation")
        .filter(
            agency=order.agency,
            is_archived=False,
            qty__gt=0,
        )
        .filter(Q(zone_code__iexact="OTG") | Q(warehouse_state_code__in=sorted(_OTG_DELIVERED_STATE_CODES)))
        .filter(item_filter)
        .exclude(container_code="")
        .order_by("container_code", "id")
    )
    grouped: dict[str, dict] = {}
    for snapshot in snapshots:
        if _as_int(getattr(snapshot, "available_qty", 0)) <= 0:
            continue
        if (
            _as_int(getattr(snapshot, "shipping_reserved_qty", 0)) > 0
            or _as_int(getattr(snapshot, "processing_reserved_qty", 0)) > 0
            or _as_int(getattr(snapshot, "other_reserved_qty", 0)) > 0
        ):
            continue
        if getattr(snapshot, "active_operation_id", None):
            continue
        if _snapshot_has_shipping_context(snapshot):
            continue
        box_code = _normalize_text(
            getattr(getattr(snapshot, "container", None), "container_code", "")
            or getattr(snapshot, "container_code", "")
        )
        if not box_code:
            continue
        entry = grouped.setdefault(box_code, {"code": box_code, "qty": 0, "barcode_qty": {}, "items": []})
        qty = max(_as_int(getattr(snapshot, "qty", 0)), 0)
        entry["qty"] += qty
        barcode = _normalize_text(getattr(snapshot, "barcode", ""))
        if barcode and qty > 0:
            entry["barcode_qty"][barcode] = entry["barcode_qty"].get(barcode, 0) + qty
        entry["items"].append(
            {
                "sku": _normalize_text(getattr(snapshot, "sku_code", "")),
                "barcode": barcode,
                "size": _normalize_text(getattr(snapshot, "size", "")),
                "goods_type": _normalize_text(getattr(snapshot, "goods_type", "")),
                "qty": qty,
            }
        )
    return list(grouped.values())


def _claim_free_otg_boxes_for_order(
    order: ShippingOrder,
    demand_payloads: list[dict],
    *,
    performed_by=None,
) -> list[str]:
    del order, demand_payloads, performed_by
    # A matching box already in OTG is only a route hint. Without a confirmed
    # reachtruck scan it must never become a shipping fact.
    return []


def _subtract_delivered_otg_boxes(
    order: ShippingOrder,
    demand_payloads: list[dict],
) -> tuple[list[dict], dict]:
    delivered_boxes = _delivered_otg_boxes_for_order(order)
    if not delivered_boxes:
        return demand_payloads, {"boxes": [], "qty": 0, "by_demand": {}}

    used_box_keys: set[str] = set()
    adjusted: list[dict] = []
    delivered_summary = {"boxes": [], "qty": 0, "by_demand": {}}
    for demand in demand_payloads:
        remaining = max(_as_int(demand.get("boxes_required")), 0)
        if remaining <= 0:
            continue
        delivered_for_demand: list[str] = []
        composition = list(demand.get("composition") or [])
        for box in delivered_boxes:
            if remaining <= 0:
                break
            box_code = _normalize_text(box.get("code"))
            box_key = box_code.lower()
            if not box_code or box_key in used_box_keys:
                continue
            if not _box_matches_composition(box, composition):
                continue
            used_box_keys.add(box_key)
            delivered_for_demand.append(box_code)
            delivered_summary["boxes"].append(box_code)
            delivered_summary["qty"] = _as_int(delivered_summary.get("qty")) + _as_int(box.get("qty"))
            remaining -= 1

        next_demand = dict(demand)
        payload = dict(next_demand.get("payload") or {})
        if delivered_for_demand:
            payload["otg_already_delivered_boxes"] = delivered_for_demand
            payload["otg_already_delivered_count"] = len(delivered_for_demand)
            delivered_summary["by_demand"][next_demand.get("demand_key")] = delivered_for_demand
        next_demand["payload"] = payload
        next_demand["boxes_required"] = remaining
        if remaining > 0:
            adjusted.append(next_demand)
    return adjusted, delivered_summary


def _subtract_delivered_otg_partial_picks(
    order: ShippingOrder,
    demand_payloads: list[dict],
    delivered_summary: dict,
) -> list[dict]:
    """Count this order's physical unit facts before requiring their packing.

    Only used for supplemental picking. Whole boxes already assigned to full-box
    demands cannot also cover partial picks, and each unit is consumed once.
    Task completion alone is deliberately not treated as a warehouse fact.
    """
    if not any(
        demand.get("demand_type") == OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT
        for demand in demand_payloads
    ):
        return demand_payloads

    from shipping.packing import _shipping_loose_snapshots, _warehouse_shipping_snapshot_qty

    available: dict[tuple, int] = defaultdict(int)
    for snapshot in _shipping_loose_snapshots(order):
        identity = _demand_line_identity({
            "sku": snapshot.sku_code,
            "barcode": snapshot.barcode,
            "size": snapshot.size,
            "goods_type": snapshot.goods_type,
        })
        available[identity] += max(_warehouse_shipping_snapshot_qty(snapshot), 0)
    used_boxes = {
        _normalize_text(code).lower() for code in delivered_summary.get("boxes") or []
    }
    for box in _delivered_otg_boxes_for_order(order):
        if _normalize_text(box.get("code")).lower() in used_boxes:
            continue
        for line in box.get("items") or []:
            available[_demand_line_identity(line)] += max(_as_int(line.get("qty")), 0)

    remaining_demands = []
    for demand in demand_payloads:
        if demand.get("demand_type") != OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT:
            remaining_demands.append(demand)
            continue
        per_pick: dict[tuple, int] = defaultdict(int)
        for line in demand.get("pick_composition") or []:
            qty = max(_as_int(line.get("qty_per_box")), 0)
            if qty:
                per_pick[_demand_line_identity(line)] += qty
        required = max(_as_int(demand.get("boxes_required")), 0)
        covered = min(
            [required] + [available[key] // qty for key, qty in per_pick.items()]
        ) if per_pick else 0
        for key, qty in per_pick.items():
            available[key] -= covered * qty
        if covered < required:
            remaining_demands.append({**demand, "boxes_required": required - covered})
    return remaining_demands


def _failed_no_stock_details(request: OtgDeliveryRequest) -> tuple[int, list[str]]:
    failed_tasks: dict[int, dict] = {}
    plans = request.pallet_plans.select_related("move_task").order_by("id")
    for plan in plans:
        task = plan.move_task
        if task is None or task.status != MoveTask.STATUS_FAILED:
            continue
        payload = task.payload if isinstance(task.payload, dict) else {}
        if _normalize_text(payload.get("blocked_reason")) != _NO_STOCK_BLOCKED_REASON:
            continue
        task_id = int(task.id or 0)
        state = failed_tasks.setdefault(
            task_id,
            {
                "boxes": 0,
                "payload_boxes": max(
                    _as_int(payload.get("otg_boxes_planned")),
                    _as_int(payload.get("requested_box_count")),
                    _as_int(dict(payload.get("route_plan") or {}).get("boxes_to_pick")),
                ),
                "pallet_code": _normalize_text(plan.pallet_code or task.pallet_code),
            },
        )
        state["boxes"] += max(_as_int(plan.boxes_planned), 0)

    shortage_boxes = 0
    pallet_codes: list[str] = []
    seen_pallets: set[str] = set()
    for state in failed_tasks.values():
        boxes = max(_as_int(state.get("boxes")), _as_int(state.get("payload_boxes")))
        if boxes <= 0:
            continue
        shortage_boxes += boxes
        pallet_code = _normalize_text(state.get("pallet_code"))
        pallet_key = pallet_code.lower()
        if pallet_code and pallet_key not in seen_pallets:
            seen_pallets.add(pallet_key)
            pallet_codes.append(pallet_code)
    return shortage_boxes, pallet_codes


def _failed_no_stock_pallet_codes_for_order(order: ShippingOrder) -> list[str]:
    """Return physical pallets already rejected by a driver for this order."""

    result: list[str] = []
    seen: set[str] = set()
    requests = OtgDeliveryRequest.objects.filter(shipping_order=order).order_by("id")
    for request in requests:
        _shortage_boxes, pallet_codes = _failed_no_stock_details(request)
        for pallet_code in pallet_codes:
            normalized = _normalize_text(pallet_code)
            key = normalized.casefold()
            if normalized and key not in seen:
                seen.add(key)
                result.append(normalized)
    return result


def _failed_no_stock_partial_demands(
    request: OtgDeliveryRequest,
    *,
    failed_pallet_codes: list[str],
) -> tuple[list[dict], dict[str, int], int]:
    """Rebuild exact unit demands from failed partial-pick plans."""

    demand_payloads: list[dict] = []
    quantities_by_item_id: dict[str, int] = defaultdict(int)
    failed_boxes = 0
    plans = request.pallet_plans.select_related("move_task", "demand").order_by("id")
    for plan in plans:
        task = plan.move_task
        demand = plan.demand
        if (
            task is None
            or task.status != MoveTask.STATUS_FAILED
            or demand.demand_type != OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT
        ):
            continue
        task_payload = task.payload if isinstance(task.payload, dict) else {}
        if _normalize_text(task_payload.get("blocked_reason")) != _NO_STOCK_BLOCKED_REASON:
            continue
        boxes_planned = max(_as_int(plan.boxes_planned), 0)
        if boxes_planned <= 0:
            continue
        failed_boxes += boxes_planned
        item_quantities_per_box = {
            str(_as_int(raw_item_id)): max(_as_int(raw_qty), 0)
            for raw_item_id, raw_qty in dict(
                demand.item_quantities_per_box or {}
            ).items()
            if _as_int(raw_item_id) > 0 and _as_int(raw_qty) > 0
        }
        for raw_item_id, raw_qty in dict(demand.item_quantities_per_box or {}).items():
            item_id = _as_int(raw_item_id)
            qty_per_box = max(_as_int(raw_qty), 0)
            if item_id > 0 and qty_per_box > 0:
                quantities_by_item_id[str(item_id)] += qty_per_box * boxes_planned
        demand_payloads.append(
            {
                "demand_key": _demand_key(
                    {
                        "type": OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT,
                        "supplemental_source_request_id": request.id,
                        "failed_plan_id": plan.id,
                    }
                ),
                "demand_type": OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT,
                "boxes_required": boxes_planned,
                "box_qty": max(_as_int(demand.box_qty), 0),
                "composition": list(demand.composition or []),
                "pick_composition": list(demand.pick_composition or []),
                "item_quantities_per_box": item_quantities_per_box,
                # The failed source is historical only. The new planner must
                # select and then bind another physical box.
                "source_box_codes": [],
                "payload": {
                    **dict(demand.payload or {}),
                    "supplemental_pick_source_request_id": request.id,
                    "supplemental_pick_failed_plan_id": plan.id,
                    "supplemental_pick_excluded_pallet_codes": failed_pallet_codes,
                    "supplemental_partial_retry": True,
                },
            }
        )
    return demand_payloads, dict(quantities_by_item_id), failed_boxes


def _confirmed_shortage_source(
    order: ShippingOrder,
) -> tuple[OtgDeliveryRequest | None, int, list[str]]:
    candidates = OtgDeliveryRequest.objects.filter(
        shipping_order=order,
        status__in=[
            OtgDeliveryRequest.STATUS_DISPATCHED,
            OtgDeliveryRequest.STATUS_IN_PROGRESS,
            OtgDeliveryRequest.STATUS_PARTIAL,
            OtgDeliveryRequest.STATUS_BLOCKED,
        ],
    ).order_by("-id")
    for candidate in candidates:
        payload = candidate.payload if isinstance(candidate.payload, dict) else {}
        if payload.get("request_reason") == _SUPPLEMENTAL_OTG_PICK_REASON:
            continue
        is_waiting_for_stock = bool(payload.get(_WAITING_FOR_STOCK_KEY))
        persisted_shortage = (
            max(_as_int(candidate.shortage_boxes), 0)
            if candidate.status == OtgDeliveryRequest.STATUS_PARTIAL
            or (
                candidate.status == OtgDeliveryRequest.STATUS_BLOCKED
                and not is_waiting_for_stock
            )
            else 0
        )
        failed_shortage, failed_pallet_codes = _failed_no_stock_details(candidate)
        confirmed_shortage = persisted_shortage + failed_shortage
        if confirmed_shortage > 0:
            return candidate, confirmed_shortage, failed_pallet_codes
    return None, 0, []


def get_otg_confirmed_shortage(order: ShippingOrder) -> dict:
    """Return the persisted or failed-task OTG shortage without mutating workflow state."""
    source_request, shortage_boxes, failed_pallet_codes = _confirmed_shortage_source(order)
    return {
        "source_request_id": int(getattr(source_request, "id", 0) or 0),
        "shortage_boxes": shortage_boxes,
        "failed_pallet_codes": failed_pallet_codes,
    }


def _discrepancy_shortages(shortage_rows: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for source in shortage_rows or []:
        if not isinstance(source, dict):
            continue
        barcode = _normalize_text(source.get("barcode"))
        missing_qty = max(_as_int(source.get("missing_qty")), 0)
        if not barcode or missing_qty <= 0:
            continue
        key = barcode.casefold()
        entry = grouped.setdefault(
            key,
            {
                "barcode": barcode,
                "missing_qty": 0,
                "shipping_item_id": _as_int(source.get("shipping_item_id")),
                "sku_code": _normalize_text(source.get("sku_code")),
                "name": _normalize_text(source.get("name")),
                "goods_type": _normalize_text(source.get("goods_type")),
                "size": _normalize_text(source.get("size")),
            },
        )
        entry["missing_qty"] += missing_qty
        if not entry["shipping_item_id"]:
            entry["shipping_item_id"] = _as_int(source.get("shipping_item_id"))
    return list(grouped.values())


def _discrepancy_box_score(box: dict) -> tuple:
    location = box.get("location") or {}
    return (
        0 if box.get("is_open") else 1,
        max(_as_int(box.get("target_qty")), 0),
        max(_as_int(location.get("tier")), 0) or 999,
        max(_as_int(location.get("row")), 0) or 999,
        max(_as_int(location.get("section")), 0) or 999,
        max(_as_int(location.get("cell")), 0) or 999,
        _normalize_text(box.get("pallet_code")).casefold(),
        _normalize_text(box.get("code")).casefold(),
    )


def _discrepancy_open_box_codes(
    *,
    agency_id: int,
    box_codes: list[str],
) -> set[str]:
    code_keys = {
        _normalize_text(code).casefold()
        for code in box_codes
        if _normalize_text(code)
    }
    if not code_keys:
        return set()
    result: set[str] = set()
    snapshots = (
        WarehouseStockSnapshot.objects.select_related("last_event", "container")
        .filter(agency_id=agency_id, is_archived=False)
        .filter(
            Q(container_code__in=list(box_codes))
            | Q(container__container_code__in=list(box_codes))
        )
        .order_by("id")
    )
    for snapshot in snapshots:
        payload = (
            snapshot.last_event.payload
            if snapshot.last_event is not None
            and isinstance(snapshot.last_event.payload, dict)
            else {}
        )
        source_code = _normalize_text(
            payload.get("source_box_code")
            or getattr(snapshot, "container_code", "")
            or getattr(getattr(snapshot, "container", None), "container_code", "")
        )
        if (
            payload.get("partial_shipping_pick")
            or payload.get("partial_processing_pick")
        ) and source_code.casefold() in code_keys:
            result.add(source_code.casefold())
    return result


def _discrepancy_stock_catalog(
    *,
    order: ShippingOrder,
    shortages: list[dict],
    excluded_pallet_codes: list[str] | None = None,
) -> dict[str, list[dict]]:
    barcode_values = {
        _normalize_text(row.get("barcode"))
        for row in shortages
        if _normalize_text(row.get("barcode"))
    }
    base_rows = _warehouse_base_rows_for_planning(
        agency_id=order.agency_id,
        barcode_values=barcode_values or None,
        use_reserve_truth=True,
        exclude_shipping_order_id=order.number,
        allow_receiving=True,
    )
    base_rows = _exclude_otg_rows(base_rows)
    blocked_pallets = _active_pallet_codes_for_agency(order.agency_id)
    open_task_pallets = MoveTask.objects.filter(
        status__in=[MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS],
        request__agency_id=order.agency_id,
    ).exclude(pallet_code="")
    blocked_pallets.update(
        _normalize_text(code)
        for code in open_task_pallets.values_list("pallet_code", flat=True)
        if _normalize_text(code)
    )
    blocked_pallets.update(
        _normalize_text(code)
        for code in (excluded_pallet_codes or [])
        if _normalize_text(code)
    )
    catalog = _pallet_catalog(
        base_rows,
        agency_id=order.agency_id,
        blocked_pallets=blocked_pallets,
    )

    available_by_box_barcode: dict[tuple[str, str], int] = defaultdict(int)
    for row in base_rows:
        box_code = _normalize_text(
            _row_value(row, "box_code", "")
            or _row_value(row, "container_code", "")
        )
        barcode = _normalize_text(_row_value(row, "barcode", ""))
        if not box_code or not barcode:
            continue
        available_qty = max(
            _as_int(_row_value(row, "available_qty", 0)),
            _as_int(_row_value(row, "shipping_reserved_qty", 0)),
        )
        if available_qty <= 0:
            available_qty = max(_as_int(_row_value(row, "qty", 0)), 0)
        available_by_box_barcode[(box_code.casefold(), barcode.casefold())] += available_qty

    all_codes = [
        _normalize_text(box.get("code"))
        for pallet in catalog.values()
        for box in pallet.get("available_boxes") or []
        if _normalize_text(box.get("code"))
    ]
    open_codes = _discrepancy_open_box_codes(
        agency_id=order.agency_id,
        box_codes=all_codes,
    )
    by_barcode: dict[str, list[dict]] = defaultdict(list)
    for pallet_code, pallet in catalog.items():
        location = dict(pallet.get("from_location") or {})
        for box in pallet.get("available_boxes") or []:
            box_code = _normalize_text(box.get("code"))
            if not box_code:
                continue
            items = [
                dict(item)
                for item in box.get("items") or []
                if isinstance(item, dict) and max(_as_int(item.get("qty")), 0) > 0
            ]
            total_qty = sum(max(_as_int(item.get("qty")), 0) for item in items)
            for shortage in shortages:
                barcode = _normalize_text(shortage.get("barcode"))
                target_qty = sum(
                    max(_as_int(item.get("qty")), 0)
                    for item in items
                    if _normalize_text(item.get("barcode")).casefold()
                    == barcode.casefold()
                )
                safe_qty = available_by_box_barcode.get(
                    (box_code.casefold(), barcode.casefold()),
                    0,
                )
                target_qty = min(target_qty, safe_qty)
                if target_qty <= 0:
                    continue
                by_barcode[barcode.casefold()].append(
                    {
                        "code": box_code,
                        "pallet_code": pallet_code,
                        "location": location,
                        "location_label": _location_label(location),
                        "items": items,
                        "target_qty": target_qty,
                        "total_qty": total_qty,
                        "is_open": box_code.casefold() in open_codes,
                    }
                )
    for boxes in by_barcode.values():
        boxes.sort(key=_discrepancy_box_score)
    return by_barcode


def _exact_whole_box_selection(candidates: list[dict], target_qty: int) -> list[dict]:
    exact_candidates = [
        box
        for box in candidates
        if max(_as_int(box.get("target_qty")), 0) > 0
        and _as_int(box.get("target_qty")) == _as_int(box.get("total_qty"))
        and _as_int(box.get("target_qty")) <= target_qty
    ]
    states: dict[int, list[dict]] = {0: []}

    def selection_score(selected: list[dict]) -> tuple:
        return (
            len(selected),
            len({_normalize_text(box.get("pallet_code")) for box in selected}),
            sum(max(_as_int((box.get("location") or {}).get("tier")), 0) or 999 for box in selected),
            tuple(_normalize_text(box.get("code")).casefold() for box in selected),
        )

    for box in exact_candidates:
        qty = max(_as_int(box.get("target_qty")), 0)
        next_states = dict(states)
        for current_qty, selected in states.items():
            combined_qty = current_qty + qty
            if combined_qty > target_qty:
                continue
            combined = [*selected, box]
            existing = next_states.get(combined_qty)
            if existing is None or selection_score(combined) < selection_score(existing):
                next_states[combined_qty] = combined
        states = next_states
    return list(states.get(target_qty) or [])


def _discrepancy_source_line(
    source: dict,
    *,
    shipping_item_id: int,
    target_barcode: str,
    qty_override: int | None = None,
) -> dict:
    barcode = _normalize_text(source.get("barcode"))
    qty = (
        max(_as_int(qty_override), 0)
        if qty_override is not None
        else max(_as_int(source.get("qty")), 0)
    )
    return {
        "shipping_item_id": (
            shipping_item_id
            if barcode.casefold() == target_barcode.casefold()
            else 0
        ),
        "sku": _normalize_text(source.get("sku")),
        "name": _normalize_text(source.get("name")),
        "size": _normalize_text(source.get("size")),
        "barcode": barcode,
        "goods_type": _normalize_text(source.get("goods_type")),
        "qty_per_box": qty,
    }


def _discrepancy_pick_is_complete_closed_box(
    box: dict,
    *,
    pick_qty: int,
    source_composition: list[dict],
    pick_composition: list[dict],
) -> bool:
    """Return True when a piece correction actually takes the whole box.

    The correction mode is chosen for the discrepancy as a whole, but its
    physical source boxes can be heterogeneous.  A closed box whose complete
    composition is requested must follow the guarded whole-box route so the
    reachtruck driver scans the box once instead of every unit.
    """

    requested_qty = max(_as_int(pick_qty), 0)
    target_qty = max(_as_int(box.get("target_qty")), 0)
    total_qty = max(_as_int(box.get("total_qty")), 0)
    if bool(box.get("is_open")) or requested_qty <= 0:
        return False
    if requested_qty != target_qty or target_qty != total_qty:
        return False
    return bool(source_composition) and (
        _composition_key(source_composition) == _composition_key(pick_composition)
    )


def get_otg_discrepancy_pick_preview(
    *,
    order: ShippingOrder,
    shortage_rows: list[dict],
    correction_mode: str,
) -> dict:
    if order.is_closed():
        return {"can_create": False, "reason": "Заявка уже закрыта."}
    mode = _normalize_text(correction_mode)
    if mode not in {"whole_box", "piece"}:
        return {"can_create": False, "reason": "Неизвестный способ добора."}
    shortages = _discrepancy_shortages(shortage_rows)
    if not shortages:
        return {"can_create": False, "reason": "Не найдена положительная недостача по ШК."}

    items_by_barcode = {
        _normalize_text(item.barcode).casefold(): item
        for item in order.items.order_by("id")
        if _normalize_text(item.barcode)
    }
    for shortage in shortages:
        item = items_by_barcode.get(_normalize_text(shortage.get("barcode")).casefold())
        if item is None:
            # Marketplace binding may expose a GM barcode that was omitted from
            # the original request. Keep that request immutable and bind the
            # correction to the warehouse identity resolved below.
            shortage["shipping_item_id"] = 0
            continue
        shortage["shipping_item_id"] = int(item.id or 0)
        shortage["sku_code"] = _normalize_text(item.sku_code)
        shortage["name"] = _normalize_text(item.name)
        shortage["goods_type"] = _normalize_text(item.goods_type)
        shortage["size"] = _normalize_text(item.size)

    excluded_pallet_codes = _failed_no_stock_pallet_codes_for_order(order)
    catalog = _discrepancy_stock_catalog(
        order=order,
        shortages=shortages,
        excluded_pallet_codes=excluded_pallet_codes,
    )
    used_box_codes: set[str] = set()
    demand_payloads: list[dict] = []
    requested_qty_by_item_id: dict[int, int] = defaultdict(int)
    detached_move_items: dict[str, dict] = {}
    plan_rows: list[dict] = []

    for shortage in shortages:
        barcode = _normalize_text(shortage.get("barcode"))
        item_id = _as_int(shortage.get("shipping_item_id"))
        required_qty = max(_as_int(shortage.get("missing_qty")), 0)
        candidates = [
            box
            for box in catalog.get(barcode.casefold(), [])
            if mode == "piece"
            or _normalize_text(box.get("code")).casefold() not in used_box_codes
        ]
        if item_id <= 0:
            warehouse_line = next(
                (
                    line
                    for box in candidates
                    for line in box.get("items") or []
                    if _normalize_text(line.get("barcode")).casefold()
                    == barcode.casefold()
                ),
                {},
            )
            shortage["sku_code"] = (
                _normalize_text(shortage.get("sku_code"))
                or _normalize_text(warehouse_line.get("sku") or warehouse_line.get("sku_code"))
            )
            shortage["name"] = (
                _normalize_text(shortage.get("name"))
                or _normalize_text(warehouse_line.get("name"))
            )
            shortage["goods_type"] = (
                _normalize_text(shortage.get("goods_type"))
                or _normalize_text(warehouse_line.get("goods_type"))
            )
            shortage["size"] = (
                _normalize_text(shortage.get("size"))
                or _normalize_text(warehouse_line.get("size"))
            )
            detached_move_items[barcode.casefold()] = {
                "sku_code": shortage["sku_code"],
                "barcode": barcode,
                "goods_type": shortage["goods_type"],
                "qty_requested": required_qty,
            }
        if mode == "whole_box":
            selected = _exact_whole_box_selection(candidates, required_qty)
            if not selected:
                excluded_hint = (
                    " Паллеты, где ричтрак уже отметил отсутствие товара, исключены: "
                    + ", ".join(excluded_pallet_codes)
                    + "."
                    if excluded_pallet_codes
                    else ""
                )
                return {
                    "can_create": False,
                    "reason": (
                        f"Для ШК {barcode} нет точной комбинации целых коробов "
                        f"на {required_qty} шт. Выберите поштучный добор."
                        f"{excluded_hint}"
                    ),
                    "excluded_pallet_codes": excluded_pallet_codes,
                }
            picks = [(box, _as_int(box.get("target_qty"))) for box in selected]
        else:
            picks: list[tuple[dict, int]] = []
            single_box = next(
                (
                    box
                    for box in candidates
                    if max(_as_int(box.get("target_qty")), 0) >= required_qty
                ),
                None,
            )
            if single_box is not None:
                picks.append((single_box, required_qty))
                remaining = 0
            else:
                remaining = required_qty
                for box in candidates:
                    if remaining <= 0:
                        break
                    take_qty = min(max(_as_int(box.get("target_qty")), 0), remaining)
                    if take_qty <= 0:
                        continue
                    picks.append((box, take_qty))
                    remaining -= take_qty
            if remaining > 0:
                excluded_hint = (
                    " Паллеты, где ричтрак уже отметил отсутствие товара, исключены: "
                    + ", ".join(excluded_pallet_codes)
                    + ". Проверьте размещение или выполните инвентаризацию."
                    if excluded_pallet_codes
                    else ""
                )
                return {
                    "can_create": False,
                    "reason": (
                        f"Для ШК {barcode} не хватает доступного товара: "
                        f"нужно {required_qty} шт., дефицит {remaining} шт."
                        f"{excluded_hint}"
                    ),
                    "excluded_pallet_codes": excluded_pallet_codes,
                }

        for box, pick_qty in picks:
            box_code = _normalize_text(box.get("code"))
            used_box_codes.add(box_code.casefold())
            source_composition = [
                _discrepancy_source_line(
                    item,
                    shipping_item_id=item_id,
                    target_barcode=barcode,
                )
                for item in box.get("items") or []
            ]
            target_source = next(
                (
                    item
                    for item in box.get("items") or []
                    if _normalize_text(item.get("barcode")).casefold()
                    == barcode.casefold()
                ),
                {},
            )
            pick_composition = [
                _discrepancy_source_line(
                    target_source,
                    shipping_item_id=item_id,
                    target_barcode=barcode,
                    qty_override=pick_qty,
                )
            ]
            complete_closed_box = _discrepancy_pick_is_complete_closed_box(
                box,
                pick_qty=pick_qty,
                source_composition=source_composition,
                pick_composition=pick_composition,
            )
            demand_type = (
                OtgDeliveryDemand.TYPE_FULL_BOX
                if mode == "whole_box" or complete_closed_box
                else OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT
            )
            key_payload = {
                "type": demand_type,
                "barcode": barcode.casefold(),
                "source_box_code": box_code.casefold(),
                "pick_qty": pick_qty,
            }
            demand_payloads.append(
                {
                    "demand_key": _demand_key(key_payload),
                    "demand_type": demand_type,
                    "boxes_required": 1,
                    "box_qty": max(_as_int(box.get("total_qty")), pick_qty),
                    "composition": source_composition,
                    "pick_composition": (
                        []
                        if demand_type == OtgDeliveryDemand.TYPE_FULL_BOX
                        else pick_composition
                    ),
                    "item_quantities_per_box": (
                        {str(item_id): pick_qty}
                        if item_id > 0
                        else {}
                    ),
                    "source_box_codes": [box_code],
                    "payload": {
                        "discrepancy_pick_v2": True,
                        "correction_mode": mode,
                        "effective_pick_mode": (
                            "whole_box"
                            if demand_type == OtgDeliveryDemand.TYPE_FULL_BOX
                            else "piece"
                        ),
                        "source_box_code": box_code,
                        "source_box_open": bool(box.get("is_open")),
                    },
                }
            )
            if item_id > 0:
                requested_qty_by_item_id[item_id] += pick_qty
            plan_rows.append(
                {
                    "barcode": barcode,
                    "sku_code": shortage.get("sku_code") or "",
                    "required_qty": required_qty,
                    "pick_qty": pick_qty,
                    "box_code": box_code,
                    "pallet_code": _normalize_text(box.get("pallet_code")),
                    "location": _normalize_text(box.get("location_label")),
                    "is_open": bool(box.get("is_open")),
                    "effective_pick_mode": (
                        "whole_box"
                        if demand_type == OtgDeliveryDemand.TYPE_FULL_BOX
                        else "piece"
                    ),
                }
            )

    if mode == "piece":
        demand_payloads = _merge_demands_for_shared_source_boxes(demand_payloads)
        for demand in demand_payloads:
            composition = list(demand.get("composition") or [])
            if not demand.get("demand_key"):
                demand["demand_key"] = _demand_key(
                    {
                        "type": demand.get("demand_type"),
                        "composition": _composition_key(composition),
                        "pick_composition": _composition_key(
                            list(demand.get("pick_composition") or [])
                        ),
                        "source_box_codes": sorted(
                            _normalize_text(code).lower()
                            for code in demand.get("source_box_codes") or []
                        ),
                    }
                )
            if _as_int(demand.get("box_qty")) <= 0:
                demand["box_qty"] = _composition_box_qty(composition)

    fingerprint_payload = {
        "mode": mode,
        "excluded_pallet_codes": excluded_pallet_codes,
        "rows": [
            {
                "barcode": row["barcode"],
                "pick_qty": row["pick_qty"],
                "box_code": row["box_code"],
                "pallet_code": row["pallet_code"],
            }
            for row in plan_rows
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "can_create": True,
        "correction_mode": mode,
        "requested_qty": (
            sum(requested_qty_by_item_id.values())
            + sum(
                max(_as_int(row.get("qty_requested")), 0)
                for row in detached_move_items.values()
            )
        ),
        "source_box_count": len(used_box_codes),
        "plan_rows": plan_rows,
        "plan_fingerprint": fingerprint,
        "demand_payloads": demand_payloads,
        "requested_qty_by_item_id": {
            str(item_id): qty
            for item_id, qty in requested_qty_by_item_id.items()
        },
        "detached_move_items": list(detached_move_items.values()),
        "excluded_pallet_codes": excluded_pallet_codes,
    }


@transaction.atomic
def create_otg_shipping_discrepancy_pick(
    *,
    order: ShippingOrder,
    shortage_rows: list[dict],
    correction_mode: str,
    user=None,
    requested_by_name: str = "",
    requested_by_role: str = "",
    expected_plan: dict | None = None,
) -> tuple[MoveRequest, list[str], dict]:
    preview = get_otg_discrepancy_pick_preview(
        order=order,
        shortage_rows=shortage_rows,
        correction_mode=correction_mode,
    )
    if not preview.get("can_create"):
        raise ValidationError(
            str(preview.get("reason") or "Корректирующий добор недоступен.")
        )
    expected_fingerprint = _normalize_text(
        (expected_plan or {}).get("plan_fingerprint")
        if isinstance(expected_plan, dict)
        else ""
    )
    if (
        expected_fingerprint
        and expected_fingerprint != preview.get("plan_fingerprint")
    ):
        raise ValidationError(
            "Остаток или размещение изменились после решения менеджера. "
            "Задание не создано, складские данные не изменены."
        )

    item_ids = {
        _as_int(item_id)
        for item_id in (preview.get("requested_qty_by_item_id") or {})
        if _as_int(item_id) > 0
    }
    demand_items = list(order.items.filter(id__in=item_ids).order_by("id"))
    if len(demand_items) != len(item_ids):
        raise ValidationError("Не найдены исходные строки заявки для добора.")
    move_request, move_ids, shortage_qty = create_otg_shipping_pick_request(
        order=order,
        user=user,
        requested_by_name=requested_by_name,
        requested_by_role=requested_by_role,
        allow_partial=False,
        demand_items=demand_items,
        demand_payloads_override=preview["demand_payloads"],
        requested_qty_by_item_id=preview["requested_qty_by_item_id"],
        detached_move_items=preview.get("detached_move_items"),
        cancel_existing=False,
        request_reason=_DISCREPANCY_OTG_PICK_REASON,
    )
    if shortage_qty > 0 or not move_ids:
        raise ValidationError(
            "Не удалось полностью покрыть добор по сохраненному плану. "
            "Новое задание не создано."
        )
    return move_request, move_ids, preview


def get_otg_supplemental_pick_preview(order: ShippingOrder) -> dict:
    """Return a read-only supplement plan for a confirmed OTG shortage."""
    if order.is_closed():
        return {"can_create": False, "reason": "Заявка уже закрыта."}
    discrepancy_status = _normalize_text(order.shipping_discrepancy_status)
    if discrepancy_status and discrepancy_status != "pending":
        return {
            "can_create": False,
            "reason": "По расхождению уже принято решение или создан отдельный добор.",
        }

    source_request, confirmed_shortage_boxes, failed_pallet_codes = _confirmed_shortage_source(order)
    if source_request is None:
        return {"can_create": False, "reason": "Нет подтвержденной недостачи для добора."}

    open_statuses = [MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS]
    all_requests = OtgDeliveryRequest.objects.filter(shipping_order=order).order_by("-id")
    for candidate in all_requests:
        if candidate.pallet_plans.filter(move_task__status__in=open_statuses).exists():
            return {"can_create": False, "reason": "По заявке уже есть активные задания ричтраку."}
        payload = candidate.payload if isinstance(candidate.payload, dict) else {}
        if payload.get("request_reason") != _SUPPLEMENTAL_OTG_PICK_REASON:
            continue
        if candidate.status != OtgDeliveryRequest.STATUS_CANCELED:
            return {
                "can_create": False,
                "reason": "Добор уже создан. Повторный добор возможен только после разбора его факта.",
            }

    partial_demands, requested_qty_by_item_id, failed_partial_boxes = (
        _failed_no_stock_partial_demands(
            source_request,
            failed_pallet_codes=failed_pallet_codes,
        )
    )
    if failed_partial_boxes > 0:
        if failed_partial_boxes != confirmed_shortage_boxes:
            return {
                "can_create": False,
                "reason": (
                    "Одновременно найдены поштучная и коробочная недостача. "
                    "Создайте добор после раздельного разбора заданий."
                ),
            }
        if not partial_demands or not requested_qty_by_item_id:
            return {
                "can_create": False,
                "reason": "Не удалось восстановить состав упавшего поштучного задания.",
            }
        item_ids = sorted(_as_int(item_id) for item_id in requested_qty_by_item_id)
        if not item_ids:
            return {"can_create": False, "reason": "Не найдены строки заявки для добора."}
        return {
            "can_create": True,
            "source_request_id": source_request.id,
            "demand_payloads": partial_demands,
            "item_ids": item_ids,
            "requested_qty_by_item_id": requested_qty_by_item_id,
            "requested_boxes": sum(
                max(_as_int(payload.get("boxes_required")), 0)
                for payload in partial_demands
            ),
            "requested_qty": sum(requested_qty_by_item_id.values()),
            "delivered_box_count": len(_delivered_otg_boxes_for_order(order)),
            "excluded_pallet_codes": failed_pallet_codes,
            "plan_rows": [],
            "correction_mode": "piece",
            "is_partial_retry": True,
        }

    original_demands = build_box_demands(order)
    full_demands = [
        demand for demand in original_demands
        if demand.get("demand_type") != OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT
    ]
    partial_demands = [
        demand for demand in original_demands
        if demand.get("demand_type") == OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT
    ]
    remaining_demands, delivered_summary = _subtract_delivered_otg_boxes(order, full_demands)
    remaining_demands = _subtract_delivered_otg_partial_picks(
        order, remaining_demands + partial_demands, delivered_summary,
    )
    if not remaining_demands:
        return {"can_create": False, "reason": "Складской факт уже покрывает заявку."}
    if any(
        demand.get("demand_type") == OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT
        for demand in remaining_demands
    ):
        return {
            "can_create": False,
            "reason": "Частичный добор оформляется отдельным сценарием сканирования единиц.",
        }

    demand_payloads = []
    item_ids: set[int] = set()
    requested_qty_by_item_id: dict[int, int] = defaultdict(int)
    requested_boxes = 0
    requested_qty = 0
    for demand in remaining_demands:
        payload = deepcopy(demand)
        # Existing source codes are historical planning hints, not facts of the new task.
        payload["source_box_codes"] = []
        payload["payload"] = {
            **dict(payload.get("payload") or {}),
            "supplemental_pick_source_request_id": source_request.id,
            "supplemental_pick_excluded_pallet_codes": failed_pallet_codes,
        }
        demand_payloads.append(payload)
        boxes_required = max(_as_int(payload.get("boxes_required")), 0)
        requested_boxes += boxes_required
        requested_qty += boxes_required * max(_as_int(payload.get("box_qty")), 0)
        for raw_item_id, raw_qty in dict(
            payload.get("item_quantities_per_box") or {}
        ).items():
            item_id = _as_int(raw_item_id)
            qty_per_box = max(_as_int(raw_qty), 0)
            if item_id > 0 and qty_per_box > 0:
                item_ids.add(item_id)
                requested_qty_by_item_id[item_id] += qty_per_box * boxes_required
        for line in payload.get("composition") or []:
            item_id = _as_int(line.get("shipping_item_id"))
            if item_id > 0:
                item_ids.add(item_id)

    if requested_boxes != confirmed_shortage_boxes:
        return {
            "can_create": False,
            "reason": "Факт OTG изменился. Сначала обновите разбор недостачи.",
        }
    if not item_ids:
        return {"can_create": False, "reason": "Не найдены строки заявки для добора."}
    return {
        "can_create": True,
        "source_request_id": source_request.id,
        "demand_payloads": demand_payloads,
        "item_ids": sorted(item_ids),
        "requested_qty_by_item_id": {
            str(item_id): qty
            for item_id, qty in sorted(requested_qty_by_item_id.items())
            if item_id > 0 and qty > 0
        },
        "requested_boxes": requested_boxes,
        "requested_qty": requested_qty,
        "delivered_box_count": len(delivered_summary.get("boxes") or []),
        "excluded_pallet_codes": failed_pallet_codes,
    }


@transaction.atomic
def create_otg_shipping_supplemental_pick(
    *,
    order: ShippingOrder,
    user=None,
    requested_by_name: str = "",
    requested_by_role: str = "",
) -> tuple[MoveRequest, list[str], dict]:
    order = ShippingOrder.objects.select_for_update().get(pk=order.pk)
    preview = get_otg_supplemental_pick_preview(order)
    if not preview.get("can_create"):
        raise ValidationError(str(preview.get("reason") or "Добор недоступен."))

    demand_items = list(order.items.filter(id__in=preview["item_ids"]).order_by("id"))
    if not demand_items:
        raise ValidationError("Не найдены строки заявки для добора.")
    move_request, move_ids, shortage_qty = create_otg_shipping_pick_request(
        order=order,
        user=user,
        requested_by_name=requested_by_name,
        requested_by_role=requested_by_role,
        allow_partial=False,
        demand_items=demand_items,
        demand_payloads_override=preview["demand_payloads"],
        requested_qty_by_item_id=preview.get("requested_qty_by_item_id"),
        detached_move_items=preview.get("detached_move_items"),
        cancel_existing=False,
        request_reason=_SUPPLEMENTAL_OTG_PICK_REASON,
    )
    if shortage_qty > 0 or not move_ids:
        raise ValidationError("Не удалось полностью покрыть добор доступным товаром. Новое задание не создано.")

    supplemental_request = OtgDeliveryRequest.objects.select_for_update().get(move_request=move_request)
    supplemental_request.payload = {
        **dict(supplemental_request.payload or {}),
        "supplemental_pick": {
            "source_request_id": preview["source_request_id"],
            "requested_boxes": preview["requested_boxes"],
            "requested_qty": preview["requested_qty"],
        },
    }
    supplemental_request.save(update_fields=["payload", "updated_at"])
    _make_event(
        supplemental_request,
        "supplemental_pick_created",
        payload={
            "source_request_id": preview["source_request_id"],
            "move_ids": move_ids,
            "requested_boxes": preview["requested_boxes"],
            "requested_qty": preview["requested_qty"],
        },
    )
    if _normalize_text(order.shipping_discrepancy_status) == "pending":
        from shipping.discrepancy import (
            attach_operational_supplement_to_pending_discrepancy,
        )

        attach_operational_supplement_to_pending_discrepancy(
            order=order,
            user=user,
            move_ids=move_ids,
            preview=preview,
        )
    return move_request, move_ids, preview


def _location_from_row(row) -> dict:
    return _build_location(
        _row_value(row, "zone", "") or _row_value(row, "zone_code", ""),
        _as_int(_row_value(row, "row", 0)),
        _as_int(_row_value(row, "section", 0)),
        _as_int(_row_value(row, "tier", 0)),
        _as_int(_row_value(row, "cell", 0)),
    )


def _fallback_boxes_from_rows(rows: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for row in rows:
        box_code = _normalize_text(_row_value(row, "box_code", "") or _row_value(row, "container_code", ""))
        if not box_code:
            continue
        entry = grouped.setdefault(box_code, {"code": box_code, "qty": 0, "barcode_qty": {}, "items": []})
        qty = max(_as_int(_row_value(row, "qty", 0)), 0)
        entry["qty"] += qty
        barcode = _normalize_text(_row_value(row, "barcode", ""))
        if barcode and qty > 0:
            entry["barcode_qty"][barcode] = entry["barcode_qty"].get(barcode, 0) + qty
        entry["items"].append(
            {
                "sku": _normalize_text(_row_value(row, "sku", "") or _row_value(row, "sku_code", "")),
                "barcode": barcode,
                "size": _normalize_text(_row_value(row, "size", "")),
                "goods_type": _normalize_text(_row_value(row, "goods_type", "")),
                "qty": qty,
            }
        )
    return list(grouped.values())


def _pallet_catalog(
    base_rows: list[dict],
    *,
    agency_id: int | None,
    blocked_pallets: set[str],
    blocked_box_keys: set[str] | None = None,
) -> dict[str, dict]:
    blocked_box_keys = {
        _normalize_text(code).lower()
        for code in (blocked_box_keys or set())
        if _normalize_text(code)
    }
    rows_by_pallet: dict[str, list[dict]] = defaultdict(list)
    available_box_keys: dict[str, set[str]] = defaultdict(set)
    for row in base_rows:
        pallet_code = _normalize_text(_row_value(row, "pallet_code", "") or _row_value(row, "parent_container_code", ""))
        if not pallet_code or pallet_code in blocked_pallets:
            continue
        box_code = _normalize_text(_row_value(row, "box_code", "") or _row_value(row, "container_code", ""))
        if box_code and box_code.lower() in blocked_box_keys:
            continue
        rows_by_pallet[pallet_code].append(row)
        if box_code:
            available_box_keys[pallet_code].add(box_code.lower())

    stock_boxes_by_pallet = OperationalStockService.get_pallet_boxes_bulk(
        rows_by_pallet.keys(),
        agency_id=agency_id,
    )
    # A box whose stock is partly held by the FBS contour still reaches this
    # point, because the rows above only require *some* free quantity while the
    # box size below comes from the physical total.  One query per catalog marks
    # those boxes so the pick never plans a box it cannot take whole.
    fbs_held_box_keys: set[str] = set()
    if rows_by_pallet:
        held_query = WarehouseStockSnapshot.objects.filter(
            parent_container__container_code__in=list(rows_by_pallet.keys()),
            is_archived=False,
            qty__gt=0,
            other_reserved_qty__gt=0,
        )
        if agency_id:
            held_query = held_query.filter(agency_id=agency_id)
        fbs_held_box_keys = {
            _normalize_text(code).lower()
            for code in held_query.values_list("container_code", flat=True)
            if _normalize_text(code)
        }
    catalog: dict[str, dict] = {}
    for pallet_code, rows in rows_by_pallet.items():
        first_row = rows[0]
        boxes = stock_boxes_by_pallet.get(pallet_code) or []
        if not boxes:
            continue
        available_keys = available_box_keys.get(pallet_code) or set()
        available_boxes = [
            box
            for box in boxes
            if _normalize_text(box.get("code")).lower() not in blocked_box_keys
            and _normalize_text(box.get("code")).lower() not in fbs_held_box_keys
            and (not available_keys or _normalize_text(box.get("code")).lower() in available_keys)
        ]
        catalog[pallet_code] = {
            "pallet_code": pallet_code,
            "all_boxes": list(boxes),
            "available_boxes": available_boxes,
            "from_location": _location_from_row(first_row),
            "receiving_order_id": _normalize_text(_row_value(first_row, "order_id", "")),
        }
    return catalog


def _snapshot_pallet_code(snapshot: WarehouseStockSnapshot) -> str:
    parent = getattr(snapshot, "parent_container", None)
    return _normalize_text(getattr(parent, "container_code", ""))


def _location_from_snapshot(snapshot: WarehouseStockSnapshot) -> dict:
    location = getattr(snapshot, "location", None)
    return _build_location(
        _normalize_text(getattr(snapshot, "zone_code", "")),
        _as_int(getattr(location, "row_no", 0)),
        _as_int(getattr(location, "section_no", 0)),
        _as_int(getattr(location, "tier_no", 0)),
        _as_int(getattr(location, "cell_no", 0)),
    )


def _snapshot_available_for_otg_source(
    snapshot: WarehouseStockSnapshot,
    *,
    allowed_active_operation_ids: set[int] | None = None,
) -> bool:
    if getattr(snapshot, "is_archived", False):
        return False
    if _as_int(getattr(snapshot, "qty", 0)) <= 0:
        return False
    if (
        _as_int(getattr(snapshot, "available_qty", 0)) <= 0
        and _as_int(getattr(snapshot, "shipping_reserved_qty", 0)) <= 0
    ):
        return False
    if _as_int(getattr(snapshot, "processing_reserved_qty", 0)) > 0:
        return False
    # Symmetric to the processing gate above: stock held by the FBS contour is
    # rejected by the warehouse write path anyway, so refuse it while planning
    # instead of failing the driver at the end of the route.
    if _as_int(getattr(snapshot, "other_reserved_qty", 0)) > 0:
        return False
    active_operation_id = _as_int(getattr(snapshot, "active_operation_id", 0))
    allowed_operation_ids = {
        _as_int(operation_id)
        for operation_id in (allowed_active_operation_ids or set())
        if _as_int(operation_id) > 0
    }
    if active_operation_id > 0 and active_operation_id not in allowed_operation_ids:
        active_operation = getattr(snapshot, "active_operation", None)
        active_status = _normalize_text(getattr(active_operation, "status", "")).lower()
        if active_status not in _FINAL_ACTIVE_OPERATION_STATUSES:
            return False
    zone = _normalize_text(getattr(snapshot, "zone_code", "")).upper()
    state = _normalize_text(getattr(snapshot, "warehouse_state_code", "")).lower()
    if state in {
        WarehouseStateCode.RESERVED_FOR_PROCESSING.value,
        WarehouseStateCode.MOVING_TO_PROCESSING.value,
        WarehouseStateCode.IN_PROCESSING_ZONE.value,
        WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
        WarehouseStateCode.PROCESSING_CONSUMED.value,
    }:
        return False
    return zone != "OTG" and state not in _OTG_DELIVERED_STATE_CODES


def _live_snapshot_by_box_code(
    box_codes: list[str],
    *,
    agency_id: int | None,
    allowed_active_operation_ids: set[int] | None = None,
) -> dict[str, WarehouseStockSnapshot]:
    normalized_codes = [_normalize_text(code) for code in box_codes or [] if _normalize_text(code)]
    if not normalized_codes:
        return {}
    result: dict[str, WarehouseStockSnapshot] = {}
    for snapshot in (
        WarehouseStockSnapshot.objects.select_related("parent_container", "location", "active_operation")
        .filter(
            agency_id=agency_id,
            container_code__in=normalized_codes,
            is_archived=False,
        )
        .order_by("container_code", "-id")
    ):
        code_key = _normalize_text(getattr(snapshot, "container_code", "")).lower()
        if not code_key or code_key in result:
            continue
        if not _snapshot_available_for_otg_source(
            snapshot,
            allowed_active_operation_ids=allowed_active_operation_ids,
        ):
            continue
        result[code_key] = snapshot
    return result


def _plan_with_live_warehouse_context(
    plan_data: dict,
    *,
    agency_id: int | None,
    allowed_active_operation_ids: set[int] | None = None,
) -> dict | None:
    planned_box_codes = [
        _normalize_text(code)
        for code in plan_data.get("planned_box_codes") or []
        if _normalize_text(code)
    ]
    if not planned_box_codes:
        pallet_code = _normalize_text(plan_data.get("pallet_code"))
        if not pallet_code:
            return None
        snapshots = list(
            WarehouseStockSnapshot.objects.select_related("parent_container", "location", "active_operation")
            .filter(
                agency_id=agency_id,
                parent_container__container_code=pallet_code,
                is_archived=False,
            )
            .order_by("container_code", "-id")
        )
        if not any(
            _snapshot_available_for_otg_source(
                snapshot,
                allowed_active_operation_ids=allowed_active_operation_ids,
            )
            for snapshot in snapshots
        ):
            return None
        return plan_data

    snapshot_by_code = _live_snapshot_by_box_code(
        planned_box_codes,
        agency_id=agency_id,
        allowed_active_operation_ids=allowed_active_operation_ids,
    )
    snapshots: list[WarehouseStockSnapshot] = []
    for code in planned_box_codes:
        snapshot = snapshot_by_code.get(code.lower())
        if snapshot is None or not _snapshot_available_for_otg_source(
            snapshot,
            allowed_active_operation_ids=allowed_active_operation_ids,
        ):
            return None
        snapshots.append(snapshot)

    pallet_codes = {_snapshot_pallet_code(snapshot) for snapshot in snapshots}
    pallet_codes.discard("")
    if len(pallet_codes) != 1:
        return None

    live_pallet_code = next(iter(pallet_codes))
    verified = dict(plan_data)
    original_pallet_code = _normalize_text(plan_data.get("pallet_code"))
    verified["pallet_code"] = live_pallet_code
    verified["from_location"] = _location_from_snapshot(snapshots[0])
    payload = dict(verified.get("payload") or {})
    payload["warehouse_live_verified"] = True
    if original_pallet_code and original_pallet_code != live_pallet_code:
        payload["original_planned_pallet_code"] = original_pallet_code
    verified["payload"] = payload
    return verified


def _filter_live_warehouse_plans(
    plans: list[dict],
    *,
    agency_id: int | None,
    allowed_active_operation_ids: set[int] | None = None,
) -> tuple[list[dict], int, int]:
    verified_plans: list[dict] = []
    dropped_boxes = 0
    dropped_qty = 0
    for plan_data in plans:
        verified = _plan_with_live_warehouse_context(
            plan_data,
            agency_id=agency_id,
            allowed_active_operation_ids=allowed_active_operation_ids,
        )
        if verified is None:
            dropped_boxes += max(_as_int(plan_data.get("boxes_planned")), 0)
            dropped_qty += max(_as_int(plan_data.get("qty_planned")), 0)
            continue
        verified_plans.append(verified)
    return verified_plans, dropped_boxes, dropped_qty


def _sync_demand_planned_boxes(demands: list[OtgDeliveryDemand], plans: list[dict]) -> None:
    planned_by_demand_id: dict[int, int] = defaultdict(int)
    for plan_data in plans:
        demand = plan_data.get("demand")
        demand_id = int(getattr(demand, "id", 0) or 0)
        if demand_id <= 0:
            continue
        planned_by_demand_id[demand_id] += max(_as_int(plan_data.get("boxes_planned")), 0)
    for demand in demands:
        planned = planned_by_demand_id.get(int(demand.id or 0), 0)
        if int(demand.boxes_planned or 0) == planned:
            continue
        demand.boxes_planned = planned
        demand.save(update_fields=["boxes_planned", "updated_at"])


def _make_event(request: OtgDeliveryRequest, event_type: str, message: str = "", payload: dict | None = None) -> None:
    OtgPlanningEvent.objects.create(
        request=request,
        event_type=event_type,
        message=message,
        payload=payload or {},
    )


def _plan_demands(
    *,
    otg_request: OtgDeliveryRequest,
    demand_models: list[OtgDeliveryDemand],
    base_rows: list[dict],
    agency_id: int | None,
    order: ShippingOrder | None = None,
    apply_shipping_priority: bool = True,
) -> tuple[list[dict], int, int]:
    blocked_pallets, blocked_box_keys = _active_otg_source_guards(agency_id)
    if order is not None and apply_shipping_priority:
        blocked_pallets.update(_higher_priority_shipping_pallet_claims(order))
    blocked_pallets.update(
        _normalize_text(pallet_code)
        for demand in demand_models
        for pallet_code in dict(demand.payload or {}).get("supplemental_pick_excluded_pallet_codes", [])
        if _normalize_text(pallet_code)
    )
    blocked_box_keys.update(
        _normalize_text(box_code).lower()
        for demand in demand_models
        for box_code in dict(demand.payload or {}).get("runtime_excluded_box_codes", [])
        if _normalize_text(box_code)
    )
    catalog = _pallet_catalog(
        base_rows,
        agency_id=agency_id,
        blocked_pallets=blocked_pallets,
        blocked_box_keys=blocked_box_keys,
    )
    used_box_keys: set[tuple[str, str]] = set()
    used_full_pallets: set[str] = set()
    preplanned_boxes_by_demand_id: dict[int, int] = defaultdict(int)
    plans: list[dict] = []
    shortage_boxes = 0
    shortage_qty = 0

    remaining_by_demand_id = {
        int(demand.id or 0): max(int(demand.boxes_required or 0), 0)
        for demand in demand_models
    }
    full_pallet_candidates = []
    for pallet_code, pallet in catalog.items():
        plan = _combined_full_pallet_plan(
            pallet_code=pallet_code,
            pallet=pallet,
            demands=demand_models,
            remaining_by_demand_id=remaining_by_demand_id,
            used_box_keys=used_box_keys,
        )
        if plan:
            full_pallet_candidates.append(plan)
    full_pallet_candidates.sort(key=lambda plan: (-_as_int(plan.get("boxes_planned")), _normalize_text(plan.get("pallet_code"))))
    for plan in full_pallet_candidates:
        pallet_code = _normalize_text(plan.get("pallet_code"))
        if not pallet_code or pallet_code in used_full_pallets:
            continue
        assigned_demand_ids = [
            int(demand_id or 0)
            for demand_id in plan.get("assigned_demand_ids") or []
            if int(demand_id or 0) > 0
        ]
        assigned_demand_counts = {
            int(demand_id or 0): max(_as_int(count), 0)
            for demand_id, count in dict(plan.get("assigned_demand_counts") or {}).items()
            if int(demand_id or 0) > 0
        }
        if any(
            remaining_by_demand_id.get(demand_id, 0) < assigned_demand_counts.get(demand_id, 0)
            for demand_id in assigned_demand_ids
        ):
            continue
        selected_box_codes = [_normalize_text(code) for code in plan.get("planned_box_codes") or [] if _normalize_text(code)]
        if any((pallet_code, box_code.lower()) in used_box_keys for box_code in selected_box_codes):
            continue
        plans.append(plan)
        for box_code in selected_box_codes:
            used_box_keys.add((pallet_code, box_code.lower()))
        used_full_pallets.add(pallet_code)
        for demand_id in assigned_demand_ids:
            assigned_count = assigned_demand_counts.get(demand_id, 0)
            remaining_by_demand_id[demand_id] = max(remaining_by_demand_id.get(demand_id, 0) - assigned_count, 0)
            preplanned_boxes_by_demand_id[demand_id] += assigned_count

    for demand in demand_models:
        remaining = max(int(demand.boxes_required or 0), 0) - preplanned_boxes_by_demand_id.get(int(demand.id or 0), 0)
        if remaining <= 0:
            if demand.boxes_planned != max(int(demand.boxes_required or 0), 0):
                demand.boxes_planned = max(int(demand.boxes_required or 0), 0)
                demand.save(update_fields=["boxes_planned", "updated_at"])
            continue
        composition = list(demand.composition or [])
        source_box_keys = {
            _normalize_text(code).lower()
            for code in (demand.source_box_codes or [])
            if _normalize_text(code)
        }
        box_qty = max(int(demand.box_qty or 0), 0)
        # Explicitly selected physical boxes are binding for every demand type.
        # A warehouse operator may replace them only through the guarded
        # assignment editor, never implicitly during route optimization.
        enforce_source_box_keys = bool(source_box_keys)
        matching_by_pallet: dict[str, list[dict]] = {}
        for pallet_code, pallet in catalog.items():
            if pallet_code in used_full_pallets:
                continue
            matches = []
            for box in pallet.get("available_boxes") or []:
                box_code = _normalize_text(box.get("code"))
                if not box_code or (pallet_code, box_code.lower()) in used_box_keys:
                    continue
                if enforce_source_box_keys and box_code.lower() not in source_box_keys:
                    continue
                if demand.demand_type == OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT:
                    if source_box_keys:
                        pick_composition = list(demand.pick_composition or composition)
                        if _box_contains_pick_composition(box, pick_composition):
                            matches.append(box)
                        continue
                    pick_composition = list(demand.pick_composition or composition)
                    if _box_contains_pick_composition(box, pick_composition):
                        matches.append(box)
                    continue
                if _box_matches_composition(box, composition):
                    matches.append(box)
            if matches:
                matching_by_pallet[pallet_code] = sorted(matches, key=lambda item: _normalize_text(item.get("code")).lower())

        if demand.demand_type != OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT:
            full_candidates = []
            for pallet_code, matches in matching_by_pallet.items():
                pallet = catalog.get(pallet_code) or {}
                all_boxes = [box for box in pallet.get("all_boxes") or [] if _normalize_text(box.get("code"))]
                if not all_boxes:
                    continue
                if len(matches) != len(all_boxes):
                    continue
                if not all(_box_matches_composition(box, composition) for box in all_boxes):
                    continue
                full_candidates.append((pallet_code, len(matches)))
            full_candidates.sort(key=lambda item: (-item[1], item[0]))

            for pallet_code, box_count in full_candidates:
                if remaining <= 0:
                    break
                if box_count <= 0 or box_count > remaining:
                    continue
                pallet = catalog[pallet_code]
                selected_boxes = matching_by_pallet.get(pallet_code) or []
                plans.append(
                    {
                        "demand": demand,
                        "plan_type": OtgPalletPlan.TYPE_FULL_PALLET,
                        "pallet_code": pallet_code,
                        "boxes_planned": box_count,
                        "qty_planned": box_count * box_qty,
                        "from_location": pallet.get("from_location") or _build_location("PR", 0, 0, 0, 0),
                        "receiving_order_id": pallet.get("receiving_order_id") or "",
                        "planned_box_codes": [_normalize_text(box.get("code")) for box in selected_boxes],
                    }
                )
                for box in selected_boxes:
                    used_box_keys.add((pallet_code, _normalize_text(box.get("code")).lower()))
                used_full_pallets.add(pallet_code)
                remaining -= box_count

        while remaining > 0:
            partial_candidates = []
            for pallet_code, matches in matching_by_pallet.items():
                if pallet_code in used_full_pallets:
                    continue
                available = [
                    box
                    for box in matches
                    if (pallet_code, _normalize_text(box.get("code")).lower()) not in used_box_keys
                ]
                if available:
                    partial_candidates.append((pallet_code, available))
            if not partial_candidates:
                break
            partial_candidates.sort(key=lambda item: (-min(len(item[1]), remaining), item[0]))
            pallet_code, available = partial_candidates[0]
            pick_count = min(len(available), remaining)
            if (
                demand.demand_type == OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT
                and not source_box_keys
            ):
                # A replacement partial pick changes one physical source box.
                # Plan it separately so the task can bind that exact box and
                # validate its live composition before any stock write.
                pick_count = 1
            selected_boxes = available[:pick_count]
            pallet = catalog[pallet_code]
            plan_type = (
                OtgPalletPlan.TYPE_PARTIAL_BOX_SPLIT
                if demand.demand_type == OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT
                else OtgPalletPlan.TYPE_PICK_BOXES
            )
            plan_payload = {}
            if (
                demand.demand_type == OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT
                and not source_box_keys
                and len(selected_boxes) == 1
            ):
                plan_payload = {
                    "composition_override": _box_composition(selected_boxes[0]),
                    "bind_planned_partial_box": True,
                }
            plans.append(
                {
                    "demand": demand,
                    "plan_type": plan_type,
                    "pallet_code": pallet_code,
                    "boxes_planned": pick_count,
                    "qty_planned": pick_count
                    * (
                        _composition_box_qty(list(demand.pick_composition or []))
                        if demand.demand_type == OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT
                        else box_qty
                    ),
                    "from_location": pallet.get("from_location") or _build_location("PR", 0, 0, 0, 0),
                    "receiving_order_id": pallet.get("receiving_order_id") or "",
                    "planned_box_codes": [_normalize_text(box.get("code")) for box in selected_boxes],
                    "payload": plan_payload,
                }
            )
            for box in selected_boxes:
                used_box_keys.add((pallet_code, _normalize_text(box.get("code")).lower()))
            remaining -= pick_count

        flexible_shortage_qty = 0
        if remaining > 0 and box_qty > 0 and not source_box_keys:
            flexible_line = _single_line_flexible_demand(demand, composition)
            if flexible_line is not None:
                requested_line, item_id = flexible_line
                target_qty = remaining * box_qty
                selected_flexible = _select_exact_flexible_candidates(
                    _flexible_box_candidates(
                        catalog=catalog,
                        requested_line=requested_line,
                        used_box_keys=used_box_keys,
                        used_full_pallets=used_full_pallets,
                    ),
                    target_qty,
                )
                selected_qty = _append_flexible_plans(
                    plans=plans,
                    demand=demand,
                    selected=selected_flexible,
                    item_id=item_id,
                    requested_line=requested_line,
                    used_box_keys=used_box_keys,
                )
                if selected_qty > 0:
                    flexible_shortage_qty = max(target_qty - selected_qty, 0)
                    remaining = 0

        planned = max(int(demand.boxes_required or 0), 0) - remaining
        if planned != demand.boxes_planned:
            demand.boxes_planned = planned
            demand.save(update_fields=["boxes_planned", "updated_at"])
        if flexible_shortage_qty > 0:
            shortage_qty += flexible_shortage_qty
            shortage_boxes += max((flexible_shortage_qty + box_qty - 1) // box_qty, 1)
        if remaining > 0:
            shortage_qty_per_box = (
                _composition_box_qty(list(demand.pick_composition or []))
                if demand.demand_type == OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT
                else box_qty
            )
            shortage_boxes += remaining
            shortage_qty += remaining * shortage_qty_per_box

    return plans, shortage_boxes, shortage_qty


def _display_otg_order_number(number: str) -> str:
    normalized = _normalize_text(number)
    match = re.fullmatch(r"OTG-0*(\d+)", normalized, flags=re.IGNORECASE)
    if not match:
        return normalized
    return f"{int(match.group(1))}_OTG"


def _blocked_shipping_orders_for_unplanned_demands(
    *,
    order: ShippingOrder,
    demand_models: list,
    plans: list[dict],
) -> list[str]:
    """Describe foreign shipping tasks occupying exact source pallets."""

    planned_by_demand_id: dict[int, int] = defaultdict(int)
    for plan in plans:
        demand = plan.get("demand")
        demand_id = int(getattr(demand, "id", 0) or 0)
        if demand_id > 0:
            planned_by_demand_id[demand_id] += max(_as_int(plan.get("boxes_planned")), 0)
        for assigned_id, count in dict(plan.get("assigned_demand_counts") or {}).items():
            normalized_id = int(assigned_id or 0)
            if normalized_id > 0:
                planned_by_demand_id[normalized_id] += max(_as_int(count), 0)

    unplanned_source_codes = {
        _normalize_text(code)
        for demand in demand_models
        if planned_by_demand_id.get(int(getattr(demand, "id", 0) or 0), 0)
        < max(int(getattr(demand, "boxes_required", 0) or 0), 0)
        for code in (getattr(demand, "source_box_codes", None) or [])
        if _normalize_text(code)
    }
    if not unplanned_source_codes:
        return []

    source_pallet_codes = {
        _normalize_text(pallet_code)
        for pallet_code in WarehouseStockSnapshot.objects.filter(
            agency=order.agency,
            is_archived=False,
            container_code__in=sorted(unplanned_source_codes),
        )
        .exclude(parent_container__container_code="")
        .values_list("parent_container__container_code", flat=True)
        if _normalize_text(pallet_code)
    }
    if not source_pallet_codes:
        return []

    blocking_request_ids = list(
        MoveTask.objects.filter(
            request__agency=order.agency,
            pallet_code__in=sorted(source_pallet_codes),
            status__in=[MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS],
        )
        .exclude(request_id__isnull=True)
        .values_list("request_id", flat=True)
        .distinct()
    )
    if not blocking_request_ids:
        return []

    numbers = (
        OtgDeliveryRequest.objects.filter(move_request_id__in=blocking_request_ids)
        .exclude(shipping_order=order)
        .exclude(shipping_order__number="")
        .values_list("shipping_order__number", flat=True)
        .distinct()
    )
    return sorted({_display_otg_order_number(number) for number in numbers if _normalize_text(number)})


def preview_otg_shipping_pick_coverage(order: ShippingOrder) -> dict:
    """Return full-pick coverage without creating requests, tasks or stock facts."""

    demand_payloads = build_box_demands(order)
    demand_models = []
    for index, payload in enumerate(demand_payloads, start=1):
        demand_models.append(
            SimpleNamespace(
                id=index,
                demand_type=payload["demand_type"],
                boxes_required=max(_as_int(payload.get("boxes_required")), 0),
                boxes_planned=0,
                box_qty=max(_as_int(payload.get("box_qty")), 0),
                composition=list(payload.get("composition") or []),
                pick_composition=list(payload.get("pick_composition") or []),
                item_quantities_per_box=dict(payload.get("item_quantities_per_box") or {}),
                source_box_codes=list(payload.get("source_box_codes") or []),
                payload=dict(payload.get("payload") or {}),
                save=lambda *args, **kwargs: None,
            )
        )

    requested_boxes = sum(int(demand.boxes_required or 0) for demand in demand_models)
    if requested_boxes <= 0:
        return {
            "can_cover": False,
            "requested_boxes": 0,
            "planned_boxes": 0,
            "shortage_boxes": 0,
            "shortage_qty": 0,
            "blocked_by_shipping_orders": [],
        }

    base_rows = _base_rows_for_order(order, demand_payloads)
    plans, shortage_boxes, shortage_qty = _plan_demands(
        otg_request=None,
        demand_models=demand_models,
        base_rows=base_rows,
        agency_id=order.agency_id,
        order=order,
    )
    allowed_active_operation_ids = _preemptible_storage_operation_ids(order.agency_id)
    plans, dropped_boxes, dropped_qty = _filter_live_warehouse_plans(
        plans,
        agency_id=order.agency_id,
        allowed_active_operation_ids=allowed_active_operation_ids,
    )
    blocked_by_shipping_orders = _blocked_shipping_orders_for_unplanned_demands(
        order=order,
        demand_models=demand_models,
        plans=plans,
    )
    shortage_boxes += dropped_boxes
    shortage_qty += dropped_qty
    priority_pallet_claims = _higher_priority_shipping_pallet_claims(order)
    if (shortage_boxes > 0 or shortage_qty > 0) and priority_pallet_claims:
        for demand in demand_models:
            demand.boxes_planned = 0
        unrestricted_plans, unrestricted_shortage_boxes, unrestricted_shortage_qty = _plan_demands(
            otg_request=None,
            demand_models=demand_models,
            base_rows=base_rows,
            agency_id=order.agency_id,
            order=order,
            apply_shipping_priority=False,
        )
        unrestricted_plans, unrestricted_dropped_boxes, unrestricted_dropped_qty = (
            _filter_live_warehouse_plans(
                unrestricted_plans,
                agency_id=order.agency_id,
                allowed_active_operation_ids=allowed_active_operation_ids,
            )
        )
        unrestricted_shortage_boxes += unrestricted_dropped_boxes
        unrestricted_shortage_qty += unrestricted_dropped_qty
        if (
            unrestricted_shortage_boxes < shortage_boxes
            or unrestricted_shortage_qty < shortage_qty
        ):
            blocked_by_shipping_orders = sorted(
                {
                    *blocked_by_shipping_orders,
                    *(
                        number
                        for numbers in priority_pallet_claims.values()
                        for number in numbers
                    ),
                }
            )
    planned_boxes = sum(max(_as_int(plan.get("boxes_planned")), 0) for plan in plans)
    return {
        "can_cover": shortage_boxes <= 0 and shortage_qty <= 0,
        "requested_boxes": requested_boxes,
        "planned_boxes": planned_boxes,
        "shortage_boxes": max(int(shortage_boxes or 0), 0),
        "shortage_qty": max(int(shortage_qty or 0), 0),
        "blocked_by_shipping_orders": blocked_by_shipping_orders,
    }


def _request_items_for_move(
    move_request: MoveRequest,
    order: ShippingOrder,
    *,
    items=None,
    requested_qty_by_item_id: dict[int, int] | None = None,
    detached_move_items: list[dict] | None = None,
) -> dict[int, MoveRequestItem]:
    items = list(items) if items is not None else list(order.items.order_by("id"))
    remaining_qty = _shipping_remaining_qty_by_item_id(order, items)
    requested_qty_by_item_id = {
        _as_int(item_id): max(_as_int(qty), 0)
        for item_id, qty in dict(requested_qty_by_item_id or {}).items()
        if _as_int(item_id) > 0
    }
    result: dict[int, MoveRequestItem] = {}
    for item in items:
        item_id = int(item.id or 0)
        requested_qty = (
            requested_qty_by_item_id[item_id]
            if item_id in requested_qty_by_item_id
            else max(_as_int(remaining_qty.get(item_id, 0)), 0)
        )
        result[item_id] = MoveRequestItem.objects.create(
            request=move_request,
            sku_code=item.sku_code,
            barcode=item.barcode,
            goods_type=item.goods_type,
            qty_requested=requested_qty,
            qty_planned=0,
        )
    for row in detached_move_items or []:
        if not isinstance(row, dict):
            continue
        requested_qty = max(_as_int(row.get("qty_requested")), 0)
        sku_code = _normalize_text(row.get("sku_code"))
        barcode = _normalize_text(row.get("barcode"))
        if requested_qty <= 0 or not (sku_code or barcode):
            continue
        MoveRequestItem.objects.create(
            request=move_request,
            sku_code=sku_code,
            barcode=barcode,
            goods_type=_normalize_text(row.get("goods_type")),
            qty_requested=requested_qty,
            qty_planned=requested_qty,
        )
    return result


def _planned_qty_by_item(plans: list[dict]) -> dict[int, int]:
    result: dict[int, int] = defaultdict(int)
    for plan in plans:
        demand: OtgDeliveryDemand = plan["demand"]
        planned_items = plan.get("planned_item_quantities")
        if isinstance(planned_items, dict):
            for raw_item_id, raw_qty in planned_items.items():
                item_id = _as_int(raw_item_id)
                qty = max(_as_int(raw_qty), 0)
                if item_id > 0 and qty > 0:
                    result[item_id] += qty
            continue
        boxes = max(_as_int(plan.get("boxes_planned")), 0)
        if boxes <= 0:
            continue
        item_quantities = demand.item_quantities_per_box or {}
        for raw_item_id, raw_qty in item_quantities.items():
            item_id = _as_int(raw_item_id)
            qty = max(_as_int(raw_qty), 0)
            if item_id > 0 and qty > 0:
                result[item_id] += boxes * qty
    return result


def _validate_otg_move_payload_quantities(payload: dict) -> None:
    """Fail closed before a task can carry contradictory item quantities."""

    move_mode = _normalize_text(payload.get("move_mode")).lower()
    if move_mode == MOVE_MODE_PALLET_FULL:
        return
    requested_qty = max(_as_int(payload.get("requested_qty")), 0)
    item_total = 0
    for item in payload.get("request_items") or []:
        if not isinstance(item, dict) or _as_int(item.get("shipping_item_id")) <= 0:
            continue
        item_total += max(_as_int(item.get("requested_qty")), 0)
    if item_total > 0 and item_total != requested_qty:
        order_number = _normalize_text(payload.get("shipping_order_id")) or "-"
        raise ValidationError(
            "Внутренняя ошибка OTG-плана: сумма позиций задания "
            f"{item_total} шт. не совпадает с количеством задания "
            f"{requested_qty} шт. по заявке {order_number}. "
            "Задание не создано, складские данные не изменены."
        )
    if move_mode == MOVE_MODE_BOX_FULL and _requested_box_selection_mode(payload) == BOX_SELECTION_FIXED:
        requested_box_count = max(_as_int(payload.get("requested_box_count")), 0)
        requested_boxes = _payload_box_codes(payload)
        if requested_box_count <= 0 or len(requested_boxes) != requested_box_count:
            order_number = _normalize_text(payload.get("shipping_order_id")) or "-"
            raise ValidationError(
                "Внутренняя ошибка OTG-плана: для точного отбора по заявке "
                f"{order_number} указано {requested_box_count} коробов, "
                f"но передано {len(requested_boxes)} уникальных кодов. "
                "Задание не создано, складские данные не изменены."
            )


def _marketplace_marking_barcodes_for_task(
    *,
    order: ShippingOrder,
    request_items: list[dict],
    requested_barcode_qty: dict,
) -> list[str]:
    """Return only marked SKUs that need physical Data Matrix scans in this task."""

    if order.delivery_type != ShippingOrder.DELIVERY_MARKETPLACE:
        return []
    item_ids = {
        _as_int(row.get("shipping_item_id"))
        for row in request_items or []
        if isinstance(row, dict) and _as_int(row.get("shipping_item_id")) > 0
    }
    if not item_ids:
        return []
    requested = {
        _normalize_text(barcode)
        for barcode, qty in dict(requested_barcode_qty or {}).items()
        if _normalize_text(barcode) and _as_int(qty) > 0
    }
    if not requested:
        return []
    candidate_barcodes = {
        _normalize_text(barcode)
        for barcode in ShippingOrderItem.objects.filter(
            order=order,
            id__in=item_ids,
            barcode__in=requested,
        ).values_list("barcode", flat=True)
        if _normalize_text(barcode)
    }
    if not candidate_barcodes:
        return []
    return sorted(
        WarehouseWritePathService._required_marking_barcodes_for_shipping(
            agency=order.agency,
            order_id=order.number,
            barcodes=candidate_barcodes,
        )
    )


def _payload_for_plan(
    *,
    order: ShippingOrder,
    otg_request: OtgDeliveryRequest,
    plan: OtgPalletPlan,
    requested_by_name: str,
    requested_by_role: str,
) -> dict:
    demand = plan.demand
    from sklad.services.operational_locations import select_operational_location

    destination_location = select_operational_location(zone_code="OTG")
    if destination_location is None:
        raise ValueError(
            "В зоне OTG не настроено конкретное место с QR. "
            "Начальник склада должен создать его до передачи заявки водителю."
        )
    destination = {
        "zone": "OTG",
        "code": str(destination_location.location_code or "").strip(),
        "label": str(
            destination_location.display_name
            or destination_location.location_code
            or ""
        ).strip(),
        "row": "",
        "section": "",
        "tier": "",
        "cell": "",
    }
    from_location = plan.from_location or _build_location("PR", 0, 0, 0, 0)
    destination_label = _location_label(destination)
    source_label = _location_label(from_location)
    plan_payload = dict(plan.payload or {})
    planned_box_codes = [
        _normalize_text(code)
        for code in (plan.planned_box_codes or [])
        if _normalize_text(code)
    ]
    explicit_source_keys = {
        _normalize_text(code).lower()
        for code in (demand.source_box_codes or [])
        if _normalize_text(code)
    }
    bind_planned_partial_box = bool(
        plan.plan_type == OtgPalletPlan.TYPE_PARTIAL_BOX_SPLIT
        and plan_payload.get("bind_planned_partial_box")
    )
    bound_box_codes = (
        list(planned_box_codes)
        if bind_planned_partial_box
        else [
            code for code in planned_box_codes if code.lower() in explicit_source_keys
        ]
    )
    if len(bound_box_codes) != len(planned_box_codes):
        bound_box_codes = []
    composition = list(plan_payload.get("composition_override") or demand.composition or [])
    pattern = _pattern_for_composition(composition, boxes=plan.boxes_planned)
    barcode_qty = {
        barcode: qty * max(int(plan.boxes_planned or 0), 0)
        for barcode, qty in _composition_barcode_qty(composition).items()
    }
    requested_box_patterns_override = _merge_requested_box_patterns(
        list(plan_payload.get("requested_box_patterns_override") or [])
    )
    planned_barcode_qty = _normalize_pattern_barcode_qty(plan_payload.get("planned_barcode_qty"))
    if requested_box_patterns_override:
        barcode_qty = _barcode_qty_for_patterns(requested_box_patterns_override)
    if planned_barcode_qty:
        barcode_qty = planned_barcode_qty
    sku_values = sorted({_normalize_text(line.get("sku")) for line in composition if _normalize_text(line.get("sku"))})
    goods_types = sorted({_normalize_goods_type(line.get("goods_type")) for line in composition if _normalize_goods_type(line.get("goods_type"))})
    base_payload = {
        # This flag is deliberately written only to tasks created after this rollout.
        # Legacy OTG tasks keep their existing execution semantics.
        "otg_scan_fact_mode": _OTG_SCAN_FACT_MODE,
        "concrete_location_required": True,
        "concrete_location_version": 1,
        "status": MoveTask.STATUS_CREATED,
        "status_label": "Ожидает отбора по OTG-плану",
        "shipping_order_id": order.number,
        "shipping_order_pk": order.pk,
        "otg_delivery_request_id": otg_request.id,
        "otg_pallet_plan_id": plan.id,
        "pallet_code": plan.pallet_code,
        "from_location": from_location,
        "to_location": destination,
        "from_label": source_label,
        "to_label": destination_label,
        "receiving_order_id": plan.payload.get("receiving_order_id") or "",
        "requested_by_name": requested_by_name,
        "requested_by_role": requested_by_role,
        "requested_sku": sku_values[0] if len(sku_values) == 1 else "",
        "requested_barcodes": sorted(barcode_qty.keys()),
        "requested_barcode_qty": barcode_qty,
        "requested_goods_type": goods_types[0] if len(goods_types) == 1 else "",
        "request_items": [],
        "otg_box_composition": composition,
        "otg_boxes_planned": int(plan.boxes_planned or 0),
        "route_plan": {
            "pallet_code": plan.pallet_code,
            "from_location": from_location,
            "boxes_to_pick": int(plan.boxes_planned or 0),
            "qty_to_pick": int(plan.qty_planned or 0),
        },
        # A route is an advisory prompt. It must not become a box fact or a reserve.
        "planned_box_codes": [],
        "selected_box_codes": [],
        "reserved_box_codes": [],
    }
    planned_items = plan_payload.get("planned_item_quantities")
    if isinstance(planned_items, dict):
        base_payload["request_items"] = [
            {
                "shipping_item_id": _as_int(item_id),
                "requested_qty": max(_as_int(qty), 0),
            }
            for item_id, qty in planned_items.items()
        ]
    else:
        base_payload["request_items"] = [
            {
                "shipping_item_id": _as_int(item_id),
                "requested_qty": max(_as_int(qty), 0) * max(int(plan.boxes_planned or 0), 0),
            }
            for item_id, qty in (demand.item_quantities_per_box or {}).items()
        ]
    if plan.plan_type == OtgPalletPlan.TYPE_FULL_PALLET:
        base_payload.update(
            {
                "status_label": "Ожидает перевозки",
                "pick_mode": "full",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "requested_qty": "",
                "requested_barcode_qty": {},
                "requested_boxes": [],
                "requested_box": "",
                "available_qty": "",
                "task_kind_label": "Паллета целиком",
                "instruction": _shipping_full_pallet_instruction(
                    pallet_code=plan.pallet_code,
                    destination_label=destination_label,
                ),
            }
        )
        return base_payload

    if plan.plan_type == OtgPalletPlan.TYPE_PARTIAL_BOX_SPLIT:
        pick_composition = list(demand.pick_composition or [])
        pick_pattern = _pattern_for_composition(pick_composition, boxes=plan.boxes_planned)
        pick_barcode_qty = {
            barcode: qty * max(int(plan.boxes_planned or 0), 0)
            for barcode, qty in _composition_barcode_qty(pick_composition).items()
        }
        pick_sku_values = sorted(
            {
                _normalize_text(line.get("sku"))
                for line in pick_composition
                if _normalize_text(line.get("sku"))
            }
        )
        pick_goods_types = sorted(
            {
                _normalize_goods_type(line.get("goods_type"))
                for line in pick_composition
                if _normalize_goods_type(line.get("goods_type"))
            }
        )
        base_payload.update(
            {
                "pick_mode": "partial",
                "move_mode": MOVE_MODE_BOX_PARTIAL,
                "requested_qty": int(plan.qty_planned or 0),
                "available_qty": int(plan.qty_planned or 0),
                # The unit scan must describe what leaves the source box, not
                # every SKU that happens to be stored in that mixed box.
                "requested_sku": pick_sku_values[0] if len(pick_sku_values) == 1 else "",
                "requested_barcodes": sorted(pick_barcode_qty.keys()),
                "requested_barcode_qty": pick_barcode_qty,
                "requested_goods_type": pick_goods_types[0] if len(pick_goods_types) == 1 else "",
                "requested_box_selection": BOX_SELECTION_PATTERN_MATCHING,
                "requested_box_count": int(plan.boxes_planned or 0),
                "requested_box_patterns": [pattern],
                "partial_pick_patterns": [
                    {
                        "source_box_qty": pattern.get("box_qty"),
                        "requested_box_count": int(plan.boxes_planned or 0),
                        "source_barcode_qty": pattern.get("barcode_qty") or {},
                        "barcode_qty": pick_pattern.get("barcode_qty") or {},
                        "pick_qty": _composition_box_qty(pick_composition),
                        "requested_article": pick_pattern.get("requested_article") or "",
                        "requested_goods_type": pick_pattern.get("requested_goods_type") or "",
                        "requested_barcodes": pick_pattern.get("requested_barcodes") or [],
                    }
                ],
                "ship_as_loose_units": True,
                "task_kind_label": "Частичный отбор с палеты для отгрузки",
            }
        )
        marking_barcodes = _marketplace_marking_barcodes_for_task(
            order=order,
            request_items=base_payload.get("request_items") or [],
            requested_barcode_qty=pick_barcode_qty,
        )
        if marking_barcodes:
            base_payload["requires_marking_scan"] = True
            base_payload["marking_required_barcodes"] = marking_barcodes
            base_payload["marking_scan_mode"] = "product_barcode_then_data_matrix"
    else:
        base_payload.update(
            {
                "pick_mode": "partial",
                "move_mode": MOVE_MODE_BOX_FULL,
                "requested_qty": int(plan.qty_planned or 0),
                "available_qty": int(plan.qty_planned or 0),
                "requested_box_selection": BOX_SELECTION_PATTERN_MATCHING,
                "requested_box_count": int(plan.boxes_planned or 0),
                "requested_box_patterns": requested_box_patterns_override or [pattern],
                "requested_boxes": [],
                "requested_box": "",
                "task_kind_label": "Короба с палеты для отгрузки",
            }
        )
    if bound_box_codes and plan.plan_type == OtgPalletPlan.TYPE_PARTIAL_BOX_SPLIT:
        # A partial pick must stay bound to the physical source box because the
        # operator changes that box's quantity. A whole-box route stays flexible:
        # the driver may scan another matching box that is physically on top.
        base_payload["requested_box_selection"] = BOX_SELECTION_FIXED
        base_payload["planned_box_codes"] = list(bound_box_codes)
        base_payload["selected_box_codes"] = list(bound_box_codes)
        base_payload["requested_boxes"] = list(bound_box_codes)
        base_payload["requested_box"] = bound_box_codes[0] if len(bound_box_codes) == 1 else ""
    base_payload["instruction"] = _move_instruction(base_payload)
    _validate_otg_move_payload_quantities(base_payload)
    return base_payload


def _group_plans_for_move_tasks(plans: list[OtgPalletPlan]) -> list[list[OtgPalletPlan]]:
    groups: list[list[OtgPalletPlan]] = []
    grouped_pick_plans: dict[tuple[str, str], list[OtgPalletPlan]] = {}
    for plan in plans:
        pallet_code = _normalize_text(plan.pallet_code)
        if plan.plan_type != OtgPalletPlan.TYPE_PICK_BOXES or not pallet_code:
            groups.append([plan])
            continue
        location_key = json.dumps(plan.from_location or {}, ensure_ascii=False, sort_keys=True, default=str)
        key = (pallet_code.lower(), location_key)
        group = grouped_pick_plans.get(key)
        if group is None:
            group = []
            grouped_pick_plans[key] = group
            groups.append(group)
        group.append(plan)
    return groups


def _fixed_partial_box_codes_by_pallet(plans: list[OtgPalletPlan]) -> dict[str, list[str]]:
    """Keep exact partial-pick sources out of flexible whole-box scans."""

    result: dict[str, list[str]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)
    for plan in plans:
        if plan.plan_type != OtgPalletPlan.TYPE_PARTIAL_BOX_SPLIT:
            continue
        pallet_key = _normalize_text(plan.pallet_code).lower()
        if not pallet_key:
            continue
        explicit_source_keys = {
            _normalize_text(code).lower()
            for code in (plan.demand.source_box_codes or [])
            if _normalize_text(code)
        }
        if dict(plan.payload or {}).get("bind_planned_partial_box"):
            explicit_source_keys.update(
                _normalize_text(code).lower()
                for code in (plan.planned_box_codes or [])
                if _normalize_text(code)
            )
        for raw_code in plan.planned_box_codes or []:
            code = _normalize_text(raw_code)
            code_key = code.lower()
            if (
                not code
                or code_key not in explicit_source_keys
                or code_key in seen[pallet_key]
            ):
                continue
            seen[pallet_key].add(code_key)
            result[pallet_key].append(code)
    return dict(result)


def _merge_pick_task_payloads(payloads: list[dict]) -> dict:
    merged = deepcopy(payloads[0])
    if len(payloads) == 1:
        _validate_otg_move_payload_quantities(merged)
        return merged

    patterns = _merge_requested_box_patterns(
        [
            pattern
            for payload in payloads
            for pattern in (payload.get("requested_box_patterns") or [])
            if isinstance(pattern, dict)
        ]
    )
    requested_qty = sum(max(_as_int(payload.get("requested_qty")), 0) for payload in payloads)
    box_count = sum(max(_as_int(payload.get("requested_box_count")), 0) for payload in payloads)
    selection_modes = {_requested_box_selection_mode(payload) for payload in payloads}
    fixed_box_codes: list[str] = []
    fixed_box_keys: set[str] = set()
    if BOX_SELECTION_FIXED in selection_modes:
        if selection_modes != {BOX_SELECTION_FIXED}:
            raise ValidationError(
                "Внутренняя ошибка OTG-плана: точный и свободный отбор коробов "
                "нельзя объединить в одно задание. Складские данные не изменены."
            )
        for payload in payloads:
            for code in _payload_box_codes(payload):
                code_key = code.lower()
                if code_key in fixed_box_keys:
                    continue
                fixed_box_keys.add(code_key)
                fixed_box_codes.append(code)

    request_items: dict[int, int] = defaultdict(int)
    for payload in payloads:
        for item in payload.get("request_items") or []:
            if not isinstance(item, dict):
                continue
            item_id = _as_int(item.get("shipping_item_id"))
            item_qty = max(_as_int(item.get("requested_qty")), 0)
            if item_id > 0 and item_qty > 0:
                request_items[item_id] += item_qty

    compositions = [
        dict(row)
        for payload in payloads
        for row in (payload.get("otg_box_composition") or [])
        if isinstance(row, dict)
    ]
    article_values = {
        _normalize_text(value)
        for payload in payloads
        for value in [payload.get("requested_sku")]
        if _normalize_text(value)
    }
    goods_type_values = {
        _normalize_goods_type(value)
        for payload in payloads
        for value in [payload.get("requested_goods_type")]
        if _normalize_goods_type(value)
    }
    for pattern in patterns:
        article = _normalize_text(pattern.get("requested_article"))
        goods_type = _normalize_goods_type(pattern.get("requested_goods_type"))
        if article:
            article_values.add(article)
        if goods_type:
            goods_type_values.add(goods_type)

    route_plan = dict(merged.get("route_plan") or {})
    route_plan["boxes_to_pick"] = box_count
    route_plan["qty_to_pick"] = requested_qty
    requested_barcode_qty = _barcode_qty_for_patterns(patterns)
    merged.update(
        {
            "requested_sku": sorted(article_values)[0] if len(article_values) == 1 else "",
            "requested_goods_type": sorted(goods_type_values)[0] if len(goods_type_values) == 1 else "",
            "requested_barcodes": sorted(requested_barcode_qty.keys()),
            "requested_barcode_qty": requested_barcode_qty,
            "requested_qty": requested_qty,
            "available_qty": requested_qty,
            "requested_box_count": box_count,
            "requested_box_patterns": patterns,
            "request_items": [
                {"shipping_item_id": item_id, "requested_qty": request_items[item_id]}
                for item_id in sorted(request_items)
            ],
            "otg_box_composition": compositions,
            "otg_boxes_planned": box_count,
            "route_plan": route_plan,
            "planned_box_codes": [],
            "selected_box_codes": [],
            "reserved_box_codes": [],
        }
    )
    if selection_modes == {BOX_SELECTION_FIXED}:
        merged.update(
            {
                "requested_box_selection": BOX_SELECTION_FIXED,
                "requested_boxes": list(fixed_box_codes),
                "requested_box": fixed_box_codes[0] if len(fixed_box_codes) == 1 else "",
                "planned_box_codes": list(fixed_box_codes),
                "selected_box_codes": list(fixed_box_codes),
            }
        )
    else:
        merged.update(
            {
                "requested_box_selection": BOX_SELECTION_PATTERN_MATCHING,
                "requested_boxes": [],
                "requested_box": "",
            }
        )
    merged["instruction"] = _move_instruction(merged)
    _validate_otg_move_payload_quantities(merged)
    return merged


def _demand_payload_from_model(demand: OtgDeliveryDemand) -> dict:
    return {
        "demand_key": demand.demand_key,
        "demand_type": demand.demand_type,
        "boxes_required": int(demand.boxes_required or 0),
        "box_qty": int(demand.box_qty or 0),
        "composition": deepcopy(list(demand.composition or [])),
        "pick_composition": deepcopy(list(demand.pick_composition or [])),
        "item_quantities_per_box": deepcopy(dict(demand.item_quantities_per_box or {})),
        "source_box_codes": [
            _normalize_text(code)
            for code in (demand.source_box_codes or [])
            if _normalize_text(code)
        ],
        "payload": deepcopy(dict(demand.payload or {})),
    }


def _assignment_editor_for_request(
    *,
    order: ShippingOrder,
    otg_request: OtgDeliveryRequest,
) -> dict:
    plans = list(
        OtgPalletPlan.objects.select_related("demand", "move_task")
        .filter(request=otg_request, move_task__isnull=False)
        .order_by("move_task_id", "id")
    )
    task_by_id = {
        int(plan.move_task_id): plan.move_task
        for plan in plans
        if plan.move_task_id and plan.move_task is not None
    }
    tasks = list(task_by_id.values())
    has_started_task = any(
        task.status != MoveTask.STATUS_CREATED
        or int(task.qty_done or 0) > 0
        or task.started_at is not None
        for task in tasks
    )
    has_claims = any(
        task.box_claims.filter(status="claimed").exists()
        for task in tasks
    )
    request_is_active = otg_request.status in _ACTIVE_OTG_STATUSES
    order_is_editable = order.status == ShippingOrder.STATUS_PICKING
    can_replace = bool(
        plans
        and tasks
        and request_is_active
        and order_is_editable
        and not has_started_task
        and not has_claims
    )
    blocked_reason = ""
    if not order_is_editable:
        blocked_reason = "Заменять короба можно только пока заявка находится в отборе."
    elif not request_is_active:
        blocked_reason = "Это задание уже не является активным."
    elif has_started_task or has_claims:
        blocked_reason = "Ричтракер уже начал выполнение. Состав задания менять нельзя."

    demands = {int(plan.demand_id): plan.demand for plan in plans if plan.demand_id}
    assigned_codes = {
        _normalize_text(code).lower()
        for plan in plans
        for code in (plan.planned_box_codes or [])
        if _normalize_text(code)
    }

    alternatives_by_demand_id: dict[int, list[dict]] = defaultdict(list)
    if can_replace:
        demand_payloads = [_demand_payload_from_model(demand) for demand in demands.values()]
        blocked_pallets, blocked_box_keys = _active_otg_source_guards(
            order.agency_id,
            exclude_move_request_ids={int(otg_request.move_request_id or 0)},
        )
        catalog = _pallet_catalog(
            _base_rows_for_order(order, demand_payloads),
            agency_id=order.agency_id,
            blocked_pallets=blocked_pallets,
            blocked_box_keys=blocked_box_keys,
        )
        for demand_id, demand in demands.items():
            if demand.demand_type == OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT:
                continue
            for pallet_code, pallet in catalog.items():
                for box in pallet.get("available_boxes") or []:
                    box_code = _normalize_text(box.get("code"))
                    if not box_code or box_code.lower() in assigned_codes:
                        continue
                    if not _box_matches_composition(box, list(demand.composition or [])):
                        continue
                    alternatives_by_demand_id[demand_id].append(
                        {
                            "code": box_code,
                            "pallet_code": pallet_code,
                            "source_place": _location_label(pallet.get("from_location") or {}),
                        }
                    )
            alternatives_by_demand_id[demand_id].sort(
                key=lambda row: (row["pallet_code"].casefold(), row["code"].casefold())
            )

    task_rows: list[dict] = []
    replaceable_box_count = 0
    for task_id, task in sorted(task_by_id.items()):
        task_plans = [plan for plan in plans if int(plan.move_task_id or 0) == task_id]
        box_rows: list[dict] = []
        for plan in task_plans:
            demand = plan.demand
            row_replaceable = can_replace and demand.demand_type != OtgDeliveryDemand.TYPE_PARTIAL_BOX_SPLIT
            for current_code in [
                _normalize_text(code)
                for code in (plan.planned_box_codes or [])
                if _normalize_text(code)
            ]:
                options = [
                    {
                        "code": current_code,
                        "pallet_code": _normalize_text(plan.pallet_code),
                        "source_place": _location_label(plan.from_location or {}),
                        "current": True,
                    },
                    *[
                        {**option, "current": False}
                        for option in alternatives_by_demand_id.get(int(demand.id), [])
                    ],
                ]
                box_rows.append(
                    {
                        "current_code": current_code,
                        "demand_id": int(demand.id),
                        "replaceable": row_replaceable,
                        "options": options,
                    }
                )
                if row_replaceable:
                    replaceable_box_count += 1
        task_rows.append(
            {
                "task_id": task_id,
                "legacy_order_id": _normalize_text(task.legacy_order_id) or f"#{task_id}",
                "status": task.get_status_display(),
                "status_code": task.status,
                "source_pallet": _normalize_text(task.pallet_code) or "-",
                "source_place": _location_label(
                    {
                        "zone": task.from_zone,
                        "row": task.from_row,
                        "section": task.from_section,
                        "tier": task.from_tier,
                        "cell": task.from_cell,
                    }
                ),
                "boxes": box_rows,
            }
        )

    return {
        "has_assignment": bool(plans),
        "request_id": int(otg_request.id),
        "can_replace": can_replace and replaceable_box_count > 0,
        "blocked_reason": blocked_reason,
        "replaceable_box_count": replaceable_box_count,
        "tasks": task_rows,
    }


def get_otg_reachtruck_assignment_editor(order: ShippingOrder) -> dict:
    otg_request = (
        OtgDeliveryRequest.objects.filter(
            shipping_order=order,
            status__in=_ACTIVE_OTG_STATUSES,
            move_request__isnull=False,
            pallet_plans__move_task__status__in=[
                MoveTask.STATUS_CREATED,
                MoveTask.STATUS_IN_PROGRESS,
            ],
        )
        .distinct()
        .order_by("-created_at", "-id")
        .first()
    )
    if otg_request is None:
        return {
            "has_assignment": False,
            "request_id": 0,
            "can_replace": False,
            "blocked_reason": "Заданий ричтраку пока нет.",
            "replaceable_box_count": 0,
            "tasks": [],
        }
    return _assignment_editor_for_request(order=order, otg_request=otg_request)


def _comment_with_source_box_codes(comment: str, box_codes: list[str]) -> str:
    value = str(comment or "").strip()
    replacement = "короба: " + ", ".join(box_codes)
    if _BOX_CODES_RE.search(value):
        value = _BOX_CODES_RE.sub(replacement, value, count=1)
    else:
        value = f"{value.rstrip('; ')}; {replacement}" if value else replacement
    split_meta = extract_partial_box_split(value)
    if str(split_meta.get("kind") or "").strip() == "partial_box_split":
        split_meta["source_box_codes"] = list(box_codes)
        split_meta["source_boxes"] = len(box_codes)
        encoded = encode_partial_box_split(split_meta)
        if _PARTIAL_BOX_SPLIT_TOKEN_RE.search(value):
            value = _PARTIAL_BOX_SPLIT_TOKEN_RE.sub(encoded, value, count=1)
    return value


def _split_payload_line(line: dict, *, qty: int | None = None) -> dict:
    return {
        "row_key": _normalize_text(line.get("row_key")),
        "sku_code": _normalize_text(line.get("sku") or line.get("sku_code")),
        "name": _normalize_text(line.get("name")),
        "size": _normalize_text(line.get("size")),
        "barcode": _normalize_text(line.get("barcode")),
        "goods_type": _normalize_text(line.get("goods_type")),
        "qty": max(
            _as_int(line.get("qty_per_box") if qty is None else qty),
            0,
        ),
    }


def _comment_with_rebound_partial_source(
    comment: str,
    *,
    box_code: str,
    group_key: str,
    source_composition: list[dict],
    pick_composition: list[dict],
    item_pick_qty: int,
    item_source_qty: int,
) -> str:
    value = _comment_with_source_box_codes(comment, [box_code])
    value = _PARTIAL_BOX_SPLIT_TOKEN_RE.sub("", value).strip().rstrip(";")
    source_pattern = [_split_payload_line(line) for line in source_composition]
    pick_pattern = [_split_payload_line(line) for line in pick_composition]
    payload = {
        "version": 1,
        "kind": "partial_box_split",
        "group_key": group_key,
        "row_key": group_key,
        "source_group": "",
        "source_boxes": 1,
        "source_box_total_qty": sum(max(_as_int(line.get("qty_per_box")), 0) for line in source_composition),
        "source_box_pattern": source_pattern,
        "source_box_codes": [box_code],
        "pick_pattern": pick_pattern,
        "item_pick_qty": max(_as_int(item_pick_qty), 0),
        "item_source_qty": max(_as_int(item_source_qty), 0),
        "is_mixed_box": len(source_pattern) > 1,
        "reason": "rebound_shared_source_box",
    }
    encoded = encode_partial_box_split(payload)
    return (
        f"{value}; Разбить короб: 1; отбор из короба: "
        f"{payload['item_pick_qty']} из {payload['item_source_qty']}; {encoded}"
    )


def _urgent_shipping_order_ids(orders: list[ShippingOrder]) -> set[int]:
    route_to_order_id = {
        f"/shipping/{int(order.pk)}/": int(order.pk)
        for order in orders
        if int(order.pk or 0) > 0
    }
    if not route_to_order_id:
        return set()
    urgent_routes = Task.objects.filter(
        route__in=sorted(route_to_order_id),
        assigned_to__role="storekeeper",
        title__startswith="Заявка на отгрузку №",
        priority="urgent",
        status__in={"backlog", "in_progress", "blocked"},
    ).values_list("route", flat=True)
    return {
        route_to_order_id[route]
        for route in urgent_routes
        if route in route_to_order_id
    }


def _shipping_acceptance_times(orders: list[ShippingOrder]) -> dict[int, Any]:
    """Read acceptance FIFO in one query; old orders fall back to creation time."""
    by_identity = {(order.agency_id, order.number): order.pk for order in orders}
    result = {order.pk: order.created_at for order in orders}
    if not by_identity:
        return result
    entries = (
        OrderAuditEntry.objects.filter(
            order_type="shipping",
            order_id__in=[order.number for order in orders],
            agency_id__in={order.agency_id for order in orders},
            action="status",
            description="Кладовщик принял заявку в работу",
        )
        .values("agency_id", "order_id")
        .annotate(accepted_at=Max("created_at"))
    )
    for entry in entries:
        order_id = by_identity.get((entry["agency_id"], entry["order_id"]))
        if order_id is not None:
            result[order_id] = entry["accepted_at"]
    return result


def _shipping_queue_priority_key(
    order: ShippingOrder,
    *,
    urgent_order_ids: set[int] | None = None,
    acceptance_times: dict | None = None,
) -> tuple:
    if urgent_order_ids is None:
        urgent_order_ids = _urgent_shipping_order_ids([order])
    if acceptance_times is None:
        acceptance_times = _shipping_acceptance_times([order])
    return (
        int(order.pk or 0) not in urgent_order_ids,
        acceptance_times.get(order.pk, order.created_at),
        int(order.pk or 0),
    )


def _shipping_order_source_box_codes(order: ShippingOrder) -> list[str]:
    source_codes: list[str] = []
    seen_keys: set[str] = set()
    for item in order.items.all():
        comment = str(item.comment or "")
        split_meta = extract_partial_box_split(comment)
        item_codes = [
            *_parse_box_codes(comment),
            *(split_meta.get("source_box_codes") or []),
        ]
        for code in item_codes:
            normalized_code = _normalize_text(code)
            code_key = normalized_code.lower()
            if normalized_code and code_key not in seen_keys:
                seen_keys.add(code_key)
                source_codes.append(normalized_code)
    return source_codes


def _source_codes_temporarily_blocked(
    order: ShippingOrder,
    source_codes: list[str],
    *,
    blocked_pallets: set[str] | None = None,
    blocked_box_keys: set[str] | None = None,
) -> bool:
    normalized_codes = [
        _normalize_text(code)
        for code in source_codes
        if _normalize_text(code)
    ]
    if not normalized_codes:
        return False
    if blocked_pallets is None or blocked_box_keys is None:
        blocked_pallets, blocked_box_keys = _active_otg_source_guards(order.agency_id)
    current_keys = {code.lower() for code in normalized_codes}
    if current_keys & blocked_box_keys:
        return True
    if not blocked_pallets:
        return False
    return WarehouseStockSnapshot.objects.filter(
        agency=order.agency,
        container_code__in=normalized_codes,
        parent_container__container_code__in=blocked_pallets,
        is_archived=False,
        qty__gt=0,
    ).exists()


def shipping_active_source_wait_reason(order: ShippingOrder) -> str:
    """Explain when selected shipping sources are hidden by active warehouse work."""

    source_codes = _shipping_order_source_box_codes(order)
    if _source_codes_temporarily_blocked(order, source_codes):
        return _ACTIVE_SOURCE_WAIT_MESSAGE
    return ""


def _queue_priority_shipping_orders(order: ShippingOrder) -> list[ShippingOrder]:
    other_orders = list(
        ShippingOrder.objects.filter(
            agency=order.agency,
            status=ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
        )
        .exclude(pk=order.pk)
        .prefetch_related("items")
        .order_by("planned_ship_date", "id")
    )
    urgent_order_ids = _urgent_shipping_order_ids([order, *other_orders])
    acceptance_times = _shipping_acceptance_times([order, *other_orders])
    current_priority = _shipping_queue_priority_key(
        order,
        urgent_order_ids=urgent_order_ids,
        acceptance_times=acceptance_times,
    )
    return [
        other
        for other in other_orders
        if _shipping_queue_priority_key(
            other,
            urgent_order_ids=urgent_order_ids,
            acceptance_times=acceptance_times,
        )
        < current_priority
    ]


def _higher_priority_shipping_box_claims(order: ShippingOrder) -> dict[str, str]:
    claims: dict[str, str] = {}
    unavailable_claim_keys = {
        _normalize_text(code).lower()
        for code in unavailable_box_claim_codes(agency_id=order.agency_id)
        if _normalize_text(code)
    }
    active_orders = list(
        ShippingOrder.objects.filter(agency=order.agency, status__in=_SOURCE_BOX_HOLD_STATUSES)
        .prefetch_related("items")
        .order_by("id")
    )
    urgent_order_ids = _urgent_shipping_order_ids([order, *active_orders])
    acceptance_times = _shipping_acceptance_times([order, *active_orders])
    current_priority = _shipping_queue_priority_key(
        order,
        urgent_order_ids=urgent_order_ids,
        acceptance_times=acceptance_times,
    )
    for other in active_orders:
        if int(other.pk or 0) == int(order.pk or 0):
            continue
        # An order with dispatched warehouse work always keeps its boxes. Before
        # dispatch, the earlier order wins deterministically and the later order
        # is rebound to equivalent free stock.
        has_priority = other.status in {
            ShippingOrder.STATUS_PICKING,
            ShippingOrder.STATUS_PACKED,
        } or _shipping_queue_priority_key(
            other,
            urgent_order_ids=urgent_order_ids,
            acceptance_times=acceptance_times,
        ) < current_priority
        if not has_priority:
            continue
        box_codes: list[str] = []
        if other.status in {
            ShippingOrder.STATUS_PICKING,
            ShippingOrder.STATUS_PACKED,
        }:
            task_ids = {
                int(task_id)
                for task_id in OtgPalletPlan.objects.filter(
                    request__shipping_order=other,
                    move_task_id__isnull=False,
                ).values_list("move_task_id", flat=True)
                if int(task_id or 0) > 0
            }
            actual_claim_codes = [
                code
                for code in BoxClaim.objects.filter(
                    Q(shipping_order_id=other.number) | Q(move_task_id__in=task_ids),
                    status__in=[BoxClaim.STATUS_CLAIMED, BoxClaim.STATUS_DELIVERED],
                )
                .order_by("id")
                .values_list("box_code", flat=True)
                if _normalize_text(code)
            ]
            actual_codes = [
                code
                for code in actual_claim_codes
                if _normalize_text(code).lower() in unavailable_claim_keys
            ]
            box_codes.extend(actual_codes)
            if other.status == ShippingOrder.STATUS_PICKING:
                for planned_codes in OtgPalletPlan.objects.filter(
                    request__shipping_order=other,
                    move_task__status__in=[MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS],
                ).values_list("planned_box_codes", flat=True):
                    box_codes.extend(planned_codes or [])
                if not box_codes and not actual_claim_codes:
                    box_codes.extend(
                        code
                        for item in other.items.all()
                        for code in _parse_box_codes(str(item.comment or ""))
                    )
        else:
            box_codes.extend(
                code
                for item in other.items.all()
                for code in _parse_box_codes(str(item.comment or ""))
            )
        for code in box_codes:
            code_key = _normalize_text(code).lower()
            if code_key:
                claims.setdefault(code_key, other.number)
    return claims


def _higher_priority_shipping_pallet_claims(order: ShippingOrder) -> dict[str, set[str]]:
    box_owners: dict[str, set[str]] = defaultdict(set)
    source_codes: set[str] = set()
    for other in _queue_priority_shipping_orders(order):
        for code in _shipping_order_source_box_codes(other):
            normalized_code = _normalize_text(code)
            code_key = normalized_code.lower()
            if code_key:
                source_codes.add(normalized_code)
                box_owners[code_key].add(_display_otg_order_number(other.number))
    if not box_owners:
        return {}

    pallet_owners: dict[str, set[str]] = defaultdict(set)
    rows = (
        WarehouseStockSnapshot.objects.filter(
            agency=order.agency,
            is_archived=False,
            container_code__in=sorted(source_codes),
            qty__gt=0,
        )
        .exclude(parent_container__container_code="")
        .values_list("container_code", "parent_container__container_code")
    )
    for box_code, pallet_code in rows:
        code_key = _normalize_text(box_code).lower()
        normalized_pallet = _normalize_text(pallet_code)
        if normalized_pallet and code_key in box_owners:
            pallet_owners[normalized_pallet].update(box_owners[code_key])
    return dict(pallet_owners)


def higher_priority_shipping_pallet_blockers(order: ShippingOrder) -> list[str]:
    """Return older/earlier shipments that own one of this order's source pallets."""

    current_codes = _shipping_order_source_box_codes(order)
    if not current_codes:
        return []
    current_pallets = {
        _normalize_text(pallet_code)
        for pallet_code in WarehouseStockSnapshot.objects.filter(
            agency=order.agency,
            is_archived=False,
            container_code__in=current_codes,
            qty__gt=0,
        )
        .exclude(parent_container__container_code="")
        .values_list("parent_container__container_code", flat=True)
        if _normalize_text(pallet_code)
    }
    if not current_pallets:
        return []
    priority_claims = _higher_priority_shipping_pallet_claims(order)
    return sorted(
        {
            number
            for pallet_code in current_pallets
            for number in priority_claims.get(pallet_code, set())
            if _normalize_text(number)
        }
    )


def shipping_queue_priority_wait_reason(order: ShippingOrder) -> str:
    blockers = higher_priority_shipping_pallet_blockers(order)
    if len(blockers) == 1:
        return (
            f"Паллета закреплена за более приоритетной заявкой {blockers[0]}, "
            "дождитесь её запуска или завершения."
        )
    if blockers:
        return (
            "Паллеты закреплены за более приоритетными заявками "
            f"{', '.join(blockers)}, дождитесь их запуска или завершения."
        )
    return ""


@transaction.atomic
def rebind_unavailable_shipping_source_boxes(order: ShippingOrder) -> list[dict]:
    """Replace stale regular-box selections before an OTG task is dispatched.

    The selected Fullbox box codes live in item comments, while quantity reserves
    are SKU-level. The caller serializes dispatch with an agency lock; the earlier
    order keeps a shared code and the later order receives an exact equivalent
    that is persisted back to its item before task creation.
    """

    order = ShippingOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != ShippingOrder.STATUS_STOREKEEPER_ACCEPTED:
        return []

    priority_claims = _higher_priority_shipping_box_claims(order)
    items = list(
        ShippingOrderItem.objects.select_for_update()
        .filter(order=order)
        .order_by("id")
    )
    if not any(
        _parse_box_codes(str(item.comment or ""))
        or extract_partial_box_split(str(item.comment or "")).get("source_box_codes")
        for item in items
    ):
        return []
    demand_payloads = build_box_demands(order, items=items)
    base_rows = _base_rows_for_order(order, demand_payloads)
    blocked_pallets, blocked_box_keys = _active_otg_source_guards(order.agency_id)
    catalog = _pallet_catalog(
        base_rows,
        agency_id=order.agency_id,
        blocked_pallets=blocked_pallets,
        blocked_box_keys=blocked_box_keys,
    )

    candidates_by_code: dict[str, dict] = {}
    candidate_rows: list[dict] = []
    for pallet_code in sorted(catalog):
        pallet = catalog[pallet_code]
        for box in sorted(
            pallet.get("available_boxes") or [],
            key=lambda row: _normalize_text(row.get("code")).lower(),
        ):
            code = _normalize_text(box.get("code"))
            code_key = code.lower()
            if not code or code_key in blocked_box_keys or code_key in candidates_by_code:
                continue
            candidate = {"code": code, "key": code_key, "pallet_code": pallet_code, "box": box}
            candidates_by_code[code_key] = candidate
            candidate_rows.append(candidate)

    specs: list[dict] = []
    protected_owner: dict[str, int] = {}
    for item in items:
        comment = str(item.comment or "")
        split_meta = extract_partial_box_split(comment)
        if str(split_meta.get("kind") or "").strip() == "partial_box_split" or "микс-короб" in comment.lower():
            continue
        box_count, box_qty = _shipping_item_box_numbers(item)
        current_codes = _parse_box_codes(comment)
        if box_count <= 0 or box_qty <= 0 or len(current_codes) != box_count:
            continue
        composition = [_line_from_item(item, qty_per_box=box_qty)]
        matching = [
            candidate
            for candidate in candidate_rows
            if candidate["key"] not in priority_claims
            and _box_matches_composition(candidate["box"], composition)
        ]
        matching_keys = {candidate["key"] for candidate in matching}
        specs.append(
            {
                "item": item,
                "current_codes": current_codes,
                "box_count": box_count,
                "box_qty": box_qty,
                "composition": composition,
                "matching": matching,
                "matching_keys": matching_keys,
            }
        )
        for code in current_codes:
            code_key = _normalize_text(code).lower()
            if code_key in matching_keys and code_key not in priority_claims:
                protected_owner.setdefault(code_key, int(item.id))

    used_keys: set[str] = set()
    selected_by_item_id: dict[int, list[str]] = {}
    partial_rebind_by_item_id: dict[int, dict] = {}

    # A marketplace supply may split one SKU across several order rows. If an
    # earlier shipment consumes the small exact box from one row, the remaining
    # rows may still point at one larger physical box that contains enough units
    # for the combined request. Treat that box as one shared partial-pick source
    # instead of requiring a separate exact-composition box for every row.
    specs_by_identity: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
    for spec in specs:
        composition = list(spec.get("composition") or [])
        if len(composition) == 1:
            specs_by_identity[_demand_line_identity(composition[0])].append(spec)

    partial_anchors_by_identity: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
    client_quantity_mode = False
    for item in items:
        split_meta = extract_partial_box_split(str(item.comment or ""))
        if str(split_meta.get("kind") or "").strip() != "partial_box_split":
            continue
        split_group_key = _normalize_text(
            split_meta.get("group_key") or split_meta.get("row_key")
        ).lower()
        if split_group_key.startswith("client-quantity"):
            client_quantity_mode = True
        source_codes = [
            _normalize_text(code)
            for code in split_meta.get("source_box_codes") or []
            if _normalize_text(code)
        ]
        item_pick_qty = max(_as_int(split_meta.get("item_pick_qty")), 0)
        if len(source_codes) != 1 or item_pick_qty <= 0:
            continue
        line = _line_from_item(item, qty_per_box=item_pick_qty)
        partial_anchors_by_identity[_demand_line_identity(line)].append(
            {
                "item": item,
                "source_code": source_codes[0],
                "source_key": source_codes[0].lower(),
                "item_pick_qty": item_pick_qty,
            }
        )

    quantity_replanned_identities: set[tuple[str, str, str, str]] = set()
    if client_quantity_mode:
        # A quantity-mode shipment is allowed to take units from another free
        # physical box of the same SKU. Preserve complete exact-box selections
        # first; only rows that no longer have enough exact boxes are converted
        # to a one-box partial pick. Partial needs are assigned from largest to
        # smallest so a small remainder cannot consume the only large source.
        for identity, group in specs_by_identity.items():
            anchors = partial_anchors_by_identity.get(identity) or []
            group_item_ids = {
                int(spec["item"].id) for spec in group
            } | {
                int(anchor["item"].id) for anchor in anchors
            }
            group_used = set(used_keys)
            group_selected: dict[int, list[str]] = {}
            unresolved: list[dict] = []

            for spec in sorted(
                group,
                key=lambda row: (
                    -max(_as_int(row.get("box_count")), 0) * max(_as_int(row.get("box_qty")), 0),
                    int(row["item"].id),
                ),
            ):
                item_id = int(spec["item"].id)
                selected: list[str] = []
                selected_keys: set[str] = set()
                current_keys = {
                    _normalize_text(code).lower()
                    for code in spec.get("current_codes") or []
                    if _normalize_text(code)
                }
                exact_candidates = sorted(
                    spec.get("matching") or [],
                    key=lambda candidate: (
                        candidate["key"] not in current_keys,
                        candidate["key"],
                    ),
                )
                for candidate in exact_candidates:
                    if len(selected) >= int(spec["box_count"]):
                        break
                    code_key = candidate["key"]
                    owner_id = protected_owner.get(code_key)
                    if (
                        code_key in priority_claims
                        or code_key in group_used
                        or code_key in selected_keys
                        or (owner_id is not None and owner_id not in group_item_ids)
                    ):
                        continue
                    selected.append(candidate["code"])
                    selected_keys.add(code_key)
                if len(selected) == int(spec["box_count"]):
                    group_selected[item_id] = selected
                    group_used.update(selected_keys)
                else:
                    unresolved.append(
                        {
                            "item": spec["item"],
                            "pick_qty": max(_as_int(spec.get("box_count")), 0)
                            * max(_as_int(spec.get("box_qty")), 0),
                            "current_keys": current_keys,
                        }
                    )

            if not unresolved:
                selected_by_item_id.update(group_selected)
                used_keys.update(group_used - used_keys)
                quantity_replanned_identities.add(identity)
                continue

            partial_needs = [
                *unresolved,
                *[
                    {
                        "item": anchor["item"],
                        "pick_qty": max(_as_int(anchor.get("item_pick_qty")), 0),
                        "current_keys": {anchor["source_key"]},
                    }
                    for anchor in anchors
                ],
            ]
            group_partial: dict[int, dict] = {}
            allocation_failed = False
            for need in sorted(
                partial_needs,
                key=lambda row: (-max(_as_int(row.get("pick_qty")), 0), int(row["item"].id)),
            ):
                item = need["item"]
                item_id = int(item.id)
                pick_qty = max(_as_int(need.get("pick_qty")), 0)
                requested_line = _line_from_item(item, qty_per_box=pick_qty)
                replacements: list[dict] = []
                for candidate in candidate_rows:
                    code_key = candidate["key"]
                    owner_id = protected_owner.get(code_key)
                    if (
                        code_key in priority_claims
                        or code_key in group_used
                        or (owner_id is not None and owner_id not in group_item_ids)
                        or not _box_contains_pick_composition(candidate["box"], [requested_line])
                    ):
                        continue
                    replacements.append(candidate)
                if not replacements:
                    allocation_failed = True
                    break
                replacements.sort(
                    key=lambda candidate: (
                        sum(
                            max(_as_int(line.get("qty_per_box")), 0)
                            for line in _box_composition(candidate["box"])
                            if _demand_line_identity(line) == identity
                        ),
                        candidate["key"] not in need.get("current_keys", set()),
                        candidate["key"],
                    )
                )
                candidate = replacements[0]
                source_composition = _box_composition(candidate["box"])
                item_source_qty = sum(
                    max(_as_int(line.get("qty_per_box")), 0)
                    for line in source_composition
                    if _demand_line_identity(line) == identity
                )
                group_used.add(candidate["key"])
                group_selected[item_id] = [candidate["code"]]
                group_partial[item_id] = {
                    "box_code": candidate["code"],
                    "group_key": (
                        f"client-quantity-rebind:{int(order.pk)}:{candidate['key']}"
                    ),
                    "source_composition": source_composition,
                    "pick_composition": [requested_line],
                    "item_pick_qty": pick_qty,
                    "item_source_qty": item_source_qty,
                }

            if allocation_failed:
                continue
            selected_by_item_id.update(group_selected)
            partial_rebind_by_item_id.update(group_partial)
            used_keys.update(group_used - used_keys)
            quantity_replanned_identities.add(identity)

    for identity, group in specs_by_identity.items():
        if identity in quantity_replanned_identities:
            continue
        anchors = partial_anchors_by_identity.get(identity) or []
        if (
            (len(group) <= 1 and not anchors)
            or any(int(spec.get("box_count") or 0) != 1 for spec in group)
        ):
            continue
        current_keys = {
            _normalize_text(code).lower()
            for spec in group
            for code in spec.get("current_codes") or []
            if _normalize_text(code)
        }
        current_keys.update(anchor["source_key"] for anchor in anchors)
        if not any(
            _normalize_text(code).lower() not in spec.get("matching_keys", set())
            or _normalize_text(code).lower() in priority_claims
            for spec in group
            for code in spec.get("current_codes") or []
            if _normalize_text(code)
        ):
            continue

        regular_pick_qty = sum(max(_as_int(spec.get("box_qty")), 0) for spec in group)
        if regular_pick_qty <= 0:
            continue
        group_item_ids = {int(spec["item"].id) for spec in group}
        shared_candidates = []
        for candidate in candidate_rows:
            code_key = candidate["key"]
            owner_id = protected_owner.get(code_key)
            anchor_pick_qty = sum(
                max(_as_int(anchor.get("item_pick_qty")), 0)
                for anchor in anchors
                if anchor.get("source_key") == code_key
            )
            if anchors and anchor_pick_qty <= 0:
                continue
            total_source_pick_qty = regular_pick_qty + anchor_pick_qty
            requested_line = {
                **group[0]["composition"][0],
                "qty_per_box": total_source_pick_qty,
            }
            if (
                code_key in priority_claims
                or code_key in used_keys
                or (owner_id is not None and owner_id not in group_item_ids)
                or not _box_contains_pick_composition(candidate["box"], [requested_line])
            ):
                continue
            shared_candidates.append((candidate, total_source_pick_qty))
        if not shared_candidates:
            continue
        shared_candidates.sort(
            key=lambda row: (
                row[0]["key"] not in current_keys,
                row[0]["key"],
            )
        )
        candidate, total_source_pick_qty = shared_candidates[0]
        requested_line = {
            **group[0]["composition"][0],
            "qty_per_box": total_source_pick_qty,
        }
        source_composition = _box_composition(candidate["box"])
        item_source_qty = sum(
            max(_as_int(line.get("qty_per_box")), 0)
            for line in source_composition
            if _demand_line_identity(line) == _demand_line_identity(requested_line)
        )
        if item_source_qty < total_source_pick_qty:
            continue
        group_key = f"rebind-partial:{int(order.pk)}:{candidate['key']}"
        pick_composition = [{**requested_line, "qty_per_box": regular_pick_qty}]
        used_keys.add(candidate["key"])
        for spec in group:
            item_id = int(spec["item"].id)
            selected_by_item_id[item_id] = [candidate["code"]]
            partial_rebind_by_item_id[item_id] = {
                "box_code": candidate["code"],
                "group_key": group_key,
                "source_composition": source_composition,
                "pick_composition": pick_composition,
                "item_pick_qty": max(_as_int(spec.get("box_qty")), 0),
                "item_source_qty": item_source_qty,
            }

    # A physical source selected for a partial pick is already occupied by that
    # demand.  Keep it out of the ordinary full-box pool unless the guarded
    # branch above deliberately merged a one-box regular row into the same
    # partial source.  Otherwise the planner can count the source contents once
    # as a full box and a second time as a partial pick (for example 35 + 30).
    used_keys.update(
        anchor["source_key"]
        for anchors in partial_anchors_by_identity.values()
        for anchor in anchors
        if anchor.get("source_key")
        and int(anchor["item"].id) not in partial_rebind_by_item_id
    )

    anchors_by_source: dict[str, list[dict]] = defaultdict(list)
    for anchors in partial_anchors_by_identity.values():
        for anchor in anchors:
            anchors_by_source[anchor["source_key"]].append(anchor)
    for source_key, anchors in anchors_by_source.items():
        if any(int(anchor["item"].id) in partial_rebind_by_item_id for anchor in anchors):
            continue
        current_candidate = candidates_by_code.get(source_key)
        current_pick_composition = _merge_physical_box_lines(
            [
                {
                    "pick_composition": [
                        _line_from_item(
                            anchor["item"],
                            qty_per_box=max(_as_int(anchor.get("item_pick_qty")), 0),
                        )
                    ]
                }
                for anchor in anchors
            ],
            requested=True,
        )
        current_is_usable = bool(
            current_candidate
            and source_key not in priority_claims
            and _box_contains_pick_composition(
                current_candidate["box"],
                current_pick_composition,
            )
        )
        if current_is_usable:
            continue

        anchor_item_ids = {int(anchor["item"].id) for anchor in anchors}
        replacements = []
        for candidate in candidate_rows:
            code_key = candidate["key"]
            owner_id = protected_owner.get(code_key)
            if (
                code_key in priority_claims
                or code_key in used_keys
                or (owner_id is not None and owner_id not in anchor_item_ids)
                or not _box_contains_pick_composition(
                    candidate["box"],
                    current_pick_composition,
                )
            ):
                continue
            replacements.append(candidate)
        if not replacements:
            if _source_codes_temporarily_blocked(
                order,
                [anchor["source_code"] for anchor in anchors],
                blocked_pallets=blocked_pallets,
                blocked_box_keys=blocked_box_keys,
            ):
                raise ValidationError(_ACTIVE_SOURCE_WAIT_MESSAGE)
            item_labels = ", ".join(
                _normalize_text(anchor["item"].sku_code or anchor["item"].barcode or anchor["item"].id)
                for anchor in anchors
            )
            raise ValidationError(
                "Нельзя переназначить занятый исходный короб для частичного отбора: "
                f"для {item_labels} нет свободного короба с достаточным остатком."
            )
        replacements.sort(
            key=lambda candidate: (
                sum(max(_as_int(line.get("qty_per_box")), 0) for line in _box_composition(candidate["box"])),
                candidate["key"],
            )
        )
        candidate = replacements[0]
        source_composition = _box_composition(candidate["box"])
        group_key = f"rebind-partial:{int(order.pk)}:{candidate['key']}"
        used_keys.add(candidate["key"])
        for anchor in anchors:
            item = anchor["item"]
            item_id = int(item.id)
            item_line = _line_from_item(
                item,
                qty_per_box=max(_as_int(anchor.get("item_pick_qty")), 0),
            )
            item_source_qty = sum(
                max(_as_int(line.get("qty_per_box")), 0)
                for line in source_composition
                if _demand_line_identity(line) == _demand_line_identity(item_line)
            )
            selected_by_item_id[item_id] = [candidate["code"]]
            partial_rebind_by_item_id[item_id] = {
                "box_code": candidate["code"],
                "group_key": group_key,
                "source_composition": source_composition,
                "pick_composition": current_pick_composition,
                "item_pick_qty": max(_as_int(anchor.get("item_pick_qty")), 0),
                "item_source_qty": item_source_qty,
            }

    for spec in specs:
        item = spec["item"]
        if int(item.id) in selected_by_item_id:
            continue
        selected: list[str] = []
        for code in spec["current_codes"]:
            code_key = _normalize_text(code).lower()
            if (
                code_key in spec["matching_keys"]
                and code_key not in used_keys
                and protected_owner.get(code_key) == int(item.id)
            ):
                selected.append(candidates_by_code[code_key]["code"])
                used_keys.add(code_key)
        for candidate in spec["matching"]:
            if len(selected) >= int(spec["box_count"]):
                break
            code_key = candidate["key"]
            owner_id = protected_owner.get(code_key)
            if code_key in used_keys or (owner_id is not None and owner_id != int(item.id)):
                continue
            selected.append(candidate["code"])
            used_keys.add(code_key)
        if len(selected) != int(spec["box_count"]):
            item = spec["item"]
            if _source_codes_temporarily_blocked(
                order,
                spec["current_codes"],
                blocked_pallets=blocked_pallets,
                blocked_box_keys=blocked_box_keys,
            ):
                raise ValidationError(_ACTIVE_SOURCE_WAIT_MESSAGE)
            raise ValidationError(
                "Нельзя сформировать точный подбор: для позиции "
                f"{item.sku_code or item.barcode or item.id} нет нужного количества "
                "свободных коробов того же состава. Заявка и складские остатки не изменены."
            )
        selected_by_item_id[int(item.id)] = selected

    changes: list[dict] = []
    for spec in specs:
        item = spec["item"]
        old_codes = list(spec["current_codes"])
        new_codes = selected_by_item_id[int(item.id)]
        partial_rebind = partial_rebind_by_item_id.get(int(item.id))
        if (
            not partial_rebind
            and [code.lower() for code in old_codes] == [code.lower() for code in new_codes]
        ):
            continue
        changes.append(
            {
                "item_id": int(item.id),
                "sku_code": item.sku_code,
                "barcode": item.barcode,
                "old_boxes": old_codes,
                "new_boxes": new_codes,
                "partial_rebind": bool(partial_rebind),
            }
        )
    spec_item_ids = {int(spec["item"].id) for spec in specs}
    item_by_id = {int(item.id): item for item in items}
    for item_id, partial_rebind in partial_rebind_by_item_id.items():
        if item_id in spec_item_ids:
            continue
        item = item_by_id[item_id]
        changes.append(
            {
                "item_id": item_id,
                "sku_code": item.sku_code,
                "barcode": item.barcode,
                "old_boxes": _parse_box_codes(str(item.comment or "")),
                "new_boxes": selected_by_item_id[item_id],
                "partial_rebind": True,
            }
        )
    if not changes:
        return []

    selected_codes = [
        code
        for codes in selected_by_item_id.values()
        for code in codes
    ]
    # Serialize against warehouse operations that may start while candidates are
    # being calculated, then verify every selected snapshot once more.
    list(
        WarehouseStockSnapshot.objects.select_for_update()
        .filter(
            agency=order.agency,
            container_code__in=selected_codes,
            is_archived=False,
        )
        .values_list("id", flat=True)
    )
    live_by_code = _live_snapshot_by_box_code(
        selected_codes,
        agency_id=order.agency_id,
        allowed_active_operation_ids=_preemptible_storage_operation_ids(order.agency_id),
    )
    refreshed_blocked_pallets, refreshed_blocked_box_keys = _active_otg_source_guards(order.agency_id)
    for code in selected_codes:
        code_key = code.lower()
        snapshot = live_by_code.get(code_key)
        if snapshot is None or code_key in refreshed_blocked_box_keys:
            raise ValidationError("Склад изменился во время замены коробов. Повторите принятие заявки.")
        if _snapshot_pallet_code(snapshot) in refreshed_blocked_pallets:
            raise ValidationError("Паллета занята другим заданием. Повторите принятие заявки.")

    changed_item_ids = {int(change["item_id"]) for change in changes}
    for item in items:
        if int(item.id) not in changed_item_ids:
            continue
        partial_rebind = partial_rebind_by_item_id.get(int(item.id))
        if partial_rebind:
            item.comment = _comment_with_rebound_partial_source(
                item.comment,
                **partial_rebind,
            )
        else:
            item.comment = _comment_with_source_box_codes(
                item.comment,
                selected_by_item_id[int(item.id)],
            )
        item.save(update_fields=["comment", "updated_at"])
    return changes


@transaction.atomic
def replace_otg_reachtruck_assignment_boxes(
    *,
    order: ShippingOrder,
    otg_request_id: int,
    old_box_codes: list[str],
    new_box_codes: list[str],
    user=None,
    requested_by_name: str = "",
    requested_by_role: str = "",
) -> dict:
    order = ShippingOrder.objects.select_for_update().get(pk=order.pk)
    otg_request = (
        OtgDeliveryRequest.objects.select_for_update()
        .filter(pk=otg_request_id, shipping_order=order)
        .first()
    )
    if otg_request is None:
        raise ValidationError("Задание ричтраку уже изменилось. Обновите страницу.")
    editor = _assignment_editor_for_request(order=order, otg_request=otg_request)
    if not editor.get("can_replace"):
        raise ValidationError(editor.get("blocked_reason") or "Состав задания уже нельзя изменить.")

    old_codes = [_normalize_text(code) for code in old_box_codes if _normalize_text(code)]
    new_codes = [_normalize_text(code) for code in new_box_codes if _normalize_text(code)]
    if len(old_codes) != len(new_codes):
        raise ValidationError("Передан неполный список замен. Обновите страницу и повторите.")
    rows_by_old: dict[str, dict] = {}
    all_rows: list[dict] = []
    for task_row in editor.get("tasks") or []:
        for row in task_row.get("boxes") or []:
            all_rows.append(row)
            if row.get("replaceable"):
                rows_by_old[_normalize_text(row.get("current_code")).lower()] = row
    posted_old_keys = [code.lower() for code in old_codes]
    if len(set(posted_old_keys)) != len(posted_old_keys) or set(posted_old_keys) != set(rows_by_old):
        raise ValidationError("Состав задания уже изменился. Обновите страницу.")

    canonical_new_by_old: dict[str, str] = {}
    for old_code, new_code in zip(old_codes, new_codes):
        row = rows_by_old[old_code.lower()]
        options = {
            _normalize_text(option.get("code")).lower(): _normalize_text(option.get("code"))
            for option in row.get("options") or []
            if _normalize_text(option.get("code"))
        }
        canonical = options.get(new_code.lower())
        if not canonical:
            raise ValidationError(f"Короб {new_code} больше не доступен для этой позиции.")
        canonical_new_by_old[old_code.lower()] = canonical

    final_codes: list[str] = []
    selected_by_demand_id: dict[int, list[str]] = defaultdict(list)
    for row in all_rows:
        current_code = _normalize_text(row.get("current_code"))
        selected_code = canonical_new_by_old.get(current_code.lower(), current_code)
        final_codes.append(selected_code)
        demand_id = int(row.get("demand_id") or 0)
        if demand_id and selected_code not in selected_by_demand_id[demand_id]:
            selected_by_demand_id[demand_id].append(selected_code)
    if len({code.lower() for code in final_codes}) != len(final_codes):
        raise ValidationError("Один короб нельзя назначить в задание дважды.")

    changes = [
        {"old": old_code, "new": canonical_new_by_old[old_code.lower()]}
        for old_code in old_codes
        if old_code.lower() != canonical_new_by_old[old_code.lower()].lower()
    ]
    if not changes:
        raise ValidationError("Выберите хотя бы один другой короб.")

    demand_models = list(
        OtgDeliveryDemand.objects.select_for_update()
        .filter(request=otg_request)
        .order_by("id")
    )
    override_payloads: list[dict] = []
    item_codes: dict[int, list[str]] = {}
    for demand in demand_models:
        payload = _demand_payload_from_model(demand)
        selected_codes = selected_by_demand_id.get(int(demand.id))
        if selected_codes:
            payload["source_box_codes"] = selected_codes
            payload["demand_key"] = _demand_key(
                {
                    "type": payload["demand_type"],
                    "composition": _composition_key(payload["composition"]),
                    "pick_composition": _composition_key(payload["pick_composition"]),
                    "source_box_codes": sorted(code.lower() for code in selected_codes),
                }
            )
            for raw_item_id in payload.get("item_quantities_per_box") or {}:
                item_id = _as_int(raw_item_id)
                if item_id > 0:
                    item_codes[item_id] = selected_codes
        override_payloads.append(payload)

    for item in ShippingOrderItem.objects.select_for_update().filter(order=order, id__in=item_codes):
        item.comment = _comment_with_source_box_codes(item.comment, item_codes[int(item.id)])
        item.save(update_fields=["comment", "updated_at"])

    move_request, move_ids, shortage_qty = create_otg_shipping_pick_request(
        order=order,
        user=user,
        requested_by_name=requested_by_name,
        requested_by_role=requested_by_role,
        allow_partial=False,
        demand_payloads_override=override_payloads,
        cancel_existing=True,
        request_reason="storekeeper_box_replacement",
    )
    if shortage_qty > 0 or not move_ids:
        raise ValidationError("Не удалось перестроить задание целиком. Изменения отменены.")
    replacement_request = OtgDeliveryRequest.objects.select_for_update().get(move_request=move_request)
    replacement_payload = dict(replacement_request.payload or {})
    replacement_payload["manual_box_replacement"] = {
        "source_request_id": int(otg_request.id),
        "changes": changes,
        "changed_at": timezone.now().isoformat(),
    }
    replacement_request.payload = replacement_payload
    replacement_request.save(update_fields=["payload", "updated_at"])
    _make_event(
        replacement_request,
        "boxes_replaced_by_storekeeper",
        message="Кладовщик изменил состав коробов до начала выполнения.",
        payload={"changes": changes, "move_ids": move_ids},
    )
    return {"move_ids": move_ids, "changes": changes, "request_id": replacement_request.id}


@transaction.atomic
def ensure_waiting_otg_shipping_pick_request(
    *,
    order: ShippingOrder,
    user=None,
    requested_by_name: str = "",
    requested_by_role: str = "",
    waiting_message: str = "",
) -> OtgDeliveryRequest:
    """Persist one idempotent queue marker without planning or stock writes."""
    order = ShippingOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != ShippingOrder.STATUS_STOREKEEPER_ACCEPTED:
        raise ValidationError("Ожидание OTG можно создать только для принятой складом заявки.")
    message = _normalize_text(waiting_message) or (
        "OTG-отбор ожидает освобождения паллеты или резерва."
    )

    waiting_requests = list(
        OtgDeliveryRequest.objects.select_for_update()
        .filter(
            shipping_order=order,
            status=OtgDeliveryRequest.STATUS_BLOCKED,
        )
        .order_by("created_at", "id")
    )
    existing = next(
        (
            request
            for request in waiting_requests
            if bool(dict(request.payload or {}).get(_WAITING_FOR_STOCK_KEY))
        ),
        None,
    )
    if existing is not None:
        payload = {
            **dict(existing.payload or {}),
            _WAITING_FOR_STOCK_KEY: True,
            "auto_retry": True,
            "waiting_reason": "pallet_or_reserve_temporarily_unavailable",
            "waiting_message": message,
        }
        existing.payload = payload
        existing.planning_error = message
        existing.save(update_fields=["payload", "planning_error", "updated_at"])
        return existing

    authenticated_user = user if getattr(user, "is_authenticated", False) else None
    waiting_request = OtgDeliveryRequest.objects.create(
        shipping_order=order,
        agency=order.agency,
        requested_by=authenticated_user,
        requested_by_name=requested_by_name,
        requested_by_role=requested_by_role,
        status=OtgDeliveryRequest.STATUS_BLOCKED,
        requested_boxes=max(_as_int(order.expected_boxes), 0),
        planned_boxes=0,
        shortage_boxes=0,
        planning_error=message,
        payload={
            _WAITING_FOR_STOCK_KEY: True,
            "auto_retry": True,
            "waiting_reason": "pallet_or_reserve_temporarily_unavailable",
            "waiting_message": message,
        },
    )
    _make_event(
        waiting_request,
        "waiting_for_stock",
        message=message,
        payload={"auto_retry": True},
    )
    return waiting_request


@transaction.atomic
def create_otg_shipping_pick_request(
    *,
    order: ShippingOrder,
    user=None,
    requested_by_name: str = "",
    requested_by_role: str = "",
    allow_partial: bool = True,
    demand_items=None,
    demand_payloads_override: list[dict] | None = None,
    requested_qty_by_item_id: dict[int, int] | None = None,
    detached_move_items: list[dict] | None = None,
    cancel_existing: bool = True,
    request_reason: str = "",
) -> tuple[MoveRequest, list[str], int]:
    order = ShippingOrder.objects.select_for_update().get(pk=order.pk)
    Agency.objects.select_for_update().filter(pk=order.agency_id).only("id").first()
    ozon_binding_errors = enforced_ozon_request_binding_errors(
        order,
        compare_source_box_count=False,
    )
    if ozon_binding_errors:
        raise ValidationError(
            [
                "Нельзя создать задания OTG: состав Ozon содержит товар с неверным ШК.",
                *ozon_binding_errors,
            ]
        )
    if (
        cancel_existing
        and not request_reason
        and order.status == ShippingOrder.STATUS_STOREKEEPER_ACCEPTED
    ):
        priority_wait_reason = shipping_queue_priority_wait_reason(order)
        if priority_wait_reason:
            raise ValidationError(priority_wait_reason)
    authenticated_user = user if getattr(user, "is_authenticated", False) else None
    if cancel_existing:
        existing = list(
            OtgDeliveryRequest.objects.filter(
                shipping_order=order,
                status__in=_ACTIVE_OTG_STATUSES,
            )
        )
        existing.extend(
            request
            for request in OtgDeliveryRequest.objects.filter(
                shipping_order=order,
                status=OtgDeliveryRequest.STATUS_BLOCKED,
            )
            if bool(dict(request.payload or {}).get(_WAITING_FOR_STOCK_KEY))
        )
        for request in {request.id: request for request in existing}.values():
            request.status = OtgDeliveryRequest.STATUS_CANCELED
            request.planning_error = "Canceled before rebuilding OTG plan"
            request.save(update_fields=["status", "planning_error", "updated_at"])
        _cancel_open_shipping_pick_tasks(order)
    demand_item_list = list(demand_items) if demand_items is not None else None

    otg_request = OtgDeliveryRequest.objects.create(
        shipping_order=order,
        agency=order.agency,
        requested_by=authenticated_user,
        requested_by_name=requested_by_name,
        requested_by_role=requested_by_role,
        status=OtgDeliveryRequest.STATUS_PLANNING,
    )
    _make_event(otg_request, "planning_started")

    try:
        if demand_payloads_override is None:
            original_demand_payloads = build_box_demands(order, items=demand_item_list)
        else:
            original_demand_payloads = [
                deepcopy(payload)
                for payload in demand_payloads_override
                if isinstance(payload, dict)
            ]
            if not original_demand_payloads:
                raise ValidationError("Не найдены позиции для добора OTG.")
        original_requested_boxes = sum(max(_as_int(payload.get("boxes_required")), 0) for payload in original_demand_payloads)
        # A strict OTG task may be satisfied only by its own confirmed scans.
        # Stock already in OTG is not a fact of this new request and therefore
        # must neither reduce demand nor close it during planning.
        demand_payloads = original_demand_payloads
        delivered_summary = {"boxes": [], "qty": 0, "by_demand": {}}
        demand_models = [
            OtgDeliveryDemand.objects.create(
                request=otg_request,
                demand_key=payload["demand_key"],
                demand_type=payload["demand_type"],
                boxes_required=payload["boxes_required"],
                box_qty=payload["box_qty"],
                composition=payload["composition"],
                pick_composition=payload.get("pick_composition") or [],
                item_quantities_per_box=payload.get("item_quantities_per_box") or {},
                source_box_codes=payload.get("source_box_codes") or [],
                payload=payload.get("payload") or {},
            )
            for payload in demand_payloads
        ]
        requested_boxes = sum(int(demand.boxes_required or 0) for demand in demand_models)
        base_rows = _base_rows_for_order(order, demand_payloads)
        plans, shortage_boxes, shortage_qty = _plan_demands(
            otg_request=otg_request,
            demand_models=demand_models,
            base_rows=base_rows,
            agency_id=order.agency_id,
            order=order,
        )
        allowed_active_operation_ids = _preemptible_storage_operation_ids(order.agency_id)
        plans, dropped_boxes, dropped_qty = _filter_live_warehouse_plans(
            plans,
            agency_id=order.agency_id,
            allowed_active_operation_ids=allowed_active_operation_ids,
        )
        if dropped_boxes > 0:
            shortage_boxes += dropped_boxes
            shortage_qty += dropped_qty
        _sync_demand_planned_boxes(demand_models, plans)
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id=str(order.pk),
            process=MoveRequest.PROCESS_SHIPPING,
            agency=order.agency,
            requested_by=authenticated_user,
            requested_by_name=requested_by_name,
            requested_by_role=requested_by_role,
            destination_zone="OTG",
            status=MoveRequest.STATUS_CREATED,
            comment=f"OTG shipping {order.number}" + (f" ({request_reason})" if request_reason else ""),
        )
        otg_request.move_request = move_request
        planned_boxes_preview = sum(int(plan.get("boxes_planned") or 0) for plan in plans)
        if shortage_boxes > 0 and not allow_partial:
            otg_request.requested_boxes = requested_boxes
            otg_request.planned_boxes = planned_boxes_preview
            otg_request.shortage_boxes = shortage_boxes
            otg_request.status = OtgDeliveryRequest.STATUS_BLOCKED
            otg_request.planning_error = f"Shortage: {shortage_boxes} boxes"
            otg_request.payload = {
                **dict(otg_request.payload or {}),
                "original_requested_boxes": original_requested_boxes,
                "otg_already_delivered": delivered_summary,
                "partial_plan_blocked": True,
            }
            otg_request.save(
                update_fields=[
                    "move_request",
                    "requested_boxes",
                    "planned_boxes",
                    "shortage_boxes",
                    "status",
                    "planning_error",
                    "payload",
                    "updated_at",
                ]
            )
            move_request.status = MoveRequest.STATUS_BLOCKED
            move_request.planning_error = (
                f"Нельзя создать частичный OTG-отбор: не удалось покрыть {shortage_boxes} короб."
            )
            move_request.save(update_fields=["status", "planning_error", "updated_at"])
            _make_event(
                otg_request,
                "partial_plan_blocked",
                payload={
                    "requested_boxes": requested_boxes,
                    "planned_boxes": planned_boxes_preview,
                    "shortage_boxes": shortage_boxes,
                    "shortage_qty": shortage_qty,
                },
            )
            return move_request, [], shortage_qty
        if not demand_models:
            otg_request.requested_boxes = 0
            otg_request.planned_boxes = 0
            otg_request.shortage_boxes = 0
            otg_request.status = OtgDeliveryRequest.STATUS_DONE
            otg_request.payload = {
                **dict(otg_request.payload or {}),
                "original_requested_boxes": original_requested_boxes,
                "otg_already_delivered": delivered_summary,
            }
            otg_request.save(
                update_fields=[
                    "move_request",
                    "requested_boxes",
                    "planned_boxes",
                    "shortage_boxes",
                    "status",
                    "payload",
                    "updated_at",
                ]
            )
            move_request.status = MoveRequest.STATUS_DONE
            move_request.save(update_fields=["status", "updated_at"])
            _make_event(
                otg_request,
                "already_delivered",
                payload={
                    "original_requested_boxes": original_requested_boxes,
                    "otg_already_delivered": delivered_summary,
                },
            )
            return move_request, [], 0
        selected_pallet_codes = {
            _normalize_text(plan.get("pallet_code"))
            for plan in plans
            if _normalize_text(plan.get("pallet_code"))
        }
        selected_box_keys_by_pallet: dict[str, set[str]] = defaultdict(set)
        whole_pallet_codes: set[str] = set()
        for plan in plans:
            pallet_key = _normalize_text(plan.get("pallet_code")).lower()
            if not pallet_key:
                continue
            if plan.get("plan_type") == OtgPalletPlan.TYPE_FULL_PALLET:
                whole_pallet_codes.add(pallet_key)
                continue
            selected_box_keys_by_pallet[pallet_key].update(
                _normalize_text(code).lower()
                for code in (plan.get("planned_box_codes") or [])
                if _normalize_text(code)
            )
        for pallet_key in whole_pallet_codes:
            selected_box_keys_by_pallet.pop(pallet_key, None)
        superseded_storage_move_ids = _cancel_preemptible_storage_routes_for_otg(
            agency_id=order.agency_id,
            pallet_codes=selected_pallet_codes,
            selected_box_keys_by_pallet=dict(selected_box_keys_by_pallet),
            order=order,
        )
        if superseded_storage_move_ids:
            verified_plans, canceled_dropped_boxes, canceled_dropped_qty = _filter_live_warehouse_plans(
                plans,
                agency_id=order.agency_id,
            )
            if canceled_dropped_boxes > 0 or canceled_dropped_qty > 0 or len(verified_plans) != len(plans):
                raise ValidationError(
                    "Склад изменился во время построения маршрута. Обновите заявку и повторите подбор."
                )
            plans = verified_plans
        request_item_map = _request_items_for_move(
            move_request,
            order,
            items=demand_item_list,
            requested_qty_by_item_id=requested_qty_by_item_id,
            detached_move_items=detached_move_items,
        )
        created_plans: list[OtgPalletPlan] = []
        for plan_data in plans:
            demand = plan_data["demand"]
            plan = OtgPalletPlan.objects.create(
                request=otg_request,
                demand=demand,
                plan_type=plan_data["plan_type"],
                pallet_code=plan_data["pallet_code"],
                boxes_planned=plan_data["boxes_planned"],
                qty_planned=plan_data["qty_planned"],
                from_location=plan_data["from_location"],
                selection_mode="pattern_matching",
                planned_box_codes=plan_data.get("planned_box_codes") or [],
                payload={
                    "receiving_order_id": plan_data.get("receiving_order_id") or "",
                    **dict(plan_data.get("payload") or {}),
                },
            )
            created_plans.append(plan)

        move_ids: list[str] = []
        fixed_partial_box_codes = _fixed_partial_box_codes_by_pallet(created_plans)
        for plan_group in _group_plans_for_move_tasks(created_plans):
            payloads = [
                _payload_for_plan(
                    order=order,
                    otg_request=otg_request,
                    plan=plan,
                    requested_by_name=requested_by_name,
                    requested_by_role=requested_by_role,
                )
                for plan in plan_group
            ]
            payload = _merge_pick_task_payloads(payloads)
            primary_plan = plan_group[0]
            if all(plan.plan_type == OtgPalletPlan.TYPE_PICK_BOXES for plan in plan_group):
                excluded_codes = fixed_partial_box_codes.get(
                    _normalize_text(primary_plan.pallet_code).lower(),
                    [],
                )
                if excluded_codes:
                    payload["reserved_partial_box_codes"] = list(excluded_codes)
                    payload["excluded_box_codes"] = list(excluded_codes)
            move_id = create_stock_move_task(
                user=authenticated_user,
                agency=order.agency,
                description=f"OTG shipping {order.number}: pallet {primary_plan.pallet_code}",
                payload=payload,
                requested_by_name=requested_by_name,
                requested_by_role=requested_by_role,
                move_request=move_request,
            )
            move_task = MoveTask.objects.filter(legacy_order_id=move_id).first()
            if move_task:
                OtgPalletPlan.objects.filter(id__in=[plan.id for plan in plan_group]).update(
                    move_task=move_task,
                    updated_at=timezone.now(),
                )
            move_ids.append(move_id)

        for item_id, planned_qty in _planned_qty_by_item(plans).items():
            request_item = request_item_map.get(item_id)
            if request_item is None:
                continue
            request_item.qty_planned = max(int(planned_qty or 0), 0)
            request_item.save(update_fields=["qty_planned", "updated_at"])

        planned_boxes = sum(int(plan.get("boxes_planned") or 0) for plan in plans)
        otg_request.requested_boxes = requested_boxes
        otg_request.planned_boxes = planned_boxes
        otg_request.shortage_boxes = shortage_boxes
        otg_request.payload = {
            **dict(otg_request.payload or {}),
            "original_requested_boxes": original_requested_boxes,
            "otg_already_delivered": delivered_summary,
            **(
                {"superseded_storage_move_ids": superseded_storage_move_ids}
                if superseded_storage_move_ids
                else {}
            ),
            **({"request_reason": request_reason} if request_reason else {}),
        }
        if not move_ids:
            otg_request.status = OtgDeliveryRequest.STATUS_BLOCKED
            otg_request.planning_error = (
                "OTG-отбор ожидает доступный товар: паллеты заняты активными "
                "заданиями или резервами."
            )
            otg_request.payload = {
                **dict(otg_request.payload or {}),
                _WAITING_FOR_STOCK_KEY: True,
                "auto_retry": True,
                "waiting_reason": "pallet_or_reserve_temporarily_unavailable",
            }
            move_request.status = MoveRequest.STATUS_BLOCKED
            move_request.planning_error = otg_request.planning_error
            move_request.save(update_fields=["status", "planning_error", "updated_at"])
        elif shortage_boxes > 0:
            otg_request.status = OtgDeliveryRequest.STATUS_PARTIAL
            otg_request.planning_error = f"Shortage: {shortage_boxes} boxes"
            move_request.status = MoveRequest.STATUS_PARTIAL
            move_request.planning_error = f"Сформировано частично: не удалось покрыть {shortage_boxes} короб."
            move_request.save(update_fields=["status", "planning_error", "updated_at"])
        else:
            otg_request.status = OtgDeliveryRequest.STATUS_DISPATCHED
        otg_request.save(
            update_fields=[
                "move_request",
                "requested_boxes",
                "planned_boxes",
                "shortage_boxes",
                "status",
                "planning_error",
                "payload",
                "updated_at",
            ]
        )
        _make_event(
            otg_request,
            "planning_finished",
            payload={
                "move_ids": move_ids,
                "requested_boxes": requested_boxes,
                "planned_boxes": planned_boxes,
                "shortage_boxes": shortage_boxes,
                "superseded_storage_move_ids": superseded_storage_move_ids,
            },
        )
        return move_request, move_ids, shortage_qty
    except Exception as exc:
        otg_request.status = OtgDeliveryRequest.STATUS_BLOCKED
        otg_request.planning_error = str(exc)
        otg_request.save(update_fields=["status", "planning_error", "updated_at"])
        _make_event(otg_request, "planning_failed", message=str(exc))
        raise


def _has_active_otg_tasks(order: ShippingOrder) -> bool:
    return MoveTask.objects.filter(
        request__agency_id=order.agency_id,
        request__context_type=MoveRequest.CONTEXT_MANUAL,
        request__context_id=str(order.pk),
        request__destination_zone="OTG",
        to_zone="OTG",
        status__in=[MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS],
    ).exists()


@transaction.atomic
def _retry_waiting_otg_request(request_id: int) -> list[str]:
    order_id = (
        OtgDeliveryRequest.objects.filter(pk=request_id)
        .values_list("shipping_order_id", flat=True)
        .first()
    )
    if not order_id:
        return []
    order = ShippingOrder.objects.select_for_update().filter(pk=order_id).first()
    if order is None or order.status != ShippingOrder.STATUS_STOREKEEPER_ACCEPTED:
        return []

    waiting_request = (
        OtgDeliveryRequest.objects.select_for_update()
        .filter(pk=request_id, status=OtgDeliveryRequest.STATUS_BLOCKED)
        .first()
    )
    if waiting_request is None:
        return []
    waiting_payload = dict(waiting_request.payload or {})
    if not bool(waiting_payload.get(_WAITING_FOR_STOCK_KEY)):
        return []

    newer_waiting_exists = any(
        bool(dict(payload or {}).get(_WAITING_FOR_STOCK_KEY))
        for payload in OtgDeliveryRequest.objects.filter(
            shipping_order=order,
            status=OtgDeliveryRequest.STATUS_BLOCKED,
            id__gt=waiting_request.id,
        )
        .exclude(pk=waiting_request.pk)
        .values_list("payload", flat=True)
    )
    if newer_waiting_exists or _has_active_otg_tasks(order):
        return []

    # Record every attempt, including a normal empty result, so one blocked
    # batch cannot prevent later independent orders from ever being retried.
    waiting_payload["last_queue_attempt_at"] = timezone.now().isoformat()
    waiting_request.payload = waiting_payload
    waiting_request.save(update_fields=["payload", "updated_at"])
    from shipping.services import create_pick_tasks

    try:
        return create_pick_tasks(
            order,
            waiting_request.requested_by,
            requested_by_name=waiting_request.requested_by_name,
            requested_by_role=waiting_request.requested_by_role,
        )
    except ValidationError as exc:
        waiting_payload["last_retry_at"] = timezone.now().isoformat()
        waiting_payload["last_retry_error"] = str(exc)
        waiting_request.payload = waiting_payload
        waiting_request.planning_error = str(exc)
        waiting_request.save(update_fields=["payload", "planning_error", "updated_at"])
        return []


def retry_waiting_otg_shipping_requests(*, agency_id: int | None, limit: int = 5) -> list[str]:
    """Retry a bounded priority-ordered batch after OTG stock becomes available."""
    if not agency_id or int(limit or 0) <= 0:
        return []

    unique_candidates: list[OtgDeliveryRequest] = []
    seen_orders: set[int] = set()
    candidates = (
        OtgDeliveryRequest.objects.select_related("shipping_order")
        .filter(
            agency_id=agency_id,
            status=OtgDeliveryRequest.STATUS_BLOCKED,
            shipping_order__status=ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
        )
        .order_by("-created_at", "-id")
    )
    for candidate in candidates:
        payload = dict(candidate.payload or {})
        order_id = int(candidate.shipping_order_id or 0)
        if not bool(payload.get(_WAITING_FOR_STOCK_KEY)) or order_id in seen_orders:
            continue
        seen_orders.add(order_id)
        unique_candidates.append(candidate)

    urgent_order_ids = _urgent_shipping_order_ids(
        [candidate.shipping_order for candidate in unique_candidates]
    )
    acceptance_times = _shipping_acceptance_times(
        [candidate.shipping_order for candidate in unique_candidates]
    )
    unique_candidates.sort(
        key=lambda candidate: (
            str((candidate.payload or {}).get("last_queue_attempt_at") or ""),
            _shipping_queue_priority_key(
                candidate.shipping_order,
                urgent_order_ids=urgent_order_ids,
                acceptance_times=acceptance_times,
            ),
            candidate.created_at,
            int(candidate.id),
        )
    )
    candidate_ids = [
        int(candidate.id)
        for candidate in unique_candidates[: int(limit)]
    ]

    started_orders: list[str] = []
    for request_id in candidate_ids:
        try:
            move_ids = _retry_waiting_otg_request(request_id)
        except Exception:
            logger.exception(
                "Failed to retry waiting OTG shipping request",
                extra={"otg_delivery_request_id": request_id, "agency_id": agency_id},
            )
            continue
        if not move_ids:
            continue
        order_number = (
            OtgDeliveryRequest.objects.filter(pk=request_id)
            .values_list("shipping_order__number", flat=True)
            .first()
        )
        if order_number:
            started_orders.append(str(order_number))
    return started_orders


@transaction.atomic
def _ensure_missing_otg_queue_marker(order_id: int) -> None:
    order = ShippingOrder.objects.select_for_update().filter(
        pk=order_id, status=ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
    ).first()
    if order is None or _has_active_otg_tasks(order):
        return
    # A recorded shortage or another existing planning result must retain its
    # own recovery workflow. Repair only orders with no OTG request at all.
    if OtgDeliveryRequest.objects.filter(shipping_order=order).exists():
        return
    ensure_waiting_otg_shipping_pick_request(
        order=order,
        waiting_message="Заявка принята складом. Ожидает автоматического подбора паллет.",
    )


def refresh_otg_shipping_queue() -> list[str]:
    """Bounded recovery from an authenticated driver POST; never run on GET.

    Move/reserve signals remain the immediate trigger. Queue polling also
    retries after missed signals, rotating clients and blocked batches.
    """
    if not getattr(settings, "USE_OTG_REACHTRUCK_PLANNER", False):
        return []
    if not cache.add("otg-shipping-queue-refresh-v1", True, timeout=30):
        return []
    agency_ids = list(ShippingOrder.objects.filter(
        status=ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
    ).order_by("agency_id").values_list("agency_id", flat=True).distinct())
    if not agency_ids:
        return []
    last_agency = cache.get("otg-shipping-queue-last-agency-v1", 0)
    agency_id = next((pk for pk in agency_ids if pk > last_agency), agency_ids[0])
    cache.set("otg-shipping-queue-last-agency-v1", agency_id, timeout=86400)
    waiting_orders = ShippingOrder.objects.filter(
        agency_id=agency_id, status=ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
        otg_delivery_requests__isnull=True,
    )
    # At most five repairs and five planning attempts per refresh. Existing
    # row/agency locks and active-task checks prevent duplicate dispatch.
    for order_id in waiting_orders.order_by("id").values_list("id", flat=True)[:5]:
        _ensure_missing_otg_queue_marker(order_id)
    return retry_waiting_otg_shipping_requests(agency_id=agency_id, limit=5)
