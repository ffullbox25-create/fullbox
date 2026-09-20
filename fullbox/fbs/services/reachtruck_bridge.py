from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from importlib import import_module
import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, Count, F, IntegerField, Q, Value, When
from django.db.models.functions import Abs
from django.utils import timezone

from employees.models import Employee
from sklad.location_occupancy import (
    empty_planned_fbs_placeholder_containers,
)
from sklad.models import WarehouseContainer, WarehouseLocation
from sklad.topology import OS_LINE_DISPLAY_LABELS, os_location_code, os_location_label

from fbs.exceptions import FbsError, FbsMovementError, FbsReplenishmentError
from fbs.models import (
    FbsInternalMovement,
    FbsPallet,
    FbsReplenishmentAllocation,
    FbsReplenishmentPlan,
    FbsReplenishmentPreparedBox,
)
from fbs.staging import movement_staging_container_code


logger = logging.getLogger(__name__)


BRIDGE_MARKER = "fbs_replenishment_bridge_v1"
LEGACY_ALLOCATION_PREFIX = "FBS-RPL-A"
LEGACY_PLAN_PREFIX = "FBS-RPL-P"
PLACEMENT_PLAN_PREFIX = "FBS-RPL-PLACE-P"
PREPARED_FLOW_MARKER = "fbs_prepared_boxes_v1"
MARKING_SCAN_MARKER = "fbs_movement_marking_scan_v1"
PREPARED_CLOSE_PREFIX = "FBS-RPL-CLOSE-P"
PREPARED_PLACEMENT_PREFIX = "FBS-RPL-PLACE-B"
DYNAMIC_OS_DESTINATION_MARKER = "fbs_dynamic_os_destination_v1"
FULL_SOURCE_PALLET_MARKER = "fbs_full_source_pallet_v1"
PALLETIZED_BOX_BATCH_MARKER = "fbs_box_pallet_batch_v2"
FULL_SOURCE_PALLET_COMMENT_PREFIX = "[client_full_pallet:"
LOGICAL_FULL_PALLET_POSTING_MARKER = "fbs_logical_full_pallet_posting_v1"
FLOOR_REPLENISHMENT_MARKER = "fbs_floor_replenishment_v1"
FLOOR_REPLENISHMENT_LEGACY_PREFIX = "FBS-FLOOR-MOVE-"
CONTAINER_CUSTODY_KEY = "fbs_container_custody_v1"

_reachtruck_models = import_module("reach" + "truck.models")
MoveRequest = _reachtruck_models.MoveRequest
MoveTask = _reachtruck_models.MoveTask


def _without_fbs_movement_marking_scan(payload_value) -> dict:
    """Disable the retired ChZ step for new and already-open FBS moves."""
    payload = dict(payload_value or {})
    if not payload.get(BRIDGE_MARKER):
        return payload
    payload.pop(MARKING_SCAN_MARKER, None)
    execution = dict(payload.get("mobile_execution") or {})
    execution.pop("pending_marking_scan", None)
    execution.pop("pending_product_barcode", None)
    payload["mobile_execution"] = execution
    return payload


def is_fbs_reachtruck_task(task: MoveTask | None) -> bool:
    return bool(task and dict(task.payload or {}).get(BRIDGE_MARKER))


def _is_fbs_box_collection_task(task: MoveTask) -> bool:
    payload = dict(task.payload or {})
    return bool(
        payload.get(BRIDGE_MARKER)
        and payload.get("fbs_allocation_ids")
        and int(payload.get("fbs_plan_id") or 0) > 0
        and int(payload.get("fbs_movement_id") or 0) > 0
        and task.move_mode in {MoveTask.MODE_BOX_FULL, MoveTask.MODE_PALLET_FULL}
        and not payload.get(PREPARED_FLOW_MARKER)
        and not payload.get("fbs_placement_task")
        and not payload.get("fbs_prepared_box_placement_task")
        and not payload.get("fbs_box_closure_task")
    )


def is_fbs_box_collection_batch(tasks: Iterable[MoveTask]) -> bool:
    task_rows = list(tasks)
    if not task_rows or not all(_is_fbs_box_collection_task(task) for task in task_rows):
        return False
    request_ids = {int(task.request_id or 0) for task in task_rows}
    agency_ids = {int(task.request.agency_id or 0) for task in task_rows}
    movement_ids = {
        int(dict(task.payload or {}).get("fbs_movement_id") or 0)
        for task in task_rows
    }
    return len(request_ids) == len(agency_ids) == len(movement_ids) == 1


def _load_box_collection_tasks(
    legacy_order_ids,
    *,
    for_update: bool = False,
) -> list[MoveTask]:
    normalized_ids = []
    seen = set()
    for raw_value in legacy_order_ids or []:
        value = str(raw_value or "").strip()
        if value and value not in seen:
            seen.add(value)
            normalized_ids.append(value)
    if not normalized_ids:
        return []
    queryset = MoveTask.objects.select_related("request", "request__agency").filter(
        legacy_order_id__in=normalized_ids
    )
    if for_update:
        queryset = queryset.select_for_update(of=("self",))
    by_id = {
        str(task.legacy_order_id or "").strip(): task
        for task in queryset.order_by("created_at", "id")
    }
    return [by_id[value] for value in normalized_ids if value in by_id]


