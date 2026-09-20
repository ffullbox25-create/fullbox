from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from reachtruck.models import BoxClaim, MoveTask
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseOperationTask,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sklad.services.warehouse_events import WarehouseEventType
from sklad.services.warehouse_transitions import (
    WarehouseStateCode,
    WarehouseTransitionError,
    WarehouseTransitionService,
)


MISSING_BOX_CONTEXT_TYPE = "missing_box_check"
MISSING_BOX_ACTIVE_RESERVE_STATUSES = {
    WarehouseReserve.STATUS_ACTIVE,
    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
    WarehouseReserve.STATUS_ALLOCATED,
    WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
}
MISSING_BOX_ALLOWED_ROLES = {
    "storekeeper",
    "head_manager",
    "admin",
    "director",
    "developer",
}


@dataclass(frozen=True)
class ExactBoxReplacement:
    container: WarehouseContainer
    pallet: WarehouseContainer
    location: object
    snapshots: tuple[WarehouseStockSnapshot, ...]


def _clean(value) -> str:
    return str(value or "").strip()


def _actor_role(user) -> str:
    employee = getattr(user, "employee_profile", None)
    role = _clean(getattr(employee, "role", "")).lower()
    if role:
        return role
    if getattr(user, "is_superuser", False):
        return "admin"
    return ""


def _require_problem_actor(user) -> str:
    if not getattr(user, "is_authenticated", False):
        raise ValueError("Пользователь проверки не авторизован.")
    role = _actor_role(user)
    if role not in MISSING_BOX_ALLOWED_ROLES:
        raise ValueError("Недостаточно прав для проверки отсутствующего короба.")
    return role


def _missing_operation(operation_id: int, *, lock: bool = True) -> WarehouseOperation:
    queryset = WarehouseOperation.objects
    if lock:
        queryset = queryset.select_for_update()
    operation = (
        queryset.filter(
            id=int(operation_id),
            context_type=MISSING_BOX_CONTEXT_TYPE,
            status__in={
                WarehouseOperation.STATUS_BLOCKED,
                WarehouseOperation.STATUS_IN_PROGRESS,
            },
        )
        .select_related("agency", "source_location")
        .first()
    )
    if operation is None:
        raise ValueError("Проверка отсутствующего короба не найдена или уже закрыта.")
    return operation


def _missing_operation_snapshots(operation: WarehouseOperation) -> list[WarehouseStockSnapshot]:
    rows = list(
        WarehouseStockSnapshot.objects.select_for_update(of=("self",))
        .select_related("container", "parent_container", "location", "active_operation")
        .filter(
            agency_id=operation.agency_id,
            active_operation=operation,
            is_archived=False,
            qty__gt=0,
        )
        .order_by("id")
    )
    if not rows:
        raise ValueError("Активный остаток отсутствующего короба не найден.")
    container_ids = {int(row.container_id or 0) for row in rows}
    if len(container_ids) != 1 or 0 in container_ids:
        raise ValueError("Проверка содержит несогласованные складские строки короба.")
    return rows


def _active_check_task(operation: WarehouseOperation) -> WarehouseOperationTask | None:
    return (
        WarehouseOperationTask.objects.select_for_update()
        .filter(
            operation=operation,
            status__in={
                WarehouseOperationTask.STATUS_CREATED,
                WarehouseOperationTask.STATUS_IN_PROGRESS,
            },
        )
        .order_by("id")
        .first()
    )


def _require_assigned_checker(operation: WarehouseOperation, user) -> WarehouseOperationTask:
    task = _active_check_task(operation)
    if task is None:
        raise ValueError("Сначала возьмите короб в проверку.")
    actor_id = int(getattr(user, "id", 0) or 0)
    role = _require_problem_actor(user)
    if int(task.assigned_to_id or 0) != actor_id and role not in {
        "head_manager",
        "admin",
        "director",
        "developer",
    }:
        raise ValueError("Короб уже проверяет другой сотрудник.")
    return task


