from dataclasses import dataclass
from collections.abc import Iterable
from uuid import uuid4

from django.conf import settings
from django.db import transaction

from fbs.exceptions import FbsFeatureDisabled, FbsStorageError
from fbs.flags import feature_enabled
from fbs.models import FbsBox, FbsPallet, FbsStorageCell
from sku.models import Agency
from sklad.location_occupancy import (
    FBS_STORAGE_CONTEXT_TYPE,
    empty_planned_fbs_placeholder_containers,
    os_location_occupancy_message,
)
from sklad.models import WarehouseContainer, WarehouseLocation, WarehouseOperation


ACTIVE_PALLET_STATUSES = [FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE]
ACTIVE_BOX_STATUSES = [FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE]


@dataclass(frozen=True)
class FbsDestinationSuggestion:
    cell_id: int
    pallet_id: int | None
    free_box_slots: int


def _require_module() -> None:
    if not feature_enabled("module"):
        raise FbsFeatureDisabled("Модуль FBS выключен.")


def _authenticated_user(user):
    return user if getattr(user, "is_authenticated", False) else None


def _planning_zone_code() -> str:
    return str(getattr(settings, "FBS_ZONE_CODE", "FBS") or "FBS").strip().upper()


def _create_virtual_planning_cell(*, agency: Agency) -> FbsStorageCell:
    """Creates a non-physical cell that keeps plan relations valid until driver scan."""
    zone_code = _planning_zone_code()
    row_no = int(agency.id or 0)
    last_cell_no = (
        WarehouseLocation.objects.filter(
            warehouse_code="MSK",
            zone_code__iexact=zone_code,
            zone_kind=WarehouseLocation.ZONE_KIND_VIRTUAL,
            row_no=row_no,
            section_no=0,
            tier_no=0,
        )
        .order_by("-cell_no", "-id")
        .values_list("cell_no", flat=True)
        .first()
        or 0
    )
    cell_no = int(last_cell_no) + 1
    token = uuid4().hex[:12].upper()
    location = WarehouseLocation.objects.create(
        warehouse_code="MSK",
        zone_code=zone_code,
        zone_kind=WarehouseLocation.ZONE_KIND_VIRTUAL,
        row_no=row_no,
        section_no=0,
        tier_no=0,
        cell_no=cell_no,
        location_code=f"FBS-PLAN-{agency.id}-{token}",
        display_name="План FBS · место определяется сканом ричтрака",
        is_active=True,
        is_storage=False,
    )
    storage_cell = FbsStorageCell(
        cell_code=f"FBS@PLAN-{agency.id}-{token}",
        location=location,
        purpose=FbsStorageCell.PURPOSE_FLEX,
        client_cluster=agency.id,
        is_active=True,
    )
    storage_cell.full_clean()
    storage_cell.save()
    return storage_cell


def _unique_planning_pallet_code(*, agency: Agency) -> str:
    for _ in range(5):
        pallet_code = f"FBS-PAL-{agency.id}-{uuid4().hex[:10].upper()}"
        if not WarehouseContainer.objects.filter(
            agency=agency,
            container_code=pallet_code,
        ).exists() and not FbsPallet.objects.filter(
            agency=agency,
            pallet_code=pallet_code,
        ).exists():
            return pallet_code
    raise FbsStorageError("Не удалось сформировать уникальный QR FBS-паллеты.")


def _create_virtual_planned_pallet(
    *,
    agency: Agency,
    max_boxes: int,
) -> FbsPallet:
    pallet_code = _unique_planning_pallet_code(agency=agency)
    storage_cell = _create_virtual_planning_cell(agency=agency)
    warehouse_container = WarehouseContainer.objects.create(
        agency=agency,
        container_type=WarehouseContainer.TYPE_PALLET,
        container_code=pallet_code,
        current_location=None,
        status=WarehouseContainer.STATUS_ACTIVE,
        source_context_type=FBS_STORAGE_CONTEXT_TYPE,
        source_context_id=pallet_code,
    )
    pallet = FbsPallet(
        agency=agency,
        pallet_code=pallet_code,
        cell=storage_cell,
        warehouse_container=warehouse_container,
        max_boxes=max_boxes,
    )
    pallet.full_clean()
    pallet.save()
    return pallet


