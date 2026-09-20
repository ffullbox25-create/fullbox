from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from employees.models import Employee
from reachtruck.services.putaway_planner import putaway_location_label, putaway_location_scan_code
from reachtruck.services.missing_box_reports import create_missing_box_verification_task
from reachtruck_free.services import clean_code, inspect_box, inspect_pallet, readable_scan_text
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseOperation,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sklad.services.warehouse_stock_rows import normalize_stock_row_from_snapshot
from sklad.services.warehouse_write_path import WarehouseWritePathService
from todo.models import Task

from .models import BoxMoveOperation


SESSION_OPERATION_KEY = "reachtruck_box_move_operation_id"
SESSION_LAST_BOX_KEY = "reachtruck_box_move_last_box"
ALLOWED_ROLES = (
    "reachtruck_driver",
    "super_car",
    "storekeeper",
    "manager",
    "head_manager",
    "director",
    "admin",
)
OPEN_STATUSES = {
    BoxMoveOperation.STATUS_SELECTING,
    BoxMoveOperation.STATUS_DESTINATION,
    BoxMoveOperation.STATUS_VERIFYING,
}
FINAL_OPERATION_STATUSES = {
    WarehouseOperation.STATUS_DONE,
    WarehouseOperation.STATUS_CANCELED,
}
FREE_STORAGE_STATE = "stored"
FREE_STORAGE_ZONE_CODES = {"OS", "MR"}
ACTIVE_RESERVE_STATUSES = {
    WarehouseReserve.STATUS_ACTIVE,
    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
    WarehouseReserve.STATUS_ALLOCATED,
    WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
}


def _user_display_name(user) -> str:
    if user is None:
        return "не указан"
    return (
        clean_code(getattr(user, "get_full_name", lambda: "")())
        or clean_code(getattr(user, "username", ""))
        or "не указан"
    )


def _local_datetime(value) -> str:
    if value is None:
        return "время не указано"
    return timezone.localtime(value).strftime("%d.%m.%Y %H:%M")


def _source_reference(source_type: str, source_id: str) -> str:
    source_key = clean_code(source_type).lower()
    identifier = clean_code(source_id)
    if not identifier:
        return ""
    labels = {
        "fbs_plan": "план FBS",
        "fbs_replenishment": "пополнение FBS",
        "shipping": "заявка на отгрузку",
        "shipping_order": "заявка на отгрузку",
        "processing": "заявка на обработку",
        "processing_order": "заявка на обработку",
        "processing_obr_request": "заявка на обработку",
        "box_move_verification": "проверка перемещения",
        "missing_box_check": "проверка отсутствующего короба",
    }
    label = labels.get(source_key, source_key.replace("_", " ") or "заявка")
    return f"{label} №{identifier}"


def _reserve_candidates_for_snapshots(snapshots: list[WarehouseStockSnapshot]) -> list[WarehouseReserve]:
    snapshot_ids = [int(snapshot.id) for snapshot in snapshots if int(snapshot.id or 0) > 0]
    container_ids = [int(snapshot.container_id) for snapshot in snapshots if int(snapshot.container_id or 0) > 0]
    if not snapshot_ids:
        return []
    event_query = WarehouseEvent.objects.exclude(reserve_id__isnull=True)
    if container_ids:
        event_query = event_query.filter(container_id__in=container_ids)
    else:
        event_query = event_query.filter(
            Q(payload__snapshot_id__in=snapshot_ids)
            | Q(payload__source_snapshot_id__in=snapshot_ids)
        )
    reserve_ids = {
        int(reserve_id)
        for reserve_id in event_query.values_list("reserve_id", flat=True).distinct()
        if reserve_id
    }
    if not reserve_ids:
        return []
    return list(
        WarehouseReserve.objects.filter(id__in=reserve_ids)
        .select_related("created_by", "released_by")
        .order_by("-created_at", "-id")
    )


def _original_reserve_for_operation(
    snapshots: list[WarehouseStockSnapshot],
    operation: WarehouseOperation,
) -> WarehouseReserve | None:
    source_id = clean_code(operation.source_document_id)
    for reserve in _reserve_candidates_for_snapshots(snapshots):
        if reserve.id == operation.reserve_id or clean_code(reserve.context_type) == "missing_box_check":
            continue
        if source_id and source_id not in {
            clean_code(reserve.context_id),
            clean_code(reserve.source_document_id),
        }:
            continue
        return reserve
    return None