def _task_employee_id(task: MoveTask) -> int:
    payload = dict(task.payload or {})
    for field_name in ("assigned_employee_id", "assigned_to_id"):
        try:
            value = int(payload.get(field_name) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    if task.assigned_to_id:
        return int(
            Employee.objects.filter(user_id=task.assigned_to_id, is_active=True)
            .order_by("id")
            .values_list("id", flat=True)
            .first()
            or 0
        )
    return 0


def _task_employee_name(task: MoveTask) -> str:
    payload = dict(task.payload or {})
    return str(
        payload.get("assigned_to_name")
        or task.assigned_to_name
        or ""
    ).strip()


def _assignment_fingerprint(employee_ids: Iterable[int]) -> str:
    normalized_ids = {
        int(value or 0)
        for value in employee_ids
        if int(value or 0) > 0
    }
    return ",".join(
        str(employee_id)
        for employee_id in sorted(normalized_ids)
    )


def _parse_assignment_fingerprint(raw_value) -> set[int]:
    result: set[int] = set()
    for value in str(raw_value or "").split(","):
        try:
            employee_id = int(value.strip() or 0)
        except (TypeError, ValueError):
            employee_id = 0
        if employee_id > 0:
            result.add(employee_id)
    return result


def _append_takeover_history(
    payload: dict,
    *,
    from_employee_ids: Iterable[int],
    from_employee_names: Iterable[str],
    to_employee_id: int,
    to_employee_name: str,
    taken_over_at: str,
) -> None:
    history = list(payload.get("takeover_history") or [])
    history.append(
        {
            "from_employee_ids": sorted(
                {int(value or 0) for value in from_employee_ids if int(value or 0) > 0}
            ),
            "from_employee_names": sorted(
                {str(value or "").strip() for value in from_employee_names if str(value or "").strip()}
            ),
            "to_employee_id": int(to_employee_id),
            "to_employee_name": str(to_employee_name or "").strip(),
            "taken_over_at": taken_over_at,
        }
    )
    payload["takeover_history"] = history[-20:]
    payload["last_takeover_at"] = taken_over_at


def _is_concrete_container_location(location: WarehouseLocation | None) -> bool:
    if location is None:
        return False
    zone_code = str(location.zone_code or "").strip().upper()
    # Coordinate-addressed zones require a complete rack address. Named zones
    # such as MR may use a stable scanned location code without rack coordinates.
    if zone_code == "OS" and str(location.location_code or "").strip().upper() == "A-B":
        # A-B is a separately scanned floor place, not a rack address.  It is
        # intentionally stored inside the OS contour without rack coordinates.
        return True
    if zone_code in {"OS", "STORAGE", "FBS"}:
        return all(
            int(value or 0) > 0
            for value in (
                location.row_no,
                location.section_no,
                location.tier_no,
                location.cell_no,
            )
        )
    return bool(str(location.location_code or "").strip())


def _payload_has_concrete_source(payload: dict) -> bool:
    location = dict(payload.get("from_location") or {})
    zone_code = str(location.get("zone") or "").strip().upper()
    if zone_code == "OS" and str(location.get("code") or "").strip().upper() == "A-B":
        return True
    if zone_code in {"OS", "STORAGE", "FBS"}:
        return all(
            int(location.get(field_name) or 0) > 0
            for field_name in ("row", "section", "tier", "cell")
        )
    return bool(
        str(payload.get("source_location_scan_code") or "").strip()
        or str(payload.get("from_label") or "").strip()
    )


def _task_requires_container_custody(task: MoveTask, payload: dict) -> bool:
    if payload.get(LOGICAL_FULL_PALLET_POSTING_MARKER):
        return False
    try:
        payload_assigned_to_id = int(payload.get("assigned_to_id") or 0)
    except (TypeError, ValueError):
        payload_assigned_to_id = 0
    if (
        payload.get(FULL_SOURCE_PALLET_MARKER)
        and task.status == MoveTask.STATUS_CREATED
        and not task.assigned_to_id
        and not payload_assigned_to_id
    ):
        # An untouched complete pallet can still use the separately confirmed
        # logical FBS posting that preserves its exact physical place.
        return False
    return bool(
        _is_fbs_box_collection_task(task)
        or payload.get(FLOOR_REPLENISHMENT_MARKER)
        or payload.get("fbs_placement_task")
        or payload.get("fbs_prepared_box_placement_task")
    )


def _task_source_physical_location(
    container: WarehouseContainer,
) -> WarehouseLocation:
    if container.current_location_id:
        if _is_concrete_container_location(container.current_location):
            return container.current_location
        raise FbsReplenishmentError(
            "У короба или паллеты указано обезличенное исходное место. "
            "Создание задания FBS остановлено до подтверждения точной ячейки."
        )
    parent = container.parent_container
    if parent is not None and parent.current_location_id:
        if _is_concrete_container_location(parent.current_location):
            return parent.current_location
        raise FbsReplenishmentError(
            "У родительской паллеты указано обезличенное исходное место. "
            "Создание задания FBS остановлено до подтверждения точной ячейки."
        )
    raise FbsReplenishmentError(
        "У короба или паллеты не найдено текущее физическое место. "
        "Автоматическое восстановление по старому остатку запрещено."
    )


def _require_task_concrete_source(task: MoveTask, payload: dict) -> None:
    if _task_requires_container_custody(task, payload) and not _payload_has_concrete_source(
        payload
    ):
        raise FbsReplenishmentError(
            "У короба или паллеты не указана точная исходная ячейка. "
            "Задание оставлено открытым; сначала восстановите физическое место."
        )


def _custody_container_identity(task: MoveTask, payload: dict) -> tuple[str, str]:
    if payload.get(FULL_SOURCE_PALLET_MARKER):
        return (
            "pallet",
            str(payload.get("pallet_code") or task.pallet_code or "").strip(),
        )
    return (
        "box",
        str(
            payload.get("source_box_code")
            or payload.get("requested_box")
            or task.pallet_code
            or ""
        ).strip(),
    )


def _assign_container_custody(
    *,
    task: MoveTask,
    payload: dict,
    employee_id: int,
    employee_name: str,
    assigned_at: str,
) -> None:
    if not _task_requires_container_custody(task, payload):
        return
    _require_task_concrete_source(task, payload)
    custody = dict(payload.get(CONTAINER_CUSTODY_KEY) or {})
    execution = dict(payload.get("mobile_execution") or {})
    container_type, container_code = _custody_container_identity(task, payload)
    already_picked = bool(
        execution.get("box_confirmed")
        or (payload.get(FULL_SOURCE_PALLET_MARKER) and execution.get("pallet_confirmed"))
    )
    custody.update(
        {
            "status": "in_transit" if already_picked else "assigned",
            "status_label": "У сотрудника / в пути" if already_picked else "Назначен сотруднику",
            "container_type": container_type,
            "container_code": container_code,
            "responsible_employee_id": int(employee_id),
            "responsible_employee_name": str(employee_name or "").strip(),
            "assigned_at": assigned_at,
            "source_location_code": str(
                payload.get("source_location_scan_code") or ""
            ).strip(),
            "source_location_label": str(payload.get("from_label") or "").strip(),
        }
    )
    if already_picked and not custody.get("picked_at"):
        fallback = task.updated_at or task.started_at or timezone.now()
        custody["picked_at"] = timezone.localtime(fallback).isoformat()
        custody["picked_at_inferred_from_existing_scan"] = True
    payload[CONTAINER_CUSTODY_KEY] = custody


def _mark_container_in_transit(
    *,
    task: MoveTask,
    payload: dict,
    employee_id: int,
    employee_name: str,
    scanned_code: str,
) -> None:
    if not _task_requires_container_custody(task, payload):
        return
    _require_task_concrete_source(task, payload)
    now_local = timezone.localtime().isoformat()
    _assign_container_custody(
        task=task,
        payload=payload,
        employee_id=employee_id,
        employee_name=employee_name,
        assigned_at=str(payload.get("taken_at") or now_local),
    )
    custody = dict(payload.get(CONTAINER_CUSTODY_KEY) or {})
    custody.update(
        {
            "status": "in_transit",
            "status_label": "У сотрудника / в пути",
            "responsible_employee_id": int(employee_id),
            "responsible_employee_name": str(employee_name or "").strip(),
            "picked_at": custody.get("picked_at") or now_local,
            "picked_scan": str(scanned_code or "").strip(),
            "destination_location_code": "",
            "destination_location_label": "",
            "placed_at": "",
        }
    )
    payload[CONTAINER_CUSTODY_KEY] = custody


def _mark_container_placed(
    *,
    task: MoveTask,
    payload: dict,
    destination: WarehouseLocation,
) -> None:
    if not _task_requires_container_custody(task, payload):
        return
    if not _is_concrete_container_location(destination):
        raise FbsReplenishmentError(
            "Нельзя закрыть задание: короб или паллета не привязаны к точной конечной ячейке."
        )
    custody = dict(payload.get(CONTAINER_CUSTODY_KEY) or {})
    if custody.get("status") != "in_transit":
        raise FbsReplenishmentError(
            "Нельзя закрыть задание: сначала подтвердите сканом, что короб или паллета взяты сотрудником."
        )
    custody.update(
        {
            "status": "placed",
            "status_label": "Размещен в ячейке",
            "destination_location_code": _location_scan_code(destination),
            "destination_location_label": _location_label(destination),
            "placed_at": timezone.localtime().isoformat(),
        }
    )
    payload[CONTAINER_CUSTODY_KEY] = custody


def _ensure_fbs_plan_assignment_for_current_driver(
    *,
    task: MoveTask,
    user,
    employee_id: int,
) -> bool:
    """Repair only a missing plan assignment for the task's current driver."""
    payload = dict(task.payload or {})
    if any(
        payload.get(marker)
        for marker in (
            "fbs_placement_task",
            "fbs_prepared_box_placement_task",
            "fbs_box_closure_task",
        )
    ):
        return False
    plan_id = int(payload.get("fbs_plan_id") or 0)
    if plan_id <= 0:
        return False
    if not getattr(user, "is_authenticated", False):
        raise FbsReplenishmentError("Для продолжения FBS-задания требуется сотрудник ричтрака.")
    if task.assigned_to_id != user.id or _task_employee_id(task) != int(employee_id):
        raise FbsReplenishmentError("Задание FBS назначено другому водителю.")

    plan = FbsReplenishmentPlan.objects.select_for_update().filter(pk=plan_id).first()
    if plan is None:
        raise FbsReplenishmentError("План FBS для задания не найден.")
    if plan.assigned_to_id not in {None, user.id}:
        raise FbsReplenishmentError("План FBS уже взят другим сотрудником.")
    if plan.assigned_to_id == user.id:
        return False

    from fbs.services.replenishment import claim_replenishment_plan

    claim_replenishment_plan(plan_id=plan.id, assigned_to=user)
    return True


def _box_count_label(count: int) -> str:
    value = max(int(count or 0), 0)
    tail = value % 100
    if 11 <= tail <= 14:
        suffix = "коробов"
    elif value % 10 == 1:
        suffix = "короб"
    elif value % 10 in {2, 3, 4}:
        suffix = "короба"
    else:
        suffix = "коробов"
    return f"{value} {suffix}"


def _destination_orientation_label(location: WarehouseLocation) -> str:
    line = OS_LINE_DISPLAY_LABELS.get(
        int(location.section_no or 0),
        str(int(location.section_no or 0)),
    )
    return f"Линия {line} · стеллаж {int(location.row_no or 0)}"


def _fbs_destination_guide(task: MoveTask) -> dict:
    """Builds a read-only physical hint; it never reserves a destination."""
    agency_id = int(getattr(getattr(task, "request", None), "agency_id", 0) or 0)
    if not agency_id:
        return {}

    agency = getattr(getattr(task, "request", None), "agency", None)
    agency_label = str(getattr(agency, "agn_name", "") or agency or "клиента").strip()
    active_statuses = (FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE)
    placeholder_ids = empty_planned_fbs_placeholder_containers().values("id")
    pallets = list(
        FbsPallet.objects.select_related("cell__location")
        .filter(
            agency_id=agency_id,
            status__in=active_statuses,
            cell__is_active=True,
            cell__location__is_active=True,
            cell__location__zone_code__iexact="OS",
            cell__location__zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            cell__location__is_storage=True,
            cell__location__row_no__gt=0,
            cell__location__section_no__gt=0,
            cell__location__tier_no__gt=0,
            cell__location__cell_no__gt=0,
        )
        .exclude(warehouse_container_id__in=placeholder_ids)
        .annotate(
            active_box_count=Count(
                "boxes",
                filter=(
                    Q(boxes__status__in=("planned", "active"))
                    & Q(boxes__stock_balances__qty__gt=0)
                ),
                distinct=True,
            )
        )
        .order_by(
            "cell__location__section_no",
            "cell__location__row_no",
            "cell__location__tier_no",
            "cell__location__cell_no",
            "id",
        )
    )
    client_locations = [pallet.cell.location for pallet in pallets]
    location_counts = Counter(
        (int(location.section_no or 0), int(location.row_no or 0))
        for location in client_locations
    )
    anchor = location_counts.most_common(1)[0][0] if location_counts else None
    warehouse_code = ""
    if client_locations:
        warehouse_code = str(client_locations[0].warehouse_code or "").strip()
    if not warehouse_code:
        source = dict(task.payload or {}).get("from_location") or {}
        source_location = (
            WarehouseLocation.objects.filter(
                zone_code__iexact=str(source.get("zone") or "").strip(),
                row_no=int(source.get("row") or 0),
                section_no=int(source.get("section") or 0),
                tier_no=int(source.get("tier") or 0),
                cell_no=int(source.get("cell") or 0),
                is_active=True,
            )
            .order_by("id")
            .first()
        )
        warehouse_code = str(getattr(source_location, "warehouse_code", "") or "").strip()

    client_places = []
    for pallet in pallets:
        location = pallet.cell.location
        client_places.append(
            {
                "code": _location_scan_code(location),
                "label": _location_label(location),
                "boxes": int(getattr(pallet, "active_box_count", 0) or 0),
                "max_boxes": int(pallet.max_boxes or 0),
                "has_space": True,
            }
        )

    free_queryset = WarehouseLocation.objects.filter(
        zone_code__iexact="OS",
        zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
        is_active=True,
        is_storage=True,
        row_no__gt=0,
        section_no__gt=0,
        tier_no__gt=0,
        cell_no__gt=0,
    )
    if warehouse_code:
        free_queryset = free_queryset.filter(warehouse_code=warehouse_code)
    free_queryset = free_queryset.annotate(
        storage_tier_priority=Case(
            When(tier_no__gt=2, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        )
    )
    if anchor:
        anchor_section, anchor_row = anchor
        free_queryset = free_queryset.annotate(
            section_distance=Abs(F("section_no") - Value(anchor_section)),
            row_distance=Abs(F("row_no") - Value(anchor_row)),
        ).order_by(
            "storage_tier_priority",
            "section_distance",
            "row_distance",
            "-tier_no",
            "cell_no",
        )
    else:
        free_queryset = free_queryset.order_by(
            "storage_tier_priority",
            "section_no",
            "row_no",
            "-tier_no",
            "cell_no",
        )
    free_locations = list(free_queryset[:4])

    if anchor and client_locations:
        orientation = _destination_orientation_label(client_locations[0])
        for location in client_locations:
            if (int(location.section_no or 0), int(location.row_no or 0)) == anchor:
                orientation = _destination_orientation_label(location)
                break
        summary = f"Ориентир по размещению {agency_label}: {orientation}."
    else:
        summary = (
            f"У {agency_label} пока нет постоянного ориентира. "
            "Выберите любую действующую ячейку OS."
        )

    return {
        "agency_label": agency_label,
        "summary": summary,
        "client_places": client_places[:4],
        "free_places": [
            {
                "code": _location_scan_code(location),
                "label": _location_label(location),
            }
            for location in free_locations[:4]
        ],
        "advisory": True,
    }


def _box_collection_runtime(task: MoveTask) -> dict:
    payload = dict(task.payload or {})
    execution = dict(payload.get("mobile_execution") or {})
    snapshot = build_fbs_mobile_execution_snapshot(task)
    return {
        "task": task,
        "payload": payload,
        "execution": execution,
        "snapshot": snapshot,
        "assigned_employee_id": _task_employee_id(task),
        "done": task.status == MoveTask.STATUS_DONE,
        "skipped": bool(
            task.status == MoveTask.STATUS_CANCELED
            and payload.get("missing_box_skipped_v1")
        ),
        "pallet_confirmed": bool(execution.get("pallet_confirmed")),
        "box_confirmed": bool(execution.get("box_confirmed")),
        "destination_confirmed": bool(execution.get("destination_confirmed")),
        "full_source_pallet": bool(payload.get(FULL_SOURCE_PALLET_MARKER)),
        "palletized_batch": bool(payload.get(PALLETIZED_BOX_BATCH_MARKER)),
        "target_pallet_id": int(payload.get("fbs_target_pallet_id") or 0),
        "target_pallet_code": str(payload.get("fbs_target_pallet_code") or "").strip(),
        "target_pallet_capacity": int(payload.get("fbs_target_pallet_capacity") or 0),
        "pallet_code": str(payload.get("pallet_code") or task.pallet_code or "").strip(),
        "box_code": str(payload.get("source_box_code") or "").strip(),
    }


def _box_collection_source_key(state: dict) -> tuple[str, str]:
    payload = state["payload"]
    pallet_code = str(payload.get("pallet_code") or state.get("pallet_code") or "").strip()
    source_code = str(payload.get("source_location_scan_code") or "").strip()
    return pallet_code.lower(), source_code.lower()


def _box_collection_source_route_key(state: dict) -> tuple:
    payload = state["payload"]
    location = payload.get("from_location") or {}
    return (
        int(location.get("section") or 0),
        int(location.get("row") or 0),
        int(location.get("tier") or 0),
        int(location.get("cell") or 0),
        str(payload.get("pallet_code") or state.get("pallet_code") or ""),
        int(state["task"].id),
    )


def _palletized_box_collection_groups(states: list[dict]) -> list[list[dict]]:
    groups: dict[tuple[str, int | str], list[dict]] = {}
    for state in states:
        target_pallet_id = int(state.get("target_pallet_id") or 0)
        if target_pallet_id > 0:
            key: tuple[str, int | str] = ("target", target_pallet_id)
        else:
            key = ("task", int(state["task"].id))
        groups.setdefault(key, []).append(state)

    ordered_groups: list[tuple[tuple, list[dict]]] = []
    for key, group in groups.items():
        source_counts = Counter(_box_collection_source_key(state) for state in group)
        group.sort(
            key=lambda state: (
                -int(source_counts[_box_collection_source_key(state)]),
                *_box_collection_source_route_key(state),
            )
        )
        first_route = min(_box_collection_source_route_key(state) for state in group)
        ordered_groups.append(((*first_route, str(key)), group))
    ordered_groups.sort(key=lambda row: row[0])
    return [group for _key, group in ordered_groups]


def build_fbs_box_collection_request_snapshot(
    legacy_order_ids,
    *,
    employee_id: int | None = None,
) -> dict:
    tasks = _load_box_collection_tasks(legacy_order_ids)
    if not is_fbs_box_collection_batch(tasks):
        return {}

    states = [_box_collection_runtime(task) for task in tasks]
    remaining = [state for state in states if not state["done"] and not state["skipped"]]
    assigned_employee_ids = {
        int(state["assigned_employee_id"] or 0)
        for state in remaining
        if int(state["assigned_employee_id"] or 0) > 0
    }
    assigned_employee_names = {
        _task_employee_name(state["task"])
        for state in remaining
        if _task_employee_name(state["task"])
    }
    taken_by_other = any(
        state["assigned_employee_id"]
        and employee_id is not None
        and state["assigned_employee_id"] != int(employee_id)
        for state in remaining
    )
    all_taken_by_current = bool(remaining) and all(
        state["assigned_employee_id"]
        and (employee_id is None or state["assigned_employee_id"] == int(employee_id))
        for state in remaining
    )
    palletized_batch = bool(states) and all(state["palletized_batch"] for state in states)
    active_group = remaining
    active_group_index = 0
    group_count = 1 if remaining else 0
    if palletized_batch:
        groups = _palletized_box_collection_groups(states)
        group_count = len(groups)
        active_group = []
        for index, group in enumerate(groups, start=1):
            group_remaining = [
                state for state in group if not state["done"] and not state["skipped"]
            ]
            if group_remaining:
                active_group = group_remaining
                active_group_index = index
                break
    collection_pending = [
        state
        for state in active_group
        if not state["box_confirmed"]
        or (state["full_source_pallet"] and not state["pallet_confirmed"])
    ]
    collection_active = next(
        (state for state in collection_pending if state["pallet_confirmed"]),
        collection_pending[0] if collection_pending else None,
    )
    placement_pending = [
        state
        for state in active_group
        if state["box_confirmed"]
        and (not state["full_source_pallet"] or state["pallet_confirmed"])
    ]
    full_pallet_placement = next(
        (state for state in placement_pending if state["full_source_pallet"]),
        None,
    )
    active = (
        full_pallet_placement
        or collection_active
        or (placement_pending[0] if placement_pending else None)
    )
    total_count = len(states)
    collected_count = sum(
        1
        for state in states
        if state["done"]
        or (
            state["box_confirmed"]
            and (not state["full_source_pallet"] or state["pallet_confirmed"])
        )
    )
    skipped_count = sum(1 for state in states if state["skipped"])
    placed_count = sum(1 for state in states if state["done"])
    all_collected = total_count > 0 and collected_count + skipped_count == total_count

    current_step = "done"
    expected_scan = ""
    prompt = "Все короба FBS размещены."
    active_order_id = ""
    active_pallet_code = ""
    active_destination_label = ""
    active_group_order_ids = [
        str(state["task"].legacy_order_id or "").strip()
        for state in active_group
        if str(state["task"].legacy_order_id or "").strip()
    ]
    active_target_pallet_code = str(
        (active_group[0].get("target_pallet_code") if active_group else "") or ""
    ).strip()
    active_target_pallet_capacity = int(
        (active_group[0].get("target_pallet_capacity") if active_group else 0) or 0
    )
    active_group_collected = sum(
        1
        for state in active_group
        if state["box_confirmed"]
        and (not state["full_source_pallet"] or state["pallet_confirmed"])
    )
    if active:
        active_order_id = str(active["task"].legacy_order_id or "").strip()
        current_step = str(active["snapshot"].get("current_step") or "").strip()
        expected_scan = str(active["snapshot"].get("expected_scan") or "").strip()
        if current_step == "boxes":
            active_pallet_code = active["pallet_code"]
            prompt = (
                (
                    f"Сборочная паллета {active_group_index} из {group_count}: "
                    f"{active_group_collected} из {len(active_group)} коробов. "
                    if palletized_batch and not active["full_source_pallet"]
                    else f"Собрано коробов {collected_count} из {total_count}. "
                )
                + f"Отсканируйте QR короба {active['box_code']}."
            )
        elif current_step == "pallet":
            active_pallet_code = active["pallet_code"]
            prompt = (
                f"Перемещено коробов {placed_count} из {total_count}. "
                f"Отсканируйте QR паллеты {active['pallet_code']}."
            )
        elif current_step == "destination":
            current_step = "destination"
            active_pallet_code = (
                active["pallet_code"]
                if active["full_source_pallet"]
                else active_target_pallet_code
                if palletized_batch
                else active["box_code"]
            )
            active_destination_label = (
                "Выбранная ячейка OS"
                if active["full_source_pallet"]
                else "Выбранная ячейка OS или точная PR-ячейка"
            )
            expected_scan = (
                "QR выбранной ячейки OS"
                if active["full_source_pallet"]
                else "QR выбранной ячейки OS или ячейки PR"
            )
            if active["full_source_pallet"]:
                prompt = (
                    f"Паллета {active['pallet_code']} подтверждена. "
                    "Выберите любую действующую ячейку OS и отсканируйте ее QR. "
                    "Заполненность оценивает водитель. "
                    "Варианты на экране — только рекомендация по размещению палет клиента."
                )
            if not active["full_source_pallet"] and skipped_count:
                prompt = (
                    f"Собрано {collected_count} из {total_count}; отсутствует {skipped_count}. "
                    f"Разместите короб {active['box_code']}: отсканируйте выбранную "
                    "ячейку OS или точную ячейку PR."
                )
            elif not active["full_source_pallet"]:
                prompt = (
                    (
                        f"Сборочная паллета {active_group_index} из {group_count} "
                        f"собрана: {_box_count_label(len(active_group))}. "
                        f"Разместите паллету {active_target_pallet_code or ''}: "
                        "отсканируйте выбранную ячейку OS."
                        if palletized_batch
                        else (
                            f"Все {_box_count_label(total_count)} собраны. Разместите короб "
                            f"{active['box_code']}: отсканируйте выбранную ячейку OS "
                            "или точную ячейку PR."
                        )
                    )
                )

    return {
        "fbs_box_collection_batch": True,
        "fbs_palletized_box_batch": palletized_batch,
        "total_count": total_count,
        "remaining_count": len(remaining),
        "completed_count": placed_count,
        "collected_count": collected_count,
        "skipped_count": skipped_count,
        "placed_count": placed_count,
        "all_collected": all_collected,
        "can_take": bool(remaining) and not taken_by_other and not all_taken_by_current,
        "can_takeover": bool(remaining) and taken_by_other,
        "can_scan": bool(remaining) and not taken_by_other and all_taken_by_current,
        "taken_by_other": taken_by_other,
        "assigned_to_ids": sorted(assigned_employee_ids),
        "assigned_to_names": sorted(assigned_employee_names),
        "assigned_to_name": (
            next(iter(assigned_employee_names))
            if len(assigned_employee_names) == 1
            else "Несколько водителей"
            if assigned_employee_names
            else ""
        ),
        "assignment_fingerprint": _assignment_fingerprint(assigned_employee_ids),
        "current_step": current_step,
        "prompt": prompt,
        "expected_scan": expected_scan,
        "candidate_locations": [],
        "destination_guide": (
            _fbs_destination_guide(active["task"])
            if active and current_step == "destination"
            else {}
        ),
        "destination_override_pending": False,
        "destination_override_confirm_pending": False,
        "destination_override_candidate_code": "",
        "destination_override_candidate_label": "",
        "active_order_id": active_order_id,
        "active_pallet_code": active_pallet_code,
        "active_destination_code": "",
        "active_destination_label": active_destination_label,
        "active_group_order_ids": active_group_order_ids,
        "active_group_index": active_group_index,
        "group_count": group_count,
        "active_group_box_count": len(active_group),
        "active_group_collected_count": active_group_collected,
        "active_target_pallet_code": active_target_pallet_code,
        "active_target_pallet_capacity": active_target_pallet_capacity,
    }


@transaction.atomic
def take_fbs_box_collection_request(
    *,
    legacy_order_ids,
    user,
    employee_id: int,
    employee_name: str,
    confirm_takeover: bool = False,
    expected_assignee_ids: str = "",
):
    tasks = _load_box_collection_tasks(legacy_order_ids, for_update=True)
    if not is_fbs_box_collection_batch(tasks):
        return _command_result(ok=False, error="Коробочная FBS-заявка не найдена.")
    if not employee_id or not getattr(user, "is_authenticated", False):
        return _command_result(ok=False, error="Профиль водителя ричтрака не найден.")

    remaining = [
        task
        for task in tasks
        if task.status != MoveTask.STATUS_DONE
        and not (
            task.status == MoveTask.STATUS_CANCELED
            and dict(task.payload or {}).get("missing_box_skipped_v1")
        )
    ]
    assigned_employee_ids: set[int] = set()
    assigned_employee_names: set[str] = set()
    assigned_employee_names_by_id: dict[int, str] = {}
    for task in remaining:
        if task.status == MoveTask.STATUS_CANCELED:
            return _command_result(ok=False, error="Часть FBS-заявки отменена.")
        assigned_employee_id = _task_employee_id(task)
        if assigned_employee_id:
            assigned_employee_ids.add(assigned_employee_id)
        assigned_employee_name = _task_employee_name(task)
        if assigned_employee_name:
            assigned_employee_names.add(assigned_employee_name)
            if assigned_employee_id:
                assigned_employee_names_by_id[assigned_employee_id] = assigned_employee_name
    takeover_employee_ids = assigned_employee_ids - {int(employee_id)}
    takeover_employee_names = {
        assigned_employee_names_by_id[value]
        for value in takeover_employee_ids
        if value in assigned_employee_names_by_id
    }
    takeover_required = bool(takeover_employee_ids)
    if takeover_required:
        if not confirm_takeover:
            return _command_result(
                ok=False,
                error="FBS-заявка уже в работе. Подтвердите передачу заявки другому водителю.",
            )
        expected_ids = _parse_assignment_fingerprint(expected_assignee_ids)
        if expected_ids != assigned_employee_ids:
            return _command_result(
                ok=False,
                error="Исполнитель FBS-заявки уже изменился. Обновите список и повторите действие.",
            )

    plan_ids = {
        int(dict(task.payload or {}).get("fbs_plan_id") or 0)
        for task in remaining
    }
    plans = list(
        FbsReplenishmentPlan.objects.select_for_update()
        .filter(id__in=plan_ids)
        .order_by("id")
    )
    if len(plans) != len(plan_ids):
        return _command_result(ok=False, error="Не все планы коробочной FBS-заявки найдены.")
    for plan in plans:
        if plan.status not in {
            FbsReplenishmentPlan.STATUS_CONFIRMED,
            FbsReplenishmentPlan.STATUS_IN_PROGRESS,
        }:
            return _command_result(ok=False, error="Часть FBS-заявки недоступна для работы.")
    from fbs.services.replenishment import claim_replenishment_plans

    try:
        claim_replenishment_plans(
            plan_ids=(plan.id for plan in plans),
            assigned_to=user,
            allow_reassignment=takeover_required,
            expected_assigned_to_ids={plan.id: plan.assigned_to_id for plan in plans},
            sync_reachtruck_assignments=False,
        )
    except FbsReplenishmentError as exc:
        transaction.set_rollback(True)
        return _command_result(ok=False, error=str(exc))

    assigned_at = timezone.now()
    assigned_at_local = timezone.localtime(assigned_at).isoformat()
    updated_tasks: list[MoveTask] = []
    for task in remaining:
        payload = dict(task.payload or {})
        if takeover_required:
            _append_takeover_history(
                payload,
                from_employee_ids=takeover_employee_ids,
                from_employee_names=takeover_employee_names,
                to_employee_id=employee_id,
                to_employee_name=employee_name,
                taken_over_at=assigned_at_local,
            )
        payload.update(
            {
                "fbs_box_collection_batch_v1": True,
                "status": MoveTask.STATUS_IN_PROGRESS,
                "status_label": "В работе",
                "assigned_to_id": int(employee_id),
                "assigned_employee_id": int(employee_id),
                "assigned_to_name": employee_name,
                "taken_at": (
                    assigned_at_local
                    if takeover_required
                    else payload.get("taken_at") or assigned_at_local
                ),
            }
        )
        try:
            _assign_container_custody(
                task=task,
                payload=payload,
                employee_id=employee_id,
                employee_name=employee_name,
                assigned_at=str(payload.get("taken_at") or assigned_at_local),
            )
        except FbsReplenishmentError as exc:
            transaction.set_rollback(True)
            return _command_result(ok=False, error=str(exc))
        task.assigned_to = user
        task.assigned_to_name = employee_name
        task.status = MoveTask.STATUS_IN_PROGRESS
        task.started_at = task.started_at or assigned_at
        task.payload = payload
        task.updated_at = assigned_at
        updated_tasks.append(task)
    if updated_tasks:
        MoveTask.objects.bulk_update(
            updated_tasks,
            [
                "assigned_to",
                "assigned_to_name",
                "status",
                "started_at",
                "payload",
                "updated_at",
            ],
            batch_size=500,
        )
        _sync_request(updated_tasks[0].request)
    return _command_result(
        ok=True,
        message=(
            f"FBS-заявка передана вам: {_box_count_label(len(remaining))}."
            if takeover_required
            else f"Вся FBS-заявка взята в работу: {_box_count_label(len(remaining))}."
        ),
    )


def _confirm_same_pallet_tasks(
    tasks: Iterable[MoveTask],
    *,
    active_task: MoveTask,
) -> None:
    active_payload = dict(active_task.payload or {})
    pallet_code = str(active_payload.get("pallet_code") or active_task.pallet_code or "").strip()
    source_location_code = str(active_payload.get("source_location_scan_code") or "").strip()
    if not pallet_code:
        return
    for task in tasks:
        if task.pk == active_task.pk or task.status == MoveTask.STATUS_DONE:
            continue
        payload = dict(task.payload or {})
        if not _scan_equal(payload.get("pallet_code") or task.pallet_code, pallet_code):
            continue
        if source_location_code and not _scan_equal(
            payload.get("source_location_scan_code"), source_location_code
        ):
            continue
        execution = dict(payload.get("mobile_execution") or {})
        if execution.get("pallet_confirmed"):
            continue
        execution["pallet_confirmed"] = True
        execution["last_scan"] = pallet_code
        payload["mobile_execution"] = execution
        task.payload = payload
        task.save(update_fields=["payload", "updated_at"])


@transaction.atomic
def scan_fbs_box_collection_request(
    *,
    legacy_order_ids,
    scan_value: str,
    user,
    employee_id: int,
    employee_name: str,
):
    tasks = _load_box_collection_tasks(legacy_order_ids, for_update=True)
    if not is_fbs_box_collection_batch(tasks):
        return _command_result(ok=False, error="Коробочная FBS-заявка не найдена.")
    snapshot_before = build_fbs_box_collection_request_snapshot(
        legacy_order_ids,
        employee_id=employee_id,
    )
    if not snapshot_before.get("can_scan"):
        return _command_result(
            ok=False,
            error="Сначала возьмите всю FBS-заявку в работу.",
        )
    active_order_id = str(snapshot_before.get("active_order_id") or "").strip()
    active_task = next(
        (
            task
            for task in tasks
            if str(task.legacy_order_id or "").strip() == active_order_id
        ),
        None,
    )
    if active_task is None:
        return _command_result(ok=False, error="Активный короб FBS-заявки не найден.")

    active_group_order_ids = {
        str(value or "").strip()
        for value in snapshot_before.get("active_group_order_ids", [])
        if str(value or "").strip()
    }
    active_group_tasks = [
        task
        for task in tasks
        if str(task.legacy_order_id or "").strip() in active_group_order_ids
    ]
    if not active_group_tasks:
        active_group_tasks = [active_task]

    active_step = str(snapshot_before.get("current_step") or "").strip()
    active_payload = dict(active_task.payload or {})
    full_pallet_destination = bool(
        active_step == "destination"
        and active_payload.get(FULL_SOURCE_PALLET_MARKER)
    )
    palletized_group_destination = bool(
        active_step == "destination"
        and snapshot_before.get("fbs_palletized_box_batch")
        and not full_pallet_destination
    )
    if full_pallet_destination:
        pallet_code = str(
            active_payload.get("pallet_code") or active_task.pallet_code or ""
        ).strip()
        pallet_tasks = [
            task
            for task in tasks
            if task.status != MoveTask.STATUS_DONE
            and dict(task.payload or {}).get(FULL_SOURCE_PALLET_MARKER)
            and _scan_equal(
                dict(task.payload or {}).get("pallet_code") or task.pallet_code,
                pallet_code,
            )
        ]
        allocation_ids = [
            int(value)
            for task in pallet_tasks
            for value in dict(task.payload or {}).get("fbs_allocation_ids", [])
            if str(value or "").strip().isdigit()
        ]
        from fbs.services.replenishment import (
            complete_replenishment_pallet_allocations,
        )

        try:
            complete_replenishment_pallet_allocations(
                allocation_ids=allocation_ids,
                source_pallet_scan=pallet_code,
                target_scan=scan_value,
                performed_by=user,
            )
        except FbsReplenishmentError as exc:
            guide = dict(snapshot_before.get("destination_guide") or {})
            recommendations = []
            for row in [
                *list(guide.get("client_places") or []),
                *list(guide.get("free_places") or []),
            ]:
                if not isinstance(row, dict) or row.get("has_space") is False:
                    continue
                code = str(row.get("code") or "").strip()
                if code and code not in recommendations:
                    recommendations.append(code)
            suffix = (
                " Водитель может выбрать любую другую действующую ячейку OS."
                + (
                    f" Рекомендации: {', '.join(recommendations[:4])}."
                    if recommendations
                    else ""
                )
            )
            return _exact_text_command_result(ok=False, error=f"{exc}{suffix}")
        active_task.refresh_from_db()
        result = _exact_text_command_result(
            ok=True,
            task=active_task,
            payload=dict(active_task.payload or {}),
            message=f"Паллета {pallet_code} перемещена в FBS целиком.",
            completed=True,
        )
    elif palletized_group_destination:
        try:
            with transaction.atomic():
                for group_task in active_group_tasks:
                    group_task.refresh_from_db()
                    group_snapshot = build_fbs_mobile_execution_snapshot(group_task)
                    if str(group_snapshot.get("current_step") or "") != "destination":
                        raise FbsReplenishmentError(
                            "Сборочная паллета проведена не полностью: "
                            "не все короба подтверждены сканированием."
                        )
                    group_result = scan_fbs_move_task(
                        task=group_task,
                        scan_value=scan_value,
                        user=user,
                        employee_id=employee_id,
                        employee_name=employee_name,
                    )
                    if not group_result.ok:
                        raise FbsReplenishmentError(
                            str(group_result.error or "Не удалось разместить короб FBS.")
                        )
        except FbsReplenishmentError as exc:
            return _exact_text_command_result(ok=False, error=str(exc))
        except Exception:
            logger.exception(
                "fbs_collection_pallet_completion_failed request_id=%s "
                "task_count=%s destination_scan=%s",
                active_task.request_id,
                len(active_group_tasks),
                scan_value,
            )
            return _exact_text_command_result(
                ok=False,
                error=(
                    "Не удалось разместить сборочную паллету FBS. "
                    "Все изменения отменены; повторите после проверки."
                ),
            )
        active_task.refresh_from_db()
        result = _exact_text_command_result(
            ok=True,
            task=active_task,
            payload=dict(active_task.payload or {}),
            message=(
                f"Сборочная паллета "
                f"{snapshot_before.get('active_target_pallet_code') or ''} "
                f"размещена: {_box_count_label(len(active_group_tasks))}."
            ),
            completed=True,
        )
    else:
        result = scan_fbs_move_task(
            task=active_task,
            scan_value=scan_value,
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
        )
    if not result.ok:
        return result
    if active_step == "pallet":
        active_task.refresh_from_db()
        _confirm_same_pallet_tasks(active_group_tasks, active_task=active_task)

    snapshot_after = build_fbs_box_collection_request_snapshot(
        legacy_order_ids,
        employee_id=employee_id,
    )
    total_count = int(snapshot_after.get("total_count") or 0)
    collected_count = int(snapshot_after.get("collected_count") or 0)
    skipped_count = int(snapshot_after.get("skipped_count") or 0)
    placed_count = int(snapshot_after.get("placed_count") or 0)
    if not snapshot_after.get("remaining_count"):
        if skipped_count:
            return _command_result(
                ok=True,
                message=(
                    f"Размещено {collected_count} из {total_count}; "
                    f"{skipped_count} отсутствует и отправлен на проверку."
                ),
                completed=True,
            )
        return _command_result(
            ok=True,
            message=f"Все {_box_count_label(total_count)} FBS размещены.",
            completed=True,
        )
    if active_step == "destination":
        return _exact_text_command_result(
            ok=True,
            message=(
                (
                    "Паллета перемещена целиком. "
                    if full_pallet_destination
                    else "Сборочная паллета размещена. "
                    if palletized_group_destination
                    else "Короб размещен. "
                )
                + f"Размещено коробов {placed_count} из {total_count}. "
                + (
                    "Отсканируйте следующую паллету."
                    if full_pallet_destination or palletized_group_destination
                    else "Отсканируйте ячейку для следующего собранного короба."
                )
            ),
            completed=False,
        )
    if (
        snapshot_before.get("fbs_palletized_box_batch")
        and active_step in {"pallet", "boxes"}
        and str(snapshot_after.get("current_step") or "") == "destination"
        and int(snapshot_after.get("active_group_collected_count") or 0)
        == int(snapshot_after.get("active_group_box_count") or 0)
    ):
        return _exact_text_command_result(
            ok=True,
            message=(
                f"Сборочная паллета "
                f"{snapshot_after.get('active_target_pallet_code') or ''} собрана: "
                f"{_box_count_label(int(snapshot_after.get('active_group_box_count') or 0))}. "
                "Отвезите её в FBS и отсканируйте ячейку OS для размещения."
            ),
        )
    if snapshot_after.get("all_collected") and not snapshot_before.get("all_collected"):
        if skipped_count:
            return _exact_text_command_result(
                ok=True,
                message=(
                    f"Собрано {collected_count} из {total_count}; "
                    f"{skipped_count} отсутствует. Отвезите собранные короба в зону FBS."
                ),
            )
        return _exact_text_command_result(
            ok=True,
            message=(
                f"Все {_box_count_label(total_count)} собраны. "
                "Отвезите партию в зону FBS и начинайте размещение."
            ),
        )
    return _exact_text_command_result(
        ok=True,
        message=(
            f"{result.message or 'Скан подтвержден.'} "
            f"Собрано коробов {collected_count} из {total_count}."
        ),
    )


def _command_result(**kwargs):
    MoveTaskCommandResult = import_module(
        "reach" + "truck.services.task_commands"
    ).MoveTaskCommandResult
    return MoveTaskCommandResult(**kwargs)


def _exact_text_command_result(**kwargs):
    """Preserves already-correct Russian text from this FBS flow."""
    result = _command_result(**kwargs)
    if "error" in kwargs:
        result.error = str(kwargs.get("error") or "")
    if "message" in kwargs:
        result.message = str(kwargs.get("message") or "")
    return result


def _location_payload(location: WarehouseLocation | None) -> dict:
    if location is None:
        return {"zone": "", "row": 0, "section": 0, "tier": 0, "cell": 0}
    return {
        "zone": str(location.zone_code or "").strip().upper(),
        "code": _location_scan_code(location),
        "row": int(location.row_no or 0),
        "section": int(location.section_no or 0),
        "tier": int(location.tier_no or 0),
        "cell": int(location.cell_no or 0),
    }


def _location_scan_code(location: WarehouseLocation | None) -> str:
    if location is None:
        return ""
    if (
        str(location.zone_code or "").strip().upper() == "OS"
        and str(location.location_code or "").strip().upper() == "A-B"
    ):
        return str(location.location_code or "").strip()
    if str(location.zone_code or "").strip().upper() == "OS":
        return os_location_code(
            row=int(location.row_no or 0),
            section=int(location.section_no or 0),
            tier=int(location.tier_no or 0),
            cell=int(location.cell_no or 0),
        )
    return str(location.location_code or location.zone_code or "").strip()


def _prepared_flow_destination(payload: dict) -> WarehouseLocation | None:
    plan_id = int(payload.get("fbs_plan_id") or 0)
    if plan_id <= 0:
        return None
    plan = (
        FbsReplenishmentPlan.objects.select_related("staging_location")
        .filter(pk=plan_id)
        .first()
    )
    return plan.staging_location if plan is not None else None


def _resolve_active_pr_location(scan_value: str) -> WarehouseLocation | None:
    scan = str(scan_value or "").strip()
    if not scan:
        return None
    return (
        WarehouseLocation.objects.filter(
            location_code__iexact=scan,
            zone_code__iexact="PR",
            is_active=True,
        )
        .order_by("id")
        .first()
    )


def _prepared_flow_location_locked(
    prepared_boxes: Iterable[FbsReplenishmentPreparedBox],
) -> bool:
    return any(
        int(prepared_box.scanned_qty or 0) > 0
        or prepared_box.status != FbsReplenishmentPreparedBox.STATUS_PRINTED
        for prepared_box in prepared_boxes
    )


def _select_prepared_flow_destination(
    *,
    task: MoveTask,
    payload: dict,
    scan_value: str,
) -> tuple[WarehouseLocation, bool]:
    """Select a free PR work place until the first prepared-box item is scanned."""
    plan_id = int(payload.get("fbs_plan_id") or 0)
    if plan_id <= 0:
        raise FbsReplenishmentError("У задания не найден план перемещения FBS.")
    plan = (
        FbsReplenishmentPlan.objects.select_for_update(of=("self",))
        .select_related("staging_location")
        .get(pk=plan_id)
    )
    prepared_boxes = list(
        FbsReplenishmentPreparedBox.objects.select_for_update(of=("self",))
        .select_related("physical_container")
        .filter(plan=plan)
        .order_by("id")
    )
    current = plan.staging_location
    current_code = _location_scan_code(current)
    if _prepared_flow_location_locked(prepared_boxes):
        if not current or not _scan_equal(scan_value, current_code):
            raise FbsReplenishmentError(
                "Место уже закреплено после начала заполнения FBS-короба. "
                f"Отсканируйте {current_code or 'назначенное место'}."
            )
        return current, False

    destination = _resolve_active_pr_location(scan_value)
    if destination is None:
        raise FbsReplenishmentError(
            "Ожидается QR любого активного места PR для свободного размещения."
        )
    try:
        from sklad.services.operational_locations import (
            require_concrete_movement_location,
        )

        require_concrete_movement_location(
            destination,
            purpose="перемещения FBS",
        )
    except ValidationError as exc:
        raise FbsReplenishmentError("; ".join(exc.messages)) from exc
    if current and destination.id == current.id:
        return destination, False

    plan.staging_location = destination
    plan.save(update_fields=["staging_location", "updated_at"])
    for prepared_box in prepared_boxes:
        physical_box = prepared_box.physical_container
        physical_box.current_location = destination
        physical_box.save(update_fields=["current_location", "updated_at"])

    destination_code = _location_scan_code(destination)
    destination_payload = _location_payload(destination)
    destination_label = _location_label(destination)
    open_tasks = list(
        _bridge_tasks_for_plan(plan)
        .select_for_update(of=("self",))
        .filter(status__in=(MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS))
        .order_by("id")
    )
    request_ids: set[int] = set()
    for open_task in open_tasks:
        open_payload = dict(open_task.payload or {})
        if not open_payload.get(PREPARED_FLOW_MARKER):
            continue
        open_payload.update(
            {
                "destination_scan_code": destination_code,
                "to_location": destination_payload,
                "to_label": destination_label,
            }
        )
        open_task.to_zone = str(destination.zone_code or "PR")
        open_task.to_row = int(destination.row_no or 0) or None
        open_task.to_section = int(destination.section_no or 0) or None
        open_task.to_tier = int(destination.tier_no or 0) or None
        open_task.to_cell = int(destination.cell_no or 0) or None
        open_task.payload = open_payload
        open_task.save(
            update_fields=[
                "to_zone",
                "to_row",
                "to_section",
                "to_tier",
                "to_cell",
                "payload",
                "updated_at",
            ]
        )
        if open_task.request_id:
            request_ids.add(int(open_task.request_id))
        if open_task.id == task.id:
            payload.update(open_payload)
    if request_ids:
        MoveRequest.objects.filter(id__in=request_ids).update(
            destination_zone=str(destination.zone_code or "PR"),
            destination_row=int(destination.row_no or 0) or None,
            destination_section=int(destination.section_no or 0) or None,
            destination_tier=int(destination.tier_no or 0) or None,
            destination_cell=int(destination.cell_no or 0) or None,
        )
    return destination, True


def _location_label(location: WarehouseLocation | None) -> str:
    if location is None:
        return "Основной склад"
    if str(location.zone_code or "").strip().upper() == "OS":
        return os_location_label(
            row=int(location.row_no or 0),
            section=int(location.section_no or 0),
            tier=int(location.tier_no or 0),
            cell=int(location.cell_no or 0),
        )
    return str(location)


@transaction.atomic
def sync_internal_floor_movement_reachtruck_task(
    movement: FbsInternalMovement,
) -> MoveTask:
    """Expose a confirmed upper-tier -> first-tier FBS move to the driver UI."""
    from fbs.services.movements import is_floor_replenishment_movement

    movement = (
        FbsInternalMovement.objects.select_for_update(of=("self",))
        .select_related(
            "agency",
            "requested_by",
            "source_box__pallet__cell__location",
            "target_pallet__cell__location",
        )
        .get(pk=movement.pk)
    )
    if not is_floor_replenishment_movement(movement):
        raise FbsMovementError("Перемещение не является заявкой пополнения первого яруса.")
    if movement.mode != FbsInternalMovement.MODE_BOX:
        raise FbsMovementError("На первый ярус можно направить только целый FBS-короб.")
    if movement.status not in (
        FbsInternalMovement.STATUS_PROPOSED,
        FbsInternalMovement.STATUS_IN_PROGRESS,
    ):
        raise FbsMovementError("Перемещение первого яруса уже недоступно.")

    source_box = movement.source_box
    source_pallet = source_box.pallet
    source_cell = source_pallet.cell
    source_location = source_cell.location
    target_pallet = movement.target_pallet
    target_cell = target_pallet.cell
    target_location = target_cell.location
    if int(source_location.tier_no or 0) <= 1 or int(target_location.tier_no or 0) != 1:
        raise FbsMovementError("Маршрут пополнения должен вести с верхнего на первый ярус.")

    context_id = f"fbs-floor-movement:{movement.id}"
    request_row = (
        MoveRequest.objects.select_for_update()
        .filter(context_type=MoveRequest.CONTEXT_MANUAL, context_id=context_id)
        .order_by("id")
        .first()
    )
    requested_by_name = str(movement.requested_by or "").strip()
    if movement.requested_by_id:
        requested_by_name = (
            str(movement.requested_by.get_full_name() or "").strip()
            or str(movement.requested_by)
        )
    if request_row is None:
        request_row = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id=context_id,
            process=MoveRequest.PROCESS_FBS,
            agency=movement.agency,
            requested_by=movement.requested_by,
            requested_by_role="storekeeper",
            requested_by_name=requested_by_name,
            destination_zone=str(target_location.zone_code or "OS"),
            destination_row=int(target_location.row_no or 0) or None,
            destination_section=int(target_location.section_no or 0) or None,
            destination_tier=int(target_location.tier_no or 0) or None,
            destination_cell=int(target_location.cell_no or 0) or None,
            priority=MoveRequest.PRIORITY_HIGH,
            comment=str(movement.comment or ""),
            status=MoveRequest.STATUS_PLANNED,
        )

    legacy_id = f"{FLOOR_REPLENISHMENT_LEGACY_PREFIX}{movement.id}"
    task = (
        MoveTask.objects.select_for_update()
        .filter(legacy_order_id=legacy_id)
        .order_by("id")
        .first()
    )
    source_scan = str(source_cell.warehouse_location_code or source_cell.cell_code).strip()
    destination_scan = str(
        target_cell.warehouse_location_code or target_cell.cell_code
    ).strip()
    instruction = str(movement.comment or "").strip()
    payload = {
        BRIDGE_MARKER: True,
        FLOOR_REPLENISHMENT_MARKER: True,
        "fbs_internal_movement_id": movement.id,
        "task_category": "movement",
        "task_kind_label": "FBS · Пополнение первого яруса",
        "status": MoveTask.STATUS_CREATED,
        "status_label": "Ожидает перемещения",
        "pallet_code": source_pallet.pallet_code,
        "source_pallet_code": source_pallet.pallet_code,
        "source_box_code": source_box.box_code,
        "source_scan_code": source_box.box_code,
        "source_location_scan_code": source_scan,
        "destination_scan_code": destination_scan,
        "from_location": _location_payload(source_location),
        "to_location": _location_payload(target_location),
        "from_label": source_cell.warehouse_location_label,
        "to_label": target_cell.warehouse_location_label,
        "move_mode": MoveTask.MODE_BOX_FULL,
        "pick_mode": "full",
        "requested_qty": int(movement.planned_qty or 0),
        "requested_box_count": 1,
        "requested_boxes": [source_box.box_code],
        "requested_box": source_box.box_code,
        "planned_box_codes": [source_box.box_code],
        "selected_box_codes": [source_box.box_code],
        "instruction": instruction,
        "mobile_execution": {
            "source_location_confirmed": False,
            "box_confirmed": False,
            "destination_confirmed": False,
            "last_scan": "",
        },
    }
    if task is None:
        task = MoveTask.objects.create(
            request=request_row,
            pallet_code=source_pallet.pallet_code,
            from_zone=str(source_location.zone_code or ""),
            from_row=int(source_location.row_no or 0) or None,
            from_section=int(source_location.section_no or 0) or None,
            from_tier=int(source_location.tier_no or 0) or None,
            from_cell=int(source_location.cell_no or 0) or None,
            to_zone=str(target_location.zone_code or ""),
            to_row=int(target_location.row_no or 0) or None,
            to_section=int(target_location.section_no or 0) or None,
            to_tier=int(target_location.tier_no or 0) or None,
            to_cell=int(target_location.cell_no or 0) or None,
            move_mode=MoveTask.MODE_BOX_FULL,
            qty_planned=int(movement.planned_qty or 0),
            payload=payload,
            status=MoveTask.STATUS_CREATED,
            legacy_order_id=legacy_id,
        )
    elif task.status != MoveTask.STATUS_DONE:
        payload["mobile_execution"] = dict(task.payload or {}).get(
            "mobile_execution"
        ) or payload["mobile_execution"]
        payload["status"] = task.status
        payload["status_label"] = (
            "В работе"
            if task.status == MoveTask.STATUS_IN_PROGRESS
            else "Ожидает перемещения"
        )
        task.request = request_row
        task.qty_planned = int(movement.planned_qty or 0)
        task.payload = payload
        task.save(update_fields=["request", "qty_planned", "payload", "updated_at"])
    _sync_request(request_row)
    return task


def _payload_source_scan_code(payload: dict) -> str:
    location = payload.get("from_location") or {}
    if str(location.get("zone") or "").strip().upper() == "OS":
        return os_location_code(
            row=int(location.get("row") or 0),
            section=int(location.get("section") or 0),
            tier=int(location.get("tier") or 0),
            cell=int(location.get("cell") or 0),
        )
    return str(payload.get("source_location_scan_code") or "").strip()


def _payload_source_label(payload: dict) -> str:
    location = payload.get("from_location") or {}
    if str(location.get("zone") or "").strip().upper() == "OS":
        return os_location_label(
            row=int(location.get("row") or 0),
            section=int(location.get("section") or 0),
            tier=int(location.get("tier") or 0),
            cell=int(location.get("cell") or 0),
        )
    return str(payload.get("from_label") or "").strip()


def _source_pallet(container: WarehouseContainer) -> WarehouseContainer:
    parent = getattr(container, "parent_container", None)
    if parent and parent.container_type in {
        WarehouseContainer.TYPE_PALLET,
        WarehouseContainer.TYPE_MIXED_PALLET,
    }:
        return parent
    return container


def _full_source_pallet_id(plan: FbsReplenishmentPlan) -> int:
    comment = str(plan.comment or "")
    if not comment.startswith(FULL_SOURCE_PALLET_COMMENT_PREFIX):
        return 0
    token = comment[len(FULL_SOURCE_PALLET_COMMENT_PREFIX) :].split("]", 1)[0]
    try:
        return int(token)
    except (TypeError, ValueError):
        return 0


def _movement_context_id(plan: FbsReplenishmentPlan) -> str:
    request_id = int(plan.client_movement_request_id or 0)
    return f"fbs-movement:{request_id or plan.id}"


def _legacy_id(plan: FbsReplenishmentPlan, allocations: list[FbsReplenishmentAllocation]) -> str:
    if plan.mode == FbsReplenishmentPlan.MODE_BOX:
        return f"{LEGACY_PLAN_PREFIX}{plan.id}"
    return f"{LEGACY_ALLOCATION_PREFIX}{allocations[0].id}"


def _task_specs(
    plan: FbsReplenishmentPlan,
    allocations: list[FbsReplenishmentAllocation],
) -> list[list[FbsReplenishmentAllocation]]:
    if plan.mode == FbsReplenishmentPlan.MODE_BOX:
        return [allocations]
    specs: list[list[FbsReplenishmentAllocation]] = []
    reserved_groups: dict[tuple[int, int], list[FbsReplenishmentAllocation]] = {}
    for allocation in allocations:
        if allocation.status != FbsReplenishmentAllocation.STATUS_RESERVED:
            specs.append([allocation])
            continue
        group_key = (
            int(allocation.source_snapshot.container_id or 0),
            int(allocation.line_id or 0),
        )
        group = reserved_groups.get(group_key)
        if group is None:
            group = []
            reserved_groups[group_key] = group
            specs.append(group)
        group.append(allocation)
    return specs


def _request_status(request_row: MoveRequest) -> str:
    task_rows = list(request_row.tasks.values("status", "payload"))
    statuses = {str(row.get("status") or "") for row in task_rows}
    if not statuses:
        return MoveRequest.STATUS_CREATED
    if task_rows and all(
        row.get("status") == MoveTask.STATUS_DONE
        or (
            row.get("status") == MoveTask.STATUS_CANCELED
            and dict(row.get("payload") or {}).get("missing_box_skipped_v1")
        )
        for row in task_rows
    ):
        return MoveRequest.STATUS_DONE
    if statuses <= {MoveTask.STATUS_DONE}:
        return MoveRequest.STATUS_DONE
    if statuses <= {MoveTask.STATUS_CANCELED}:
        return MoveRequest.STATUS_CANCELED
    if MoveTask.STATUS_IN_PROGRESS in statuses:
        return MoveRequest.STATUS_IN_PROGRESS
    if MoveTask.STATUS_DONE in statuses:
        return MoveRequest.STATUS_PARTIAL
    return MoveRequest.STATUS_PLANNED


def _sync_request(request_row: MoveRequest) -> None:
    status = _request_status(request_row)
    if request_row.status != status:
        request_row.status = status
        request_row.save(update_fields=["status", "updated_at"])


def _task_payload(
    *,
    plan: FbsReplenishmentPlan,
    allocations: list[FbsReplenishmentAllocation],
    source_container: WarehouseContainer,
    source_pallet: WarehouseContainer,
    destination: WarehouseLocation,
) -> dict:
    first = allocations[0]
    snapshot = first.source_snapshot
    movement = plan.client_movement_request
    qty = sum(int(allocation.qty_planned or 0) for allocation in allocations)
    item_mode = plan.mode == FbsReplenishmentPlan.MODE_ITEM
    full_source_pallet_id = _full_source_pallet_id(plan)
    full_source_pallet = bool(
        not item_mode
        and source_pallet.id != source_container.id
        and full_source_pallet_id == source_pallet.id
    )
    source_location = _task_source_physical_location(
        source_pallet if full_source_pallet else source_container
    )
    prepared_box_codes = list(
        plan.prepared_boxes.exclude(
            status=FbsReplenishmentPreparedBox.STATUS_UNUSED
        ).values_list("box_code", flat=True)
    ) if item_mode else []
    prepared_box_flow = bool(prepared_box_codes)
    barcode = str(first.line.barcode or snapshot.barcode or "").strip()
    # FBS movements confirm every unit by the product barcode. Data Matrix is
    # deliberately not requested here: ChZ verification belongs to receiving,
    # processing and shipment controls, not to an internal warehouse move.
    marking_scan_required = False
    if item_mode and not barcode:
        raise FbsReplenishmentError("Для штучного FBS-задания не найден ШК товара.")
    destination_scan = _location_scan_code(destination) if item_mode else ""
    staging_container_code = (
        movement_staging_container_code(plan.client_movement_request_id)
        if item_mode and plan.client_movement_request_id and not prepared_box_flow
        else ""
    )
    source_qty = sum(int(allocation.source_snapshot.qty or 0) for allocation in allocations)
    payload = {
        BRIDGE_MARKER: True,
        "fbs_plan_id": plan.id,
        "fbs_allocation_ids": [allocation.id for allocation in allocations],
        "fbs_movement_id": int(plan.client_movement_request_id or 0),
        "fbs_movement_number": str(getattr(movement, "number", "") or f"FBS-{plan.id}"),
        PALLETIZED_BOX_BATCH_MARKER: not item_mode,
        "fbs_target_pallet_id": int(plan.target_pallet_id or 0),
        "fbs_target_pallet_code": str(plan.target_pallet.pallet_code or "").strip(),
        "fbs_target_pallet_capacity": int(plan.target_pallet.max_boxes or 0),
        "task_category": "movement",
        "task_kind_label": "Перемещение в FBS",
        "status": MoveTask.STATUS_CREATED,
        "status_label": "Ожидает перевозки",
        "pallet_code": source_pallet.container_code,
        "source_box_code": source_container.container_code,
        "source_scan_code": source_container.container_code,
        "source_location_scan_code": _location_scan_code(source_location),
        "destination_scan_code": destination_scan,
        DYNAMIC_OS_DESTINATION_MARKER: not item_mode,
        "fbs_staging_container_code": staging_container_code,
        "from_location": _location_payload(source_location),
        "to_location": (
            _location_payload(destination)
            if item_mode
            else {"zone": "OS", "row": 0, "section": 0, "tier": 0, "cell": 0}
        ),
        "from_label": _location_label(source_location),
        "to_label": (
            _location_label(destination)
            if item_mode
            else "Свободная ячейка OS или точная PR-ячейка"
        ),
        "move_mode": (
            MoveTask.MODE_BOX_PARTIAL if item_mode else MoveTask.MODE_BOX_FULL
        ),
        "pick_mode": "partial" if item_mode else "full",
        "requested_qty": qty,
        "requested_box_count": 1,
        "requested_boxes": [source_container.container_code],
        "requested_box": source_container.container_code,
        "planned_box_codes": [source_container.container_code],
        "selected_box_codes": [source_container.container_code],
        "requested_barcode_qty": {barcode: qty} if item_mode and barcode else {},
        "fbs_unit_scan_code": barcode if item_mode else "",
        MARKING_SCAN_MARKER: marking_scan_required,
        "fbs_source_qty": source_qty,
        "instruction": (
            f"Отбери {qty} шт. из короба {source_container.container_code} и передай в FBS."
            if item_mode
            else (
                f"Перемести короб {source_container.container_code} в FBS и "
                "отсканируй выбранную ячейку OS или точную ячейку PR."
            )
        ),
        "mobile_execution": {
            "pallet_confirmed": False,
            "box_confirmed": False,
            "units_scanned_qty": 0,
            "staging_container_confirmed": False,
            "staging_container_scan": "",
            "destination_confirmed": False,
            "last_scan": "",
        },
    }
    if marking_scan_required:
        payload["mobile_execution"].update(
            {
                "pending_marking_scan": False,
                "pending_product_barcode": "",
            }
        )
    if prepared_box_flow:
        payload.update(
            {
                PREPARED_FLOW_MARKER: True,
                "fbs_prepared_box_codes": prepared_box_codes,
                "fbs_staging_container_code": "",
                "to_label": _location_label(destination),
                "instruction": (
                    f"Доставь короб {source_container.container_code} в место "
                    f"{_location_label(destination)}, отсканируй QR места, затем "
                    f"отбери {qty} шт. в постоянный FBS-короб."
                ),
            }
        )
        payload["mobile_execution"].update(
            {
                "active_prepared_box_id": 0,
                "active_prepared_box_code": "",
                "closed_prepared_box_codes": [],
            }
        )
    if item_mode:
        payload["partial_pick_patterns"] = [
            {
                "source_box_qty": source_qty,
                "requested_box_count": 1,
                "barcode_qty": {barcode: qty} if barcode else {},
                "pick_qty": qty,
                "requested_article": str(snapshot.sku_code or "").strip(),
                "requested_barcodes": [barcode] if barcode else [],
            }
        ]
    if full_source_pallet:
        payload.update(
            {
                FULL_SOURCE_PALLET_MARKER: True,
                "fbs_source_pallet_id": source_pallet.id,
                "task_kind_label": "Перемещение паллеты в FBS",
                "source_scan_code": source_pallet.container_code,
                "move_mode": MoveTask.MODE_PALLET_FULL,
                "instruction": (
                    f"Отсканируйте паллету {source_pallet.container_code}, "
                    "переместите ее целиком в FBS и отсканируйте выбранную ячейку OS."
                ),
            }
        )
        payload["mobile_execution"]["box_confirmed"] = True
    return payload


@transaction.atomic
def sync_plan_reachtruck_tasks(
    plan: FbsReplenishmentPlan,
    *,
    sync_request: bool = True,
) -> tuple[MoveTask, ...]:
    if not plan.client_movement_request_id:
        return ()
    plan = (
        FbsReplenishmentPlan.objects.select_for_update(of=("self",))
        .select_related(
            "agency",
            "client_movement_request",
            "staging_location",
            "target_cell__location",
        )
        .get(pk=plan.pk)
    )
    allocations = list(
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "line__sku_ref",
            "source_snapshot__container__current_location",
            "source_snapshot__container__parent_container__current_location",
            "source_snapshot__location",
        )
        .filter(line__plan=plan)
        .order_by("id")
    )
    if not allocations:
        return ()
    destination = (
        plan.staging_location
        if plan.mode == FbsReplenishmentPlan.MODE_ITEM and plan.staging_location_id
        else plan.target_cell.location
    )
    dynamic_destination = plan.mode == FbsReplenishmentPlan.MODE_BOX
    destination_zone = "OS" if dynamic_destination else str(destination.zone_code or "")
    destination_row = None if dynamic_destination else int(destination.row_no or 0) or None
    destination_section = (
        None if dynamic_destination else int(destination.section_no or 0) or None
    )
    destination_tier = None if dynamic_destination else int(destination.tier_no or 0) or None
    destination_cell = None if dynamic_destination else int(destination.cell_no or 0) or None
    context_id = _movement_context_id(plan)
    move_request = (
        MoveRequest.objects.select_for_update()
        .filter(context_type=MoveRequest.CONTEXT_MANUAL, context_id=context_id)
        .order_by("id")
        .first()
    )
    if move_request is None:
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id=context_id,
            process=MoveRequest.PROCESS_FBS,
            agency=plan.agency,
            requested_by=plan.confirmed_by,
            requested_by_role="storekeeper",
            requested_by_name=str(plan.confirmed_by or ""),
            destination_zone=destination_zone,
            destination_row=destination_row,
            destination_section=destination_section,
            destination_tier=destination_tier,
            destination_cell=destination_cell,
            priority=MoveRequest.PRIORITY_NORMAL,
            comment=f"FBS-перемещение {plan.client_movement_request.number}",
            status=MoveRequest.STATUS_PLANNED,
        )
    elif dynamic_destination and (
        move_request.destination_zone != "OS"
        or move_request.destination_row is not None
        or move_request.destination_section is not None
        or move_request.destination_tier is not None
        or move_request.destination_cell is not None
    ):
        move_request.destination_zone = "OS"
        move_request.destination_row = None
        move_request.destination_section = None
        move_request.destination_tier = None
        move_request.destination_cell = None
        move_request.save(
            update_fields=[
                "destination_zone",
                "destination_row",
                "destination_section",
                "destination_tier",
                "destination_cell",
                "updated_at",
            ]
        )
    tasks: list[MoveTask] = []
    for allocation_group in _task_specs(plan, allocations):
        source_container = allocation_group[0].source_snapshot.container
        if source_container is None:
            raise FbsReplenishmentError("У FBS-задания отсутствует исходный короб.")
        if any(
            allocation.source_snapshot.container_id != source_container.id
            for allocation in allocation_group
        ):
            raise FbsReplenishmentError("Один FBS-маршрут должен брать товар из одного короба.")
        source_pallet = _source_pallet(source_container)
        payload = _task_payload(
            plan=plan,
            allocations=allocation_group,
            source_container=source_container,
            source_pallet=source_pallet,
            destination=destination,
        )
        source_location_payload = dict(payload.get("from_location") or {})
        legacy_id = _legacy_id(plan, allocation_group)
        task = (
            MoveTask.objects.select_for_update()
            .filter(legacy_order_id=legacy_id)
            .order_by("id")
            .first()
        )
        if task is None:
            task = MoveTask.objects.create(
                request=move_request,
                pallet_code=source_pallet.container_code,
                from_zone=str(source_location_payload.get("zone") or ""),
                from_row=int(source_location_payload.get("row") or 0) or None,
                from_section=int(source_location_payload.get("section") or 0) or None,
                from_tier=int(source_location_payload.get("tier") or 0) or None,
                from_cell=int(source_location_payload.get("cell") or 0) or None,
                to_zone=destination_zone,
                to_row=destination_row,
                to_section=destination_section,
                to_tier=destination_tier,
                to_cell=destination_cell,
                move_mode=payload["move_mode"],
                qty_planned=sum(int(row.qty_planned or 0) for row in allocation_group),
                payload=payload,
                status=MoveTask.STATUS_CREATED,
                legacy_order_id=legacy_id,
            )
        elif task.status == MoveTask.STATUS_CREATED:
            previous_payload = dict(task.payload or {})
            progress = previous_payload.get("mobile_execution") or {}
            payload["mobile_execution"] = progress
            if MARKING_SCAN_MARKER not in previous_payload:
                payload.pop(MARKING_SCAN_MARKER, None)
            if PALLETIZED_BOX_BATCH_MARKER not in previous_payload:
                payload.pop(PALLETIZED_BOX_BATCH_MARKER, None)
            task.request = move_request
            task.pallet_code = source_pallet.container_code
            task.from_zone = str(source_location_payload.get("zone") or "")
            task.from_row = int(source_location_payload.get("row") or 0) or None
            task.from_section = int(source_location_payload.get("section") or 0) or None
            task.from_tier = int(source_location_payload.get("tier") or 0) or None
            task.from_cell = int(source_location_payload.get("cell") or 0) or None
            task.to_zone = destination_zone
            task.to_row = destination_row
            task.to_section = destination_section
            task.to_tier = destination_tier
            task.to_cell = destination_cell
            task.move_mode = payload["move_mode"]
            task.qty_planned = sum(int(row.qty_planned or 0) for row in allocation_group)
            task.payload = payload
            task.save(
                update_fields=[
                    "request",
                    "pallet_code",
                    "from_zone",
                    "from_row",
                    "from_section",
                    "from_tier",
                    "from_cell",
                    "to_zone",
                    "to_row",
                    "to_section",
                    "to_tier",
                    "to_cell",
                    "move_mode",
                    "qty_planned",
                    "payload",
                    "updated_at",
                ]
            )
        tasks.append(task)
    if sync_request:
        _sync_request(move_request)
    return tuple(tasks)


