from __future__ import annotations

import hashlib

from django.core.exceptions import ValidationError
from django.db import transaction

from sklad.models import WarehouseLocation
from sklad.topology import os_location_code


def _text(value) -> str:
    return str(value or "").strip()


def _unique_storage_cell_code(*, location: WarehouseLocation, prefix: str) -> str:
    """Return a stable FBS alias for one canonical warehouse location."""

    from fbs.models import FbsStorageCell

    physical_code = _text(location.location_code)
    if not physical_code and str(location.zone_code or "").strip().upper() == "OS":
        physical_code = os_location_code(
            row=location.row_no,
            section=location.section_no,
            tier=location.tier_no,
            cell=location.cell_no,
        )
    physical_code = physical_code or f"LOCATION-{int(location.id or 0)}"
    candidate = f"{prefix}{physical_code}"
    if len(candidate) > 64:
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:12].upper()
        candidate = f"{candidate[:51]}-{digest}"
    conflict = FbsStorageCell.objects.filter(cell_code=candidate).exclude(
        location=location
    )
    if conflict.exists():
        candidate = f"FBS@LOCATION-{int(location.id or 0)}"
    return candidate


@transaction.atomic
def ensure_shared_fbs_storage_cell(
    location: WarehouseLocation,
    *,
    purpose: str | None = None,
):
    """Attach the FBS catalog to the same physical WarehouseLocation row."""

    from fbs.models import FbsStorageCell

    locked = WarehouseLocation.objects.select_for_update().get(pk=location.pk)
    zone = str(locked.zone_code or "").strip().upper()
    coordinates = (
        int(locked.row_no or 0),
        int(locked.section_no or 0),
        int(locked.tier_no or 0),
        int(locked.cell_no or 0),
    )
    eligible_os = zone == "OS" and all(coordinates)
    eligible_pr = (
        zone == "PR"
        and not locked.is_topology_visible
        and locked.is_fbs_visible
    )
    if not (eligible_os or eligible_pr):
        raise ValidationError(
            "Общей FBS-ячейкой может быть только точная ячейка OS "
            "или дополнительная ячейка PR, разрешённая для FBS."
        )

    cell = FbsStorageCell.objects.select_for_update().filter(location=locked).first()
    if cell is None:
        prefix = "" if eligible_pr else "FBS@"
        cell = FbsStorageCell(
            cell_code=_unique_storage_cell_code(location=locked, prefix=prefix),
            location=locked,
            purpose=purpose or FbsStorageCell.PURPOSE_FLEX,
            client_cluster=0,
            is_active=bool(locked.is_active),
        )
        cell.full_clean()
        cell.save()
        return cell

    updates: list[str] = []
    if cell.is_active != bool(locked.is_active):
        cell.is_active = bool(locked.is_active)
        updates.append("is_active")
    if purpose and cell.purpose != purpose:
        cell.purpose = purpose
        updates.append("purpose")
    if updates:
        cell.full_clean()
        cell.save(update_fields=[*updates, "updated_at"])
    return cell


@transaction.atomic
def ensure_exact_fbs_rack_cell(
    location: WarehouseLocation,
    *,
    created_by=None,
):
    """Make an exact additional PR place directly scannable as a one-cell rack."""

    from fbs.models import FbsPallet, FbsRack, FbsRackCell, FbsStorageCell

    locked = WarehouseLocation.objects.select_for_update().get(pk=location.pk)
    if (
        str(locked.zone_code or "").strip().upper() != "PR"
        or locked.is_topology_visible
        or not locked.is_fbs_visible
        or not locked.is_active
    ):
        raise ValidationError(
            "Точная FBS-ячейка создаётся только для активного дополнительного места PR."
        )

    actor = created_by if getattr(created_by, "is_authenticated", False) else None
    rack, created = FbsRack.objects.select_for_update().get_or_create(
        location=locked,
        defaults={"created_by": actor, "is_active": True},
    )
    if created:
        rack.full_clean()
    elif not rack.is_active:
        rack.is_active = True
        rack.save(update_fields=["is_active", "updated_at"])

    existing = (
        FbsRackCell.objects.select_for_update()
        .select_related("storage_cell__location")
        .filter(rack=rack, is_active=True, storage_cell__is_active=True)
        .order_by("position", "id")
        .first()
    )
    if existing is not None:
        return existing

    storage_cell = ensure_shared_fbs_storage_cell(
        locked,
        purpose=FbsStorageCell.PURPOSE_PICK,
    )
    if FbsPallet.objects.filter(
        cell=storage_cell,
        status__in=(FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE),
        is_rack_binding=False,
    ).exists():
        raise ValidationError(
            "В PR-ячейке уже находится обычная FBS-паллета. "
            "Сначала освободите место."
        )
    rack_cell = FbsRackCell.objects.select_for_update().filter(
        storage_cell=storage_cell
    ).first()
    if rack_cell is not None:
        if rack_cell.rack_id != rack.id:
            raise ValidationError(
                "Физическая PR-ячейка уже привязана к другому FBS-стеллажу."
            )
        updates: list[str] = []
        if rack_cell.position != 1:
            rack_cell.position = 1
            updates.append("position")
        if not rack_cell.is_active:
            rack_cell.is_active = True
            updates.append("is_active")
        if updates:
            rack_cell.full_clean()
            rack_cell.save(update_fields=[*updates, "updated_at"])
        return rack_cell

    rack_cell = FbsRackCell(
        rack=rack,
        storage_cell=storage_cell,
        position=1,
        is_active=True,
    )
    rack_cell.full_clean()
    rack_cell.save()
    return rack_cell
