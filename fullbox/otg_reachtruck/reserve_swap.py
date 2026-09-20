from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from reachtruck.models import BoxClaim, MoveTask
from reachtruck.services import task_commands as shared_execution
from shipping.models import ShippingOrder

try:
    from shipping.models import ShippingReservationUnit
except ImportError:
    ShippingReservationUnit = None
from shipping.reservation_units import ACTIVE_RESERVATION_STATUSES
from sklad.models import WarehouseEvent, WarehouseOperation, WarehouseOperationTask, WarehouseReserve, WarehouseStockSnapshot
from sklad.services.warehouse_events import WarehouseEventType
from sklad.services.warehouse_transitions import WarehouseTransitionError
from sklad.services.warehouse_write_path import WarehouseWritePathService


_ACTIVE_WAREHOUSE_RESERVE_STATUSES = [
    WarehouseReserve.STATUS_ACTIVE,
    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
    WarehouseReserve.STATUS_ALLOCATED,
    WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
    WarehouseReserve.STATUS_SATISFIED,
]

_FINAL_TASK_STATUSES = {
    MoveTask.STATUS_DONE,
    MoveTask.STATUS_CANCELED,
    MoveTask.STATUS_FAILED,
}


@dataclass
class OtgReserveSwapResult:
    ok: bool = True
    changed: bool = False
    error: str = ""


def _clean(value) -> str:
    return str(value or "").strip()