@transaction.atomic
def sync_client_movement_reachtruck_request(
    client_movement_request_id: int,
) -> MoveRequest | None:
    move_request = (
        MoveRequest.objects.select_for_update()
        .filter(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id=f"fbs-movement:{int(client_movement_request_id)}",
        )
        .order_by("id")
        .first()
    )
    if move_request is not None:
        _sync_request(move_request)
    return move_request


@transaction.atomic
def assert_client_movement_full_pallet_tasks_unstarted(
    client_movement_request_id: int,
) -> tuple[MoveTask, ...]:
    """Lock and validate tasks before replacing a pallet route with logical posting."""
    tasks = tuple(
        MoveTask.objects.select_for_update(of=("self",))
        .select_related("request")
        .filter(payload__fbs_movement_id=int(client_movement_request_id))
        .order_by("id")
    )
    if any(
        task.move_mode != MoveTask.MODE_PALLET_FULL
        or not dict(task.payload or {}).get(FULL_SOURCE_PALLET_MARKER)
        for task in tasks
    ):
        raise FbsReplenishmentError(
            "Заявка содержит отдельные короба и должна выполняться водителем ричтрака."
        )
    if any(
        task.status != MoveTask.STATUS_CREATED
        or task.assigned_to_id is not None
        or bool(str(task.assigned_to_name or "").strip())
        or task.started_at is not None
        for task in tasks
    ):
        raise FbsReplenishmentError(
            "Паллетное задание уже взято водителем ричтрака; автоматическая проводка запрещена."
        )
    return tasks


