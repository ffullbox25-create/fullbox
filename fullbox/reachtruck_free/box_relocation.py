from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from fbs.exceptions import FbsMovementError
from fbs.models import (
    FbsBox,
    FbsInternalMovement,
    FbsInventorySession,
    FbsPallet,
    FbsRack,
    FbsRackStagingBox,
    FbsReplenishmentPlan,
    FbsStockBalance,
    FbsStorageLock,
)
from reachtruck.models import BoxClaim, MoveTask, PalletLock
from reachtruck.services.task_commands import _same_box_code_scan, _same_pallet_code_scan
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseOperationTask,
    WarehouseStockSnapshot,
)
from sklad.services.warehouse_events import WarehouseEventType
from sklad.services.warehouse_transitions import (
    WarehouseStateCode,
    WarehouseTransitionError,
    WarehouseTransitionService,
)
from sklad.services.operational_locations import (
    normalize_operational_location_scan,
    require_concrete_movement_location,
    validate_operational_location,
)
from sklad.location_occupancy import os_location_occupancy_message
from sklad.topology import os_location_code
from shipping.reservation_units import active_reserved_pallet_codes_for_agency
from fbs.services.relocation_reservations import relocation_reservation_state


GENERAL_FREE_BOX_CONTEXT = "reachtruck_free_box"
FBS_FREE_BOX_CONTEXT = "fbs_free_box"
ACTIVE_FBS_STATUSES = (FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE)
ACTIVE_FBS_PALLET_STATUSES = (FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE)
ACTIVE_TASK_STATUSES = (
    WarehouseOperationTask.STATUS_CREATED,
    WarehouseOperationTask.STATUS_IN_PROGRESS,
)
FINAL_MOVE_TASK_STATUSES = (
    MoveTask.STATUS_DONE,
    MoveTask.STATUS_CANCELED,
    MoveTask.STATUS_FAILED,
)


def _text(value) -> str:
    return str(value or "").strip()


def _actor(user):
    return user if getattr(user, "is_authenticated", False) else None


def _actor_name(user) -> str:
    if user is None:
        return ""
    return _text(user.get_full_name() or user.get_username())


def _fbs_physical_location(pallet: FbsPallet):
    pallet_container = getattr(pallet, "warehouse_container", None)
    return (
        getattr(pallet_container, "current_location", None)
        or getattr(getattr(pallet, "cell", None), "location", None)
    )


def _fbs_box_location(box: FbsBox):
    return (
        getattr(getattr(box, "source_container", None), "current_location", None)
        or _fbs_physical_location(box.pallet)
    )


def _resolve_direct_box_destination(scan_value: str):
    """Resolve an exact PR/OTG code or an OS label such as ``A-10/1-3``."""
    normalized = normalize_operational_location_scan(scan_value)
    if not normalized:
        return None
    queryset = WarehouseLocation.objects.select_for_update().filter(
        warehouse_code="MSK",
    )
    destination = queryset.filter(location_code__iexact=normalized).first()
    if destination is not None:
        return destination

    # OS labels printed on the racks are display scan values, while legacy rows
    # can still keep OS-<row>-<section>-<tier>-<cell> in location_code.
    from reachtruck_free.services import parse_location_scan

    parsed, _error = parse_location_scan(normalized)
    if not parsed or _text(parsed.get("zone")).upper() != "OS":
        return None
    return queryset.filter(
        zone_code__iexact="OS",
        row_no=int(parsed.get("row") or 0),
        section_no=int(parsed.get("section") or 0),
        tier_no=int(parsed.get("tier") or 0),
        cell_no=int(parsed.get("cell") or 0),
    ).first()


def _os_destination_fbs_pallet(*, destination, box: FbsBox):
    """Return the single physical FBS pallet occupying an OS location."""
    pallets = list(
        FbsPallet.objects.select_for_update(of=("self",))
        .select_related("warehouse_container")
        .filter(
            warehouse_container__current_location=destination,
            warehouse_container__status=WarehouseContainer.STATUS_ACTIVE,
            status__in=ACTIVE_FBS_PALLET_STATUSES,
        )
        .order_by("id")[:2]
    )
    if len(pallets) > 1:
        raise FbsMovementError(
            "В выбранной ячейке числится несколько FBS-паллет. Требуется проверка места."
        )
    if not pallets:
        return None
    pallet = pallets[0]
    if int(pallet.agency_id) != int(box.agency_id):
        raise FbsMovementError(
            "В выбранной ячейке находится FBS-паллета другого клиента. "
            "Смешивать клиентов запрещено."
        )
    return pallet


def _os_destination_loose_fbs_box_containers(*, destination, box: FbsBox):
    """Return compatible FBS boxes placed directly in one OS location.

    A full box can be stored in OS without a physical pallet.  Such a box must
    not make the whole address unavailable to other loose boxes of the same
    client, while mixed clients and inconsistent parent links stay blocked.
    """
    containers = list(
        WarehouseContainer.objects.select_for_update(of=("self",))
        .select_related("fbs_box")
        .filter(
            current_location=destination,
            status=WarehouseContainer.STATUS_ACTIVE,
            fbs_box__status__in=ACTIVE_FBS_STATUSES,
        )
        .order_by("id")
    )
    if not containers:
        return []
    if any(container.parent_container_id for container in containers):
        return []
    if any(int(container.agency_id) != int(box.agency_id) for container in containers):
        raise FbsMovementError(
            "В выбранной ячейке находится FBS-короб другого клиента. "
            "Смешивать клиентов запрещено."
        )
    return containers


def _release_empty_source_fbs_pallet_after_box_move(
    *,
    source_logical_pallet: FbsPallet | None,
    source_physical_pallet: WarehouseContainer | None,
    source_location: WarehouseLocation | None,
    performed_by,
    occurred_at,
) -> bool:
    """Release an emptied source pallet after its last physical box leaves.

    A box placed directly on a PR rack remains an active ``FbsBox`` and keeps
    its historical logical-pallet FK.  Physical occupancy, however, is owned
    by ``WarehouseContainer``.  Therefore active boxes that have already left
    the source physical pallet must not keep that empty OS pallet and address
    occupied forever.
    """
    if source_logical_pallet is None or source_physical_pallet is None:
        return False
    if int(source_logical_pallet.warehouse_container_id or 0) != int(
        source_physical_pallet.id
    ):
        return False

    logical = FbsPallet.objects.select_for_update(of=("self",)).get(
        pk=source_logical_pallet.pk
    )
    physical = (
        WarehouseContainer.objects.select_for_update(of=("self",))
        .select_related("current_location")
        .get(pk=source_physical_pallet.pk)
    )
    if logical.status not in ACTIVE_FBS_PALLET_STATUSES:
        return physical.status == WarehouseContainer.STATUS_ARCHIVED
    if physical.status != WarehouseContainer.STATUS_ACTIVE:
        return False
    if source_location is not None and int(physical.current_location_id or 0) != int(
        source_location.id
    ):
        return False
    if physical.child_containers.filter(
        status=WarehouseContainer.STATUS_ACTIVE
    ).exists():
        return False

    from fbs.services.picking import _release_empty_warehouse_container

    released = _release_empty_warehouse_container(
        container=physical,
        stock_context_id=str(logical.id),
        performed_by=_actor(performed_by),
        occurred_at=occurred_at,
        payload={
            "reason": "last_fbs_box_left_source_pallet",
            "fbs_pallet_id": int(logical.id),
            "fbs_pallet_code": logical.pallet_code,
            "source_location_id": int(getattr(source_location, "id", 0) or 0),
        },
    )
    if not released:
        return False
    logical.status = FbsPallet.STATUS_ARCHIVED
    logical.save(update_fields=["status", "updated_at"])
    return True


def _location_code(location) -> str:
    if location is None:
        return ""
    zone = _text(location.zone_code).upper()
    if zone == "OS" and all(
        int(value or 0) > 0
        for value in (location.row_no, location.section_no, location.tier_no, location.cell_no)
    ):
        return os_location_code(
            row=location.row_no,
            section=location.section_no,
            tier=location.tier_no,
            cell=location.cell_no,
        )
    return _text(location.location_code) or zone


