from __future__ import annotations

from django.db import models

from sklad.models import (
    WarehouseContainer,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseOperationTask,
    WarehouseStockSnapshot,
)
from sklad.topology import os_location_code


FBS_STORAGE_CONTEXT_TYPE = "fbs_storage"
ACTIVE_FBS_PALLET_STATUSES = ("planned", "active")
ACTIVE_PUTAWAY_STATUSES = (
    WarehouseOperation.STATUS_CREATED,
    WarehouseOperation.STATUS_PLANNED,
    WarehouseOperation.STATUS_IN_PROGRESS,
    WarehouseOperation.STATUS_PARTIAL,
    WarehouseOperation.STATUS_BLOCKED,
)
ACTIVE_OPERATION_TASK_STATUSES = (
    WarehouseOperationTask.STATUS_CREATED,
    WarehouseOperationTask.STATUS_IN_PROGRESS,
)
NON_OCCUPYING_STOCK_STATES = (
    "processing_consumed",
    "shipped",
    "canceled",
)


def empty_planned_fbs_placeholder_containers():
    """Technical plan containers are not physical OS occupancy before driver scan."""
    return (
        WarehouseContainer.objects.filter(
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type=FBS_STORAGE_CONTEXT_TYPE,
            container_code__startswith="FBS-PAL-",
            current_location__zone_code__iexact="OS",
            fbs_pallet__status="planned",
        )
        .exclude(
            child_containers__status=WarehouseContainer.STATUS_ACTIVE,
        )
        .exclude(
            snapshots__is_archived=False,
            snapshots__qty__gt=0,
        )
        .exclude(
            child_snapshots__is_archived=False,
            child_snapshots__qty__gt=0,
        )
        .exclude(
            fbs_pallet__boxes__stock_balances__qty__gt=0,
        )
        .distinct()
    )


def active_os_putaway_operations():
    """OS reservations, excluding legacy FBS plans that have no physical pallet yet."""
    placeholder_ids = empty_planned_fbs_placeholder_containers().values("id")
    return WarehouseOperation.objects.filter(
        operation_type=WarehouseOperation.TYPE_PUTAWAY,
        status__in=ACTIVE_PUTAWAY_STATUSES,
        destination_location__zone_code__iexact="OS",
    ).exclude(
        fbs_replenishment_plan__target_pallet__warehouse_container_id__in=placeholder_ids,
    )


def active_fbs_storage_containers():
    return (
        WarehouseContainer.objects.filter(
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type=FBS_STORAGE_CONTEXT_TYPE,
            current_location__zone_code__iexact="OS",
            fbs_pallet__status__in=ACTIVE_FBS_PALLET_STATUSES,
        )
        .exclude(pk__in=empty_planned_fbs_placeholder_containers())
        .select_related("agency", "current_location", "fbs_pallet")
        .order_by("id")
    )


def fbs_storage_occupied_rows() -> list[dict]:
    rows: list[dict] = []
    for container in active_fbs_storage_containers():
        location = container.current_location
        if location is None:
            continue
        rows.append(
            {
                "row": int(location.row_no or 0),
                "section": int(location.section_no or 0),
                "tier": int(location.tier_no or 0),
                "cell": int(location.cell_no or 0),
                "agency_id": int(container.agency_id or 0),
                "container_id": int(container.id or 0),
                "container_code": str(container.container_code or "").strip(),
            }
        )
    return rows


def active_os_physical_containers():
    """Active physical containers, excluding unplaced FBS planning placeholders."""
    return (
        WarehouseContainer.objects.filter(
            status=WarehouseContainer.STATUS_ACTIVE,
            current_location__zone_code__iexact="OS",
        )
        .exclude(pk__in=empty_planned_fbs_placeholder_containers())
        .select_related("current_location")
        .order_by("id")
    )