def _active_operation_block_message(snapshots: list[WarehouseStockSnapshot]) -> str:
    operation_ids = {
        int(snapshot.active_operation_id)
        for snapshot in snapshots
        if snapshot.active_operation_id
    }
    if not operation_ids:
        return ""
    operations = list(
        WarehouseOperation.objects.filter(id__in=operation_ids)
        .exclude(status__in=FINAL_OPERATION_STATUSES)
        .select_related("requested_by", "reserve", "reserve__created_by")
        .order_by("id")
    )
    if not operations:
        return ""
    operation = next(
        (row for row in operations if clean_code(row.context_type) == "missing_box_check"),
        operations[0],
    )
    source = _source_reference(
        operation.source_document_type or operation.context_type,
        operation.source_document_id or operation.context_id,
    )
    actor = operation.requested_by
    actor_text = _user_display_name(actor)
    created_text = _local_datetime(operation.created_at)
    if clean_code(operation.context_type) == "missing_box_check":
        parts = [
            "Короб заблокирован: он отмечен отсутствующим и ожидает проверки",
            f"Проверка №{operation.id}",
        ]
        if source:
            parts.append(source)
        original_reserve = _original_reserve_for_operation(snapshots, operation)
        if original_reserve is not None:
            parts.append(
                f"автор исходного резерва №{original_reserve.id}: "
                f"{_user_display_name(original_reserve.created_by)}, "
                f"{_local_datetime(original_reserve.created_at)}"
            )
            parts.append(
                f"исходный резерв сейчас: {clean_code(original_reserve.get_status_display()).lower()}"
            )
        parts.append(f"«Нет короба» отметил {actor_text} {created_text}")
        reserve = operation.reserve
        if reserve is not None and reserve.status in ACTIVE_RESERVE_STATUSES:
            reserve_qty = max(int(reserve.qty_reserved or 0) - int(reserve.qty_satisfied or 0), 0)
            parts.append(f"карантинный резерв №{reserve.id}: {reserve_qty} шт.")
        parts.append("Перемещение будет доступно после проверки начальником склада.")
        return "; ".join(parts)

    operation_name = clean_code(operation.get_operation_type_display()) or "складская операция"
    parts = [f"Короб занят: {operation_name}, операция №{operation.id}"]
    if source:
        parts.append(source)
    parts.append(f"создал {actor_text} {created_text}")
    return "; ".join(parts)


def _active_reserves_for_snapshots(snapshots: list[WarehouseStockSnapshot]) -> list[WarehouseReserve]:
    return [
        reserve
        for reserve in _reserve_candidates_for_snapshots(snapshots)
        if reserve.status in ACTIVE_RESERVE_STATUSES
        if int(reserve.qty_reserved or 0) - int(reserve.qty_satisfied or 0) > 0
    ]


def _processing_context_block_message(snapshots: list[WarehouseStockSnapshot]) -> str:
    processing_ids = {
        clean_code(snapshot.source_context_id)
        for snapshot in snapshots
        if clean_code(snapshot.source_context_type).lower() == "processing"
        and clean_code(snapshot.source_context_id)
    }
    if len(processing_ids) != 1:
        return ""
    processing_id = next(iter(processing_ids))
    active_task = (
        Task.objects.filter(route__startswith=f"/orders/processing/{processing_id}/")
        .exclude(status="done")
        .order_by("-created_at", "-id")
        .first()
    )
    if active_task is not None:
        task_title = clean_code(active_task.title) or "этап обработки не завершён"
        return (
            "Это не резерв. Короб недоступен: он относится к заявке на обработку "
            f"№{processing_id}. Активный этап: «{task_title}». "
            "Сначала завершите или отмените этот этап обработки."
        )
    return (
        "Это не резерв. В карточке короба количество отмечено недоступным после обработки "
        f"№{processing_id}, но открытый этап обработки не найден. "
        "Нужно согласовать возврат результата обработки в складской остаток."
    )