@transaction.atomic
def release_unplaced_os_reservation(*, pallet: FbsPallet) -> bool:
    """Moves a legacy empty planned placeholder out of physical OS topology."""
    locked = (
        FbsPallet.objects.select_for_update(of=("self",))
        .select_related("agency", "cell__location", "warehouse_container")
        .get(pk=pallet.pk)
    )
    if str(locked.cell.location.zone_code or "").strip().upper() != "OS":
        return True
    container = locked.warehouse_container
    if container is None or not empty_planned_fbs_placeholder_containers().filter(
        pk=container.pk
    ).exists():
        return False

    virtual_cell = _create_virtual_planning_cell(agency=locked.agency)
    locked.cell = virtual_cell
    locked.full_clean()
    locked.save(update_fields=["cell", "updated_at"])

    container = WarehouseContainer.objects.select_for_update().get(pk=container.pk)
    if container.current_location_id is not None:
        container.current_location = None
        container.save(update_fields=["current_location", "updated_at"])

    from fbs.models import FbsReplenishmentPlan

    plans = list(
        FbsReplenishmentPlan.objects.select_for_update(of=("self",))
        .select_related("warehouse_operation")
        .filter(
            target_pallet=locked,
            status__in=(
                FbsReplenishmentPlan.STATUS_PROPOSED,
                FbsReplenishmentPlan.STATUS_CONFIRMED,
                FbsReplenishmentPlan.STATUS_IN_PROGRESS,
                FbsReplenishmentPlan.STATUS_AWAITING_PACK,
                FbsReplenishmentPlan.STATUS_BLOCKED,
            ),
        )
        .order_by("id")
    )
    for plan in plans:
        plan.target_cell = virtual_cell
        plan.save(update_fields=["target_cell", "updated_at"])
        operation = plan.warehouse_operation
        if operation is not None and operation.status not in {
            WarehouseOperation.STATUS_DONE,
            WarehouseOperation.STATUS_CANCELED,
        }:
            operation.destination_location = virtual_cell.location
            operation.destination_zone_code = virtual_cell.location.zone_code
            operation.save(
                update_fields=[
                    "destination_location",
                    "destination_zone_code",
                    "updated_at",
                ]
            )
            for task in operation.tasks.select_for_update().all():
                payload = dict(task.payload or {})
                payload["target_cell_code"] = virtual_cell.cell_code
                task.to_location = virtual_cell.location
                task.to_zone_code = virtual_cell.location.zone_code
                task.payload = payload
                task.save(
                    update_fields=[
                        "to_location",
                        "to_zone_code",
                        "payload",
                        "updated_at",
                    ]
                )
    return True


@transaction.atomic
def create_fbs_pallet(
    *,
    agency: Agency,
    cell: FbsStorageCell,
    pallet_code: str,
    max_boxes: int = 10,
    warehouse_container: WarehouseContainer | None = None,
) -> FbsPallet:
    _require_module()
    locked_cell = FbsStorageCell.objects.select_for_update().select_related("location").get(pk=cell.pk)
    location = WarehouseLocation.objects.select_for_update().get(pk=locked_cell.location_id)
    if not locked_cell.is_active:
        raise FbsStorageError("FBS-ячейка неактивна.")
    if FbsPallet.objects.filter(cell=locked_cell, status__in=ACTIVE_PALLET_STATUSES).exists():
        raise FbsStorageError("В FBS-ячейке уже есть активная или запланированная паллета.")
    normalized_pallet_code = str(pallet_code or "").strip()
    if not normalized_pallet_code:
        raise FbsStorageError("Не указан QR FBS-паллеты.")
    if warehouse_container is not None:
        warehouse_container = WarehouseContainer.objects.select_for_update().get(
            pk=warehouse_container.pk
        )
        if warehouse_container.agency_id != agency.id:
            raise FbsStorageError("Складская паллета принадлежит другому клиенту.")
        if warehouse_container.container_type not in {
            WarehouseContainer.TYPE_PALLET,
            WarehouseContainer.TYPE_MIXED_PALLET,
        }:
            raise FbsStorageError("Для FBS-паллеты нужен паллетный контейнер.")

    if str(location.zone_code or "").strip().upper() == "OS":
        occupancy_message = os_location_occupancy_message(
            location,
            exclude_container_code=(
                str(warehouse_container.container_code or "").strip()
                if warehouse_container is not None
                else ""
            ),
        )
        if occupancy_message:
            raise FbsStorageError(occupancy_message)
        if warehouse_container is None:
            warehouse_container = WarehouseContainer.objects.create(
                agency=agency,
                container_type=WarehouseContainer.TYPE_PALLET,
                container_code=normalized_pallet_code,
                current_location=location,
                status=WarehouseContainer.STATUS_ACTIVE,
                source_context_type=FBS_STORAGE_CONTEXT_TYPE,
                source_context_id=normalized_pallet_code,
            )
        else:
            warehouse_container.current_location = location
            warehouse_container.status = WarehouseContainer.STATUS_ACTIVE
            warehouse_container.source_context_type = FBS_STORAGE_CONTEXT_TYPE
            warehouse_container.source_context_id = normalized_pallet_code
            warehouse_container.save(
                update_fields=[
                    "current_location",
                    "status",
                    "source_context_type",
                    "source_context_id",
                    "updated_at",
                ]
            )

    pallet = FbsPallet(
        agency=agency,
        pallet_code=normalized_pallet_code,
        cell=locked_cell,
        warehouse_container=warehouse_container,
        max_boxes=max_boxes,
    )
    pallet.full_clean()
    pallet.save()
    return pallet