def active_os_container_occupied_rows() -> list[dict]:
    rows: list[dict] = []
    containers = (
        active_os_physical_containers()
    )
    for container in containers:
        location = container.current_location
        if location is None:
            continue
        rows.append(
            {
                "row": int(location.row_no or 0),
                "section": int(location.section_no or 0),
                "tier": int(location.tier_no or 0),
                "cell": int(location.cell_no or 0),
                "agency_id": int(container.agency_id or 0),
                "container_id": int(container.id or 0),
                "container_code": str(container.container_code or "").strip(),
            }
        )
    return rows


def active_os_pallet_occupied_rows() -> list[dict]:
    """Compatibility alias for callers that use the old pallet-specific name."""
    return active_os_container_occupied_rows()


def fbs_storage_occupies_location(location: WarehouseLocation) -> bool:
    return active_fbs_storage_containers().filter(current_location=location).exists()


def shared_os_occupied_rows() -> list[dict]:
    rows = list(
        WarehouseStockSnapshot.objects.filter(
            location__zone_code__iexact="OS",
            zone_code__iexact="OS",
            is_archived=False,
            qty__gt=0,
        )
        .exclude(warehouse_state_code__in=NON_OCCUPYING_STOCK_STATES)
        .values(
            "location__row_no",
            "location__section_no",
            "location__tier_no",
            "location__cell_no",
            "agency_id",
        )
    )
    result = [
        {
            "row": int(row["location__row_no"] or 0),
            "section": int(row["location__section_no"] or 0),
            "tier": int(row["location__tier_no"] or 0),
            "cell": int(row["location__cell_no"] or 0),
            "agency_id": int(row["agency_id"] or 0),
        }
        for row in rows
    ]
    result.extend(active_os_container_occupied_rows())
    return result


def shared_os_occupied_location_ids() -> set[int]:
    """Returns existing OS locations that cannot accept another physical container."""
    location_ids = set(
        WarehouseStockSnapshot.objects.filter(
            location__zone_code__iexact="OS",
            zone_code__iexact="OS",
            is_archived=False,
            qty__gt=0,
        )
        .exclude(warehouse_state_code__in=NON_OCCUPYING_STOCK_STATES)
        .values_list("location_id", flat=True)
    )
    location_ids.update(
        active_fbs_storage_containers().values_list("current_location_id", flat=True)
    )
    location_ids.update(
        active_os_physical_containers().values_list("current_location_id", flat=True)
    )
    return {int(location_id) for location_id in location_ids if location_id}


def occupied_operational_location_ids(
    *,
    warehouse_code: str = "MSK",
    zone_code: str = "",
    exclude_context_type: str = "",
    exclude_context_id: str = "",
) -> set[int]:
    """Return physical places unavailable for a new receiving target.

    A place is occupied by live stock, an active physical container, or an
    unfinished operation whose destination is already that place.  The current
    receiving order may be excluded so that closing it remains idempotent after
    one of its pallets has already been materialized in PR.
    """

    location_filter = {"warehouse_code": str(warehouse_code or "MSK").strip()}
    normalized_zone = str(zone_code or "").strip().upper()
    if normalized_zone:
        location_filter["zone_code__iexact"] = normalized_zone

    context_type = str(exclude_context_type or "").strip()
    context_id = str(exclude_context_id or "").strip()

    snapshots = WarehouseStockSnapshot.objects.filter(
        location__isnull=False,
        location__is_active=True,
        location__in=WarehouseLocation.objects.filter(**location_filter),
        is_archived=False,
        qty__gt=0,
    ).exclude(warehouse_state_code__in=NON_OCCUPYING_STOCK_STATES)
    if context_type and context_id:
        snapshots = snapshots.exclude(
            source_context_type=context_type,
            source_context_id=context_id,
        )

    containers = WarehouseContainer.objects.filter(
        current_location__isnull=False,
        current_location__is_active=True,
        current_location__in=WarehouseLocation.objects.filter(**location_filter),
        status=WarehouseContainer.STATUS_ACTIVE,
    )
    if context_type and context_id:
        containers = containers.exclude(
            models.Q(
                source_context_type=context_type,
                source_context_id=context_id,
            )
            | models.Q(
                parent_container__source_context_type=context_type,
                parent_container__source_context_id=context_id,
            )
        )

    operations = WarehouseOperation.objects.filter(
        destination_location__isnull=False,
        destination_location__is_active=True,
        destination_location__in=WarehouseLocation.objects.filter(**location_filter),
        status__in=ACTIVE_PUTAWAY_STATUSES,
    )
    if context_type and context_id:
        operations = operations.exclude(
            context_type=context_type,
            context_id=context_id,
        )

    operation_tasks = WarehouseOperationTask.objects.filter(
        to_location__isnull=False,
        to_location__is_active=True,
        to_location__in=WarehouseLocation.objects.filter(**location_filter),
        status__in=ACTIVE_OPERATION_TASK_STATUSES,
    )
    if context_type and context_id:
        operation_tasks = operation_tasks.exclude(
            operation__context_type=context_type,
            operation__context_id=context_id,
        )

    result = set(snapshots.values_list("location_id", flat=True))
    result.update(containers.values_list("current_location_id", flat=True))
    result.update(operations.values_list("destination_location_id", flat=True))
    result.update(operation_tasks.values_list("to_location_id", flat=True))
    return {int(location_id) for location_id in result if location_id}