def _location_label(location) -> str:
    return _text(getattr(location, "display_name", "")) or _location_code(location)


def _target_pallet_blocker(*, agency_id: int, pallet_code: str) -> str:
    code = _text(pallet_code)
    if not code:
        return "У паллеты назначения отсутствует код."
    if PalletLock.objects.filter(
        agency_id=int(agency_id),
        pallet_code__iexact=code,
        status=PalletLock.STATUS_ACTIVE,
    ).exclude(move_task__status__in=FINAL_MOVE_TASK_STATUSES).exists():
        return "Паллета назначения уже взята в задание ричтрака."
    if MoveTask.objects.filter(
        request__agency_id=int(agency_id),
        status__in=(MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS),
    ).filter(
        Q(pallet_code__iexact=code) | Q(payload__pallet_code=code)
    ).exists():
        return "Для паллеты назначения уже существует активное задание ричтрака."
    if active_reserved_pallet_codes_for_agency(int(agency_id), [code]):
        return "Паллета назначения зарезервирована под отгрузку."
    return ""


def _resolve_fbs_box(scan_value: str) -> tuple[list[FbsBox], bool]:
    normalized = _text(scan_value)
    if not normalized:
        return [], False
    queryset = FbsBox.objects.filter(status__in=ACTIVE_FBS_STATUSES).select_related(
        "agency",
        "pallet__cell__location",
        "pallet__warehouse_container__current_location",
        "source_container__current_location",
        "source_container__parent_container__current_location",
    )
    direct = list(queryset.filter(box_code__iexact=normalized).order_by("id")[:3])
    if direct:
        return direct, len(direct) > 1
    matches: list[FbsBox] = []
    for box in queryset.order_by("id")[:5000]:
        if _same_box_code_scan(normalized, box.box_code):
            matches.append(box)
            if len(matches) > 1:
                break
    return matches, len(matches) > 1


def _resolve_fbs_pallet(scan_value: str, *, agency_id: int) -> tuple[FbsPallet | None, bool]:
    normalized = _text(scan_value)
    queryset = FbsPallet.objects.filter(
        agency_id=int(agency_id),
        status__in=ACTIVE_FBS_PALLET_STATUSES,
    ).select_related(
        "agency",
        "cell__location",
        "warehouse_container__current_location",
    )
    direct = list(queryset.filter(pallet_code__iexact=normalized).order_by("id")[:2])
    if direct:
        return direct[0], len(direct) > 1
    matches: list[FbsPallet] = []
    for pallet in queryset.order_by("id")[:5000]:
        if _same_pallet_code_scan(normalized, pallet.pallet_code):
            matches.append(pallet)
            if len(matches) > 1:
                break
    return (matches[0] if matches else None), len(matches) > 1


def _fbs_box_balances(box: FbsBox):
    return FbsStockBalance.objects.filter(box=box, qty__gt=0).select_related("sku_ref")


def _fbs_box_blockers(
    box: FbsBox,
    *,
    exclude_operation_id: int | None = None,
    lock_reservations: bool = False,
) -> list[str]:
    blockers: list[str] = []
    pallet = box.pallet
    location = _fbs_box_location(box)
    source_container = box.source_container
    physical_parent = (
        source_container.parent_container
        if source_container is not None and source_container.parent_container_id
        else None
    )
    physical_parent_location = getattr(physical_parent, "current_location", None)
    zone = _text(getattr(location, "zone_code", "")).upper()
    if location is None:
        blockers.append("У FBS-короба не найдено физическое место.")
    elif not location.is_active:
        blockers.append("Текущее место FBS-короба выключено.")
    elif zone not in {"OS", "PR", "OTG"}:
        blockers.append("Свободное перемещение FBS-короба разрешено только из OS, PR или OTG.")
    if source_container is None:
        blockers.append("У FBS-короба отсутствует физический складской контейнер.")
    elif physical_parent is not None and (
        physical_parent_location is None
        or int(source_container.current_location_id or 0)
        != int(physical_parent_location.id or 0)
    ):
        blockers.append(
            "Физическое место FBS-короба не совпадает с местом физической паллеты."
        )

    balances = list(_fbs_box_balances(box).order_by("id"))
    if sum(int(balance.qty or 0) for balance in balances) <= 0:
        blockers.append("В FBS-коробе нет товара для перемещения.")
    reservation_state = relocation_reservation_state(
        [balance.id for balance in balances],
        lock=lock_reservations,
    )
    blockers.extend(reservation_state.blockers)

    sku_ids = [int(balance.sku_ref_id) for balance in balances if balance.sku_ref_id]
    lock_query = (
        Q(scope_type=FbsInventorySession.SCOPE_ALL)
        | Q(scope_type=FbsInventorySession.SCOPE_AGENCY, agency_id=box.agency_id)
        | Q(scope_type=FbsInventorySession.SCOPE_CELL, cell_id=pallet.cell_id)
        | Q(scope_type=FbsInventorySession.SCOPE_PALLET, pallet_id=pallet.id)
        | Q(scope_type=FbsInventorySession.SCOPE_BOX, box_id=box.id)
    )
    if sku_ids:
        lock_query |= Q(
            scope_type=FbsInventorySession.SCOPE_SKU,
            agency_id=box.agency_id,
            sku_id__in=sku_ids,
        )
    if FbsStorageLock.objects.filter(is_active=True).filter(lock_query).exists():
        blockers.append("Инвентаризация блокирует этот FBS-короб.")

    if FbsInternalMovement.objects.filter(
        Q(source_box=box) | Q(target_box=box),
        status__in=(
            FbsInternalMovement.STATUS_PROPOSED,
            FbsInternalMovement.STATUS_IN_PROGRESS,
            FbsInternalMovement.STATUS_BLOCKED,
        ),
    ).exists():
        blockers.append("У FBS-короба уже есть внутреннее перемещение.")
    if FbsReplenishmentPlan.objects.filter(
        target_box=box,
        status__in=(
            FbsReplenishmentPlan.STATUS_PROPOSED,
            FbsReplenishmentPlan.STATUS_CONFIRMED,
            FbsReplenishmentPlan.STATUS_IN_PROGRESS,
            FbsReplenishmentPlan.STATUS_AWAITING_PACK,
            FbsReplenishmentPlan.STATUS_BLOCKED,
        ),
    ).exists():
        blockers.append("FBS-короб участвует в действующем плане пополнения.")

    if BoxClaim.objects.filter(
        agency_id=box.agency_id,
        box_code__iexact=box.box_code,
        status=BoxClaim.STATUS_CLAIMED,
    ).exclude(move_task__status__in=FINAL_MOVE_TASK_STATUSES).exists():
        blockers.append("FBS-короб уже взят в задание ричтрака.")

    if box.source_container_id:
        task_query = WarehouseOperationTask.objects.filter(
            container_id=box.source_container_id,
            status__in=ACTIVE_TASK_STATUSES,
        )
        if exclude_operation_id:
            task_query = task_query.exclude(operation_id=int(exclude_operation_id))
        if task_query.exists():
            blockers.append("У FBS-короба есть активная складская операция.")
    pallet_operation = WarehouseOperation.objects.filter(
        operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
        context_type="fbs_free_relocation",
        context_id=str(pallet.id),
        agency_id=box.agency_id,
        status=WarehouseOperation.STATUS_IN_PROGRESS,
    )
    if exclude_operation_id:
        pallet_operation = pallet_operation.exclude(pk=int(exclude_operation_id))
    if pallet_operation.exists():
        blockers.append("Паллета FBS уже находится в свободном перемещении.")

    linked_snapshot = Q(container_id=box.source_container_id) if box.source_container_id else Q(pk__in=[])
    linked_snapshot |= Q(container_code__iexact=box.box_code)
    if WarehouseStockSnapshot.objects.filter(
        linked_snapshot,
        is_archived=False,
        qty__gt=0,
    ).exists():
        blockers.append(
            "FBS-короб ещё связан с остатком общего склада. Сначала завершите исходное перемещение."
        )
    return list(dict.fromkeys(blockers))