@transaction.atomic
def allocate_shared_os_fbs_pallet(
    *,
    agency: Agency,
    required_box_slots: int = 1,
    max_boxes: int = 10,
) -> FbsPallet:
    """Creates one plan placeholder; the physical OS cell is chosen by driver scan."""
    return allocate_shared_os_fbs_pallet_batch(
        agency=agency,
        slot_requirements=((required_box_slots, max_boxes),),
    )[0]


@transaction.atomic
def allocate_shared_os_fbs_pallet_batch(
    *,
    agency: Agency,
    slot_requirements: Iterable[tuple[int, int]],
) -> tuple[FbsPallet, ...]:
    """Creates plan placeholders without reserving physical warehouse topology."""
    _require_module()
    requirements = []
    for raw_required, raw_capacity in slot_requirements:
        required_slots = max(1, int(raw_required or 1))
        # This pallet is a planning placeholder, not a physical capacity limit.
        # Reserve enough slots for the whole movement so later box scans fit too.
        capacity = max(required_slots, int(raw_capacity or 10), 1)
        requirements.append((required_slots, capacity))
    if not requirements:
        return ()

    agency = Agency.objects.select_for_update().get(pk=agency.pk)
    allocated: list[FbsPallet] = []
    for _required_slots, capacity in requirements:
        allocated.append(
            _create_virtual_planned_pallet(
                agency=agency,
                max_boxes=capacity,
            )
        )
    return tuple(allocated)


@transaction.atomic
def allocate_exact_fbs_pallet(
    *,
    agency: Agency,
    required_box_slots: int = 1,
    max_boxes: int = 10,
) -> FbsPallet:
    """Reserve a real FBS place, never a virtual reachtruck placeholder."""
    _require_module()
    required_slots = max(1, int(required_box_slots or 1))
    capacity = max(required_slots, int(max_boxes or 10), 1)
    agency = Agency.objects.select_for_update().get(pk=agency.pk)

    existing_pallets = (
        FbsPallet.objects.select_for_update(of=("self",))
        .filter(
            agency=agency,
            status__in=ACTIVE_PALLET_STATUSES,
            cell__is_active=True,
            cell__location__is_active=True,
        )
        .exclude(cell__location__zone_kind=WarehouseLocation.ZONE_KIND_VIRTUAL)
        .select_related("cell__location")
        .order_by("cell__cell_code", "id")
    )
    from fbs.models import FbsReplenishmentPlan

    for pallet in existing_pallets:
        active_box_count = FbsBox.objects.filter(
            pallet=pallet,
            status__in=ACTIVE_BOX_STATUSES,
        ).count()
        pending_box_count = FbsReplenishmentPlan.objects.filter(
            target_pallet=pallet,
            mode=FbsReplenishmentPlan.MODE_ITEM,
            target_box__isnull=True,
            status__in=(
                FbsReplenishmentPlan.STATUS_PROPOSED,
                FbsReplenishmentPlan.STATUS_CONFIRMED,
                FbsReplenishmentPlan.STATUS_IN_PROGRESS,
                FbsReplenishmentPlan.STATUS_AWAITING_PACK,
            ),
        ).count()
        if (
            int(pallet.max_boxes or 0)
            - active_box_count
            - pending_box_count
            >= required_slots
        ):
            return pallet

    occupied_cell_ids = FbsPallet.objects.filter(
        status__in=ACTIVE_PALLET_STATUSES,
        is_rack_binding=False,
    ).values_list("cell_id", flat=True)
    free_cells = (
        FbsStorageCell.objects.select_for_update(of=("self",), skip_locked=True)
        .filter(
            is_active=True,
            location__is_active=True,
            location__zone_code__iexact="OS",
            client_cluster__in=(0, int(agency.id)),
            rack_cell__isnull=True,
        )
        .exclude(id__in=occupied_cell_ids)
        .select_related("location")
        .order_by("cell_code", "id")
    )
    for free_cell in free_cells:
        try:
            return create_fbs_pallet(
                agency=agency,
                cell=free_cell,
                pallet_code=_unique_planning_pallet_code(agency=agency),
                max_boxes=capacity,
            )
        except FbsStorageError:
            # The warehouse occupancy guard is authoritative. Another physical
            # container may be using a cell that has no active FBS pallet row.
            continue
    raise FbsStorageError(
        "Нет свободного точного FBS-места. Паллета остается в зоне приемки PR; "
        "освободите FBS-место и повторите размещение."
    )


