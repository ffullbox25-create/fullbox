from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Prefetch, Q
from django.utils import timezone

from fullbox.order_numbers import format_order_number
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseOperation,
    WarehouseOperationTask,
    WarehouseStockSnapshot,
)
from sklad.services.warehouse_events import WarehouseEventType
from sklad.services.warehouse_transitions import WarehouseStateCode


PROBLEM_BOX_CONTEXT_TYPE = "warehouse_problem_box_return"
PROBLEM_BOX_REASON_FRAGMENT = "not included in shipping packing act"
PROBLEM_BOX_STATES = {
    WarehouseStateCode.MOVING_TO_OTG.value,
    WarehouseStateCode.IN_OTG.value,
    WarehouseStateCode.PALLETIZING.value,
    WarehouseStateCode.READY_FOR_LOADING.value,
    WarehouseStateCode.ASSIGNED_TO_TRIP.value,
    WarehouseStateCode.LOADING_IN_PROGRESS.value,
    WarehouseStateCode.LOADED_TO_VEHICLE.value,
}
ACTIVE_OPERATION_STATUSES = {
    WarehouseOperation.STATUS_CREATED,
    WarehouseOperation.STATUS_PLANNED,
    WarehouseOperation.STATUS_IN_PROGRESS,
    WarehouseOperation.STATUS_PARTIAL,
    WarehouseOperation.STATUS_BLOCKED,
}
DAILY_PROBLEM_CONTROL_ROUTE_PREFIX = "/sklad/journal/?daily_problem_check="
MISSING_BOX_CONTEXT_TYPE = "missing_box_check"
MISSING_BOX_ACTIVE_STATUSES = {
    WarehouseOperation.STATUS_BLOCKED,
    WarehouseOperation.STATUS_IN_PROGRESS,
}


@dataclass(frozen=True)
class ProblemBoxCheck:
    is_problem: bool
    reason: str = ""


def _payload(snapshot: WarehouseStockSnapshot) -> dict:
    event = getattr(snapshot, "last_event", None)
    payload = getattr(event, "payload", None) if event is not None else None
    return payload if isinstance(payload, dict) else {}


def snapshot_box_code(snapshot: WarehouseStockSnapshot) -> str:
    container = getattr(snapshot, "container", None)
    return str(getattr(container, "container_code", "") or snapshot.container_code or "").strip()


def check_problem_box_snapshot(
    snapshot: WarehouseStockSnapshot,
    *,
    allow_claimed_operation: bool = True,
) -> ProblemBoxCheck:
    if snapshot.is_archived or int(snapshot.qty or 0) <= 0:
        return ProblemBoxCheck(False, "Короб уже не содержит активного остатка.")
    if str(snapshot.zone_code or "").strip().upper() != "OTG":
        return ProblemBoxCheck(False, "Короб уже не находится в зоне OTG.")
    if str(snapshot.warehouse_state_code or "").strip() not in PROBLEM_BOX_STATES:
        return ProblemBoxCheck(False, "Состояние короба уже изменилось.")
    if int(snapshot.available_qty or 0) != 0:
        return ProblemBoxCheck(False, "Короб уже доступен на остатке.")
    if any(
        int(value or 0) != 0
        for value in (
            snapshot.processing_reserved_qty,
            snapshot.shipping_reserved_qty,
            snapshot.other_reserved_qty,
        )
    ):
        return ProblemBoxCheck(False, "На коробе появился складской резерв.")
    if snapshot.parent_container_id:
        return ProblemBoxCheck(False, "Короб уже включен в паллету.")
    if str(snapshot.current_trip_id or "").strip() or snapshot.is_in_vehicle:
        return ProblemBoxCheck(False, "Короб уже включен в рейс или транспорт.")
    event = getattr(snapshot, "last_event", None)
    if event is None or str(event.event_type or "").strip() != WarehouseEventType.WAREHOUSE_CONTEXT_CANCELED.value:
        return ProblemBoxCheck(False, "Последнее складское событие короба изменилось.")
    reason = str(_payload(snapshot).get("reason") or "").strip().lower()
    if PROBLEM_BOX_REASON_FRAGMENT not in reason:
        return ProblemBoxCheck(False, "Причина исключения короба из отгрузки не подтверждена.")
    if not snapshot_box_code(snapshot):
        return ProblemBoxCheck(False, "У складской единицы отсутствует ШК короба.")
    operation = getattr(snapshot, "active_operation", None)
    if operation is not None and str(operation.status or "").strip() in ACTIVE_OPERATION_STATUSES:
        is_own_claim = (
            allow_claimed_operation
            and operation.operation_type == WarehouseOperation.TYPE_RETURN_TO_STORAGE
            and operation.context_type == PROBLEM_BOX_CONTEXT_TYPE
            and str(operation.context_id or "").strip() == str(snapshot.id)
        )
        if not is_own_claim:
            return ProblemBoxCheck(False, "Короб уже участвует в другой складской операции.")
    return ProblemBoxCheck(True)