def _reserve_block_message(snapshots: list[WarehouseStockSnapshot]) -> str:
    reserved_qty = sum(
        int(snapshot.processing_reserved_qty or 0)
        + int(snapshot.shipping_reserved_qty or 0)
        + int(snapshot.other_reserved_qty or 0)
        for snapshot in snapshots
    )
    unavailable_qty = sum(
        max(int(snapshot.qty or 0) - int(snapshot.available_qty or 0), 0)
        for snapshot in snapshots
    )
    blocked_qty = max(reserved_qty, unavailable_qty)
    if blocked_qty <= 0:
        return ""
    reserves = _active_reserves_for_snapshots(snapshots)
    if not reserves:
        processing_message = _processing_context_block_message(snapshots)
        if processing_message:
            return processing_message
        if reserved_qty <= 0:
            return (
                f"Это не резерв. В карточке короба недоступно {unavailable_qty} шт., "
                "но активная складская операция не найдена. "
                "Это несогласованное состояние остатка; передайте короб начальнику склада для проверки."
            )
        return (
            f"В карточке короба числится резерв {blocked_qty} шт., но активная заявка резерва не найдена. "
            "Это несогласованный остаток; передайте короб начальнику склада для проверки."
        )
    reserve = reserves[0]
    reserve_qty = max(int(reserve.qty_reserved or 0) - int(reserve.qty_satisfied or 0), 0)
    source = _source_reference(
        reserve.source_document_type or reserve.context_type,
        reserve.source_document_id or reserve.context_id,
    )
    parts = [
        f"Короб зарезервирован: {clean_code(reserve.get_reserve_type_display()).lower()}",
        f"резерв №{reserve.id}: {reserve_qty} шт.",
    ]
    if source:
        parts.append(source)
    if clean_code(reserve.context_type) == "missing_box_check" and reserve.created_by is None:
        parts.append(f"карантин установлен системой {_local_datetime(reserve.created_at)}")
    else:
        parts.append(
            f"зарезервировал {_user_display_name(reserve.created_by)} {_local_datetime(reserve.created_at)}"
        )
    if len(reserves) > 1:
        parts.append(f"всего активных резервов: {len(reserves)}")
    return "; ".join(parts)


def _agency_display_name(agency) -> str:
    return (
        clean_code(getattr(agency, "short_name", ""))
        or clean_code(getattr(agency, "agn_name", ""))
        or clean_code(getattr(agency, "name", ""))
        or (str(agency) if agency else "")
    )


def _short_agency_name(value: str) -> str:
    text = clean_code(value)
    if len(text) <= 18:
        return text
    return f"{text[:17].rstrip()}."


def _location_dict_from_snapshot(snapshot: WarehouseStockSnapshot | None) -> dict:
    location = getattr(snapshot, "location", None)
    if location is None:
        return {"zone": clean_code(getattr(snapshot, "zone_code", "")).upper(), "row": "", "section": "", "tier": "", "cell": ""}
    return {
        "zone": clean_code(location.zone_code).upper(),
        "row": int(location.row_no or 0) or "",
        "section": int(location.section_no or 0) or "",
        "tier": int(location.tier_no or 0) or "",
        "cell": int(location.cell_no or 0) or "",
    }


def _box_snapshot_query(box_code: str):
    normalized = clean_code(box_code)
    if not normalized:
        return WarehouseStockSnapshot.objects.none()
    return (
        WarehouseStockSnapshot.objects.filter(is_archived=False, qty__gt=0)
        .select_related("agency", "sku_ref", "container", "parent_container", "location", "active_operation")
        .filter(
            Q(container__container_type=WarehouseContainer.TYPE_BOX, container__container_code__iexact=normalized)
            | Q(container_code__iexact=normalized, parent_container__isnull=False)
        )
        .order_by("agency_id", "id")
    )


def _pallet_snapshot_query(pallet_code: str):
    normalized = clean_code(pallet_code)
    if not normalized:
        return WarehouseStockSnapshot.objects.none()
    return (
        WarehouseStockSnapshot.objects.filter(is_archived=False, qty__gt=0)
        .select_related("agency", "sku_ref", "container", "parent_container", "location", "active_operation")
        .filter(
            Q(container_code__iexact=normalized)
            | Q(container__container_code__iexact=normalized)
            | Q(parent_container__container_code__iexact=normalized)
        )
        .order_by("agency_id", "id")
    )


def _lines_from_rows(rows: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str, str, str, str], dict] = {}
    for row in rows:
        key = (
            clean_code(row.get("sku")),
            clean_code(row.get("name")),
            clean_code(row.get("size")),
            clean_code(row.get("barcode")),
            clean_code(row.get("goods_type")),
        )
        bucket = grouped.setdefault(
            key,
            {
                "sku": key[0] or "-",
                "name": key[1],
                "size": key[2],
                "barcode": key[3],
                "goods_type": key[4],
                "qty": 0,
            },
        )
        bucket["qty"] += int(row.get("qty") or 0)
    return sorted(grouped.values(), key=lambda item: (item["sku"], item["barcode"], item["name"]))


