from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from employees.models import Employee
from fbs.models import (
    FbsBox,
    FbsExternalIssueLine,
    FbsOrderStockAllocation,
    FbsPallet,
    FbsStockBalance,
)
from reachtruck.models import BoxClaim, MoveTask, PalletLock
from shipping.reservation_units import active_reserved_pallet_codes_for_agency
from sklad.location_occupancy import active_os_physical_containers, os_location_occupancy_message
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseOperation,
    WarehouseOperationTask,
    WarehouseStockSnapshot,
)
from sklad.services.warehouse_write_path import WarehouseWritePathService
from todo.models import Task

from .services import _location_from_dict, clean_code, parse_location_scan


PHYSICAL_EMPTY_LOCATION_ROLES = {
    "reachtruck_driver",
    "super_car",
    "head_manager",
    "admin",
}
OPEN_OPERATION_STATUSES = {
    WarehouseOperation.STATUS_CREATED,
    WarehouseOperation.STATUS_PLANNED,
    WarehouseOperation.STATUS_IN_PROGRESS,
    WarehouseOperation.STATUS_PARTIAL,
    WarehouseOperation.STATUS_BLOCKED,
}
OPEN_OPERATION_TASK_STATUSES = {
    WarehouseOperationTask.STATUS_CREATED,
    WarehouseOperationTask.STATUS_IN_PROGRESS,
}
OPEN_MOVE_TASK_STATUSES = {
    MoveTask.STATUS_CREATED,
    MoveTask.STATUS_IN_PROGRESS,
}
FINAL_MOVE_TASK_STATUSES = {
    MoveTask.STATUS_DONE,
    MoveTask.STATUS_CANCELED,
    MoveTask.STATUS_FAILED,
}
ACTIVE_FBS_ALLOCATION_STATUSES = {
    FbsOrderStockAllocation.STATUS_RESERVED,
    FbsOrderStockAllocation.STATUS_PICKING,
}
ACTIVE_FBS_BOX_STATUSES = {FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE}
ACTIVE_FBS_PALLET_STATUSES = {FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE}


@dataclass(frozen=True)
class PhysicalEmptyLocationResult:
    released: bool
    already_free: bool = False
    message: str = ""
    task_id: int | None = None
    container_codes: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


def _actor(user):
    return user if getattr(user, "is_authenticated", False) else None


def _location_label(location) -> str:
    return clean_code(location.display_name) or clean_code(location.location_code) or f"место #{location.pk}"


def _deduplicate(values) -> list[str]:
    return list(dict.fromkeys(clean_code(value) for value in values if clean_code(value)))


def _location_move_tasks(location):
    source = Q(
        from_zone__iexact="OS",
        from_row=location.row_no,
        from_section=location.section_no,
        from_tier=location.tier_no,
        from_cell=location.cell_no,
    )
    destination = Q(
        to_zone__iexact="OS",
        to_row=location.row_no,
        to_section=location.section_no,
        to_tier=location.tier_no,
        to_cell=location.cell_no,
    )
    return MoveTask.objects.select_for_update().filter(
        source | destination,
        status__in=OPEN_MOVE_TASK_STATUSES,
    )


def _lock_container_tree(location) -> list[WarehouseContainer]:
    initial = list(
        active_os_physical_containers()
        .select_related(None)
        .select_for_update()
        .filter(current_location=location)
        .order_by("id")
    )
    by_id = {row.id: row for row in initial}
    frontier = set(by_id)
    while frontier:
        children = list(
            WarehouseContainer.objects.select_for_update()
            .filter(parent_container_id__in=frontier, status=WarehouseContainer.STATUS_ACTIVE)
            .exclude(id__in=by_id)
            .order_by("id")
        )
        frontier = {row.id for row in children}
        by_id.update({row.id: row for row in children})
    return list(by_id.values())