@transaction.atomic
def take_missing_box_for_check(*, operation_id: int, performed_by) -> WarehouseOperation:
    """Assign a physical check without changing stock, location or reserves."""
    role = _require_problem_actor(performed_by)
    operation = _missing_operation(operation_id)
    rows = _missing_operation_snapshots(operation)
    existing = _active_check_task(operation)
    actor_id = int(getattr(performed_by, "id", 0) or 0)
    if existing is not None:
        if int(existing.assigned_to_id or 0) == actor_id:
            return operation
        raise ValueError("Короб уже взят в проверку другим сотрудником.")
    now = timezone.now()
    actor_name = (
        getattr(performed_by, "get_full_name", lambda: "")()
        or getattr(performed_by, "username", "")
        or ""
    )
    first = rows[0]
    WarehouseOperationTask.objects.create(
        operation=operation,
        task_type=WarehouseOperationTask.TYPE_BOX_MOVE,
        container=first.container,
        from_location=first.location,
        from_zone_code=str(first.zone_code or ""),
        qty_planned=sum(int(row.qty or 0) for row in rows),
        status=WarehouseOperationTask.STATUS_IN_PROGRESS,
        assigned_to=performed_by,
        assigned_to_name=actor_name,
        executor_role=role,
        payload={
            "missing_box_check": True,
            "box_code": _clean(first.container.container_code),
            "accounting_qty_unchanged": True,
        },
        started_at=now,
    )
    operation.status = WarehouseOperation.STATUS_IN_PROGRESS
    operation.started_at = operation.started_at or now
    operation.save(update_fields=["status", "started_at", "updated_at"])
    return operation


@transaction.atomic
def mark_missing_box_not_found(*, operation_id: int, performed_by) -> WarehouseOperation:
    """Record a failed physical check and deliberately keep quarantine unchanged."""
    operation = _missing_operation(operation_id)
    task = _require_assigned_checker(operation, performed_by)
    rows = _missing_operation_snapshots(operation)
    first = rows[0]
    now = timezone.now()
    WarehouseEvent.objects.create(
        agency_id=operation.agency_id,
        event_type="missing_box_check_not_found",
        stock_context_type=MISSING_BOX_CONTEXT_TYPE,
        stock_context_id=str(operation.id),
        container=first.container,
        operation=operation,
        operation_task=task,
        source_document_type=operation.source_document_type,
        source_document_id=operation.source_document_id,
        from_location=first.location,
        from_zone_code=first.zone_code,
        qty=sum(int(row.qty or 0) for row in rows),
        payload={
            "box_code": _clean(first.container.container_code),
            "stock_unchanged": True,
            "location_unchanged": True,
            "quarantine_unchanged": True,
        },
        performed_by=performed_by,
        performed_by_role=_actor_role(performed_by),
        occurred_at=now,
    )
    payload = dict(task.payload or {})
    payload.update(
        {
            "last_check_result": "not_found",
            "last_checked_at": timezone.localtime(now).isoformat(),
            "last_checked_by": int(getattr(performed_by, "id", 0) or 0),
        }
    )
    task.payload = payload
    task.save(update_fields=["payload", "updated_at"])
    return operation


def _reserve_identity(reserve: WarehouseReserve) -> tuple:
    expiry = getattr(reserve, "expiry_date", None)
    return (
        int(reserve.sku_ref_id or 0),
        _clean(reserve.sku_code),
        _clean(reserve.size),
        _clean(reserve.barcode),
        _clean(reserve.goods_type).casefold(),
        _clean(reserve.marking_code),
        _clean(getattr(reserve, "lot_code", "")),
        expiry.isoformat() if expiry else "",
    )