def _fbs_box_lines(box: FbsBox) -> list[dict]:
    rows: list[dict] = []
    for balance in _fbs_box_balances(box).order_by("id"):
        rows.append(
            {
                "sku": _text(balance.sku_code) or "-",
                "name": _text(balance.name),
                "size": _text(balance.size),
                "barcode": _text(balance.barcode),
                "goods_type": _text(balance.goods_type),
                "qty": int(balance.qty or 0),
                "boxes": [box.box_code],
            }
        )
    return rows


def inspect_fbs_box(scan_value: str) -> dict:
    normalized = _text(scan_value)
    boxes, ambiguous = _resolve_fbs_box(normalized)
    if ambiguous:
        return {
            "ok": False,
            "found": True,
            "object_type": "box",
            "stock_contour": "fbs",
            "title": "FBS-короб требует проверки",
            "code": normalized,
            "box_code": normalized,
            "can_move": False,
            "blockers": ["Один QR найден у нескольких клиентов FBS. Требуется ручная проверка."],
            "summary": [],
            "lines": [],
            "placements": [],
        }
    if not boxes:
        return {"ok": False, "found": False, "object_type": "box"}
    box = boxes[0]
    pallet = box.pallet
    physical_pallet = (
        box.source_container.parent_container
        if box.source_container_id and box.source_container.parent_container_id
        else None
    )
    physical_pallet_code = _text(getattr(physical_pallet, "container_code", ""))
    location = _fbs_box_location(box)
    lines = _fbs_box_lines(box)
    total_qty = sum(int(row["qty"] or 0) for row in lines)
    blockers = _fbs_box_blockers(box)
    location_code = _location_code(location)
    location_label = _location_label(location)
    return {
        "ok": True,
        "found": True,
        "object_type": "box",
        "stock_contour": "fbs",
        "move_kind": "fbs_box",
        "title": f"FBS-короб {box.box_code}",
        "code": box.box_code,
        "box_code": box.box_code,
        "pallet_code": physical_pallet_code or pallet.pallet_code,
        "fbs_logical_pallet_code": pallet.pallet_code,
        "fbs_box_id": int(box.id),
        "agency_id": int(box.agency_id),
        "agency_name": str(box.agency),
        "location_id": int(getattr(location, "id", 0) or 0),
        "location_label": location_label,
        "location_scan_code": location_code,
        "total_qty": total_qty,
        "contexts": [
            "Контур FBS",
            f"Физическая паллета: {physical_pallet_code or '-'}",
            f"Учётная FBS-паллета: {pallet.pallet_code}",
        ],
        "lines": lines,
        "placements": [{
            "location": location_label,
            "qty": total_qty,
            "pallet_code": physical_pallet_code or pallet.pallet_code,
            "box_code": box.box_code,
            "state_label": "FBS",
        }],
        "blockers": blockers,
        "is_free": not blockers,
        "can_move": not blockers,
        "status_label": "Свободен" if not blockers else "Не свободен",
        "status_kind": "free" if not blockers else "blocked",
        "summary": [
            {"label": "Контур", "value": "FBS"},
            {"label": "Клиент", "value": str(box.agency)},
            {"label": "Паллета", "value": physical_pallet_code or pallet.pallet_code},
            {"label": "QR места", "value": location_code},
            {"label": "Количество", "value": f"{total_qty} шт."},
        ],
    }


def active_box_relocation_for_scan(scan_value: str):
    normalized = _text(scan_value)
    boxes, ambiguous = _resolve_fbs_box(normalized)
    if not ambiguous and len(boxes) == 1:
        operation = WarehouseOperation.objects.filter(
            context_type=FBS_FREE_BOX_CONTEXT,
            context_id=str(boxes[0].id),
            operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
        ).order_by("-id").first()
        if operation:
            operation.free_box_code = boxes[0].box_code
            operation.free_box_contour = "fbs"
            return operation
    return WarehouseOperation.objects.filter(
        context_type=GENERAL_FREE_BOX_CONTEXT,
        context_id__iexact=normalized,
        operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
        status=WarehouseOperation.STATUS_IN_PROGRESS,
    ).order_by("-id").first()


@transaction.atomic
def start_fbs_box_relocation(
    *,
    box_id: int,
    performed_by=None,
    expected_location_id=None,
    performed_by_role: str = "reachtruck",
):
    box = FbsBox.objects.select_for_update(of=("self",)).select_related(
        "agency",
        "pallet__cell__location",
        "pallet__warehouse_container__current_location",
        "source_container__current_location",
        "source_container__parent_container__current_location",
    ).get(pk=int(box_id), status__in=ACTIVE_FBS_STATUSES)
    list(_fbs_box_balances(box).select_for_update(of=("self",)).order_by("id"))
    location = _fbs_box_location(box)
    if expected_location_id and int(getattr(location, "id", 0) or 0) != int(expected_location_id):
        raise FbsMovementError("Адрес FBS-короба изменился. Отсканируйте его повторно.")
    blockers = _fbs_box_blockers(box, lock_reservations=True)
    if blockers:
        raise FbsMovementError("; ".join(blockers))
    balances = list(_fbs_box_balances(box).order_by("id"))
    total_qty = sum(int(balance.qty or 0) for balance in balances)
    actor = _actor(performed_by)
    actor_role = _text(performed_by_role).lower() or "reachtruck"
    now = timezone.now()
    zone = _text(location.zone_code).upper()
    operation = WarehouseOperation.objects.create(
        agency=box.agency,
        operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
        context_type=FBS_FREE_BOX_CONTEXT,
        context_id=str(box.id),
        source_document_type="fbs_box",
        source_document_id=str(box.id),
        source_location=location,
        source_zone_code=zone,
        status=WarehouseOperation.STATUS_IN_PROGRESS,
        requested_by=actor,
        requested_by_role=actor_role,
        assigned_executor_role=actor_role,
        planned_qty=total_qty,
        started_at=now,
        comment=f"Свободное перемещение FBS-короба {box.box_code}",
    )
    task = WarehouseOperationTask.objects.create(
        operation=operation,
        task_type=WarehouseOperationTask.TYPE_BOX_MOVE,
        container=box.source_container,
        from_location=location,
        from_zone_code=zone,
        qty_planned=total_qty,
        status=WarehouseOperationTask.STATUS_IN_PROGRESS,
        assigned_to=actor,
        assigned_to_name=_actor_name(actor),
        executor_role=actor_role,
        payload={
            "free_move": True,
            "fbs_free_box_move": True,
            "fbs_box_id": int(box.id),
            "fbs_box_code": box.box_code,
            "source_logical_pallet_id": int(box.pallet_id),
            "source_logical_pallet_code": box.pallet.pallet_code,
            "source_pallet_id": int(box.source_container.parent_container_id or 0),
            "source_pallet_code": _text(
                getattr(box.source_container.parent_container, "container_code", "")
            ),
            "source_location_id": int(location.id),
        },
        started_at=now,
    )
    WarehouseEvent.objects.create(
        agency=box.agency,
        event_type="fbs_free_box_relocation_started",
        stock_context_type="fbs_box",
        stock_context_id=str(box.id),
        container=box.source_container,
        operation=operation,
        operation_task=task,
        source_document_type="fbs_box",
        source_document_id=str(box.id),
        from_location=location,
        to_location=location,
        from_zone_code=zone,
        to_zone_code=zone,
        qty=total_qty,
        payload=dict(task.payload),
        performed_by=actor,
        performed_by_role=actor_role,
        occurred_at=now,
    )
    return operation