def _create_verification_task(*, location, container_codes: list[str], blockers: list[str], user):
    marker = f"[physical-empty-location:{location.pk}]"
    existing = (
        Task.objects.select_for_update()
        .filter(
            title="СРОЧНО: физически пустое место занято в системе",
            description__contains=marker,
        )
        .exclude(status="done")
        .order_by("-created_at", "-id")
        .first()
    )
    if existing is not None:
        return existing, False

    head_manager = (
        Employee.objects.select_for_update()
        .filter(role="head_manager", is_active=True)
        .order_by("full_name", "id")
        .first()
    )
    if head_manager is None:
        return None, False

    location_label = _location_label(location)
    codes = ", ".join(container_codes[:30]) or "не определены"
    reasons = "\n".join(f"- {reason}" for reason in blockers[:30])
    task = Task.objects.create(
        title="СРОЧНО: физически пустое место занято в системе",
        description=(
            f"Ричтракер подтвердил, что место {location_label} физически пустое, "
            "но автоматическое освобождение заблокировано.\n\n"
            f"Контейнеры: {codes}\n"
            f"Причины:\n{reasons}\n\n"
            "Проверьте движение тары и остатки. Не обнуляйте товар без инвентаризации.\n\n"
            f"{marker}"
        ),
        route="/reachtruck-free/",
        assigned_to=head_manager,
        created_by=_actor(user),
        status="in_progress",
        priority="urgent",
        due_date=timezone.localtime(),
    )
    return task, True


def _write_check_events(
    *,
    event_type: str,
    location,
    containers: list[WarehouseContainer],
    blockers: list[str],
    task_id: int | None,
    user,
    role: str,
) -> None:
    occurred_at = timezone.now()
    container_codes = _deduplicate(row.container_code for row in containers)
    first_by_agency = {}
    for container in containers:
        first_by_agency.setdefault(container.agency_id, container)
    for agency_id, container in first_by_agency.items():
        WarehouseEvent.objects.create(
            agency_id=agency_id,
            event_type=event_type,
            stock_context_type="physical_empty_location_check",
            stock_context_id=str(location.pk),
            container=container,
            from_location=location,
            from_zone_code=clean_code(location.zone_code),
            qty=0,
            payload={
                "location_id": location.pk,
                "location_code": _location_label(location),
                "container_codes": container_codes[:50],
                "blockers": blockers[:50],
                "verification_task_id": task_id,
                "physical_empty_confirmed": True,
                "stock_quantity_changed": False,
                "reserve_changed": False,
            },
            performed_by=_actor(user),
            performed_by_role=clean_code(role),
            occurred_at=occurred_at,
        )


