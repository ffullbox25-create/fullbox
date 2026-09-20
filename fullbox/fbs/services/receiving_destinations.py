"""Explicit storekeeper selection of the final FBS receiving destination."""
from django.db import transaction
from django.db.models import Count

from fbs.exceptions import FbsStorageError
from fbs.models import FbsBox, FbsPallet, FbsReplenishmentPlan, FbsStorageCell
from sklad.location_occupancy import shared_os_occupied_location_ids
from sklad.models import WarehouseLocation
from sku.models import Agency

from .storage import (
    ACTIVE_BOX_STATUSES, ACTIVE_PALLET_STATUSES, _require_module,
    _unique_planning_pallet_code, create_fbs_pallet,
)


def _cells(agency_id):
    return FbsStorageCell.objects.filter(
        is_active=True, location__is_active=True,
        location__warehouse_code="MSK", location__zone_code__iexact="OS",
        client_cluster__in=(0, int(agency_id)), rack_cell__isnull=True,
    ).exclude(location__zone_kind=WarehouseLocation.ZONE_KIND_VIRTUAL)


def _used_slots(pallet_ids):
    used = dict(FbsBox.objects.filter(
        pallet_id__in=pallet_ids, status__in=ACTIVE_BOX_STATUSES,
    ).values("pallet_id").annotate(n=Count("id")).values_list("pallet_id", "n"))
    pending = FbsReplenishmentPlan.objects.filter(
        target_pallet_id__in=pallet_ids, target_box__isnull=True,
        status__in=("proposed", "confirmed", "in_progress", "awaiting_pack"),
    ).values("target_pallet_id").annotate(n=Count("id"))
    for row in pending:
        key = row["target_pallet_id"]
        used[key] = used.get(key, 0) + row["n"]
    return used


def receiving_fbs_destination_options(*, agency_id):
    """Read-only choices. The selected place is checked again under locks."""
    cells = list(_cells(agency_id).select_related("location").order_by("cell_code", "id"))
    pallets = list(FbsPallet.objects.filter(
        cell_id__in=[cell.id for cell in cells], status__in=ACTIVE_PALLET_STATUSES,
    ))
    by_cell = {pallet.cell_id: pallet for pallet in pallets}
    used = _used_slots([pallet.id for pallet in pallets])
    occupied = shared_os_occupied_location_ids()
    result = []
    for cell in cells:
        pallet = by_cell.get(cell.id)
        free = None
        if pallet:
            if pallet.agency_id != int(agency_id) or pallet.is_rack_binding:
                continue
            free = max(0, int(pallet.max_boxes) - used.get(pallet.id, 0))
            if not free:
                continue
        elif cell.location_id in occupied:
            continue
        result.append({
            "cell_id": cell.id,
            "code": cell.warehouse_location_code,
            "label": cell.warehouse_location_label,
            "free_box_slots": free,
            "pallet_code": pallet.pallet_code if pallet else "",
        })
    return result


@transaction.atomic
def allocate_selected_receiving_fbs_pallet(*, agency, cell_id, required_box_slots):
    """Never substitute a different cell if the chosen one is unavailable."""
    _require_module()
    try:
        selected_id = int(cell_id)
    except (TypeError, ValueError):
        raise FbsStorageError("Кладовщик должен выбрать конечное FBS-место для паллеты.")
    Agency.objects.select_for_update().get(pk=agency.pk)
    cell = _cells(agency.id).select_for_update(of=("self",)).select_related("location").filter(pk=selected_id).first()
    if cell is None:
        raise FbsStorageError("Выбранное FBS-место недоступно этому клиенту. Выберите другое место.")
    WarehouseLocation.objects.select_for_update().get(pk=cell.location_id)
    pallet = FbsPallet.objects.select_for_update().filter(
        cell=cell, status__in=ACTIVE_PALLET_STATUSES,
    ).first()
    required = max(1, int(required_box_slots or 1))
    if pallet:
        if pallet.agency_id != agency.id or pallet.is_rack_binding:
            raise FbsStorageError("Выбранное FBS-место занято другой паллетой.")
        from .inventory import assert_pallet_not_relocating
        assert_pallet_not_relocating(pallet.id, agency_id=agency.id)
        free = int(pallet.max_boxes) - _used_slots([pallet.id]).get(pallet.id, 0)
        if free < required:
            raise FbsStorageError(
                f"В выбранном FBS-месте свободно {max(free, 0)} мест для коробов, нужно {required}. Выберите другое место."
            )
        return pallet
    return create_fbs_pallet(
        agency=agency, cell=cell,
        pallet_code=_unique_planning_pallet_code(agency=agency),
        max_boxes=max(required, 10),
    )