@transaction.atomic
def complete_fbs_box_relocation(
    *,
    operation_id: int,
    destination_scan: str,
    performed_by=None,
    performed_by_role: str = "reachtruck",
):
    operation = WarehouseOperation.objects.select_for_update().get(pk=int(operation_id))
    if operation.context_type != FBS_FREE_BOX_CONTEXT or operation.status != WarehouseOperation.STATUS_IN_PROGRESS:
        raise FbsMovementError("Нет активного свободного перемещения FBS-короба.")
    box = FbsBox.objects.select_for_update(of=("self",)).select_related(
        "agency",
        "pallet__cell__location",
        "pallet__warehouse_container__current_location",
        "source_container__current_location",
        "source_container__parent_container__current_location",
    ).get(pk=int(operation.context_id), status__in=ACTIVE_FBS_STATUSES)
    actor_role = _text(performed_by_role).lower() or "reachtruck"
    from fbs.services.racks import resolve_fbs_rack_scan

    normalized_destination = normalize_operational_location_scan(destination_scan)
    rack_exists = FbsRack.objects.filter(
        location__location_code__iexact=normalized_destination,
    ).exists()
    if rack_exists:
        rack = resolve_fbs_rack_scan(destination_scan, lock=True)
        if box.source_container_id is None:
            raise FbsMovementError("У FBS-короба отсутствует физический складской контейнер.")
        destination = WarehouseLocation.objects.select_for_update().get(
            pk=rack.location_id
        )
        if int(box.source_container.current_location_id or 0) == int(destination.id):
            raise FbsMovementError("Короб уже размещен на этом PR-стеллаже.")
        validate_operational_location(
            destination,
            expected_zone="PR",
            require_fbs=True,
            required_slots=1,
        )
        blockers = _fbs_box_blockers(
            box,
            exclude_operation_id=operation.id,
            lock_reservations=True,
        )
        if blockers:
            raise FbsMovementError("; ".join(blockers))
        source_logical_pallet = box.pallet
        source_physical_pallet = box.source_container.parent_container
        source = operation.source_location or _fbs_box_location(box)
        total_qty = int(operation.planned_qty or 0)
        actor = _actor(performed_by)
        now = timezone.now()
        box.source_container.parent_container = None
        box.source_container.current_location = destination
        box.source_container.status = WarehouseContainer.STATUS_ACTIVE
        box.source_container.save(
            update_fields=["parent_container", "current_location", "status", "updated_at"]
        )
        source_pallet_released = _release_empty_source_fbs_pallet_after_box_move(
            source_logical_pallet=source_logical_pallet,
            source_physical_pallet=source_physical_pallet,
            source_location=source,
            performed_by=performed_by,
            occurred_at=now,
        )
        task = operation.tasks.select_for_update().order_by("id").first()
        if task:
            task.to_location = destination
            task.to_zone_code = "PR"
            task.qty_done = total_qty
            task.status = WarehouseOperationTask.STATUS_DONE
            task.completed_at = now
            task.payload = {
                **(task.payload or {}),
                "destination_fbs_rack_id": int(rack.id),
                "destination_fbs_rack_code": rack.rack_code,
                "destination_location_id": int(destination.id),
                "rack_placement_complete": True,
                "source_pallet_released": source_pallet_released,
            }
            task.save(
                update_fields=[
                    "to_location",
                    "to_zone_code",
                    "qty_done",
                    "status",
                    "completed_at",
                    "payload",
                    "updated_at",
                ]
            )
        operation.destination_location = destination
        operation.destination_zone_code = "PR"
        operation.status = WarehouseOperation.STATUS_DONE
        operation.done_qty = total_qty
        operation.completed_at = now
        operation.save(
            update_fields=[
                "destination_location",
                "destination_zone_code",
                "status",
                "done_qty",
                "completed_at",
                "updated_at",
            ]
        )
        staging = FbsRackStagingBox(
            box=box,
            rack=rack,
            arrival_operation=operation,
            status=FbsRackStagingBox.STATUS_PLACED,
            arrived_by=actor,
            placed_by=actor,
            placed_at=now,
        )
        staging.full_clean()
        staging.save()
        WarehouseEvent.objects.create(
            agency=box.agency,
            event_type="fbs_box_placed_at_rack",
            stock_context_type="fbs_box",
            stock_context_id=str(box.id),
            container=box.source_container,
            operation=operation,
            operation_task=task,
            source_document_type="fbs_box",
            source_document_id=str(box.id),
            from_location=source,
            to_location=destination,
            from_zone_code=_text(getattr(source, "zone_code", "")).upper(),
            to_zone_code="PR",
            qty=total_qty,
            payload={
                "free_move": True,
                "fbs_free_box_move": True,
                "rack_placement_complete": True,
                "fbs_box_code": box.box_code,
                "source_pallet_code": _text(
                    getattr(source_physical_pallet, "container_code", "")
                ),
                "source_logical_pallet_code": source_logical_pallet.pallet_code,
                "destination_rack_id": int(rack.id),
                "destination_rack_code": rack.rack_code,
                "source_pallet_released": source_pallet_released,
            },
            performed_by=actor,
            performed_by_role=actor_role,
            occurred_at=now,
        )
        from fbs.services.picking import resort_queued_pick_batches_for_balances

        moved_balance_ids = tuple(
            _fbs_box_balances(box).values_list("id", flat=True)
        )
        transaction.on_commit(
            lambda balance_ids=moved_balance_ids: (
                resort_queued_pick_batches_for_balances(balance_ids)
            )
        )
        return operation
    destination = _resolve_direct_box_destination(destination_scan)
    if destination is not None:
        destination_zone = _text(destination.zone_code).upper()
        if destination_zone not in ("OS", "PR", "OTG"):
            raise FbsMovementError(
                "Целый FBS-короб можно поставить только в точное место OS, PR или ОТГ."
            )
        target_fbs_pallet = None
        target_container = None
        target_loose_box_containers = []
        target_balance_ids: list[int] = []
        if destination_zone == "OS":
            try:
                require_concrete_movement_location(
                    destination,
                    purpose="свободного перемещения FBS-короба",
                )
            except ValidationError as exc:
                messages = getattr(exc, "messages", ())
                raise FbsMovementError(
                    "; ".join(messages) if messages else str(exc)
                ) from exc
            if (
                not destination.is_storage
                or min(
                    int(destination.row_no or 0),
                    int(destination.section_no or 0),
                    int(destination.tier_no or 0),
                    int(destination.cell_no or 0),
                ) <= 0
            ):
                raise FbsMovementError(
                    "Для OS отсканируйте точную активную ячейку действующей топологии."
                )
            target_fbs_pallet = _os_destination_fbs_pallet(
                destination=destination,
                box=box,
            )
            if target_fbs_pallet is not None:
                target_container = WarehouseContainer.objects.select_for_update(
                    of=("self",)
                ).get(pk=target_fbs_pallet.warehouse_container_id)
                occupancy_message = os_location_occupancy_message(
                    destination,
                    exclude_container_code=target_container.container_code,
                )
                if occupancy_message:
                    raise FbsMovementError(occupancy_message)
                target_blocker = _target_pallet_blocker(
                    agency_id=box.agency_id,
                    pallet_code=target_container.container_code,
                )
                if target_blocker:
                    raise FbsMovementError(target_blocker)
                if WarehouseOperationTask.objects.filter(
                    container_id=target_container.id,
                    status__in=ACTIVE_TASK_STATUSES,
                ).exclude(operation=operation).exists():
                    raise FbsMovementError(
                        "Паллета в выбранной ячейке участвует в активной складской операции."
                    )
                target_balances = list(
                    FbsStockBalance.objects.select_for_update()
                    .filter(
                        box__pallet=target_fbs_pallet,
                        box__status__in=ACTIVE_FBS_STATUSES,
                        qty__gt=0,
                    )
                    .order_by("id")
                )
                target_reservations = relocation_reservation_state(
                    [balance.id for balance in target_balances],
                    lock=True,
                )
                if target_reservations.blockers:
                    raise FbsMovementError("; ".join(target_reservations.blockers))
                active_box_count = (
                    target_fbs_pallet.boxes.filter(status__in=ACTIVE_FBS_STATUSES)
                    .exclude(pk=box.pk)
                    .count()
                )
                if active_box_count >= int(target_fbs_pallet.max_boxes or 0):
                    raise FbsMovementError(
                        "На FBS-паллете в выбранной ячейке нет свободного места для короба."
                    )
                target_balance_ids = [balance.id for balance in target_balances]
            else:
                target_loose_box_containers = (
                    _os_destination_loose_fbs_box_containers(
                        destination=destination,
                        box=box,
                    )
                )
                capacity = int(destination.capacity_containers or 0)
                if capacity and len(target_loose_box_containers) >= capacity:
                    raise FbsMovementError(
                        "В выбранной ячейке нет свободного места для ещё одного короба."
                    )
                occupancy_message = os_location_occupancy_message(
                    destination,
                    exclude_container_codes=[
                        container.container_code
                        for container in target_loose_box_containers
                    ],
                )
                if occupancy_message:
                    raise FbsMovementError(occupancy_message)
        else:
            try:
                validate_operational_location(
                    destination,
                    expected_zone=destination_zone,
                    require_fbs=True,
                    required_slots=1,
                )
            except ValidationError as exc:
                messages = getattr(exc, "messages", ())
                raise FbsMovementError(
                    "; ".join(messages) if messages else str(exc)
                ) from exc
        if box.source_container_id is None:
            raise FbsMovementError(
                "У FBS-короба отсутствует физический складской контейнер."
            )
        if int(box.source_container.current_location_id or 0) == int(destination.id):
            raise FbsMovementError("Короб уже находится в этом месте.")
        blockers = _fbs_box_blockers(
            box,
            exclude_operation_id=operation.id,
            lock_reservations=True,
        )
        if blockers:
            raise FbsMovementError("; ".join(blockers))
        source_logical_pallet = box.pallet
        source_physical_pallet = box.source_container.parent_container
        source = operation.source_location or _fbs_box_location(box)
        total_qty = int(operation.planned_qty or 0)
        actor = _actor(performed_by)
        now = timezone.now()
        if target_fbs_pallet is not None and box.pallet_id != target_fbs_pallet.id:
            box.pallet = target_fbs_pallet
            box.full_clean()
            box.save(update_fields=["pallet", "updated_at"])
        box.source_container.parent_container = target_container
        box.source_container.current_location = destination
        box.source_container.status = WarehouseContainer.STATUS_ACTIVE
        box.source_container.save(
            update_fields=[
                "parent_container",
                "current_location",
                "status",
                "updated_at",
            ]
        )
        source_pallet_released = _release_empty_source_fbs_pallet_after_box_move(
            source_logical_pallet=source_logical_pallet,
            source_physical_pallet=source_physical_pallet,
            source_location=source,
            performed_by=performed_by,
            occurred_at=now,
        )
        task = operation.tasks.select_for_update().order_by("id").first()
        if task:
            task.to_location = destination
            task.to_zone_code = _text(destination.zone_code).upper()
            task.qty_done = total_qty
            task.status = WarehouseOperationTask.STATUS_DONE
            task.completed_at = now
            task.payload = {
                **(task.payload or {}),
                "destination_location_id": int(destination.id),
                "destination_location_code": destination.location_code,
                "destination_exact_place": True,
                "destination_pallet_auto_resolved": bool(target_container),
                "destination_shared_loose_boxes": bool(
                    target_loose_box_containers
                ),
                "destination_existing_loose_box_count": len(
                    target_loose_box_containers
                ),
                "destination_pallet_id": int(getattr(target_container, "id", 0) or 0),
                "destination_pallet_code": _text(
                    getattr(target_container, "container_code", "")
                ),
                "source_pallet_released": source_pallet_released,
            }
            task.save(
                update_fields=[
                    "to_location",
                    "to_zone_code",
                    "qty_done",
                    "status",
                    "completed_at",
                    "payload",
                    "updated_at",
                ]
            )
        WarehouseEvent.objects.create(
            agency=box.agency,
            event_type="fbs_free_box_relocation_completed",
            stock_context_type="fbs_box",
            stock_context_id=str(box.id),
            container=box.source_container,
            operation=operation,
            operation_task=task,
            source_document_type="fbs_box",
            source_document_id=str(box.id),
            from_location=source,
            to_location=destination,
            from_zone_code=_text(getattr(source, "zone_code", "")).upper(),
            to_zone_code=_text(destination.zone_code).upper(),
            qty=total_qty,
            payload={
                "free_move": True,
                "fbs_free_box_move": True,
                "fbs_box_code": box.box_code,
                "source_pallet_code": _text(
                    getattr(source_physical_pallet, "container_code", "")
                ),
                "source_logical_pallet_code": source_logical_pallet.pallet_code,
                "destination_location_id": int(destination.id),
                "destination_location_code": destination.location_code,
                "destination_exact_place": True,
                "destination_pallet_auto_resolved": bool(target_container),
                "destination_shared_loose_boxes": bool(
                    target_loose_box_containers
                ),
                "destination_existing_loose_box_count": len(
                    target_loose_box_containers
                ),
                "destination_pallet_code": _text(
                    getattr(target_container, "container_code", "")
                ),
                "source_pallet_released": source_pallet_released,
            },
            performed_by=actor,
            performed_by_role=actor_role,
            occurred_at=now,
        )
        operation.destination_location = destination
        operation.destination_zone_code = _text(destination.zone_code).upper()
        operation.status = WarehouseOperation.STATUS_DONE
        operation.done_qty = total_qty
        operation.completed_at = now
        operation.save(
            update_fields=[
                "destination_location",
                "destination_zone_code",
                "status",
                "done_qty",
                "completed_at",
                "updated_at",
            ]
        )
        from fbs.services.picking import resort_queued_pick_batches_for_balances

        moved_balance_ids = list(
            _fbs_box_balances(box).values_list("id", flat=True)
        )
        reroute_balance_ids = tuple(moved_balance_ids + target_balance_ids)
        transaction.on_commit(
            lambda balance_ids=reroute_balance_ids: (
                resort_queued_pick_batches_for_balances(balance_ids)
            )
        )
        return operation
    target, ambiguous = _resolve_warehouse_pallet(
        destination_scan,
        agency_id=box.agency_id,
        include_fbs=True,
    )
    if ambiguous:
        raise FbsMovementError("По этому QR найдено несколько паллет. Отсканируйте прямой код.")
    if target is None:
        raise FbsMovementError("Паллета назначения не найдена.")
    target = WarehouseContainer.objects.select_for_update(of=("self",)).select_related(
        "current_location"
    ).get(pk=target.pk)
    if int(box.source_container.parent_container_id or 0) == int(target.id):
        raise FbsMovementError("Короб уже находится на этой паллете.")
    destination = target.current_location
    if destination is None or not destination.is_active:
        raise FbsMovementError("У паллеты назначения нет действующего физического места.")
    try:
        require_concrete_movement_location(
            destination,
            purpose="свободного перемещения FBS-короба",
        )
    except ValidationError as exc:
        messages = getattr(exc, "messages", ())
        raise FbsMovementError("; ".join(messages) if messages else str(exc)) from exc
    target_blocker = _target_pallet_blocker(
        agency_id=box.agency_id,
        pallet_code=target.container_code,
    )
    if target_blocker:
        raise FbsMovementError(target_blocker)
    blockers = _fbs_box_blockers(
        box,
        exclude_operation_id=operation.id,
        lock_reservations=True,
    )
    if blockers:
        raise FbsMovementError("; ".join(blockers))
    if WarehouseOperationTask.objects.filter(
        container_id=target.id,
        status__in=ACTIVE_TASK_STATUSES,
    ).exclude(operation=operation).exists():
        raise FbsMovementError("Паллета назначения участвует в активной складской операции.")

    source_logical_pallet = box.pallet
    source_physical_pallet = box.source_container.parent_container
    source = operation.source_location or _fbs_box_location(box)
    total_qty = int(operation.planned_qty or 0)
    actor = _actor(performed_by)
    now = timezone.now()
    target_fbs_pallet = FbsPallet.objects.select_for_update(of=("self",)).filter(
        warehouse_container=target,
        agency_id=box.agency_id,
        status__in=ACTIVE_FBS_PALLET_STATUSES,
    ).first()
    if target_fbs_pallet is not None:
        target_balances = list(FbsStockBalance.objects.select_for_update().filter(
            box__pallet=target_fbs_pallet,
            box__status__in=ACTIVE_FBS_STATUSES,
            qty__gt=0,
        ).order_by("id"))
        target_reservations = relocation_reservation_state(
            [balance.id for balance in target_balances],
            lock=True,
        )
        if target_reservations.blockers:
            raise FbsMovementError("; ".join(target_reservations.blockers))
        count = target_fbs_pallet.boxes.filter(status__in=ACTIVE_FBS_STATUSES).exclude(pk=box.pk).count()
        if count >= int(target_fbs_pallet.max_boxes or 0):
            raise FbsMovementError("На FBS-паллете назначения нет свободного места для короба.")
        box.pallet = target_fbs_pallet
        box.full_clean()
        box.save(update_fields=["pallet", "updated_at"])
    if box.source_container_id:
        box.source_container.parent_container = target
        box.source_container.current_location = destination
        box.source_container.status = WarehouseContainer.STATUS_ACTIVE
        box.source_container.save(update_fields=[
            "parent_container", "current_location", "status", "updated_at"
        ])
    source_pallet_released = _release_empty_source_fbs_pallet_after_box_move(
        source_logical_pallet=source_logical_pallet,
        source_physical_pallet=source_physical_pallet,
        source_location=source,
        performed_by=performed_by,
        occurred_at=now,
    )
    task = operation.tasks.select_for_update().order_by("id").first()
    if task:
        task.to_location = destination
        task.to_zone_code = _text(destination.zone_code).upper()
        task.qty_done = total_qty
        task.status = WarehouseOperationTask.STATUS_DONE
        task.completed_at = now
        task.payload = {
            **(task.payload or {}),
            "destination_pallet_id": int(target.id),
            "destination_pallet_code": target.container_code,
            "destination_location_id": int(destination.id),
            "source_pallet_released": source_pallet_released,
        }
        task.save(update_fields=[
            "to_location", "to_zone_code", "qty_done", "status", "completed_at", "payload", "updated_at"
        ])
    WarehouseEvent.objects.create(
        agency=box.agency,
        event_type="fbs_free_box_relocation_completed",
        stock_context_type="fbs_box",
        stock_context_id=str(box.id),
        container=box.source_container,
        operation=operation,
        operation_task=task,
        source_document_type="fbs_box",
        source_document_id=str(box.id),
        from_location=source,
        to_location=destination,
        from_zone_code=_text(getattr(source, "zone_code", "")).upper(),
        to_zone_code=_text(destination.zone_code).upper(),
        qty=total_qty,
        payload={
            "free_move": True,
            "fbs_free_box_move": True,
            "fbs_box_code": box.box_code,
            "source_pallet_code": _text(
                getattr(source_physical_pallet, "container_code", "")
            ),
            "source_logical_pallet_code": source_logical_pallet.pallet_code,
            "destination_pallet_code": target.container_code,
            "source_pallet_released": source_pallet_released,
        },
        performed_by=actor,
        performed_by_role=actor_role,
        occurred_at=now,
    )
    operation.destination_location = destination
    operation.destination_zone_code = _text(destination.zone_code).upper()
    operation.status = WarehouseOperation.STATUS_DONE
    operation.done_qty = total_qty
    operation.completed_at = now
    operation.save(update_fields=[
        "destination_location", "destination_zone_code", "status", "done_qty", "completed_at", "updated_at"
    ])
    from fbs.services.picking import resort_queued_pick_batches_for_balances

    moved_balance_ids = list(_fbs_box_balances(box).values_list("id", flat=True))
    target_balance_ids = (
        [balance.id for balance in target_balances]
        if target_fbs_pallet is not None
        else []
    )
    reroute_balance_ids = tuple(moved_balance_ids + target_balance_ids)
    transaction.on_commit(
        lambda balance_ids=reroute_balance_ids: (
            resort_queued_pick_batches_for_balances(balance_ids)
        )
    )
    return operation


