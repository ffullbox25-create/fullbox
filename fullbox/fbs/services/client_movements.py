from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Prefetch, Q, Sum
from django.utils import timezone

from fbs.exceptions import FbsReplenishmentError, FbsStorageError
from fbs.goods_types import (
    fbs_client_movement_source_stock_q,
    is_fbs_client_movement_source_stock,
    receiving_placement_allowed_snapshot_ids,
)
from fbs.models import (
    FbsBox,
    FbsClientMovementRequest,
    FbsClientMovementRequestLine,
    FbsPallet,
    FbsReplenishmentAllocation,
    FbsReplenishmentLine,
    FbsReplenishmentPlan,
    FbsReplenishmentPreparedBox,
    FbsStockBalance,
)
from fbs.signals import movement_completed, movement_warehouse_confirmed
from fbs.staging import ensure_movement_staging_container
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseLocation,
    WarehouseStockSnapshot,
)
from sklad.services.warehouse_transitions import WarehouseTransitionError
from sklad.services.warehouse_write_path import WarehouseWritePathService

from .inventory import validate_boxes_unlocked
from .storage import (
    ACTIVE_BOX_STATUSES,
    ACTIVE_PALLET_STATUSES,
    allocate_shared_os_fbs_pallet,
    allocate_shared_os_fbs_pallet_batch,
    attach_existing_warehouse_box_to_fbs,
)


ORDER_TYPE = "fbs_movement"
MANAGER_ROLES = {"manager", "head_manager", "director", "admin"}
WAREHOUSE_ROLES = {"storekeeper", "head_manager", "director", "admin"}
UNRESERVED_CLIENT_MOVEMENT_STATUSES = {
    FbsClientMovementRequest.STATUS_SUBMITTED,
    FbsClientMovementRequest.STATUS_APPROVED,
}
WAREHOUSE_SNAPSHOT_FIELDS = {
    field.name for field in WarehouseStockSnapshot._meta.concrete_fields
}
FULL_SOURCE_PALLET_COMMENT_PREFIX = "[client_full_pallet:"
RECEIVING_SOURCE_FILE_PREFIX = "receiving:"
SUPERSEDED_FULL_BOX_RECOVERY_MARKER = "[superseded_full_box_recovery]"
PHYSICAL_REPLACEMENT_PLAN_MARKER_RE = re.compile(
    r"\[replacement for plan (?P<plan_id>[1-9]\d*): [^\]\r\n]{1,80} physical scan\]"
)


def _is_superseded_full_box_recovery_plan(plan: FbsReplenishmentPlan) -> bool:
    return (
        plan.status == FbsReplenishmentPlan.STATUS_CANCELED
        and str(plan.comment or "").startswith(SUPERSEDED_FULL_BOX_RECOVERY_MARKER)
    )


def _client_movement_line_qty(
    plan: FbsReplenishmentPlan,
    *,
    moved: bool,
) -> dict[int, int]:
    quantities: dict[int, int] = defaultdict(int)
    for line in plan.lines.all():
        if not line.client_movement_line_id:
            return {}
        qty = int((line.qty_moved if moved else line.qty_planned) or 0)
        if qty <= 0:
            return {}
        quantities[int(line.client_movement_line_id)] += qty
    return dict(quantities)


def _completed_physical_replacement_source_plan_ids(
    plans: list[FbsReplenishmentPlan],
) -> set[int]:
    """Return canceled zero-move plans strictly covered by a completed replacement."""
    plans_by_id = {int(plan.id): plan for plan in plans}
    superseded_ids: set[int] = set()
    for replacement in plans:
        if replacement.status != FbsReplenishmentPlan.STATUS_DONE:
            continue
        match = PHYSICAL_REPLACEMENT_PLAN_MARKER_RE.search(
            str(replacement.comment or "")
        )
        if match is None:
            continue
        source = plans_by_id.get(int(match.group("plan_id")))
        if (
            source is None
            or source.status != FbsReplenishmentPlan.STATUS_CANCELED
            or int(source.moved_qty or 0) != 0
            or source.client_movement_request_id
            != replacement.client_movement_request_id
        ):
            continue
        source_quantities = _client_movement_line_qty(source, moved=False)
        if source_quantities and source_quantities == _client_movement_line_qty(
            replacement,
            moved=True,
        ):
            superseded_ids.add(int(source.id))
    return superseded_ids


def _movement_plan_queryset(request_row: FbsClientMovementRequest):
    return request_row.replenishment_plans.prefetch_related(
        Prefetch(
            "lines",
            queryset=FbsReplenishmentLine.objects.only(
                "id",
                "plan_id",
                "client_movement_line_id",
                "qty_planned",
                "qty_moved",
            ),
        )
    ).order_by("id")


def _snapshot_expiry_date(snapshot: WarehouseStockSnapshot):
    if "expiry_date" not in WAREHOUSE_SNAPSHOT_FIELDS:
        return None
    return getattr(snapshot, "expiry_date", None)


def _locked_fbs_staging_location(*, required_slots: int) -> WarehouseLocation:
    """Prefer an enabled PR place shared with FBS, preserving the legacy prep zone."""
    from sklad.services.operational_locations import select_operational_location

    location = select_operational_location(
        zone_code="PR",
        require_fbs=True,
        required_slots=max(int(required_slots or 0), 1),
        lock=True,
    )
    if location is not None:
        return location
    staging_zone = str(
        getattr(settings, "FBS_PREP_ZONE_CODE", "FBS-PREP") or "FBS-PREP"
    ).strip().upper()
    location = (
        WarehouseLocation.objects.select_for_update()
        .filter(zone_code__iexact=staging_zone, is_active=True)
        .order_by("warehouse_code", "location_code", "id")
        .first()
    )
    if location is None:
        raise FbsReplenishmentError(
            f"Не настроено активное место FBS в PR или зона подготовки {staging_zone}."
        )
    return location


@dataclass(frozen=True)
class ClientMovementApprovalResult:
    request: FbsClientMovementRequest
    plans: tuple[FbsReplenishmentPlan, ...]


@dataclass(frozen=True)
class BoxCandidateSummary:
    line_id: int
    barcode: str
    required_count: int
    available_count: int
    container_codes: tuple[str, ...]


@dataclass(frozen=True)
class EligibleSourceBox:
    container: WarehouseContainer
    barcode: str
    units_per_box: int


@dataclass(frozen=True)
class EligibleMixedSourceBox:
    container: WarehouseContainer
    snapshots: tuple[WarehouseStockSnapshot, ...]

    @property
    def total_qty(self) -> int:
        return sum(int(snapshot.qty or 0) for snapshot in self.snapshots)


def _authenticated_user(user):
    return user if getattr(user, "is_authenticated", False) else None


def _queue_client_movement_stock_export(request_row: FbsClientMovementRequest) -> None:
    from .stock_sync import queue_agency_stock_exports

    queue_agency_stock_exports(agency_id=request_row.agency_id)


def _activate_client_movement_stock_export(
    request_row: FbsClientMovementRequest,
    *,
    actor,
) -> None:
    from .stock_sync import (
        CLIENT_MOVEMENT_EXPORT_CONTEXT,
        CLIENT_MOVEMENT_EXPORT_EVENT,
    )

    event = (
        WarehouseEvent.objects.select_for_update()
        .filter(
            agency_id=request_row.agency_id,
            event_type=CLIENT_MOVEMENT_EXPORT_EVENT,
            stock_context_type=CLIENT_MOVEMENT_EXPORT_CONTEXT,
            stock_context_id=str(request_row.id),
        )
        .order_by("id")
        .first()
    )
    if event is None:
        WarehouseEvent.objects.create(
            agency_id=request_row.agency_id,
            event_type=CLIENT_MOVEMENT_EXPORT_EVENT,
            stock_context_type=CLIENT_MOVEMENT_EXPORT_CONTEXT,
            stock_context_id=str(request_row.id),
            source_document_type=ORDER_TYPE,
            source_document_id=str(request_row.id),
            qty=int(request_row.requested_qty or 0),
            payload={
                "request_id": request_row.id,
                "request_number": request_row.number,
                "warehouse_confirmed_at": (
                    request_row.warehouse_confirmed_at.isoformat()
                    if request_row.warehouse_confirmed_at
                    else ""
                ),
            },
            performed_by=actor,
            performed_by_role=_actor_role(actor),
            occurred_at=timezone.now(),
        )
    _queue_client_movement_stock_export(request_row)


def _release_client_movement_stock_after_warehouse_confirmation(
    request_row: FbsClientMovementRequest,
    *,
    actor,
    now,
) -> int:
    """Make physically posted stock available only after the storekeeper signs off."""
    from .replenishment import (
        CLIENT_MOVEMENT_PENDING_CONFIRMATION_KEY,
        CLIENT_MOVEMENT_PENDING_QTY_KEY,
        CONTEXT_TYPE,
    )

    plan_ids = tuple(request_row.replenishment_plans.values_list("id", flat=True))
    if not plan_ids:
        return 0
    events = list(
        WarehouseEvent.objects.select_for_update()
        .filter(
            agency_id=request_row.agency_id,
            event_type="fbs_replenishment_completed",
            stock_context_type=CONTEXT_TYPE,
            stock_context_id__in=tuple(str(value) for value in plan_ids),
            **{f"payload__{CLIENT_MOVEMENT_PENDING_CONFIRMATION_KEY}": True},
        )
        .order_by("id")
    )
    release_by_balance: dict[int, int] = defaultdict(int)
    event_release_qty: dict[int, int] = {}
    for event in events:
        payload = dict(event.payload or {})
        balance_id = int(payload.get("target_balance_id") or 0)
        qty = int(payload.get(CLIENT_MOVEMENT_PENDING_QTY_KEY) or 0)
        if balance_id <= 0 or qty <= 0:
            raise FbsReplenishmentError(
                "Не удалось определить ожидающий подтверждения FBS-остаток."
            )
        release_by_balance[balance_id] += qty
        event_release_qty[event.id] = qty

    balances = {
        balance.id: balance
        for balance in FbsStockBalance.objects.select_for_update().filter(
            id__in=release_by_balance
        )
    }
    if len(balances) != len(release_by_balance):
        raise FbsReplenishmentError(
            "Часть ожидающего подтверждения FBS-остатка не найдена."
        )
    for balance_id, qty in release_by_balance.items():
        balance = balances[balance_id]
        unavailable_qty = (
            int(balance.qty or 0)
            - int(balance.available_qty or 0)
            - int(balance.reserved_qty or 0)
            - int(balance.external_reserved_qty or 0)
        )
        if unavailable_qty < qty:
            raise FbsReplenishmentError(
                "Ожидающий подтверждения FBS-остаток уже был использован."
            )
        balance.available_qty = int(balance.available_qty or 0) + qty
        balance.save(update_fields=["available_qty", "updated_at"])

    released_by_id = getattr(actor, "id", None)
    for event in events:
        payload = dict(event.payload or {})
        payload[CLIENT_MOVEMENT_PENDING_CONFIRMATION_KEY] = False
        payload["client_movement_warehouse_confirmed_at"] = now.isoformat()
        payload["client_movement_warehouse_confirmed_by"] = released_by_id
        payload["client_movement_released_qty"] = event_release_qty[event.id]
        event.payload = payload
    if events:
        WarehouseEvent.objects.bulk_update(events, ["payload"])
    return sum(release_by_balance.values())