@transaction.atomic
def cancel_client_movement_full_pallet_tasks_after_logical_posting(
    client_movement_request_id: int,
) -> int:
    """Remove only the obsolete driver route after an atomic logical pallet posting."""
    tasks = list(
        MoveTask.objects.select_for_update(of=("self",))
        .filter(payload__fbs_movement_id=int(client_movement_request_id))
        .order_by("id")
    )
    if any(
        task.move_mode != MoveTask.MODE_PALLET_FULL
        or not dict(task.payload or {}).get(FULL_SOURCE_PALLET_MARKER)
        or task.assigned_to_id is not None
        or bool(str(task.assigned_to_name or "").strip())
        for task in tasks
    ):
        raise FbsReplenishmentError(
            "Задания ричтрака изменились во время проводки паллеты. Операция отменена."
        )

    now = timezone.now()
    request_ids = set()
    for task in tasks:
        payload = dict(task.payload or {})
        payload.update(
            {
                LOGICAL_FULL_PALLET_POSTING_MARKER: True,
                "status": MoveTask.STATUS_CANCELED,
                "status_label": "Не требуется: паллета логически принята в FBS",
            }
        )
        task.status = MoveTask.STATUS_CANCELED
        task.qty_done = 0
        task.assigned_to = None
        task.assigned_to_name = ""
        task.started_at = None
        task.completed_at = None
        task.canceled_at = now
        task.payload = payload
        task.save(
            update_fields=[
                "status",
                "qty_done",
                "assigned_to",
                "assigned_to_name",
                "started_at",
                "completed_at",
                "canceled_at",
                "payload",
                "updated_at",
            ]
        )
        request_ids.add(int(task.request_id))

    for request_row in MoveRequest.objects.select_for_update().filter(id__in=request_ids):
        _sync_request(request_row)
    return len(tasks)