@transaction.atomic
def cancel_fbs_box_relocation(*, operation_id: int, performed_by=None):
    operation = WarehouseOperation.objects.select_for_update().get(pk=int(operation_id))
    if operation.context_type != FBS_FREE_BOX_CONTEXT:
        raise FbsMovementError("Операция не является перемещением FBS-короба.")
    if operation.status == WarehouseOperation.STATUS_CANCELED:
        return operation
    if operation.status != WarehouseOperation.STATUS_IN_PROGRESS:
        raise FbsMovementError("Завершённое перемещение FBS-короба отменить нельзя.")
    box = FbsBox.objects.select_related("source_container", "pallet__cell__location").get(pk=int(operation.context_id))
    now = timezone.now()
    task = operation.tasks.select_for_update().order_by("id").first()
    if task:
        task.status = WarehouseOperationTask.STATUS_CANCELED
        task.completed_at = now
        task.save(update_fields=["status", "completed_at", "updated_at"])
    actor = _actor(performed_by)
    location = operation.source_location or _fbs_box_location(box)
    WarehouseEvent.objects.create(
        agency=box.agency,
        event_type="fbs_free_box_relocation_canceled",
        stock_context_type="fbs_box",
        stock_context_id=str(box.id),
        container=box.source_container,
        operation=operation,
        operation_task=task,
        from_location=location,
        to_location=location,
        from_zone_code=_text(getattr(location, "zone_code", "")).upper(),
        to_zone_code=_text(getattr(location, "zone_code", "")).upper(),
        qty=int(operation.planned_qty or 0),
        payload={"free_move": True, "fbs_free_box_move": True, "fbs_box_code": box.box_code},
        performed_by=actor,
        performed_by_role="reachtruck",
        occurred_at=now,
    )
    operation.status = WarehouseOperation.STATUS_CANCELED
    operation.completed_at = now
    operation.save(update_fields=["status", "completed_at", "updated_at"])
    return operation