def _actor_role(user) -> str:
    actor = _authenticated_user(user)
    if actor is None:
        return ""
    if getattr(actor, "is_superuser", False):
        return "admin"
    from employees.models import Employee

    return str(
        Employee.objects.filter(user=actor, is_active=True)
        .order_by("id")
        .values_list("role", flat=True)
        .first()
        or ""
    )


def _require_actor_role(user, allowed_roles: set[str], message: str):
    actor = _authenticated_user(user)
    if actor is None or _actor_role(actor) not in allowed_roles:
        raise FbsReplenishmentError(message)
    return actor


def _log_request_status(request_row: FbsClientMovementRequest, *, user=None, description: str):
    from audit.models import log_order_action

    log_order_action(
        "status",
        order_id=request_row.number,
        order_type=ORDER_TYPE,
        user=_authenticated_user(user),
        agency=request_row.agency,
        description=description,
        payload={
            "status": request_row.status,
            "status_label": request_row.get_status_display(),
            "request_id": request_row.id,
            "mode": request_row.mode,
            "requested_qty": request_row.requested_qty,
            "requested_box_count": request_row.requested_box_count,
            "requested_mixed_box_count": request_row.requested_mixed_box_count,
            "actual_moved_qty": request_row.actual_moved_qty,
            "actual_moved_box_count": request_row.actual_moved_box_count,
            "clarification_reason": request_row.clarification_reason,
        },
    )


def _fbs_zone_code() -> str:
    return str(getattr(settings, "FBS_ZONE_CODE", "FBS") or "FBS").strip().upper()