def _box_card_from_snapshots(box_code: str, snapshots: list[WarehouseStockSnapshot]) -> dict:
    rows = [normalize_stock_row_from_snapshot(snapshot) for snapshot in snapshots]
    rows = [row for row in rows if row]
    first = snapshots[0] if snapshots else None
    first_row = rows[0] if rows else {}
    location_dict = _location_dict_from_snapshot(first)
    location_label = putaway_location_label(location_dict)
    location_scan_code = putaway_location_scan_code(location_dict)
    agency_name = _agency_display_name(getattr(first, "agency", None))
    pallet_code = clean_code(first_row.get("pallet_code"))
    lines = _lines_from_rows(rows)
    return {
        "box_code": clean_code(box_code),
        "pallet_code": pallet_code,
        "agency_id": int(getattr(first, "agency_id", 0) or 0),
        "agency_name": agency_name,
        "agency_name_short": _short_agency_name(agency_name),
        "location_label": location_label,
        "location_scan_code": location_scan_code,
        "total_qty": sum(int(snapshot.qty or 0) for snapshot in snapshots),
        "lines": lines,
    }


def _box_card(box_code: str, *, require_free: bool = False) -> tuple[dict | None, str]:
    info = inspect_box(box_code)
    if not info.get("found"):
        return None, clean_code(info.get("error")) or "Короб не найден."
    normalized = clean_code(info.get("box_code")) or clean_code(box_code)
    snapshots = list(_box_snapshot_query(normalized))
    if not snapshots:
        return None, "Короб не найден на складе."
    card = _box_card_from_snapshots(normalized, snapshots)
    if not card.get("pallet_code"):
        return None, "Короб не привязан к паллете."
    if require_free:
        operation_block_message = _active_operation_block_message(snapshots)
        if operation_block_message:
            return None, operation_block_message
        for snapshot in snapshots:
            zone_code = clean_code(snapshot.zone_code).upper()
            state_code = clean_code(snapshot.warehouse_state_code)
            if zone_code == "OTG" or state_code == "ready_for_loading":
                return None, "Короб уже в отгрузке/OTG. Забрать его нельзя."
            if clean_code(snapshot.current_trip_id) or snapshot.is_in_vehicle:
                return None, "Короб уже назначен к рейсу или погрузке."
            if state_code != FREE_STORAGE_STATE:
                return None, "Короб не в свободном складском хранении."
            if zone_code and zone_code not in FREE_STORAGE_ZONE_CODES:
                return None, "Короб не в зоне складского хранения."
        reserve_block_message = _reserve_block_message(snapshots)
        if reserve_block_message:
            return None, reserve_block_message
    return card, ""


def _selected_codes(operation: BoxMoveOperation) -> set[str]:
    return {
        clean_code(item.get("box_code")).casefold()
        for item in list(operation.selected_boxes or [])
        if clean_code(item.get("box_code"))
    }


def _expected_codes(operation: BoxMoveOperation) -> set[str]:
    return {
        clean_code(item.get("box_code")).casefold()
        for item in list(operation.destination_expected_boxes or [])
        if clean_code(item.get("box_code"))
    }


def _scanned_codes(operation: BoxMoveOperation) -> set[str]:
    return {
        clean_code(item.get("box_code")).casefold()
        for item in list(operation.destination_scanned_boxes or [])
        if clean_code(item.get("box_code"))
    }


def _pallet_box_cards(pallet_code: str) -> list[dict]:
    cards: dict[str, dict] = {}
    snapshots_by_box: dict[str, list[WarehouseStockSnapshot]] = {}
    normalized_pallet = clean_code(pallet_code).casefold()
    for snapshot in _pallet_snapshot_query(pallet_code):
        state_code = clean_code(snapshot.warehouse_state_code)
        zone_code = clean_code(snapshot.zone_code).upper()
        if state_code != FREE_STORAGE_STATE:
            continue
        if zone_code and zone_code not in FREE_STORAGE_ZONE_CODES:
            continue
        row = normalize_stock_row_from_snapshot(snapshot)
        if not row:
            continue
        box_code = clean_code(row.get("box_code"))
        if not box_code or box_code.casefold() == normalized_pallet:
            continue
        snapshots_by_box.setdefault(box_code, []).append(snapshot)
    for box_code, snapshots in snapshots_by_box.items():
        cards[box_code.casefold()] = _box_card_from_snapshots(box_code, snapshots)
    return sorted(cards.values(), key=lambda item: clean_code(item.get("box_code")).casefold())