@transaction.atomic
def resolve_found_missing_box(
    *,
    operation_id: int,
    scanned_box_code: str,
    destination_location_code: str,
    performed_by,
) -> WarehouseOperation:
    """Atomically place a found box and release only its missing-box quarantine."""
    operation = _missing_operation(operation_id)
    task = _require_assigned_checker(operation, performed_by)
    rows = _missing_operation_snapshots(operation)
    first = rows[0]
    expected_box_code = _clean(first.container.container_code)
    if not _clean(scanned_box_code):
        raise ValueError("Отсканируйте ШК найденного короба.")
    if _clean(scanned_box_code).casefold() != expected_box_code.casefold():
        raise ValueError(f"Отсканирован другой короб. Ожидается {expected_box_code}.")

    location_code = _clean(destination_location_code)
    if not location_code:
        raise ValueError("Отсканируйте фактическую ячейку хранения.")
    destination_query = WarehouseLocation.objects.select_for_update().filter(
        location_code__iexact=location_code,
        zone_code__in={"OS", "MR"},
        is_active=True,
        is_storage=True,
    )
    warehouse_code = _clean(getattr(first.location, "warehouse_code", ""))
    if warehouse_code:
        destination_query = destination_query.filter(warehouse_code=warehouse_code)
    destination = destination_query.order_by("id").first()
    if destination is None:
        raise ValueError("Активная складская ячейка с таким ШК не найдена.")
    from sklad.services.warehouse_write_path import WarehouseWritePathService

    WarehouseWritePathService.ensure_putaway_destination_available(
        destination=destination,
        exclude_container_code=expected_box_code,
    )

    reserves = list(
        WarehouseReserve.objects.select_for_update()
        .filter(
            agency_id=operation.agency_id,
            context_type=MISSING_BOX_CONTEXT_TYPE,
            context_id=str(operation.id),
            status__in=MISSING_BOX_ACTIVE_RESERVE_STATUSES,
        )
        .order_by("id")
    )
    reserve_qty_by_identity: dict[tuple, int] = {}
    for reserve in reserves:
        held = max(int(reserve.qty_allocated or 0) - int(reserve.qty_satisfied or 0), 0)
        reserve_qty_by_identity[_reserve_identity(reserve)] = (
            reserve_qty_by_identity.get(_reserve_identity(reserve), 0) + held
        )
    snapshot_qty_by_identity: dict[tuple, int] = {}
    for snapshot in rows:
        snapshot_qty_by_identity[_snapshot_identity(snapshot)[:-1]] = (
            snapshot_qty_by_identity.get(_snapshot_identity(snapshot)[:-1], 0)
            + int(snapshot.other_reserved_qty or 0)
        )
    if not reserves or reserve_qty_by_identity != snapshot_qty_by_identity:
        raise ValueError(
            "Карантинный резерв короба не совпадает с остатком; автоматическое исправление запрещено."
        )

    now = timezone.now()
    for snapshot in rows:
        release_qty = int(snapshot.other_reserved_qty or 0)
        if release_qty <= 0:
            raise ValueError("На строке короба отсутствует карантинный резерв.")
        new_available = int(snapshot.available_qty or 0) + release_qty
        if new_available > int(snapshot.qty or 0):
            raise ValueError("Снятие карантина превысит учётное количество короба.")
        try:
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.STOCK_RETURNED_TO_STORAGE,
                operation_type=WarehouseOperation.TYPE_RETURN_TO_STORAGE,
                zone_to=destination.zone_code,
            )
        except WarehouseTransitionError:
            try:
                transition = WarehouseTransitionService.apply_event(
                    snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                    WarehouseEventType.MOVEMENT_COMPLETED,
                    operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
                    zone_to=destination.zone_code,
                )
            except WarehouseTransitionError as exc:
                raise ValueError(
                    "Текущее складское состояние не допускает возврат найденного короба."
                ) from exc
        event = WarehouseEvent.objects.create(
            agency_id=operation.agency_id,
            event_type=WarehouseEventType.STOCK_RETURNED_TO_STORAGE.value,
            stock_context_type=MISSING_BOX_CONTEXT_TYPE,
            stock_context_id=str(operation.id),
            container=snapshot.container,
            operation=operation,
            operation_task=task,
            source_document_type=operation.source_document_type,
            source_document_id=operation.source_document_id,
            from_location=snapshot.location,
            to_location=destination,
            from_zone_code=snapshot.zone_code,
            to_zone_code=destination.zone_code,
            qty=int(snapshot.qty or 0),
            payload={
                "missing_box_resolved": True,
                "box_code": expected_box_code,
                "released_quarantine_qty": release_qty,
                "accounting_qty_unchanged": True,
            },
            performed_by=performed_by,
            performed_by_role=_actor_role(performed_by),
            occurred_at=now,
        )
        snapshot.parent_container = None
        snapshot.location = destination
        snapshot.zone_code = destination.zone_code
        snapshot.zone_kind = destination.zone_kind
        snapshot.warehouse_state_code = transition.code.value
        snapshot.available_qty = new_available
        snapshot.other_reserved_qty = int(snapshot.other_reserved_qty or 0) - release_qty
        snapshot.active_operation = None
        snapshot.active_operation_type = ""
        snapshot.last_event = event
        snapshot.snapshot_version = int(snapshot.snapshot_version or 0) + 1
        snapshot.save(
            update_fields=[
                "parent_container",
                "location",
                "zone_code",
                "zone_kind",
                "warehouse_state_code",
                "available_qty",
                "other_reserved_qty",
                "active_operation",
                "active_operation_type",
                "last_event",
                "snapshot_version",
                "updated_at",
            ]
        )

    WarehouseReserve.objects.filter(id__in=[reserve.id for reserve in reserves]).update(
        status=WarehouseReserve.STATUS_RELEASED,
        released_by=performed_by,
        updated_at=now,
    )
    WarehouseContainer.objects.filter(id=first.container_id).update(
        parent_container=None,
        current_location=destination,
        status=WarehouseContainer.STATUS_ACTIVE,
        updated_at=now,
    )
    task.to_location = destination
    task.to_zone_code = destination.zone_code
    task.qty_done = sum(int(row.qty or 0) for row in rows)
    task.status = WarehouseOperationTask.STATUS_DONE
    task.completed_at = now
    task.save(
        update_fields=[
            "to_location",
            "to_zone_code",
            "qty_done",
            "status",
            "completed_at",
            "updated_at",
        ]
    )
    operation.destination_location = destination
    operation.destination_zone_code = destination.zone_code
    operation.done_qty = sum(int(row.qty or 0) for row in rows)
    operation.status = WarehouseOperation.STATUS_DONE
    operation.completed_at = now
    operation.comment = (
        f"Короб {expected_box_code} найден и проверен; фактическое место {destination.location_code}."
    )
    operation.save(
        update_fields=[
            "destination_location",
            "destination_zone_code",
            "done_qty",
            "status",
            "completed_at",
            "comment",
            "updated_at",
        ]
    )
    from todo.models import Task

    Task.objects.filter(
        title="СРОЧНО: короб не найден на месте",
        description__icontains=f":{expected_box_code.casefold()}]",
    ).exclude(status="done").update(status="done", updated_at=now)
    return operation