def problem_box_snapshot_queryset(*, agency=None):
    queryset = (
        WarehouseStockSnapshot.objects.filter(
            is_archived=False,
            qty__gt=0,
            available_qty=0,
            zone_code__iexact="OTG",
            warehouse_state_code__in=PROBLEM_BOX_STATES,
            parent_container__isnull=True,
            current_trip_id="",
            is_in_vehicle=False,
            processing_reserved_qty=0,
            shipping_reserved_qty=0,
            other_reserved_qty=0,
            last_event__event_type=WarehouseEventType.WAREHOUSE_CONTEXT_CANCELED.value,
        )
        .filter(
            Q(active_operation__isnull=True)
            | Q(
                active_operation__operation_type=WarehouseOperation.TYPE_RETURN_TO_STORAGE,
                active_operation__context_type=PROBLEM_BOX_CONTEXT_TYPE,
                active_operation__status__in=ACTIVE_OPERATION_STATUSES,
            )
        )
        .select_related(
            "agency",
            "container",
            "parent_container",
            "location",
            "last_event",
            "last_event__performed_by",
            "active_operation",
        )
        .prefetch_related("active_operation__tasks")
        .order_by("last_event__occurred_at", "id")
    )
    if agency is not None:
        queryset = queryset.filter(agency=agency)
    return queryset


def problem_box_snapshots(*, agency=None) -> list[WarehouseStockSnapshot]:
    return [
        snapshot
        for snapshot in problem_box_snapshot_queryset(agency=agency)
        if check_problem_box_snapshot(snapshot).is_problem
    ]


def problem_box_snapshot_ids(*, agency=None) -> set[int]:
    return {int(snapshot.id) for snapshot in problem_box_snapshots(agency=agency)}


def _shipping_document(snapshot: WarehouseStockSnapshot) -> str:
    payload = _payload(snapshot)
    raw_value = str(
        payload.get("operation_context_id")
        or payload.get("shipping_order_id")
        or payload.get("order_id")
        or ""
    ).strip()
    if not raw_value:
        return "-"
    if raw_value.upper().startswith("OTG-"):
        return raw_value
    try:
        return format_order_number("shipping", raw_value)
    except Exception:
        return raw_value


def daily_problem_control_route(day=None) -> str:
    target_day = day or timezone.localdate()
    return f"{DAILY_PROBLEM_CONTROL_ROUTE_PREFIX}{target_day.isoformat()}#daily-problem-control"


def problem_pallet_snapshot_queryset(*, agency=None):
    queryset = (
        WarehouseStockSnapshot.objects.filter(
            is_archived=False,
            qty__gt=0,
            available_qty=0,
            zone_code__iexact="OTG",
            warehouse_state_code__in=PROBLEM_BOX_STATES,
            parent_container__isnull=False,
            parent_container__container_type__in={
                WarehouseContainer.TYPE_PALLET,
                WarehouseContainer.TYPE_MIXED_PALLET,
            },
            parent_container__status=WarehouseContainer.STATUS_ACTIVE,
            current_trip_id="",
            is_in_vehicle=False,
            processing_reserved_qty=0,
            shipping_reserved_qty=0,
            other_reserved_qty=0,
            last_event__event_type=WarehouseEventType.WAREHOUSE_CONTEXT_CANCELED.value,
        )
        .filter(
            Q(active_operation__isnull=True)
            | ~Q(active_operation__status__in=ACTIVE_OPERATION_STATUSES)
        )
        .select_related(
            "agency",
            "container",
            "parent_container",
            "parent_container__current_location",
            "last_event",
        )
        .order_by("parent_container_id", "last_event__occurred_at", "id")
    )
    if agency is not None:
        queryset = queryset.filter(agency=agency)
    return queryset


def _has_confirmed_problem_marker(snapshot: WarehouseStockSnapshot) -> bool:
    event = getattr(snapshot, "last_event", None)
    if event is None or str(event.event_type or "").strip() != WarehouseEventType.WAREHOUSE_CONTEXT_CANCELED.value:
        return False
    return PROBLEM_BOX_REASON_FRAGMENT in str(_payload(snapshot).get("reason") or "").strip().lower()