def _pallet_container(pallet_code: str) -> WarehouseContainer | None:
    normalized = clean_code(pallet_code)
    if not normalized:
        return None
    return (
        WarehouseContainer.objects.filter(
            container_code__iexact=normalized,
            container_type__in=[WarehouseContainer.TYPE_PALLET, WarehouseContainer.TYPE_MIXED_PALLET],
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        .select_related("current_location", "agency")
        .order_by("id")
        .first()
    )


def _resolve_destination_pallet(scan_value: str) -> tuple[str, str]:
    info = inspect_pallet(scan_value)
    if info.get("found"):
        return clean_code(info.get("pallet_code")) or clean_code(scan_value), ""
    normalized = readable_scan_text(scan_value)
    container = _pallet_container(normalized)
    if container is not None:
        return clean_code(container.container_code), ""
    return "", "Паллета назначения не найдена."


def active_operation(request) -> BoxMoveOperation | None:
    raw_id = request.session.get(SESSION_OPERATION_KEY)
    if raw_id:
        operation = BoxMoveOperation.objects.filter(id=int(raw_id), status__in=OPEN_STATUSES).first()
        if operation:
            return operation
        request.session.pop(SESSION_OPERATION_KEY, None)
        request.session.pop(SESSION_LAST_BOX_KEY, None)
        request.session.modified = True
    return None


def pending_box(request) -> dict | None:
    value = request.session.get(SESSION_LAST_BOX_KEY)
    return value if isinstance(value, dict) else None


def set_pending_box(request, card: dict | None) -> None:
    if card:
        request.session[SESSION_LAST_BOX_KEY] = card
    else:
        request.session.pop(SESSION_LAST_BOX_KEY, None)
    request.session.modified = True


def clear_pending_box(request) -> None:
    request.session.pop(SESSION_LAST_BOX_KEY, None)
    request.session.modified = True


def operation_for_display(request) -> BoxMoveOperation | None:
    query_id = clean_code(request.GET.get("operation"))
    if query_id.isdigit():
        return BoxMoveOperation.objects.filter(id=int(query_id)).first()
    return active_operation(request)


def reset_operation(request) -> None:
    request.session.pop(SESSION_OPERATION_KEY, None)
    request.session.pop(SESSION_LAST_BOX_KEY, None)
    request.session.modified = True


@transaction.atomic
def scan_source_box(request, scan_value: str) -> tuple[BoxMoveOperation | None, dict | None, str]:
    card, error = _box_card(scan_value, require_free=True)
    if error:
        clear_pending_box(request)
        return active_operation(request), None, error
    operation = active_operation(request)
    if operation and operation.status != BoxMoveOperation.STATUS_SELECTING:
        clear_pending_box(request)
        return operation, card, "Выбор коробов уже завершен."
    if operation is None:
        operation = BoxMoveOperation.objects.create(
            agency_id=card.get("agency_id") or None,
            driver=request.user if getattr(request.user, "is_authenticated", False) else None,
            source_pallet_code=card["pallet_code"],
            source_location_label=card["location_label"],
            source_location_scan_code=card["location_scan_code"],
        )
        request.session[SESSION_OPERATION_KEY] = operation.id
        request.session.modified = True
    elif clean_code(card.get("pallet_code")).casefold() != clean_code(operation.source_pallet_code).casefold():
        if operation.selected_boxes:
            clear_pending_box(request)
            return operation, card, f"Можно выбирать короба только с паллеты {operation.source_pallet_code}."
        operation.agency_id = card.get("agency_id") or None
        operation.source_pallet_code = card["pallet_code"]
        operation.source_location_label = card["location_label"]
        operation.source_location_scan_code = card["location_scan_code"]
        operation.save(
            update_fields=[
                "agency",
                "source_pallet_code",
                "source_location_label",
                "source_location_scan_code",
                "updated_at",
            ]
        )
    set_pending_box(request, card)
    return operation, card, ""


@transaction.atomic
def confirm_source_box(operation: BoxMoveOperation, box_code: str) -> tuple[BoxMoveOperation, str]:
    card, error = _box_card(box_code, require_free=True)
    if error:
        return operation, error
    if clean_code(card.get("pallet_code")).casefold() != clean_code(operation.source_pallet_code).casefold():
        return operation, f"Можно выбирать короба только с паллеты {operation.source_pallet_code}."
    selected = list(operation.selected_boxes or [])
    if clean_code(card["box_code"]).casefold() not in _selected_codes(operation):
        selected.append(card)
        operation.selected_boxes = selected
        operation.save(update_fields=["selected_boxes", "updated_at"])
    return operation, ""


@transaction.atomic
def finish_selection(operation: BoxMoveOperation) -> tuple[BoxMoveOperation, str]:
    if not operation.selected_boxes:
        return operation, "Выберите хотя бы один короб."
    operation.status = BoxMoveOperation.STATUS_DESTINATION
    operation.save(update_fields=["status", "updated_at"])
    return operation, ""


@transaction.atomic
def scan_destination_pallet(operation: BoxMoveOperation, scan_value: str) -> tuple[BoxMoveOperation, str]:
    pallet_code, error = _resolve_destination_pallet(scan_value)
    if error:
        return operation, error
    if clean_code(pallet_code).casefold() == clean_code(operation.source_pallet_code).casefold():
        return operation, "Паллета назначения не должна совпадать с паллетой-источником."
    container = _pallet_container(pallet_code)
    destination_info = inspect_pallet(pallet_code)
    location_label = clean_code(destination_info.get("location_label"))
    location_scan_code = clean_code(destination_info.get("location_scan_code"))
    if not location_label and container and container.current_location:
        location = {
            "zone": clean_code(container.current_location.zone_code).upper(),
            "row": int(container.current_location.row_no or 0) or "",
            "section": int(container.current_location.section_no or 0) or "",
            "tier": int(container.current_location.tier_no or 0) or "",
            "cell": int(container.current_location.cell_no or 0) or "",
        }
        location_label = putaway_location_label(location)
        location_scan_code = putaway_location_scan_code(location)
    if not location_label:
        return operation, "У паллеты назначения нет места хранения."
    operation.destination_pallet_code = pallet_code
    operation.destination_location_label = location_label
    operation.destination_location_scan_code = location_scan_code
    operation.destination_expected_boxes = _pallet_box_cards(pallet_code)
    operation.destination_scanned_boxes = []
    operation.discrepancies = []
    operation.status = BoxMoveOperation.STATUS_VERIFYING
    operation.save(
        update_fields=[
            "destination_pallet_code",
            "destination_location_label",
            "destination_location_scan_code",
            "destination_expected_boxes",
            "destination_scanned_boxes",
            "discrepancies",
            "status",
            "updated_at",
        ]
    )
    return operation, ""


def _append_unique_discrepancy(operation: BoxMoveOperation, discrepancy: dict) -> None:
    current = list(operation.discrepancies or [])
    key = (clean_code(discrepancy.get("type")), clean_code(discrepancy.get("box_code")).casefold())
    for item in current:
        existing_key = (clean_code(item.get("type")), clean_code(item.get("box_code")).casefold())
        if existing_key == key:
            return
    current.append(discrepancy)
    operation.discrepancies = current


@transaction.atomic
def scan_destination_box(operation: BoxMoveOperation, scan_value: str) -> tuple[BoxMoveOperation, str, str]:
    card, error = _box_card(scan_value)
    scanned_value = clean_code(readable_scan_text(scan_value))
    expected = _expected_codes(operation)
    selected = _selected_codes(operation)
    if error:
        _append_unique_discrepancy(
            operation,
            {
                "type": "unknown_box",
                "box_code": scanned_value,
                "message": f"Отсканирован неизвестный короб {scanned_value}.",
            },
        )
        operation.save(update_fields=["discrepancies", "updated_at"])
        return operation, "", "Неизвестный короб записан как расхождение."
    code_key = clean_code(card["box_code"]).casefold()
    if code_key in expected:
        scanned = list(operation.destination_scanned_boxes or [])
        if code_key not in _scanned_codes(operation):
            scanned.append(card)
            operation.destination_scanned_boxes = scanned
            operation.save(update_fields=["destination_scanned_boxes", "updated_at"])
        return operation, "Короб паллеты назначения проверен.", ""
    if code_key in selected:
        return operation, "Это короб из перемещения, он будет добавлен на паллету.", ""
    _append_unique_discrepancy(
        operation,
        {
            "type": "extra_box",
            "box_code": card["box_code"],
            "message": (
                f"На паллете назначения найден лишний короб {card['box_code']}. "
                f"По системе он на паллете {card.get('pallet_code') or '-'}, место {card.get('location_scan_code') or card.get('location_label') or '-'}."
            ),
            "pallet_code": card.get("pallet_code") or "",
            "location_label": card.get("location_label") or "",
            "location_scan_code": card.get("location_scan_code") or "",
        },
    )
    operation.save(update_fields=["discrepancies", "updated_at"])
    return operation, "", "Лишний короб записан как расхождение."


def _missing_discrepancies(operation: BoxMoveOperation) -> list[dict]:
    scanned = _scanned_codes(operation)
    result: list[dict] = []
    for item in list(operation.destination_expected_boxes or []):
        box_code = clean_code(item.get("box_code"))
        if not box_code or box_code.casefold() in scanned:
            continue
        result.append(
            {
                "type": "missing_box",
                "box_code": box_code,
                "message": f"Не найден короб {box_code}, который по системе был на паллете назначения.",
            }
        )
    return result


def discrepancy_list(operation: BoxMoveOperation, *, include_missing: bool = True) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    source = list(operation.discrepancies or [])
    if include_missing:
        source += _missing_discrepancies(operation)
    for item in source:
        key = (clean_code(item.get("type")), clean_code(item.get("box_code")).casefold())
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _head_manager_employee() -> Employee | None:
    return Employee.objects.filter(role="head_manager", is_active=True).order_by("full_name", "id").first()


def _create_head_manager_task(operation: BoxMoveOperation, discrepancies: list[dict], request) -> Task | None:
    head_manager = _head_manager_employee()
    if head_manager is None:
        return None
    selected_codes = ", ".join(clean_code(item.get("box_code")) for item in operation.selected_boxes or [] if clean_code(item.get("box_code"))) or "-"
    discrepancy_lines = "\n".join(f"{index}. {clean_code(item.get('message'))}" for index, item in enumerate(discrepancies, start=1))
    description = (
        "Обнаружены расхождения при перемещении коробов.\n\n"
        f"Операция: #{operation.id}\n"
        f"Водитель: {getattr(request.user, 'get_full_name', lambda: '')() or getattr(request.user, 'username', '-')}\n"
        f"Паллета-источник: {operation.source_pallet_code}\n"
        f"Паллета-назначение: {operation.destination_pallet_code}\n"
        f"Перемещено коробов: {len(operation.selected_boxes or [])}\n"
        f"Короба: {selected_codes}\n\n"
        f"Расхождения:\n{discrepancy_lines}"
    )
    return Task.objects.create(
        title="СРОЧНО: расхождение при перемещении коробов",
        description=description,
        route=f"/reachtruck-box-move/?operation={operation.id}",
        assigned_to=head_manager,
        created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
        status="in_progress",
        priority="urgent",
        due_date=timezone.localtime(),
    )


@transaction.atomic
def report_destination_box_missing(
    operation: BoxMoveOperation,
    box_code: str,
    request,
) -> tuple[BoxMoveOperation, str, str]:
    operation = BoxMoveOperation.objects.select_for_update().get(pk=operation.pk)
    if operation.status != BoxMoveOperation.STATUS_VERIFYING:
        return operation, "", "Сообщить можно только во время проверки коробов."
    if operation.driver_id and operation.driver_id != getattr(request.user, "id", None):
        return operation, "", "Эта операция выполняется другим водителем ричтрака."

    normalized_code = clean_code(box_code)
    expected_by_key = {
        clean_code(item.get("box_code")).casefold(): clean_code(item.get("box_code"))
        for item in list(operation.destination_expected_boxes or [])
        if clean_code(item.get("box_code"))
    }
    expected_code = expected_by_key.get(normalized_code.casefold(), "")
    if not expected_code:
        return operation, "", "Этот короб не относится к проверяемой паллете."
    if normalized_code.casefold() in _scanned_codes(operation):
        return operation, "", "Этот короб уже проверен."

    driver_name = (
        getattr(request.user, "get_full_name", lambda: "")()
        or getattr(request.user, "username", "")
        or "-"
    )
    missing_container = (
        WarehouseContainer.objects.select_for_update()
        .filter(
            agency_id=operation.agency_id,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code__iexact=expected_code,
        )
        .order_by("id")
        .first()
    )
    quarantine_operation = None
    if missing_container is not None:
        from sklad.services.missing_box_replacement import quarantine_missing_box

        quarantine_operation = quarantine_missing_box(
            container_id=missing_container.id,
            agency_id=operation.agency_id,
            context_type="box_move_verification",
            context_id=str(operation.id),
            performed_by=request.user,
        )
    _append_unique_discrepancy(
        operation,
        {
            "type": "missing_box",
            "box_code": expected_code,
            "message": f"Не найден короб {expected_code}, который по системе был на паллете назначения.",
            "reported_by_driver": True,
            "quarantine_operation_id": int(getattr(quarantine_operation, "id", 0) or 0),
        },
    )
    operation.save(update_fields=["discrepancies", "updated_at"])
    description = (
        "Водитель ричтрака сообщил, что ожидаемого короба нет на паллете назначения.\n\n"
        f"Операция проверки коробов: #{operation.id}\n"
        f"Водитель: {driver_name}\n"
        f"Короб: {expected_code}\n"
        f"Паллета назначения: {operation.destination_pallet_code or '-'}\n"
        f"Место по системе: {operation.destination_location_scan_code or operation.destination_location_label or '-'}\n"
        f"Паллета-источник: {operation.source_pallet_code or '-'}\n\n"
        "Нужно проверить фактическое наличие и складской учет. "
        + (
            "Короб заблокирован без списания учётного количества. "
            if quarantine_operation is not None
            else "Короб не найден в активном складском остатке; автоматическая блокировка не создавалась. "
        )
        + "Перемещение выбранных коробов можно завершить с расхождением."
    )
    task, _created, message = create_missing_box_verification_task(
        box_code=expected_code,
        source_kind="box-move",
        source_id=str(operation.id),
        description=description,
        route=f"/reachtruck-box-move/?operation={operation.id}",
        user=request.user,
    )
    if task is None:
        return operation, "", message
    return operation, (
        f"{message} Если похожего короба нет, завершите операцию с уже собранными коробами."
    ), ""


@transaction.atomic
def complete_operation(operation: BoxMoveOperation, request) -> tuple[BoxMoveOperation, str]:
    if not operation.selected_boxes:
        return operation, "Не выбраны короба для перемещения."
    if not operation.destination_pallet_code:
        return operation, "Не выбрана паллета назначения."
    discrepancies = discrepancy_list(operation)
    selected_codes = [clean_code(item.get("box_code")) for item in operation.selected_boxes or [] if clean_code(item.get("box_code"))]
    warehouse_operation = WarehouseWritePathService.complete_inventory_box_relocation(
        source_pallet_code=operation.source_pallet_code,
        destination_pallet_code=operation.destination_pallet_code,
        box_codes=selected_codes,
        performed_by=request.user if getattr(request.user, "is_authenticated", False) else None,
        context_id=str(operation.id),
    )
    operation.warehouse_operation = warehouse_operation
    operation.discrepancies = discrepancies
    operation.status = (
        BoxMoveOperation.STATUS_DONE_WITH_DISCREPANCY
        if discrepancies
        else BoxMoveOperation.STATUS_DONE
    )
    operation.completed_at = timezone.now()
    if discrepancies:
        operation.manager_task = _create_head_manager_task(operation, discrepancies, request)
    operation.save(
        update_fields=[
            "warehouse_operation",
            "discrepancies",
            "status",
            "completed_at",
            "manager_task",
            "updated_at",
        ]
    )
    reset_operation(request)
    if discrepancies:
        return operation, "Операция завершена с расхождениями. Главному менеджеру создана красная задача."
    return operation, "Короба перемещены, проверка паллеты назначения завершена."


@transaction.atomic
def cancel_operation(operation: BoxMoveOperation, request) -> None:
    operation.status = BoxMoveOperation.STATUS_CANCELED
    operation.completed_at = timezone.now()
    operation.save(update_fields=["status", "completed_at", "updated_at"])
    reset_operation(request)


def operation_stats(operation: BoxMoveOperation) -> dict:
    selected_count = len(operation.selected_boxes or [])
    source_boxes = _pallet_box_cards(operation.source_pallet_code) if operation.source_pallet_code else []
    selected_codes = _selected_codes(operation)
    source_box_rows = []
    for box in source_boxes:
        box_code = clean_code(box.get("box_code"))
        row = dict(box)
        row["is_selected"] = box_code.casefold() in selected_codes
        source_box_rows.append(row)
    source_total = len(source_box_rows)
    destination_scanned_codes = _scanned_codes(operation)
    destination_box_rows = []
    for box in list(operation.destination_expected_boxes or []):
        box_code = clean_code(box.get("box_code"))
        row = dict(box)
        row["is_scanned"] = box_code.casefold() in destination_scanned_codes
        destination_box_rows.append(row)
    expected_count = len(operation.destination_expected_boxes or [])
    scanned_count = len(operation.destination_scanned_boxes or [])
    missing_count = max(expected_count - scanned_count, 0)
    active_discrepancies = discrepancy_list(operation, include_missing=False)
    discrepancies = discrepancy_list(
        operation,
        include_missing=operation.status
        in {
            BoxMoveOperation.STATUS_DONE,
            BoxMoveOperation.STATUS_DONE_WITH_DISCREPANCY,
        },
    )
    return {
        "selected_count": selected_count,
        "source_total": source_total,
        "source_remaining": max(source_total - selected_count, 0),
        "expected_count": expected_count,
        "scanned_count": scanned_count,
        "missing_count": missing_count,
        "active_discrepancy_count": len(active_discrepancies),
        "active_discrepancies": active_discrepancies,
        "discrepancy_count": len(discrepancies),
        "discrepancies": discrepancies,
        "source_boxes": source_box_rows,
        "destination_boxes": destination_box_rows,
    }