@transaction.atomic
def sync_plan_reachtruck_placement(plan: FbsReplenishmentPlan) -> MoveTask:
    """Creates the second task: prepared FBS box -> driver-selected free OS cell."""
    plan = (
        FbsReplenishmentPlan.objects.select_for_update(of=("self",))
        .select_related(
            "agency",
            "client_movement_request",
            "staging_location",
            "target_box__source_container",
        )
        .get(pk=plan.pk)
    )
    if (
        not plan.client_movement_request_id
        or plan.mode != FbsReplenishmentPlan.MODE_ITEM
        or not plan.target_box_id
        or not plan.staging_location_id
    ):
        raise FbsReplenishmentError("План не готов к размещению FBS-короба.")
    context_id = _movement_context_id(plan)
    move_request = (
        MoveRequest.objects.select_for_update()
        .filter(context_type=MoveRequest.CONTEXT_MANUAL, context_id=context_id)
        .order_by("id")
        .first()
    )
    if move_request is None:
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id=context_id,
            process=MoveRequest.PROCESS_FBS,
            agency=plan.agency,
            requested_by=plan.confirmed_by,
            requested_by_role="storekeeper",
            requested_by_name=str(plan.confirmed_by or ""),
            destination_zone="OS",
            priority=MoveRequest.PRIORITY_NORMAL,
            comment=f"Размещение FBS-короба {plan.client_movement_request.number}",
            status=MoveRequest.STATUS_PLANNED,
        )
    legacy_id = f"{PLACEMENT_PLAN_PREFIX}{plan.id}"
    task = (
        MoveTask.objects.select_for_update()
        .filter(legacy_order_id=legacy_id)
        .order_by("id")
        .first()
    )
    payload = {
        BRIDGE_MARKER: True,
        "fbs_placement_task": True,
        "fbs_plan_id": plan.id,
        "fbs_movement_id": int(plan.client_movement_request_id),
        "fbs_movement_number": str(plan.client_movement_request.number),
        "task_category": "movement",
        "task_kind_label": "Размещение FBS-короба",
        "status": MoveTask.STATUS_CREATED,
        "status_label": "Ожидает размещения",
        "pallet_code": plan.target_box.box_code,
        "source_box_code": plan.target_box.box_code,
        "source_scan_code": plan.target_box.box_code,
        "source_location_scan_code": _location_scan_code(plan.staging_location),
        "destination_scan_code": "",
        "from_location": _location_payload(plan.staging_location),
        "to_location": {"zone": "OS", "row": 0, "section": 0, "tier": 0, "cell": 0},
        "from_label": "Зона подготовки FBS",
        "to_label": "Любая выбранная водителем ячейка действующей топологии OS",
        "move_mode": MoveTask.MODE_BOX_FULL,
        "pick_mode": "full",
        "requested_qty": int(plan.planned_qty or 0),
        "requested_box_count": 1,
        "requested_boxes": [plan.target_box.box_code],
        "requested_box": plan.target_box.box_code,
        "planned_box_codes": [plan.target_box.box_code],
        "selected_box_codes": [plan.target_box.box_code],
        "instruction": (
            f"Отсканируйте короб {plan.target_box.box_code}, отвезите к свободной "
            "ячейке OS и отсканируйте QR ячейки."
        ),
        "mobile_execution": {
            "box_confirmed": False,
            "destination_confirmed": False,
            "last_scan": "",
        },
    }
    if task is None:
        task = MoveTask.objects.create(
            request=move_request,
            pallet_code=plan.target_box.box_code,
            from_zone=str(plan.staging_location.zone_code or ""),
            from_row=int(plan.staging_location.row_no or 0) or None,
            from_section=int(plan.staging_location.section_no or 0) or None,
            from_tier=int(plan.staging_location.tier_no or 0) or None,
            from_cell=int(plan.staging_location.cell_no or 0) or None,
            to_zone="OS",
            move_mode=MoveTask.MODE_BOX_FULL,
            qty_planned=int(plan.planned_qty or 0),
            payload=payload,
            status=MoveTask.STATUS_CREATED,
            legacy_order_id=legacy_id,
        )
    elif task.status != MoveTask.STATUS_DONE:
        progress = dict(task.payload or {}).get("mobile_execution") or {}
        payload["mobile_execution"] = progress
        task.request = move_request
        task.pallet_code = plan.target_box.box_code
        task.qty_planned = int(plan.planned_qty or 0)
        task.payload = payload
        task.save(
            update_fields=[
                "request",
                "pallet_code",
                "qty_planned",
                "payload",
                "updated_at",
            ]
        )
    _sync_request(move_request)
    return task


def _prepared_flow_move_request(plan: FbsReplenishmentPlan) -> MoveRequest:
    context_id = _movement_context_id(plan)
    move_request = (
        MoveRequest.objects.select_for_update()
        .filter(context_type=MoveRequest.CONTEXT_MANUAL, context_id=context_id)
        .order_by("id")
        .first()
    )
    if move_request is None:
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id=context_id,
            process=MoveRequest.PROCESS_FBS,
            agency=plan.agency,
            requested_by=plan.confirmed_by,
            requested_by_role="storekeeper",
            requested_by_name=str(plan.confirmed_by or ""),
            destination_zone="OS",
            priority=MoveRequest.PRIORITY_NORMAL,
            comment=f"FBS-перемещение {plan.client_movement_request.number}",
            status=MoveRequest.STATUS_PLANNED,
        )
    return move_request


@transaction.atomic
def sync_plan_reachtruck_box_closure(plan: FbsReplenishmentPlan) -> MoveTask:
    plan = (
        FbsReplenishmentPlan.objects.select_for_update(of=("self",))
        .select_related("agency", "client_movement_request", "staging_location")
        .get(pk=plan.pk)
    )
    boxes = list(
        FbsReplenishmentPreparedBox.objects.select_for_update()
        .filter(
            plan=plan,
            status=FbsReplenishmentPreparedBox.STATUS_FILLING,
            scanned_qty__gt=0,
        )
        .order_by("sequence_no", "id")
    )
    if not boxes:
        raise FbsReplenishmentError("Нет заполненных коробов, ожидающих закрытия.")
    move_request = _prepared_flow_move_request(plan)
    legacy_id = f"{PREPARED_CLOSE_PREFIX}{plan.id}"
    task = MoveTask.objects.select_for_update().filter(legacy_order_id=legacy_id).first()
    box_codes = [box.box_code for box in boxes]
    payload = {
        BRIDGE_MARKER: True,
        "fbs_box_closure_task": True,
        "fbs_plan_id": plan.id,
        "fbs_movement_id": int(plan.client_movement_request_id),
        "fbs_movement_number": str(plan.client_movement_request.number),
        "task_category": "movement",
        "task_kind_label": "Закрытие FBS-коробов",
        "status": MoveTask.STATUS_CREATED,
        "status_label": "Ожидает закрытия коробов",
        "pallet_code": box_codes[0],
        "source_box_code": box_codes[0],
        "source_scan_code": box_codes[0],
        "source_location_scan_code": _location_scan_code(plan.staging_location),
        "destination_scan_code": "",
        "from_location": _location_payload(plan.staging_location),
        "to_location": _location_payload(plan.staging_location),
        "from_label": "Зона подготовки FBS",
        "to_label": "Закрытые постоянные FBS-короба",
        "move_mode": MoveTask.MODE_BOX_FULL,
        "pick_mode": "full",
        "requested_qty": sum(int(box.scanned_qty or 0) for box in boxes),
        "requested_box_count": len(boxes),
        "requested_boxes": box_codes,
        "planned_box_codes": box_codes,
        "selected_box_codes": box_codes,
        "instruction": "Повторно отсканируйте QR каждого заполненного короба, чтобы закрыть его.",
        "mobile_execution": {"closed_box_codes": [], "last_scan": ""},
    }
    if task is None:
        task = MoveTask.objects.create(
            request=move_request,
            pallet_code=box_codes[0],
            from_zone=str(plan.staging_location.zone_code or ""),
            move_mode=MoveTask.MODE_BOX_FULL,
            qty_planned=payload["requested_qty"],
            payload=payload,
            status=MoveTask.STATUS_CREATED,
            legacy_order_id=legacy_id,
        )
    elif task.status != MoveTask.STATUS_DONE:
        progress = dict(task.payload or {}).get("mobile_execution") or {}
        payload["mobile_execution"] = progress
        task.qty_planned = payload["requested_qty"]
        task.payload = payload
        task.save(update_fields=["qty_planned", "payload", "updated_at"])
    _sync_request(move_request)
    return task


@transaction.atomic
def sync_plan_reachtruck_prepared_box_placements(
    plan: FbsReplenishmentPlan,
) -> tuple[MoveTask, ...]:
    plan = (
        FbsReplenishmentPlan.objects.select_for_update(of=("self",))
        .select_related("agency", "client_movement_request", "staging_location")
        .get(pk=plan.pk)
    )
    boxes = list(
        FbsReplenishmentPreparedBox.objects.select_for_update()
        .filter(
            plan=plan,
            status=FbsReplenishmentPreparedBox.STATUS_CLOSED,
            scanned_qty__gt=0,
        )
        .order_by("sequence_no", "id")
    )
    if not boxes:
        return ()
    move_request = _prepared_flow_move_request(plan)
    tasks = []
    for prepared_box in boxes:
        legacy_id = f"{PREPARED_PLACEMENT_PREFIX}{prepared_box.id}"
        task = MoveTask.objects.select_for_update().filter(legacy_order_id=legacy_id).first()
        payload = {
            BRIDGE_MARKER: True,
            "fbs_prepared_box_placement_task": True,
            "fbs_plan_id": plan.id,
            "fbs_prepared_box_id": prepared_box.id,
            "fbs_movement_id": int(plan.client_movement_request_id),
            "fbs_movement_number": str(plan.client_movement_request.number),
            "task_category": "movement",
            "task_kind_label": "Размещение FBS-короба",
            "status": MoveTask.STATUS_CREATED,
            "status_label": "Ожидает размещения",
            "pallet_code": prepared_box.box_code,
            "source_box_code": prepared_box.box_code,
            "source_scan_code": prepared_box.box_code,
            "source_location_scan_code": _location_scan_code(plan.staging_location),
            "destination_scan_code": "",
            "from_location": _location_payload(plan.staging_location),
            "to_location": {"zone": "OS", "row": 0, "section": 0, "tier": 0, "cell": 0},
            "from_label": "Зона подготовки FBS",
            "to_label": "Свободная ячейка OS или FBS-паллета этого клиента",
            "move_mode": MoveTask.MODE_BOX_FULL,
            "pick_mode": "full",
            "requested_qty": int(prepared_box.scanned_qty or 0),
            "requested_box_count": 1,
            "requested_boxes": [prepared_box.box_code],
            "requested_box": prepared_box.box_code,
            "planned_box_codes": [prepared_box.box_code],
            "selected_box_codes": [prepared_box.box_code],
            "instruction": (
                f"Отсканируйте короб {prepared_box.box_code}, затем QR ячейки действующей топологии."
            ),
            "mobile_execution": {
                "box_confirmed": False,
                "destination_confirmed": False,
                "last_scan": "",
            },
        }
        if task is None:
            task = MoveTask.objects.create(
                request=move_request,
                pallet_code=prepared_box.box_code,
                from_zone=str(plan.staging_location.zone_code or ""),
                move_mode=MoveTask.MODE_BOX_FULL,
                qty_planned=int(prepared_box.scanned_qty or 0),
                payload=payload,
                status=MoveTask.STATUS_CREATED,
                legacy_order_id=legacy_id,
            )
        elif task.status != MoveTask.STATUS_DONE:
            progress = dict(task.payload or {}).get("mobile_execution") or {}
            payload["mobile_execution"] = progress
            task.qty_planned = int(prepared_box.scanned_qty or 0)
            task.payload = payload
            task.save(update_fields=["qty_planned", "payload", "updated_at"])
        tasks.append(task)
    _sync_request(move_request)
    return tuple(tasks)


def _bridge_tasks_for_plan(plan: FbsReplenishmentPlan):
    if not plan.client_movement_request_id:
        return MoveTask.objects.none()
    return MoveTask.objects.filter(
        request__context_type=MoveRequest.CONTEXT_MANUAL,
        request__context_id=_movement_context_id(plan),
        payload__fbs_plan_id=plan.id,
    )


@transaction.atomic
def sync_plans_reachtruck_assignment(
    plans: Iterable[FbsReplenishmentPlan],
    user,
) -> None:
    plan_ids = tuple(sorted({int(plan.id) for plan in plans if int(plan.id) > 0}))
    if not plan_ids:
        return
    employee = Employee.objects.filter(user=user, is_active=True).order_by("id").first()
    employee_id = int(employee.id) if employee else 0
    employee_name = str(getattr(employee, "full_name", "") or user.get_full_name() or user.get_username())
    tasks = list(
        MoveTask.objects.select_for_update(of=("self",))
        .select_related("request")
        .filter(
            request__context_type=MoveRequest.CONTEXT_MANUAL,
            payload__fbs_plan_id__in=plan_ids,
        )
        .order_by("id")
    )
    assigned_at = timezone.now()
    assigned_at_local = timezone.localtime(assigned_at).isoformat()
    updated_tasks: list[MoveTask] = []
    for task in tasks:
        if task.status in {MoveTask.STATUS_DONE, MoveTask.STATUS_CANCELED}:
            continue
        payload = dict(task.payload or {})
        payload.update(
            {
                "status": MoveTask.STATUS_IN_PROGRESS,
                "status_label": "В работе",
                "assigned_to_id": employee_id,
                "assigned_employee_id": employee_id,
                "assigned_to_name": employee_name,
                "taken_at": assigned_at_local,
            }
        )
        _assign_container_custody(
            task=task,
            payload=payload,
            employee_id=employee_id,
            employee_name=employee_name,
            assigned_at=assigned_at_local,
        )
        task.assigned_to = user
        task.assigned_to_name = employee_name
        task.status = MoveTask.STATUS_IN_PROGRESS
        task.started_at = task.started_at or assigned_at
        task.payload = payload
        task.updated_at = assigned_at
        updated_tasks.append(task)
    if updated_tasks:
        MoveTask.objects.bulk_update(
            updated_tasks,
            [
                "assigned_to",
                "assigned_to_name",
                "status",
                "started_at",
                "payload",
                "updated_at",
            ],
            batch_size=500,
        )
        request_ids = sorted({int(task.request_id) for task in updated_tasks})
        for request_row in MoveRequest.objects.select_for_update().filter(
            id__in=request_ids
        ):
            _sync_request(request_row)


@transaction.atomic
def sync_plan_reachtruck_assignment(plan: FbsReplenishmentPlan, user) -> None:
    sync_plans_reachtruck_assignment((plan,), user)


@transaction.atomic
def sync_plan_reachtruck_canceled(plan: FbsReplenishmentPlan) -> None:
    tasks = list(_bridge_tasks_for_plan(plan).select_for_update())
    now = timezone.now()
    for task in tasks:
        if task.status == MoveTask.STATUS_DONE:
            continue
        payload = dict(task.payload or {})
        payload.update({"status": MoveTask.STATUS_CANCELED, "status_label": "Отменено"})
        task.status = MoveTask.STATUS_CANCELED
        task.canceled_at = now
        task.payload = payload
        task.save(update_fields=["status", "canceled_at", "payload", "updated_at"])
    if tasks:
        _sync_request(tasks[0].request)


def _task_allocations(task: MoveTask) -> list[FbsReplenishmentAllocation]:
    ids = [
        int(value)
        for value in dict(task.payload or {}).get("fbs_allocation_ids", [])
        if str(value or "").strip().isdigit()
    ]
    return list(
        FbsReplenishmentAllocation.objects.select_related(
            "line__plan",
            "source_snapshot__container",
        )
        .filter(id__in=ids)
        .order_by("id")
    )


def _current_prepared_flow_allocation(
    allocations: Iterable[FbsReplenishmentAllocation],
) -> FbsReplenishmentAllocation | None:
    open_statuses = {
        FbsReplenishmentAllocation.STATUS_RESERVED,
        FbsReplenishmentAllocation.STATUS_IN_PROGRESS,
    }
    return next(
        (allocation for allocation in allocations if allocation.status in open_statuses),
        None,
    )