def _risk_assessment(*, location, containers: list[WarehouseContainer]):
    blockers: list[str] = []
    container_ids = {row.id for row in containers}
    container_codes = _deduplicate(row.container_code for row in containers)
    pallet_codes = [
        row.container_code
        for row in containers
        if row.container_type in {WarehouseContainer.TYPE_PALLET, WarehouseContainer.TYPE_MIXED_PALLET}
    ]
    box_codes = [
        row.container_code
        for row in containers
        if row.container_type == WarehouseContainer.TYPE_BOX
    ]

    remote_children = [
        row for row in containers
        if row.parent_container_id in container_ids
        and row.current_location_id
        and row.current_location_id != location.id
    ]
    if remote_children:
        blockers.append(
            "Дочерние короба числятся в других местах, но всё ещё привязаны к паллете этого адреса: "
            + ", ".join(row.container_code for row in remote_children[:5])
        )

    foreign_parent_boxes = [
        row for row in containers
        if row.current_location_id == location.id
        and row.parent_container_id
        and row.parent_container_id not in container_ids
    ]
    if foreign_parent_boxes:
        blockers.append("Есть короба, привязанные к паллете другого места.")

    snapshot_scope = WarehouseStockSnapshot.objects.select_for_update().filter(
        Q(location=location)
        | Q(container_id__in=container_ids)
        | Q(parent_container_id__in=container_ids)
    )
    live_snapshots = list(snapshot_scope.filter(is_archived=False).order_by("id"))
    total_qty = sum(int(row.qty or 0) for row in live_snapshots)
    available_qty = sum(int(row.available_qty or 0) for row in live_snapshots)
    processing_reserved = sum(int(row.processing_reserved_qty or 0) for row in live_snapshots)
    shipping_reserved = sum(int(row.shipping_reserved_qty or 0) for row in live_snapshots)
    other_reserved = sum(int(row.other_reserved_qty or 0) for row in live_snapshots)
    if total_qty or available_qty:
        blockers.append(
            f"На общем складе числится товар: {total_qty} шт., доступно {available_qty} шт."
        )
    if processing_reserved:
        blockers.append(f"Есть резерв под обработку: {processing_reserved} шт.")
    if shipping_reserved:
        blockers.append(f"Есть резерв под отгрузку: {shipping_reserved} шт.")
    if other_reserved:
        blockers.append(f"Есть другой складской резерв: {other_reserved} шт.")

    active_snapshot_operations = {
        row.active_operation_id
        for row in live_snapshots
        if row.active_operation_id
        and row.active_operation
        and row.active_operation.status in OPEN_OPERATION_STATUSES
    }
    if active_snapshot_operations:
        blockers.append(
            "Остаток участвует в активных складских операциях: "
            + ", ".join(f"#{value}" for value in sorted(active_snapshot_operations)[:5])
        )

    operations = list(
        WarehouseOperation.objects.select_for_update()
        .filter(Q(source_location=location) | Q(destination_location=location))
        .filter(status__in=OPEN_OPERATION_STATUSES)
        .order_by("id")
    )
    if operations:
        blockers.append(
            "Адрес участвует в незавершённых операциях: "
            + ", ".join(f"#{row.id}" for row in operations[:5])
        )

    operation_tasks = list(
        WarehouseOperationTask.objects.select_for_update()
        .filter(
            Q(from_location=location)
            | Q(to_location=location)
            | Q(container_id__in=container_ids)
        )
        .filter(status__in=OPEN_OPERATION_TASK_STATUSES)
        .order_by("id")
    )
    if operation_tasks:
        blockers.append(
            "Есть незавершённые складские задания: "
            + ", ".join(f"#{row.id}" for row in operation_tasks[:5])
        )

    move_tasks = list(_location_move_tasks(location).order_by("id"))
    if move_tasks:
        blockers.append(
            "Есть активные задания ричтракера: "
            + ", ".join(clean_code(row.legacy_order_id) or f"#{row.id}" for row in move_tasks[:5])
        )

    pallet_locks = list(
        PalletLock.objects.select_for_update()
        .filter(pallet_code__in=pallet_codes, status=PalletLock.STATUS_ACTIVE)
        .exclude(move_task__status__in=FINAL_MOVE_TASK_STATUSES)
        .order_by("id")
    )
    if pallet_locks:
        blockers.append("Паллета взята в работу ричтракером.")

    box_claims = list(
        BoxClaim.objects.select_for_update()
        .filter(box_code__in=box_codes, status=BoxClaim.STATUS_CLAIMED)
        .exclude(move_task__status__in=FINAL_MOVE_TASK_STATUSES)
        .order_by("id")
    )
    if box_claims:
        blockers.append("Короб взят в работу ричтракером.")

    for agency_id in sorted({row.agency_id for row in containers if row.agency_id}):
        reserved_pallets = active_reserved_pallet_codes_for_agency(agency_id, pallet_codes)
        if reserved_pallets:
            blockers.append(
                "Есть резерв по паллете под отгрузку: " + ", ".join(sorted(reserved_pallets)[:5])
            )

    fbs_pallet_ids = set(
        FbsPallet.objects.filter(
            Q(warehouse_container_id__in=container_ids)
            | Q(boxes__source_container_id__in=container_ids)
            | Q(boxes__source_container__parent_container_id__in=container_ids)
        ).values_list("id", flat=True)
    )
    fbs_pallets = list(
        FbsPallet.objects.select_for_update().filter(id__in=fbs_pallet_ids).order_by("id")
    )
    fbs_box_ids = set(
        FbsBox.objects.filter(
            Q(source_container_id__in=container_ids)
            | Q(source_container__parent_container_id__in=container_ids)
            | Q(pallet_id__in=fbs_pallet_ids)
        ).values_list("id", flat=True)
    )
    fbs_boxes = list(
        FbsBox.objects.select_for_update().filter(id__in=fbs_box_ids).order_by("id")
    )
    balances = list(
        FbsStockBalance.objects.select_for_update().filter(box_id__in=fbs_box_ids).order_by("id")
    )
    fbs_qty = sum(int(row.qty or 0) for row in balances)
    fbs_available = sum(int(row.available_qty or 0) for row in balances)
    fbs_reserved = sum(
        int(row.reserved_qty or 0) + int(row.external_reserved_qty or 0)
        for row in balances
    )
    if fbs_qty or fbs_available or fbs_reserved:
        blockers.append(
            f"В контуре FBS числится товар: {fbs_qty} шт., доступно {fbs_available} шт., "
            f"в резерве {fbs_reserved} шт."
        )
    if FbsOrderStockAllocation.objects.select_for_update().filter(
        balance__box_id__in=fbs_box_ids,
        status__in=ACTIVE_FBS_ALLOCATION_STATUSES,
    ).exists():
        blockers.append("Есть активный резерв FBS-заказа или начатый подбор.")
    if FbsExternalIssueLine.objects.select_for_update().filter(
        balance__box_id__in=fbs_box_ids,
        issue__status__in=("reserved", "picking"),
    ).exists():
        blockers.append("Есть активный внешний FBS-резерв.")
    if any(row.is_rack_binding for row in fbs_pallets if row.status in ACTIVE_FBS_PALLET_STATUSES):
        blockers.append("Адрес связан с системной паллетой FBS-стеллажа.")

    fbs_linked_container_ids = {
        row.source_container_id for row in fbs_boxes if row.source_container_id
    } | {
        row.warehouse_container_id for row in fbs_pallets if row.warehouse_container_id
    }
    broken_fbs_links = [
        row.container_code
        for row in containers
        if clean_code(row.source_context_type).lower().startswith("fbs_")
        and row.id not in fbs_linked_container_ids
    ]
    if broken_fbs_links:
        blockers.append(
            "FBS-контейнеры не имеют полной учётной связи: "
            + ", ".join(broken_fbs_links[:5])
        )

    agency_ids = {row.agency_id for row in containers if row.agency_id}
    if len(agency_ids) > 1:
        blockers.append("В одном адресе числятся контейнеры нескольких клиентов.")
    general_container_ids = container_ids - fbs_linked_container_ids
    if fbs_linked_container_ids and general_container_ids:
        blockers.append("В одном адресе смешаны контуры общего склада и FBS.")

    return {
        "blockers": _deduplicate(blockers),
        "container_codes": container_codes,
        "fbs_boxes": fbs_boxes,
        "fbs_pallets": fbs_pallets,
        "fbs_linked_container_ids": fbs_linked_container_ids,
    }