def _snapshot_identity(snapshot: WarehouseStockSnapshot) -> tuple:
    expiry = getattr(snapshot, "expiry_date", None)
    return (
        int(snapshot.sku_ref_id or 0),
        _clean(snapshot.sku_code),
        _clean(snapshot.size),
        _clean(snapshot.barcode),
        _clean(snapshot.goods_type).casefold(),
        _clean(snapshot.marking_code),
        _clean(getattr(snapshot, "lot_code", "")),
        expiry.isoformat() if expiry else "",
        int(snapshot.qty or 0),
    )


def _box_signature(snapshots) -> tuple[tuple, ...]:
    return tuple(sorted(_snapshot_identity(snapshot) for snapshot in snapshots))


def _box_snapshots(container_id: int, *, lock: bool) -> list[WarehouseStockSnapshot]:
    rows = (
        WarehouseStockSnapshot.objects.filter(
            container_id=container_id,
            is_archived=False,
            qty__gt=0,
        )
        .select_related("container", "parent_container", "location", "active_operation")
        .order_by("id")
    )
    if lock:
        rows = rows.select_for_update(of=("self",))
    return list(rows)


def _candidate_is_free(snapshots: list[WarehouseStockSnapshot]) -> bool:
    return bool(snapshots) and all(
        not snapshot.is_in_vehicle
        and snapshot.active_operation_id is None
        and int(snapshot.available_qty or 0) == int(snapshot.qty or 0)
        and int(snapshot.processing_reserved_qty or 0) == 0
        and int(snapshot.shipping_reserved_qty or 0) == 0
        and int(snapshot.other_reserved_qty or 0) == 0
        for snapshot in snapshots
    )