def fbs_box_relocation_context(operation: WarehouseOperation) -> dict:
    box = FbsBox.objects.select_related(
        "pallet__cell__location", "source_container__parent_container",
        "pallet__warehouse_container__current_location",
    ).filter(pk=int(operation.context_id), agency_id=operation.agency_id).first()
    if box is None:
        return {}
    source = operation.source_location or _fbs_box_location(box)
    lines = _fbs_box_lines(box)
    return {
        "moving": True,
        "operation": operation,
        "object_type": "box",
        "object_code": box.box_code,
        "box_code": box.box_code,
        "pallet_code": _text(
            getattr(box.source_container.parent_container, "container_code", "")
        ) or box.pallet.pallet_code,
        "move_contour_label": "FBS",
        "moving_title": f"Короб {box.box_code} в пути",
        "destination_prompt": (
            "Сканируйте точное место OS / PR / ОТГ, "
            "FBS-паллету или PR-стеллаж назначения"
        ),
        "destination_label": "Место, паллета или PR-стеллаж назначения",
        "destination_button": "Подтвердить место назначения",
        "source_label": _location_label(source),
        "source_scan_code": _location_code(source),
        "total_qty": sum(int(row["qty"] or 0) for row in lines),
        "lines": lines,
    }


def _general_box_snapshots(box_code: str, *, lock: bool = False):
    queryset = WarehouseStockSnapshot.objects.filter(
        Q(container_code__iexact=_text(box_code))
        | Q(container__container_code__iexact=_text(box_code)),
        is_archived=False,
        qty__gt=0,
    ).select_related("agency", "container", "parent_container", "location", "active_operation")
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    return queryset.order_by("id")