def _key(value) -> str:
    return _clean(value).lower()


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _ordered_codes(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_value in values or []:
        code = shared_execution._normalize_box_code(raw_value)
        code_key = code.lower()
        if not code or code_key in seen:
            continue
        seen.add(code_key)
        result.append(code)
    return result


def _payload_box_codes(payload: dict) -> list[str]:
    if hasattr(shared_execution, "_otg_payload_explicit_box_codes"):
        return _ordered_codes(shared_execution._otg_payload_explicit_box_codes(payload))
    result: list[str] = []
    for field in ("reserved_box_codes", "selected_box_codes", "planned_box_codes", "requested_boxes"):
        values = payload.get(field)
        if isinstance(values, (list, tuple, set)):
            result.extend(values)
    for field in ("reserved_box_code", "selected_box_code", "planned_box_code", "requested_box", "box_code"):
        result.append(payload.get(field))
    return _ordered_codes(result)


def _replace_payload_box_code(payload: dict, old_code: str, new_code: str) -> tuple[dict, bool]:
    old_key = _key(old_code)
    new_code = shared_execution._normalize_box_code(new_code)
    if not old_key or not new_code:
        return dict(payload or {}), False
    result = dict(payload or {})
    changed = False

    def replace_value(value):
        nonlocal changed
        if isinstance(value, dict):
            updated = dict(value)
            for key in ("box_code", "code", "container_code"):
                if _key(updated.get(key)) == old_key:
                    updated[key] = new_code
                    changed = True
            return updated
        if _key(value) == old_key:
            changed = True
            return new_code
        return value

    for field in ("reserved_box_codes", "selected_box_codes", "planned_box_codes", "requested_boxes"):
        values = result.get(field)
        if not isinstance(values, (list, tuple, set)):
            continue
        updated_values = []
        seen: set[str] = set()
        for value in values:
            updated = replace_value(value)
            code = ""
            if isinstance(updated, dict):
                code = shared_execution._normalize_box_code(
                    updated.get("box_code") or updated.get("code") or updated.get("container_code")
                )
            else:
                code = shared_execution._normalize_box_code(updated)
            code_key = code.lower()
            if code and code_key in seen:
                changed = True
                continue
            if code:
                seen.add(code_key)
            updated_values.append(updated)
        result[field] = updated_values

    for field in ("reserved_box_code", "selected_box_code", "planned_box_code", "requested_box", "box_code"):
        if _key(result.get(field)) == old_key:
            result[field] = new_code
            changed = True
    return result, changed


def _box_code(snapshot: WarehouseStockSnapshot | None) -> str:
    if snapshot is None:
        return ""
    container = getattr(snapshot, "container", None)
    return _clean(getattr(snapshot, "container_code", "") or getattr(container, "container_code", ""))


def _pallet_code(snapshot: WarehouseStockSnapshot | None) -> str:
    if snapshot is None:
        return ""
    parent = getattr(snapshot, "parent_container", None)
    return _clean(getattr(parent, "container_code", ""))


def _line_signature_from_snapshot(snapshot: WarehouseStockSnapshot, qty: int | None = None) -> tuple:
    return (
        _key(getattr(snapshot, "sku_code", "")),
        _key(getattr(snapshot, "barcode", "")),
        _key(getattr(snapshot, "size", "")),
        _key(getattr(snapshot, "goods_type", "")),
        _key(getattr(snapshot, "marking_code", "")),
        _as_int(qty if qty is not None else getattr(snapshot, "qty", 0)),
    )


def _line_signature_from_reserve(reserve: WarehouseReserve, qty: int) -> tuple:
    return (
        _key(getattr(reserve, "sku_code", "")),
        _key(getattr(reserve, "barcode", "")),
        _key(getattr(reserve, "size", "")),
        _key(getattr(reserve, "goods_type", "")),
        _key(getattr(reserve, "marking_code", "")),
        _as_int(qty),
    )


def _box_signature(snapshots: list[WarehouseStockSnapshot]) -> tuple:
    return tuple(sorted(_line_signature_from_snapshot(snapshot) for snapshot in snapshots))


def _fbs_claim_labels(snapshot_ids: set[int]) -> list[str]:
    if not snapshot_ids:
        return []
    context_ids = {
        _clean(context_id)
        for context_id in (
            WarehouseEvent.objects.filter(
                event_type="fbs_movement_reserved",
                reserve__reserve_type=WarehouseReserve.TYPE_FBS_MOVEMENT,
                reserve__status__in=_ACTIVE_WAREHOUSE_RESERVE_STATUSES,
                payload__source_snapshot_id__in=sorted(snapshot_ids),
            )
            .exclude(stock_context_id="")
            .values_list("stock_context_id", flat=True)
        )
        if _clean(context_id)
    }
    if not context_ids:
        return []
    try:
        from fbs.models import FbsClientMovementRequest
    except ImportError:
        return [f"FBS-перемещение #{context_id}" for context_id in sorted(context_ids)]
    existing_ids = {
        int(request_id)
        for request_id in FbsClientMovementRequest.objects.filter(
            id__in=[_as_int(context_id) for context_id in context_ids]
        ).values_list("id", flat=True)
    }
    return sorted(
        (
            f"FBS-MOV-{_as_int(context_id):06d}"
            if _as_int(context_id) in existing_ids
            else f"FBS-перемещение #{context_id}"
        )
        for context_id in context_ids
    )


def _box_is_free_for_current_shipping_order(
    *,
    agency,
    current_task: MoveTask,
    current_order_id: str,
    box_code: str,
    snapshots: list[WarehouseStockSnapshot],
) -> bool:
    if not snapshots:
        return False
    if any(
        snapshot.is_archived
        or snapshot.is_in_vehicle
        or snapshot.active_operation_id is not None
        or _as_int(snapshot.processing_reserved_qty) > 0
        or _as_int(snapshot.other_reserved_qty) > 0
        for snapshot in snapshots
    ):
        return False
    foreign_shipping_orders = _active_reserve_order_ids_for_box(
        agency,
        box_code,
        exclude_order_id=current_order_id,
    )
    foreign_shipping_orders.update(
        _active_unit_order_ids_for_box(
            agency,
            box_code,
            exclude_order_id=current_order_id,
        )
    )
    if foreign_shipping_orders:
        return False
    if BoxClaim.objects.filter(
        agency=agency,
        status=BoxClaim.STATUS_CLAIMED,
        box_code__iexact=box_code,
    ).select_for_update().exclude(move_task=current_task).exclude(
        move_task__status__in=_FINAL_TASK_STATUSES
    ).exists():
        return False
    owns_shipping_box = bool(
        _reserve_entries_for_order_box(agency, current_order_id, box_code)
    ) or current_order_id in _active_unit_order_ids_for_box(agency, box_code)
    if any(
        _as_int(snapshot.available_qty)
        + (_as_int(snapshot.shipping_reserved_qty) if owns_shipping_box else 0)
        < _as_int(snapshot.qty)
        for snapshot in snapshots
    ):
        return False
    try:
        WarehouseWritePathService._assert_shipping_does_not_take_fbs_stock(snapshots)
    except WarehouseTransitionError:
        return False
    return True


def _scan_fact_reserved_box_hint(
    *,
    agency,
    task: MoveTask,
    payload: dict,
    placement_payload: dict,
    scan_code: str,
) -> OtgReserveSwapResult | None:
    scanned_rows = _stock_snapshots_for_boxes(agency, [scan_code]).get(
        _key(scan_code),
        [],
    )
    if not scanned_rows:
        return None
    current_order_id = _clean(payload.get("shipping_order_id"))
    if _box_is_free_for_current_shipping_order(
        agency=agency,
        current_task=task,
        current_order_id=current_order_id,
        box_code=scan_code,
        snapshots=scanned_rows,
    ):
        return None

    pallet_code = _clean(payload.get("pallet_code") or task.pallet_code)
    matching_rows = shared_execution._matching_box_rows_for_flexible_selection(
        payload,
        placement_payload,
        pallet_code,
    )
    execution = dict(payload.get("mobile_execution") or {})
    scanned_keys = {
        _key(code)
        for code in execution.get("boxes_scanned") or []
        if _key(code)
    }
    candidate_codes = _ordered_codes(
        [
            row.get("code") or row.get("box_code") or row.get("container_code")
            for row in matching_rows
            if isinstance(row, dict)
        ]
    )
    snapshots_by_code = _stock_snapshots_for_boxes(
        agency,
        [code for code in candidate_codes if _key(code) != _key(scan_code)],
    )
    scanned_signature = _box_signature(scanned_rows)
    alternatives: list[str] = []
    for candidate_code in candidate_codes:
        candidate_key = _key(candidate_code)
        if (
            not candidate_key
            or candidate_key == _key(scan_code)
            or candidate_key in scanned_keys
        ):
            continue
        candidate_rows = snapshots_by_code.get(candidate_key, [])
        if _box_signature(candidate_rows) != scanned_signature:
            continue
        if not _box_is_free_for_current_shipping_order(
            agency=agency,
            current_task=task,
            current_order_id=current_order_id,
            box_code=candidate_code,
            snapshots=candidate_rows,
        ):
            continue
        alternatives.append(candidate_code)
        if len(alternatives) >= 3:
            break

    claim_labels = _fbs_claim_labels(
        {int(snapshot.id) for snapshot in scanned_rows if snapshot.id}
    )
    foreign_shipping_orders = _active_reserve_order_ids_for_box(
        agency,
        scan_code,
        exclude_order_id=current_order_id,
    )
    foreign_shipping_orders.update(
        _active_unit_order_ids_for_box(
            agency,
            scan_code,
            exclude_order_id=current_order_id,
        )
    )
    if claim_labels:
        owner_text = f" заявкой {', '.join(claim_labels)}"
    elif foreign_shipping_orders:
        owner_text = f" другой отгрузкой: {', '.join(sorted(foreign_shipping_orders))}"
    else:
        owner_text = " другой складской операцией"
    if alternatives:
        return OtgReserveSwapResult(
            ok=False,
            error=(
                f"Короб {scan_code} уже занят{owner_text}. Не берите его. "
                f"Отсканируйте другой свободный короб с этой паллеты: "
                f"{', '.join(alternatives)}."
            ),
        )
    return OtgReserveSwapResult(
        ok=False,
        error=(
            f"Короб {scan_code} уже занят{owner_text}. Не берите его. "
            "Свободного равнозначного короба на этой паллете сейчас нет; "
            "задание сохранено без списания товара."
        ),
    )


def _stock_snapshots_for_boxes(agency, box_codes: list[str]) -> dict[str, list[WarehouseStockSnapshot]]:
    codes = _ordered_codes(box_codes)
    if agency is None or not codes:
        return {}
    result: dict[str, list[WarehouseStockSnapshot]] = defaultdict(list)
    for snapshot in (
        WarehouseStockSnapshot.objects.select_for_update(of=("self",))
        .select_related("container", "parent_container", "location", "active_operation")
        .filter(agency=agency, is_archived=False, qty__gt=0)
        .filter(Q(container_code__in=codes) | Q(container__container_code__in=codes))
        .order_by("container_code", "id")
    ):
        code = _box_code(snapshot)
        if code:
            result[code.lower()].append(snapshot)
    return dict(result)


def _reserve_snapshot_map(reserves: list[WarehouseReserve]) -> dict[int, int]:
    return WarehouseWritePathService._reserve_snapshot_id_map(reserves)


def _reserve_open_qty(reserve: WarehouseReserve) -> int:
    return WarehouseWritePathService._reserve_open_qty(reserve)


def _active_reserves_for_order(agency, order_id: str) -> list[WarehouseReserve]:
    order_key = _clean(order_id)
    if agency is None or not order_key:
        return []
    return list(
        WarehouseReserve.objects.select_for_update()
        .filter(
            agency=agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id=order_key,
            status__in=_ACTIVE_WAREHOUSE_RESERVE_STATUSES,
        )
        .order_by("id")
    )


def _reserve_entries_for_order_box(agency, order_id: str, box_code: str) -> list[dict]:
    reserves = _active_reserves_for_order(agency, order_id)
    if not reserves:
        return []
    snapshot_ids = {
        snapshot_id
        for snapshot_id in _reserve_snapshot_map(reserves).values()
        if _as_int(snapshot_id) > 0
    }
    if not snapshot_ids:
        return []
    snapshots = {
        int(snapshot.id): snapshot
        for snapshot in (
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("container", "parent_container", "location", "active_operation")
            .filter(id__in=snapshot_ids, agency=agency, is_archived=False)
        )
    }
    snapshot_id_by_reserve = _reserve_snapshot_map(reserves)
    target_key = _key(box_code)
    entries: list[dict] = []
    for reserve in reserves:
        open_qty = _reserve_open_qty(reserve)
        if open_qty <= 0:
            continue
        snapshot = snapshots.get(_as_int(snapshot_id_by_reserve.get(int(reserve.id or 0))))
        if snapshot is None or _key(_box_code(snapshot)) != target_key:
            continue
        entries.append(
            {
                "reserve": reserve,
                "snapshot": snapshot,
                "qty": open_qty,
                "signature": _line_signature_from_reserve(reserve, open_qty),
            }
        )
    return entries


def _active_reserve_order_ids_for_box(agency, box_code: str, *, exclude_order_id: str = "") -> set[str]:
    if agency is None or not box_code:
        return set()
    reserves = list(
        WarehouseReserve.objects.select_for_update()
        .filter(
            agency=agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            status__in=_ACTIVE_WAREHOUSE_RESERVE_STATUSES,
        )
        .exclude(context_id=_clean(exclude_order_id))
        .order_by("id")
    )
    if not reserves:
        return set()
    snapshot_id_by_reserve = _reserve_snapshot_map(reserves)
    snapshot_ids = {
        snapshot_id
        for snapshot_id in snapshot_id_by_reserve.values()
        if _as_int(snapshot_id) > 0
    }
    snapshots = {
        int(snapshot.id): snapshot
        for snapshot in (
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("container")
            .filter(id__in=snapshot_ids, agency=agency, is_archived=False)
        )
    }
    target_key = _key(box_code)
    order_ids: set[str] = set()
    for reserve in reserves:
        if _reserve_open_qty(reserve) <= 0:
            continue
        snapshot = snapshots.get(_as_int(snapshot_id_by_reserve.get(int(reserve.id or 0))))
        if snapshot is None or _key(_box_code(snapshot)) != target_key:
            continue
        order_id = _clean(reserve.context_id)
        if order_id:
            order_ids.add(order_id)
    return order_ids


def _active_unit_order_ids_for_box(agency, box_code: str, *, exclude_order_id: str = "") -> set[str]:
    if ShippingReservationUnit is None or agency is None or not box_code:
        return set()
    result: set[str] = set()
    units = (
        ShippingReservationUnit.objects.select_for_update()
        .select_related("order")
        .filter(agency=agency, status__in=ACTIVE_RESERVATION_STATUSES)
        .order_by("id")
    )
    exclude_key = _clean(exclude_order_id)
    for unit in units:
        order = getattr(unit, "order", None)
        order_id = _clean(getattr(order, "number", ""))
        if not order_id or order_id == exclude_key:
            continue
        if _unit_contains_box(unit, box_code):
            result.add(order_id)
    return result


def _current_order_reserve_codes(agency, order_id: str) -> list[str]:
    reserves = _active_reserves_for_order(agency, order_id)
    if not reserves:
        return []
    snapshot_id_by_reserve = _reserve_snapshot_map(reserves)
    snapshot_ids = {
        snapshot_id
        for snapshot_id in snapshot_id_by_reserve.values()
        if _as_int(snapshot_id) > 0
    }
    snapshots = {
        int(snapshot.id): snapshot
        for snapshot in (
            WarehouseStockSnapshot.objects.select_related("container")
            .filter(id__in=snapshot_ids, agency=agency, is_archived=False)
        )
    }
    result: list[str] = []
    for reserve in reserves:
        snapshot = snapshots.get(_as_int(snapshot_id_by_reserve.get(int(reserve.id or 0))))
        code = _box_code(snapshot)
        if code:
            result.append(code)
    return _ordered_codes(result)


def _operation_matches_reserve(snapshot: WarehouseStockSnapshot, reserve: WarehouseReserve) -> bool:
    operation = getattr(snapshot, "active_operation", None)
    if operation is None:
        return False
    return (
        operation.operation_type == WarehouseOperation.TYPE_MOVE_TO_OTG
        and _key(operation.context_type) == "shipping"
        and _clean(operation.context_id) == _clean(reserve.context_id)
    )


def _record_reserve_mapping_change(
    *,
    reserve: WarehouseReserve,
    from_snapshot: WarehouseStockSnapshot,
    to_snapshot: WarehouseStockSnapshot,
    qty: int,
    performed_by,
    reason: str,
) -> tuple[WarehouseOperation | None, int | None, int | None]:
    now = timezone.now()
    release_event = WarehouseEvent.objects.create(
        agency=from_snapshot.agency,
        event_type=WarehouseEventType.SHIPPING_RESERVE_RELEASED.value,
        stock_context_type=reserve.context_type,
        stock_context_id=reserve.context_id,
        container=from_snapshot.container,
        reserve=reserve,
        source_document_type=reserve.source_document_type,
        source_document_id=reserve.source_document_id or reserve.context_id,
        from_location=from_snapshot.location,
        to_location=from_snapshot.location,
        from_zone_code=from_snapshot.zone_code,
        to_zone_code=from_snapshot.zone_code,
        qty=qty,
        performed_by=performed_by if getattr(performed_by, "is_authenticated", False) else None,
        performed_by_role=WarehouseWritePathService._role_of(performed_by),
        occurred_at=now,
        payload={
            "snapshot_id": from_snapshot.id,
            "box_code": _box_code(from_snapshot),
            "otg_cross_order_reserve_swap": True,
            "swap_reason": reason,
            "swap_to_snapshot_id": to_snapshot.id,
            "swap_to_box_code": _box_code(to_snapshot),
        },
    )
    reserve_event = WarehouseEvent.objects.create(
        agency=to_snapshot.agency,
        event_type=WarehouseEventType.SHIPPING_RESERVED.value,
        stock_context_type=reserve.context_type,
        stock_context_id=reserve.context_id,
        container=to_snapshot.container,
        reserve=reserve,
        source_document_type=reserve.source_document_type,
        source_document_id=reserve.source_document_id or reserve.context_id,
        from_location=to_snapshot.location,
        to_location=to_snapshot.location,
        from_zone_code=to_snapshot.zone_code,
        to_zone_code=to_snapshot.zone_code,
        qty=qty,
        performed_by=performed_by if getattr(performed_by, "is_authenticated", False) else None,
        performed_by_role=WarehouseWritePathService._role_of(performed_by),
        occurred_at=now,
        payload={
            "snapshot_id": to_snapshot.id,
            "box_code": _box_code(to_snapshot),
            "otg_cross_order_reserve_swap": True,
            "swap_reason": reason,
            "swap_from_snapshot_id": from_snapshot.id,
            "swap_from_box_code": _box_code(from_snapshot),
        },
    )
    from_snapshot.last_event = release_event
    to_snapshot.last_event = reserve_event
    operation = from_snapshot.active_operation if _operation_matches_reserve(from_snapshot, reserve) else None
    return operation, from_snapshot.container_id, to_snapshot.container_id


def _apply_operation_moves(operation_moves: list[tuple[WarehouseOperation | None, int | None, int | None]]) -> None:
    for operation, from_container_id, to_container_id in operation_moves:
        if operation is None or not from_container_id or not to_container_id:
            continue
        if int(from_container_id) == int(to_container_id):
            continue
        WarehouseOperationTask.objects.filter(
            operation=operation,
            container_id=from_container_id,
            status__in=[
                WarehouseOperationTask.STATUS_CREATED,
                WarehouseOperationTask.STATUS_IN_PROGRESS,
            ],
        ).update(container_id=to_container_id, updated_at=timezone.now())


def _swap_warehouse_reserve_mappings(
    *,
    agency,
    current_order_id: str,
    donor_order_id: str,
    current_reserved_box: str,
    donor_scanned_box: str,
    performed_by,
) -> tuple[bool, str]:
    current_entries = _reserve_entries_for_order_box(agency, current_order_id, current_reserved_box)
    donor_entries = _reserve_entries_for_order_box(agency, donor_order_id, donor_scanned_box)
    if not donor_entries:
        return True, ""
    if not current_entries:
        return False, (
            f"Короб {donor_scanned_box} зарезервирован под заявку {donor_order_id}, "
            "но в текущей заявке нет свободного эквивалентного короба для обмена."
        )

    box_snapshots = _stock_snapshots_for_boxes(agency, [current_reserved_box, donor_scanned_box])
    current_snapshots = box_snapshots.get(_key(current_reserved_box), [])
    donor_snapshots = box_snapshots.get(_key(donor_scanned_box), [])
    if _box_signature(current_snapshots) != _box_signature(donor_snapshots):
        return False, (
            f"Короб {donor_scanned_box} нельзя заменить коробом {current_reserved_box}: состав коробов отличается."
        )

    donor_targets: dict[tuple, list[WarehouseStockSnapshot]] = defaultdict(list)
    current_targets: dict[tuple, list[WarehouseStockSnapshot]] = defaultdict(list)
    for snapshot in donor_snapshots:
        donor_targets[_line_signature_from_snapshot(snapshot)].append(snapshot)
    for snapshot in current_snapshots:
        current_targets[_line_signature_from_snapshot(snapshot)].append(snapshot)

    operation_moves: list[tuple[WarehouseOperation | None, int | None, int | None]] = []
    affected_snapshots: dict[int, WarehouseStockSnapshot] = {}
    for entry in current_entries:
        targets = donor_targets.get(entry["signature"]) or []
        if not targets:
            return False, f"Для резерва {current_order_id} не найден нужный состав в коробе {donor_scanned_box}."
        target = targets.pop(0)
        affected_snapshots[int(entry["snapshot"].id)] = entry["snapshot"]
        affected_snapshots[int(target.id)] = target
        operation_moves.append(
            _record_reserve_mapping_change(
                reserve=entry["reserve"],
                from_snapshot=entry["snapshot"],
                to_snapshot=target,
                qty=_as_int(entry["qty"]),
                performed_by=performed_by,
                reason="otg_equivalent_box_taken",
            )
        )

    for entry in donor_entries:
        targets = current_targets.get(entry["signature"]) or []
        if not targets:
            return False, f"Для заявки {donor_order_id} не найден нужный состав в коробе {current_reserved_box}."
        target = targets.pop(0)
        affected_snapshots[int(entry["snapshot"].id)] = entry["snapshot"]
        affected_snapshots[int(target.id)] = target
        operation_moves.append(
            _record_reserve_mapping_change(
                reserve=entry["reserve"],
                from_snapshot=entry["snapshot"],
                to_snapshot=target,
                qty=_as_int(entry["qty"]),
                performed_by=performed_by,
                reason="otg_equivalent_box_given_back",
            )
        )

    desired_operations: dict[int, WarehouseOperation | None] = {}
    for operation, from_container_id, to_container_id in operation_moves:
        if operation is None:
            continue
        for snapshot in affected_snapshots.values():
            if int(snapshot.container_id or 0) == int(from_container_id or 0):
                desired_operations[int(snapshot.id)] = None
            if int(snapshot.container_id or 0) == int(to_container_id or 0):
                desired_operations[int(snapshot.id)] = operation
    for snapshot_id, snapshot in affected_snapshots.items():
        update_fields = ["last_event"]
        if snapshot_id in desired_operations:
            snapshot.active_operation = desired_operations[snapshot_id]
            snapshot.active_operation_type = desired_operations[snapshot_id].operation_type if desired_operations[snapshot_id] else ""
            update_fields.extend(["active_operation", "active_operation_type"])
        snapshot.save(update_fields=update_fields + ["updated_at"])
    _apply_operation_moves(operation_moves)
    return True, ""


def _find_shipping_order(agency, order_id: str, order_pk=None) -> ShippingOrder | None:
    qs = ShippingOrder.objects.select_for_update().filter(agency=agency)
    if order_pk:
        order = qs.filter(pk=order_pk).order_by("-id").first()
        if order is not None:
            return order
    return qs.filter(number=_clean(order_id)).order_by("-id").first()


def _unit_contains_box(unit: ShippingReservationUnit, box_code: str) -> bool:
    target_key = _key(box_code)
    if not target_key:
        return False
    if _key(unit.box_code) == target_key:
        return True
    payload = unit.payload if isinstance(unit.payload, dict) else {}
    for field in ("planned_box_codes", "actual_box_codes", "reserved_box_codes", "selected_box_codes", "box_codes"):
        for code in payload.get(field) or []:
            if _key(code) == target_key:
                return True
    return False


def _replace_unit_box_code(unit: ShippingReservationUnit, old_code: str, new_code: str, snapshots: list[WarehouseStockSnapshot]) -> bool:
    old_key = _key(old_code)
    new_code = shared_execution._normalize_box_code(new_code)
    if not old_key or not new_code:
        return False
    payload = dict(unit.payload or {})
    changed = False
    for field in ("planned_box_codes", "actual_box_codes", "reserved_box_codes", "selected_box_codes", "box_codes"):
        values = payload.get(field)
        if not isinstance(values, (list, tuple, set)):
            continue
        updated = []
        seen: set[str] = set()
        for raw_code in values:
            code = shared_execution._normalize_box_code(raw_code)
            if _key(code) == old_key:
                code = new_code
                changed = True
            code_key = code.lower()
            if code and code_key in seen:
                changed = True
                continue
            if code:
                seen.add(code_key)
            updated.append(code)
        payload[field] = updated
    if _key(unit.box_code) == old_key:
        unit.box_code = new_code
        changed = True
    if snapshots:
        first = snapshots[0]
        unit.pallet_code = _pallet_code(first)
        unit.snapshot_id = first.id if unit.reserve_mode == ShippingReservationUnit.MODE_FIXED_BOX else unit.snapshot_id
        unit.source_zone = _clean(getattr(first, "zone_code", ""))
        location = getattr(first, "location", None)
        unit.source_location = _clean(location) if location is not None else unit.source_location
        changed = True
    if not changed:
        return False
    unit.payload = payload
    unit.save(update_fields=["payload", "box_code", "pallet_code", "snapshot_id", "source_zone", "source_location", "updated_at"])
    return True


def _update_reservation_units(
    *,
    agency,
    current_order_id: str,
    donor_order_id: str,
    current_reserved_box: str,
    donor_scanned_box: str,
) -> int:
    if ShippingReservationUnit is None:
        return 0
    changed = 0
    box_snapshots = _stock_snapshots_for_boxes(agency, [current_reserved_box, donor_scanned_box])
    current_order = _find_shipping_order(agency, current_order_id)
    donor_order = _find_shipping_order(agency, donor_order_id)
    if current_order is not None:
        current_units = list(
            current_order.reservation_units.select_for_update()
            .filter(status__in=ACTIVE_RESERVATION_STATUSES)
            .order_by("id")
        )
        for unit in current_units:
            if _unit_contains_box(unit, current_reserved_box):
                changed += int(
                    _replace_unit_box_code(
                        unit,
                        current_reserved_box,
                        donor_scanned_box,
                        box_snapshots.get(_key(donor_scanned_box), []),
                    )
                )
    if donor_order is not None:
        donor_units = list(
            donor_order.reservation_units.select_for_update()
            .filter(status__in=ACTIVE_RESERVATION_STATUSES)
            .order_by("id")
        )
        for unit in donor_units:
            if _unit_contains_box(unit, donor_scanned_box):
                changed += int(
                    _replace_unit_box_code(
                        unit,
                        donor_scanned_box,
                        current_reserved_box,
                        box_snapshots.get(_key(current_reserved_box), []),
                    )
                )
    return changed


def _sync_plan(payload: dict) -> None:
    if hasattr(shared_execution, "_sync_otg_plan_live_pallet"):
        shared_execution._sync_otg_plan_live_pallet(payload, _payload_box_codes(payload))


def _update_open_donor_tasks(
    *,
    agency,
    current_task: MoveTask,
    donor_order_id: str,
    donor_scanned_box: str,
    current_reserved_box: str,
    performed_by,
) -> int:
    changed = 0
    replacement_snapshot = (_stock_snapshots_for_boxes(agency, [current_reserved_box]).get(_key(current_reserved_box)) or [None])[0]
    tasks = (
        MoveTask.objects.select_for_update()
        .select_related("request")
        .filter(request__agency=agency, status__in=[MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS])
        .exclude(pk=current_task.pk)
        .order_by("id")
    )
    for task in tasks:
        payload = dict(task.payload or {})
        if _clean(payload.get("shipping_order_id")) != _clean(donor_order_id):
            continue
        execution = dict(payload.get("mobile_execution") or {})
        scanned_keys = {_key(code) for code in execution.get("boxes_scanned") or [] if _key(code)}
        if _key(donor_scanned_box) in scanned_keys:
            continue
        if _key(donor_scanned_box) not in {_key(code) for code in _payload_box_codes(payload)}:
            continue
        payload, replaced = _replace_payload_box_code(payload, donor_scanned_box, current_reserved_box)
        if not replaced:
            continue
        if replacement_snapshot is not None and hasattr(shared_execution, "_otg_apply_source_snapshot_to_task"):
            payload = shared_execution._otg_apply_source_snapshot_to_task(task, payload, replacement_snapshot)
        payload.setdefault("otg_reserve_swaps", []).append(
            {
                "reason": "box_taken_by_other_otg_task",
                "old_box_code": donor_scanned_box,
                "new_box_code": current_reserved_box,
                "other_task_id": current_task.id,
                "swapped_at": timezone.localtime().isoformat(),
            }
        )
        _sync_plan(payload)
        task.payload = payload
        update_fields = ["payload"]
        existing_fields = {field.name for field in task._meta.fields}
        for field in ("pallet_code", "from_zone", "from_row", "from_section", "from_tier", "from_cell"):
            if field in existing_fields and field not in update_fields:
                update_fields.append(field)
        if "updated_at" in existing_fields:
            update_fields.append("updated_at")
        task.save(update_fields=update_fields)
        if hasattr(shared_execution, "_log_otg_box_replacement_history"):
            shared_execution._log_otg_box_replacement_history(
                task,
                payload,
                [{"old_box_code": donor_scanned_box, "new_box_code": current_reserved_box}],
                reason="box_taken_by_other_otg_task",
                from_pallet_code=_clean(current_task.pallet_code),
                to_pallet_code=_clean(current_task.pallet_code),
                user=performed_by,
            )
        changed += 1
    return changed


def _scan_matches_current_pallet(payload: dict, placement_payload: dict, scan_code: str) -> bool:
    pallet_code = _clean(payload.get("pallet_code"))
    if not pallet_code:
        return False
    if _clean(shared_execution._requested_box_selection_mode(payload)) == "pattern_matching":
        execution = dict(payload.get("mobile_execution") or {})
        prospective = _ordered_codes(list(execution.get("boxes_scanned") or []) + [scan_code])
        resolved = shared_execution._assign_requested_patterns_to_boxes(
            payload,
            placement_payload,
            pallet_code,
            prospective,
            allow_partial=True,
        )
        return _key(scan_code) in {_key(code) for code in resolved}
    matching_rows = shared_execution._matching_box_rows_for_flexible_selection(payload, placement_payload, pallet_code)
    return _key(scan_code) in {_key(row.get("code")) for row in matching_rows}


def _box_is_on_current_pallet(payload: dict, placement_payload: dict, scan_code: str) -> bool:
    pallet_code = _clean(payload.get("pallet_code"))
    if not pallet_code:
        return False
    _pallets, _idx, pallet = shared_execution._find_pallet_in_placement(placement_payload, pallet_code)
    if not pallet:
        return False
    return _key(scan_code) in {_key(code) for code in (pallet.get("boxes") or [])}


def _find_replacement_box(
    *,
    agency,
    current_task: MoveTask,
    current_order_id: str,
    payload: dict,
    scan_code: str,
    scanned_keys: set[str],
) -> str:
    current_codes = _ordered_codes([*_payload_box_codes(payload), *_current_order_reserve_codes(agency, current_order_id)])
    candidates = [
        code
        for code in current_codes
        if _key(code) and _key(code) != _key(scan_code) and _key(code) not in scanned_keys
    ]
    if not candidates:
        return ""
    claimed_keys = {
        _key(code)
        for code in (
            BoxClaim.objects.filter(agency=agency, status=BoxClaim.STATUS_CLAIMED)
            .exclude(move_task=current_task)
            .exclude(move_task__status__in=_FINAL_TASK_STATUSES)
            .values_list("box_code", flat=True)
        )
        if _key(code)
    }
    snapshots = _stock_snapshots_for_boxes(agency, [scan_code, *candidates])
    scan_signature = _box_signature(snapshots.get(_key(scan_code), []))
    if not scan_signature:
        return ""
    for candidate in candidates:
        if _key(candidate) in claimed_keys:
            continue
        candidate_snapshots = snapshots.get(_key(candidate), [])
        if not candidate_snapshots:
            continue
        if _box_signature(candidate_snapshots) != scan_signature:
            continue
        return candidate
    return ""


def prepare_otg_box_scan(
    *,
    legacy_order_id: str,
    scan_value: str,
    user,
) -> OtgReserveSwapResult:
    if not connection.in_atomic_block:
        with transaction.atomic():
            return prepare_otg_box_scan(
                legacy_order_id=legacy_order_id,
                scan_value=scan_value,
                user=user,
            )

    scan_code = shared_execution._normalize_box_code(scan_value)
    if not scan_code:
        return OtgReserveSwapResult()

    task = shared_execution._load_task(legacy_order_id, for_update=True)
    if task is None:
        return OtgReserveSwapResult()
    agency = getattr(getattr(task, "request", None), "agency", None)
    if agency is None:
        return OtgReserveSwapResult()

    payload = dict(task.payload or {})
    uses_scan_facts = shared_execution._uses_otg_scan_fact_mode(payload)
    to_zone = shared_execution._normalize_zone_code(
        (payload.get("to_location") or {}).get("zone") or payload.get("to_zone") or getattr(task, "to_zone", "")
    )
    if to_zone != "OTG":
        return OtgReserveSwapResult()
    move_mode = shared_execution._normalize_move_mode(payload.get("move_mode"), payload.get("pick_mode"))
    if move_mode != "box_full":
        return OtgReserveSwapResult()
    if not shared_execution._is_any_matching_box_selection(payload):
        return OtgReserveSwapResult()
    if shared_execution._pallet_choice_pending(payload):
        return OtgReserveSwapResult()
    if shared_execution._task_payload_status(task, payload) != MoveTask.STATUS_IN_PROGRESS:
        return OtgReserveSwapResult()

    payload, placement_payload, _placement_source, payload_changed, error = shared_execution._ensure_mobile_runtime_payload(
        task,
        payload,
    )
    if error:
        return OtgReserveSwapResult()
    if payload_changed:
        task.payload = payload
        task.save(update_fields=["payload", "updated_at"])

    execution = dict(payload.get("mobile_execution") or {})
    if not execution.get("pallet_confirmed") or execution.get("destination_confirmed"):
        return OtgReserveSwapResult()
    if not _box_is_on_current_pallet(payload, placement_payload, scan_code):
        return OtgReserveSwapResult()
    if not _scan_matches_current_pallet(payload, placement_payload, scan_code):
        return OtgReserveSwapResult()

    if uses_scan_facts:
        reserved_hint = _scan_fact_reserved_box_hint(
            agency=agency,
            task=task,
            payload=payload,
            placement_payload=placement_payload,
            scan_code=scan_code,
        )
        return reserved_hint or OtgReserveSwapResult()

    conflict = (
        BoxClaim.objects.select_for_update()
        .filter(agency=agency, status=BoxClaim.STATUS_CLAIMED, box_code__iexact=scan_code)
        .exclude(move_task=task)
        .exclude(move_task__status__in=_FINAL_TASK_STATUSES)
        .order_by("id")
        .first()
    )
    if conflict:
        return OtgReserveSwapResult(
            ok=False,
            error=f"Короб {scan_code} уже взят в работу по другому заданию.",
        )

    current_order_id = _clean(payload.get("shipping_order_id"))
    if not current_order_id:
        return OtgReserveSwapResult()
    donor_order_ids = _active_reserve_order_ids_for_box(agency, scan_code, exclude_order_id=current_order_id)
    donor_order_ids.update(_active_unit_order_ids_for_box(agency, scan_code, exclude_order_id=current_order_id))
    if not donor_order_ids:
        return OtgReserveSwapResult()
    if len(donor_order_ids) > 1:
        return OtgReserveSwapResult(
            ok=False,
            error=f"Короб {scan_code} зарезервирован сразу под несколько заявок: {', '.join(sorted(donor_order_ids))}.",
        )
    donor_order_id = next(iter(donor_order_ids))

    scanned_keys = {_key(code) for code in execution.get("boxes_scanned") or [] if _key(code)}
    replacement_box = _find_replacement_box(
        agency=agency,
        current_task=task,
        current_order_id=current_order_id,
        payload=payload,
        scan_code=scan_code,
        scanned_keys=scanned_keys,
    )
    if not replacement_box:
        return OtgReserveSwapResult(
            ok=False,
            error=(
                f"Короб {scan_code} зарезервирован под заявку {donor_order_id}. "
                "Для обмена не найден свободный эквивалентный короб текущей заявки."
            ),
        )

    swapped, swap_error = _swap_warehouse_reserve_mappings(
        agency=agency,
        current_order_id=current_order_id,
        donor_order_id=donor_order_id,
        current_reserved_box=replacement_box,
        donor_scanned_box=scan_code,
        performed_by=user,
    )
    if not swapped:
        return OtgReserveSwapResult(ok=False, error=swap_error)

    payload, current_changed = _replace_payload_box_code(payload, replacement_box, scan_code)
    payload.setdefault("otg_reserve_swaps", []).append(
        {
            "reason": "driver_scanned_equivalent_foreign_reserved_box",
            "taken_box": scan_code,
            "given_box": replacement_box,
            "donor_order_id": donor_order_id,
            "swapped_at": timezone.localtime().isoformat(),
        }
    )
    _sync_plan(payload)
    task.payload = payload
    task.save(update_fields=["payload", "updated_at"])

    _update_reservation_units(
        agency=agency,
        current_order_id=current_order_id,
        donor_order_id=donor_order_id,
        current_reserved_box=replacement_box,
        donor_scanned_box=scan_code,
    )
    _update_open_donor_tasks(
        agency=agency,
        current_task=task,
        donor_order_id=donor_order_id,
        donor_scanned_box=scan_code,
        current_reserved_box=replacement_box,
        performed_by=user,
    )

    if hasattr(shared_execution, "_log_otg_box_replacement_history"):
        shared_execution._log_otg_box_replacement_history(
            task,
            payload,
            [{"old_box_code": replacement_box, "new_box_code": scan_code}],
            reason="driver_scanned_equivalent_foreign_reserved_box",
            from_pallet_code=_clean(payload.get("pallet_code")),
            to_pallet_code=_clean(payload.get("pallet_code")),
            user=user,
            extra_payload={
                "donor_order_id": donor_order_id,
                "given_box": replacement_box,
                "taken_box": scan_code,
            },
        )
    return OtgReserveSwapResult(ok=True, changed=True or current_changed)