def problem_pallet_rows(*, agency=None) -> list[dict]:
    grouped: dict[int, dict] = {}
    for snapshot in problem_pallet_snapshot_queryset(agency=agency):
        if not _has_confirmed_problem_marker(snapshot):
            continue
        pallet = snapshot.parent_container
        if pallet is None:
            continue
        row = grouped.setdefault(
            int(pallet.id),
            {
                "pallet_id": int(pallet.id),
                "pallet_code": str(pallet.container_code or "-").strip() or "-",
                "client_label": str(getattr(snapshot.agency, "agn_name", "") or "-").strip() or "-",
                "location_label": str(
                    getattr(getattr(pallet, "current_location", None), "location_code", "") or "-"
                ).strip()
                or "-",
                "qty": 0,
                "box_codes": set(),
                "shipping_documents": set(),
                "sku_labels": set(),
                "reason": "В паллете есть короб, исключенный из акта отгрузки и оставшийся недоступным в OTG",
                "detected_at": getattr(snapshot.last_event, "occurred_at", None),
            },
        )
        row["qty"] += int(snapshot.qty or 0)
        box_code = snapshot_box_code(snapshot)
        if box_code:
            row["box_codes"].add(box_code)
        shipping_document = _shipping_document(snapshot)
        if shipping_document and shipping_document != "-":
            row["shipping_documents"].add(shipping_document)
        sku_label = " · ".join(
            value
            for value in (
                str(snapshot.sku_code or "").strip(),
                str(snapshot.name or "").strip(),
            )
            if value
        )
        if sku_label:
            row["sku_labels"].add(sku_label)
        detected_at = getattr(snapshot.last_event, "occurred_at", None)
        if detected_at and (row["detected_at"] is None or detected_at < row["detected_at"]):
            row["detected_at"] = detected_at

    rows = []
    for row in grouped.values():
        box_codes = sorted(row.pop("box_codes"))
        row["box_codes"] = box_codes
        row["box_count"] = len(box_codes)
        row["shipping_documents"] = sorted(row["shipping_documents"])
        row["sku_labels"] = sorted(row["sku_labels"])
        rows.append(row)
    rows.sort(key=lambda item: (item["detected_at"] or timezone.now(), item["pallet_code"]))
    return rows


def problem_box_rows(*, agency=None) -> list[dict]:
    rows = []
    for snapshot in problem_box_snapshots(agency=agency):
        operation = snapshot.active_operation
        active_task = None
        if operation is not None:
            active_task = next(
                (
                    task
                    for task in operation.tasks.all()
                    if task.status in {
                        task.STATUS_CREATED,
                        task.STATUS_IN_PROGRESS,
                    }
                ),
                None,
            )
        event = snapshot.last_event
        performer = getattr(event, "performed_by", None)
        active_task_payload = (
            dict(active_task.payload or {})
            if active_task is not None and isinstance(active_task.payload, dict)
            else {}
        )
        rows.append(
            {
                "snapshot_id": int(snapshot.id),
                "last_event_id": int(snapshot.last_event_id or 0),
                "operation_id": int(operation.id) if operation is not None else 0,
                "assigned_to_id": int(active_task.assigned_to_id or 0) if active_task is not None else 0,
                "assigned_to_name": (
                    str(active_task.assigned_to_name or "").strip()
                    if active_task is not None
                    else ""
                ),
                "reachtruck_move_id": str(active_task_payload.get("reachtruck_move_id") or "").strip(),
                "box_code": snapshot_box_code(snapshot),
                "sku": str(snapshot.sku_code or "-").strip() or "-",
                "name": str(snapshot.name or "-").strip() or "-",
                "size": str(snapshot.size or "-").strip() or "-",
                "barcode": str(snapshot.barcode or "-").strip() or "-",
                "qty": int(snapshot.qty or 0),
                "client_label": str(getattr(snapshot.agency, "agn_name", "") or "-").strip() or "-",
                "shipping_document": _shipping_document(snapshot),
                "reason": "Исключен из акта отгрузки, но остался недоступным в зоне OTG",
                "detected_at": getattr(event, "occurred_at", None),
                "detected_by": (
                    getattr(performer, "get_full_name", lambda: "")()
                    or getattr(performer, "username", "")
                    or str(getattr(event, "performed_by_role", "") or "").strip()
                    or "Система"
                ),
            }
        )
    return rows