@transaction.atomic
def start_general_box_relocation(*, box_code: str, performed_by=None, expected_location_id=None):
    snapshots = list(_general_box_snapshots(box_code, lock=True))
    if not snapshots:
        raise ValueError("Короб не найден в общем складском остатке.")
    agency_ids = {int(row.agency_id or 0) for row in snapshots}
    location_ids = {int(row.location_id or 0) for row in snapshots}
    if len(agency_ids) != 1 or len(location_ids) != 1:
        raise ValueError("Короб разбит между клиентами или местами. Требуется проверка.")
    source = snapshots[0].location
    if expected_location_id and int(getattr(source, "id", 0) or 0) != int(expected_location_id):
        raise ValueError("Адрес короба изменился. Отсканируйте его повторно.")
    if any(row.active_operation_id for row in snapshots):
        raise ValueError("У короба уже есть активная складская операция.")
    if any(
        int(row.processing_reserved_qty or 0) > 0
        or int(row.shipping_reserved_qty or 0) > 0
        or int(row.other_reserved_qty or 0) > 0
        for row in snapshots
    ):
        raise ValueError("Короб зарезервирован и не может быть свободно перемещён.")
    allowed_states = {
        WarehouseStateCode.STORED.value,
        WarehouseStateCode.PLACED_IN_RECEIVING.value,
        WarehouseStateCode.IN_PROCESSING_ZONE.value,
        WarehouseStateCode.PLACED_AFTER_PROCESSING.value,
    }
    if any(_text(row.warehouse_state_code) not in allowed_states for row in snapshots):
        raise ValueError("Короб не находится на свободном складском остатке.")
    box_container = next((row.container for row in snapshots if row.container_id), None)
    if box_container is None or box_container.container_type != WarehouseContainer.TYPE_BOX:
        raise ValueError("У короба отсутствует физический складской контейнер.")
    actor = _actor(performed_by)
    now = timezone.now()
    total_qty = sum(int(row.qty or 0) for row in snapshots)
    source_pallet = next((row.parent_container for row in snapshots if row.parent_container_id), None)
    operation = WarehouseOperation.objects.create(
        agency=snapshots[0].agency,
        operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
        context_type=GENERAL_FREE_BOX_CONTEXT,
        context_id=_text(box_code),
        source_location=source,
        source_zone_code=_text(getattr(source, "zone_code", "")).upper(),
        status=WarehouseOperation.STATUS_IN_PROGRESS,
        requested_by=actor,
        requested_by_role="reachtruck",
        assigned_executor_role="reachtruck",
        planned_qty=total_qty,
        started_at=now,
        comment=f"Свободное перемещение короба {_text(box_code)}",
    )
    task = WarehouseOperationTask.objects.create(
        operation=operation,
        task_type=WarehouseOperationTask.TYPE_BOX_MOVE,
        container=box_container,
        from_location=source,
        from_zone_code=_text(getattr(source, "zone_code", "")).upper(),
        qty_planned=total_qty,
        status=WarehouseOperationTask.STATUS_IN_PROGRESS,
        assigned_to=actor,
        assigned_to_name=_actor_name(actor),
        executor_role="reachtruck",
        payload={
            "free_move": True,
            "free_box_move": True,
            "box_code": _text(box_code),
            "source_pallet_id": int(getattr(source_pallet, "id", 0) or 0),
            "source_pallet_code": _text(getattr(source_pallet, "container_code", "")),
            "snapshot_ids": [int(row.id) for row in snapshots],
        },
        started_at=now,
    )
    for snapshot in snapshots:
        transition = WarehouseTransitionService.apply_event(
            snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
            WarehouseEventType.MOVEMENT_STARTED,
            operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
        )
        event = WarehouseEvent.objects.create(
            agency=snapshot.agency,
            event_type=WarehouseEventType.MOVEMENT_STARTED.value,
            stock_context_type=snapshot.source_context_type,
            stock_context_id=snapshot.source_context_id,
            container=box_container,
            operation=operation,
            operation_task=task,
            from_location=snapshot.location,
            to_location=snapshot.location,
            from_zone_code=snapshot.zone_code,
            to_zone_code=snapshot.zone_code,
            qty=int(snapshot.qty or 0),
            payload={"free_move": True, "free_box_move": True, "box_code": _text(box_code)},
            performed_by=actor,
            performed_by_role="reachtruck",
            occurred_at=now,
        )
        snapshot.active_operation = operation
        snapshot.active_operation_type = operation.operation_type
        snapshot.warehouse_state_code = transition.code.value
        snapshot.last_event = event
        snapshot.save(update_fields=[
            "active_operation", "active_operation_type", "warehouse_state_code", "last_event", "updated_at"
        ])
    return operation


def _resolve_warehouse_pallet(
    scan_value: str,
    *,
    agency_id: int,
    include_fbs: bool,
):
    normalized = _text(scan_value)
    queryset = WarehouseContainer.objects.filter(
        agency_id=int(agency_id),
        status=WarehouseContainer.STATUS_ACTIVE,
        container_type__in=(WarehouseContainer.TYPE_PALLET, WarehouseContainer.TYPE_MIXED_PALLET),
    ).select_related("current_location")
    if not include_fbs:
        queryset = queryset.filter(fbs_pallet__isnull=True)
    direct = list(queryset.filter(container_code__iexact=normalized).order_by("id")[:2])
    if direct:
        return direct[0], len(direct) > 1
    matches = []
    for pallet in queryset.order_by("id")[:10000]:
        if _same_pallet_code_scan(normalized, pallet.container_code):
            matches.append(pallet)
            if len(matches) > 1:
                break
    return (matches[0] if matches else None), len(matches) > 1


def _resolve_general_pallet(scan_value: str, *, agency_id: int):
    return _resolve_warehouse_pallet(
        scan_value,
        agency_id=agency_id,
        include_fbs=False,
    )


def _os_destination_loose_general_box_containers(
    *,
    destination: WarehouseLocation,
    agency_id: int,
    moving_container_ids=(),
):
    """Return compatible loose warehouse boxes already stored in one OS cell."""
    containers = list(
        WarehouseContainer.objects.select_for_update(of=("self",))
        .filter(
            current_location=destination,
            status=WarehouseContainer.STATUS_ACTIVE,
            container_type=WarehouseContainer.TYPE_BOX,
            parent_container__isnull=True,
            fbs_box__isnull=True,
        )
        .exclude(id__in=tuple(moving_container_ids or ()))
        .order_by("id")
    )
    if any(int(container.agency_id or 0) != int(agency_id) for container in containers):
        raise ValueError(
            "В выбранной ячейке находится короб другого клиента. "
            "Смешивать клиентов запрещено."
        )
    return containers