def operational_location_occupancy_message(
    location: WarehouseLocation,
    *,
    exclude_context_type: str = "",
    exclude_context_id: str = "",
) -> str:
    """Return an explicit blocker for a selected operational place."""

    occupied_ids = occupied_operational_location_ids(
        warehouse_code=location.warehouse_code,
        zone_code=location.zone_code,
        exclude_context_type=exclude_context_type,
        exclude_context_id=exclude_context_id,
    )
    if int(location.id or 0) not in occupied_ids:
        return ""
    label = str(
        location.location_code or location.display_name or location.zone_code or "место"
    ).strip()
    return f"Место {label} уже занято. Выберите другое свободное место."


def shared_os_occupied_keys() -> set[tuple[int, int, int, int]]:
    return {
        (
            int(row.get("row") or 0),
            int(row.get("section") or 0),
            int(row.get("tier") or 0),
            int(row.get("cell") or 0),
        )
        for row in shared_os_occupied_rows()
        if all(
            int(row.get(field) or 0)
            for field in ("row", "section", "tier", "cell")
        )
    }


def shared_os_section_agencies() -> dict[tuple[int, int], set[int]]:
    sections: dict[tuple[int, int], set[int]] = {}
    for row in shared_os_occupied_rows():
        row_no = int(row.get("row") or 0)
        section = int(row.get("section") or 0)
        agency_id = int(row.get("agency_id") or 0)
        if row_no and section and agency_id:
            sections.setdefault((row_no, section), set()).add(agency_id)
    return sections


def os_location_occupancy_message(
    location: WarehouseLocation,
    *,
    exclude_container_code: str = "",
    exclude_container_codes=(),
) -> str:
    if str(location.zone_code or "").strip().upper() != "OS":
        return ""
    location_label = os_location_code(
        row=location.row_no,
        section=location.section_no,
        tier=location.tier_no,
        cell=location.cell_no,
    )
    container_codes = {
        str(value or "").strip()
        for value in (exclude_container_code, *tuple(exclude_container_codes or ()))
        if str(value or "").strip()
    }
    snapshots = WarehouseStockSnapshot.objects.filter(
        location=location,
        zone_code__iexact="OS",
        is_archived=False,
        qty__gt=0,
    ).exclude(warehouse_state_code__in=NON_OCCUPYING_STOCK_STATES)
    if container_codes:
        allowed_snapshots = models.Q()
        for container_code in container_codes:
            allowed_snapshots |= (
                models.Q(container_code__iexact=container_code)
                | models.Q(container__container_code__iexact=container_code)
                | models.Q(parent_container__container_code__iexact=container_code)
            )
        snapshots = snapshots.exclude(allowed_snapshots)
    if snapshots.exists():
        return f"Место {location_label} уже занято складским остатком."

    fbs_containers = active_fbs_storage_containers().filter(current_location=location)
    if container_codes:
        allowed_fbs_containers = models.Q()
        for container_code in container_codes:
            allowed_fbs_containers |= models.Q(
                container_code__iexact=container_code,
            )
        fbs_containers = fbs_containers.exclude(allowed_fbs_containers)
    if fbs_containers.exists():
        return f"Место {location_label} уже занято физическим FBS-контейнером."
    return os_physical_container_occupancy_message(
        location,
        exclude_container_codes=container_codes,
    )


