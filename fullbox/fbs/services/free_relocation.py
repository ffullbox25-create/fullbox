from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from fbs.exceptions import FbsFeatureDisabled, FbsMovementError
from fbs.flags import feature_enabled
from fbs.models import (
    FbsBox,
    FbsInternalMovement,
    FbsInventorySession,
    FbsPallet,
    FbsReplenishmentAllocation,
    FbsReplenishmentPlan,
    FbsStockBalance,
    FbsStorageCell,
    FbsStorageLock,
)
from reachtruck.models import BoxClaim, MoveTask, PalletLock
from reachtruck.services.task_commands import _same_box_code_scan, _same_pallet_code_scan
from shipping.reservation_units import active_reserved_pallet_codes_for_agency
from sklad.location_occupancy import (
    FBS_STORAGE_CONTEXT_TYPE,
    NON_OCCUPYING_STOCK_STATES,
    active_os_physical_containers,
    os_location_occupancy_message,
)
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseOperationTask,
    WarehouseStockSnapshot,
)
from sklad.topology import os_location_code
from sklad.services.operational_locations import (
    require_concrete_movement_location,
    validate_operational_location,
)

from .inventory import FBS_FREE_RELOCATION_CONTEXT
from .relocation_reservations import relocation_reservation_state


ACTIVE_PALLET_STATUSES = (FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE)
ACTIVE_BOX_STATUSES = (FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE)
ACTIVE_REPLENISHMENT_STATUSES = (
    FbsReplenishmentPlan.STATUS_PROPOSED,
    FbsReplenishmentPlan.STATUS_CONFIRMED,
    FbsReplenishmentPlan.STATUS_IN_PROGRESS,
    FbsReplenishmentPlan.STATUS_AWAITING_PACK,
    FbsReplenishmentPlan.STATUS_BLOCKED,
)
ACTIVE_REPLENISHMENT_ALLOCATION_STATUSES = (
    FbsReplenishmentAllocation.STATUS_RESERVED,
    FbsReplenishmentAllocation.STATUS_IN_PROGRESS,
    FbsReplenishmentAllocation.STATUS_STAGED,
)
ACTIVE_INTERNAL_MOVEMENT_STATUSES = (
    FbsInternalMovement.STATUS_PROPOSED,
    FbsInternalMovement.STATUS_IN_PROGRESS,
    FbsInternalMovement.STATUS_BLOCKED,
)
ACTIVE_TASK_STATUSES = (MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS)
FBS_FREE_SOURCE_ZONES = {"OS", "PR", "OTG"}
def _require_writes() -> None:
    if not feature_enabled("module"):
        raise FbsFeatureDisabled("Модуль FBS выключен.")
    if not feature_enabled("warehouse_writes"):
        raise FbsFeatureDisabled("Складские операции FBS выключены.")


def _authenticated_user(user):
    return user if getattr(user, "is_authenticated", False) else None


def _normalize(value) -> str:
    return str(value or "").strip()


def _zone_code(location: WarehouseLocation | None) -> str:
    return _normalize(getattr(location, "zone_code", "")).upper()


def _physical_location(pallet: FbsPallet) -> WarehouseLocation:
    container = getattr(pallet, "warehouse_container", None)
    location = getattr(container, "current_location", None) or pallet.cell.location
    if location is None:
        raise FbsMovementError("У FBS-паллеты не найдено текущее физическое место.")
    return location


def allowed_fbs_destination_zones(source_zone_code: str) -> set[str]:
    source_zone = _normalize(source_zone_code).upper()
    if source_zone not in FBS_FREE_SOURCE_ZONES:
        return set()
    return set(FBS_FREE_SOURCE_ZONES)


def _location_scan_code(location: WarehouseLocation) -> str:
    if _zone_code(location) == "OS" and all(
        int(value or 0) > 0
        for value in (
            location.row_no,
            location.section_no,
            location.tier_no,
            location.cell_no,
        )
    ):
        return os_location_code(
            row=location.row_no,
            section=location.section_no,
            tier=location.tier_no,
            cell=location.cell_no,
        )
    return _normalize(location.location_code) or _zone_code(location)


def _location_label(location: WarehouseLocation) -> str:
    return _normalize(location.display_name) or _location_scan_code(location)


def _fbs_storage_cell_code(location: WarehouseLocation) -> str:
    coordinates = (
        int(location.row_no or 0),
        int(location.section_no or 0),
        int(location.tier_no or 0),
        int(location.cell_no or 0),
    )
    if _zone_code(location) == "OS" and all(value > 0 for value in coordinates):
        return "FBS@{}".format(
            os_location_code(
                row=coordinates[0],
                section=coordinates[1],
                tier=coordinates[2],
                cell=coordinates[3],
            )
        )
    return f"FBS@LOCATION-{int(location.id)}"


def _is_shared_named_os_location(location: WarehouseLocation) -> bool:
    """A configured inter-row OS place may hold its configured pallet count."""
    return bool(
        _zone_code(location) == "OS"
        and location.is_storage
        and location.is_fbs_visible
        and not location.is_topology_visible
    )


def _active_pallet_queryset():
    return FbsPallet.objects.filter(status__in=ACTIVE_PALLET_STATUSES).select_related(
        "agency",
        "cell__location",
        "warehouse_container",
        "warehouse_container__current_location",
    )


def _resolve_pallet_scan(scan_value: str) -> tuple[list[FbsPallet], bool]:
    normalized = _normalize(scan_value)
    if not normalized:
        return [], False
    direct = list(
        _active_pallet_queryset()
        .filter(pallet_code__iexact=normalized)
        .order_by("id")[:3]
    )
    if direct:
        return direct, len(direct) > 1

    matches = []
    for pallet in _active_pallet_queryset().order_by("id")[:500]:
        if _same_pallet_code_scan(normalized, pallet.pallet_code):
            matches.append(pallet)
            if len(matches) > 1:
                break
    return matches, len(matches) > 1


def _active_free_operation(pallet: FbsPallet, *, exclude_operation_id: int | None = None):
    queryset = WarehouseOperation.objects.filter(
        operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
        context_type=FBS_FREE_RELOCATION_CONTEXT,
        context_id=str(pallet.id),
        agency_id=pallet.agency_id,
        status=WarehouseOperation.STATUS_IN_PROGRESS,
    )
    if exclude_operation_id:
        queryset = queryset.exclude(pk=int(exclude_operation_id))
    return queryset.order_by("-id").first()


def active_relocation_for_pallet_scan(scan_value: str):
    pallets, ambiguous = _resolve_pallet_scan(scan_value)
    if ambiguous or len(pallets) != 1:
        return None
    pallet = pallets[0]
    operation = _active_free_operation(pallet)
    if operation is None:
        return None
    operation.fbs_pallet_code = pallet.pallet_code
    return operation


def _active_boxes(pallet: FbsPallet):
    return FbsBox.objects.filter(
        pallet=pallet,
        status__in=ACTIVE_BOX_STATUSES,
    ).select_related("source_container", "source_container__current_location")


def _active_balances(pallet: FbsPallet):
    return FbsStockBalance.objects.filter(
        box__pallet=pallet,
        box__status__in=ACTIVE_BOX_STATUSES,
        qty__gt=0,
    ).select_related("box", "sku_ref")