def _release_verified_empty_containers(*, location, containers, assessment, user, role):
    from fbs.services.picking import (
        _archive_empty_fbs_box_after_pick,
        _release_empty_warehouse_container,
    )

    now = timezone.now()
    for fbs_box in assessment["fbs_boxes"]:
        if fbs_box.status not in ACTIVE_FBS_BOX_STATUSES and not fbs_box.source_container_id:
            continue
        if not _archive_empty_fbs_box_after_pick(
            box_id=fbs_box.id,
            performed_by=user,
            occurred_at=now,
        ):
            raise ValueError(
                f"FBS-короб {fbs_box.box_code} нельзя освободить автоматически. Создана проверка."
            )

    for fbs_pallet in assessment["fbs_pallets"]:
        fbs_pallet.refresh_from_db()
        if fbs_pallet.status not in ACTIVE_FBS_PALLET_STATUSES:
            continue
        if fbs_pallet.is_rack_binding:
            raise ValueError("Системную паллету FBS-стеллажа нельзя освободить этим действием.")
        if fbs_pallet.boxes.filter(status__in=ACTIVE_FBS_BOX_STATUSES).exists():
            raise ValueError(f"На FBS-паллете {fbs_pallet.pallet_code} остались активные короба.")
        fbs_pallet.status = FbsPallet.STATUS_ARCHIVED
        fbs_pallet.save(update_fields=["status", "updated_at"])
        if fbs_pallet.warehouse_container_id:
            physical = (
                WarehouseContainer.objects.select_for_update()
                .get(pk=fbs_pallet.warehouse_container_id)
            )
            if not _release_empty_warehouse_container(
                container=physical,
                stock_context_id=str(location.pk),
                performed_by=_actor(user),
                occurred_at=now,
                payload={
                    "reason": "physical_empty_location_confirmed",
                    "location_id": location.pk,
                    "location_code": _location_label(location),
                },
            ):
                raise ValueError(f"Паллета {fbs_pallet.pallet_code} осталась связана с учётом FBS.")

    fbs_linked_ids = assessment["fbs_linked_container_ids"]
    general_boxes_by_agency = {}
    general_pallet_ids = []
    for container in containers:
        if container.id in fbs_linked_ids:
            continue
        if container.container_type == WarehouseContainer.TYPE_BOX:
            general_boxes_by_agency.setdefault(container.agency_id, []).append(container.container_code)
        elif container.container_type in {
            WarehouseContainer.TYPE_PALLET,
            WarehouseContainer.TYPE_MIXED_PALLET,
        }:
            general_pallet_ids.append(container.id)

    for agency_id, box_codes in general_boxes_by_agency.items():
        agency = next(row.agency for row in containers if row.agency_id == agency_id)
        WarehouseWritePathService._archive_empty_shipping_source_containers(
            agency=agency,
            box_codes=box_codes,
        )
    WarehouseWritePathService._archive_empty_source_pallets(general_pallet_ids)

    remaining = list(
        active_os_physical_containers()
        .select_for_update()
        .filter(current_location=location)
        .values_list("container_code", flat=True)
    )
    if remaining or os_location_occupancy_message(location):
        raise ValueError(
            "После проверки у адреса остались складские связи: "
            + ", ".join(remaining[:5])
        )

    _write_check_events(
        event_type="physical_empty_location_released",
        location=location,
        containers=containers,
        blockers=[],
        task_id=None,
        user=user,
        role=role,
    )