@transaction.atomic
def complete_general_box_relocation(*, operation_id: int, destination_scan: str, performed_by=None):
    operation = WarehouseOperation.objects.select_for_update().get(pk=int(operation_id))
    if operation.context_type != GENERAL_FREE_BOX_CONTEXT or operation.status != WarehouseOperation.STATUS_IN_PROGRESS:
        raise ValueError("Нет активного свободного перемещения короба.")
    snapshots = list(
        WarehouseStockSnapshot.objects.select_for_update(of=("self",)).select_related(
            "container", "parent_container", "location"
        ).filter(active_operation=operation, is_archived=False, qty__gt=0).order_by("id")
    )
    if not snapshots:
        raise ValueError("Остаток перемещаемого короба не найден.")
    task = operation.tasks.select_for_update().order_by("id").first()
    payload = task.payload if task and isinstance(task.payload, dict) else {}
    moving_container_ids = {
        int(snapshot.container_id)
        for snapshot in snapshots
        if snapshot.container_id
    }
    destination_pallet = None
    destination_type = "exact_location"
    destination = _resolve_direct_box_destination(destination_scan)
    shared_loose_boxes = []
    if destination is not None:
        destination_zone = _text(destination.zone_code).upper()
        if destination_zone not in ("OS", "PR", "OTG"):
            raise ValueError(
                "Целый короб можно поставить только в точное место OS, PR или ОТГ."
            )
        if any(
            int(snapshot.location_id or 0) == int(destination.id)
            for snapshot in snapshots
        ):
            raise ValueError("Короб уже находится в этом месте.")
        if destination_zone == "OS":
            try:
                require_concrete_movement_location(
                    destination,
                    purpose="свободного перемещения короба",
                )
            except ValidationError as exc:
                messages = getattr(exc, "messages", ())
                raise ValueError(
                    "; ".join(messages) if messages else str(exc)
                ) from exc
            if (
                not destination.is_storage
                or min(
                    int(destination.row_no or 0),
                    int(destination.section_no or 0),
                    int(destination.tier_no or 0),
                    int(destination.cell_no or 0),
                ) <= 0
            ):
                raise ValueError(
                    "Для OS отсканируйте точную активную ячейку действующей топологии."
                )
            shared_loose_boxes = _os_destination_loose_general_box_containers(
                destination=destination,
                agency_id=operation.agency_id,
                moving_container_ids=moving_container_ids,
            )
            capacity = int(destination.capacity_containers or 0)
            if capacity and len(shared_loose_boxes) >= capacity:
                raise ValueError(
                    "В выбранной ячейке нет свободного места для ещё одного короба."
                )
            occupancy_message = os_location_occupancy_message(
                destination,
                exclude_container_codes=[
                    container.container_code for container in shared_loose_boxes
                ],
            )
            if occupancy_message:
                raise ValueError(occupancy_message)
        else:
            try:
                validate_operational_location(
                    destination,
                    expected_zone=destination_zone,
                    required_slots=1,
                )
            except ValidationError as exc:
                messages = getattr(exc, "messages", ())
                raise ValueError(
                    "; ".join(messages) if messages else str(exc)
                ) from exc
    else:
        destination_type = "pallet"
        destination_pallet, ambiguous = _resolve_general_pallet(
            destination_scan,
            agency_id=operation.agency_id,
        )
        if ambiguous:
            raise ValueError("По этому QR найдено несколько паллет. Отсканируйте прямой код.")
        if destination_pallet is None:
            raise ValueError("Место или паллета назначения не найдены.")
        destination_pallet = WarehouseContainer.objects.select_for_update(of=("self",)).select_related(
            "current_location"
        ).get(pk=destination_pallet.pk)
        if int(payload.get("source_pallet_id") or 0) == int(destination_pallet.id):
            raise ValueError("Короб уже находится на этой паллете.")
        destination_rows = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",)).select_related("location").filter(
                Q(parent_container=destination_pallet) | Q(container=destination_pallet),
                is_archived=False,
                qty__gt=0,
            ).order_by("id")
        )
        destination_ids = {int(row.location_id or 0) for row in destination_rows}
        if len(destination_ids) > 1:
            raise ValueError("Паллета назначения разбита по нескольким местам.")
        if any(row.active_operation_id for row in destination_rows):
            raise ValueError("Паллета назначения участвует в активной складской операции.")
        destination = destination_rows[0].location if destination_rows else destination_pallet.current_location
        if destination is None or not destination.is_active:
            raise ValueError("У паллеты назначения нет действующего складского места.")
        target_blocker = _target_pallet_blocker(
            agency_id=operation.agency_id,
            pallet_code=destination_pallet.container_code,
        )
        if target_blocker:
            raise ValueError(target_blocker)
        if WarehouseOperationTask.objects.filter(
            container=destination_pallet,
            status__in=ACTIVE_TASK_STATUSES,
        ).exclude(operation=operation).exists():
            raise ValueError("Паллета назначения участвует в активной складской операции.")
    destination_payload = {
        "destination_type": destination_type,
        "destination_location_id": int(destination.id),
        "destination_location_code": destination.location_code,
        "destination_exact_place": destination_type == "exact_location",
        "destination_shared_loose_boxes": bool(shared_loose_boxes),
        "destination_existing_loose_box_count": len(shared_loose_boxes),
        "destination_pallet_id": int(getattr(destination_pallet, "id", 0) or 0),
        "destination_pallet_code": _text(
            getattr(destination_pallet, "container_code", "")
        ),
    }
    actor = _actor(performed_by)
    now = timezone.now()
    total_done = 0
    box_container_ids: set[int] = set()
    source_pallet_id = int(payload.get("source_pallet_id") or 0)
    for snapshot in snapshots:
        try:
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.MOVEMENT_COMPLETED,
                operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
                zone_to=destination.zone_code,
            )
            next_state = transition.code.value
        except WarehouseTransitionError:
            next_state = snapshot.warehouse_state_code
        event = WarehouseEvent.objects.create(
            agency=snapshot.agency,
            event_type=WarehouseEventType.MOVEMENT_COMPLETED.value,
            stock_context_type=snapshot.source_context_type,
            stock_context_id=snapshot.source_context_id,
            container=snapshot.container,
            operation=operation,
            operation_task=task,
            from_location=snapshot.location,
            to_location=destination,
            from_zone_code=snapshot.zone_code,
            to_zone_code=destination.zone_code,
            qty=int(snapshot.qty or 0),
            payload={
                "free_move": True,
                "free_box_move": True,
                "box_code": operation.context_id,
                **destination_payload,
            },
            performed_by=actor,
            performed_by_role="reachtruck",
            occurred_at=now,
        )
        if snapshot.container_id:
            box_container_ids.add(int(snapshot.container_id))
        snapshot.parent_container = destination_pallet
        snapshot.location = destination
        snapshot.zone_code = destination.zone_code
        snapshot.zone_kind = destination.zone_kind
        snapshot.warehouse_state_code = next_state
        snapshot.active_operation = None
        snapshot.active_operation_type = ""
        snapshot.last_event = event
        snapshot.save(update_fields=[
            "parent_container", "location", "zone_code", "zone_kind", "warehouse_state_code",
            "active_operation", "active_operation_type", "last_event", "updated_at"
        ])
        total_done += int(snapshot.qty or 0)
    if box_container_ids:
        WarehouseContainer.objects.filter(id__in=box_container_ids).update(
            parent_container=destination_pallet,
            current_location=destination,
            updated_at=now,
        )
    if source_pallet_id:
        source_pallet = WarehouseContainer.objects.select_for_update().filter(pk=source_pallet_id).first()
        if source_pallet and not WarehouseContainer.objects.filter(
            parent_container=source_pallet, status=WarehouseContainer.STATUS_ACTIVE
        ).exists() and not WarehouseStockSnapshot.objects.filter(
            parent_container=source_pallet, is_archived=False, qty__gt=0
        ).exists():
            source_pallet.current_location = None
            source_pallet.parent_container = None
            source_pallet.status = WarehouseContainer.STATUS_ARCHIVED
            source_pallet.save(update_fields=["current_location", "parent_container", "status", "updated_at"])
    if task:
        task.to_location = destination
        task.to_zone_code = destination.zone_code
        task.qty_done = total_done
        task.status = WarehouseOperationTask.STATUS_DONE
        task.completed_at = now
        task.payload = {
            **payload,
            **destination_payload,
        }
        task.save(update_fields=[
            "to_location", "to_zone_code", "qty_done", "status", "completed_at", "payload", "updated_at"
        ])
    operation.destination_location = destination
    operation.destination_zone_code = destination.zone_code
    operation.status = WarehouseOperation.STATUS_DONE
    operation.done_qty = total_done
    operation.completed_at = now
    operation.save(update_fields=[
        "destination_location", "destination_zone_code", "status", "done_qty", "completed_at", "updated_at"
    ])
    return operation


def general_box_relocation_context(operation: WarehouseOperation, *, lines: list[dict]) -> dict:
    task = operation.tasks.order_by("id").first()
    payload = task.payload if task and isinstance(task.payload, dict) else {}
    return {
        "moving": True,
        "operation": operation,
        "object_type": "box",
        "object_code": operation.context_id,
        "box_code": operation.context_id,
        "pallet_code": _text(payload.get("source_pallet_code")),
        "move_contour_label": "FBO",
        "moving_title": f"Короб {operation.context_id} в пути",
        "destination_prompt": "Сканируйте точное место OS / PR / ОТГ или паллету назначения",
        "destination_label": "Место или паллета назначения",
        "destination_button": "Переместить короб",
        "source_label": _location_label(operation.source_location),
        "source_scan_code": _location_code(operation.source_location),
        "total_qty": sum(int(row.get("qty") or 0) for row in lines),
        "lines": lines,
    }