def _inventory_blocks_pallet(
    pallet: FbsPallet,
    *,
    box_ids: list[int],
    sku_ids: list[int],
) -> bool:
    return FbsStorageLock.objects.filter(is_active=True).filter(
        Q(scope_type=FbsInventorySession.SCOPE_ALL)
        | Q(scope_type=FbsInventorySession.SCOPE_AGENCY, agency_id=pallet.agency_id)
        | Q(scope_type=FbsInventorySession.SCOPE_CELL, cell_id=pallet.cell_id)
        | Q(scope_type=FbsInventorySession.SCOPE_PALLET, pallet_id=pallet.id)
        | Q(scope_type=FbsInventorySession.SCOPE_BOX, box_id__in=box_ids)
        | Q(
            scope_type=FbsInventorySession.SCOPE_SKU,
            agency_id=pallet.agency_id,
            sku_id__in=sku_ids,
        )
    ).exists()


def _linked_general_snapshots(
    *,
    container_ids: list[int],
    container_codes: list[str],
):
    query = Q()
    if container_ids:
        query |= Q(container_id__in=container_ids) | Q(parent_container_id__in=container_ids)
    if container_codes:
        query |= Q(container_code__in=container_codes)
    if not query:
        return WarehouseStockSnapshot.objects.none()
    return WarehouseStockSnapshot.objects.filter(
        query,
        is_archived=False,
        qty__gt=0,
    )


def _pallet_blockers(
    pallet: FbsPallet,
    *,
    exclude_operation_id: int | None = None,
    source_location: WarehouseLocation | None = None,
    allow_in_transit: bool = False,
    allowed_box_location_ids: set[int] | None = None,
    lock_reservations: bool = False,
) -> list[str]:
    blockers: list[str] = []
    location = source_location or _physical_location(pallet)
    source_zone = _zone_code(location)
    coordinates = (
        int(location.row_no or 0),
        int(location.section_no or 0),
        int(location.tier_no or 0),
        int(location.cell_no or 0),
    )
    if not location.is_active:
        blockers.append("Текущее место FBS-паллеты выключено.")
    elif source_zone not in FBS_FREE_SOURCE_ZONES:
        blockers.append("Свободное перемещение FBS разрешено только из OS, PR или OTG.")
    elif source_zone == "OS" and (
        not location.is_storage
        or (
            location.is_topology_visible
            and not all(value > 0 for value in coordinates)
        )
        or (
            not location.is_topology_visible
            and not location.is_fbs_visible
        )
    ):
        blockers.append("FBS-паллета должна находиться в действующей ячейке хранения OS.")
    if not pallet.cell.is_active:
        blockers.append("FBS-ячейка паллеты выключена.")
    if pallet.warehouse_container_id is None:
        blockers.append("У FBS-паллеты отсутствует физический складской контейнер.")

    boxes = list(_active_boxes(pallet).order_by("id"))
    allowed_location_ids = (
        {int(value) for value in allowed_box_location_ids if value}
        if allowed_box_location_ids is not None
        else ({None, int(location.id)} if allow_in_transit else {int(location.id)})
    )
    mismatched_box_codes = [
        box.box_code
        for box in boxes
        if box.source_container_id
        and box.source_container.current_location_id not in allowed_location_ids
    ]
    if mismatched_box_codes:
        blockers.append(
            "Короба FBS-паллеты находятся в другом месте: "
            + ", ".join(mismatched_box_codes[:5])
        )
    balances = list(_active_balances(pallet).order_by("id"))
    if _active_free_operation(pallet, exclude_operation_id=exclude_operation_id):
        blockers.append("FBS-паллета уже находится в свободном перемещении.")
    reservation_state = relocation_reservation_state(
        [balance.id for balance in balances],
        lock=lock_reservations,
    )
    blockers.extend(reservation_state.blockers)

    box_ids = [int(box.id) for box in boxes]
    sku_ids = sorted(
        {
            int(balance.sku_ref_id)
            for balance in balances
            if balance.sku_ref_id
        }
    )
    if _inventory_blocks_pallet(pallet, box_ids=box_ids, sku_ids=sku_ids):
        blockers.append("Инвентаризация блокирует эту FBS-паллету.")
    if FbsInternalMovement.objects.filter(
        Q(source_box_id__in=box_ids)
        | Q(target_box_id__in=box_ids)
        | Q(target_pallet=pallet),
        status__in=ACTIVE_INTERNAL_MOVEMENT_STATUSES,
    ).exists():
        blockers.append("У FBS-паллеты есть внутреннее перемещение.")
    if FbsReplenishmentPlan.objects.filter(
        target_pallet=pallet,
        status__in=ACTIVE_REPLENISHMENT_STATUSES,
    ).exists():
        blockers.append("FBS-паллета участвует в действующем плане пополнения.")
    if FbsReplenishmentAllocation.objects.filter(
        target_box_id__in=box_ids,
        status__in=ACTIVE_REPLENISHMENT_ALLOCATION_STATUSES,
    ).exists():
        blockers.append("На FBS-паллету выполняется пополнение.")

    box_codes = [_normalize(box.box_code) for box in boxes if _normalize(box.box_code)]
    if PalletLock.objects.filter(
        agency_id=pallet.agency_id,
        pallet_code__iexact=pallet.pallet_code,
        status=PalletLock.STATUS_ACTIVE,
    ).exclude(move_task__status__in=(
        MoveTask.STATUS_DONE,
        MoveTask.STATUS_CANCELED,
        MoveTask.STATUS_FAILED,
    )).exists():
        blockers.append("FBS-паллета уже взята в задание ричтрака.")
    if box_codes and BoxClaim.objects.filter(
        agency_id=pallet.agency_id,
        box_code__in=box_codes,
        status=BoxClaim.STATUS_CLAIMED,
    ).exclude(move_task__status__in=(
        MoveTask.STATUS_DONE,
        MoveTask.STATUS_CANCELED,
        MoveTask.STATUS_FAILED,
    )).exists():
        blockers.append("Один из FBS-коробов уже взят в задание ричтрака.")
    if MoveTask.objects.filter(
        request__agency_id=pallet.agency_id,
        status__in=ACTIVE_TASK_STATUSES,
    ).filter(
        Q(pallet_code__iexact=pallet.pallet_code)
        | Q(payload__pallet_code=pallet.pallet_code)
    ).exists():
        blockers.append("Для FBS-паллеты уже существует активное задание ричтрака.")

    container_ids = []
    container_codes = []
    if pallet.warehouse_container_id:
        container_ids.append(int(pallet.warehouse_container_id))
        container_codes.append(_normalize(pallet.warehouse_container.container_code))
    for box in boxes:
        if box.source_container_id:
            container_ids.append(int(box.source_container_id))
            container_codes.append(_normalize(box.source_container.container_code))
    container_codes = [code for code in container_codes if code]
    if _linked_general_snapshots(
        container_ids=container_ids,
        container_codes=container_codes,
    ).exists():
        blockers.append(
            "Паллета ещё связана с незавершённым остатком общего склада. "
            "Сначала завершите исходное FBS-перемещение."
        )
    if container_ids and WarehouseOperationTask.objects.filter(
        container_id__in=container_ids,
        status__in=(
            WarehouseOperationTask.STATUS_CREATED,
            WarehouseOperationTask.STATUS_IN_PROGRESS,
        ),
    ).exclude(operation_id=exclude_operation_id).exists():
        blockers.append("У контейнеров FBS-паллеты есть активная складская операция.")

    return list(dict.fromkeys(blockers))