@transaction.atomic
def confirm_physical_empty_location(*, scan_value: str, user, role: str) -> PhysicalEmptyLocationResult:
    normalized_role = clean_code(role).lower()
    if normalized_role not in PHYSICAL_EMPTY_LOCATION_ROLES:
        raise ValueError("Освобождать физически пустое место может только водитель ричтрака или начальник склада.")

    location_dict, _error = parse_location_scan(scan_value)
    location = _location_from_dict(location_dict) if location_dict else None
    if location is None:
        raise ValueError("Место не распознано. Повторно отсканируйте QR адреса.")
    location = type(location).objects.select_for_update().get(pk=location.pk)
    if clean_code(location.zone_code).upper() != "OS":
        raise ValueError("Этим действием можно проверять только адреса основного хранения OS.")
    if not location.is_active:
        raise ValueError("Адрес отключён в топологии склада.")

    containers = _lock_container_tree(location)
    if not containers and not os_location_occupancy_message(location):
        return PhysicalEmptyLocationResult(
            released=False,
            already_free=True,
            message=f"Место {_location_label(location)} уже свободно.",
        )

    assessment = _risk_assessment(location=location, containers=containers)
    blockers = assessment["blockers"]
    if blockers:
        task, _created = _create_verification_task(
            location=location,
            container_codes=assessment["container_codes"],
            blockers=blockers,
            user=user,
        )
        _write_check_events(
            event_type="physical_empty_location_blocked",
            location=location,
            containers=containers,
            blockers=blockers,
            task_id=task.id if task else None,
            user=user,
            role=normalized_role,
        )
        task_note = f" Создана задача #{task.id} начальнику склада." if task else ""
        return PhysicalEmptyLocationResult(
            released=False,
            message="Место не освобождено: " + " ".join(blockers) + task_note,
            task_id=task.id if task else None,
            container_codes=tuple(assessment["container_codes"]),
            blockers=tuple(blockers),
        )

    _release_verified_empty_containers(
        location=location,
        containers=containers,
        assessment=assessment,
        user=user,
        role=normalized_role,
    )
    return PhysicalEmptyLocationResult(
        released=True,
        message=f"Место {_location_label(location)} освобождено. Остатки и резервы не изменялись.",
        container_codes=tuple(assessment["container_codes"]),
    )