@transaction.atomic
def create_fbs_box(
    *,
    agency: Agency,
    pallet: FbsPallet,
    box_code: str,
    source_container: WarehouseContainer | None = None,
) -> FbsBox:
    _require_module()
    locked_pallet = (
        FbsPallet.objects.select_for_update().select_related("cell").get(pk=pallet.pk)
    )
    from .inventory import assert_pallet_not_relocating

    assert_pallet_not_relocating(
        locked_pallet.id,
        agency_id=locked_pallet.agency_id,
    )
    if locked_pallet.agency_id != agency.id:
        raise FbsStorageError("В одной FBS-паллете нельзя смешивать клиентов.")
    if locked_pallet.status not in ACTIVE_PALLET_STATUSES:
        raise FbsStorageError("Нельзя добавить короб на архивную FBS-паллету.")
    box_count = FbsBox.objects.filter(
        pallet=locked_pallet,
        status__in=ACTIVE_BOX_STATUSES,
    ).count()
    if box_count >= locked_pallet.max_boxes:
        raise FbsStorageError(
            f"На паллете уже достигнут лимит {locked_pallet.max_boxes} коробов."
        )

    if source_container is not None:
        source_container = WarehouseContainer.objects.select_for_update().get(
            pk=source_container.pk
        )
        if source_container.agency_id != agency.id:
            raise FbsStorageError("Исходный короб принадлежит другому клиенту.")
        if source_container.container_type != WarehouseContainer.TYPE_BOX:
            raise FbsStorageError("Для подсорта коробами нужен складской контейнер типа box.")

    box = FbsBox(
        agency=agency,
        pallet=locked_pallet,
        box_code=str(box_code or "").strip(),
        source_container=source_container,
    )
    box.full_clean()
    box.save()
    return box