@transaction.atomic
def sync_allocation_reachtruck_done(
    allocation: FbsReplenishmentAllocation,
    *,
    sync_request: bool = True,
) -> None:
    plan = allocation.line.plan
    tasks = list(
        _bridge_tasks_for_plan(plan)
        .select_for_update(of=("self",))
        .order_by("id")
    )
    task = next(
        (
            row
            for row in tasks
            if allocation.id
            in {
                int(value)
                for value in dict(row.payload or {}).get("fbs_allocation_ids", [])
                if str(value or "").strip().isdigit()
            }
            and not dict(row.payload or {}).get("fbs_compacted_into_task_id")
        ),
        None,
    )
    if task is None:
        return
    allocations = _task_allocations(task)
    finished_statuses = {
        FbsReplenishmentAllocation.STATUS_STAGED,
        FbsReplenishmentAllocation.STATUS_DONE,
    }
    if not allocations or any(row.status not in finished_statuses for row in allocations):
        return
    now = timezone.now()
    payload = dict(task.payload or {})
    execution = dict(payload.get("mobile_execution") or {})
    execution["destination_confirmed"] = True
    update_fields = ["status", "qty_done", "completed_at", "payload", "updated_at"]
    if plan.mode == FbsReplenishmentPlan.MODE_BOX:
        plan.refresh_from_db()
        destination = plan.target_cell.location
        if _task_requires_container_custody(task, payload):
            employee_id = _task_employee_id(task)
            employee_name = _task_employee_name(task)
            _assign_container_custody(
                task=task,
                payload=payload,
                employee_id=employee_id,
                employee_name=employee_name,
                assigned_at=str(
                    payload.get("taken_at")
                    or timezone.localtime(task.started_at or now).isoformat()
                ),
            )
            custody = dict(payload.get(CONTAINER_CUSTODY_KEY) or {})
            if custody.get("status") != "in_transit":
                _mark_container_in_transit(
                    task=task,
                    payload=payload,
                    employee_id=employee_id,
                    employee_name=employee_name,
                    scanned_code=str(
                        payload.get("source_box_code")
                        or payload.get("pallet_code")
                        or ""
                    ),
                )
                custody = dict(payload.get(CONTAINER_CUSTODY_KEY) or {})
                custody["picked_at_confirmed_by_completed_source_scan"] = True
                payload[CONTAINER_CUSTODY_KEY] = custody
            _mark_container_placed(
                task=task,
                payload=payload,
                destination=destination,
            )
        destination_code = _location_scan_code(destination)
        execution["last_scan"] = destination_code
        payload.update(
            {
                "destination_scan_code": destination_code,
                "to_location": _location_payload(destination),
                "to_label": _location_label(destination),
            }
        )
        task.to_zone = str(destination.zone_code or "")
        task.to_row = int(destination.row_no or 0) or None
        task.to_section = int(destination.section_no or 0) or None
        task.to_tier = int(destination.tier_no or 0) or None
        task.to_cell = int(destination.cell_no or 0) or None
        update_fields.extend(["to_zone", "to_row", "to_section", "to_tier", "to_cell"])
    payload.update(
        {
            "status": MoveTask.STATUS_DONE,
            "status_label": "Выполнено",
            "mobile_execution": execution,
        }
    )
    task.status = MoveTask.STATUS_DONE
    task.qty_done = task.qty_planned
    task.completed_at = now
    task.payload = payload
    task.save(update_fields=update_fields)
    if sync_request:
        _sync_request(task.request)


def _scan_equal(left: str, right: str) -> bool:
    return bool(str(left or "").strip()) and str(left or "").strip().casefold() == str(
        right or ""
    ).strip().casefold()


def build_fbs_mobile_execution_snapshot(task: MoveTask) -> dict:
    payload = _without_fbs_movement_marking_scan(task.payload)
    execution = dict(payload.get("mobile_execution") or {})
    qty = int(task.qty_planned or payload.get("requested_qty") or 0)
    if payload.get(FLOOR_REPLENISHMENT_MARKER):
        source_confirmed = bool(execution.get("source_location_confirmed"))
        box_confirmed = bool(execution.get("box_confirmed"))
        destination_confirmed = bool(execution.get("destination_confirmed"))
        box_code = str(payload.get("source_box_code") or "").strip()
        if task.status == MoveTask.STATUS_DONE or destination_confirmed:
            current_step = "done"
            expected_scan = ""
            prompt = "Короб перемещен на первый ярус FBS."
        elif not source_confirmed:
            current_step = "source"
            expected_scan = str(payload.get("source_location_scan_code") or "").strip()
            prompt = "Подъедьте к исходному адресу и отсканируйте QR адреса."
        elif not box_confirmed:
            current_step = "boxes"
            expected_scan = box_code
            prompt = "Отсканируйте QR короба, который нужно снять с верхнего яруса."
        else:
            current_step = "destination"
            expected_scan = str(payload.get("destination_scan_code") or "").strip()
            prompt = "Отвезите короб на первый ярус и отсканируйте адрес назначения."
        box_row = {
            "box_code": box_code,
            "box_qty": qty,
            "unit_barcode_qty": {},
            "unit_barcode_preview": "",
            "units_required": 0,
            "unit_scanned_total": 0,
            "box_scanned": box_confirmed,
            "requires_unit_scan": False,
            "box_complete": box_confirmed,
            "return_required": False,
        }
        return {
            "source_code": str(payload.get("source_location_scan_code") or "").strip(),
            "source_label": str(payload.get("from_label") or "Исходный адрес").strip(),
            "source_confirmed": source_confirmed,
            "pallet_code": str(payload.get("source_pallet_code") or task.pallet_code).strip(),
            "pallet_confirmed": source_confirmed,
            "destination_code": str(payload.get("destination_scan_code") or "").strip(),
            "destination_label": str(payload.get("to_label") or "Первый ярус FBS").strip(),
            "destination_confirmed": destination_confirmed,
            "staging_container_code": "",
            "staging_container_confirmed": False,
            "boxes": [box_row],
            "boxes_pending": [] if box_confirmed else [box_row],
            "boxes_found": [box_row] if box_confirmed else [],
            "boxes_total": 1,
            "pallet_boxes_total": 1,
            "pallet_boxes_found_count": 1 if box_confirmed else 0,
            "boxes_completed": 1 if box_confirmed else 0,
            "boxes_scanned_count": 1 if box_confirmed else 0,
            "box_selection_complete": box_confirmed,
            "flexible_box_selection": False,
            "boxes_ready": source_confirmed,
            "all_boxes_complete": box_confirmed,
            "expected_units_total": 0,
            "scanned_units_total": 0,
            "current_step": current_step,
            "expected_scan": expected_scan,
            "prompt": prompt,
            "task_status": task.status,
        }
    if payload.get("fbs_box_closure_task"):
        plan_id = int(payload.get("fbs_plan_id") or 0)
        remaining_codes = list(
            FbsReplenishmentPreparedBox.objects.filter(
                plan_id=plan_id,
                status=FbsReplenishmentPreparedBox.STATUS_FILLING,
                scanned_qty__gt=0,
            )
            .order_by("sequence_no", "id")
            .values_list("box_code", flat=True)
        )
        done = task.status == MoveTask.STATUS_DONE or not remaining_codes
        return {
            "source_code": str(payload.get("source_location_scan_code") or "").strip(),
            "source_label": "Зона подготовки FBS",
            "source_confirmed": True,
            "pallet_code": "",
            "pallet_confirmed": True,
            "destination_code": "",
            "destination_label": "Закрытые FBS-короба",
            "destination_confirmed": done,
            "staging_container_code": "",
            "staging_container_confirmed": False,
            "boxes": [
                {
                    "box_code": code,
                    "box_qty": 0,
                    "unit_barcode_qty": {},
                    "unit_barcode_preview": "",
                    "units_required": 0,
                    "unit_scanned_total": 0,
                    "box_scanned": False,
                    "requires_unit_scan": False,
                    "box_complete": False,
                    "return_required": False,
                }
                for code in remaining_codes
            ],
            "boxes_pending": remaining_codes,
            "boxes_found": list(execution.get("closed_box_codes") or []),
            "boxes_total": int(payload.get("requested_box_count") or 0),
            "pallet_boxes_total": int(payload.get("requested_box_count") or 0),
            "pallet_boxes_found_count": len(execution.get("closed_box_codes") or []),
            "boxes_completed": len(execution.get("closed_box_codes") or []),
            "boxes_scanned_count": len(execution.get("closed_box_codes") or []),
            "box_selection_complete": done,
            "flexible_box_selection": True,
            "boxes_ready": True,
            "all_boxes_complete": done,
            "expected_units_total": 0,
            "scanned_units_total": 0,
            "current_step": "done" if done else "boxes",
            "expected_scan": "" if done else "QR заполненного FBS-короба",
            "prompt": (
                "Все заполненные короба закрыты."
                if done
                else f"Закройте короб повторным сканом QR. Осталось: {len(remaining_codes)}."
            ),
            "task_status": task.status,
        }
    if payload.get("fbs_prepared_box_placement_task"):
        box_code = str(payload.get("source_box_code") or task.pallet_code or "").strip()
        box_confirmed = bool(execution.get("box_confirmed"))
        destination_confirmed = bool(execution.get("destination_confirmed"))
        if task.status == MoveTask.STATUS_DONE or destination_confirmed:
            current_step = "done"
            expected_scan = ""
            prompt = "FBS-короб размещен."
        elif not box_confirmed:
            current_step = "boxes"
            expected_scan = box_code
            prompt = "Заберите закрытый FBS-короб и отсканируйте его QR."
        else:
            current_step = "destination"
            expected_scan = "QR ячейки OS"
            prompt = (
                "Отсканируйте выбранную ячейку OS или FBS-паллету этого же клиента."
            )
        box_row = {
            "box_code": box_code,
            "box_qty": qty,
            "unit_barcode_qty": {},
            "unit_barcode_preview": "",
            "units_required": 0,
            "unit_scanned_total": 0,
            "box_scanned": box_confirmed,
            "requires_unit_scan": False,
            "box_complete": box_confirmed,
            "return_required": False,
        }
        return {
            "source_code": str(payload.get("source_location_scan_code") or "").strip(),
            "source_label": "Зона подготовки FBS",
            "source_confirmed": box_confirmed,
            "pallet_code": box_code,
            "pallet_confirmed": box_confirmed,
            "destination_code": str(payload.get("destination_scan_code") or "").strip(),
            "destination_label": str(payload.get("to_label") or "Ячейка OS").strip(),
            "destination_confirmed": destination_confirmed,
            "staging_container_code": "",
            "staging_container_confirmed": False,
            "boxes": [box_row],
            "boxes_pending": [] if box_confirmed else [box_row],
            "boxes_found": [box_row] if box_confirmed else [],
            "boxes_total": 1,
            "pallet_boxes_total": 1,
            "pallet_boxes_found_count": 1 if box_confirmed else 0,
            "boxes_completed": 1 if box_confirmed else 0,
            "boxes_scanned_count": 1 if box_confirmed else 0,
            "box_selection_complete": box_confirmed,
            "flexible_box_selection": False,
            "boxes_ready": True,
            "all_boxes_complete": box_confirmed,
            "expected_units_total": 0,
            "scanned_units_total": 0,
            "current_step": current_step,
            "expected_scan": expected_scan,
            "prompt": prompt,
            "destination_guide": (
                _fbs_destination_guide(task) if current_step == "destination" else {}
            ),
            "task_status": task.status,
        }
    if (
        payload.get("fbs_placement_task")
        or payload.get("fbs_prepared_box_placement_task")
        or payload.get("fbs_box_closure_task")
    ):
        box_code = str(payload.get("source_box_code") or task.pallet_code or "").strip()
        box_confirmed = bool(execution.get("box_confirmed"))
        destination_confirmed = bool(execution.get("destination_confirmed"))
        if task.status == MoveTask.STATUS_DONE or destination_confirmed:
            current_step = "done"
            expected_scan = ""
            prompt = "FBS-короб размещен."
        elif not box_confirmed:
            current_step = "boxes"
            expected_scan = box_code
            prompt = "Заберите подготовленный FBS-короб и отсканируйте его QR."
        else:
            current_step = "destination"
            expected_scan = "QR выбранной ячейки OS"
            prompt = (
                "Отвезите короб к любой выбранной ячейке действующей топологии "
                "и отсканируйте QR ячейки."
            )
        box_row = {
            "box_code": box_code,
            "box_qty": qty,
            "unit_barcode_qty": {},
            "unit_barcode_preview": "",
            "units_required": 0,
            "unit_scanned_total": 0,
            "box_scanned": box_confirmed,
            "requires_unit_scan": False,
            "box_complete": box_confirmed,
            "return_required": False,
        }
        return {
            "source_code": str(payload.get("source_location_scan_code") or "").strip(),
            "source_label": str(payload.get("from_label") or "Зона подготовки FBS").strip(),
            "source_confirmed": box_confirmed,
            "pallet_code": box_code,
            "pallet_confirmed": box_confirmed,
            "destination_code": str(payload.get("destination_scan_code") or "").strip(),
            "destination_label": str(payload.get("to_label") or "Свободная ячейка OS").strip(),
            "destination_confirmed": destination_confirmed,
            "staging_container_code": "",
            "staging_container_confirmed": False,
            "boxes": [box_row],
            "boxes_pending": [] if box_confirmed else [box_row],
            "boxes_found": [box_row] if box_confirmed else [],
            "boxes_total": 1,
            "pallet_boxes_total": 1,
            "pallet_boxes_found_count": 1 if box_confirmed else 0,
            "boxes_completed": 1 if box_confirmed else 0,
            "boxes_scanned_count": 1 if box_confirmed else 0,
            "box_selection_complete": box_confirmed,
            "flexible_box_selection": False,
            "boxes_ready": True,
            "all_boxes_complete": box_confirmed,
            "expected_units_total": 0,
            "scanned_units_total": 0,
            "current_step": current_step,
            "expected_scan": expected_scan,
            "prompt": prompt,
            "destination_guide": (
                _fbs_destination_guide(task) if current_step == "destination" else {}
            ),
            "task_status": task.status,
        }
    if payload.get(PREPARED_FLOW_MARKER):
        item_mode = True
        pallet_confirmed = bool(execution.get("pallet_confirmed"))
        box_confirmed = bool(execution.get("box_confirmed"))
        destination_confirmed = bool(execution.get("destination_confirmed"))
        units_scanned = min(int(execution.get("units_scanned_qty") or 0), qty)
        active_box_code = str(execution.get("active_prepared_box_code") or "").strip()
        source_box = str(payload.get("source_box_code") or "").strip()
        unit_code = str(payload.get("fbs_unit_scan_code") or "").strip()
        destination = _prepared_flow_destination(payload)
        destination_code = str(
            payload.get("destination_scan_code") or _location_scan_code(destination)
        ).strip()
        destination_label = str(
            _location_label(destination) if destination is not None else payload.get("to_label")
        ).strip()
        if task.status == MoveTask.STATUS_DONE:
            current_step = "done"
            expected_scan = ""
            prompt = "Товар собран в постоянные FBS-короба."
        elif not pallet_confirmed:
            current_step = "pallet"
            expected_scan = str(payload.get("pallet_code") or task.pallet_code or "").strip()
            prompt = "Отсканируйте QR паллеты."
        elif not box_confirmed:
            current_step = "boxes"
            expected_scan = source_box
            prompt = "Отсканируйте QR исходного короба."
        elif not destination_confirmed:
            current_step = "destination"
            expected_scan = destination_code
            prompt = f"Доставьте товар в место {destination_label} и отсканируйте его QR."
        elif not active_box_code:
            current_step = "prepared_box"
            expected_scan = "QR напечатанного FBS-короба"
            prompt = "Отсканируйте постоянный FBS-короб, в который складываете товар."
        elif payload.get(MARKING_SCAN_MARKER) and execution.get("pending_marking_scan"):
            current_step = "marking"
            expected_scan = "Data Matrix ЧЗ"
            prompt = (
                f"Короб {active_box_code}: ШК товара принят. "
                "Отсканируйте Data Matrix Честного знака этой же единицы."
            )
        else:
            current_step = "units"
            expected_scan = unit_code
            prompt = (
                f"Короб {active_box_code}: сканируйте ШК товара {units_scanned} из {qty}. "
                "Чтобы закрыть короб, повторно отсканируйте его QR."
            )
        box_row = {
            "box_code": source_box,
            "box_qty": int(payload.get("fbs_source_qty") or qty),
            "unit_barcode_qty": {unit_code: qty} if unit_code else {},
            "unit_barcode_preview": unit_code,
            "units_required": qty,
            "unit_scanned_total": units_scanned,
            "box_scanned": box_confirmed,
            "requires_unit_scan": item_mode,
            "box_complete": units_scanned >= qty,
            "return_required": int(payload.get("fbs_source_qty") or qty) > qty,
        }
        return {
            "source_code": _payload_source_scan_code(payload),
            "source_label": _payload_source_label(payload),
            "source_confirmed": pallet_confirmed,
            "pallet_code": str(payload.get("pallet_code") or task.pallet_code or "").strip(),
            "pallet_confirmed": pallet_confirmed,
            "destination_code": destination_code,
            "destination_label": destination_label,
            "destination_confirmed": destination_confirmed,
            "staging_container_code": active_box_code,
            "staging_container_confirmed": bool(active_box_code),
            "boxes": [box_row],
            "boxes_pending": [] if box_row["box_complete"] else [box_row],
            "boxes_found": [box_row] if box_confirmed else [],
            "boxes_total": 1,
            "pallet_boxes_total": 1,
            "pallet_boxes_found_count": 1 if box_confirmed else 0,
            "boxes_completed": 1 if box_row["box_complete"] else 0,
            "boxes_scanned_count": 1 if box_confirmed else 0,
            "box_selection_complete": box_confirmed,
            "flexible_box_selection": False,
            "boxes_ready": pallet_confirmed,
            "all_boxes_complete": box_row["box_complete"],
            "expected_units_total": qty,
            "scanned_units_total": units_scanned,
            "current_step": current_step,
            "expected_scan": expected_scan,
            "prompt": prompt,
            "task_status": task.status,
        }
    item_mode = task.move_mode == MoveTask.MODE_BOX_PARTIAL
    pallet_confirmed = bool(execution.get("pallet_confirmed"))
    box_confirmed = bool(execution.get("box_confirmed"))
    units_scanned = min(int(execution.get("units_scanned_qty") or 0), qty)
    units_complete = not item_mode or units_scanned >= qty
    staging_container_confirmed = bool(execution.get("staging_container_confirmed"))
    destination_confirmed = bool(execution.get("destination_confirmed"))
    box_complete = box_confirmed and units_complete
    source_box = str(payload.get("source_box_code") or "").strip()
    unit_code = str(payload.get("fbs_unit_scan_code") or "").strip()
    movement_id = int(payload.get("fbs_movement_id") or 0)
    staging_container_code = str(
        payload.get("fbs_staging_container_code")
        or (movement_staging_container_code(movement_id) if movement_id else "")
    ).strip()
    if task.status == MoveTask.STATUS_DONE or destination_confirmed:
        current_step = "done"
        expected_scan = ""
        prompt = "Задание выполнено."
    elif not pallet_confirmed:
        current_step = "pallet"
        expected_scan = str(payload.get("pallet_code") or task.pallet_code or "").strip()
        prompt = "Отсканируйте QR паллеты."
    elif not box_confirmed:
        current_step = "boxes"
        expected_scan = source_box
        prompt = "Отсканируйте QR короба."
    elif payload.get(MARKING_SCAN_MARKER) and execution.get("pending_marking_scan"):
        current_step = "marking"
        expected_scan = "Data Matrix ЧЗ"
        prompt = "ШК товара принят. Отсканируйте Data Matrix Честного знака этой же единицы."
    elif not units_complete:
        current_step = "units"
        expected_scan = unit_code
        prompt = f"Нужен ШК товара из короба {source_box}: {units_scanned} из {qty}."
    elif item_mode and not staging_container_confirmed:
        current_step = "staging_container"
        expected_scan = staging_container_code
        prompt = "Положите товар во временный короб заявки и отсканируйте его QR."
    else:
        current_step = "destination"
        if item_mode:
            expected_scan = str(payload.get("destination_scan_code") or "").strip()
            prompt = "Отвезите временный короб в зону подготовки FBS и отсканируйте QR зоны."
        else:
            if payload.get(FULL_SOURCE_PALLET_MARKER):
                expected_scan = "QR выбранной ячейки OS"
                prompt = (
                    "Отвезите паллету в зону FBS и отсканируйте выбранную "
                    "ячейку действующей топологии OS. Место определяет водитель."
                )
            else:
                expected_scan = "QR выбранной ячейки OS или ячейки PR"
                prompt = (
                    "Отвезите короб в FBS и отсканируйте выбранную ячейку OS "
                    "либо точную ячейку PR."
                )
    box_row = {
        "box_code": source_box,
        "box_qty": int(payload.get("fbs_source_qty") or qty),
        "unit_barcode_qty": {unit_code: qty} if item_mode and unit_code else {},
        "unit_barcode_preview": unit_code,
        "units_required": qty if item_mode else 0,
        "unit_scanned_total": units_scanned,
        "box_scanned": box_confirmed,
        "requires_unit_scan": item_mode,
        "box_complete": box_complete,
        "return_required": item_mode and int(payload.get("fbs_source_qty") or qty) > qty,
    }
    return {
        "source_code": _payload_source_scan_code(payload),
        "source_label": _payload_source_label(payload),
        "source_confirmed": pallet_confirmed,
        "pallet_code": str(payload.get("pallet_code") or task.pallet_code or "").strip(),
        "pallet_confirmed": pallet_confirmed,
        "destination_code": str(payload.get("destination_scan_code") or "").strip(),
        "destination_label": str(payload.get("to_label") or "").strip(),
        "destination_confirmed": destination_confirmed,
        "staging_container_code": staging_container_code,
        "staging_container_confirmed": staging_container_confirmed,
        "boxes": [box_row],
        "boxes_pending": [] if box_complete else [box_row],
        "boxes_found": [box_row] if box_confirmed else [],
        "boxes_total": 1,
        "pallet_boxes_total": 1,
        "pallet_boxes_found_count": 1 if box_confirmed else 0,
        "boxes_completed": 1 if box_complete else 0,
        "boxes_scanned_count": 1 if box_confirmed else 0,
        "box_selection_complete": box_confirmed,
        "flexible_box_selection": False,
        "boxes_ready": pallet_confirmed,
        "all_boxes_complete": box_complete,
        "expected_units_total": qty if item_mode else 0,
        "scanned_units_total": units_scanned,
        "current_step": current_step,
        "expected_scan": expected_scan,
        "prompt": prompt,
        "destination_guide": (
            _fbs_destination_guide(task)
            if current_step == "destination" and not item_mode
            else {}
        ),
        "task_status": task.status,
    }