def os_physical_container_occupancy_message(
    location: WarehouseLocation,
    *,
    exclude_container_code: str = "",
    exclude_container_codes=(),
) -> str:
    """Share physical occupancy between inspection and transactional placement.

    Empty active pallets still occupy space.  Only an explicit archival or a
    completed movement may release their location; never clean up on a read.
    """
    if str(location.zone_code or "").strip().upper() != "OS":
        return ""
    location_label = os_location_code(
        row=location.row_no, section=location.section_no,
        tier=location.tier_no, cell=location.cell_no,
    )
    container_codes = {
        str(value or "").strip()
        for value in (exclude_container_code, *tuple(exclude_container_codes or ()))
        if str(value or "").strip()
    }
    physical_containers = active_os_physical_containers().filter(
        current_location=location,
    )
    if container_codes:
        allowed_physical_containers = models.Q()
        for container_code in container_codes:
            allowed_physical_containers |= (
                models.Q(container_code__iexact=container_code)
                | models.Q(parent_container__container_code__iexact=container_code)
            )
        physical_containers = physical_containers.exclude(
            allowed_physical_containers,
        )
    blockers = list(physical_containers.select_related("parent_container"))
    if blockers:
        codes = []
        for blocker in blockers:
            code = str(
                getattr(blocker.parent_container, "container_code", "")
                or blocker.container_code
                or ""
            ).strip()
            if code and code not in codes:
                codes.append(code)
        if codes:
            label = "контейнером" if len(codes) == 1 else "контейнерами"
            suffix = ", ".join(codes[:3])
            if len(codes) > 3:
                suffix += f" и ещё {len(codes) - 3}"
            return f"Место {location_label} уже занято {label}: {suffix}."
        return f"Место {location_label} уже занято физическим контейнером."
    return ""


def general_warehouse_location_occupancy_message(
    location: WarehouseLocation,
) -> str:
    """Return a blocker when a physical place is occupied outside FBS.

    FBS stock lives in its own balance tables, while ordinary warehouse stock
    lives in snapshots.  Containers are shared, so known FBS containers must be
    excluded explicitly to avoid blocking an existing rack binding with itself.
    """

    location_label = str(
        location.display_name or location.location_code or location.zone_code or "место"
    ).strip()
    snapshots = WarehouseStockSnapshot.objects.filter(
        location=location,
        is_archived=False,
        qty__gt=0,
    ).exclude(warehouse_state_code__in=NON_OCCUPYING_STOCK_STATES)
    if snapshots.exists():
        return f"Место {location_label} уже занято остатком общего склада."

    containers = WarehouseContainer.objects.filter(
        current_location=location,
        status=WarehouseContainer.STATUS_ACTIVE,
    ).exclude(
        models.Q(source_context_type__startswith="fbs_")
        | models.Q(parent_container__source_context_type__startswith="fbs_")
        | models.Q(fbs_pallet__isnull=False)
        | models.Q(fbs_box__isnull=False)
        | models.Q(parent_container__fbs_pallet__isnull=False)
    )
    if containers.exists():
        return f"Место {location_label} уже занято контейнером общего склада."

    return ""