@transaction.atomic
def attach_existing_warehouse_box_to_fbs(
    *,
    agency: Agency,
    pallet: FbsPallet,
    source_container: WarehouseContainer,
) -> FbsBox:
    """Registers an existing physical warehouse box in FBS without issuing a new QR."""
    _require_module()
    locked_pallet = (
        FbsPallet.objects.select_for_update().select_related("cell").get(pk=pallet.pk)
    )
    from .inventory import assert_pallet_not_relocating

    assert_pallet_not_relocating(
        locked_pallet.id,
        agency_id=locked_pallet.agency_id,
    )
    source_container = WarehouseContainer.objects.select_for_update().get(
        pk=source_container.pk
    )
    if locked_pallet.agency_id != agency.id:
        raise FbsStorageError("В одной FBS-паллете нельзя смешивать клиентов.")
    if locked_pallet.status not in ACTIVE_PALLET_STATUSES:
        raise FbsStorageError("Нельзя добавить короб на архивную FBS-паллету.")
    if source_container.agency_id != agency.id:
        raise FbsStorageError("Исходный короб принадлежит другому клиенту.")
    if source_container.container_type != WarehouseContainer.TYPE_BOX:
        raise FbsStorageError("Для коробочного перемещения нужен контейнер типа box.")

    existing = (
        FbsBox.objects.select_for_update()
        .filter(source_container=source_container)
        .first()
    )
    if existing is None:
        existing = (
            FbsBox.objects.select_for_update()
            .filter(agency=agency, box_code=source_container.container_code)
            .first()
        )
    if existing is not None:
        if existing.agency_id != agency.id:
            raise FbsStorageError("QR исходного короба уже занят другим клиентом.")
        if existing.source_container_id not in {None, source_container.id}:
            raise FbsStorageError("QR исходного короба уже связан с другим контейнером.")
        if existing.pallet_id != locked_pallet.id:
            if existing.stock_balances.filter(qty__gt=0).exists():
                raise FbsStorageError("Короб с этим QR уже содержит FBS-остаток на другой паллете.")
            box_count = FbsBox.objects.filter(
                pallet=locked_pallet,
                status__in=ACTIVE_BOX_STATUSES,
            ).count()
            if box_count >= locked_pallet.max_boxes:
                raise FbsStorageError(
                    f"На паллете уже достигнут лимит {locked_pallet.max_boxes} коробов."
                )
            existing.pallet = locked_pallet
        elif existing.status not in ACTIVE_BOX_STATUSES:
            box_count = FbsBox.objects.filter(
                pallet=locked_pallet,
                status__in=ACTIVE_BOX_STATUSES,
            ).count()
            if box_count >= locked_pallet.max_boxes:
                raise FbsStorageError(
                    f"На паллете уже достигнут лимит {locked_pallet.max_boxes} коробов."
                )
        existing.source_container = source_container
        existing.status = FbsBox.STATUS_PLANNED
        existing.full_clean()
        existing.save(
            update_fields=["pallet", "source_container", "status", "updated_at"]
        )
        return existing

    box_count = FbsBox.objects.filter(
        pallet=locked_pallet,
        status__in=ACTIVE_BOX_STATUSES,
    ).count()
    if box_count >= locked_pallet.max_boxes:
        raise FbsStorageError(
            f"На паллете уже достигнут лимит {locked_pallet.max_boxes} коробов."
        )
    return create_fbs_box(
        agency=agency,
        pallet=locked_pallet,
        box_code=source_container.container_code,
        source_container=source_container,
    )


def create_generated_fbs_box(*, agency: Agency, pallet: FbsPallet) -> FbsBox:
    """Creates a new physical FBS box and returns the code that must be printed as QR."""
    for _ in range(5):
        box_code = f"FBS-BOX-{agency.id}-{uuid4().hex[:10].upper()}"
        if not FbsBox.objects.filter(agency=agency, box_code=box_code).exists():
            return create_fbs_box(
                agency=agency,
                pallet=pallet,
                box_code=box_code,
            )
    raise FbsStorageError("Не удалось сформировать уникальный QR нового FBS-короба.")


def suggest_fbs_destination(*, agency: Agency) -> FbsDestinationSuggestion:
    _require_module()
    pallets = (
        FbsPallet.objects.filter(
            agency=agency,
            status__in=ACTIVE_PALLET_STATUSES,
            cell__is_active=True,
        )
        .select_related("cell")
        .order_by("cell__cell_code", "id")
    )
    for pallet in pallets:
        box_count = pallet.boxes.filter(status__in=ACTIVE_BOX_STATUSES).count()
        if box_count < pallet.max_boxes:
            return FbsDestinationSuggestion(
                cell_id=pallet.cell_id,
                pallet_id=pallet.id,
                free_box_slots=pallet.max_boxes - box_count,
            )

    occupied_cell_ids = FbsPallet.objects.filter(
        status__in=ACTIVE_PALLET_STATUSES
    ).values_list("cell_id", flat=True)
    free_cell = (
        FbsStorageCell.objects.filter(is_active=True)
        .exclude(id__in=occupied_cell_ids)
        .order_by("cell_code", "id")
        .first()
    )
    if free_cell is None:
        raise FbsStorageError("Нет свободной FBS-ячейки для новой паллеты.")
    return FbsDestinationSuggestion(
        cell_id=free_cell.id,
        pallet_id=None,
        free_box_slots=0,
    )