@transaction.atomic
def replace_missing_fbs_box_for_task(
    *,
    task: MoveTask,
    missing_box_code: str,
    user,
    employee_id: int,
    employee_name: str,
) -> dict:
    task = MoveTask.objects.select_for_update().select_related("request").get(pk=task.pk)
    payload = dict(task.payload or {})
    if task.status != MoveTask.STATUS_IN_PROGRESS:
        raise FbsReplenishmentError("Задание FBS должно быть взято в работу.")
    if _task_employee_id(task) != int(employee_id):
        raise FbsReplenishmentError("Задание FBS назначено другому водителю.")
    if _ensure_fbs_plan_assignment_for_current_driver(
        task=task,
        user=user,
        employee_id=employee_id,
    ):
        task.refresh_from_db()
        payload = dict(task.payload or {})
    snapshot = build_fbs_mobile_execution_snapshot(task)
    if str(snapshot.get("current_step") or "").strip() != "boxes":
        raise FbsReplenishmentError("Сейчас задание не находится на этапе поиска коробов.")
    expected_codes = {
        str(row.get("box_code") if isinstance(row, dict) else row).strip().casefold()
        for row in list(snapshot.get("boxes_pending") or [])
        if str(row.get("box_code") if isinstance(row, dict) else row).strip()
    }
    if str(missing_box_code or "").strip().casefold() not in expected_codes:
        raise FbsReplenishmentError("Короб уже найден или не относится к текущему FBS-заданию.")
    if not _is_fbs_box_collection_task(task):
        raise FbsReplenishmentError("Пропуск доступен только для целого короба FBS.")
    allocations = _task_allocations(task)
    if not allocations:
        raise FbsReplenishmentError("Не найдена связь задания с резервом FBS.")

    from fbs.services.replenishment import skip_missing_whole_box_source

    skipped_plan = skip_missing_whole_box_source(
        allocation_id=allocations[0].id,
        missing_box_code=missing_box_code,
        performed_by=user,
    )
    movement_id = int(payload.get("fbs_movement_id") or 0)
    sibling_tasks = list(
        MoveTask.objects.select_for_update(of=("self",))
        .filter(
            request_id=task.request_id,
            payload__fbs_movement_id=movement_id,
            payload__fbs_allocation_ids__isnull=False,
        )
        .exclude(pk=task.pk)
        .order_by("id")
    )
    collected_count = sum(
        1
        for sibling in sibling_tasks
        if sibling.status == MoveTask.STATUS_DONE
        or bool(dict(sibling.payload or {}).get("mobile_execution", {}).get("box_confirmed"))
    )
    now = timezone.now()
    payload["missing_box_skipped_v1"] = {
        "missing_box_code": str(missing_box_code or "").strip(),
        "skipped_at": timezone.localtime(now).isoformat(),
        "plan_id": skipped_plan.id,
        "collected_sibling_count": collected_count,
    }
    payload.update({"status": MoveTask.STATUS_CANCELED, "status_label": "Нет на месте"})
    task.status = MoveTask.STATUS_CANCELED
    task.canceled_at = now
    task.payload = payload
    task.save(update_fields=["status", "canceled_at", "payload", "updated_at"])
    _sync_request(task.request)
    return {
        "replaced": False,
        "partial_completed": True,
        "collected_count": collected_count,
    }


@transaction.atomic
def take_fbs_move_task(
    *,
    task: MoveTask,
    user,
    employee_id: int,
    employee_name: str,
    confirm_takeover: bool = False,
    expected_assignee_ids: str = "",
):
    task = MoveTask.objects.select_for_update().select_related("request").get(pk=task.pk)
    payload = dict(task.payload or {})
    if task.status == MoveTask.STATUS_DONE:
        return _command_result(ok=False, error="Задание уже выполнено.")
    try:
        _require_task_concrete_source(task, payload)
    except FbsReplenishmentError as exc:
        return _command_result(ok=False, error=str(exc))
    assigned_employee_id = _task_employee_id(task)
    takeover_required = bool(
        assigned_employee_id and assigned_employee_id != int(employee_id)
    )
    previous_employee_name = _task_employee_name(task)
    if takeover_required:
        if not confirm_takeover:
            return _command_result(
                ok=False,
                error="Задание уже в работе. Подтвердите передачу другому водителю.",
            )
        if _parse_assignment_fingerprint(expected_assignee_ids) != {assigned_employee_id}:
            return _command_result(
                ok=False,
                error="Исполнитель задания уже изменился. Обновите список и повторите действие.",
            )
    if payload.get(FLOOR_REPLENISHMENT_MARKER):
        if task.status in (MoveTask.STATUS_CANCELED, MoveTask.STATUS_FAILED):
            return _command_result(ok=False, error="Задание уже недоступно.")
        movement_id = int(payload.get("fbs_internal_movement_id") or 0)
        from fbs.services.movements import claim_internal_movement

        try:
            claim_internal_movement(
                movement_id=movement_id,
                assigned_to=user,
                allow_reassignment=takeover_required,
            )
        except FbsError as exc:
            return _command_result(ok=False, error=str(exc))
        now = timezone.now()
        local_now = timezone.localtime(now).isoformat()
        if takeover_required:
            _append_takeover_history(
                payload,
                from_employee_ids={assigned_employee_id},
                from_employee_names={previous_employee_name},
                to_employee_id=employee_id,
                to_employee_name=employee_name,
                taken_over_at=local_now,
            )
        payload.update(
            {
                "status": MoveTask.STATUS_IN_PROGRESS,
                "status_label": "В работе",
                "assigned_to_id": int(employee_id),
                "assigned_employee_id": int(employee_id),
                "assigned_to_name": employee_name,
                "taken_at": local_now,
            }
        )
        _assign_container_custody(
            task=task,
            payload=payload,
            employee_id=employee_id,
            employee_name=employee_name,
            assigned_at=local_now,
        )
        task.assigned_to = user
        task.assigned_to_name = employee_name
        task.status = MoveTask.STATUS_IN_PROGRESS
        task.started_at = task.started_at or now
        task.payload = payload
        task.save(
            update_fields=[
                "assigned_to",
                "assigned_to_name",
                "status",
                "started_at",
                "payload",
                "updated_at",
            ]
        )
        _sync_request(task.request)
        return _command_result(
            ok=True,
            task=task,
            payload=payload,
            message=(
                "Заявка на первый ярус передана вам."
                if takeover_required
                else "Заявка на первый ярус взята в работу."
            ),
        )
    if payload.get("fbs_placement_task"):
        now = timezone.now()
        local_now = timezone.localtime(now).isoformat()
        if takeover_required:
            _append_takeover_history(
                payload,
                from_employee_ids={assigned_employee_id},
                from_employee_names={previous_employee_name},
                to_employee_id=employee_id,
                to_employee_name=employee_name,
                taken_over_at=local_now,
            )
        payload.update(
            {
                "status": MoveTask.STATUS_IN_PROGRESS,
                "status_label": "В работе",
                "assigned_to_id": int(employee_id),
                "assigned_to_name": employee_name,
                "assigned_employee_id": int(employee_id),
                "taken_at": local_now,
            }
        )
        _assign_container_custody(
            task=task,
            payload=payload,
            employee_id=employee_id,
            employee_name=employee_name,
            assigned_at=local_now,
        )
        task.assigned_to = user
        task.assigned_to_name = employee_name
        task.status = MoveTask.STATUS_IN_PROGRESS
        task.started_at = task.started_at or now
        task.payload = payload
        task.save(
            update_fields=[
                "assigned_to",
                "assigned_to_name",
                "status",
                "started_at",
                "payload",
                "updated_at",
            ]
        )
        _sync_request(task.request)
        return _command_result(
            ok=True,
            task=task,
            payload=payload,
            message=(
                "Задание на закрытие FBS-коробов взято в работу."
                if payload.get("fbs_box_closure_task")
                else "Задание на размещение FBS-короба взято в работу."
            ),
        )
    plan = FbsReplenishmentPlan.objects.select_for_update().filter(
        pk=int(payload.get("fbs_plan_id") or 0)
    ).first()
    if plan is None:
        return _command_result(ok=False, error="План FBS для задания не найден.")
    from fbs.services.replenishment import claim_replenishment_plan

    try:
        claim_replenishment_plan(
            plan_id=plan.id,
            assigned_to=user,
            allow_reassignment=takeover_required,
            expected_assigned_to_id=plan.assigned_to_id,
        )
    except FbsReplenishmentError as exc:
        return _command_result(ok=False, error=str(exc))
    task.refresh_from_db()
    if takeover_required:
        payload = dict(task.payload or {})
        local_now = timezone.localtime().isoformat()
        _append_takeover_history(
            payload,
            from_employee_ids={assigned_employee_id},
            from_employee_names={previous_employee_name},
            to_employee_id=employee_id,
            to_employee_name=employee_name,
            taken_over_at=local_now,
        )
        payload.update(
            {
                "assigned_to_id": int(employee_id),
                "assigned_employee_id": int(employee_id),
                "assigned_to_name": employee_name,
                "taken_at": local_now,
            }
        )
        _assign_container_custody(
            task=task,
            payload=payload,
            employee_id=employee_id,
            employee_name=employee_name,
            assigned_at=local_now,
        )
        task.payload = payload
        task.save(update_fields=["payload", "updated_at"])
    return _command_result(
        ok=True,
        task=task,
        payload=dict(task.payload or {}),
        message="Задание передано вам." if takeover_required else "Задание взято в работу.",
    )