def _normalized_search(value) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def missing_box_rows(*, agency=None, query: str = "") -> list[dict]:
    """Return driver-reported missing boxes without changing warehouse state."""
    active_tasks = WarehouseOperationTask.objects.filter(
        status__in={
            WarehouseOperationTask.STATUS_CREATED,
            WarehouseOperationTask.STATUS_IN_PROGRESS,
        }
    ).select_related("assigned_to")
    operations = (
        WarehouseOperation.objects.filter(
            context_type=MISSING_BOX_CONTEXT_TYPE,
            status__in=MISSING_BOX_ACTIVE_STATUSES,
        )
        .select_related("agency", "source_location", "requested_by")
        .prefetch_related(
            "active_snapshots__container",
            "active_snapshots__parent_container",
            "active_snapshots__location",
            "active_snapshots__last_event",
            Prefetch("tasks", queryset=active_tasks, to_attr="active_check_tasks"),
        )
        .order_by("created_at", "id")
    )
    if agency is not None:
        operations = operations.filter(agency=agency)

    normalized_query = _normalized_search(query)
    rows = []
    for operation in operations:
        snapshots = [
            snapshot
            for snapshot in operation.active_snapshots.all()
            if not snapshot.is_archived and int(snapshot.qty or 0) > 0
        ]
        if not snapshots:
            continue
        first_snapshot = snapshots[0]
        container = first_snapshot.container
        box_code = snapshot_box_code(first_snapshot)
        if not box_code:
            continue
        active_task = next(iter(getattr(operation, "active_check_tasks", []) or []), None)
        source_location = operation.source_location or first_snapshot.location
        reporter = operation.requested_by
        replacement_event = (
            WarehouseEvent.objects.filter(operation=operation)
            .exclude(event_type="missing_box_reported")
            .order_by("-id")
            .first()
        )
        if replacement_event is None:
            replacement_event = (
                WarehouseEvent.objects.filter(
                    payload__quarantine_operation_id=int(operation.id),
                )
                .order_by("-id")
                .first()
            )
        replacement_payload = (
            replacement_event.payload
            if replacement_event is not None and isinstance(replacement_event.payload, dict)
            else {}
        )
        pallet_codes = sorted(
            {
                str(getattr(snapshot.parent_container, "container_code", "") or "").strip()
                for snapshot in snapshots
                if str(getattr(snapshot.parent_container, "container_code", "") or "").strip()
            }
        )
        sku_labels = []
        for snapshot in snapshots:
            sku_label = " · ".join(
                value
                for value in (
                    str(snapshot.sku_code or "").strip(),
                    str(snapshot.name or "").strip(),
                    str(snapshot.size or "").strip(),
                )
                if value
            )
            if sku_label:
                sku_labels.append(f"{sku_label}: {int(snapshot.qty or 0)} шт.")
        location_label = str(
            getattr(source_location, "location_code", "")
            or getattr(source_location, "display_name", "")
            or operation.source_zone_code
            or first_snapshot.zone_code
            or "-"
        ).strip() or "-"
        reported_by = (
            getattr(reporter, "get_full_name", lambda: "")()
            or getattr(reporter, "username", "")
            or operation.requested_by_role
            or "Система"
        )
        row = {
            "operation_id": int(operation.id),
            "snapshot_id": int(first_snapshot.id),
            "box_code": box_code,
            "pallet_code": ", ".join(pallet_codes) or "-",
            "client_label": str(getattr(operation.agency, "agn_name", "") or "-").strip() or "-",
            "location_label": location_label,
            "source_document_type": str(operation.source_document_type or "-").strip() or "-",
            "source_document_id": str(operation.source_document_id or "-").strip() or "-",
            "qty": sum(int(snapshot.qty or 0) for snapshot in snapshots),
            "sku_labels": sku_labels,
            "reported_at": operation.created_at,
            "reported_by": reported_by,
            "assigned_to_id": int(active_task.assigned_to_id or 0) if active_task else 0,
            "assigned_to_name": (
                str(active_task.assigned_to_name or "").strip()
                if active_task
                else ""
            ),
            "replacement_box_code": str(
                replacement_payload.get("replacement_box_code") or ""
            ).strip(),
            "reason": str(operation.comment or "Короб не найден на месте.").strip(),
        }
        if normalized_query:
            search_blob = _normalized_search(
                " ".join(
                    [
                        row["box_code"],
                        row["pallet_code"],
                        row["client_label"],
                        row["location_label"],
                        row["source_document_type"],
                        row["source_document_id"],
                        row["reported_by"],
                        row["assigned_to_name"],
                        row["replacement_box_code"],
                        *row["sku_labels"],
                    ]
                )
            )
            if normalized_query not in search_blob:
                continue
        rows.append(row)
    return rows