def eligible_source_boxes(
    *,
    agency_id: int,
    barcodes,
    units_per_box: int | None = None,
    lock: bool = False,
) -> list[EligibleSourceBox]:
    normalized_barcodes = {
        str(value or "").strip()
        for value in barcodes
        if str(value or "").strip()
    }
    if not normalized_barcodes:
        return []
    open_container_ids = FbsReplenishmentLine.objects.filter(
        source_container__isnull=False,
        status__in=(
            FbsReplenishmentLine.STATUS_PROPOSED,
            FbsReplenishmentLine.STATUS_RESERVED,
            FbsReplenishmentLine.STATUS_IN_PROGRESS,
        ),
    ).values_list("source_container_id", flat=True)
    positive_snapshots = WarehouseStockSnapshot.objects.filter(
        is_archived=False,
        qty__gt=0,
    ).select_related("location", "sku_ref", "parent_container")
    matching_snapshots = WarehouseStockSnapshot.objects.filter(
        container_id=OuterRef("pk"),
        barcode__in=normalized_barcodes,
        is_archived=False,
        qty__gt=0,
    ).filter(fbs_client_movement_source_stock_q(agency_id=agency_id))
    containers = (
        WarehouseContainer.objects.filter(
            agency_id=agency_id,
            container_type=WarehouseContainer.TYPE_BOX,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        .annotate(has_matching_stock=Exists(matching_snapshots))
        .filter(has_matching_stock=True)
        .exclude(pk__in=open_container_ids)
        .exclude(fbs_box__isnull=False)
        .select_related("current_location", "parent_container__current_location")
        .prefetch_related(Prefetch("snapshots", queryset=positive_snapshots, to_attr="movement_stock"))
        .order_by("id")
    )
    if lock:
        containers = containers.select_for_update(of=("self",))
    eligible: list[EligibleSourceBox] = []
    container_rows = list(containers)
    receiving_allowed_ids = receiving_placement_allowed_snapshot_ids(
        snapshot for container in container_rows for snapshot in container.movement_stock
    )
    shipping_blocked_ids = WarehouseWritePathService.shipping_reserved_snapshot_ids(
        agency=agency_id,
        snapshots=[
            snapshot
            for container in container_rows
            for snapshot in container.movement_stock
        ],
    )
    for container in container_rows:
        snapshots = list(container.movement_stock)
        if not snapshots:
            continue
        barcode = str(snapshots[0].barcode or "").strip()
        exact = all(
            snapshot.agency_id == agency_id
            and str(snapshot.barcode or "").strip() == barcode
            and barcode in normalized_barcodes
            and is_fbs_client_movement_source_stock(
                snapshot=snapshot,
                receiving_allowed_ids=receiving_allowed_ids,
                goods_type=snapshot.goods_type,
                zone_kind=snapshot.zone_kind,
                warehouse_state_code=snapshot.warehouse_state_code,
                container_type=container.container_type,
                container_code=container.container_code,
                container_status=container.status,
            )
            and int(snapshot.available_qty or 0) == int(snapshot.qty or 0)
            and int(snapshot.processing_reserved_qty or 0) == 0
            and int(snapshot.shipping_reserved_qty or 0) == 0
            and int(snapshot.other_reserved_qty or 0) == 0
            and snapshot.active_operation_id is None
            and not snapshot.is_in_vehicle
            and str(snapshot.zone_code or "").strip().upper() != _fbs_zone_code()
            and (
                _snapshot_expiry_date(snapshot) is None
                or _snapshot_expiry_date(snapshot) >= today
            )
            for snapshot in snapshots
        )
        if not exact:
            continue
        if any(int(snapshot.id) in shipping_blocked_ids for snapshot in snapshots):
            continue
        container_qty = sum(int(snapshot.qty or 0) for snapshot in snapshots)
        if units_per_box is not None and container_qty != int(units_per_box):
            continue
        eligible.append(
            EligibleSourceBox(
                container=container,
                barcode=barcode,
                units_per_box=container_qty,
            )
        )

    def source_group_key(candidate: EligibleSourceBox) -> tuple[str, int]:
        container = candidate.container
        parent = getattr(container, "parent_container", None)
        if (
            parent is not None
            and parent.container_type
            in {
                WarehouseContainer.TYPE_PALLET,
                WarehouseContainer.TYPE_MIXED_PALLET,
            }
            and (
                not container.current_location_id
                or int(parent.current_location_id or 0)
                == int(container.current_location_id or 0)
            )
        ):
            return ("pallet", int(parent.id))
        return ("box", int(container.id))

    boxes_per_source = Counter(source_group_key(candidate) for candidate in eligible)

    def source_route_key(candidate: EligibleSourceBox) -> tuple:
        container = candidate.container
        parent = getattr(container, "parent_container", None)
        grouped_on_parent = source_group_key(candidate)[0] == "pallet"
        source = parent if grouped_on_parent else container
        location = getattr(source, "current_location", None)
        return (
            -int(boxes_per_source[source_group_key(candidate)]),
            int(getattr(location, "section_no", 0) or 0),
            int(getattr(location, "row_no", 0) or 0),
            int(getattr(location, "tier_no", 0) or 0),
            int(getattr(location, "cell_no", 0) or 0),
            str(getattr(source, "container_code", "") or ""),
            str(container.container_code or ""),
            int(container.id),
        )

    # FBS client movements are selected by physical box multiplicity and source
    # pallet concentration.  FIFO/FEFO must not split one SKU across several
    # pallets merely because one of the boxes was received earlier.
    return sorted(eligible, key=source_route_key)


def _select_exact_box_combination(
    candidates: list[EligibleSourceBox],
    *,
    requested_box_count: int,
    requested_qty: int,
) -> list[EligibleSourceBox]:
    """Choose whole boxes whose count and total quantity both match the request."""
    requested_box_count = int(requested_box_count or 0)
    requested_qty = int(requested_qty or 0)
    if requested_box_count <= 0 or requested_qty <= 0:
        return []

    states: list[dict[int, tuple[EligibleSourceBox, ...]]] = [
        {} for _ in range(requested_box_count + 1)
    ]
    states[0][0] = ()
    for candidate in candidates:
        units = int(candidate.units_per_box or 0)
        if units <= 0 or units > requested_qty:
            continue
        for used_count in range(requested_box_count, 0, -1):
            previous_states = list(states[used_count - 1].items())
            for previous_qty, selected in previous_states:
                total_qty = previous_qty + units
                if total_qty > requested_qty or total_qty in states[used_count]:
                    continue
                states[used_count][total_qty] = (*selected, candidate)

    return list(states[requested_box_count].get(requested_qty, ()))


def eligible_source_boxes_for_line(
    line: FbsClientMovementRequestLine,
    *,
    lock: bool = False,
) -> list[EligibleSourceBox]:
    """Return whole source boxes matching a fixed or mixed-size request line."""
    requested_qty = int(line.requested_qty or 0)
    requested_box_count = int(line.requested_box_count or 0)
    units_per_box = int(line.units_per_box or 0)
    mixed_size_plan = (
        requested_box_count > 0
        and requested_qty != requested_box_count * units_per_box
    )
    candidates = eligible_source_boxes(
        agency_id=line.request.agency_id,
        barcodes=(line.barcode,),
        units_per_box=None if mixed_size_plan else units_per_box,
        lock=lock,
    )
    candidates = [
        candidate
        for candidate in candidates
        if candidate.container.movement_stock
        and all(
            int(snapshot.sku_ref_id or 0) == int(line.sku_id or 0)
            for snapshot in candidate.container.movement_stock
        )
    ]
    if not mixed_size_plan:
        return candidates
    return _select_exact_box_combination(
        candidates,
        requested_box_count=requested_box_count,
        requested_qty=requested_qty,
    )


def eligible_mixed_source_boxes(
    *,
    agency_id: int,
    container_codes=None,
    lock: bool = False,
) -> list[EligibleMixedSourceBox]:
    normalized_codes = {
        str(value or "").strip()
        for value in (container_codes or [])
        if str(value or "").strip()
    }
    open_container_ids = FbsReplenishmentLine.objects.filter(
        source_container__isnull=False,
        status__in=(
            FbsReplenishmentLine.STATUS_PROPOSED,
            FbsReplenishmentLine.STATUS_RESERVED,
            FbsReplenishmentLine.STATUS_IN_PROGRESS,
        ),
    ).values_list("source_container_id", flat=True)
    positive_snapshots = (
        WarehouseStockSnapshot.objects.filter(is_archived=False, qty__gt=0)
        .select_related("location", "sku_ref", "parent_container")
        .order_by("id")
    )
    containers = WarehouseContainer.objects.filter(
        agency_id=agency_id,
        container_type=WarehouseContainer.TYPE_BOX,
        status=WarehouseContainer.STATUS_ACTIVE,
    )
    if normalized_codes:
        containers = containers.filter(container_code__in=normalized_codes)
    if not lock:
        containers = containers.annotate(
            positive_barcode_count=Count(
                "snapshots__barcode",
                distinct=True,
                filter=Q(
                    snapshots__is_archived=False,
                    snapshots__qty__gt=0,
                ),
            )
        ).filter(positive_barcode_count__gte=2)
    containers = (
        containers
        .exclude(pk__in=open_container_ids)
        .exclude(fbs_box__isnull=False)
        .prefetch_related(
            Prefetch("snapshots", queryset=positive_snapshots, to_attr="movement_stock")
        )
        .order_by("created_at", "id")
    )
    if lock:
        containers = containers.select_for_update(of=("self",))
    today = timezone.localdate()
    eligible: list[tuple[tuple, EligibleMixedSourceBox]] = []
    container_rows = list(containers)
    receiving_allowed_ids = receiving_placement_allowed_snapshot_ids(
        snapshot for container in container_rows for snapshot in container.movement_stock
    )
    shipping_blocked_ids = WarehouseWritePathService.shipping_reserved_snapshot_ids(
        agency=agency_id,
        snapshots=[
            snapshot
            for container in container_rows
            for snapshot in container.movement_stock
        ],
    )
    for container in container_rows:
        snapshots = tuple(container.movement_stock)
        barcodes = {
            str(snapshot.barcode or "").strip()
            for snapshot in snapshots
            if str(snapshot.barcode or "").strip()
        }
        if len(barcodes) < 2:
            continue
        exact = all(
            snapshot.agency_id == agency_id
            and str(snapshot.barcode or "").strip() in barcodes
            and is_fbs_client_movement_source_stock(
                snapshot=snapshot,
                receiving_allowed_ids=receiving_allowed_ids,
                goods_type=snapshot.goods_type,
                zone_kind=snapshot.zone_kind,
                warehouse_state_code=snapshot.warehouse_state_code,
                container_type=container.container_type,
                container_code=container.container_code,
                container_status=container.status,
            )
            and int(snapshot.available_qty or 0) == int(snapshot.qty or 0)
            and int(snapshot.processing_reserved_qty or 0) == 0
            and int(snapshot.shipping_reserved_qty or 0) == 0
            and int(snapshot.other_reserved_qty or 0) == 0
            and snapshot.active_operation_id is None
            and not snapshot.is_in_vehicle
            and str(snapshot.zone_code or "").strip().upper() != _fbs_zone_code()
            and (
                _snapshot_expiry_date(snapshot) is None
                or _snapshot_expiry_date(snapshot) >= today
            )
            for snapshot in snapshots
        )
        if not snapshots or not exact:
            continue
        if any(int(snapshot.id) in shipping_blocked_ids for snapshot in snapshots):
            continue
        first_expiry = min(
            (
                _snapshot_expiry_date(snapshot)
                for snapshot in snapshots
                if _snapshot_expiry_date(snapshot)
            ),
            default=None,
        )
        eligible.append(
            (
                (
                    first_expiry is None,
                    first_expiry or today,
                    container.created_at,
                    container.id,
                ),
                EligibleMixedSourceBox(
                    container=container,
                    snapshots=snapshots,
                ),
            )
        )
    return [candidate for _, candidate in sorted(eligible, key=lambda item: item[0])]


def _eligible_box_candidates(
    line: FbsClientMovementRequestLine,
    *,
    lock: bool = False,
) -> list[WarehouseContainer]:
    return [
        candidate.container
        for candidate in eligible_source_boxes_for_line(line, lock=lock)
    ]


def _hard_reserve_allocations(
    request_row: FbsClientMovementRequest,
    *,
    lock: bool,
) -> list[dict]:
    try:
        allocations = WarehouseWritePathService.fbs_movement_reserve_allocations(
            agency=request_row.agency,
            request_id=request_row.id,
            lock=lock,
        )
    except WarehouseTransitionError as exc:
        raise FbsReplenishmentError(str(exc)) from exc
    if not allocations:
        raise FbsReplenishmentError(
            "По новой FBS-заявке отсутствует отдельный складской резерв."
        )
    expected_by_line = {
        line.id: int(line.requested_qty or 0)
        for line in request_row.lines.only("id", "requested_qty")
    }
    reserved_by_line: dict[int, int] = {}
    for row in allocations:
        line_id = int(row.get("request_line_id") or 0)
        reserved_by_line[line_id] = reserved_by_line.get(line_id, 0) + int(
            row.get("qty") or 0
        )
    if reserved_by_line != expected_by_line:
        raise FbsReplenishmentError(
            "Отдельный складской резерв не совпадает с составом FBS-заявки."
        )
    return allocations


def _release_hard_reserve(
    request_row: FbsClientMovementRequest,
    *,
    actor,
    reason: str,
) -> list[dict]:
    try:
        return WarehouseWritePathService.release_fbs_movement_reserves(
            agency=request_row.agency,
            request_id=request_row.id,
            released_by=actor,
            reason=reason,
        )
    except WarehouseTransitionError as exc:
        raise FbsReplenishmentError(str(exc)) from exc


def _available_box_options_text(
    *,
    agency_id: int,
    barcode: str,
    lock: bool = False,
) -> str:
    counts: dict[int, int] = {}
    for candidate in eligible_source_boxes(
        agency_id=agency_id,
        barcodes=(barcode,),
        lock=lock,
    ):
        counts[candidate.units_per_box] = counts.get(candidate.units_per_box, 0) + 1
    if not counts:
        return "целых свободных коробов нет"
    return ", ".join(
        f"{units} шт. × {count} кор."
        for units, count in sorted(counts.items())
    )


def _client_movement_source_stock_queryset(*, agency_id: int):
    queryset = WarehouseStockSnapshot.objects.filter(
        agency_id=agency_id,
        is_archived=False,
        is_in_vehicle=False,
        qty__gt=0,
    ).exclude(zone_code__iexact=_fbs_zone_code())
    if "expiry_date" in WAREHOUSE_SNAPSHOT_FIELDS:
        queryset = queryset.filter(
            Q(expiry_date__isnull=True) | Q(expiry_date__gte=timezone.localdate())
        )
    return queryset.filter(
        fbs_client_movement_source_stock_q(agency_id=agency_id),
        active_operation__isnull=True,
    )


def _box_fallback_stock_context(
    *,
    request_row: FbsClientMovementRequest,
    barcodes,
    lock: bool = False,
) -> tuple[dict[str, int], dict[str, dict[int, int]]]:
    normalized_barcodes = [
        str(value or "").strip() for value in barcodes if str(value or "").strip()
    ]
    if not normalized_barcodes:
        return {}, {}
    stock_queryset = _client_movement_source_stock_queryset(
        agency_id=request_row.agency_id
    ).filter(barcode__in=normalized_barcodes)
    if lock:
        list(stock_queryset.select_for_update().values_list("id", flat=True))
    candidate_snapshots = list(
        stock_queryset.select_related("container__parent_container", "parent_container")
    )
    shipping_blocked_ids = WarehouseWritePathService.shipping_reserved_snapshot_ids(
        agency=request_row.agency,
        snapshots=candidate_snapshots,
    )
    if shipping_blocked_ids:
        stock_queryset = stock_queryset.exclude(id__in=shipping_blocked_ids)
    warehouse_available_by_barcode = {
        str(row["barcode"]): int(row["total"] or 0)
        for row in stock_queryset.values("barcode").annotate(total=Sum("available_qty"))
    }
    pending_by_barcode = {
        str(row["barcode"]): int(row["total"] or 0)
        for row in FbsClientMovementRequestLine.objects.filter(
            request__agency_id=request_row.agency_id,
            request__status__in=UNRESERVED_CLIENT_MOVEMENT_STATUSES,
            request__idempotency_key="",
            barcode__in=normalized_barcodes,
        )
        .exclude(request_id=request_row.id)
        .values("barcode")
        .annotate(total=Sum("requested_qty"))
    }
    client_available_by_barcode = {
        barcode: max(
            int(warehouse_available_by_barcode.get(barcode, 0))
            - int(pending_by_barcode.get(barcode, 0)),
            0,
        )
        for barcode in normalized_barcodes
    }
    box_size_counts: dict[str, dict[int, int]] = defaultdict(dict)
    rows = (
        stock_queryset.filter(container_id__isnull=False, available_qty__gt=0)
        .values("barcode", "container_id")
        .annotate(
            units_per_box=Sum("qty"),
            available_in_container=Sum("available_qty"),
        )
    )
    for row in rows:
        barcode = str(row["barcode"] or "").strip()
        units_per_box = int(row["units_per_box"] or 0)
        available_in_container = int(row["available_in_container"] or 0)
        if not barcode or units_per_box <= 0 or available_in_container <= 0:
            continue
        box_size_counts.setdefault(barcode, {})[units_per_box] = (
            box_size_counts.get(barcode, {}).get(units_per_box, 0) + 1
        )
    return client_available_by_barcode, box_size_counts


def _validate_box_fallback_stock(
    *,
    request_row: FbsClientMovementRequest,
    lines,
) -> None:
    client_available_by_barcode, box_size_counts = _box_fallback_stock_context(
        request_row=request_row,
        barcodes=[line.barcode for line in lines],
        lock=True,
    )
    errors: list[str] = []
    for line in lines:
        barcode = str(line.barcode or "").strip()
        units_per_box = int(line.units_per_box or 0)
        requested_qty = int(line.requested_qty or 0)
        requested_box_count = int(line.requested_box_count or 0)
        known_box_count = int(box_size_counts.get(barcode, {}).get(units_per_box, 0))
        if known_box_count <= 0:
            errors.append(
                f"По ШК {barcode} кратность {units_per_box} шт. не найдена "
                "среди доступных складских коробов."
            )
            continue
        client_available = int(client_available_by_barcode.get(barcode, 0))
        available_box_count = client_available // units_per_box if units_per_box else 0
        if client_available < requested_qty or available_box_count < requested_box_count:
            errors.append(
                f"По ШК {barcode} целых коробов по {units_per_box} шт. из доступного "
                f"остатка доступно {available_box_count}, запрошено "
                f"{requested_box_count}."
            )
    if errors:
        raise FbsReplenishmentError(" ".join(errors))


def _hard_reserve_can_attach_exact_boxes(
    *,
    request_row: FbsClientMovementRequest,
    lines,
    allocations: list[dict],
) -> bool:
    del lines
    allocations_by_container: dict[int, dict[int, int]] = defaultdict(dict)
    for row in allocations:
        container_id = int(row.get("container_id") or 0)
        snapshot_id = int(row.get("snapshot_id") or 0)
        qty = int(row.get("qty") or 0)
        if container_id <= 0 or snapshot_id <= 0 or qty <= 0:
            return False
        if snapshot_id in allocations_by_container[container_id]:
            return False
        allocations_by_container[container_id][snapshot_id] = qty
    from sklad.models import WarehouseEvent
    from sklad.services.fbs_quantity_reserves import EVENT
    quantity_planned = WarehouseEvent.objects.filter(event_type=EVENT,
        stock_context_type='fbs_client_movement',stock_context_id=str(request_row.id)).exists()
    if not quantity_planned and len(allocations_by_container) != int(request_row.requested_box_count or 0):
        return False
    containers = list(
        WarehouseContainer.objects.filter(
            id__in=allocations_by_container,
            agency=request_row.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            status=WarehouseContainer.STATUS_ACTIVE,
        ).exclude(fbs_box__isnull=False)
    )
    if len(containers) != len(allocations_by_container):
        return False
    snapshots_by_container: dict[int, dict[int, int]] = defaultdict(dict)
    for snapshot in WarehouseStockSnapshot.objects.filter(
        container_id__in=allocations_by_container,
        is_archived=False,
        qty__gt=0,
    ).only("id", "container_id", "qty"):
        snapshots_by_container[int(snapshot.container_id)][snapshot.id] = int(
            snapshot.qty or 0
        )
    for container_id, reserved in allocations_by_container.items():
        if snapshots_by_container.get(container_id, {}) != reserved:
            return False
    return True


def _hard_reserve_full_box_container_ids(
    *,
    request_row: FbsClientMovementRequest,
    allocations: list[dict],
) -> set[int]:
    allocations_by_container: dict[int, dict[int, int]] = defaultdict(dict)
    for row in allocations:
        container_id = int(row.get("container_id") or 0)
        snapshot_id = int(row.get("snapshot_id") or 0)
        qty = int(row.get("qty") or 0)
        if container_id <= 0 or snapshot_id <= 0 or qty <= 0:
            continue
        if snapshot_id in allocations_by_container[container_id]:
            allocations_by_container.pop(container_id, None)
            continue
        allocations_by_container[container_id][snapshot_id] = qty
    if not allocations_by_container:
        return set()
    eligible_container_ids = set(
        WarehouseContainer.objects.filter(
            id__in=allocations_by_container,
            agency=request_row.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        .exclude(fbs_box__isnull=False)
        .values_list("id", flat=True)
    )
    snapshots_by_container: dict[int, dict[int, int]] = defaultdict(dict)
    for snapshot in WarehouseStockSnapshot.objects.filter(
        container_id__in=eligible_container_ids,
        is_archived=False,
        qty__gt=0,
    ).only("id", "container_id", "qty"):
        snapshots_by_container[int(snapshot.container_id)][snapshot.id] = int(
            snapshot.qty or 0
        )
    return {
        container_id
        for container_id, reserved in allocations_by_container.items()
        if container_id in eligible_container_ids
        and snapshots_by_container.get(container_id, {}) == reserved
    }


def _full_source_pallet_by_box(
    *,
    request_row: FbsClientMovementRequest,
    containers: list[WarehouseContainer],
) -> dict[int, WarehouseContainer]:
    """Returns only pallets whose complete active physical box set is selected."""
    selected_by_parent: dict[int, set[int]] = defaultdict(set)
    for container in containers:
        if container.parent_container_id:
            selected_by_parent[int(container.parent_container_id)].add(container.id)
    if not selected_by_parent:
        return {}

    parents = {
        pallet.id: pallet
        for pallet in WarehouseContainer.objects.select_for_update()
        .filter(
            id__in=selected_by_parent,
            agency=request_row.agency,
            status=WarehouseContainer.STATUS_ACTIVE,
            container_type__in=(
                WarehouseContainer.TYPE_PALLET,
                WarehouseContainer.TYPE_MIXED_PALLET,
            ),
        )
        .order_by("id")
    }
    active_children_by_parent: dict[int, set[int]] = defaultdict(set)
    for child in (
        WarehouseContainer.objects.select_for_update()
        .filter(
            parent_container_id__in=parents,
            agency=request_row.agency,
            status=WarehouseContainer.STATUS_ACTIVE,
            container_type=WarehouseContainer.TYPE_BOX,
        )
        .only("id", "parent_container_id", "current_location_id")
        .order_by("id")
    ):
        parent = parents[int(child.parent_container_id)]
        if int(child.current_location_id or 0) != int(parent.current_location_id or 0):
            continue
        active_children_by_parent[int(child.parent_container_id)].add(child.id)

    result: dict[int, WarehouseContainer] = {}
    for parent_id, selected_ids in selected_by_parent.items():
        parent = parents.get(parent_id)
        if (
            parent is None
            or not selected_ids
            or selected_ids != active_children_by_parent.get(parent_id, set())
        ):
            continue
        for box_id in selected_ids:
            result[box_id] = parent
    return result


def _full_source_pallet_comment(parent_id: int, comment: str) -> str:
    return (
        f"{FULL_SOURCE_PALLET_COMMENT_PREFIX}{int(parent_id)}] "
        f"{str(comment or '').strip()}"
    ).strip()


def movement_box_candidate_summaries(
    request_row: FbsClientMovementRequest,
) -> tuple[BoxCandidateSummary, ...]:
    if request_row.mode != FbsClientMovementRequest.MODE_BOX:
        return ()
    reserved_codes_by_line: dict[int, list[str]] = {}
    if request_row.uses_hard_reserve:
        allocations = _hard_reserve_allocations(request_row, lock=False)
        if allocations and allocations[0].get('reserve_scope') == 'quantity':
            # Suggestions only. No source box is fixed before warehouse planning.
            for line in request_row.lines.all():
                reserved_codes_by_line[line.id] = [c.container_code for c in _eligible_box_candidates(line)]
        for row in allocations:
            code = str(row.get("container_code") or "").strip()
            line_codes = reserved_codes_by_line.setdefault(
                int(row["request_line_id"]), []
            )
            if code and code not in line_codes:
                line_codes.append(code)
    summaries = []
    for line in request_row.lines.select_related("request").order_by("id"):
        candidates = _eligible_box_candidates(line) if not request_row.uses_hard_reserve else []
        reserved_codes = reserved_codes_by_line.get(line.id, [])
        summaries.append(
            BoxCandidateSummary(
                line_id=line.id,
                barcode=line.barcode,
                required_count=line.requested_box_count,
                available_count=(len(reserved_codes) if request_row.uses_hard_reserve else len(candidates)),
                container_codes=(
                    tuple(reserved_codes)
                    if request_row.uses_hard_reserve
                    else tuple(
                        container.container_code
                        for container in candidates[: line.requested_box_count]
                    )
                ),
            )
        )
    return tuple(summaries)


@transaction.atomic
def sync_client_movement_request_status(
    request_id: int, *, performed_by=None
) -> FbsClientMovementRequest:
    request_row = (
        FbsClientMovementRequest.objects.select_for_update()
        .select_related("agency")
        .get(pk=request_id)
    )
    if request_row.status == FbsClientMovementRequest.STATUS_COMPLETED:
        return request_row
    all_plans = list(_movement_plan_queryset(request_row))
    physical_replacement_source_ids = (
        _completed_physical_replacement_source_plan_ids(all_plans)
    )
    plans = [
        plan
        for plan in all_plans
        if not _is_superseded_full_box_recovery_plan(plan)
        and plan.id not in physical_replacement_source_ids
    ]
    if not plans or request_row.status == FbsClientMovementRequest.STATUS_REJECTED:
        return request_row

    statuses = {plan.status for plan in plans}
    missing_box_partial = bool(statuses) and statuses <= {
        FbsReplenishmentPlan.STATUS_DONE,
        FbsReplenishmentPlan.STATUS_CANCELED,
    } and FbsReplenishmentPlan.STATUS_CANCELED in statuses and all(
        plan.status != FbsReplenishmentPlan.STATUS_CANCELED
        or str(plan.comment or "").startswith("[missing_box_partial]")
        for plan in plans
    )
    if statuses == {FbsReplenishmentPlan.STATUS_DONE} or missing_box_partial:
        status = FbsClientMovementRequest.STATUS_MOVED
        completed_at = None
    elif statuses == {FbsReplenishmentPlan.STATUS_CANCELED}:
        status = FbsClientMovementRequest.STATUS_CANCELED
        completed_at = None
    elif any(
        plan.assigned_to_id
        or plan.status
        in {
            FbsReplenishmentPlan.STATUS_IN_PROGRESS,
            FbsReplenishmentPlan.STATUS_AWAITING_PACK,
            FbsReplenishmentPlan.STATUS_DONE,
        }
        for plan in plans
    ):
        status = FbsClientMovementRequest.STATUS_IN_PROGRESS
        completed_at = None
    else:
        status = FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED
        completed_at = None

    if request_row.status != status or request_row.completed_at != completed_at:
        request_row.status = status
        request_row.completed_at = completed_at
        request_row.save(update_fields=["status", "completed_at", "updated_at"])
        _log_request_status(
            request_row,
            user=performed_by,
            description=f"Статус FBS-перемещения: {request_row.get_status_display()}",
        )
    return request_row


@transaction.atomic
def approve_client_movement_by_manager(
    *, request_id: int, reviewed_by=None
) -> FbsClientMovementRequest:
    actor = _require_actor_role(
        reviewed_by,
        MANAGER_ROLES,
        "Подтвердить клиентскую заявку может только менеджер.",
    )
    request_row = (
        FbsClientMovementRequest.objects.select_for_update()
        .select_related("agency")
        .get(pk=request_id)
    )
    if request_row.status == FbsClientMovementRequest.STATUS_APPROVED:
        return request_row
    if request_row.status != FbsClientMovementRequest.STATUS_SUBMITTED:
        raise FbsReplenishmentError("Подтвердить можно только заявку, ожидающую менеджера.")
    if not request_row.lines.exists():
        raise FbsReplenishmentError("В заявке нет строк для перемещения.")
    if request_row.uses_hard_reserve:
        _hard_reserve_allocations(request_row, lock=True)
        from sklad.services.fbs_quantity_reserves import validate_pool
        try:
            validate_pool(request_row.agency_id, request_row.id)
        except WarehouseTransitionError as exc:
            raise FbsReplenishmentError(str(exc)) from exc
    request_row.reviewed_by = actor
    request_row.reviewed_at = timezone.now()
    request_row.status = FbsClientMovementRequest.STATUS_APPROVED
    request_row.clarification_reason = ""
    request_row.save(
        update_fields=[
            "reviewed_by",
            "reviewed_at",
            "status",
            "clarification_reason",
            "updated_at",
        ]
    )
    _log_request_status(
        request_row,
        user=actor,
        description="Менеджер подтвердил FBS-перемещение и передал заявку на склад",
    )
    return request_row


def _locked_target_pallet(*, request_row, target_pallet_id: int) -> FbsPallet:
    pallet = (
        FbsPallet.objects.select_for_update()
        .select_related("cell__location")
        .filter(
            pk=target_pallet_id,
            agency=request_row.agency,
            status__in=ACTIVE_PALLET_STATUSES,
            cell__is_active=True,
        )
        .first()
    )
    if pallet is None:
        raise FbsReplenishmentError("Выбранная FBS-паллета недоступна для этого клиента.")
    return pallet


def _automatic_target_pallet(
    request_row: FbsClientMovementRequest,
    *,
    required_boxes: int | None = None,
) -> FbsPallet | None:
    if required_boxes is None:
        required_boxes = (
            max(int(request_row.requested_box_count or 0), 1)
            if request_row.mode == FbsClientMovementRequest.MODE_ITEM
            else int(request_row.requested_box_count or 0)
        )
    pallets = (
        FbsPallet.objects.filter(
            agency=request_row.agency,
            status__in=ACTIVE_PALLET_STATUSES,
            cell__is_active=True,
        )
        .select_related("cell__location")
        .annotate(
            active_box_count=Count(
                "boxes",
                filter=Q(boxes__status__in=ACTIVE_BOX_STATUSES),
                distinct=True,
            ),
            pending_box_count=Count(
                "replenishment_plans",
                filter=Q(
                    replenishment_plans__mode=FbsReplenishmentPlan.MODE_ITEM,
                    replenishment_plans__target_box__isnull=True,
                    replenishment_plans__status__in=(
                        FbsReplenishmentPlan.STATUS_PROPOSED,
                        FbsReplenishmentPlan.STATUS_CONFIRMED,
                        FbsReplenishmentPlan.STATUS_IN_PROGRESS,
                        FbsReplenishmentPlan.STATUS_AWAITING_PACK,
                    ),
                ),
                distinct=True,
            ),
        )
        .order_by("cell__cell_code", "pallet_code", "id")
    )
    for pallet in pallets:
        if (
            int(pallet.max_boxes or 0)
            - int(pallet.active_box_count or 0)
            - int(pallet.pending_box_count or 0)
            >= required_boxes
        ):
            return pallet
    try:
        return allocate_shared_os_fbs_pallet(
            agency=request_row.agency,
            required_box_slots=required_boxes,
        )
    except FbsStorageError as exc:
        raise FbsReplenishmentError(str(exc)) from exc


def _full_source_pallet_id_from_comment(comment: str) -> int:
    value = str(comment or "")
    if not value.startswith(FULL_SOURCE_PALLET_COMMENT_PREFIX):
        return 0
    token = value[len(FULL_SOURCE_PALLET_COMMENT_PREFIX) :].split("]", 1)[0]
    try:
        return int(token)
    except (TypeError, ValueError):
        return 0


def _is_complete_pallet_movement(
    request_row: FbsClientMovementRequest,
    plans,
) -> bool:
    # A completely selected physical pallet is posted to FBS in place.  This
    # includes pallets released from receiving: their physical address and
    # parent/child structure stay unchanged until a later free FBS relocation.
    # A separate box never passes the complete-pallet checks below and keeps the
    # ordinary reachtruck scan flow.
    if not bool(
        getattr(settings, "FBS_CLIENT_FULL_PALLET_LOGICAL_POST_ON_ACCEPT", True)
    ):
        return False
    plan_rows = list(plans)
    if (
        request_row.mode != FbsClientMovementRequest.MODE_BOX
        or not plan_rows
        or any(plan.mode != FbsReplenishmentPlan.MODE_BOX for plan in plan_rows)
    ):
        return False
    expected_parent_by_plan = {
        int(plan.id): _full_source_pallet_id_from_comment(plan.comment)
        for plan in plan_rows
    }
    if any(not parent_id for parent_id in expected_parent_by_plan.values()):
        return False
    source_rows = list(
        FbsReplenishmentLine.objects.filter(plan_id__in=expected_parent_by_plan)
        .values_list("plan_id", "source_container__parent_container_id")
        .order_by("plan_id", "id")
    )
    return bool(source_rows) and all(
        int(parent_id or 0) == expected_parent_by_plan.get(int(plan_id), 0)
        for plan_id, parent_id in source_rows
    ) and {int(plan_id) for plan_id, _parent_id in source_rows} == set(
        expected_parent_by_plan
    )


def _post_complete_pallet_movement_logically(
    *,
    request_row: FbsClientMovementRequest,
    plans,
    actor,
) -> FbsClientMovementRequest:
    from .reachtruck_bridge import (
        assert_client_movement_full_pallet_tasks_unstarted,
        cancel_client_movement_full_pallet_tasks_after_logical_posting,
    )
    from .replenishment import complete_full_pallet_client_movement_logically

    locked_plans = list(
        FbsReplenishmentPlan.objects.select_for_update()
        .filter(id__in=[int(plan.id) for plan in plans])
        .order_by("id")
    )
    if not _is_complete_pallet_movement(request_row, locked_plans):
        raise FbsReplenishmentError(
            "Автоматическая проводка разрешена только для полностью выбранной паллеты."
        )
    if request_row.status == FbsClientMovementRequest.STATUS_COMPLETED:
        return request_row

    allocations = list(
        FbsReplenishmentAllocation.objects.select_for_update()
        .filter(line__plan_id__in=[plan.id for plan in locked_plans])
        .order_by("line__plan_id", "id")
    )
    if not allocations:
        raise FbsReplenishmentError("В паллетном FBS-перемещении нет распределений.")
    completed_statuses = {FbsReplenishmentAllocation.STATUS_DONE}
    if all(allocation.status in completed_statuses for allocation in allocations):
        request_row = sync_client_movement_request_status(
            request_row.id,
            performed_by=actor,
        )
    else:
        if any(
            allocation.status
            not in {
                FbsReplenishmentAllocation.STATUS_RESERVED,
                FbsReplenishmentAllocation.STATUS_IN_PROGRESS,
            }
            for allocation in allocations
        ):
            raise FbsReplenishmentError(
                "Паллетное перемещение уже выполнено частично; требуется проверка склада."
            )
        assert_client_movement_full_pallet_tasks_unstarted(request_row.id)
        for plan in locked_plans:
            if plan.assigned_to_id not in {None, actor.id}:
                raise FbsReplenishmentError(
                    "Паллетное перемещение уже назначено другому сотруднику."
                )
            if plan.assigned_to_id is None:
                plan.assigned_to = actor
                plan.save(update_fields=["assigned_to", "updated_at"])
        complete_full_pallet_client_movement_logically(
            allocation_ids=[allocation.id for allocation in allocations],
            performed_by=actor,
        )
        request_row = sync_client_movement_request_status(
            request_row.id,
            performed_by=actor,
        )

    cancel_client_movement_full_pallet_tasks_after_logical_posting(request_row.id)
    if request_row.status != FbsClientMovementRequest.STATUS_MOVED:
        raise FbsReplenishmentError(
            "Паллетное перемещение не перешло в статус фактического проведения."
        )
    return confirm_client_movement_by_warehouse(
        request_id=request_row.id,
        confirmed_by=actor,
        comment="",
    )


@transaction.atomic
def accept_client_movement_request(
    *,
    request_id: int,
    accepted_by=None,
    target_box_id: int | None = None,
    target_pallet_id: int | None = None,
    prepared_box_count: int | None = None,
    allow_item_fallback: bool = False,
) -> ClientMovementApprovalResult:
    from .replenishment import (
        MAX_PREPARED_BOXES_PER_PLAN,
        confirm_replenishment_plan,
        create_replenishment_plan,
        prepare_replenishment_boxes,
    )

    actor = _require_actor_role(
        accepted_by,
        WAREHOUSE_ROLES,
        "Принять FBS-перемещение в работу может только сотрудник склада.",
    )
    request_row = (
        FbsClientMovementRequest.objects.select_for_update()
        .select_related("agency")
        .get(pk=request_id)
    )
    existing = tuple(
        plan
        for plan in request_row.replenishment_plans.order_by("id")
        if not _is_superseded_full_box_recovery_plan(plan)
    )
    if existing:
        request_row = sync_client_movement_request_status(
            request_row.id,
            performed_by=actor,
        )
        if (
            request_row.status == FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED
            and _is_complete_pallet_movement(request_row, existing)
        ):
            request_row = _post_complete_pallet_movement_logically(
                request_row=request_row,
                plans=existing,
                actor=actor,
            )
            existing = tuple(
                plan
                for plan in request_row.replenishment_plans.order_by("id")
                if not _is_superseded_full_box_recovery_plan(plan)
            )
        return ClientMovementApprovalResult(request=request_row, plans=existing)
    if request_row.mode != FbsClientMovementRequest.MODE_BOX:
        raise FbsReplenishmentError(
            "Новые FBS-перемещения выполняются только целыми коробами по кратности. "
            "Поштучный добор и пересборка коробов отключены."
        )
    if prepared_box_count not in (None, ""):
        raise FbsReplenishmentError(
            "Количество новых коробов для коробного FBS-перемещения "
            "определяется составом заявки автоматически."
        )
    allowed_statuses = {FbsClientMovementRequest.STATUS_APPROVED}
    if not request_row.uses_hard_reserve:
        allowed_statuses.add(FbsClientMovementRequest.STATUS_SUBMITTED)
    if request_row.status not in allowed_statuses:
        raise FbsReplenishmentError(
            "Склад может принять новую FBS-заявку только после подтверждения менеджером."
        )

    lines = list(request_row.lines.select_related("sku", "request").order_by("id"))
    if not lines:
        raise FbsReplenishmentError("В заявке нет строк для перемещения.")
    plans: list[FbsReplenishmentPlan] = []
    comment = f"Клиентская заявка {request_row.number}. {request_row.comment}".strip()
    from sklad.services.fbs_quantity_reserves import allocate_pool
    try:
        allocate_pool(
            request_row,
            actor,
            allow_item_fallback=True,
        )
    except WarehouseTransitionError as exc:
        raise FbsReplenishmentError(str(exc)) from exc
    exact_allocations = (
        _hard_reserve_allocations(request_row, lock=True)
        if request_row.uses_hard_reserve
        else None
    )

    full_box_container_ids = (
        _hard_reserve_full_box_container_ids(
            request_row=request_row,
            allocations=exact_allocations,
        )
        if exact_allocations is not None
        else set()
    )

    if (
        request_row.mode == FbsClientMovementRequest.MODE_ITEM
        and not full_box_container_ids
    ):
        direct_box_flow = prepared_box_count is not None
        try:
            requested_prepared_boxes = int(
                prepared_box_count
                if prepared_box_count is not None
                else max(int(request_row.requested_box_count or 0), 1)
            )
        except (TypeError, ValueError) as exc:
            raise FbsReplenishmentError("Укажите количество коробов для печати.") from exc
        if direct_box_flow and not (
            1 <= requested_prepared_boxes <= MAX_PREPARED_BOXES_PER_PLAN
        ):
            raise FbsReplenishmentError(
                "Количество коробов должно быть от 1 "
                f"до {MAX_PREPARED_BOXES_PER_PLAN}."
            )
        if target_pallet_id:
            target_pallet = _locked_target_pallet(
                request_row=request_row,
                target_pallet_id=int(target_pallet_id),
            )
        elif target_box_id:
            selected_box = (
                FbsBox.objects.select_for_update()
                .select_related("pallet__cell__location")
                .filter(
                    pk=target_box_id,
                    agency=request_row.agency,
                    status__in=ACTIVE_BOX_STATUSES,
                    pallet__status__in=ACTIVE_PALLET_STATUSES,
                    pallet__cell__is_active=True,
                )
                .first()
            )
            target_pallet = selected_box.pallet if selected_box else None
        else:
            suggested_pallet = _automatic_target_pallet(
                request_row,
                required_boxes=requested_prepared_boxes,
            )
            target_pallet = (
                FbsPallet.objects.select_for_update()
                .select_related("cell__location")
                .filter(pk=getattr(suggested_pallet, "pk", None))
                .first()
            )
        if target_pallet is None:
            raise FbsReplenishmentError(
                "Для клиента нет FBS-паллеты со свободным местом под короб."
            )
        staging_location = _locked_fbs_staging_location(
            required_slots=requested_prepared_boxes,
        )
        if not direct_box_flow:
            ensure_movement_staging_container(
                movement=request_row,
                location=staging_location,
                created_by=actor,
            )
        if exact_allocations is not None:
            released = _release_hard_reserve(
                request_row,
                actor=actor,
                reason="Атомарная передача точного резерва в складской план FBS",
            )
            if released != exact_allocations:
                raise FbsReplenishmentError(
                    "Состав снятого резерва FBS изменился перед созданием задания."
                )
        plan = create_replenishment_plan(
            agency=request_row.agency,
            mode=FbsReplenishmentPlan.MODE_ITEM,
            target_pallet=target_pallet,
            target_box=None,
            staging_location=staging_location,
            lines=[
                {
                    "sku": line.sku,
                    "barcode": line.barcode,
                    "qty": line.requested_qty,
                    "client_movement_line": line,
                }
                for line in lines
            ],
            requested_by=actor,
            requested_by_role="storekeeper",
            comment=comment,
            client_movement_request=request_row,
        )
        if direct_box_flow:
            prepare_replenishment_boxes(
                plan_id=plan.id,
                box_count=prepared_box_count,
                prepared_by=actor,
            )
        plans.append(
            confirm_replenishment_plan(
                plan_id=plan.id,
                confirmed_by=actor,
                source_allocations=exact_allocations,
                defer_client_movement_sync=True,
                defer_reachtruck_tasks=True,
            )
        )
    else:
        selected: list[tuple[FbsClientMovementRequestLine, WarehouseContainer]] = []
        item_fallback_lines: list[
            tuple[FbsClientMovementRequestLine, int, int]
        ] = []
        hard_reserve_fallback_allocations = None
        if exact_allocations is not None:
            if full_box_container_ids:
                container_ids = set(full_box_container_ids)
                containers_by_id = {
                    container.id: container
                    for container in WarehouseContainer.objects.select_for_update().filter(
                        id__in=container_ids,
                        agency=request_row.agency,
                        container_type=WarehouseContainer.TYPE_BOX,
                    )
                }
                if len(containers_by_id) != len(container_ids):
                    raise FbsReplenishmentError(
                        "Часть полных зарезервированных коробов FBS больше не существует."
                    )
                lines_by_id = {line.id: line for line in lines}
                representative_line_by_container = {}
                for row in exact_allocations:
                    container_id = int(row.get("container_id") or 0)
                    line_id = int(row.get("request_line_id") or 0)
                    if line_id not in lines_by_id:
                        raise FbsReplenishmentError(
                            "Точный резерв ссылается на отсутствующую строку FBS-заявки."
                        )
                    if container_id in container_ids:
                        representative_line_by_container.setdefault(
                            container_id,
                            lines_by_id[line_id],
                        )
                selected.extend(
                    (
                        representative_line_by_container[container_id],
                        containers_by_id[container_id],
                    )
                    for container_id in sorted(container_ids)
                )
                hard_reserve_fallback_allocations = [
                    row
                    for row in exact_allocations
                    if int(row.get("container_id") or 0) not in container_ids
                ]
                if hard_reserve_fallback_allocations:
                    fallback_qty_by_line: dict[int, int] = defaultdict(int)
                    for row in hard_reserve_fallback_allocations:
                        fallback_qty_by_line[int(row["request_line_id"])] += int(
                            row["qty"]
                        )
                    try:
                        total_requested_boxes = int(
                            prepared_box_count
                            if prepared_box_count is not None
                            else request_row.requested_box_count
                        )
                    except (TypeError, ValueError) as exc:
                        raise FbsReplenishmentError(
                            "Укажите количество коробов для печати."
                        ) from exc
                    remaining_box_count = max(
                        total_requested_boxes - len(container_ids),
                        1,
                    )
                    fallback_lines = [
                        lines_by_id[line_id]
                        for line_id in sorted(fallback_qty_by_line)
                    ]
                    item_fallback_lines.extend(
                        (
                            line,
                            fallback_qty_by_line[line.id],
                            remaining_box_count if index == 0 else 0,
                        )
                        for index, line in enumerate(fallback_lines)
                    )
                elif allow_item_fallback:
                    raise FbsReplenishmentError(
                        "Поштучный добор не требуется: весь резерв состоит из полных коробов."
                    )
            elif _hard_reserve_can_attach_exact_boxes(
                request_row=request_row,
                lines=lines,
                allocations=exact_allocations,
            ):
                container_ids = {
                    int(row.get("container_id") or 0) for row in exact_allocations
                }
                containers_by_id = {
                    container.id: container
                    for container in WarehouseContainer.objects.select_for_update().filter(
                        id__in=container_ids,
                        agency=request_row.agency,
                        container_type=WarehouseContainer.TYPE_BOX,
                    )
                }
                if len(containers_by_id) != len(container_ids):
                    raise FbsReplenishmentError(
                        "Часть зарезервированных коробов FBS больше не существует."
                    )
                lines_by_id = {line.id: line for line in lines}
                representative_line_by_container = {}
                for row in exact_allocations:
                    container_id = int(row["container_id"])
                    line_id = int(row["request_line_id"])
                    if line_id not in lines_by_id:
                        raise FbsReplenishmentError(
                            "Точный резерв ссылается на отсутствующую строку FBS-заявки."
                        )
                    representative_line_by_container.setdefault(
                        container_id,
                        lines_by_id[line_id],
                    )
                selected.extend(
                    (
                        representative_line_by_container[container_id],
                        containers_by_id[container_id],
                    )
                    for container_id in representative_line_by_container
                )
                if allow_item_fallback:
                    raise FbsReplenishmentError(
                        "Поштучный добор не требуется: все зарезервированные короба можно перенести целиком."
                    )
            else:
                hard_reserve_fallback_allocations = exact_allocations
                item_fallback_lines.extend(
                    (
                        line,
                        int(line.requested_qty or 0),
                        int(line.requested_box_count or 0),
                    )
                    for line in lines
                )
        else:
            for line in lines:
                candidates = _eligible_box_candidates(line, lock=True)
                required = int(line.requested_box_count or 0)
                selected_count = min(len(candidates), required)
                selected.extend(
                    (line, container) for container in candidates[:selected_count]
                )
                missing_box_count = required - selected_count
                if missing_box_count:
                    item_fallback_lines.append(
                        (
                            line,
                            missing_box_count * int(line.units_per_box or 0),
                            missing_box_count,
                        )
                    )
            if item_fallback_lines:
                _validate_box_fallback_stock(
                    request_row=request_row,
                    lines=[line for line, _, _ in item_fallback_lines],
                )
        if allow_item_fallback and not item_fallback_lines:
            raise FbsReplenishmentError(
                "Поштучный добор не требуется: все короба нужной кратности доступны."
            )
        fallback_box_count = sum(row[2] for row in item_fallback_lines)
        if fallback_box_count > MAX_PREPARED_BOXES_PER_PLAN:
            raise FbsReplenishmentError(
                "Для поштучного добора требуется слишком много новых коробов: "
                f"{fallback_box_count}. Максимум: {MAX_PREPARED_BOXES_PER_PLAN}."
            )
        full_pallet_by_box = (
            _full_source_pallet_by_box(
                request_row=request_row,
                containers=[container for _, container in selected],
            )
            if request_row.uses_hard_reserve and not target_pallet_id
            else {}
        )
        selected_by_full_pallet: dict[
            int,
            list[tuple[FbsClientMovementRequestLine, WarehouseContainer]],
        ] = defaultdict(list)
        partial_selected = []
        for line, source_container in selected:
            source_pallet = full_pallet_by_box.get(source_container.id)
            if source_pallet is None:
                partial_selected.append((line, source_container))
            else:
                selected_by_full_pallet[source_pallet.id].append(
                    (line, source_container)
                )

        prepared_box_plans: list[
            tuple[
                FbsClientMovementRequestLine,
                WarehouseContainer,
                FbsPallet,
                FbsBox,
                WarehouseContainer | None,
            ]
        ] = []
        full_pallet_groups = sorted(selected_by_full_pallet.items())
        reserved_target_pallets = allocate_shared_os_fbs_pallet_batch(
            agency=request_row.agency,
            slot_requirements=(
                (len(pallet_rows), len(pallet_rows))
                for _source_pallet_id, pallet_rows in full_pallet_groups
            ),
        )
        for (
            (_source_pallet_id, pallet_rows),
            target_pallet,
        ) in zip(full_pallet_groups, reserved_target_pallets):
            source_pallet = full_pallet_by_box[pallet_rows[0][1].id]
            for line, source_container in pallet_rows:
                target_box = attach_existing_warehouse_box_to_fbs(
                    agency=request_row.agency,
                    pallet=target_pallet,
                    source_container=source_container,
                )
                prepared_box_plans.append(
                    (
                        line,
                        source_container,
                        target_pallet,
                        target_box,
                        source_pallet,
                    )
                )

        explicit_partial_pallet = (
            _locked_target_pallet(
                request_row=request_row,
                target_pallet_id=int(target_pallet_id),
            )
            if target_pallet_id and (partial_selected or fallback_box_count)
            else None
        )
        if explicit_partial_pallet is not None:
            active_box_count = FbsBox.objects.filter(
                pallet=explicit_partial_pallet,
                status__in=ACTIVE_BOX_STATUSES,
            ).count()
            free_slots = int(explicit_partial_pallet.max_boxes or 0) - active_box_count
            required_slots = len(partial_selected) + fallback_box_count
            if required_slots > free_slots:
                raise FbsReplenishmentError(
                    f"На паллете свободно мест: {free_slots}; требуется: {required_slots}."
                )

        for line, source_container in partial_selected:
            target_pallet = explicit_partial_pallet
            if target_pallet is None:
                target_pallet = _automatic_target_pallet(
                    request_row,
                    required_boxes=1,
                )
                target_pallet = _locked_target_pallet(
                    request_row=request_row,
                    target_pallet_id=int(getattr(target_pallet, "pk", 0) or 0),
                )
            target_box = attach_existing_warehouse_box_to_fbs(
                agency=request_row.agency,
                pallet=target_pallet,
                source_container=source_container,
            )
            prepared_box_plans.append(
                (line, source_container, target_pallet, target_box, None)
            )

        fallback_pallet = explicit_partial_pallet
        if item_fallback_lines and fallback_pallet is None:
            fallback_pallet = _automatic_target_pallet(
                request_row,
                required_boxes=max(fallback_box_count, 1),
            )
            fallback_pallet = _locked_target_pallet(
                request_row=request_row,
                target_pallet_id=int(getattr(fallback_pallet, "pk", 0) or 0),
            )
        if exact_allocations is not None:
            released = _release_hard_reserve(
                request_row,
                actor=actor,
                reason="Атомарная передача точных коробов в складской план FBS",
            )
            if released != exact_allocations:
                raise FbsReplenishmentError(
                    "Состав снятого коробного резерва FBS изменился перед созданием задания."
                )
        proposed_box_plans: list[
            tuple[FbsReplenishmentPlan, list[dict] | None]
        ] = []
        for (
            line,
            source_container,
            target_pallet,
            target_box,
            source_pallet,
        ) in prepared_box_plans:
            box_plan_comment = comment
            if (
                request_row.mode == FbsClientMovementRequest.MODE_ITEM
                and full_box_container_ids
            ):
                box_plan_comment = (
                    f"{FbsReplenishmentPlan.CLIENT_ITEM_WHOLE_BOX_MARKER} "
                    f"{comment}"
                ).strip()
            plan_comment = (
                _full_source_pallet_comment(source_pallet.id, box_plan_comment)
                if source_pallet is not None
                else box_plan_comment
            )
            plan = create_replenishment_plan(
                agency=request_row.agency,
                mode=FbsReplenishmentPlan.MODE_BOX,
                target_pallet=target_pallet,
                target_box=target_box,
                lines=[
                    {
                        "source_container": source_container,
                        "client_movement_line": line,
                    }
                ],
                requested_by=actor,
                requested_by_role="storekeeper",
                comment=plan_comment,
                client_movement_request=request_row,
            )
            plan_allocations = None
            if exact_allocations is not None:
                plan_allocations = [
                    row
                    for row in exact_allocations
                    if int(row["container_id"]) == source_container.id
                ]
            proposed_box_plans.append((plan, plan_allocations))
        for plan, plan_allocations in proposed_box_plans:
            if plan.target_box_id is None:
                raise FbsReplenishmentError("У коробного плана отсутствует короб назначения.")
        validated_box_locks = validate_boxes_unlocked(
            plan.target_box_id for plan, _plan_allocations in proposed_box_plans
        )
        for plan, plan_allocations in proposed_box_plans:
            plans.append(
                confirm_replenishment_plan(
                    plan_id=plan.id,
                    confirmed_by=actor,
                    source_allocations=plan_allocations,
                    validated_box_locks=validated_box_locks,
                    defer_client_movement_sync=True,
                    defer_reachtruck_tasks=True,
                )
            )
        if item_fallback_lines:
            staging_location = _locked_fbs_staging_location(
                required_slots=fallback_box_count,
            )
            fallback_plan = create_replenishment_plan(
                agency=request_row.agency,
                mode=FbsReplenishmentPlan.MODE_ITEM,
                target_pallet=fallback_pallet,
                target_box=None,
                staging_location=staging_location,
                lines=[
                    {
                        "sku": line.sku,
                        "barcode": line.barcode,
                        "qty": fallback_qty,
                        "client_movement_line": line,
                    }
                    for line, fallback_qty, _ in item_fallback_lines
                ],
                requested_by=actor,
                requested_by_role="storekeeper",
                comment=(
                    (
                        f"{FbsReplenishmentPlan.CLIENT_BOX_ITEM_FALLBACK_MARKER} "
                        if request_row.mode == FbsClientMovementRequest.MODE_BOX
                        else ""
                    )
                    + comment
                ).strip(),
                client_movement_request=request_row,
            )
            prepare_replenishment_boxes(
                plan_id=fallback_plan.id,
                box_count=fallback_box_count,
                prepared_by=actor,
            )
            plans.append(
                confirm_replenishment_plan(
                    plan_id=fallback_plan.id,
                    confirmed_by=actor,
                    source_allocations=hard_reserve_fallback_allocations,
                    defer_client_movement_sync=True,
                    defer_reachtruck_tasks=True,
                )
            )

    request_row.warehouse_accepted_by = actor
    request_row.warehouse_accepted_at = request_row.warehouse_accepted_at or timezone.now()
    request_row.status = FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED
    request_row.save(
        update_fields=[
            "warehouse_accepted_by",
            "warehouse_accepted_at",
            "status",
            "updated_at",
        ]
    )
    complete_pallet_movement = _is_complete_pallet_movement(request_row, plans)
    _log_request_status(
        request_row,
        user=actor,
        description=(
            "Кладовщик принял полную паллету FBS: остаток проводится сразу, "
            "физическое место сохраняется до свободного перемещения"
            if complete_pallet_movement
            else "Кладовщик принял FBS-перемещение и передал задание ричтраку"
        ),
    )
    if complete_pallet_movement:
        request_row = _post_complete_pallet_movement_logically(
            request_row=request_row,
            plans=plans,
            actor=actor,
        )
        plans = list(request_row.replenishment_plans.order_by("id"))
    elif plans:
        from .reachtruck_bridge import (
            sync_client_movement_reachtruck_request,
            sync_plan_reachtruck_tasks,
        )

        # The route is built only here, once the pallet is known to need a real
        # trip.  Confirming the plans above deferred it on purpose: a whole-pallet
        # movement is posted logically just above and would cancel the route in
        # the same transaction.
        for plan in plans:
            # Same guard confirm_replenishment_plan applied before the route was
            # deferred: a finished plan never gets a driver task.
            if (
                plan.client_movement_request_id
                and plan.status != FbsReplenishmentPlan.STATUS_DONE
            ):
                sync_plan_reachtruck_tasks(plan, sync_request=False)
        sync_client_movement_reachtruck_request(request_row.id)
    return ClientMovementApprovalResult(request=request_row, plans=tuple(plans))


def approve_client_movement_request(
    *,
    request_id: int,
    reviewed_by=None,
    target_box_id: int | None = None,
    target_pallet_id: int | None = None,
    prepared_box_count: int | None = None,
    allow_item_fallback: bool = False,
) -> ClientMovementApprovalResult:
    """Совместимое имя складского действия для принятия заявки кладовщиком."""
    return accept_client_movement_request(
        request_id=request_id,
        accepted_by=reviewed_by,
        target_box_id=target_box_id,
        target_pallet_id=target_pallet_id,
        prepared_box_count=prepared_box_count,
        allow_item_fallback=allow_item_fallback,
    )


@transaction.atomic
def confirm_client_movement_by_warehouse(
    *, request_id: int, confirmed_by=None, comment: str = ""
) -> FbsClientMovementRequest:
    actor = _require_actor_role(
        confirmed_by,
        WAREHOUSE_ROLES,
        "Подтвердить факт перемещения может только сотрудник склада.",
    )
    request_row = FbsClientMovementRequest.objects.select_for_update().select_related(
        "agency"
    ).get(pk=request_id)
    if request_row.status == FbsClientMovementRequest.STATUS_COMPLETED:
        return request_row
    if request_row.status not in {
        FbsClientMovementRequest.STATUS_MOVED,
        FbsClientMovementRequest.STATUS_NEEDS_CLARIFICATION,
        FbsClientMovementRequest.STATUS_AWAITING_MANAGER_CONFIRMATION,
    }:
        raise FbsReplenishmentError("Сначала ричтрак должен завершить все перемещения.")
    plans = list(_movement_plan_queryset(request_row).select_for_update())
    physical_replacement_source_ids = (
        _completed_physical_replacement_source_plan_ids(plans)
    )
    allowed_plan_statuses = {
        FbsReplenishmentPlan.STATUS_DONE,
        FbsReplenishmentPlan.STATUS_CANCELED,
    }
    if not plans or any(plan.status not in allowed_plan_statuses for plan in plans):
        raise FbsReplenishmentError("Не все задания ричтрака завершены.")
    if any(
        plan.status == FbsReplenishmentPlan.STATUS_CANCELED
        and not str(plan.comment or "").startswith("[missing_box_partial]")
        and plan.id not in physical_replacement_source_ids
        for plan in plans
    ):
        raise FbsReplenishmentError("Есть отмененные задания без отметки «короб не найден».")
    actual_qty = sum(int(plan.moved_qty or 0) for plan in plans)
    if request_row.mode == FbsClientMovementRequest.MODE_BOX:
        actual_boxes = sum(
            1
            for plan in plans
            if plan.mode == FbsReplenishmentPlan.MODE_BOX
            and plan.status == FbsReplenishmentPlan.STATUS_DONE
        ) + FbsReplenishmentPreparedBox.objects.filter(
            plan__client_movement_request=request_row,
            plan__mode=FbsReplenishmentPlan.MODE_ITEM,
            status=FbsReplenishmentPreparedBox.STATUS_PLACED,
        ).count()
        if not actual_boxes:
            actual_boxes = sum(
                1
                for plan in plans
                if plan.status == FbsReplenishmentPlan.STATUS_DONE
            )
    else:
        actual_boxes = FbsReplenishmentPreparedBox.objects.filter(
            plan__client_movement_request=request_row,
            status=FbsReplenishmentPreparedBox.STATUS_PLACED,
        ).count()
        if not actual_boxes:
            actual_boxes = len(
                {
                    plan.target_box_id
                    for plan in plans
                    if plan.target_box_id
                    and plan.status == FbsReplenishmentPlan.STATUS_DONE
                }
            )
    note = str(comment or "").strip()
    if actual_qty != int(request_row.requested_qty or 0) and not note:
        raise FbsReplenishmentError("При расхождении количества укажите комментарий кладовщика.")
    now = timezone.now()
    _release_client_movement_stock_after_warehouse_confirmation(
        request_row,
        actor=actor,
        now=now,
    )
    request_row.actual_moved_qty = actual_qty
    request_row.actual_moved_box_count = actual_boxes
    request_row.warehouse_confirmed_by = actor
    request_row.warehouse_confirmed_at = now
    request_row.clarification_reason = note
    request_row.status = FbsClientMovementRequest.STATUS_COMPLETED
    request_row.completed_at = now
    request_row.save(
        update_fields=[
            "actual_moved_qty",
            "actual_moved_box_count",
            "warehouse_confirmed_by",
            "warehouse_confirmed_at",
            "clarification_reason",
            "status",
            "completed_at",
            "updated_at",
        ]
    )
    _activate_client_movement_stock_export(request_row, actor=actor)
    _log_request_status(
        request_row,
        user=actor,
        description=(
            "Кладовщик подтвердил фактическое FBS-перемещение; "
            "заявка выполнена"
        ),
    )
    movement_warehouse_confirmed.send_robust(
        sender=confirm_client_movement_by_warehouse,
        request_row=request_row,
        user=actor,
    )
    responses = movement_completed.send(
        sender=confirm_client_movement_by_warehouse,
        request_row=request_row,
        user=actor,
    )
    if not responses:
        raise FbsReplenishmentError("Обработчик биллинга FBS не подключен.")
    request_row.billing_synced_at = now
    request_row.save(update_fields=["billing_synced_at", "updated_at"])

    from client_cabinet.messaging_lk import create_notification

    create_notification(
        agency=request_row.agency,
        title=f"Заявка {request_row.number} выполнена",
        text=f"На отдельный остаток FBS перемещено {request_row.actual_moved_qty} шт.",
        notif_type="status",
        detail_url=f"#/fbs?tab=movements&movement={request_row.id}",
        priority="normal",
        source_key=f"fbs-movement:completed:{request_row.id}",
    )
    return request_row


@transaction.atomic
def confirm_client_movement_by_manager(
    *, request_id: int, confirmed_by=None
) -> FbsClientMovementRequest:
    actor = _require_actor_role(
        confirmed_by,
        MANAGER_ROLES,
        "Подтвердить выполнение FBS-заявки может только менеджер.",
    )
    request_row = FbsClientMovementRequest.objects.select_for_update().select_related(
        "agency"
    ).get(pk=request_id)
    if request_row.status == FbsClientMovementRequest.STATUS_COMPLETED:
        return request_row
    if request_row.status != FbsClientMovementRequest.STATUS_AWAITING_MANAGER_CONFIRMATION:
        raise FbsReplenishmentError("Заявка еще не подтверждена кладовщиком.")
    now = timezone.now()
    request_row.status = FbsClientMovementRequest.STATUS_COMPLETED
    request_row.manager_confirmed_by = actor
    request_row.manager_confirmed_at = now
    request_row.completed_at = now
    request_row.clarification_reason = ""
    request_row.save(
        update_fields=[
            "status",
            "manager_confirmed_by",
            "manager_confirmed_at",
            "completed_at",
            "clarification_reason",
            "updated_at",
        ]
    )
    responses = movement_completed.send(
        sender=confirm_client_movement_by_manager,
        request_row=request_row,
        user=actor,
    )
    if not responses:
        raise FbsReplenishmentError("Обработчик биллинга FBS не подключен.")
    request_row.billing_synced_at = now
    request_row.save(update_fields=["billing_synced_at", "updated_at"])
    _log_request_status(
        request_row,
        user=actor,
        description="Менеджер подтвердил выполнение FBS-перемещения",
    )
    from client_cabinet.messaging_lk import create_notification

    create_notification(
        agency=request_row.agency,
        title=f"Заявка {request_row.number} выполнена",
        text=f"На отдельный остаток FBS перемещено {request_row.actual_moved_qty} шт.",
        notif_type="status",
        detail_url=f"#/fbs?tab=movements&movement={request_row.id}",
        priority="normal",
        source_key=f"fbs-movement:completed:{request_row.id}",
    )
    return request_row


@transaction.atomic
def return_client_movement_for_clarification(
    *, request_id: int, returned_by=None, reason: str
) -> FbsClientMovementRequest:
    actor = _require_actor_role(
        returned_by,
        MANAGER_ROLES,
        "Вернуть FBS-заявку может только менеджер.",
    )
    reason = str(reason or "").strip()
    if not reason:
        raise FbsReplenishmentError("Укажите причину возврата кладовщику.")
    request_row = FbsClientMovementRequest.objects.select_for_update().select_related(
        "agency"
    ).get(pk=request_id)
    if request_row.status != FbsClientMovementRequest.STATUS_AWAITING_MANAGER_CONFIRMATION:
        raise FbsReplenishmentError("Вернуть можно только заявку на проверке менеджера.")
    request_row.status = FbsClientMovementRequest.STATUS_NEEDS_CLARIFICATION
    request_row.clarification_reason = reason
    request_row.save(update_fields=["status", "clarification_reason", "updated_at"])
    _log_request_status(
        request_row,
        user=actor,
        description=f"Менеджер вернул FBS-перемещение на уточнение: {reason}",
    )
    return request_row


@transaction.atomic
def reject_client_movement_request(
    *, request_id: int, reviewed_by=None
) -> FbsClientMovementRequest:
    actor = _require_actor_role(
        reviewed_by,
        MANAGER_ROLES,
        "Отклонить клиентскую заявку может только менеджер.",
    )
    request_row = FbsClientMovementRequest.objects.select_for_update().get(pk=request_id)
    if request_row.status == FbsClientMovementRequest.STATUS_REJECTED:
        return request_row
    if request_row.status != FbsClientMovementRequest.STATUS_SUBMITTED:
        raise FbsReplenishmentError("Отклонить можно только новую клиентскую заявку.")
    if request_row.replenishment_plans.exists():
        raise FbsReplenishmentError("По заявке уже созданы складские планы.")
    if request_row.uses_hard_reserve:
        _hard_reserve_allocations(request_row, lock=True)
        _release_hard_reserve(
            request_row,
            actor=actor,
            reason="Менеджер отклонил новую FBS-заявку",
        )
    request_row.status = FbsClientMovementRequest.STATUS_REJECTED
    request_row.reviewed_by = actor
    request_row.reviewed_at = timezone.now()
    request_row.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
    _log_request_status(
        request_row,
        user=actor,
        description="Менеджер отклонил FBS-перемещение",
    )
    return request_row


@transaction.atomic
def cancel_client_movement_request(
    *,
    request_id: int,
    agency,
    canceled_by=None,
) -> FbsClientMovementRequest:
    actor = _authenticated_user(canceled_by)
    request_row = (
        FbsClientMovementRequest.objects.select_for_update()
        .select_related("agency")
        .get(pk=request_id, agency=agency)
    )
    if request_row.status == FbsClientMovementRequest.STATUS_CANCELED:
        return request_row
    if not request_row.uses_hard_reserve:
        raise FbsReplenishmentError(
            "Старая FBS-заявка отменяется по прежнему складскому процессу."
        )
    if request_row.status not in {
        FbsClientMovementRequest.STATUS_SUBMITTED,
        FbsClientMovementRequest.STATUS_APPROVED,
    }:
        raise FbsReplenishmentError(
            "Отменить можно только новую заявку до принятия складом."
        )
    if request_row.replenishment_plans.exists():
        raise FbsReplenishmentError("По заявке уже созданы складские планы.")
    _hard_reserve_allocations(request_row, lock=True)
    _release_hard_reserve(
        request_row,
        actor=actor,
        reason="Клиент отменил новую FBS-заявку",
    )
    request_row.status = FbsClientMovementRequest.STATUS_CANCELED
    request_row.save(update_fields=["status", "updated_at"])
    _log_request_status(
        request_row,
        user=actor,
        description="Клиент отменил FBS-перемещение; отдельный резерв снят",
    )
    return request_row


@transaction.atomic
def cancel_client_movement_by_warehouse(
    *,
    request_id: int,
    canceled_by=None,
    reason: str,
) -> FbsClientMovementRequest:
    actor = _require_actor_role(
        canceled_by,
        WAREHOUSE_ROLES,
        "Отменить FBS-перемещение может только сотрудник склада.",
    )
    reason = str(reason or "").strip()
    if not reason:
        raise FbsReplenishmentError("Укажите причину отмены заявки.")

    request_row = (
        FbsClientMovementRequest.objects.select_for_update()
        .select_related("agency")
        .get(pk=request_id)
    )
    if request_row.status == FbsClientMovementRequest.STATUS_CANCELED:
        return request_row
    if request_row.status not in {
        FbsClientMovementRequest.STATUS_SUBMITTED,
        FbsClientMovementRequest.STATUS_APPROVED,
        FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED,
        FbsClientMovementRequest.STATUS_IN_PROGRESS,
    }:
        raise FbsReplenishmentError(
            "Заявку нельзя отменить после фактического перемещения товара. "
            "Оформите отдельное обратное перемещение со сканированием."
        )

    plans = list(
        request_row.replenishment_plans.select_for_update().order_by("id")
    )
    if plans:
        from .replenishment import cancel_replenishment_plan

        source_box_target_ids = set(
            FbsReplenishmentLine.objects.filter(
                plan__in=plans,
                source_container__isnull=False,
                target_box__isnull=False,
            ).values_list("target_box_id", flat=True)
        )
        for plan in plans:
            cancel_replenishment_plan(plan_id=plan.id, canceled_by=actor)
        if source_box_target_ids:
            FbsBox.objects.filter(
                id__in=source_box_target_ids,
                source_container__isnull=True,
                status__in=(FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE),
            ).exclude(stock_balances__qty__gt=0).update(
                status=FbsBox.STATUS_ARCHIVED,
                updated_at=timezone.now(),
            )
    elif request_row.uses_hard_reserve:
        _hard_reserve_allocations(request_row, lock=True)
        _release_hard_reserve(
            request_row,
            actor=actor,
            reason=f"Склад отменил FBS-заявку: {reason}",
        )

    request_row.refresh_from_db()
    request_row.status = FbsClientMovementRequest.STATUS_CANCELED
    request_row.clarification_reason = reason
    request_row.save(
        update_fields=["status", "clarification_reason", "updated_at"]
    )
    _log_request_status(
        request_row,
        user=actor,
        description=(
            "Склад отменил FBS-перемещение; незапущенные задания отменены, "
            f"резерв возвращен в доступный остаток. Причина: {reason}"
        ),
    )
    _queue_client_movement_stock_export(request_row)
    return request_row