def _balance_lines(pallet: FbsPallet) -> list[dict]:
    grouped: dict[tuple[str, ...], dict] = {}
    for balance in _active_balances(pallet).order_by("box__box_code", "id"):
        key = (
            _normalize(balance.sku_code),
            _normalize(balance.name),
            _normalize(balance.size),
            _normalize(balance.barcode),
            _normalize(balance.goods_type),
        )
        row = grouped.setdefault(
            key,
            {
                "sku": key[0] or "-",
                "name": key[1],
                "size": key[2],
                "barcode": key[3],
                "goods_type": key[4],
                "qty": 0,
                "boxes": [],
            },
        )
        row["qty"] += int(balance.qty or 0)
        box_code = _normalize(balance.box.box_code)
        if box_code and box_code not in row["boxes"]:
            row["boxes"].append(box_code)
    return list(grouped.values())


def inspect_fbs_pallet(scan_value: str) -> dict:
    normalized = _normalize(scan_value)
    pallets, ambiguous = _resolve_pallet_scan(normalized)
    if ambiguous:
        return {
            "ok": False,
            "found": True,
            "object_type": "pallet",
            "stock_contour": "fbs",
            "title": "FBS-паллета требует проверки",
            "code": normalized,
            "pallet_code": normalized,
            "can_move": False,
            "blockers": [
                "Один QR найден у нескольких клиентов FBS. Требуется ручная проверка."
            ],
            "summary": [],
            "lines": [],
            "placements": [],
        }
    if not pallets:
        return {"ok": False, "found": False, "object_type": "pallet"}

    pallet = pallets[0]
    location = _physical_location(pallet)
    lines = _balance_lines(pallet)
    total_qty = sum(int(row["qty"] or 0) for row in lines)
    box_count = _active_boxes(pallet).count()
    blockers = _pallet_blockers(pallet)
    location_code = _location_scan_code(location)
    location_label = _location_label(location)
    return {
        "ok": True,
        "found": True,
        "object_type": "pallet",
        "stock_contour": "fbs",
        "move_kind": "fbs",
        "title": f"FBS-паллета {pallet.pallet_code}",
        "code": pallet.pallet_code,
        "pallet_code": pallet.pallet_code,
        "fbs_pallet_id": int(pallet.id),
        "agency_id": int(pallet.agency_id),
        "agency_name": str(pallet.agency),
        "location_id": int(location.id),
        "location_zone_code": _zone_code(location),
        "location_label": location_label,
        "location_scan_code": location_code,
        "total_qty": total_qty,
        "box_count": box_count,
        "contexts": ["Контур FBS", f"Текущее место: {location_label}"],
        "lines": lines,
        "placements": [
            {
                "location": location_label,
                "qty": total_qty,
                "pallet_code": pallet.pallet_code,
                "box_code": "",
                "state_label": "FBS",
            }
        ],
        "blockers": blockers,
        "is_free": not blockers,
        "can_move": not blockers,
        "status_label": "Свободна" if not blockers else "Не свободна",
        "status_kind": "free" if not blockers else "blocked",
        "summary": [
            {"label": "Контур", "value": "FBS"},
            {"label": "Клиент", "value": str(pallet.agency)},
            {"label": "QR места", "value": location_code},
            {"label": "Ярус", "value": int(location.tier_no or 0)},
            {"label": "Количество", "value": f"{total_qty} шт."},
            {"label": "Короба", "value": box_count},
        ],
    }


def _locked_pallet(pallet_id: int) -> FbsPallet:
    return (
        FbsPallet.objects.select_for_update(of=("self",))
        .select_related(
            "agency",
            "cell__location",
            "warehouse_container",
            "warehouse_container__current_location",
        )
        .get(pk=int(pallet_id), status__in=ACTIVE_PALLET_STATUSES)
    )


def _delete_unused_transit_cell(cell_id: int) -> None:
    cell = (
        FbsStorageCell.objects.select_related("location")
        .filter(pk=int(cell_id or 0), cell_code__startswith="FBS@PLAN-")
        .first()
    )
    if cell is None or cell.pallets.exists():
        return
    location = cell.location
    if location.zone_kind != WarehouseLocation.ZONE_KIND_VIRTUAL:
        return
    cell.delete()
    location.delete()


def _group_pallet_from_scan(scan_value: str) -> tuple[int, int | None, int | None, str]:
    """Resolve a logical FBS pallet from a box, FBS pallet, or physical pallet QR."""

    normalized = _normalize(scan_value)
    if not normalized:
        raise FbsMovementError("Отсканируйте FBS-короб или паллету.")

    box_queryset = FbsBox.objects.filter(status__in=ACTIVE_BOX_STATUSES).order_by("id")
    boxes = list(box_queryset.filter(box_code__iexact=normalized)[:3])
    if not boxes:
        boxes = []
        for box in box_queryset[:5000]:
            if _same_box_code_scan(normalized, box.box_code):
                boxes.append(box)
                if len(boxes) > 1:
                    break
    if len(boxes) > 1:
        raise FbsMovementError(
            "QR короба найден у нескольких клиентов. Требуется ручная проверка."
        )
    if boxes:
        box = boxes[0]
        return int(box.pallet_id), int(box.id), None, "box"

    pallets, ambiguous = _resolve_pallet_scan(normalized)
    if ambiguous:
        raise FbsMovementError(
            "QR FBS-паллеты найден у нескольких клиентов. Требуется ручная проверка."
        )
    if pallets:
        return int(pallets[0].id), None, None, "fbs_pallet"

    physical_queryset = WarehouseContainer.objects.filter(
        container_type__in=(
            WarehouseContainer.TYPE_PALLET,
            WarehouseContainer.TYPE_MIXED_PALLET,
        ),
        status=WarehouseContainer.STATUS_ACTIVE,
    ).order_by("id")
    physical_pallets = list(
        physical_queryset.filter(container_code__iexact=normalized)[:3]
    )
    if not physical_pallets:
        physical_pallets = []
        for container in physical_queryset[:5000]:
            if _same_pallet_code_scan(normalized, container.container_code):
                physical_pallets.append(container)
                if len(physical_pallets) > 1:
                    break
    if len(physical_pallets) > 1:
        raise FbsMovementError(
            "QR физической паллеты найден у нескольких клиентов. "
            "Требуется ручная проверка."
        )
    if not physical_pallets:
        raise FbsMovementError("Активный FBS-короб или паллета по этому QR не найдены.")

    physical_pallet = physical_pallets[0]
    pallet_ids = list(
        FbsBox.objects.filter(
            source_container__parent_container=physical_pallet,
            status__in=ACTIVE_BOX_STATUSES,
        )
        .order_by()
        .values_list("pallet_id", flat=True)
        .distinct()
    )
    if not pallet_ids:
        raise FbsMovementError("На отсканированной паллете нет активных FBS-коробов.")
    if len(pallet_ids) > 1:
        raise FbsMovementError(
            "На физической паллете находятся короба разных FBS-паллет. "
            "Групповое размещение запрещено."
        )
    return int(pallet_ids[0]), None, int(physical_pallet.id), "physical_pallet"


