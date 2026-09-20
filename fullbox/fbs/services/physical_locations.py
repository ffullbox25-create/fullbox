from __future__ import annotations

from django.db.models import Q

from sklad.topology import os_location_code, os_location_label


VIRTUAL_FBS_PLAN_PREFIX = "FBS-PLAN-"
ACTIVE_FBS_BOX_STATUSES = ("planned", "active")
ACTIVE_FBS_PALLET_STATUSES = ("planned", "active")
ACTIVE_WAREHOUSE_CONTAINER_STATUS = "active"


def is_virtual_fbs_plan_location(location) -> bool:
    """Return True only for the technical locations used by FBS plans."""
    if location is None:
        return False
    if str(getattr(location, "zone_kind", "") or "").strip().lower() == "virtual":
        return True
    location_code = str(getattr(location, "location_code", "") or "").strip().upper()
    return location_code.startswith(VIRTUAL_FBS_PLAN_PREFIX)


def fbs_box_reservable_q(*, prefix: str = "box") -> Q:
    """Match balances whose box has a usable physical FBS location.

    A loose box moved to an FBS-visible PR rack remains tied to its source
    logical pallet for history.  That pallet can be archived after the last
    physical box leaves it, so reservability must not depend on the historical
    pallet alone.  The pallet branch remains as a compatibility fallback for
    legacy boxes that are still stored through the original pallet model.
    """

    path = f"{prefix}__" if prefix else ""
    active_box = Q(**{f"{path}status__in": ACTIVE_FBS_BOX_STATUSES})
    physical_box = Q(
        **{
            f"{path}source_container__status": ACTIVE_WAREHOUSE_CONTAINER_STATUS,
            f"{path}source_container__current_location__is_active": True,
            f"{path}source_container__current_location__is_fbs_visible": True,
        }
    )
    legacy_pallet = Q(
        **{
            f"{path}pallet__status__in": ACTIVE_FBS_PALLET_STATUSES,
            f"{path}pallet__cell__is_active": True,
        }
    )
    return active_box & (physical_box | legacy_pallet)


def fbs_box_is_reservable(box) -> bool:
    """Object-level counterpart of :func:`fbs_box_reservable_q`."""

    if box is None or str(getattr(box, "status", "") or "") not in ACTIVE_FBS_BOX_STATUSES:
        return False

    source_container = getattr(box, "source_container", None)
    current_location = (
        getattr(source_container, "current_location", None)
        if source_container is not None
        else None
    )
    if (
        source_container is not None
        and str(getattr(source_container, "status", "") or "")
        == ACTIVE_WAREHOUSE_CONTAINER_STATUS
        and current_location is not None
        and bool(getattr(current_location, "is_active", False))
        and bool(getattr(current_location, "is_fbs_visible", False))
    ):
        return True

    pallet = getattr(box, "pallet", None)
    cell = getattr(pallet, "cell", None) if pallet is not None else None
    return bool(
        pallet is not None
        and str(getattr(pallet, "status", "") or "") in ACTIVE_FBS_PALLET_STATUSES
        and cell is not None
        and bool(getattr(cell, "is_active", False))
    )


def fbs_box_physical_location(box):
    """Resolve the warehouse place where an FBS box physically stands.

    A plan cell can be virtual and is only an internal grouping mechanism.  In
    that case the linked warehouse container remains the physical source of
    truth.  Real FBS/OS destinations keep their existing behaviour.
    """
    source_container = getattr(box, "source_container", None) if box is not None else None
    current_location = (
        getattr(source_container, "current_location", None)
        if source_container is not None
        else None
    )
    if current_location is not None and not is_virtual_fbs_plan_location(current_location):
        return current_location

    pallet = getattr(box, "pallet", None) if box is not None else None
    cell = getattr(pallet, "cell", None) if pallet is not None else None
    return getattr(cell, "location", None) if cell is not None else current_location


def fbs_box_physical_location_code(box) -> str:
    location = fbs_box_physical_location(box)
    if location is None:
        return ""
    coordinates = tuple(
        int(getattr(location, field, 0) or 0)
        for field in ("row_no", "section_no", "tier_no", "cell_no")
    )
    if all(value > 0 for value in coordinates):
        return os_location_code(
            row=coordinates[0],
            section=coordinates[1],
            tier=coordinates[2],
            cell=coordinates[3],
        )
    return str(
        getattr(location, "location_code", "")
        or getattr(location, "zone_code", "")
        or ""
    ).strip()


def fbs_box_physical_location_label(box) -> str:
    location = fbs_box_physical_location(box)
    if location is None:
        return "Место не указано"
    coordinates = tuple(
        int(getattr(location, field, 0) or 0)
        for field in ("row_no", "section_no", "tier_no", "cell_no")
    )
    if all(value > 0 for value in coordinates):
        return os_location_label(
            row=coordinates[0],
            section=coordinates[1],
            tier=coordinates[2],
            cell=coordinates[3],
        )
    code = fbs_box_physical_location_code(box)
    display_name = str(getattr(location, "display_name", "") or "").strip()
    if display_name and code and code.lower() not in display_name.lower():
        return f"{display_name} · {code}"
    return display_name or code or "Место не указано"


def fbs_box_physical_location_scan_values(box) -> set[str]:
    location = fbs_box_physical_location(box)
    if location is None:
        return set()
    values = {
        fbs_box_physical_location_code(box),
        str(getattr(location, "location_code", "") or "").strip(),
        str(getattr(location, "zone_code", "") or "").strip(),
    }
    return {value for value in values if value}