@transaction.atomic
def scan_fbs_move_task(
    *,
    task: MoveTask,
    scan_value: str,
    user,
    employee_id: int,
    employee_name: str,
):
    task = MoveTask.objects.select_for_update().select_related("request").get(pk=task.pk)
    payload = _without_fbs_movement_marking_scan(task.payload)
    if task.status == MoveTask.STATUS_DONE:
        return _command_result(ok=True, task=task, payload=payload, completed=True)
    if task.status != MoveTask.STATUS_IN_PROGRESS:
        return _command_result(ok=False, error="Возьмите задание в работу перед сканированием.")
    if int(payload.get("assigned_to_id") or 0) != int(employee_id):
        return _command_result(ok=False, error="Задание назначено другому водителю.")
    if payload.get(FLOOR_REPLENISHMENT_MARKER):
        scan = str(scan_value or "").strip()
        if not scan:
            return _command_result(ok=False, error="Отсканируйте код.")
        execution = dict(payload.get("mobile_execution") or {})
        snapshot = build_fbs_mobile_execution_snapshot(task)
        step = snapshot["current_step"]
        if step == "done":
            return _command_result(ok=True, task=task, payload=payload, completed=True)
        if step == "source":
            expected_source = str(payload.get("source_location_scan_code") or "").strip()
            if not _scan_equal(scan, expected_source):
                return _command_result(
                    ok=False,
                    error=f"Ожидается QR исходного адреса {expected_source}.",
                )
            execution["source_location_confirmed"] = True
            message = "Исходный адрес подтвержден. Отсканируйте QR короба."
        elif step == "boxes":
            expected_box = str(payload.get("source_box_code") or "").strip()
            if not _scan_equal(scan, expected_box):
                return _command_result(
                    ok=False,
                    error=f"Ожидается QR короба {expected_box}.",
                )
            execution["box_confirmed"] = True
            _mark_container_in_transit(
                task=task,
                payload=payload,
                employee_id=employee_id,
                employee_name=employee_name,
                scanned_code=scan,
            )
            message = "Короб подтвержден. Отвезите его на первый ярус."
        elif step == "destination":
            from fbs.services.movements import scan_internal_movement

            try:
                movement = scan_internal_movement(
                    movement_id=int(payload.get("fbs_internal_movement_id") or 0),
                    source_location_scan=str(
                        payload.get("source_location_scan_code") or ""
                    ),
                    source_box_scan=str(payload.get("source_box_code") or ""),
                    target_scan=scan,
                    performed_by=user,
                )
            except FbsError as exc:
                return _command_result(ok=False, error=str(exc))
            execution["destination_confirmed"] = True
            execution["last_scan"] = scan
            _mark_container_placed(
                task=task,
                payload=payload,
                destination=movement.target_pallet.cell.location,
            )
            payload.update(
                {
                    "status": MoveTask.STATUS_DONE,
                    "status_label": "Выполнено",
                    "mobile_execution": execution,
                }
            )
            now = timezone.now()
            task.status = MoveTask.STATUS_DONE
            task.qty_done = task.qty_planned
            task.completed_at = now
            task.payload = payload
            task.save(
                update_fields=[
                    "status",
                    "qty_done",
                    "completed_at",
                    "payload",
                    "updated_at",
                ]
            )
            _sync_request(task.request)
            return _command_result(
                ok=True,
                task=task,
                payload=payload,
                message="Короб перемещен на первый ярус FBS.",
                completed=True,
            )
        else:
            return _command_result(ok=False, error="Неизвестный шаг FBS-задания.")
        execution["last_scan"] = scan
        payload["mobile_execution"] = execution
        task.payload = payload
        task.save(update_fields=["payload", "updated_at"])
        return _command_result(ok=True, task=task, payload=payload, message=message)
    try:
        assignment_repaired = _ensure_fbs_plan_assignment_for_current_driver(
            task=task,
            user=user,
            employee_id=employee_id,
        )
    except FbsReplenishmentError as exc:
        return _command_result(ok=False, error=str(exc))
    if assignment_repaired:
        task.refresh_from_db()
        payload = _without_fbs_movement_marking_scan(task.payload)
    scan = str(scan_value or "").strip()
    if not scan:
        return _command_result(ok=False, error="Отсканируйте код.")
    execution = dict(payload.get("mobile_execution") or {})
    snapshot = build_fbs_mobile_execution_snapshot(task)
    step = snapshot["current_step"]
    expected = str(snapshot.get("expected_scan") or "").strip()
    if payload.get("fbs_box_closure_task"):
        if step == "done":
            return _command_result(ok=True, task=task, payload=payload, completed=True)
        prepared_box = (
            FbsReplenishmentPreparedBox.objects.filter(
                plan_id=int(payload.get("fbs_plan_id") or 0),
                box_code__iexact=scan,
                status=FbsReplenishmentPreparedBox.STATUS_FILLING,
                scanned_qty__gt=0,
            )
            .order_by("id")
            .first()
        )
        if prepared_box is None:
            return _command_result(
                ok=False,
                error="Ожидается QR одного из заполненных незакрытых FBS-коробов.",
            )
        from fbs.services.replenishment import (
            close_prepared_box,
            finalize_prepared_box_plan,
        )

        try:
            with transaction.atomic():
                close_prepared_box(
                    prepared_box_id=prepared_box.id,
                    box_scan=scan,
                    performed_by=user,
                )
                remaining = FbsReplenishmentPreparedBox.objects.filter(
                    plan_id=int(payload.get("fbs_plan_id") or 0),
                    status=FbsReplenishmentPreparedBox.STATUS_FILLING,
                    scanned_qty__gt=0,
                ).count()
                closed_codes = list(execution.get("closed_box_codes") or [])
                if prepared_box.box_code not in closed_codes:
                    closed_codes.append(prepared_box.box_code)
                execution["closed_box_codes"] = closed_codes
                execution["last_scan"] = scan
                payload["mobile_execution"] = execution
                if remaining:
                    task.payload = payload
                    task.save(update_fields=["payload", "updated_at"])
                    return _command_result(
                        ok=True,
                        task=task,
                        payload=payload,
                        message=f"Короб закрыт. Осталось закрыть: {remaining}.",
                    )
                finalize_prepared_box_plan(
                    plan_id=int(payload.get("fbs_plan_id") or 0),
                    performed_by=user,
                )
        except FbsReplenishmentError as exc:
            return _command_result(ok=False, error=str(exc))
        now = timezone.now()
        payload.update({"status": MoveTask.STATUS_DONE, "status_label": "Выполнено"})
        task.status = MoveTask.STATUS_DONE
        task.qty_done = task.qty_planned
        task.completed_at = now
        task.payload = payload
        task.save(
            update_fields=["status", "qty_done", "completed_at", "payload", "updated_at"]
        )
        _sync_request(task.request)
        return _command_result(
            ok=True,
            task=task,
            payload=payload,
            message="Все заполненные короба закрыты. Пустые этикетки отмечены как неиспользованные.",
            completed=True,
        )
    if payload.get("fbs_prepared_box_placement_task"):
        box_code = str(payload.get("source_box_code") or task.pallet_code or "").strip()
        if step == "boxes":
            if not _scan_equal(scan, box_code):
                return _command_result(
                    ok=False,
                    error=f"Ожидается QR закрытого FBS-короба {box_code}.",
                )
            execution["box_confirmed"] = True
            execution["last_scan"] = scan
            _mark_container_in_transit(
                task=task,
                payload=payload,
                employee_id=employee_id,
                employee_name=employee_name,
                scanned_code=scan,
            )
            payload["mobile_execution"] = execution
            task.payload = payload
            task.save(update_fields=["payload", "updated_at"])
            return _command_result(
                ok=True,
                task=task,
                payload=payload,
                message="FBS-короб подтвержден. Отсканируйте ячейку действующей топологии.",
            )
        if step == "destination":
            from fbs.services.replenishment import complete_prepared_box_placement

            try:
                complete_prepared_box_placement(
                    prepared_box_id=int(payload.get("fbs_prepared_box_id") or 0),
                    box_scan=box_code,
                    destination_scan=scan,
                    performed_by=user,
                )
            except FbsReplenishmentError as exc:
                return _command_result(ok=False, error=str(exc))
            prepared_box = FbsReplenishmentPreparedBox.objects.select_related(
                "physical_container__current_location"
            ).get(pk=int(payload.get("fbs_prepared_box_id") or 0))
            destination = prepared_box.physical_container.current_location
            destination_code = _location_scan_code(destination)
            execution["destination_confirmed"] = True
            execution["last_scan"] = destination_code or scan
            _mark_container_placed(
                task=task,
                payload=payload,
                destination=destination,
            )
            payload.update(
                {
                    "status": MoveTask.STATUS_DONE,
                    "status_label": "Выполнено",
                    "destination_scan_code": destination_code,
                    "to_location": _location_payload(destination),
                    "to_label": _location_label(destination),
                    "mobile_execution": execution,
                }
            )
            now = timezone.now()
            task.to_zone = "OS"
            task.to_row = int(destination.row_no or 0) or None
            task.to_section = int(destination.section_no or 0) or None
            task.to_tier = int(destination.tier_no or 0) or None
            task.to_cell = int(destination.cell_no or 0) or None
            task.status = MoveTask.STATUS_DONE
            task.qty_done = task.qty_planned
            task.completed_at = now
            task.payload = payload
            task.save(
                update_fields=[
                    "to_zone",
                    "to_row",
                    "to_section",
                    "to_tier",
                    "to_cell",
                    "status",
                    "qty_done",
                    "completed_at",
                    "payload",
                    "updated_at",
                ]
            )
            _sync_request(task.request)
            return _command_result(
                ok=True,
                task=task,
                payload=payload,
                message=f"FBS-короб размещен в ячейке {destination_code}.",
                completed=True,
            )
        return _command_result(ok=True, task=task, payload=payload, completed=True)
    if payload.get(PREPARED_FLOW_MARKER) and step in {
        "destination",
        "prepared_box",
        "units",
        "marking",
    }:
        allocations = _task_allocations(task)
        if not allocations:
            return _command_result(ok=False, error="Нарушена связь задания с товаром FBS.")
        allocation = _current_prepared_flow_allocation(allocations)
        from fbs.services.replenishment import (
            close_prepared_box,
            open_prepared_box_for_allocation,
            record_prepared_box_item_scan,
            stage_prepared_box_allocation,
        )

        if step == "destination":
            try:
                destination, destination_changed = _select_prepared_flow_destination(
                    task=task,
                    payload=payload,
                    scan_value=scan,
                )
            except FbsReplenishmentError as exc:
                return _command_result(ok=False, error=str(exc))
            destination_code = _location_scan_code(destination)
            execution["destination_confirmed"] = True
            execution["last_scan"] = destination_code or scan
            payload.update(
                {
                    "destination_scan_code": destination_code,
                    "to_location": _location_payload(destination),
                    "to_label": _location_label(destination),
                    "mobile_execution": execution,
                }
            )
            task.payload = payload
            task.save(update_fields=["payload", "updated_at"])
            return _command_result(
                ok=True,
                task=task,
                payload=payload,
                message=(
                    f"Свободное место {destination_code} выбрано. "
                    "Отсканируйте постоянный FBS-короб."
                    if destination_changed
                    else f"Место {destination_code} подтверждено. "
                    "Отсканируйте постоянный FBS-короб."
                ),
            )

        if step == "prepared_box":
            if allocation is None:
                return _command_result(
                    ok=True,
                    task=task,
                    payload=payload,
                    message="Все товары этого задания уже собраны.",
                    completed=True,
                )
            try:
                prepared_box = open_prepared_box_for_allocation(
                    allocation_id=allocation.id,
                    box_scan=scan,
                    performed_by=user,
                )
            except FbsReplenishmentError as exc:
                return _command_result(ok=False, error=str(exc))
            execution["active_prepared_box_id"] = prepared_box.id
            execution["active_prepared_box_code"] = prepared_box.box_code
            execution["last_scan"] = scan
            payload["mobile_execution"] = execution
            task.payload = payload
            task.save(update_fields=["payload", "updated_at"])
            return _command_result(
                ok=True,
                task=task,
                payload=payload,
                message=f"Короб {prepared_box.box_code} открыт. Сканируйте товар.",
            )
        active_box_id = int(execution.get("active_prepared_box_id") or 0)
        active_box_code = str(execution.get("active_prepared_box_code") or "").strip()
        if allocation is None:
            task.refresh_from_db()
            return _command_result(
                ok=True,
                task=task,
                payload=dict(task.payload or {}),
                message="Все товары этого задания уже собраны.",
                completed=True,
            )
        if step == "marking":
            pending_barcode = str(execution.get("pending_product_barcode") or "").strip()
            if not payload.get(MARKING_SCAN_MARKER) or not pending_barcode:
                return _command_result(
                    ok=False,
                    error="Не найден подтвержденный ШК товара перед сканированием ЧЗ.",
                )
            if _scan_equal(scan, active_box_code):
                return _command_result(
                    ok=False,
                    error=(
                        "Сначала отсканируйте Data Matrix Честного знака принятой единицы. "
                        "Короб пока нельзя закрыть."
                    ),
                )
            try:
                with transaction.atomic():
                    prepared_item = record_prepared_box_item_scan(
                        allocation_id=allocation.id,
                        prepared_box_id=active_box_id,
                        item_scan=pending_barcode,
                        marking_scan=scan,
                        performed_by=user,
                    )
                    execution["units_scanned_qty"] = (
                        int(execution.get("units_scanned_qty") or 0) + 1
                    )
                    execution["pending_marking_scan"] = False
                    execution["pending_product_barcode"] = ""
                    execution["last_scan"] = scan
                    payload["mobile_execution"] = execution
                    task.payload = payload
                    task.save(update_fields=["payload", "updated_at"])
                    if int(prepared_item.qty_scanned or 0) >= int(
                        allocation.qty_planned or 0
                    ):
                        stage_prepared_box_allocation(
                            allocation_id=allocation.id,
                            performed_by=user,
                        )
            except FbsReplenishmentError as exc:
                return _command_result(ok=False, error=str(exc))
            if int(execution["units_scanned_qty"]) >= int(task.qty_planned or 0):
                task.refresh_from_db()
                return _command_result(
                    ok=True,
                    task=task,
                    payload=dict(task.payload or {}),
                    message="ШК и Честный знак подтверждены. Товар задания собран.",
                    completed=True,
                )
            return _command_result(
                ok=True,
                task=task,
                payload=payload,
                message=(
                    "ШК и Честный знак подтверждены: "
                    f"{execution['units_scanned_qty']} из {task.qty_planned}."
                ),
            )
        if _scan_equal(scan, active_box_code):
            try:
                close_prepared_box(
                    prepared_box_id=active_box_id,
                    box_scan=scan,
                    performed_by=user,
                )
            except FbsReplenishmentError as exc:
                return _command_result(ok=False, error=str(exc))
            closed_codes = list(execution.get("closed_prepared_box_codes") or [])
            if active_box_code not in closed_codes:
                closed_codes.append(active_box_code)
            execution.update(
                {
                    "active_prepared_box_id": 0,
                    "active_prepared_box_code": "",
                    "closed_prepared_box_codes": closed_codes,
                    "last_scan": scan,
                }
            )
            payload["mobile_execution"] = execution
            task.payload = payload
            task.save(update_fields=["payload", "updated_at"])
            return _command_result(
                ok=True,
                task=task,
                payload=payload,
                message="Короб закрыт. Отсканируйте следующий напечатанный короб.",
            )
        if payload.get(MARKING_SCAN_MARKER):
            if not _scan_equal(scan, expected):
                return _command_result(ok=False, error=f"Ожидается ШК товара {expected}.")
            execution["pending_marking_scan"] = True
            execution["pending_product_barcode"] = scan
            execution["last_scan"] = scan
            payload["mobile_execution"] = execution
            task.payload = payload
            task.save(update_fields=["payload", "updated_at"])
            return _command_result(
                ok=True,
                task=task,
                payload=payload,
                message=(
                    "ШК товара принят. Теперь отсканируйте Data Matrix "
                    "Честного знака этой же единицы."
                ),
            )
        try:
            with transaction.atomic():
                prepared_item = record_prepared_box_item_scan(
                    allocation_id=allocation.id,
                    prepared_box_id=active_box_id,
                    item_scan=scan,
                    performed_by=user,
                )
                execution["units_scanned_qty"] = (
                    int(execution.get("units_scanned_qty") or 0) + 1
                )
                execution["last_scan"] = scan
                payload["mobile_execution"] = execution
                task.payload = payload
                task.save(update_fields=["payload", "updated_at"])
                if int(prepared_item.qty_scanned or 0) >= int(
                    allocation.qty_planned or 0
                ):
                    stage_prepared_box_allocation(
                        allocation_id=allocation.id,
                        performed_by=user,
                    )
        except FbsReplenishmentError as exc:
            return _command_result(ok=False, error=str(exc))
        if int(execution["units_scanned_qty"]) >= int(task.qty_planned or 0):
            try:
                task.refresh_from_db()
            except MoveTask.DoesNotExist:
                return _command_result(ok=False, error="Задание FBS не найдено после проводки.")
            return _command_result(
                ok=True,
                task=task,
                payload=dict(task.payload or {}),
                message="Товар задания собран в постоянный FBS-короб.",
                completed=True,
            )
        return _command_result(
            ok=True,
            task=task,
            payload=payload,
            message=(
                f"Товар подтвержден: {execution['units_scanned_qty']} из {task.qty_planned}."
            ),
        )
    if payload.get("fbs_placement_task"):
        box_code = str(payload.get("source_box_code") or task.pallet_code or "").strip()
        if step == "boxes":
            if not _scan_equal(scan, box_code):
                return _command_result(
                    ok=False,
                    error=f"Ожидается QR подготовленного FBS-короба {box_code}.",
                )
            execution["box_confirmed"] = True
            execution["last_scan"] = scan
            _mark_container_in_transit(
                task=task,
                payload=payload,
                employee_id=employee_id,
                employee_name=employee_name,
                scanned_code=scan,
            )
            payload["mobile_execution"] = execution
            task.payload = payload
            task.save(update_fields=["payload", "updated_at"])
            return _command_result(
                ok=True,
                task=task,
                payload=payload,
                message="FBS-короб подтвержден. Отвезите его к свободной ячейке OS.",
            )
        if step == "destination":
            from fbs.services.replenishment import complete_staged_item_plan_placement

            try:
                complete_staged_item_plan_placement(
                    plan_id=int(payload.get("fbs_plan_id") or 0),
                    target_box_scan=box_code,
                    destination_scan=scan,
                    performed_by=user,
                )
            except FbsReplenishmentError as exc:
                return _command_result(ok=False, error=str(exc))
            plan = FbsReplenishmentPlan.objects.select_related(
                "target_cell__location"
            ).get(pk=int(payload.get("fbs_plan_id") or 0))
            destination = plan.target_cell.location
            destination_code = _location_scan_code(destination)
            execution["destination_confirmed"] = True
            execution["last_scan"] = destination_code or scan
            _mark_container_placed(
                task=task,
                payload=payload,
                destination=destination,
            )
            payload.update(
                {
                    "status": MoveTask.STATUS_DONE,
                    "status_label": "Выполнено",
                    "destination_scan_code": destination_code,
                    "to_location": _location_payload(destination),
                    "to_label": _location_label(destination),
                    "mobile_execution": execution,
                }
            )
            now = timezone.now()
            task.to_zone = "OS"
            task.to_row = int(destination.row_no or 0) or None
            task.to_section = int(destination.section_no or 0) or None
            task.to_tier = int(destination.tier_no or 0) or None
            task.to_cell = int(destination.cell_no or 0) or None
            task.status = MoveTask.STATUS_DONE
            task.qty_done = task.qty_planned
            task.completed_at = now
            task.payload = payload
            task.save(
                update_fields=[
                    "to_zone",
                    "to_row",
                    "to_section",
                    "to_tier",
                    "to_cell",
                    "status",
                    "qty_done",
                    "completed_at",
                    "payload",
                    "updated_at",
                ]
            )
            move_request = task.request
            move_request.destination_zone = "OS"
            move_request.destination_row = task.to_row
            move_request.destination_section = task.to_section
            move_request.destination_tier = task.to_tier
            move_request.destination_cell = task.to_cell
            move_request.save(
                update_fields=[
                    "destination_zone",
                    "destination_row",
                    "destination_section",
                    "destination_tier",
                    "destination_cell",
                    "updated_at",
                ]
            )
            _sync_request(move_request)
            return _command_result(
                ok=True,
                task=task,
                payload=payload,
                message=f"FBS-короб размещен в ячейке {destination_code}.",
                completed=True,
            )
        return _command_result(ok=True, task=task, payload=payload, completed=True)
    if step == "pallet":
        if not _scan_equal(scan, expected):
            return _command_result(ok=False, error=f"Ожидается QR паллеты {expected}.")
        execution["pallet_confirmed"] = True
        if payload.get(FULL_SOURCE_PALLET_MARKER):
            _mark_container_in_transit(
                task=task,
                payload=payload,
                employee_id=employee_id,
                employee_name=employee_name,
                scanned_code=scan,
            )
        message = "Паллета подтверждена."
    elif step == "boxes":
        if not _scan_equal(scan, expected):
            return _command_result(ok=False, error=f"Ожидается QR короба {expected}.")
        execution["box_confirmed"] = True
        _mark_container_in_transit(
            task=task,
            payload=payload,
            employee_id=employee_id,
            employee_name=employee_name,
            scanned_code=scan,
        )
        message = "Короб подтвержден."
    elif step == "units":
        if not _scan_equal(scan, expected):
            return _command_result(ok=False, error=f"Ожидается ШК товара {expected}.")
        if payload.get(MARKING_SCAN_MARKER):
            execution["pending_marking_scan"] = True
            execution["pending_product_barcode"] = scan
            message = (
                "ШК товара принят. Теперь отсканируйте Data Matrix "
                "Честного знака этой же единицы."
            )
        else:
            qty = int(task.qty_planned or 0)
            execution["units_scanned_qty"] = min(
                int(execution.get("units_scanned_qty") or 0) + 1,
                qty,
            )
            message = f"Товар подтвержден: {execution['units_scanned_qty']} из {qty}."
    elif step == "marking":
        pending_barcode = str(execution.get("pending_product_barcode") or "").strip()
        allocations = _task_allocations(task)
        if (
            not payload.get(MARKING_SCAN_MARKER)
            or not pending_barcode
            or len(allocations) != 1
        ):
            return _command_result(
                ok=False,
                error="Не найдено подтвержденное штучное задание перед сканированием ЧЗ.",
            )
        from fbs.services.replenishment import record_fbs_movement_marking_scan

        try:
            record_fbs_movement_marking_scan(
                allocation_id=allocations[0].id,
                marking_scan=scan,
                performed_by=user,
            )
        except FbsReplenishmentError as exc:
            return _command_result(ok=False, error=str(exc))
        qty = int(task.qty_planned or 0)
        execution["units_scanned_qty"] = min(
            int(execution.get("units_scanned_qty") or 0) + 1,
            qty,
        )
        execution["pending_marking_scan"] = False
        execution["pending_product_barcode"] = ""
        message = (
            "ШК и Честный знак подтверждены: "
            f"{execution['units_scanned_qty']} из {qty}."
        )
    elif step == "staging_container":
        if not _scan_equal(scan, expected):
            return _command_result(
                ok=False,
                error=f"Ожидается QR временного короба {expected}.",
            )
        execution["staging_container_confirmed"] = True
        execution["staging_container_scan"] = scan
        message = "Товар подтвержден во временном коробе заявки."
    elif step == "destination":
        item_mode = task.move_mode == MoveTask.MODE_BOX_PARTIAL
        if item_mode and not _scan_equal(scan, expected):
            return _command_result(ok=False, error=f"Ожидается QR места {expected}.")
        execution["destination_confirmed"] = True
        execution["last_scan"] = scan
        allocations = _task_allocations(task)
        if not allocations:
            return _command_result(ok=False, error="Не найдена связь задания с FBS-перемещением.")
        from fbs.services.replenishment import (
            complete_replenishment_allocation,
            complete_replenishment_box_allocations,
            stage_replenishment_allocation,
        )

        try:
            with transaction.atomic():
                box_mode = (
                    allocations[0].line.plan.mode == FbsReplenishmentPlan.MODE_BOX
                )
                if box_mode:
                    complete_replenishment_box_allocations(
                        allocation_ids=[allocation.id for allocation in allocations],
                        source_scan=str(
                            allocations[0].source_snapshot.container.container_code or ""
                        ).strip(),
                        target_box_scan=scan,
                        performed_by=user,
                    )
                for allocation in (() if box_mode else allocations):
                    source_scan = str(
                        allocation.source_snapshot.container.container_code or ""
                    ).strip()
                    uses_staging = bool(
                        allocation.line.plan.mode == FbsReplenishmentPlan.MODE_ITEM
                        and allocation.line.plan.staging_location_id
                    )
                    if uses_staging:
                        stage_replenishment_allocation(
                            allocation_id=allocation.id,
                            source_scan=source_scan,
                            staging_container_scan=str(
                                execution.get("staging_container_scan") or ""
                            ),
                            staging_scan=scan,
                            performed_by=user,
                        )
                    else:
                        complete_replenishment_allocation(
                            allocation_id=allocation.id,
                            source_scan=source_scan,
                            target_box_scan=scan,
                            performed_by=user,
                        )
        except FbsReplenishmentError as exc:
            return _command_result(ok=False, error=str(exc))
        except Exception:
            logger.exception(
                "fbs_box_batch_completion_failed task_id=%s plan_id=%s "
                "allocation_count=%s destination_scan=%s",
                task.id,
                allocations[0].line.plan_id,
                len(allocations),
                scan,
            )
            return _command_result(
                ok=False,
                error=(
                    "Не удалось провести FBS-короб. Изменения отменены; "
                    "сообщите администратору и повторите после проверки."
                ),
            )
        task.refresh_from_db()
        if task.status != MoveTask.STATUS_DONE:
            return _command_result(
                ok=False,
                error="FBS-задание не закрылось после складской проводки.",
            )
        plan_mode = allocations[0].line.plan.mode
        return _command_result(
            ok=True,
            task=task,
            payload=dict(task.payload or {}),
            message=(
                "FBS-перемещение передано кладовщику."
                if plan_mode == FbsReplenishmentPlan.MODE_ITEM
                and allocations[0].line.plan.staging_location_id
                else "FBS-перемещение выполнено."
            ),
            completed=True,
        )
    else:
        return _command_result(ok=True, task=task, payload=payload, completed=True)
    execution["last_scan"] = scan
    payload["mobile_execution"] = execution
    task.payload = payload
    task.save(update_fields=["payload", "updated_at"])
    return _command_result(ok=True, task=task, payload=payload, message=message)


def complete_fbs_move_task(*, task: MoveTask):
    task.refresh_from_db()
    if task.status == MoveTask.STATUS_DONE:
        return _command_result(
            ok=True,
            task=task,
            payload=dict(task.payload or {}),
            message="FBS-задание выполнено.",
            completed=True,
        )
    return _command_result(ok=False, error="Завершите обязательные сканы задания.")