@transaction.atomic
def place_fbs_pallet_box_group(
    *,
    source_scan: str,
    destination_location_id: int,
    performed_by=None,
    performed_by_role: str = "picker",
) -> WarehouseOperation:
    """Atomically place every box of one logical FBS pallet into one OS address.

    The source QR can belong to any box, the logical FBS pallet, or the physical
    pallet carrying its boxes.  Boxes that already reached the destination are
    consolidated into the same FBS pallet instead of blocking the remaining
    group.
    """

    _require_writes()
    pallet_id, scanned_box_id, scanned_parent_id, scan_kind = _group_pallet_from_scan(
        source_scan
    )
    pallet = _locked_pallet(pallet_id)
    destination = (
        WarehouseLocation.objects.select_for_update()
        .filter(pk=int(destination_location_id), is_active=True)
        .first()
    )
    if destination is None:
        raise FbsMovementError("Конечное место не найдено или выключено.")
    if _zone_code(destination) != "OS":
        raise FbsMovementError(
            "Групповое размещение FBS-паллеты разрешено только в точную ячейку OS."
        )
    try:
        require_concrete_movement_location(
            destination,
            purpose="группового размещения FBS-паллеты",
        )
    except ValidationError as exc:
        raise FbsMovementError("; ".join(exc.messages)) from exc
    if (
        not destination.is_storage
        or min(
            int(destination.row_no or 0),
            int(destination.section_no or 0),
            int(destination.tier_no or 0),
            int(destination.cell_no or 0),
        )
        <= 0
    ):
        raise FbsMovementError(
            "Для группового размещения отсканируйте точную активную ячейку OS."
        )

    boxes = list(
        _active_boxes(pallet)
        .select_for_update(of=("self",))
        .order_by("id")
    )
    if not boxes:
        raise FbsMovementError("На FBS-паллете нет активных коробов.")
    if scanned_box_id and all(int(box.id) != int(scanned_box_id) for box in boxes):
        raise FbsMovementError("Отсканированный короб больше не относится к этой паллете.")
    if len(boxes) > int(pallet.max_boxes or 0):
        raise FbsMovementError(
            "Количество коробов превышает ёмкость FBS-паллеты. "
            "Требуется ручная проверка."
        )
    if any(box.source_container_id is None for box in boxes):
        raise FbsMovementError(
            "У одного из FBS-коробов отсутствует физический складской контейнер."
        )

    pallet_container = WarehouseContainer.objects.select_for_update().get(
        pk=pallet.warehouse_container_id
    )
    box_container_ids = [int(box.source_container_id) for box in boxes]
    box_containers = {
        int(container.id): container
        for container in WarehouseContainer.objects.select_for_update(of=("self",))
        .select_related("current_location", "parent_container__current_location")
        .filter(id__in=box_container_ids)
        .order_by("id")
    }
    if len(box_containers) != len(box_container_ids):
        raise FbsMovementError("Не все физические короба FBS найдены на складе.")
    if any(
        container.status != WarehouseContainer.STATUS_ACTIVE
        or int(container.agency_id) != int(pallet.agency_id)
        for container in box_containers.values()
    ):
        raise FbsMovementError(
            "Один из физических коробов выключен или принадлежит другому клиенту."
        )

    already_at_destination = [
        box
        for box in boxes
        if int(box_containers[int(box.source_container_id)].current_location_id or 0)
        == int(destination.id)
    ]
    moving_boxes = [box for box in boxes if box not in already_at_destination]
    if not moving_boxes:
        raise FbsMovementError("Все короба паллеты уже находятся в этом месте.")

    source_parent_ids = {
        int(box_containers[int(box.source_container_id)].parent_container_id or 0)
        for box in moving_boxes
    }
    if 0 in source_parent_ids or len(source_parent_ids) != 1:
        raise FbsMovementError(
            "Короба паллеты находятся на разных физических паллетах или без паллеты. "
            "Групповое размещение запрещено."
        )
    source_parent_id = source_parent_ids.pop()
    if scanned_parent_id and int(scanned_parent_id) != source_parent_id:
        raise FbsMovementError(
            "Состав отсканированной физической паллеты изменился. "
            "Отсканируйте её повторно."
        )
    source_parent = (
        WarehouseContainer.objects.select_for_update(of=("self",))
        .select_related("current_location")
        .get(pk=source_parent_id)
    )
    source = source_parent.current_location
    if (
        source is None
        or not source.is_active
        or _zone_code(source) not in FBS_FREE_SOURCE_ZONES
    ):
        raise FbsMovementError(
            "У исходной физической паллеты нет действующего места OS, PR или ОТГ."
        )
    if source.warehouse_code != destination.warehouse_code:
        raise FbsMovementError("Перемещение между разными складами запрещено.")
    if (
        source_parent.status != WarehouseContainer.STATUS_ACTIVE
        or source_parent.container_type
        not in (
            WarehouseContainer.TYPE_PALLET,
            WarehouseContainer.TYPE_MIXED_PALLET,
        )
        or int(source_parent.agency_id) != int(pallet.agency_id)
    ):
        raise FbsMovementError(
            "Исходная физическая паллета выключена или принадлежит другому клиенту."
        )

    moving_container_ids = {
        int(box.source_container_id) for box in moving_boxes
    }
    if any(
        int(box_containers[int(box.source_container_id)].current_location_id or 0)
        != int(source.id)
        or int(box_containers[int(box.source_container_id)].parent_container_id or 0)
        != int(source_parent.id)
        for box in moving_boxes
    ):
        raise FbsMovementError(
            "Физическое место одного из коробов не совпадает с местом паллеты."
        )
    if any(
        box_containers[int(box.source_container_id)].parent_container_id
        not in (None, int(pallet_container.id))
        for box in already_at_destination
    ):
        raise FbsMovementError(
            "Короб на конечном адресе привязан к другой паллете. "
            "Требуется ручная проверка."
        )

    extra_children = list(
        WarehouseContainer.objects.select_for_update()
        .filter(
            parent_container=source_parent,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        .exclude(id__in=moving_container_ids)
        .order_by("container_code")
        .values_list("container_code", flat=True)[:4]
    )
    if extra_children:
        raise FbsMovementError(
            "На исходной физической паллете есть другие контейнеры: "
            + ", ".join(extra_children)
            + ". Групповое размещение запрещено."
        )

    source_snapshot_query = (
        Q(container=source_parent)
        | Q(parent_container=source_parent)
        | Q(container_code__iexact=source_parent.container_code)
    )
    if WarehouseStockSnapshot.objects.filter(
        source_snapshot_query,
        is_archived=False,
        qty__gt=0,
    ).exclude(warehouse_state_code__in=NON_OCCUPYING_STOCK_STATES).exists():
        raise FbsMovementError(
            "Исходная паллета ещё связана с остатком общего склада. "
            "Сначала завершите исходное перемещение."
        )
    if WarehouseOperationTask.objects.filter(
        container=source_parent,
        status__in=(
            WarehouseOperationTask.STATUS_CREATED,
            WarehouseOperationTask.STATUS_IN_PROGRESS,
        ),
    ).exists():
        raise FbsMovementError(
            "У исходной физической паллеты есть активная складская операция."
        )
    if PalletLock.objects.filter(
        agency_id=pallet.agency_id,
        pallet_code__iexact=source_parent.container_code,
        status=PalletLock.STATUS_ACTIVE,
    ).exclude(
        move_task__status__in=(
            MoveTask.STATUS_DONE,
            MoveTask.STATUS_CANCELED,
            MoveTask.STATUS_FAILED,
        )
    ).exists():
        raise FbsMovementError("Исходная паллета уже взята в задание ричтрака.")
    if MoveTask.objects.filter(
        request__agency_id=pallet.agency_id,
        status__in=(MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS),
    ).filter(
        Q(pallet_code__iexact=source_parent.container_code)
        | Q(payload__pallet_code=source_parent.container_code)
    ).exists():
        raise FbsMovementError(
            "Для исходной паллеты уже существует активное задание ричтрака."
        )
    if active_reserved_pallet_codes_for_agency(
        int(pallet.agency_id), [source_parent.container_code]
    ):
        raise FbsMovementError("Исходная паллета зарезервирована под отгрузку.")

    blockers = _pallet_blockers(
        pallet,
        source_location=source,
        allowed_box_location_ids={int(source.id), int(destination.id)},
        lock_reservations=True,
    )
    if blockers:
        raise FbsMovementError("; ".join(blockers))

    destination_cell, _created = FbsStorageCell.objects.get_or_create(
        location=destination,
        defaults={
            "cell_code": f"FBS@{os_location_code(row=destination.row_no, section=destination.section_no, tier=destination.tier_no, cell=destination.cell_no)}",
            "purpose": FbsStorageCell.PURPOSE_FLEX,
            "client_cluster": pallet.agency_id,
            "is_active": True,
        },
    )
    destination_cell = FbsStorageCell.objects.select_for_update().get(
        pk=destination_cell.pk
    )
    if not destination_cell.is_active:
        raise FbsMovementError("Выбранная ячейка выключена для FBS.")
    destination_pallet = (
        FbsPallet.objects.select_for_update(of=("self",))
        .filter(cell=destination_cell, status__in=ACTIVE_PALLET_STATUSES)
        .exclude(pk=pallet.pk)
        .order_by("id")
        .first()
    )
    if destination_pallet is not None:
        from .storage import release_unplaced_os_reservation

        if release_unplaced_os_reservation(pallet=destination_pallet):
            destination_pallet = None
    if destination_pallet is not None:
        raise FbsMovementError("В выбранной ячейке уже находится FBS-паллета.")

    allowed_destination_ids = {
        int(pallet_container.id),
        *[int(box.source_container_id) for box in already_at_destination],
    }
    allowed_destination_codes = [
        box_containers[container_id].container_code
        for container_id in allowed_destination_ids
        if container_id in box_containers
    ]
    allowed_destination_codes.append(pallet_container.container_code)
    destination_snapshots = WarehouseStockSnapshot.objects.filter(
        location=destination,
        zone_code__iexact="OS",
        is_archived=False,
        qty__gt=0,
    ).exclude(warehouse_state_code__in=NON_OCCUPYING_STOCK_STATES)
    destination_snapshots = destination_snapshots.exclude(
        Q(container_id__in=allowed_destination_ids)
        | Q(parent_container_id__in=allowed_destination_ids)
        | Q(container_code__in=allowed_destination_codes)
    )
    if destination_snapshots.exists():
        raise FbsMovementError("Выбранная ячейка уже занята складским остатком.")
    destination_containers = list(
        active_os_physical_containers()
        .select_for_update(of=("self",))
        .filter(current_location=destination)
        .exclude(id__in=allowed_destination_ids)
        .select_related("parent_container")
        .order_by("container_code")
    )
    if destination_containers:
        codes: list[str] = []
        for container in destination_containers:
            code = _normalize(
                getattr(container.parent_container, "container_code", "")
                or container.container_code
            )
            if code and code not in codes:
                codes.append(code)
        suffix = ", ".join(codes[:3]) if codes else "неизвестным контейнером"
        raise FbsMovementError(f"Выбранная ячейка уже занята: {suffix}.")

    balances = list(
        _active_balances(pallet).select_for_update(of=("self",)).order_by("id")
    )
    total_qty = sum(int(balance.qty or 0) for balance in balances)
    moving_box_ids = {int(box.id) for box in moving_boxes}
    moved_qty = sum(
        int(balance.qty or 0)
        for balance in balances
        if int(balance.box_id) in moving_box_ids
    )
    actor = _authenticated_user(performed_by)
    actor_role = _normalize(performed_by_role) or "picker"
    now = timezone.now()
    previous_cell_id = int(pallet.cell_id)
    operation = WarehouseOperation.objects.create(
        agency=pallet.agency,
        operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
        context_type=FBS_FREE_RELOCATION_CONTEXT,
        context_id=str(pallet.id),
        source_document_type="fbs_pallet",
        source_document_id=str(pallet.id),
        source_location=source,
        source_zone_code=_zone_code(source),
        destination_location=destination,
        destination_zone_code="OS",
        status=WarehouseOperation.STATUS_DONE,
        requested_by=actor,
        requested_by_role=actor_role,
        assigned_executor_role=actor_role,
        planned_qty=moved_qty,
        done_qty=moved_qty,
        started_at=now,
        completed_at=now,
        comment=f"Групповое размещение FBS-паллеты {pallet.pallet_code}",
    )
    task = WarehouseOperationTask.objects.create(
        operation=operation,
        task_type=WarehouseOperationTask.TYPE_PALLET_MOVE,
        container=pallet_container,
        from_location=source,
        to_location=destination,
        from_zone_code=_zone_code(source),
        to_zone_code="OS",
        qty_planned=moved_qty,
        qty_done=moved_qty,
        status=WarehouseOperationTask.STATUS_DONE,
        assigned_to=actor,
        assigned_to_name=(
            str(actor.get_full_name() or actor.get_username()).strip() if actor else ""
        ),
        executor_role=actor_role,
        payload={
            "free_move": True,
            "fbs_group_putaway": True,
            "scan_kind": scan_kind,
            "fbs_pallet_id": int(pallet.id),
            "fbs_pallet_code": pallet.pallet_code,
            "source_physical_pallet_id": int(source_parent.id),
            "source_physical_pallet_code": source_parent.container_code,
            "source_location_id": int(source.id),
            "destination_location_id": int(destination.id),
            "destination_location_code": destination.location_code,
            "box_count": len(boxes),
            "moved_box_count": len(moving_boxes),
            "already_at_destination_count": len(already_at_destination),
            "total_pallet_qty": total_qty,
        },
        started_at=now,
        completed_at=now,
    )

    pallet.cell = destination_cell
    pallet.status = FbsPallet.STATUS_ACTIVE
    pallet.full_clean()
    pallet.save(update_fields=["cell", "status", "updated_at"])
    pallet_container.current_location = destination
    pallet_container.status = WarehouseContainer.STATUS_ACTIVE
    pallet_container.source_context_type = FBS_STORAGE_CONTEXT_TYPE
    pallet_container.source_context_id = pallet.pallet_code
    pallet_container.save(
        update_fields=[
            "current_location",
            "status",
            "source_context_type",
            "source_context_id",
            "updated_at",
        ]
    )
    WarehouseContainer.objects.filter(id__in=box_container_ids).update(
        current_location=destination,
        parent_container=pallet_container,
        status=WarehouseContainer.STATUS_ACTIVE,
        updated_at=now,
    )

    WarehouseEvent.objects.create(
        agency=pallet.agency,
        event_type="fbs_pallet_boxes_group_relocation_completed",
        stock_context_type="fbs_pallet",
        stock_context_id=str(pallet.id),
        container=pallet_container,
        operation=operation,
        operation_task=task,
        source_document_type="fbs_pallet",
        source_document_id=str(pallet.id),
        from_location=source,
        to_location=destination,
        from_zone_code=_zone_code(source),
        to_zone_code="OS",
        qty=moved_qty,
        payload={
            **task.payload,
            "moved_box_codes": [box.box_code for box in moving_boxes],
            "already_at_destination_codes": [
                box.box_code for box in already_at_destination
            ],
        },
        performed_by=actor,
        performed_by_role=actor_role,
        occurred_at=now,
    )

    from .picking import resort_queued_pick_batches_for_balances

    moved_balance_ids = tuple(int(balance.id) for balance in balances)
    transaction.on_commit(
        lambda balance_ids=moved_balance_ids: (
            resort_queued_pick_batches_for_balances(balance_ids)
        )
    )
    _delete_unused_transit_cell(previous_cell_id)
    return operation


@transaction.atomic
def start_fbs_free_relocation(
    *,
    pallet_id: int,
    performed_by=None,
    expected_location_id: int | None = None,
    performed_by_role: str = "reachtruck",
) -> WarehouseOperation:
    _require_writes()
    pallet = _locked_pallet(pallet_id)
    source = _physical_location(pallet)
    source_zone = _zone_code(source)
    if expected_location_id and int(source.id) != int(expected_location_id):
        raise FbsMovementError("Адрес FBS-паллеты изменился. Отсканируйте её повторно.")
    boxes = list(_active_boxes(pallet).select_for_update(of=("self",)).order_by("id"))
    pallet_container = WarehouseContainer.objects.select_for_update().get(
        pk=pallet.warehouse_container_id
    )
    box_container_ids = [
        int(box.source_container_id)
        for box in boxes
        if box.source_container_id
    ]
    if box_container_ids:
        list(
            WarehouseContainer.objects.select_for_update()
            .filter(id__in=box_container_ids)
            .values_list("id", flat=True)
        )
    balances = list(
        _active_balances(pallet).select_for_update(of=("self",)).order_by("id")
    )
    blockers = _pallet_blockers(pallet, lock_reservations=True)
    if blockers:
        raise FbsMovementError("; ".join(blockers))

    total_qty = sum(int(balance.qty or 0) for balance in balances)
    actor = _authenticated_user(performed_by)
    actor_role = _normalize(performed_by_role) or "reachtruck"
    now = timezone.now()
    operation = WarehouseOperation.objects.create(
        agency=pallet.agency,
        operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
        context_type=FBS_FREE_RELOCATION_CONTEXT,
        context_id=str(pallet.id),
        source_document_type="fbs_pallet",
        source_document_id=str(pallet.id),
        source_location=source,
        source_zone_code=source_zone,
        status=WarehouseOperation.STATUS_IN_PROGRESS,
        requested_by=actor,
        requested_by_role=actor_role,
        assigned_executor_role=actor_role,
        planned_qty=total_qty,
        started_at=now,
        comment=f"Свободное перемещение FBS-паллеты {pallet.pallet_code}",
    )
    source_cell_id = int(pallet.cell_id)
    task = WarehouseOperationTask.objects.create(
        operation=operation,
        task_type=WarehouseOperationTask.TYPE_PALLET_MOVE,
        container=pallet_container,
        from_location=source,
        from_zone_code=source_zone,
        qty_planned=total_qty,
        status=WarehouseOperationTask.STATUS_IN_PROGRESS,
        assigned_to=actor,
        assigned_to_name=(
            str(actor.get_full_name() or actor.get_username()).strip() if actor else ""
        ),
        executor_role=actor_role,
        payload={
            "free_move": True,
            "fbs_free_move": True,
            "fbs_pallet_id": int(pallet.id),
            "fbs_pallet_code": pallet.pallet_code,
            "source_cell_id": source_cell_id,
            "source_location_id": int(source.id),
            "source_zone_code": source_zone,
        },
        started_at=now,
    )
    WarehouseEvent.objects.create(
        agency=pallet.agency,
        event_type="fbs_free_relocation_started",
        stock_context_type="fbs_pallet",
        stock_context_id=str(pallet.id),
        container=pallet_container,
        operation=operation,
        operation_task=task,
        source_document_type="fbs_pallet",
        source_document_id=str(pallet.id),
        from_location=source,
        to_location=None,
        from_zone_code=source_zone,
        to_zone_code=source_zone,
        qty=total_qty,
        payload={
            "free_move": True,
            "fbs_free_move": True,
            "fbs_pallet_code": pallet.pallet_code,
            "source_cell_id": source_cell_id,
            "source_location_id": int(source.id),
            "source_zone_code": source_zone,
        },
        performed_by=actor,
        performed_by_role=actor_role,
        occurred_at=now,
    )

    # The pallet QR is the physical pickup fact.  From this moment the pallet
    # and all of its boxes are in transit, so the source rack place must be
    # available immediately.  Keep the original cell on the task for an
    # explicit cancellation/return and move the FBS relation out of OS.
    from .storage import _create_virtual_planning_cell

    transit_cell = _create_virtual_planning_cell(agency=pallet.agency)
    pallet.cell = transit_cell
    pallet.full_clean()
    pallet.save(update_fields=["cell", "updated_at"])
    pallet_container.current_location = None
    pallet_container.save(update_fields=["current_location", "updated_at"])
    if box_container_ids:
        WarehouseContainer.objects.filter(id__in=box_container_ids).update(
            current_location=None,
            updated_at=now,
        )
    return operation


def _destination_location(
    *,
    source: WarehouseLocation,
    destination_zone_code: str,
    row_no: int,
    section_no: int,
    tier_no: int,
    cell_no: int,
    destination_location_id: int | None = None,
) -> WarehouseLocation:
    destination_zone = _normalize(destination_zone_code).upper()
    queryset = WarehouseLocation.objects.select_for_update().filter(
        warehouse_code=source.warehouse_code,
        zone_code__iexact=destination_zone,
        is_active=True,
    )
    if destination_location_id:
        destination = queryset.filter(pk=int(destination_location_id)).first()
    elif destination_zone == "OS":
        destination = queryset.filter(
            row_no=max(int(row_no or 0), 0),
            section_no=max(int(section_no or 0), 0),
            tier_no=max(int(tier_no or 0), 0),
            cell_no=max(int(cell_no or 0), 0),
            is_storage=True,
        ).first()
    else:
        raise FbsMovementError(
            f"Для зоны {destination_zone} отсканируйте конкретное место с QR."
        )
    if destination is None:
        if destination_zone == "OS":
            raise FbsMovementError(
                "Ячейка не найдена в действующей топологии OS или выключена."
            )
        raise FbsMovementError(
            f"Зона {destination_zone} не найдена в действующей топологии склада."
        )
    try:
        destination = require_concrete_movement_location(
            destination,
            purpose="свободного перемещения FBS",
        )
        if destination_zone in {"PR", "OTG"}:
            validate_operational_location(
                destination,
                expected_zone=destination_zone,
                require_fbs=True,
                required_slots=1,
            )
        return destination
    except ValidationError as exc:
        raise FbsMovementError("; ".join(exc.messages)) from exc


def _validate_destination_client_mix(
    *,
    destination: WarehouseLocation,
    pallet: FbsPallet,
) -> None:
    destination_zone = _zone_code(destination)
    if destination_zone not in {"PR", "OTG"} and not _is_shared_named_os_location(
        destination
    ):
        return
    if destination.allow_mixed_client_pallets:
        return
    has_other_client = (
        FbsPallet.objects.select_for_update(of=("self",))
        .filter(
            warehouse_container__current_location=destination,
            status__in=ACTIVE_PALLET_STATUSES,
        )
        .exclude(pk=pallet.pk)
        .exclude(agency_id=pallet.agency_id)
        .exists()
    )
    if has_other_client:
        raise FbsMovementError(
            f"Место {destination.location_code} уже содержит FBS-паллету "
            "другого клиента. Начальник склада может разрешить смешивание "
            "клиентов в настройках этого места."
        )


@transaction.atomic
def complete_fbs_free_relocation(
    *,
    operation_id: int,
    destination_row_no: int,
    destination_section_no: int,
    destination_tier_no: int,
    destination_cell_no: int,
    destination_zone_code: str = "OS",
    destination_location_id: int | None = None,
    performed_by=None,
    performed_by_role: str = "reachtruck",
) -> WarehouseOperation:
    _require_writes()
    operation = WarehouseOperation.objects.select_for_update().get(pk=int(operation_id))
    if (
        operation.operation_type != WarehouseOperation.TYPE_INTERNAL_RELOCATION
        or operation.context_type != FBS_FREE_RELOCATION_CONTEXT
    ):
        raise FbsMovementError("Операция не является свободным перемещением FBS.")
    if operation.status != WarehouseOperation.STATUS_IN_PROGRESS:
        raise FbsMovementError("Свободное перемещение FBS уже завершено или отменено.")

    pallet = _locked_pallet(int(operation.context_id))
    if pallet.agency_id != operation.agency_id:
        raise FbsMovementError("Клиент операции и FBS-паллеты не совпадает.")
    source = operation.source_location
    if source is None:
        raise FbsMovementError("У операции не найдено исходное место FBS-паллеты.")
    source_zone = _zone_code(source)
    pallet_container = WarehouseContainer.objects.select_for_update().get(
        pk=pallet.warehouse_container_id
    )
    if pallet_container.current_location_id not in {None, int(source.id)}:
        raise FbsMovementError("Адрес FBS-паллеты изменился во время перемещения.")
    destination_zone = _normalize(destination_zone_code).upper()
    allowed_destinations = allowed_fbs_destination_zones(source_zone)
    if destination_zone not in allowed_destinations:
        raise FbsMovementError(
            "Свободное FBS-перемещение разрешено только между точными местами "
            "OS, PR и OTG."
        )
    list(_active_boxes(pallet).select_for_update(of=("self",)).order_by("id"))
    balances = list(
        _active_balances(pallet).select_for_update(of=("self",)).order_by("id")
    )
    blockers = _pallet_blockers(
        pallet,
        exclude_operation_id=operation.id,
        source_location=source,
        allow_in_transit=True,
        lock_reservations=True,
    )
    if blockers:
        raise FbsMovementError("; ".join(blockers))

    destination = _destination_location(
        source=source,
        destination_zone_code=destination_zone,
        row_no=destination_row_no,
        section_no=destination_section_no,
        tier_no=destination_tier_no,
        cell_no=destination_cell_no,
        destination_location_id=destination_location_id,
    )
    if int(destination.id) == int(source.id):
        raise FbsMovementError("Новое место совпадает с текущим адресом FBS-паллеты.")
    _validate_destination_client_mix(destination=destination, pallet=pallet)

    destination_cell = None
    if destination_zone == "OS":
        shared_named_location = _is_shared_named_os_location(destination)
        if shared_named_location:
            try:
                validate_operational_location(
                    destination,
                    expected_zone="OS",
                    require_fbs=True,
                    required_slots=1,
                )
            except ValidationError as exc:
                raise FbsMovementError("; ".join(exc.messages)) from exc
        destination_cell, _created = FbsStorageCell.objects.get_or_create(
            location=destination,
            defaults={
                "cell_code": _fbs_storage_cell_code(destination),
                "purpose": FbsStorageCell.PURPOSE_FLEX,
                "client_cluster": pallet.agency_id,
                "is_active": True,
            },
        )
        destination_cell = FbsStorageCell.objects.select_for_update().get(
            pk=destination_cell.pk
        )
        if not destination_cell.is_active:
            raise FbsMovementError("Выбранная ячейка выключена для FBS.")
        if not shared_named_location:
            destination_pallet = FbsPallet.objects.select_for_update(of=("self",)).filter(
                cell=destination_cell,
                status__in=ACTIVE_PALLET_STATUSES,
            ).exclude(pk=pallet.pk).order_by("id").first()
            if destination_pallet is not None:
                from .storage import release_unplaced_os_reservation

                if release_unplaced_os_reservation(pallet=destination_pallet):
                    destination_pallet = None
            if destination_pallet is not None:
                raise FbsMovementError("В выбранной ячейке уже находится FBS-паллета.")
            occupancy_message = os_location_occupancy_message(
                destination,
                exclude_container_code=pallet_container.container_code,
            )
            if occupancy_message:
                raise FbsMovementError(f"Ячейка недоступна: {occupancy_message}")

    total_qty = sum(int(balance.qty or 0) for balance in balances)
    boxes = list(_active_boxes(pallet).select_for_update(of=("self",)).order_by("id"))
    box_container_ids = [int(box.source_container_id) for box in boxes if box.source_container_id]
    transit_cell_id = int(pallet.cell_id)
    now = timezone.now()

    if destination_cell is not None:
        pallet.cell = destination_cell
        pallet.full_clean()
        pallet.save(update_fields=["cell", "updated_at"])
    else:
        task_for_source_cell = operation.tasks.select_for_update().order_by("id").first()
        source_cell_id = int(
            (task_for_source_cell.payload if task_for_source_cell else {}).get(
                "source_cell_id"
            )
            or 0
        )
        source_cell = (
            FbsStorageCell.objects.select_for_update().filter(
                pk=source_cell_id,
                is_active=True,
            ).first()
            if source_cell_id
            else None
        )
        if source_cell is None:
            raise FbsMovementError("Исходная FBS-ячейка операции не найдена.")
        pallet.cell = source_cell
        pallet.full_clean()
        pallet.save(update_fields=["cell", "updated_at"])

    pallet_container.current_location = destination
    pallet_container.status = WarehouseContainer.STATUS_ACTIVE
    pallet_container.source_context_type = FBS_STORAGE_CONTEXT_TYPE
    pallet_container.source_context_id = pallet.pallet_code
    pallet_container.save(
        update_fields=[
            "current_location",
            "status",
            "source_context_type",
            "source_context_id",
            "updated_at",
        ]
    )
    if box_container_ids:
        WarehouseContainer.objects.select_for_update().filter(
            id__in=box_container_ids
        ).update(
            current_location=destination,
            parent_container=pallet_container,
            status=WarehouseContainer.STATUS_ACTIVE,
            updated_at=now,
        )

    task = operation.tasks.select_for_update().order_by("id").first()
    if task:
        task.to_location = destination
        task.to_zone_code = destination_zone
        task.qty_done = total_qty
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

    actor = _authenticated_user(performed_by)
    actor_role = _normalize(performed_by_role) or "reachtruck"
    WarehouseEvent.objects.create(
        agency=pallet.agency,
        event_type="fbs_free_relocation_completed",
        stock_context_type="fbs_pallet",
        stock_context_id=str(pallet.id),
        container=pallet_container,
        operation=operation,
        operation_task=task,
        source_document_type="fbs_pallet",
        source_document_id=str(pallet.id),
        from_location=source,
        to_location=destination,
        from_zone_code=source_zone,
        to_zone_code=destination_zone,
        qty=total_qty,
        payload={
            "free_move": True,
            "fbs_free_move": True,
            "fbs_pallet_code": pallet.pallet_code,
            "source_cell_id": int((task.payload if task else {}).get("source_cell_id") or 0),
            "destination_cell_id": int(destination_cell.id) if destination_cell else 0,
            "source_location_id": int(source.id),
            "destination_location_id": int(destination.id),
            "source_zone_code": source_zone,
            "destination_zone_code": destination_zone,
        },
        performed_by=actor,
        performed_by_role=actor_role,
        occurred_at=now,
    )
    operation.destination_location = destination
    operation.destination_zone_code = destination_zone
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
    from .picking import resort_queued_pick_batches_for_balances

    moved_balance_ids = tuple(int(balance.id) for balance in balances)
    transaction.on_commit(
        lambda balance_ids=moved_balance_ids: (
            resort_queued_pick_batches_for_balances(balance_ids)
        )
    )
    _delete_unused_transit_cell(transit_cell_id)
    return operation


@transaction.atomic
def cancel_fbs_free_relocation(
    *,
    operation_id: int,
    performed_by=None,
) -> WarehouseOperation:
    _require_writes()
    operation = WarehouseOperation.objects.select_for_update().get(pk=int(operation_id))
    if (
        operation.operation_type != WarehouseOperation.TYPE_INTERNAL_RELOCATION
        or operation.context_type != FBS_FREE_RELOCATION_CONTEXT
    ):
        raise FbsMovementError("Операция не является свободным перемещением FBS.")
    if operation.status == WarehouseOperation.STATUS_CANCELED:
        return operation
    if operation.status != WarehouseOperation.STATUS_IN_PROGRESS:
        raise FbsMovementError("Завершённое перемещение FBS отменить нельзя.")

    pallet = _locked_pallet(int(operation.context_id))
    location = operation.source_location
    if location is None:
        raise FbsMovementError("У операции не найдено исходное место FBS-паллеты.")
    zone = _zone_code(location)
    now = timezone.now()
    task = operation.tasks.select_for_update().order_by("id").first()
    source_cell_id = int((task.payload if task else {}).get("source_cell_id") or 0)
    transit_cell_id = int(pallet.cell_id)
    source_cell = (
        FbsStorageCell.objects.select_for_update().filter(
            pk=source_cell_id,
            location=location,
            is_active=True,
        ).first()
        if source_cell_id
        else None
    )
    if source_cell is None:
        raise FbsMovementError("Исходная FBS-ячейка операции не найдена.")
    pallet_container = WarehouseContainer.objects.select_for_update().get(
        pk=pallet.warehouse_container_id
    )
    occupancy_message = os_location_occupancy_message(
        location,
        exclude_container_code=pallet_container.container_code,
    )
    other_fbs_pallet = FbsPallet.objects.select_for_update(of=("self",)).filter(
        cell=source_cell,
        status__in=ACTIVE_PALLET_STATUSES,
    ).exclude(pk=pallet.pk).exists()
    if occupancy_message or other_fbs_pallet:
        raise FbsMovementError(
            "Исходное место уже занято после снятия паллеты. "
            "Отменить перемещение с возвратом на это место нельзя."
        )
    pallet.cell = source_cell
    pallet.full_clean()
    pallet.save(update_fields=["cell", "updated_at"])
    pallet_container.current_location = location
    pallet_container.save(update_fields=["current_location", "updated_at"])
    box_container_ids = list(
        _active_boxes(pallet)
        .exclude(source_container_id=None)
        .values_list("source_container_id", flat=True)
    )
    if box_container_ids:
        WarehouseContainer.objects.filter(id__in=box_container_ids).update(
            current_location=location,
            parent_container=pallet_container,
            updated_at=now,
        )
    if task:
        task.status = WarehouseOperationTask.STATUS_CANCELED
        task.completed_at = now
        task.save(update_fields=["status", "completed_at", "updated_at"])
    actor = _authenticated_user(performed_by)
    WarehouseEvent.objects.create(
        agency=pallet.agency,
        event_type="fbs_free_relocation_canceled",
        stock_context_type="fbs_pallet",
        stock_context_id=str(pallet.id),
        container=pallet.warehouse_container,
        operation=operation,
        operation_task=task,
        source_document_type="fbs_pallet",
        source_document_id=str(pallet.id),
        from_location=location,
        to_location=location,
        from_zone_code=zone,
        to_zone_code=zone,
        qty=int(operation.planned_qty or 0),
        payload={
            "free_move": True,
            "fbs_free_move": True,
            "fbs_pallet_code": pallet.pallet_code,
        },
        performed_by=actor,
        performed_by_role="reachtruck",
        occurred_at=now,
    )
    operation.status = WarehouseOperation.STATUS_CANCELED
    operation.completed_at = now
    operation.save(update_fields=["status", "completed_at", "updated_at"])
    _delete_unused_transit_cell(transit_cell_id)
    return operation


def fbs_relocation_context(operation: WarehouseOperation) -> dict:
    pallet = (
        FbsPallet.objects.select_related("cell__location", "agency")
        .filter(pk=int(operation.context_id), agency_id=operation.agency_id)
        .first()
    )
    if pallet is None:
        return {}
    lines = _balance_lines(pallet)
    source = operation.source_location or pallet.cell.location
    source_code = (
        os_location_code(
            row=source.row_no,
            section=source.section_no,
            tier=source.tier_no,
            cell=source.cell_no,
        )
        if str(source.zone_code or "").strip().upper() == "OS"
        and all(
            int(value or 0) > 0
            for value in (source.row_no, source.section_no, source.tier_no, source.cell_no)
        )
        else str(source.location_code or "")
    )
    return {
        "moving": True,
        "operation": operation,
        "pallet_code": pallet.pallet_code,
        "move_contour_label": "FBS",
        "source_label": str(source.display_name or source_code),
        "source_scan_code": source_code,
        "total_qty": sum(int(row["qty"] or 0) for row in lines),
        "lines": lines,
    }