def find_exact_free_box_replacement(
    *,
    agency_id: int,
    missing_container_id: int,
    parent_container_id: int | None = None,
    blocked_box_codes=(),
) -> ExactBoxReplacement | None:
    """Return a locked whole box with exactly the same stock rows and quantities."""
    missing_rows = _box_snapshots(missing_container_id, lock=True)
    if not missing_rows:
        return None
    expected_signature = _box_signature(missing_rows)
    claimed_codes = {
        _clean(code).casefold()
        for code in BoxClaim.objects.filter(
            agency_id=agency_id,
            status=BoxClaim.STATUS_CLAIMED,
        )
        .exclude(move_task__status__in=(
            MoveTask.STATUS_DONE,
            MoveTask.STATUS_CANCELED,
            MoveTask.STATUS_FAILED,
        ))
        .values_list("box_code", flat=True)
        if _clean(code)
    }
    claimed_codes.update(
        _clean(code).casefold()
        for code in blocked_box_codes or ()
        if _clean(code)
    )
    candidate_rows = (
        WarehouseContainer.objects.filter(
            agency_id=agency_id,
            container_type=WarehouseContainer.TYPE_BOX,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        .exclude(pk=missing_container_id)
        .exclude(parent_container=None)
        .exclude(current_location=None)
        .exclude(fbs_box__isnull=False)
        .order_by("created_at", "id")
    )
    if parent_container_id:
        candidate_rows = candidate_rows.filter(parent_container_id=parent_container_id)
    candidates = [
        (int(container_id), _clean(container_code).casefold())
        for container_id, container_code in candidate_rows.values_list("id", "container_code")
        if _clean(container_code).casefold() not in claimed_codes
    ]
    if not candidates:
        return None

    # Load all candidate stock rows in one query. The previous implementation
    # issued two locked queries per box and timed out on clients with 1000+
    # boxes. Only the matching candidate is locked and revalidated below.
    rows_by_container: dict[int, list[WarehouseStockSnapshot]] = {}
    candidate_ids = [container_id for container_id, _code in candidates]
    probe_rows = (
        WarehouseStockSnapshot.objects.filter(
            container_id__in=candidate_ids,
            is_archived=False,
            qty__gt=0,
        )
        .only(
            "id",
            "container_id",
            "sku_ref_id",
            "sku_code",
            "size",
            "barcode",
            "goods_type",
            "marking_code",
            "qty",
            "available_qty",
            "processing_reserved_qty",
            "shipping_reserved_qty",
            "other_reserved_qty",
            "active_operation_id",
            "is_in_vehicle",
        )
        .order_by("container_id", "id")
    )
    for row in probe_rows:
        rows_by_container.setdefault(int(row.container_id), []).append(row)

    for candidate_id, candidate_code in candidates:
        rows = rows_by_container.get(candidate_id, [])
        if not _candidate_is_free(rows) or _box_signature(rows) != expected_signature:
            continue
        container = (
            WarehouseContainer.objects.select_for_update(of=("self",))
            .select_related("parent_container", "current_location")
            .get(pk=candidate_id)
        )
        if candidate_code in claimed_codes or _clean(container.container_code).casefold() in claimed_codes:
            continue
        # State can change after the batched probe, so correctness still rests
        # on this locked read of the single candidate we are about to return.
        rows = _box_snapshots(container.id, lock=True)
        if not _candidate_is_free(rows) or _box_signature(rows) != expected_signature:
            continue
        return ExactBoxReplacement(
            container=container,
            pallet=container.parent_container,
            location=container.current_location,
            snapshots=tuple(rows),
        )
    return None


@transaction.atomic
def quarantine_missing_box(
    *,
    container_id: int,
    agency_id: int,
    context_type: str,
    context_id: str,
    performed_by=None,
    reserve_existing_blocked: bool = False,
) -> WarehouseOperation:
    """Block a reported-missing box without reducing its accounting quantity."""
    container = WarehouseContainer.objects.select_for_update().get(pk=container_id)
    if container.agency_id != agency_id:
        raise ValueError("Короб принадлежит другому клиенту.")
    rows = _box_snapshots(container.id, lock=True)
    if not rows:
        raise ValueError("В коробе нет активного складского остатка.")

    operation_context_id = f"{context_type}:{context_id}:{container.id}"[:64]
    existing = (
        WarehouseOperation.objects.select_for_update()
        .filter(
            agency_id=agency_id,
            context_type="missing_box_check",
            context_id=operation_context_id,
            status=WarehouseOperation.STATUS_BLOCKED,
        )
        .order_by("id")
        .first()
    )
    if existing is not None:
        if reserve_existing_blocked and existing.reserve_id is None:
            first_reserve = None
            for snapshot in rows:
                reserve = WarehouseReserve.objects.create(
                    agency_id=agency_id,
                    reserve_type=WarehouseReserve.TYPE_MANUAL,
                    context_type="missing_box_check",
                    context_id=str(existing.id),
                    sku_ref=snapshot.sku_ref,
                    sku_code=snapshot.sku_code,
                    size=snapshot.size,
                    barcode=snapshot.barcode,
                    goods_type=snapshot.goods_type,
                    marking_code=snapshot.marking_code,
                    qty_reserved=int(snapshot.qty or 0),
                    qty_allocated=int(snapshot.qty or 0),
                    status=WarehouseReserve.STATUS_ALLOCATED,
                    source_document_type=context_type[:32],
                    source_document_id=str(context_id)[:64],
                    # Карантин создаёт система. Водитель остаётся автором
                    # сообщения в операции и событии, но не автором резерва.
                    created_by=None,
                )
                first_reserve = first_reserve or reserve
            existing.reserve = first_reserve
            existing.save(update_fields=["reserve", "updated_at"])
        return existing

    operation = WarehouseOperation.objects.create(
        agency_id=agency_id,
        operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
        context_type="missing_box_check",
        context_id=operation_context_id,
        source_document_type=context_type[:32],
        source_document_id=str(context_id)[:64],
        source_location=container.current_location,
        source_zone_code=_clean(getattr(container.current_location, "zone_code", "")),
        status=WarehouseOperation.STATUS_BLOCKED,
        requested_by=performed_by if getattr(performed_by, "is_authenticated", False) else None,
        requested_by_role="reachtruck_driver",
        assigned_executor_role="head_manager",
        comment=f"Короб {container.container_code} не найден водителем; ожидает проверки.",
        planned_qty=sum(int(row.qty or 0) for row in rows),
    )
    first_reserve = None
    for snapshot in rows:
        qty = int(snapshot.qty or 0)
        already_blocked = int(snapshot.other_reserved_qty or 0)
        add_to_block = max(qty - already_blocked, 0)
        if add_to_block > int(snapshot.available_qty or 0):
            raise ValueError("Остаток короба уже занят другим процессом; автоматическая замена запрещена.")
        snapshot.available_qty = int(snapshot.available_qty or 0) - add_to_block
        snapshot.other_reserved_qty = already_blocked + add_to_block
        snapshot.active_operation = operation
        snapshot.active_operation_type = operation.operation_type
        snapshot.snapshot_version = int(snapshot.snapshot_version or 0) + 1
        reserve_qty = qty if reserve_existing_blocked else add_to_block
        reserve = None
        if reserve_qty > 0:
            reserve = WarehouseReserve.objects.create(
                agency_id=agency_id,
                reserve_type=WarehouseReserve.TYPE_MANUAL,
                context_type="missing_box_check",
                context_id=str(operation.id),
                sku_ref=snapshot.sku_ref,
                sku_code=snapshot.sku_code,
                size=snapshot.size,
                barcode=snapshot.barcode,
                goods_type=snapshot.goods_type,
                marking_code=snapshot.marking_code,
                qty_reserved=reserve_qty,
                qty_allocated=reserve_qty,
                status=WarehouseReserve.STATUS_ALLOCATED,
                source_document_type=context_type[:32],
                source_document_id=str(context_id)[:64],
                # Карантин создаёт система. Водитель остаётся автором
                # сообщения в операции и событии, но не автором резерва.
                created_by=None,
            )
            first_reserve = first_reserve or reserve
        event = WarehouseEvent.objects.create(
            agency_id=agency_id,
            event_type="missing_box_reported",
            stock_context_type="missing_box_check",
            stock_context_id=str(operation.id),
            container=container,
            operation=operation,
            reserve=reserve,
            source_document_type=context_type[:32],
            source_document_id=str(context_id)[:64],
            from_location=snapshot.location,
            from_zone_code=snapshot.zone_code,
            qty=qty,
            payload={
                "snapshot_id": snapshot.id,
                "box_code": container.container_code,
                "accounting_qty_unchanged": True,
            },
            performed_by=performed_by if getattr(performed_by, "is_authenticated", False) else None,
            performed_by_role="reachtruck_driver",
            occurred_at=timezone.now(),
        )
        snapshot.last_event = event
        snapshot.save(
            update_fields=[
                "available_qty",
                "other_reserved_qty",
                "active_operation",
                "active_operation_type",
                "snapshot_version",
                "last_event",
                "updated_at",
            ]
        )
    operation.reserve = first_reserve
    operation.save(update_fields=["reserve", "updated_at"])
    return operation
