from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from importlib import import_module
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from fbs.exceptions import FbsFeatureDisabled, FbsReplenishmentError, FbsStorageError
from fbs.barcode_aliases import normalize_barcode
from fbs.flags import feature_enabled
from fbs.goods_types import (
    fbs_client_movement_source_stock_q,
    fbs_ready_goods_type_q,
    is_fbs_client_movement_source_stock,
    is_fbs_ready_goods_type,
    receiving_placement_allowed_snapshot_ids,
)
from fbs.models import (
    FbsBox,
    FbsClientMovementRequest,
    FbsClientMovementRequestLine,
    FbsPallet,
    FbsRackCellBinding,
    FbsReplenishmentAllocation,
    FbsReplenishmentLine,
    FbsReplenishmentPlan,
    FbsReplenishmentPreparedBox,
    FbsReplenishmentPreparedBoxItem,
    FbsStockBalance,
    FbsStockMovement,
    FbsStorageCell,
)
from fbs.staging import (
    archive_movement_staging_container,
    ensure_movement_staging_container,
    movement_staging_container_code,
)
from sku.models import Agency, SKU, SKUBarcode
from marking.codes import (
    MarkingCodeFormatError,
    marking_code_identity,
    marking_code_variants,
    validate_import_marking_code,
)
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseOperationTask,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sklad.location_occupancy import FBS_STORAGE_CONTEXT_TYPE
from sklad.topology import OS_LINE_DISPLAY_LABELS, os_location_code
from sklad.services.warehouse_transitions import WarehouseTransitionError
from sklad.services.warehouse_write_path import WarehouseWritePathService

from .physical_locations import is_virtual_fbs_plan_location


CONTEXT_TYPE = "fbs_replenishment"
SOURCE_DOCUMENT_TYPE = "fbs_plan"
FBS_PLACEMENT_CONTEXT_TYPE = "fbs_placement"
FBS_PREPARED_BOX_CONTEXT_TYPE = "fbs_prepared_box"
FBS_MOVEMENT_MARKING_SCAN_MARKER = "fbs_movement_marking_scan_v1"
FBS_MOVEMENT_MARKING_EVENT_TYPE = "fbs_replenishment_marking_scanned"
CLIENT_MOVEMENT_PENDING_CONFIRMATION_KEY = (
    "client_movement_pending_warehouse_confirmation"
)
CLIENT_MOVEMENT_PENDING_QTY_KEY = "client_movement_pending_confirmation_qty"
MAX_PREPARED_BOXES_PER_PLAN = 50
FULL_SOURCE_PALLET_COMMENT_RE = re.compile(r"^\[client_full_pallet:(\d+)](?:\s|$)")
_OS_SCAN_RE = re.compile(r"^(?P<line>[0-9A-Z]+)-(?P<row>\d+)/(?P<tier>\d+)-(?P<cell>\d+)$")
_LEGACY_OS_SCAN_RE = re.compile(
    r"^OS-(?P<row>\d+)-(?P<section>\d+)-(?P<tier>\d+)-(?P<cell>\d+)$"
)
_AIM_SYMBOLOGY_PREFIX_RE = re.compile(r"^\][A-Z0-9]{2}")
_CYRILLIC_B_OS_LINE_RE = re.compile(r"^В(?=-\d+/)")
_OS_SECTION_BY_SCAN_LABEL = {
    str(label).upper(): int(section)
    for section, label in OS_LINE_DISPLAY_LABELS.items()
}


def fbs_movement_marking_scan_required() -> bool:
    """FBS internal moves require product barcodes, never ChZ/Data Matrix."""
    return False


WAREHOUSE_SNAPSHOT_FIELDS = {
    field.name for field in WarehouseStockSnapshot._meta.concrete_fields
}
WAREHOUSE_RESERVE_FIELDS = {
    field.name for field in WarehouseReserve._meta.concrete_fields
}


def _is_concrete_physical_location(location: WarehouseLocation | None) -> bool:
    if location is None or is_virtual_fbs_plan_location(location):
        return False
    zone_code = str(location.zone_code or "").strip().upper()
    if (
        zone_code == "OS"
        or str(location.zone_kind or "").strip()
        == WarehouseLocation.ZONE_KIND_STORAGE
    ):
        return all(
            int(value or 0) > 0
            for value in (
                location.row_no,
                location.section_no,
                location.tier_no,
                location.cell_no,
            )
        )
    return bool(str(location.location_code or "").strip())


def _require_concrete_physical_location(
    location: WarehouseLocation | None,
    *,
    purpose: str,
) -> WarehouseLocation:
    if not _is_concrete_physical_location(location):
        raise FbsReplenishmentError(
            f"{purpose}: требуется точная физическая ячейка с рядом, секцией, "
            "ярусом и местом. Техническая зона OS не является местом хранения."
        )
    return location


def _container_physical_location(
    container: WarehouseContainer | None,
    *,
    purpose: str,
) -> WarehouseLocation:
    if container is None:
        raise FbsReplenishmentError(f"{purpose}: исходный короб или паллета не найдены.")
    if container.current_location_id:
        return _require_concrete_physical_location(
            container.current_location,
            purpose=purpose,
        )
    parent = container.parent_container
    if parent is not None and parent.current_location_id:
        return _require_concrete_physical_location(
            parent.current_location,
            purpose=purpose,
        )
    raise FbsReplenishmentError(
        f"{purpose}: у короба или паллеты нет точного физического места. "
        "Автоматическое восстановление по старым остаткам запрещено."
    )


def _completion_destination_location(
    *,
    plan,
    snapshot,
    preserve_source_physical_place: bool = False,
):
    """Keep a box at its real place for a logical FBS posting."""
    target_location = plan.target_cell.location
    if (
        not preserve_source_physical_place
        and not is_virtual_fbs_plan_location(target_location)
    ):
        return (
            _require_concrete_physical_location(
                target_location,
                purpose="Нельзя завершить FBS-перемещение",
            ),
            False,
        )

    return (
        _container_physical_location(
            snapshot.container,
            purpose="Нельзя провести паллету в FBS без физического перемещения",
        ),
        True,
    )


def _apply_completion_destination(*, allocation, plan, destination_location) -> None:
    """Persist a truthful physical destination on the task and its operation."""
    task = allocation.warehouse_task
    task.to_location = destination_location
    task.to_zone_code = str(destination_location.zone_code or "")
    task.save(update_fields=["to_location", "to_zone_code", "updated_at"])

    operation = plan.warehouse_operation
    source_locations = list(
        FbsReplenishmentAllocation.objects.filter(line__plan=plan)
        .values_list("source_snapshot__location_id", flat=True)
        .distinct()[:2]
    )
    if len(source_locations) == 1:
        operation.destination_location = destination_location
        operation.destination_zone_code = str(destination_location.zone_code or "")
    else:
        operation.destination_location = None
        operation.destination_zone_code = ""
    operation.save(
        update_fields=["destination_location", "destination_zone_code", "updated_at"]
    )


def _validate_fbs_staging_location(location: WarehouseLocation | None) -> None:
    if location is None or not location.is_active:
        raise FbsReplenishmentError("Место подготовки FBS выключено или не найдено.")
    zone = str(location.zone_code or "").strip().upper()
    legacy_zone = str(
        getattr(settings, "FBS_PREP_ZONE_CODE", "FBS-PREP") or "FBS-PREP"
    ).strip().upper()
    if zone == legacy_zone:
        return
    if zone != "PR":
        raise FbsReplenishmentError(
            f"Подготовка FBS разрешена только в PR или в зоне {legacy_zone}."
        )
    from sklad.services.operational_locations import validate_operational_location

    try:
        validate_operational_location(
            location,
            expected_zone="PR",
            require_fbs=True,
        )
    except ValidationError as exc:
        raise FbsReplenishmentError("; ".join(exc.messages)) from exc


def _snapshot_lot_code(snapshot: WarehouseStockSnapshot) -> str:
    if "lot_code" not in WAREHOUSE_SNAPSHOT_FIELDS:
        return ""
    return str(getattr(snapshot, "lot_code", "") or "").strip()


def _full_source_pallet_id(plan: FbsReplenishmentPlan) -> int:
    match = FULL_SOURCE_PALLET_COMMENT_RE.match(str(plan.comment or ""))
    return int(match.group(1)) if match else 0


def _snapshot_expiry_date(snapshot: WarehouseStockSnapshot):
    if "expiry_date" not in WAREHOUSE_SNAPSHOT_FIELDS:
        return None
    return getattr(snapshot, "expiry_date", None)


def _reserve_traceability_kwargs(snapshot: WarehouseStockSnapshot) -> dict:
    values = {}
    if "lot_code" in WAREHOUSE_RESERVE_FIELDS:
        values["lot_code"] = _snapshot_lot_code(snapshot)
    if "expiry_date" in WAREHOUSE_RESERVE_FIELDS:
        values["expiry_date"] = _snapshot_expiry_date(snapshot)
    return values


def _is_plan_source_stock_allowed(
    plan: FbsReplenishmentPlan,
    snapshot: WarehouseStockSnapshot,
    *,
    receiving_allowed_ids=None,
) -> bool:
    if plan.client_movement_request_id:
        container = snapshot.container if snapshot.container_id else None
        return is_fbs_client_movement_source_stock(
            snapshot=snapshot,
            goods_type=snapshot.goods_type,
            zone_kind=snapshot.zone_kind,
            warehouse_state_code=snapshot.warehouse_state_code,
            container_type=getattr(container, "container_type", ""),
            container_code=getattr(container, "container_code", ""),
            container_status=getattr(container, "status", ""),
            receiving_allowed_ids=receiving_allowed_ids,
        )
    return is_fbs_ready_goods_type(snapshot.goods_type)


def _plan_source_stock_q(
    plan: FbsReplenishmentPlan,
) -> Q:
    if plan.client_movement_request_id:
        return fbs_client_movement_source_stock_q(agency_id=plan.agency_id)
    return fbs_ready_goods_type_q()


def _require_module() -> None:
    if not feature_enabled("module"):
        raise FbsFeatureDisabled("Модуль FBS выключен.")


def _require_warehouse_writes() -> None:
    if not feature_enabled("warehouse_writes"):
        raise FbsFeatureDisabled("Складские записи FBS выключены.")


def _authenticated_user(user):
    return user if getattr(user, "is_authenticated", False) else None


def _credit_replenishment_balance(
    balance: FbsStockBalance,
    *,
    plan: FbsReplenishmentPlan,
    qty: int,
) -> bool:
    """Book physical stock but keep client-movement stock unavailable until sign-off."""
    qty = int(qty or 0)
    pending_confirmation = bool(plan.client_movement_request_id)
    balance.qty = int(balance.qty or 0) + qty
    if not pending_confirmation:
        balance.available_qty = int(balance.available_qty or 0) + qty
    balance.save(update_fields=["qty", "available_qty", "updated_at"])
    return pending_confirmation


def _pending_confirmation_payload(*, pending: bool, qty: int) -> dict:
    if not pending:
        return {}
    return {
        CLIENT_MOVEMENT_PENDING_CONFIRMATION_KEY: True,
        CLIENT_MOVEMENT_PENDING_QTY_KEY: int(qty or 0),
    }


def _normalize_scan(value: str) -> str:
    return str(value or "").strip().casefold()


def _barcode_as_gtin14(value: object) -> str:
    barcode = normalize_barcode(value)
    if not barcode.isdigit() or len(barcode) not in {8, 12, 13, 14}:
        return ""
    return barcode.zfill(14)


def _marking_lookup(field_name: str, scan_code: str) -> Q:
    identity = marking_code_identity(scan_code)
    variants = marking_code_variants(scan_code)
    lookup = Q(**{f"{field_name}__in": variants})
    if identity:
        lookup |= Q(**{f"{field_name}__startswith": identity})
        lookup |= Q(**{f"{field_name}__startswith": f"]d2{identity}"})
        lookup |= Q(**{f"{field_name}__startswith": f"]D2{identity}"})
    return lookup


def _movement_marking_task(allocation: FbsReplenishmentAllocation):
    move_task_model = import_module("reach" + "truck.models").MoveTask
    task = (
        move_task_model.objects.select_for_update(of=("self",))
        .filter(legacy_order_id=f"FBS-RPL-A{allocation.id}")
        .order_by("id")
        .first()
    )
    if task is None or not dict(task.payload or {}).get(FBS_MOVEMENT_MARKING_SCAN_MARKER):
        raise FbsReplenishmentError(
            "Для этого задания обязательное сканирование Честного знака не включено."
        )
    return task


def _normalize_os_scan(value: str) -> str:
    scan = unicodedata.normalize("NFKC", str(value or ""))
    scan = "".join(ch for ch in scan if unicodedata.category(ch) != "Cf")
    scan = "".join(scan.split()).upper()
    scan = _AIM_SYMBOLOGY_PREFIX_RE.sub("", scan)
    return _CYRILLIC_B_OS_LINE_RE.sub("B", scan)


def _source_scan_values(snapshot: WarehouseStockSnapshot) -> set[str]:
    container_values = {
        str(snapshot.container_code or "").strip(),
        str(getattr(snapshot.container, "container_code", "") or "").strip(),
    }
    normalized = {_normalize_scan(value) for value in container_values if value}
    if normalized:
        return normalized
    location_code = str(getattr(snapshot.location, "location_code", "") or "").strip()
    return {_normalize_scan(location_code)} if location_code else set()


def _stock_identity(snapshot: WarehouseStockSnapshot) -> str:
    expiry_date = _snapshot_expiry_date(snapshot)
    payload = {
        "sku_code": str(snapshot.sku_code or "").strip(),
        "size": str(snapshot.size or "").strip(),
        "barcode": str(snapshot.barcode or "").strip(),
        "goods_type": str(snapshot.goods_type or "").strip(),
        "marking_code": str(snapshot.marking_code or "").strip(),
        "lot_code": _snapshot_lot_code(snapshot),
        "expiry_date": expiry_date.isoformat() if expiry_date else "",
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _whole_box_snapshot_identity(snapshot: WarehouseStockSnapshot) -> tuple[str, int]:
    return (_stock_identity(snapshot), int(snapshot.qty or 0))


def _requires_storekeeper_packing(plan: FbsReplenishmentPlan) -> bool:
    return bool(
        plan.client_movement_request_id
        and plan.mode == FbsReplenishmentPlan.MODE_ITEM
        and plan.target_box_id is None
        and plan.staging_location_id
        and not plan.prepared_boxes.exists()
    )


def _uses_prepared_box_flow(plan: FbsReplenishmentPlan) -> bool:
    return bool(
        plan.client_movement_request_id
        and plan.mode == FbsReplenishmentPlan.MODE_ITEM
        and plan.staging_location_id
        and plan.prepared_boxes.exists()
    )


def _validate_target_pallet(plan: FbsReplenishmentPlan) -> None:
    if not plan.target_cell.is_active:
        raise FbsReplenishmentError("FBS-ячейка назначения неактивна.")
    expected_zone = str(getattr(settings, "FBS_ZONE_CODE", "FBS") or "FBS").strip().upper()
    actual_zone = str(plan.target_cell.location.zone_code or "").strip().upper()
    if actual_zone not in {expected_zone, "OS", "PR"}:
        raise FbsReplenishmentError(
            f"Ячейка назначения должна находиться в общей зоне OS, PR или зоне {expected_zone}."
        )
    if plan.target_pallet.agency_id != plan.agency_id:
        raise FbsReplenishmentError("Паллета назначения принадлежит другому клиенту.")
    if plan.target_pallet.cell_id != plan.target_cell_id:
        raise FbsReplenishmentError("Паллета назначения находится в другой FBS-ячейке.")
    if actual_zone == "OS":
        warehouse_container = plan.target_pallet.warehouse_container
        if (
            warehouse_container is None
            or warehouse_container.status != WarehouseContainer.STATUS_ACTIVE
            or warehouse_container.current_location_id != plan.target_cell.location_id
            or str(warehouse_container.source_context_type or "").strip()
            != FBS_STORAGE_CONTEXT_TYPE
        ):
            raise FbsReplenishmentError(
                "Общая OS-ячейка не зарезервирована физической FBS-паллетой."
            )
    elif actual_zone == "PR":
        location = plan.target_cell.location
        if (
            not location.is_active
            or not location.is_fbs_visible
            or location.is_topology_visible
            or not plan.target_pallet.is_rack_binding
        ):
            raise FbsReplenishmentError(
                "Назначение PR должно быть точной активной ячейкой FBS-стеллажа."
            )
        warehouse_container = plan.target_pallet.warehouse_container
        if (
            warehouse_container is None
            or warehouse_container.status != WarehouseContainer.STATUS_ACTIVE
            or warehouse_container.current_location_id != location.id
            or str(warehouse_container.source_context_type or "").strip()
            != "fbs_rack_cell"
        ):
            raise FbsReplenishmentError(
                "Системная FBS-паллета PR-ячейки имеет неверный физический адрес."
            )
    active_boxes = list(
        FbsBox.objects.filter(
            pallet=plan.target_pallet,
            status__in=[FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE],
        ).values_list("agency_id", flat=True)
    )
    if any(agency_id != plan.agency_id for agency_id in active_boxes):
        raise FbsReplenishmentError("На FBS-паллете обнаружен короб другого клиента.")


def _validate_destination(plan: FbsReplenishmentPlan, target_box: FbsBox) -> None:
    _validate_target_pallet(plan)
    if target_box.agency_id != plan.agency_id:
        raise FbsReplenishmentError("Короб назначения принадлежит другому клиенту.")
    if target_box.pallet_id != plan.target_pallet_id:
        raise FbsReplenishmentError("Короб назначения находится на другой FBS-паллете.")
    if target_box.status == FbsBox.STATUS_ARCHIVED:
        raise FbsReplenishmentError("Короб назначения архивирован.")
    if str(plan.target_cell.location.zone_code or "").strip().upper() == "PR":
        binding = FbsRackCellBinding.objects.filter(
            agency_id=plan.agency_id,
            pallet_id=plan.target_pallet_id,
            box_id=target_box.id,
            rack_cell__storage_cell_id=plan.target_cell_id,
            rack_cell__is_active=True,
        ).first()
        physical_box = target_box.source_container
        if (
            binding is None
            or physical_box is None
            or physical_box.status != WarehouseContainer.STATUS_ACTIVE
            or physical_box.current_location_id != plan.target_cell.location_id
            or str(physical_box.source_context_type or "").strip()
            != "fbs_rack_cell"
        ):
            raise FbsReplenishmentError(
                "Короб назначения не имеет точной системной привязки к PR-ячейке."
            )


@transaction.atomic
def create_replenishment_plan(
    *,
    agency: Agency,
    mode: str,
    target_pallet: FbsPallet,
    target_box: FbsBox | None,
    staging_location: WarehouseLocation | None = None,
    lines: Iterable[dict],
    requested_by=None,
    requested_by_role: str = "storekeeper",
    comment: str = "",
    client_movement_request: FbsClientMovementRequest | None = None,
) -> FbsReplenishmentPlan:
    _require_module()
    if mode not in {FbsReplenishmentPlan.MODE_BOX, FbsReplenishmentPlan.MODE_ITEM}:
        raise FbsReplenishmentError("Неизвестный режим подсорта FBS.")
    line_items = list(lines)
    if mode == FbsReplenishmentPlan.MODE_BOX and len(line_items) != 1:
        raise FbsReplenishmentError(
            "Один коробный план FBS должен содержать ровно один исходный короб."
        )
    agency = Agency.objects.select_for_update().get(pk=agency.pk)
    target_pallet = (
        FbsPallet.objects.select_for_update().select_related("cell__location").get(pk=target_pallet.pk)
    )
    from .inventory import assert_pallet_not_relocating

    assert_pallet_not_relocating(
        target_pallet.id,
        agency_id=target_pallet.agency_id,
    )
    if target_box is not None:
        target_box = FbsBox.objects.select_for_update().select_related("pallet").get(
            pk=target_box.pk
        )
    if staging_location is not None:
        staging_location = WarehouseLocation.objects.select_for_update().get(
            pk=staging_location.pk
        )
    plan = FbsReplenishmentPlan(
        agency=agency,
        client_movement_request=client_movement_request,
        mode=mode,
        target_cell=target_pallet.cell,
        target_pallet=target_pallet,
        target_box=target_box,
        staging_location=staging_location,
        requested_by=_authenticated_user(requested_by),
        requested_by_role=str(requested_by_role or "").strip(),
        comment=str(comment or "").strip(),
    )
    if target_box is None:
        if not (
            mode == FbsReplenishmentPlan.MODE_ITEM
            and client_movement_request is not None
            and staging_location is not None
        ):
            raise FbsReplenishmentError("Для плана FBS необходимо указать короб назначения.")
        _validate_fbs_staging_location(staging_location)
        _validate_target_pallet(plan)
    else:
        _validate_destination(plan, target_box)
    plan.full_clean()
    plan.save()

    total_requested = 0
    for item in line_items:
        sku = item.get("sku")
        source_container = item.get("source_container")
        barcode = ""
        if mode == FbsReplenishmentPlan.MODE_ITEM:
            if not isinstance(sku, SKU):
                raise FbsReplenishmentError("Для штучного подсорта необходимо указать SKU.")
            barcode = str(item.get("barcode") or "").strip()
            if not barcode:
                raise FbsReplenishmentError(
                    "Для штучного подсорта необходимо указать штрихкод."
                )
            if not SKUBarcode.objects.filter(
                sku=sku,
                value=barcode,
                sku__agency_id=agency.id,
                sku__deleted=False,
            ).exists():
                raise FbsReplenishmentError(
                    f"Штрихкод {barcode} не привязан к SKU клиента."
                )
            qty_requested = int(item.get("qty") or 0)
            if qty_requested <= 0:
                raise FbsReplenishmentError("Количество штучного подсорта должно быть больше нуля.")
        else:
            if not isinstance(source_container, WarehouseContainer):
                raise FbsReplenishmentError("Для подсорта коробами необходимо указать исходный короб.")
            source_container = WarehouseContainer.objects.select_for_update().get(
                pk=source_container.pk
            )
            if source_container.agency_id != agency.id:
                raise FbsReplenishmentError("Исходный короб принадлежит другому клиенту.")
            if source_container.container_type != WarehouseContainer.TYPE_BOX:
                raise FbsReplenishmentError("Подсорт коробами принимает только контейнер типа box.")
            if FbsReplenishmentLine.objects.filter(
                source_container=source_container,
                status__in=(
                    FbsReplenishmentLine.STATUS_PROPOSED,
                    FbsReplenishmentLine.STATUS_RESERVED,
                    FbsReplenishmentLine.STATUS_IN_PROGRESS,
                ),
            ).exists():
                raise FbsReplenishmentError(
                    "Для исходного короба уже существует открытый план FBS."
                )
            mixed_owner_exists = (
                WarehouseStockSnapshot.objects.filter(
                    container=source_container,
                    is_archived=False,
                    qty__gt=0,
                )
                .exclude(agency=agency)
                .exists()
            )
            if mixed_owner_exists:
                raise FbsReplenishmentError(
                    "В исходном коробе обнаружены остатки нескольких клиентов."
                )
            source_snapshots = list(
                WarehouseStockSnapshot.objects.filter(
                    agency=agency,
                    container=source_container,
                    is_archived=False,
                    qty__gt=0,
                )
            )
            receiving_allowed_ids = (
                receiving_placement_allowed_snapshot_ids(source_snapshots)
                if plan.client_movement_request_id
                else None
            )
            if any(
                not _is_plan_source_stock_allowed(
                    plan,
                    snapshot,
                    receiving_allowed_ids=receiving_allowed_ids,
                )
                for snapshot in source_snapshots
            ):
                raise FbsReplenishmentError(
                    "Этот короб нельзя переместить в FBS по текущему плану."
                )
            qty_requested = sum(int(snapshot.qty or 0) for snapshot in source_snapshots)
            if qty_requested <= 0:
                raise FbsReplenishmentError("В исходном коробе нет доступного складского остатка.")

        line = FbsReplenishmentLine(
            plan=plan,
            client_movement_line=item.get("client_movement_line"),
            sku_ref=sku if mode == FbsReplenishmentPlan.MODE_ITEM else None,
            sku_code=str(getattr(sku, "sku_code", "") or "").strip(),
            barcode=barcode,
            source_container=source_container if mode == FbsReplenishmentPlan.MODE_BOX else None,
            target_box=target_box,
            qty_requested=qty_requested,
        )
        line.full_clean()
        line.save()
        total_requested += qty_requested

    if total_requested <= 0:
        raise FbsReplenishmentError("В плане подсорта должна быть хотя бы одна строка.")
    plan.planned_qty = total_requested
    plan.save(update_fields=["planned_qty", "updated_at"])
    return plan


def _transfer_destination(plan: FbsReplenishmentPlan) -> WarehouseLocation:
    if (
        plan.mode == FbsReplenishmentPlan.MODE_ITEM
        and plan.target_box_id is None
        and plan.staging_location_id
    ):
        return plan.staging_location
    return plan.target_cell.location


def _create_operation(plan: FbsReplenishmentPlan, confirmed_by) -> WarehouseOperation:
    destination = _transfer_destination(plan)
    return WarehouseOperation.objects.create(
        agency=plan.agency,
        operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
        context_type=CONTEXT_TYPE,
        context_id=str(plan.id),
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(plan.id),
        destination_location=destination,
        destination_zone_code=str(destination.zone_code or ""),
        status=WarehouseOperation.STATUS_PLANNED,
        requested_by=confirmed_by,
        requested_by_role="storekeeper",
        assigned_executor_role="reachtruck",
        comment=plan.comment,
        planned_qty=plan.planned_qty,
    )


def _create_task(
    *,
    plan: FbsReplenishmentPlan,
    operation: WarehouseOperation,
    snapshot: WarehouseStockSnapshot,
    qty: int,
    task_type: str,
) -> WarehouseOperationTask:
    destination = _transfer_destination(plan)
    return WarehouseOperationTask.objects.create(
        operation=operation,
        task_type=task_type,
        container=snapshot.container,
        from_location=snapshot.location,
        to_location=destination,
        from_zone_code=str(snapshot.zone_code or ""),
        to_zone_code=str(destination.zone_code or ""),
        qty_planned=qty,
        assigned_to=plan.assigned_to,
        executor_role="reachtruck",
        payload={
            "fbs_plan_id": plan.id,
            "source_snapshot_id": snapshot.id,
            "source_scan_codes": sorted(_source_scan_values(snapshot)),
            "target_box_code": str(getattr(plan.target_box, "box_code", "") or ""),
            "staging_location_code": str(
                getattr(plan.staging_location, "location_code", "") or ""
            ),
            "mode": plan.mode,
        },
    )


def _reserve_snapshot(
    *,
    plan: FbsReplenishmentPlan,
    line: FbsReplenishmentLine,
    operation: WarehouseOperation,
    task: WarehouseOperationTask,
    snapshot: WarehouseStockSnapshot,
    qty: int,
    confirmed_by,
    lock_whole_snapshot: bool,
    receiving_allowed_ids=None,
) -> FbsReplenishmentAllocation:
    destination = _transfer_destination(plan)
    try:
        WarehouseWritePathService.assert_not_reserved_for_shipping(
            agency=plan.agency,
            snapshots=[snapshot],
        )
    except WarehouseTransitionError as exc:
        raise FbsReplenishmentError(str(exc)) from exc
    if snapshot.agency_id != plan.agency_id:
        raise FbsReplenishmentError("Исходный остаток принадлежит другому клиенту.")
    if not _is_plan_source_stock_allowed(
        plan,
        snapshot,
        receiving_allowed_ids=receiving_allowed_ids,
    ):
        raise FbsReplenishmentError(
            "В FBS можно перемещать только готовый товар с основного хранения."
        )
    snapshot_barcode = str(snapshot.barcode or "").strip()
    if not snapshot_barcode:
        raise FbsReplenishmentError("У исходного складского остатка отсутствует штрихкод.")
    if (
        line.plan.mode == FbsReplenishmentPlan.MODE_ITEM
        and snapshot_barcode != str(line.barcode or "").strip()
    ):
        raise FbsReplenishmentError(
            "Штрихкод исходного остатка не совпадает со строкой подсорта."
        )
    if snapshot.is_archived or snapshot.is_in_vehicle:
        raise FbsReplenishmentError("Исходный остаток недоступен для подсорта.")
    expiry_date = _snapshot_expiry_date(snapshot)
    lot_code = _snapshot_lot_code(snapshot)
    if expiry_date and expiry_date < timezone.localdate():
        raise FbsReplenishmentError("Нельзя перемещать просроченный остаток в зону FBS.")
    if int(snapshot.available_qty or 0) < qty:
        raise FbsReplenishmentError(
            f"Недостаточно доступного остатка по {snapshot.sku_code}: нужно {qty}."
        )
    if str(snapshot.zone_code or "").strip().upper() == str(
        getattr(settings, "FBS_ZONE_CODE", "FBS") or "FBS"
    ).strip().upper():
        raise FbsReplenishmentError("Нельзя повторно перемещать FBS-остаток из общего склада.")

    reserve = WarehouseReserve.objects.create(
        agency=plan.agency,
        reserve_type=WarehouseReserve.TYPE_MANUAL,
        context_type=CONTEXT_TYPE,
        context_id=str(plan.id),
        sku_ref=snapshot.sku_ref,
        sku_code=snapshot.sku_code,
        size=snapshot.size,
        barcode=snapshot.barcode,
        goods_type=snapshot.goods_type,
        marking_code=snapshot.marking_code,
        qty_reserved=qty,
        qty_allocated=qty,
        status=WarehouseReserve.STATUS_ALLOCATED,
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(plan.id),
        created_by=confirmed_by,
        **_reserve_traceability_kwargs(snapshot),
    )
    source_version = int(snapshot.snapshot_version or 0)
    snapshot.available_qty = int(snapshot.available_qty or 0) - qty
    snapshot.other_reserved_qty = int(snapshot.other_reserved_qty or 0) + qty
    snapshot.snapshot_version = source_version + 1
    if lock_whole_snapshot:
        snapshot.active_operation = operation
        snapshot.active_operation_type = operation.operation_type
    reserved_at = timezone.now()
    event = WarehouseEvent.objects.create(
        agency=plan.agency,
        event_type="fbs_replenishment_reserved",
        stock_context_type=CONTEXT_TYPE,
        stock_context_id=str(plan.id),
        container=snapshot.container,
        operation=operation,
        operation_task=task,
        reserve=reserve,
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(plan.id),
        from_location=snapshot.location,
        to_location=destination,
        from_zone_code=str(snapshot.zone_code or ""),
        to_zone_code=str(destination.zone_code or ""),
        qty=qty,
        payload={
            "source_snapshot_id": snapshot.id,
            "source_snapshot_version": source_version,
            "target_box_id": line.target_box_id,
            "target_box_code": str(getattr(line.target_box, "box_code", "") or ""),
            "staging_location_id": plan.staging_location_id,
            "staging_location_code": str(
                getattr(plan.staging_location, "location_code", "") or ""
            ),
            "mode": plan.mode,
            "marking_code": snapshot.marking_code,
            "lot_code": lot_code,
            "expiry_date": expiry_date.isoformat() if expiry_date else "",
        },
        performed_by=confirmed_by,
        performed_by_role="storekeeper",
        occurred_at=reserved_at,
    )
    snapshot.last_event = event
    snapshot.save(
        update_fields=[
            "available_qty",
            "other_reserved_qty",
            "snapshot_version",
            "active_operation",
            "active_operation_type",
            "last_event",
            "updated_at",
        ]
    )
    return FbsReplenishmentAllocation.objects.create(
        line=line,
        source_snapshot=snapshot,
        target_box=line.target_box,
        warehouse_reserve=reserve,
        warehouse_task=task,
        qty_planned=qty,
        source_snapshot_version=source_version,
    )


def _confirm_item_line(
    *,
    plan: FbsReplenishmentPlan,
    line: FbsReplenishmentLine,
    operation: WarehouseOperation,
    confirmed_by,
    source_allocations: list[dict] | None = None,
) -> tuple[int, WarehouseReserve | None, WarehouseStockSnapshot | None]:
    remaining = line.qty_requested
    planned = 0
    first_reserve = None
    first_snapshot = None
    snapshots = WarehouseStockSnapshot.objects.select_for_update(of=("self",)).select_related(
        "container", "location", "sku_ref"
    )
    if source_allocations is not None:
        allocation_rows = list(source_allocations)
        expected_ids = [int(row.get("snapshot_id") or 0) for row in allocation_rows]
        if not allocation_rows or any(not snapshot_id for snapshot_id in expected_ids):
            raise FbsReplenishmentError("Точный резерв FBS не содержит складских остатков.")
        if len(expected_ids) != len(set(expected_ids)):
            raise FbsReplenishmentError("Складской остаток повторяется в точном резерве FBS.")
        snapshots_by_id = {
            snapshot.id: snapshot
            for snapshot in snapshots.filter(id__in=expected_ids)
        }
        if len(snapshots_by_id) != len(expected_ids):
            raise FbsReplenishmentError("Часть точного резерва FBS больше не существует.")
        exact_rows = []
        for row in allocation_rows:
            snapshot = snapshots_by_id[int(row["snapshot_id"])]
            qty = int(row.get("qty") or 0)
            if qty <= 0:
                raise FbsReplenishmentError("В точном резерве FBS указано неверное количество.")
            exact_rows.append((snapshot, qty))
        if sum(qty for _, qty in exact_rows) != int(line.qty_requested or 0):
            raise FbsReplenishmentError(
                "Количество точного резерва FBS не совпадает со строкой заявки."
            )
    else:
        snapshots = (
            snapshots.filter(
                agency=plan.agency,
                barcode=line.barcode,
                is_archived=False,
                is_in_vehicle=False,
                available_qty__gt=0,
                active_operation__isnull=True,
            )
            .filter(_plan_source_stock_q(plan))
            .exclude(
                zone_code__iexact=str(
                    getattr(settings, "FBS_ZONE_CODE", "FBS") or "FBS"
                )
            )
        )
    if source_allocations is None and line.sku_ref_id and line.sku_ref.honest_sign:
        snapshots = snapshots.exclude(marking_code="").exclude(marking_code__isnull=True)
    if source_allocations is None and "expiry_date" in WAREHOUSE_SNAPSHOT_FIELDS:
        snapshots = snapshots.filter(
            Q(expiry_date__isnull=True) | Q(expiry_date__gte=timezone.localdate())
        ).order_by(F("expiry_date").asc(nulls_last=True), "created_at", "id")
    elif source_allocations is None:
        snapshots = snapshots.order_by("created_at", "id")
    if source_allocations is not None:
        source_rows = exact_rows
    else:
        candidate_snapshots = list(snapshots)
        shipping_blocked_ids = WarehouseWritePathService.shipping_reserved_snapshot_ids(
            agency=plan.agency,
            snapshots=candidate_snapshots,
        )
        source_rows = [
            (snapshot, min(remaining, int(snapshot.available_qty or 0)))
            for snapshot in candidate_snapshots
            if int(snapshot.id) not in shipping_blocked_ids
        ]
    receiving_allowed_ids = receiving_placement_allowed_snapshot_ids(
        [snapshot for snapshot, _qty in source_rows]
    ) if plan.client_movement_request_id else None
    for snapshot, exact_qty in source_rows:
        if remaining <= 0:
            break
        qty = int(exact_qty)
        if qty > remaining:
            raise FbsReplenishmentError(
                "Точный резерв FBS превышает количество строки заявки."
            )
        task = _create_task(
            plan=plan,
            operation=operation,
            snapshot=snapshot,
            qty=qty,
            task_type=WarehouseOperationTask.TYPE_PARTIAL_PICK,
        )
        allocation = _reserve_snapshot(
            plan=plan,
            line=line,
            operation=operation,
            task=task,
            snapshot=snapshot,
            qty=qty,
            confirmed_by=confirmed_by,
            lock_whole_snapshot=False,
            receiving_allowed_ids=receiving_allowed_ids,
        )
        first_reserve = first_reserve or allocation.warehouse_reserve
        first_snapshot = first_snapshot or snapshot
        planned += qty
        remaining -= qty
    if remaining > 0:
        raise FbsReplenishmentError(
            f"Недостаточно общего остатка по ШК {line.barcode}: не хватает {remaining}."
        )
    return planned, first_reserve, first_snapshot


def _confirm_box_line(
    *,
    plan: FbsReplenishmentPlan,
    line: FbsReplenishmentLine,
    operation: WarehouseOperation,
    confirmed_by,
    source_allocations: list[dict] | None = None,
) -> tuple[int, WarehouseReserve | None, WarehouseStockSnapshot | None]:
    source_container = WarehouseContainer.objects.select_for_update().get(
        pk=line.source_container_id
    )
    if source_container.agency_id != plan.agency_id:
        raise FbsReplenishmentError("Исходный короб принадлежит другому клиенту.")
    if source_container.container_type != WarehouseContainer.TYPE_BOX:
        raise FbsReplenishmentError("Подсорт коробами принимает только контейнер типа box.")
    if _normalize_scan(line.target_box.box_code) != _normalize_scan(source_container.container_code):
        raise FbsReplenishmentError(
            "При перемещении целого короба код FBS-короба должен совпадать с исходным коробом."
        )
    if line.target_box.source_container_id not in {None, source_container.id}:
        raise FbsReplenishmentError("FBS-короб уже связан с другим складским контейнером.")

    snapshots = list(
        WarehouseStockSnapshot.objects.select_for_update(of=("self",))
        .select_related("container", "location", "sku_ref")
        .filter(
            container=source_container,
            is_archived=False,
            qty__gt=0,
        )
        .order_by("id")
    )
    if not snapshots:
        raise FbsReplenishmentError("В исходном коробе нет складского остатка.")
    receiving_allowed_ids = (
        receiving_placement_allowed_snapshot_ids(snapshots)
        if plan.client_movement_request_id
        else None
    )
    try:
        WarehouseWritePathService.assert_not_reserved_for_shipping(
            agency=plan.agency,
            snapshots=snapshots,
        )
    except WarehouseTransitionError as exc:
        raise FbsReplenishmentError(str(exc)) from exc
    exact_qty_by_snapshot: dict[int, int] | None = None
    if source_allocations is not None:
        exact_qty_by_snapshot = {
            int(row.get("snapshot_id") or 0): int(row.get("qty") or 0)
            for row in source_allocations
        }
        if (
            not exact_qty_by_snapshot
            or 0 in exact_qty_by_snapshot
            or set(exact_qty_by_snapshot) != {snapshot.id for snapshot in snapshots}
            or any(qty <= 0 for qty in exact_qty_by_snapshot.values())
        ):
            raise FbsReplenishmentError(
                "Точный резерв FBS не совпадает с составом исходного короба."
            )
    for snapshot in snapshots:
        if snapshot.agency_id != plan.agency_id:
            raise FbsReplenishmentError(
                "В исходном коробе обнаружены остатки нескольких клиентов."
            )
        if not _is_plan_source_stock_allowed(
            plan,
            snapshot,
            receiving_allowed_ids=receiving_allowed_ids,
        ):
            raise FbsReplenishmentError(
                "В FBS можно перемещать только готовый короб с основного хранения."
            )
        expected_available = (
            int(exact_qty_by_snapshot[snapshot.id])
            if exact_qty_by_snapshot is not None
            else int(snapshot.qty or 0)
        )
        if (
            expected_available != int(snapshot.qty or 0)
            or int(snapshot.available_qty or 0) != expected_available
            or int(snapshot.processing_reserved_qty or 0) > 0
            or int(snapshot.shipping_reserved_qty or 0) > 0
            or int(snapshot.other_reserved_qty or 0) > 0
            or snapshot.active_operation_id is not None
            or snapshot.is_in_vehicle
        ):
            raise FbsReplenishmentError(
                f"Короб {source_container.container_code} имеет резервы или активную операцию."
            )
    planned = sum(int(snapshot.qty or 0) for snapshot in snapshots)
    if planned != line.qty_requested:
        raise FbsReplenishmentError(
            "Состав исходного короба изменился после построения плана. Постройте план заново."
        )
    task = _create_task(
        plan=plan,
        operation=operation,
        snapshot=snapshots[0],
        qty=planned,
        task_type=WarehouseOperationTask.TYPE_BOX_MOVE,
    )
    first_reserve = None
    for snapshot in snapshots:
        allocation = _reserve_snapshot(
            plan=plan,
            line=line,
            operation=operation,
            task=task,
            snapshot=snapshot,
            qty=int(snapshot.qty or 0),
            confirmed_by=confirmed_by,
            lock_whole_snapshot=True,
            receiving_allowed_ids=receiving_allowed_ids,
        )
        first_reserve = first_reserve or allocation.warehouse_reserve
    line.target_box.source_container = source_container
    line.target_box.save(update_fields=["source_container", "updated_at"])
    return planned, first_reserve, snapshots[0]


@transaction.atomic
def confirm_replenishment_plan(
    *,
    plan_id: int,
    confirmed_by=None,
    source_allocations: list[dict] | None = None,
    validated_box_locks=None,
    defer_client_movement_sync: bool = False,
    defer_reachtruck_tasks: bool = False,
) -> FbsReplenishmentPlan:
    _require_warehouse_writes()
    actor = _authenticated_user(confirmed_by)
    plan = (
        FbsReplenishmentPlan.objects.select_for_update(of=("self",))
        .select_related(
            "agency",
            "target_cell__location",
            "target_pallet",
            "target_box__pallet",
            "staging_location",
        )
        .get(pk=plan_id)
    )
    if plan.status in {
        FbsReplenishmentPlan.STATUS_CONFIRMED,
        FbsReplenishmentPlan.STATUS_IN_PROGRESS,
        FbsReplenishmentPlan.STATUS_AWAITING_PACK,
        FbsReplenishmentPlan.STATUS_DONE,
    }:
        if (
            plan.client_movement_request_id
            and plan.status != FbsReplenishmentPlan.STATUS_DONE
            and not defer_reachtruck_tasks
        ):
            from .reachtruck_bridge import sync_plan_reachtruck_tasks

            sync_plan_reachtruck_tasks(
                plan,
                sync_request=not defer_client_movement_sync,
            )
        return plan
    if plan.status != FbsReplenishmentPlan.STATUS_PROPOSED:
        raise FbsReplenishmentError("Подтвердить можно только предложенный план FBS.")
    target_pallet = FbsPallet.objects.select_for_update().get(pk=plan.target_pallet_id)
    target_box = None
    if plan.target_box_id:
        target_box = FbsBox.objects.select_for_update().select_related("pallet").get(
            pk=plan.target_box_id
        )
    plan.target_pallet = target_pallet
    plan.target_box = target_box
    if _uses_prepared_box_flow(plan):
        _validate_fbs_staging_location(plan.staging_location)
        prepared_boxes = list(
            FbsReplenishmentPreparedBox.objects.select_for_update()
            .filter(plan=plan)
            .order_by("sequence_no", "id")
        )
        if not prepared_boxes or any(
            box.status != FbsReplenishmentPreparedBox.STATUS_PRINTED
            or box.scanned_qty
            or box.placed_qty
            for box in prepared_boxes
        ):
            raise FbsReplenishmentError("Печатные FBS-короба не готовы к началу перемещения.")
    elif _requires_storekeeper_packing(plan):
        _validate_fbs_staging_location(plan.staging_location)
        _validate_target_pallet(plan)
    elif target_box is not None:
        _validate_destination(plan, target_box)
        from .inventory import assert_box_unlocked

        assert_box_unlocked(target_box.id, validated_batch=validated_box_locks)
    else:
        raise FbsReplenishmentError("У плана отсутствует короб назначения.")

    operation = _create_operation(plan, actor)
    first_reserve = None
    first_snapshot = None
    total_planned = 0
    lines = list(
        FbsReplenishmentLine.objects.select_for_update(of=("self",))
        .select_related("sku_ref", "source_container", "target_box")
        .filter(plan=plan)
        .order_by("id")
    )
    if not lines:
        raise FbsReplenishmentError("В плане подсорта нет строк.")
    allocations_by_request_line: dict[int, list[dict]] | None = None
    allocations_by_source_container: dict[int, list[dict]] | None = None
    if source_allocations is not None:
        if plan.mode == FbsReplenishmentPlan.MODE_BOX:
            allocations_by_source_container = {}
            for row in source_allocations:
                container_id = int(row.get("container_id") or 0)
                if not container_id:
                    raise FbsReplenishmentError(
                        "Точный резерв целого короба FBS не связан с контейнером."
                    )
                allocations_by_source_container.setdefault(container_id, []).append(row)
        else:
            allocations_by_request_line = {}
            for row in source_allocations:
                request_line_id = int(row.get("request_line_id") or 0)
                if not request_line_id:
                    raise FbsReplenishmentError(
                        "Точный резерв FBS не связан со строкой клиентской заявки."
                    )
                allocations_by_request_line.setdefault(request_line_id, []).append(row)
    for line in lines:
        exact_line_allocations = None
        if allocations_by_source_container is not None:
            if not line.source_container_id:
                raise FbsReplenishmentError(
                    "Строка коробного плана не связана с исходным контейнером."
                )
            exact_line_allocations = allocations_by_source_container.pop(
                line.source_container_id,
                None,
            )
            if not exact_line_allocations:
                raise FbsReplenishmentError(
                    "Для исходного короба отсутствует точный резерв FBS."
                )
        elif allocations_by_request_line is not None:
            if not line.client_movement_line_id:
                raise FbsReplenishmentError(
                    "Строка складского плана не связана с клиентской заявкой FBS."
                )
            exact_line_allocations = allocations_by_request_line.pop(
                line.client_movement_line_id,
                None,
            )
            if not exact_line_allocations:
                raise FbsReplenishmentError(
                    "Для строки складского плана отсутствует точный резерв FBS."
                )
        if plan.mode == FbsReplenishmentPlan.MODE_BOX:
            planned, reserve, source_snapshot = _confirm_box_line(
                plan=plan,
                line=line,
                operation=operation,
                confirmed_by=actor,
                source_allocations=exact_line_allocations,
            )
        else:
            planned, reserve, source_snapshot = _confirm_item_line(
                plan=plan,
                line=line,
                operation=operation,
                confirmed_by=actor,
                source_allocations=exact_line_allocations,
            )
        line.qty_planned = planned
        line.status = FbsReplenishmentLine.STATUS_RESERVED
        line.save(update_fields=["qty_planned", "status", "updated_at"])
        total_planned += planned
        first_reserve = first_reserve or reserve
        first_snapshot = first_snapshot or source_snapshot
    if allocations_by_request_line:
        raise FbsReplenishmentError(
            "В точном резерве FBS остались строки, которых нет в складском плане."
        )
    if allocations_by_source_container:
        raise FbsReplenishmentError(
            "В точном резерве FBS остались короба, которых нет в складском плане."
        )

    operation.reserve = first_reserve
    if first_snapshot is not None:
        operation.source_location = first_snapshot.location
        operation.source_zone_code = str(first_snapshot.zone_code or "")
    operation.planned_qty = total_planned
    operation.save(
        update_fields=[
            "reserve",
            "source_location",
            "source_zone_code",
            "planned_qty",
            "updated_at",
        ]
    )
    plan.warehouse_operation = operation
    plan.confirmed_by = actor
    plan.confirmed_at = timezone.now()
    plan.status = FbsReplenishmentPlan.STATUS_CONFIRMED
    plan.planned_qty = total_planned
    plan.save(
        update_fields=[
            "warehouse_operation",
            "confirmed_by",
            "confirmed_at",
            "status",
            "planned_qty",
            "updated_at",
        ]
    )
    if plan.client_movement_request_id:
        from .client_movements import sync_client_movement_request_status

        # The caller defers the driver route when it does not yet know whether the
        # pallet will be carried at all: a whole-pallet movement is posted
        # logically a few lines later and the route would be cancelled at once.
        if not defer_reachtruck_tasks:
            from .reachtruck_bridge import sync_plan_reachtruck_tasks

            sync_plan_reachtruck_tasks(
                plan,
                sync_request=not defer_client_movement_sync,
            )
        if not defer_client_movement_sync:
            sync_client_movement_request_status(
                plan.client_movement_request_id, performed_by=actor
            )
    return plan


@transaction.atomic
def claim_replenishment_plans(
    *,
    plan_ids: Iterable[int],
    assigned_to,
    allow_reassignment: bool = False,
    expected_assigned_to_ids: Mapping[int, int | None] | None = None,
    sync_reachtruck_assignments: bool = True,
) -> tuple[FbsReplenishmentPlan, ...]:
    _require_warehouse_writes()
    actor = _authenticated_user(assigned_to)
    if actor is None:
        raise FbsReplenishmentError("Для получения задания требуется сотрудник ричтрака.")
    normalized_ids = tuple(sorted({int(value) for value in plan_ids if int(value) > 0}))
    if not normalized_ids:
        raise FbsReplenishmentError("Планы FBS для получения в работу не найдены.")
    plans = list(
        FbsReplenishmentPlan.objects.select_for_update()
        .filter(pk__in=normalized_ids)
        .order_by("id")
    )
    if len(plans) != len(normalized_ids):
        raise FbsReplenishmentError("Не все планы FBS найдены.")
    expected_by_plan = {
        int(key): (int(value) if value is not None else None)
        for key, value in dict(expected_assigned_to_ids or {}).items()
    }
    changed_plans: list[FbsReplenishmentPlan] = []
    for plan in plans:
        if plan.status not in {
            FbsReplenishmentPlan.STATUS_CONFIRMED,
            FbsReplenishmentPlan.STATUS_IN_PROGRESS,
        }:
            raise FbsReplenishmentError("План FBS недоступен для получения в работу.")
        reassignment_required = plan.assigned_to_id not in {None, actor.id}
        if reassignment_required:
            if not allow_reassignment:
                raise FbsReplenishmentError("План FBS уже взят другим сотрудником.")
            expected_assigned_to_id = expected_by_plan.get(plan.id)
            if (
                not expected_assigned_to_id
                or plan.assigned_to_id != expected_assigned_to_id
            ):
                raise FbsReplenishmentError(
                    "Исполнитель плана FBS уже изменился. Обновите заявку и повторите действие."
                )
        if plan.assigned_to_id != actor.id:
            changed_plans.append(plan)

    now = timezone.now()
    if changed_plans:
        changed_plan_ids = [plan.id for plan in changed_plans]
        FbsReplenishmentPlan.objects.filter(pk__in=changed_plan_ids).update(
            assigned_to=actor,
            updated_at=now,
        )
        assigned_name = str(actor.get_full_name() or actor.get_username() or "").strip()
        WarehouseOperationTask.objects.filter(
            operation_id__in=[plan.warehouse_operation_id for plan in changed_plans]
        ).update(
            assigned_to=actor,
            assigned_to_name=assigned_name,
            updated_at=now,
        )
        for plan in changed_plans:
            plan.assigned_to = actor
            plan.updated_at = now

    movement_request_ids = sorted(
        {
            int(plan.client_movement_request_id)
            for plan in plans
            if plan.client_movement_request_id
        }
    )
    if movement_request_ids:
        if sync_reachtruck_assignments:
            from .reachtruck_bridge import sync_plans_reachtruck_assignment

            sync_plans_reachtruck_assignment(plans, actor)
        from .client_movements import sync_client_movement_request_status

        for request_id in movement_request_ids:
            sync_client_movement_request_status(request_id, performed_by=actor)
    return tuple(plans)


@transaction.atomic
def claim_replenishment_plan(
    *,
    plan_id: int,
    assigned_to,
    allow_reassignment: bool = False,
    expected_assigned_to_id: int | None = None,
) -> FbsReplenishmentPlan:
    return claim_replenishment_plans(
        plan_ids=(plan_id,),
        assigned_to=assigned_to,
        allow_reassignment=allow_reassignment,
        expected_assigned_to_ids={int(plan_id): expected_assigned_to_id},
    )[0]


@transaction.atomic
def replace_missing_whole_box_source(
    *,
    allocation_id: int,
    missing_box_code: str,
    performed_by,
) -> FbsReplenishmentAllocation | None:
    """Atomically move an FBS whole-box reserve to an identical free box."""
    _require_warehouse_writes()
    actor = _authenticated_user(performed_by)
    allocation = (
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "line__plan__agency",
            "line__plan__warehouse_operation",
            "line__target_box",
            "line__source_container",
            "source_snapshot__container",
            "warehouse_task",
            "warehouse_reserve",
        )
        .get(pk=allocation_id)
    )
    plan = allocation.line.plan
    if actor is None or plan.assigned_to_id != actor.id:
        raise FbsReplenishmentError("Задание FBS не назначено этому водителю.")
    if plan.mode != FbsReplenishmentPlan.MODE_BOX:
        raise FbsReplenishmentError("Автозамена доступна только для перемещения целого короба.")
    if allocation.status not in {
        FbsReplenishmentAllocation.STATUS_RESERVED,
        FbsReplenishmentAllocation.STATUS_IN_PROGRESS,
    } or int(allocation.qty_moved or 0) > 0 or int(allocation.qty_staged or 0) > 0:
        raise FbsReplenishmentError("Источник FBS уже начал проводиться; автоматическая замена запрещена.")
    missing_container = allocation.line.source_container
    if (
        missing_container is None
        or _normalize_scan(missing_container.container_code) != _normalize_scan(missing_box_code)
    ):
        raise FbsReplenishmentError("Короб уже заменен или не относится к этому FBS-заданию.")

    from sklad.services.missing_box_replacement import find_exact_free_box_replacement

    replacement = find_exact_free_box_replacement(
        agency_id=plan.agency_id,
        missing_container_id=missing_container.id,
    )
    if replacement is None:
        # «Нет короба» от водителя — это только факт осмотра. Он не создаёт
        # карантин, новый резерв или отдельную задачу начальнику склада.
        WarehouseEvent.objects.get_or_create(
            agency=plan.agency,
            event_type="fbs_missing_box_reported",
            stock_context_type=CONTEXT_TYPE,
            stock_context_id=str(plan.id),
            container=missing_container,
            operation=plan.warehouse_operation,
            operation_task=allocation.warehouse_task,
            reserve=allocation.warehouse_reserve,
            defaults={
                "source_document_type": SOURCE_DOCUMENT_TYPE,
                "source_document_id": str(plan.id),
                "from_location": allocation.source_snapshot.location,
                "from_zone_code": str(allocation.source_snapshot.zone_code or ""),
                "qty": max(
                    int(allocation.qty_planned or 0) - int(allocation.qty_moved or 0),
                    0,
                ),
                "payload": {
                    "allocation_id": allocation.id,
                    "missing_box_code": missing_container.container_code,
                    "replacement_found": False,
                    "stock_unchanged": True,
                    "reserve_unchanged": True,
                },
                "performed_by": actor,
                "performed_by_role": "reachtruck_driver",
                "occurred_at": timezone.now(),
            },
        )
        return None
    old_allocations = list(
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related("source_snapshot", "warehouse_reserve")
        .filter(line=allocation.line)
        .order_by("id")
    )
    old_rows = sorted(
        old_allocations,
        key=lambda row: (_whole_box_snapshot_identity(row.source_snapshot), row.id),
    )
    replacement_rows = sorted(
        replacement.snapshots,
        key=lambda snapshot: (_whole_box_snapshot_identity(snapshot), snapshot.id),
    )
    if [
        _whole_box_snapshot_identity(row.source_snapshot) for row in old_rows
    ] != [
        _whole_box_snapshot_identity(snapshot) for snapshot in replacement_rows
    ]:
        return None

    for allocation_row, replacement_snapshot in zip(old_rows, replacement_rows):
        old_snapshot = WarehouseStockSnapshot.objects.select_for_update(of=("self",)).get(
            pk=allocation_row.source_snapshot_id
        )
        new_snapshot = WarehouseStockSnapshot.objects.select_for_update(of=("self",)).get(
            pk=replacement_snapshot.pk
        )
        qty = int(allocation_row.qty_planned or 0) - int(allocation_row.qty_moved or 0)
        if qty <= 0 or int(new_snapshot.available_qty or 0) < qty:
            raise FbsReplenishmentError("Похожий короб перестал быть доступен; повторите поиск.")
        if int(old_snapshot.other_reserved_qty or 0) < qty:
            raise FbsReplenishmentError("Резерв исходного FBS-короба поврежден.")

        restored_available = int(old_snapshot.available_qty or 0) + qty
        if restored_available > int(old_snapshot.qty or 0):
            raise FbsReplenishmentError("Снятие резерва исходного короба превысит его количество.")
        old_snapshot.available_qty = restored_available
        old_snapshot.other_reserved_qty = int(old_snapshot.other_reserved_qty or 0) - qty
        if old_snapshot.active_operation_id == plan.warehouse_operation_id:
            old_snapshot.active_operation = None
            old_snapshot.active_operation_type = ""
        old_snapshot.snapshot_version = int(old_snapshot.snapshot_version or 0) + 1
        old_snapshot.save(
            update_fields=[
                "available_qty",
                "other_reserved_qty",
                "active_operation",
                "active_operation_type",
                "snapshot_version",
                "updated_at",
            ]
        )

        # Бизнес-резерв переносится на найденный аналог. Исходный короб
        # остаётся свободным: водитель не создаёт для него блокировку.
        new_snapshot.available_qty = int(new_snapshot.available_qty or 0) - qty
        new_snapshot.other_reserved_qty = int(new_snapshot.other_reserved_qty or 0) + qty
        new_snapshot.active_operation = plan.warehouse_operation
        new_snapshot.active_operation_type = plan.warehouse_operation.operation_type
        new_snapshot.snapshot_version = int(new_snapshot.snapshot_version or 0) + 1
        new_snapshot.save(
            update_fields=[
                "available_qty",
                "other_reserved_qty",
                "active_operation",
                "active_operation_type",
                "snapshot_version",
                "updated_at",
            ]
        )

        reserve = WarehouseReserve.objects.select_for_update().get(
            pk=allocation_row.warehouse_reserve_id
        )
        reserve.sku_ref = new_snapshot.sku_ref
        reserve.sku_code = new_snapshot.sku_code
        reserve.size = new_snapshot.size
        reserve.barcode = new_snapshot.barcode
        reserve.goods_type = new_snapshot.goods_type
        reserve.marking_code = new_snapshot.marking_code
        reserve_update_fields = [
            "sku_ref",
            "sku_code",
            "size",
            "barcode",
            "goods_type",
            "marking_code",
        ]
        if "lot_code" in WAREHOUSE_RESERVE_FIELDS:
            reserve.lot_code = _snapshot_lot_code(new_snapshot)
            reserve_update_fields.append("lot_code")
        if "expiry_date" in WAREHOUSE_RESERVE_FIELDS:
            reserve.expiry_date = _snapshot_expiry_date(new_snapshot)
            reserve_update_fields.append("expiry_date")
        reserve.save(update_fields=[*reserve_update_fields, "updated_at"])
        allocation_row.source_snapshot = new_snapshot
        allocation_row.source_snapshot_version = int(new_snapshot.snapshot_version or 0)
        allocation_row.save(
            update_fields=["source_snapshot", "source_snapshot_version", "updated_at"]
        )
        WarehouseEvent.objects.create(
            agency=plan.agency,
            event_type="fbs_missing_box_replaced",
            stock_context_type=CONTEXT_TYPE,
            stock_context_id=str(plan.id),
            container=replacement.container,
            operation=plan.warehouse_operation,
            operation_task=allocation_row.warehouse_task,
            reserve=reserve,
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(plan.id),
            from_location=replacement.location,
            from_zone_code=str(getattr(replacement.location, "zone_code", "") or ""),
            qty=qty,
            payload={
                "allocation_id": allocation_row.id,
                "missing_container_id": missing_container.id,
                "missing_box_code": missing_container.container_code,
                "replacement_container_id": replacement.container.id,
                "replacement_box_code": replacement.container.container_code,
                "accounting_qty_unchanged": True,
                "source_reserve_released": True,
            },
            performed_by=actor,
            performed_by_role="reachtruck_driver",
            occurred_at=timezone.now(),
        )

    line = allocation.line
    old_target_box = FbsBox.objects.select_for_update().get(pk=line.target_box_id)
    new_target_box = (
        FbsBox.objects.select_for_update()
        .filter(agency_id=plan.agency_id, box_code=replacement.container.container_code)
        .order_by("id")
        .first()
    )
    if new_target_box is None:
        new_target_box = FbsBox(
            agency_id=plan.agency_id,
            pallet_id=old_target_box.pallet_id,
            box_code=replacement.container.container_code,
            source_container=replacement.container,
            status=FbsBox.STATUS_PLANNED,
        )
        new_target_box.full_clean()
        new_target_box.save()
    elif new_target_box.source_container_id not in {None, replacement.container.id}:
        raise FbsReplenishmentError("QR похожего короба уже связан с другим контейнером.")
    else:
        new_target_box.pallet_id = old_target_box.pallet_id
        new_target_box.source_container = replacement.container
        new_target_box.status = FbsBox.STATUS_PLANNED
        new_target_box.full_clean()
        new_target_box.save(
            update_fields=["pallet", "source_container", "status", "updated_at"]
        )
    if old_target_box.source_container_id == missing_container.id:
        old_target_box.source_container = None
        old_target_box.status = FbsBox.STATUS_ARCHIVED
        old_target_box.save(update_fields=["source_container", "status", "updated_at"])
    line.source_container = replacement.container
    line.target_box = new_target_box
    line.save(update_fields=["source_container", "target_box", "updated_at"])
    FbsReplenishmentAllocation.objects.filter(line=line).update(
        target_box=new_target_box,
        updated_at=timezone.now(),
    )
    task = allocation.warehouse_task
    task.container = replacement.container
    task.from_location = replacement.location
    task.from_zone_code = str(getattr(replacement.location, "zone_code", "") or "")
    task_payload = dict(task.payload or {})
    task_payload.update(
        {
            "source_snapshot_id": replacement.snapshots[0].id,
            "source_scan_codes": sorted(_source_scan_values(replacement.snapshots[0])),
            "target_box_code": new_target_box.box_code,
            "missing_box_replacement": {
                "missing_box_code": missing_container.container_code,
                "replacement_box_code": replacement.container.container_code,
                "replaced_at": timezone.localtime().isoformat(),
            },
        }
    )
    task.payload = task_payload
    task.save(
        update_fields=["container", "from_location", "from_zone_code", "payload", "updated_at"]
    )
    plan.target_box = new_target_box
    plan.warehouse_operation.source_location = replacement.location
    plan.warehouse_operation.source_zone_code = str(
        getattr(replacement.location, "zone_code", "") or ""
    )
    plan.warehouse_operation.save(
        update_fields=["source_location", "source_zone_code", "updated_at"]
    )
    plan.save(update_fields=["target_box", "updated_at"])
    return (
        FbsReplenishmentAllocation.objects.select_related(
            "source_snapshot__container__parent_container",
            "source_snapshot__location",
            "line__plan",
        )
        .get(pk=allocation.pk)
    )


@transaction.atomic
def skip_missing_whole_box_source(
    *,
    allocation_id: int,
    missing_box_code: str,
    performed_by,
) -> FbsReplenishmentPlan:
    """Cancel one unavailable FBS box while keeping its quantity quarantined.

    This is intentionally different from ``cancel_replenishment_plan``: the
    business reserve is replaced by an equally sized missing-box reserve, so
    the unavailable quantity never becomes available again.
    """
    _require_warehouse_writes()
    actor = _authenticated_user(performed_by)
    allocation = (
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "line__plan__agency",
            "line__plan__warehouse_operation",
            "line__target_box",
            "line__source_container",
            "source_snapshot__container",
            "warehouse_task",
            "warehouse_reserve",
        )
        .get(pk=allocation_id)
    )
    line = allocation.line
    plan = line.plan
    if actor is None or plan.assigned_to_id != actor.id:
        raise FbsReplenishmentError("Задание FBS не назначено этому водителю.")
    if plan.mode != FbsReplenishmentPlan.MODE_BOX:
        raise FbsReplenishmentError("Пропуск доступен только для перемещения целого короба.")
    if allocation.status not in {
        FbsReplenishmentAllocation.STATUS_RESERVED,
        FbsReplenishmentAllocation.STATUS_IN_PROGRESS,
    } or int(allocation.qty_moved or 0) > 0 or int(allocation.qty_staged or 0) > 0:
        raise FbsReplenishmentError(
            "Источник FBS уже начал проводиться; завершение без этого короба запрещено."
        )
    missing_container = line.source_container
    if (
        missing_container is None
        or _normalize_scan(missing_container.container_code) != _normalize_scan(missing_box_code)
    ):
        raise FbsReplenishmentError("Короб уже заменен или не относится к этому FBS-заданию.")

    allocations = list(
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related("source_snapshot", "warehouse_reserve", "warehouse_task")
        .filter(line=line)
        .order_by("id")
    )
    if not allocations:
        raise FbsReplenishmentError("Не найден резерв отсутствующего FBS-короба.")

    from sklad.services.missing_box_replacement import quarantine_missing_box

    quarantine_operation = quarantine_missing_box(
        container_id=missing_container.id,
        agency_id=plan.agency_id,
        context_type=CONTEXT_TYPE,
        context_id=str(plan.id),
        performed_by=actor,
        reserve_existing_blocked=True,
    )
    now = timezone.now()
    skipped_qty = 0
    for allocation_row in allocations:
        snapshot = WarehouseStockSnapshot.objects.select_for_update(of=("self",)).get(
            pk=allocation_row.source_snapshot_id
        )
        qty = int(allocation_row.qty_planned or 0) - int(allocation_row.qty_moved or 0)
        if qty <= 0 or int(snapshot.other_reserved_qty or 0) < qty:
            raise FbsReplenishmentError("Резерв отсутствующего FBS-короба поврежден.")

        # Do not alter qty/available/reserved here. quarantine_missing_box has
        # already bound the same held quantity to the verification reserve.
        reserve = WarehouseReserve.objects.select_for_update().get(
            pk=allocation_row.warehouse_reserve_id
        )
        reserve.status = WarehouseReserve.STATUS_CANCELED
        reserve.released_by = actor
        reserve.save(update_fields=["status", "released_by", "updated_at"])
        allocation_row.status = FbsReplenishmentAllocation.STATUS_CANCELED
        allocation_row.save(update_fields=["status", "updated_at"])
        allocation_row.warehouse_task.status = WarehouseOperationTask.STATUS_CANCELED
        task_payload = dict(allocation_row.warehouse_task.payload or {})
        task_payload["missing_box_skipped_v1"] = {
            "box_code": missing_container.container_code,
            "skipped_at": timezone.localtime(now).isoformat(),
            "quarantine_operation_id": quarantine_operation.id,
        }
        allocation_row.warehouse_task.payload = task_payload
        allocation_row.warehouse_task.save(update_fields=["status", "payload", "updated_at"])
        event = WarehouseEvent.objects.create(
            agency=plan.agency,
            event_type="fbs_missing_box_skipped",
            stock_context_type=CONTEXT_TYPE,
            stock_context_id=str(plan.id),
            container=missing_container,
            operation=plan.warehouse_operation,
            operation_task=allocation_row.warehouse_task,
            reserve=reserve,
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(plan.id),
            from_location=snapshot.location,
            from_zone_code=str(snapshot.zone_code or ""),
            qty=qty,
            payload={
                "allocation_id": allocation_row.id,
                "missing_box_code": missing_container.container_code,
                "quarantine_operation_id": quarantine_operation.id,
                "accounting_qty_unchanged": True,
                "partial_batch_completion": True,
            },
            performed_by=actor,
            performed_by_role="reachtruck_driver",
            occurred_at=now,
        )
        snapshot.last_event = event
        snapshot.save(update_fields=["last_event", "updated_at"])
        skipped_qty += qty

    line.status = FbsReplenishmentLine.STATUS_CANCELED
    line.save(update_fields=["status", "updated_at"])
    target_box = FbsBox.objects.select_for_update().get(pk=line.target_box_id)
    if target_box.source_container_id == missing_container.id:
        target_box.source_container = None
        target_box.status = FbsBox.STATUS_ARCHIVED
        target_box.save(update_fields=["source_container", "status", "updated_at"])
    if plan.warehouse_operation_id:
        plan.warehouse_operation.status = WarehouseOperation.STATUS_CANCELED
        plan.warehouse_operation.comment = (
            f"Короб {missing_container.container_code} отсутствует; "
            "позиция исключена из частично собранной FBS-заявки."
        )
        plan.warehouse_operation.save(update_fields=["status", "comment", "updated_at"])
    plan.status = FbsReplenishmentPlan.STATUS_CANCELED
    plan.canceled_at = now
    plan.comment = (
        f"[missing_box_partial] Короб {missing_container.container_code} не найден. "
        f"Исключено {skipped_qty} шт.; остаток оставлен в карантине до проверки."
    )
    plan.save(update_fields=["status", "canceled_at", "comment", "updated_at"])
    if plan.client_movement_request_id:
        from .client_movements import sync_client_movement_request_status

        sync_client_movement_request_status(
            plan.client_movement_request_id, performed_by=actor
        )
    return plan


def validate_replenishment_source_scan(
    *,
    allocation_id: int,
    source_scan: str,
    performed_by,
) -> FbsReplenishmentAllocation:
    _require_warehouse_writes()
    actor = _authenticated_user(performed_by)
    allocation = (
        FbsReplenishmentAllocation.objects.select_related(
            "line__plan",
            "source_snapshot__container",
            "source_snapshot__location",
        )
        .filter(pk=allocation_id)
        .first()
    )
    if allocation is None:
        raise FbsReplenishmentError("Задание FBS не найдено.")
    if actor is None or allocation.line.plan.assigned_to_id != actor.id:
        raise FbsReplenishmentError("Задание FBS не назначено этому сотруднику.")
    if allocation.status not in {
        FbsReplenishmentAllocation.STATUS_RESERVED,
        FbsReplenishmentAllocation.STATUS_IN_PROGRESS,
    }:
        raise FbsReplenishmentError("Задание FBS уже закрыто или отменено.")
    if _normalize_scan(source_scan) not in _source_scan_values(allocation.source_snapshot):
        raise FbsReplenishmentError("Скан источника не совпадает с заданием FBS.")
    return allocation


def _locked_destination_pallet(
    *,
    plan: FbsReplenishmentPlan,
    target_scan: str,
) -> FbsPallet:
    pallets = (
        FbsPallet.objects.select_for_update()
        .select_related("cell__location")
        .filter(
            agency_id=plan.agency_id,
            status__in=(FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE),
            cell__is_active=True,
        )
    )
    pallet = pallets.filter(pallet_code__iexact=target_scan).first()
    if pallet is not None:
        return pallet
    cell = (
        FbsStorageCell.objects.select_for_update()
        .filter(cell_code__iexact=target_scan, is_active=True)
        .first()
    )
    if cell is None:
        raise FbsReplenishmentError("QR места назначения FBS не найден.")
    pallet = pallets.filter(cell=cell).first()
    if pallet is None:
        raise FbsReplenishmentError(
            "В этой FBS-ячейке нет активной паллеты выбранного клиента."
        )
    return pallet


def _set_operation_destination(
    *,
    plan: FbsReplenishmentPlan,
    target_box: FbsBox,
) -> None:
    operation = plan.warehouse_operation
    if operation is None:
        raise FbsReplenishmentError("У плана отсутствует складская операция.")
    operation.destination_location = plan.target_cell.location
    operation.destination_zone_code = str(plan.target_cell.location.zone_code or "")
    operation.save(
        update_fields=["destination_location", "destination_zone_code", "updated_at"]
    )
    tasks = list(WarehouseOperationTask.objects.select_for_update().filter(operation=operation))
    for task in tasks:
        payload = dict(task.payload or {})
        payload.update(
            {
                "target_box_code": target_box.box_code,
                "target_pallet_code": plan.target_pallet.pallet_code,
                "target_cell_code": plan.target_cell.cell_code,
            }
        )
        task.to_location = plan.target_cell.location
        task.to_zone_code = str(plan.target_cell.location.zone_code or "")
        task.payload = payload
        task.save(update_fields=["to_location", "to_zone_code", "payload", "updated_at"])


def _select_replenishment_destination(
    *,
    allocation: FbsReplenishmentAllocation,
    target_scan: str,
    actor,
) -> FbsReplenishmentAllocation:
    scan = str(target_scan or "").strip()
    if not scan:
        raise FbsReplenishmentError("Отсканируйте QR места назначения FBS.")
    plan = (
        FbsReplenishmentPlan.objects.select_for_update(of=("self",))
        .select_related(
            "agency",
            "target_cell__location",
            "target_pallet",
            "target_box__pallet__cell__location",
            "warehouse_operation",
        )
        .get(pk=allocation.line.plan_id)
    )
    all_allocations = list(
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related("target_box")
        .filter(line__plan=plan)
        .order_by("id")
    )
    any_moved = any(int(row.qty_moved or 0) > 0 for row in all_allocations)
    old_cell_id = plan.target_cell_id
    old_pallet_id = plan.target_pallet_id
    old_box_id = allocation.target_box_id

    if plan.mode == FbsReplenishmentPlan.MODE_ITEM:
        target_box = (
            FbsBox.objects.select_for_update()
            .select_related("pallet__cell__location")
            .filter(
                agency_id=plan.agency_id,
                box_code__iexact=scan,
                status__in=(FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE),
                pallet__status__in=(FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE),
                pallet__cell__is_active=True,
            )
            .first()
        )
        if target_box is None:
            raise FbsReplenishmentError("QR короба FBS этого клиента не найден.")
        if any_moved and target_box.id != plan.target_box_id:
            raise FbsReplenishmentError(
                "После первого перемещения место этого плана менять нельзя."
            )
        plan.target_box = target_box
        plan.target_pallet = target_box.pallet
        plan.target_cell = target_box.pallet.cell
        FbsReplenishmentLine.objects.filter(plan=plan).update(
            target_box=target_box,
            updated_at=timezone.now(),
        )
        FbsReplenishmentAllocation.objects.filter(line__plan=plan).update(
            target_box=target_box,
            updated_at=timezone.now(),
        )
    else:
        target_box = (
            FbsBox.objects.select_for_update()
            .select_related("pallet__cell__location")
            .get(pk=plan.target_box_id)
        )
        if plan.client_movement_request_id:
            if any_moved:
                if str(plan.target_cell.location.zone_code or "").strip().upper() == "PR":
                    pr_location = _pr_location_for_scan(scan)
                    if pr_location is None:
                        raise FbsReplenishmentError(
                            "После первого перемещения ожидается QR выбранной PR-ячейки."
                        )
                    destination = pr_location
                else:
                    destination = _resolve_active_os_location(scan)
                if plan.target_cell.location_id != destination.id:
                    raise FbsReplenishmentError(
                        "После первого перемещения место этого плана менять нельзя."
                    )
            else:
                _bind_box_to_scanned_client_movement_location(
                    plan=plan,
                    target_box=target_box,
                    destination_scan=scan,
                    actor=actor,
                )
            plan.refresh_from_db()
            target_box = (
                FbsBox.objects.select_for_update()
                .select_related("pallet__cell")
                .get(pk=plan.target_box_id)
            )
            plan.target_box = target_box
            plan.target_pallet = target_box.pallet
            plan.target_cell = target_box.pallet.cell
        else:
            if _normalize_scan(scan) == _normalize_scan(target_box.box_code):
                target_pallet = target_box.pallet
            else:
                target_pallet = _locked_destination_pallet(plan=plan, target_scan=scan)
            if any_moved and target_pallet.id != plan.target_pallet_id:
                raise FbsReplenishmentError(
                    "После первого перемещения место этого плана менять нельзя."
                )
            if target_box.pallet_id != target_pallet.id:
                target_box.pallet = target_pallet
                target_box.full_clean()
                target_box.save(update_fields=["pallet", "updated_at"])
            plan.target_box = target_box
            plan.target_pallet = target_pallet
            plan.target_cell = target_pallet.cell

    _validate_destination(plan, target_box)
    plan.full_clean()
    plan.save(
        update_fields=["target_cell", "target_pallet", "target_box", "updated_at"]
    )
    _set_operation_destination(plan=plan, target_box=target_box)
    changed = (
        old_cell_id != plan.target_cell_id
        or old_pallet_id != plan.target_pallet_id
        or old_box_id != target_box.id
    )
    if changed:
        WarehouseEvent.objects.create(
            agency=plan.agency,
            event_type="fbs_replenishment_destination_selected",
            stock_context_type=CONTEXT_TYPE,
            stock_context_id=str(plan.id),
            operation=plan.warehouse_operation,
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(plan.id),
            to_location=plan.target_cell.location,
            to_zone_code=str(plan.target_cell.location.zone_code or ""),
            qty=0,
            payload={
                "allocation_id": allocation.id,
                "old_cell_id": old_cell_id,
                "old_pallet_id": old_pallet_id,
                "old_box_id": old_box_id,
                "target_cell_id": plan.target_cell_id,
                "target_pallet_id": plan.target_pallet_id,
                "target_box_id": target_box.id,
                "target_scan": scan,
                "mode": plan.mode,
            },
            performed_by=actor,
            performed_by_role="reachtruck",
            occurred_at=timezone.now(),
        )
    return (
        FbsReplenishmentAllocation.objects.select_related(
            "line__plan__agency",
            "line__plan__target_cell__location",
            "line__plan__target_pallet__warehouse_container",
            "line__plan__warehouse_operation",
            "line__source_container",
            "target_box__pallet",
            "warehouse_task",
        )
        .get(pk=allocation.pk)
    )


def _get_or_create_balance(
    *,
    allocation: FbsReplenishmentAllocation,
    snapshot: WarehouseStockSnapshot,
) -> FbsStockBalance:
    return _get_or_create_balance_for_box(
        target_box=allocation.target_box,
        allocation=allocation,
        snapshot=snapshot,
    )


def _get_or_create_balance_for_box(
    *,
    target_box: FbsBox,
    allocation: FbsReplenishmentAllocation,
    snapshot: WarehouseStockSnapshot,
) -> FbsStockBalance:
    identity_key = _stock_identity(snapshot)
    balance = (
        FbsStockBalance.objects.select_for_update()
        .filter(box=target_box, identity_key=identity_key)
        .first()
    )
    if balance is not None:
        if balance.agency_id != allocation.line.plan.agency_id:
            raise FbsReplenishmentError("FBS-остаток назначения принадлежит другому клиенту.")
        return balance
    balance = FbsStockBalance(
        agency=allocation.line.plan.agency,
        box=target_box,
        sku_ref=snapshot.sku_ref,
        identity_key=identity_key,
        sku_code=snapshot.sku_code,
        name=snapshot.name,
        size=snapshot.size,
        barcode=snapshot.barcode,
        goods_type=snapshot.goods_type,
        marking_code=snapshot.marking_code,
        lot_code=_snapshot_lot_code(snapshot),
        expiry_date=_snapshot_expiry_date(snapshot),
    )
    balance.full_clean()
    balance.save()
    return balance


def _assert_single_physical_stock_contour(
    containers: Iterable[WarehouseContainer],
) -> None:
    """Reject a completed whole-box posting that leaves stock in FBO and FBS."""
    containers_by_id = {
        int(container.id): container
        for container in containers
        if int(getattr(container, "id", 0) or 0) > 0
    }
    if not containers_by_id:
        return

    fbs_container_ids = {
        int(container_id)
        for container_id in (
            FbsBox.objects.select_for_update(of=("self",))
            .filter(
                source_container_id__in=containers_by_id,
                stock_balances__qty__gt=0,
            )
            .values_list("source_container_id", flat=True)
        )
        if container_id
    }
    if not fbs_container_ids:
        return

    conflicting_snapshot = (
        WarehouseStockSnapshot.objects.select_for_update(of=("self",))
        .filter(
            container_id__in=fbs_container_ids,
            is_archived=False,
            qty__gt=0,
        )
        .select_related("container")
        .order_by("id")
        .first()
    )
    if conflicting_snapshot is None:
        return

    container = containers_by_id.get(int(conflicting_snapshot.container_id))
    container_code = str(
        getattr(container, "container_code", "")
        or conflicting_snapshot.container_code
        or conflicting_snapshot.container_id
    ).strip()
    raise FbsReplenishmentError(
        "Проведение остановлено: короб "
        f"{container_code} одновременно получит остаток FBS и основного склада. "
        "Выполните инвентаризацию короба."
    )


def _refresh_completion_status(
    allocation: FbsReplenishmentAllocation,
    now,
    *,
    actor,
    line_ids: Iterable[int] | None = None,
    preserve_source_physical_place: bool | None = None,
    defer_request_sync: bool = False,
) -> None:
    normalized_line_ids = list(
        dict.fromkeys(int(value) for value in (line_ids or (allocation.line_id,)))
    )
    lines = list(
        FbsReplenishmentLine.objects.select_for_update(of=("self",))
        .select_related("plan")
        .filter(id__in=normalized_line_ids)
        .order_by("id")
    )
    if not lines:
        raise FbsReplenishmentError("Строки плана FBS не найдены.")
    plan = allocation.line.plan
    preserve_physical_place = (
        is_virtual_fbs_plan_location(plan.target_cell.location)
        if preserve_source_physical_place is None
        else bool(preserve_source_physical_place)
    )
    if plan.mode == FbsReplenishmentPlan.MODE_BOX and not preserve_physical_place:
        _require_concrete_physical_location(
            plan.target_cell.location,
            purpose="Нельзя закрыть задание FBS",
        )
    source_container_ids = {
        int(line.source_container_id)
        for line in lines
        if line.source_container_id
    }
    source_containers = {
        int(container.id): container
        for container in WarehouseContainer.objects.select_for_update(of=("self",))
        .select_related("current_location", "parent_container__current_location")
        .filter(id__in=source_container_ids)
        .order_by("id")
    }
    completed_source_containers = {}
    for line in lines:
        line.qty_moved = int(
            line.allocations.aggregate(total=Sum("qty_moved"))["total"] or 0
        )
        line_has_open = line.allocations.exclude(
            status=FbsReplenishmentAllocation.STATUS_DONE
        ).exists()
        line.status = (
            FbsReplenishmentLine.STATUS_IN_PROGRESS
            if line_has_open
            else FbsReplenishmentLine.STATUS_DONE
        )
        line.save(update_fields=["qty_moved", "status", "updated_at"])
        if not line_has_open and line.source_container_id:
            source_container = source_containers.get(int(line.source_container_id))
            if source_container is None:
                raise FbsReplenishmentError("Исходный короб FBS не найден.")
            completed_source_containers[line.source_container_id] = source_container

    _assert_single_physical_stock_contour(completed_source_containers.values())

    task = allocation.warehouse_task
    task.qty_done = int(
        task.fbs_replenishment_allocations.aggregate(total=Sum("qty_moved"))["total"] or 0
    )
    task_has_open = task.fbs_replenishment_allocations.exclude(
        status=FbsReplenishmentAllocation.STATUS_DONE
    ).exists()
    if task.started_at is None:
        task.started_at = now
    if task_has_open:
        task.status = WarehouseOperationTask.STATUS_IN_PROGRESS
    else:
        task.status = WarehouseOperationTask.STATUS_DONE
        task.completed_at = now
    task.save(
        update_fields=["qty_done", "status", "started_at", "completed_at", "updated_at"]
    )

    plan.moved_qty = int(
        FbsReplenishmentAllocation.objects.filter(line__plan=plan).aggregate(
            total=Sum("qty_moved")
        )["total"]
        or 0
    )
    plan_has_open = FbsReplenishmentAllocation.objects.filter(line__plan=plan).exclude(
        status=FbsReplenishmentAllocation.STATUS_DONE
    ).exists()
    if plan.started_at is None:
        plan.started_at = now
    if plan_has_open:
        plan.status = FbsReplenishmentPlan.STATUS_IN_PROGRESS
    else:
        plan.status = FbsReplenishmentPlan.STATUS_DONE
        plan.completed_at = now
    plan.save(
        update_fields=["moved_qty", "status", "started_at", "completed_at", "updated_at"]
    )

    operation = plan.warehouse_operation
    operation.done_qty = plan.moved_qty
    if operation.started_at is None:
        operation.started_at = now
    if plan_has_open:
        operation.status = WarehouseOperation.STATUS_IN_PROGRESS
    else:
        operation.status = WarehouseOperation.STATUS_DONE
        operation.completed_at = now
    operation.save(
        update_fields=["done_qty", "status", "started_at", "completed_at", "updated_at"]
    )

    if plan.client_movement_request_id:
        from .client_movements import sync_client_movement_request_status
        from .reachtruck_bridge import sync_allocation_reachtruck_done

        sync_allocation_reachtruck_done(
            allocation,
            sync_request=not defer_request_sync,
        )
        if not defer_request_sync:
            sync_client_movement_request_status(
                plan.client_movement_request_id, performed_by=actor
            )

    if plan.mode == FbsReplenishmentPlan.MODE_BOX:
        # A plan cell may point at a whole zone rather than an addressed place.
        # Writing that onto the container erases the concrete cell the box
        # already had: the picker is then sent to a zone that carries no label
        # to scan, and the box is effectively unfindable.  The branch just below
        # already refuses a depersonalized place; hold the same line here, but
        # without failing work that is physically finished -- keep the address
        # the box already has instead of degrading it.
        target_location = plan.target_cell.location
        target_is_addressed = _is_concrete_physical_location(target_location)
        for source_container in completed_source_containers.values():
            if preserve_physical_place:
                _container_physical_location(
                    source_container,
                    purpose="Нельзя сохранить паллету FBS на обезличенном месте",
                )
                continue
            update_fields = ["parent_container", "updated_at"]
            if target_is_addressed or not _is_concrete_physical_location(
                source_container.current_location
            ):
                source_container.current_location = target_location
                update_fields.insert(0, "current_location")
            source_container.parent_container = plan.target_pallet.warehouse_container
            source_container.save(update_fields=update_fields)
        if (
            plan.client_movement_request_id
            and str(plan.target_cell.location.zone_code or "").strip().upper() == "PR"
        ):
            for source_container in completed_source_containers.values():
                source_box = (
                    FbsBox.objects.select_for_update(of=("self",))
                    .filter(source_container=source_container)
                    .first()
                )
                if source_box is None or source_box.id == plan.target_box_id:
                    continue
                if FbsStockBalance.objects.filter(box=source_box, qty__gt=0).exists():
                    raise FbsReplenishmentError(
                        "Исходный короб нельзя закрыть: в нем остался FBS-остаток."
                    )
                if WarehouseStockSnapshot.objects.filter(
                    container=source_container,
                    is_archived=False,
                    qty__gt=0,
                ).exists():
                    raise FbsReplenishmentError(
                        "Исходный короб нельзя закрыть: в нем остался складской остаток."
                    )
                source_box.status = FbsBox.STATUS_ARCHIVED
                source_box.save(update_fields=["status", "updated_at"])
                source_container.status = WarehouseContainer.STATUS_ARCHIVED
                source_container.save(update_fields=["status", "updated_at"])
    allocation.target_box.status = FbsBox.STATUS_ACTIVE
    allocation.target_box.save(update_fields=["status", "updated_at"])
    plan.target_pallet.status = FbsPallet.STATUS_ACTIVE
    plan.target_pallet.save(update_fields=["status", "updated_at"])


def _storekeeper_actor(user):
    actor = _authenticated_user(user)
    if actor is None:
        raise FbsReplenishmentError("Для укладки товара требуется кладовщик.")
    if getattr(actor, "is_superuser", False):
        return actor
    from employees.models import Employee

    role = str(
        Employee.objects.filter(user=actor, is_active=True)
        .order_by("id")
        .values_list("role", flat=True)
        .first()
        or ""
    )
    if role not in {"storekeeper", "head_manager", "director", "admin"}:
        raise FbsReplenishmentError("Уложить товар в FBS-короб может только кладовщик.")
    return actor


def _generated_fbs_box_code(agency_id: int) -> str:
    for _ in range(10):
        code = f"FBS-BOX-{int(agency_id)}-{uuid4().hex[:10].upper()}"
        if (
            not FbsBox.objects.filter(agency_id=agency_id, box_code=code).exists()
            and not FbsReplenishmentPreparedBox.objects.filter(
                agency_id=agency_id,
                box_code=code,
            ).exists()
            and not WarehouseContainer.objects.filter(
                agency_id=agency_id,
                container_code=code,
            ).exists()
        ):
            return code
    raise FbsReplenishmentError("Не удалось сформировать уникальный QR нового FBS-короба.")


@transaction.atomic
def prepare_replenishment_boxes(
    *,
    plan_id: int,
    box_count: int,
    prepared_by=None,
) -> tuple[FbsReplenishmentPreparedBox, ...]:
    """Prints final QR boxes before item picking; no FBS stock or slot is booked."""
    _require_warehouse_writes()
    actor = _storekeeper_actor(prepared_by)
    try:
        requested_count = int(box_count)
    except (TypeError, ValueError) as exc:
        raise FbsReplenishmentError("Укажите количество коробов для печати.") from exc
    if requested_count < 1 or requested_count > MAX_PREPARED_BOXES_PER_PLAN:
        raise FbsReplenishmentError(
            f"Количество коробов должно быть от 1 до {MAX_PREPARED_BOXES_PER_PLAN}."
        )
    plan = (
        FbsReplenishmentPlan.objects.select_for_update(of=("self",))
        .select_related("agency", "staging_location", "warehouse_operation")
        .get(pk=plan_id)
    )
    if (
        plan.mode != FbsReplenishmentPlan.MODE_ITEM
        or not plan.client_movement_request_id
        or not plan.staging_location_id
    ):
        raise FbsReplenishmentError("Печатные короба доступны только штучной заявке FBS.")
    if plan.status != FbsReplenishmentPlan.STATUS_PROPOSED:
        raise FbsReplenishmentError("Короба нужно напечатать до передачи задания ричтраку.")
    existing = tuple(
        FbsReplenishmentPreparedBox.objects.select_for_update(of=("self",))
        .filter(plan=plan)
        .order_by("sequence_no", "id")
    )
    if existing:
        if len(existing) != requested_count:
            raise FbsReplenishmentError(
                f"Для плана уже напечатано коробов: {len(existing)}."
            )
        return existing

    boxes = []
    now = timezone.now()
    for sequence_no in range(1, requested_count + 1):
        code = _generated_fbs_box_code(plan.agency_id)
        physical_box = WarehouseContainer.objects.create(
            agency=plan.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code=code,
            current_location=plan.staging_location,
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type=FBS_PREPARED_BOX_CONTEXT_TYPE,
            source_context_id=str(plan.id),
            created_by=actor,
        )
        prepared_box = FbsReplenishmentPreparedBox(
            plan=plan,
            agency=plan.agency,
            sequence_no=sequence_no,
            box_code=code,
            physical_container=physical_box,
            printed_by=actor,
        )
        prepared_box.full_clean()
        prepared_box.save()
        boxes.append(prepared_box)
        WarehouseEvent.objects.create(
            agency=plan.agency,
            event_type="fbs_replenishment_box_printed",
            stock_context_type=CONTEXT_TYPE,
            stock_context_id=str(plan.id),
            container=physical_box,
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(plan.id),
            from_location=plan.staging_location,
            to_location=plan.staging_location,
            from_zone_code=str(plan.staging_location.zone_code or ""),
            to_zone_code=str(plan.staging_location.zone_code or ""),
            qty=0,
            payload={
                "prepared_box_id": prepared_box.id,
                "prepared_box_code": prepared_box.box_code,
                "sequence_no": sequence_no,
                "printed_box_count": requested_count,
            },
            performed_by=actor,
            performed_by_role="storekeeper",
            occurred_at=now,
        )
    return tuple(boxes)


def _create_staged_fbs_box(
    *,
    plan: FbsReplenishmentPlan,
    actor,
) -> FbsBox:
    code = _generated_fbs_box_code(plan.agency_id)
    physical_box = WarehouseContainer.objects.create(
        agency=plan.agency,
        container_type=WarehouseContainer.TYPE_BOX,
        container_code=code,
        current_location=plan.staging_location,
        status=WarehouseContainer.STATUS_ACTIVE,
        source_context_type=FBS_PLACEMENT_CONTEXT_TYPE,
        source_context_id=str(plan.id),
        created_by=actor,
    )
    from .storage import create_fbs_box

    return create_fbs_box(
        agency=plan.agency,
        pallet=plan.target_pallet,
        box_code=code,
        source_container=physical_box,
    )


def _resolve_active_os_location(scan_value: str) -> WarehouseLocation:
    scan = _normalize_os_scan(scan_value)
    location = (
        WarehouseLocation.objects.select_for_update()
        .filter(
            warehouse_code="MSK",
            zone_code__iexact="OS",
            location_code__iexact=scan,
            is_active=True,
            is_storage=True,
            row_no__gt=0,
            section_no__gt=0,
            tier_no__gt=0,
            cell_no__gt=0,
        )
        .first()
    )
    if location is not None:
        return _require_concrete_physical_location(
            location,
            purpose="Нельзя завершить FBS-перемещение",
        )
    legacy_match = _LEGACY_OS_SCAN_RE.match(scan)
    if legacy_match:
        row = int(legacy_match.group("row"))
        section = int(legacy_match.group("section"))
        tier = int(legacy_match.group("tier"))
        cell = int(legacy_match.group("cell"))
    else:
        match = _OS_SCAN_RE.match(scan)
        if match is None:
            raise FbsReplenishmentError(
                "Отсканируйте QR выбранной ячейки действующей топологии OS."
            )
        section_token = str(match.group("line") or "").upper()
        section = _OS_SECTION_BY_SCAN_LABEL.get(section_token)
        if section is None and section_token.isdigit():
            section = int(section_token)
        row = int(match.group("row"))
        tier = int(match.group("tier"))
        cell = int(match.group("cell"))
    if min(row, int(section or 0), tier, cell) <= 0:
        raise FbsReplenishmentError(
            "Отсканируйте точный QR ячейки OS: ряд, секция, ярус и место должны быть указаны."
        )
    location = (
        WarehouseLocation.objects.select_for_update()
        .filter(
            warehouse_code="MSK",
            zone_code__iexact="OS",
            row_no=row,
            section_no=int(section or 0),
            tier_no=tier,
            cell_no=cell,
            is_active=True,
            is_storage=True,
        )
        .first()
    )
    if location is None:
        raise FbsReplenishmentError(
            "Ячейка не найдена в действующей топологии склада или выключена."
        )
    return _require_concrete_physical_location(
        location,
        purpose="Нельзя завершить FBS-перемещение",
    )


def _occupied_fbs_destination_error(
    *,
    destination: WarehouseLocation,
    pallet: FbsPallet,
) -> str:
    pallet_code = str(
        getattr(getattr(pallet, "warehouse_container", None), "container_code", "")
        or pallet.pallet_code
        or f"FBS-{pallet.id}"
    ).strip()
    location_code = os_location_code(
        row=destination.row_no,
        section=destination.section_no,
        tier=destination.tier_no,
        cell=destination.cell_no,
    )
    return (
        f"В ячейке {location_code} находится FBS-паллета другого клиента "
        f"{pallet_code}. Смешивать товар разных клиентов на одной паллете нельзя."
    )


def _pallet_is_dedicated_to_plan(
    *,
    plan: FbsReplenishmentPlan,
    target_box: FbsBox,
    pallet: FbsPallet,
) -> bool:
    if FbsBox.objects.filter(
        pallet=pallet,
        status__in=(FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE),
    ).exclude(pk=target_box.pk).exists():
        return False
    return not FbsReplenishmentPlan.objects.filter(
        target_pallet=pallet,
        status__in=(
            FbsReplenishmentPlan.STATUS_PROPOSED,
            FbsReplenishmentPlan.STATUS_CONFIRMED,
            FbsReplenishmentPlan.STATUS_IN_PROGRESS,
            FbsReplenishmentPlan.STATUS_AWAITING_PACK,
        ),
    ).exclude(pk=plan.pk).exists()


def _pr_location_for_scan(destination_scan: str) -> WarehouseLocation | None:
    from sklad.services.operational_locations import normalize_operational_location_scan

    code = normalize_operational_location_scan(destination_scan)
    if not code:
        return None
    return (
        WarehouseLocation.objects.select_for_update()
        .filter(
            warehouse_code="MSK",
            location_code__iexact=code,
            zone_code__iexact="PR",
        )
        .first()
    )


def _bind_box_to_scanned_client_movement_location(
    *,
    plan: FbsReplenishmentPlan,
    target_box: FbsBox,
    destination_scan: str,
    actor,
) -> WarehouseLocation:
    pr_location = _pr_location_for_scan(destination_scan)
    if pr_location is None:
        return _bind_box_to_scanned_os_location(
            plan=plan,
            target_box=target_box,
            destination_scan=destination_scan,
        )

    from fbs.exceptions import FbsMovementError
    from .racks import bind_client_movement_box_to_rack_cell

    try:
        binding = bind_client_movement_box_to_rack_cell(
            source_box=target_box,
            target_cell_scan=destination_scan,
            agency=plan.agency,
            performed_by=actor,
        )
    except FbsMovementError as exc:
        raise FbsReplenishmentError(str(exc)) from exc

    plan.target_cell = binding.rack_cell.storage_cell
    plan.target_pallet = binding.pallet
    plan.target_box = binding.box
    plan.full_clean()
    plan.save(update_fields=["target_cell", "target_pallet", "target_box", "updated_at"])
    now = timezone.now()
    FbsReplenishmentLine.objects.filter(plan=plan).update(
        target_box=binding.box,
        updated_at=now,
    )
    FbsReplenishmentAllocation.objects.filter(line__plan=plan).update(
        target_box=binding.box,
        updated_at=now,
    )
    return binding.rack_cell.storage_cell.location


def _bind_box_to_scanned_os_location(
    *,
    plan: FbsReplenishmentPlan,
    target_box: FbsBox,
    destination_scan: str,
) -> WarehouseLocation:
    destination = _resolve_active_os_location(destination_scan)
    target_box = (
        FbsBox.objects.select_for_update(of=("self",))
        .select_related("pallet__cell__location", "pallet__warehouse_container", "source_container")
        .get(pk=target_box.pk)
    )
    pallet = FbsPallet.objects.select_for_update(of=("self",)).select_related(
        "cell__location", "warehouse_container"
    ).get(pk=target_box.pallet_id)
    if target_box.agency_id != plan.agency_id or pallet.agency_id != plan.agency_id:
        raise FbsReplenishmentError("FBS-короб или паллета принадлежат другому клиенту.")
    dedicated = _pallet_is_dedicated_to_plan(
        plan=plan,
        target_box=target_box,
        pallet=pallet,
    )
    destination_cell, _created = FbsStorageCell.objects.get_or_create(
        location=destination,
        defaults={
            "cell_code": f"FBS@{os_location_code(row=destination.row_no, section=destination.section_no, tier=destination.tier_no, cell=destination.cell_no)}",
            "purpose": FbsStorageCell.PURPOSE_FLEX,
            "client_cluster": plan.agency_id,
            "is_active": True,
        },
    )
    destination_cell = FbsStorageCell.objects.select_for_update().get(pk=destination_cell.pk)
    if not destination_cell.is_active:
        raise FbsReplenishmentError("Выбранная ячейка выключена для FBS.")
    destination_pallet = (
        FbsPallet.objects.select_for_update(of=("self",))
        .select_related("warehouse_container", "cell__location")
        .filter(
            cell=destination_cell,
            status__in=(FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE),
        )
        .order_by("id")
        .first()
    )
    if destination_pallet is not None and destination_pallet.id != pallet.id:
        from .storage import release_unplaced_os_reservation

        if release_unplaced_os_reservation(pallet=destination_pallet):
            destination_pallet = None
    if destination_pallet is not None:
        if destination_pallet.agency_id != plan.agency_id:
            raise FbsReplenishmentError(
                _occupied_fbs_destination_error(
                    destination=destination,
                    pallet=destination_pallet,
                )
            )
        pallet = destination_pallet
        if target_box.pallet_id != pallet.id:
            target_box.pallet = pallet
            target_box.full_clean()
            target_box.save(update_fields=["pallet", "updated_at"])
    else:
        if dedicated:
            pallet.cell = destination_cell
            pallet.full_clean()
            pallet.save(update_fields=["cell", "updated_at"])
            if pallet.warehouse_container_id:
                pallet.warehouse_container.current_location = destination
                pallet.warehouse_container.save(
                    update_fields=["current_location", "updated_at"]
                )
            else:
                pallet.warehouse_container = WarehouseContainer.objects.create(
                    agency=plan.agency,
                    container_type=WarehouseContainer.TYPE_PALLET,
                    container_code=pallet.pallet_code,
                    current_location=destination,
                    status=WarehouseContainer.STATUS_ACTIVE,
                    source_context_type=FBS_STORAGE_CONTEXT_TYPE,
                    source_context_id=pallet.pallet_code,
                )
                pallet.save(update_fields=["warehouse_container", "updated_at"])
        else:
            from .storage import create_fbs_pallet

            try:
                pallet = create_fbs_pallet(
                    agency=plan.agency,
                    cell=destination_cell,
                    pallet_code=f"FBS-PAL-{plan.agency_id}-{uuid4().hex[:10].upper()}",
                )
            except FbsStorageError as exc:
                # A physical OS place may be occupied by a regular warehouse box
                # even when no FBS pallet exists there yet. Keep that operational
                # conflict user-facing instead of turning it into a generic HTTP 500.
                raise FbsReplenishmentError(str(exc)) from exc
            target_box.pallet = pallet
            target_box.full_clean()
            target_box.save(update_fields=["pallet", "updated_at"])

    if pallet.warehouse_container_id is None:
        pallet.warehouse_container = WarehouseContainer.objects.create(
            agency=plan.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code=pallet.pallet_code,
            current_location=destination,
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type=FBS_STORAGE_CONTEXT_TYPE,
            source_context_id=pallet.pallet_code,
        )
        pallet.save(update_fields=["warehouse_container", "updated_at"])

    physical_box = target_box.source_container
    if physical_box is None:
        physical_box = WarehouseContainer.objects.create(
            agency=plan.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code=target_box.box_code,
            current_location=destination,
            parent_container=pallet.warehouse_container,
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type=FBS_PLACEMENT_CONTEXT_TYPE,
            source_context_id=str(plan.id),
        )
        target_box.source_container = physical_box
        target_box.save(update_fields=["source_container", "updated_at"])
    else:
        physical_box = WarehouseContainer.objects.select_for_update().get(
            pk=physical_box.pk
        )
        physical_box.current_location = destination
        physical_box.parent_container = pallet.warehouse_container
        physical_box.status = WarehouseContainer.STATUS_ACTIVE
        physical_box.source_context_type = FBS_PLACEMENT_CONTEXT_TYPE
        physical_box.source_context_id = str(plan.id)
        physical_box.save(
            update_fields=[
                "current_location",
                "parent_container",
                "status",
                "source_context_type",
                "source_context_id",
                "updated_at",
            ]
        )

    plan.target_cell = pallet.cell
    plan.target_pallet = pallet
    plan.target_box = target_box
    plan.full_clean()
    plan.save(update_fields=["target_cell", "target_pallet", "target_box", "updated_at"])
    FbsReplenishmentLine.objects.filter(plan=plan).update(
        target_box=target_box,
        updated_at=timezone.now(),
    )
    FbsReplenishmentAllocation.objects.filter(line__plan=plan).update(
        target_box=target_box,
        updated_at=timezone.now(),
    )
    return destination


@transaction.atomic
def stage_replenishment_allocation(
    *,
    allocation_id: int,
    source_scan: str,
    staging_container_scan: str,
    staging_scan: str,
    performed_by=None,
) -> FbsReplenishmentAllocation:
    """Moves a client item request out of common stock into storekeeper staging."""
    _require_warehouse_writes()
    actor = _authenticated_user(performed_by)
    allocation = (
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "line__plan__agency",
            "line__plan__staging_location",
            "line__plan__warehouse_operation",
            "source_snapshot__container",
            "source_snapshot__location",
            "source_snapshot__sku_ref",
            "warehouse_task",
        )
        .get(pk=allocation_id)
    )
    if allocation.status == FbsReplenishmentAllocation.STATUS_STAGED:
        return allocation
    if allocation.status not in {
        FbsReplenishmentAllocation.STATUS_RESERVED,
        FbsReplenishmentAllocation.STATUS_IN_PROGRESS,
    }:
        raise FbsReplenishmentError("Задание нельзя передать кладовщику в текущем статусе.")
    plan = allocation.line.plan
    if not _requires_storekeeper_packing(plan):
        raise FbsReplenishmentError("Это задание должно завершаться напрямую в FBS-коробе.")
    if actor is None or plan.assigned_to_id != actor.id:
        raise FbsReplenishmentError("Задание FBS не назначено этому сотруднику.")

    snapshot = WarehouseStockSnapshot.objects.select_for_update(of=("self",)).get(
        pk=allocation.source_snapshot_id
    )
    if _normalize_scan(source_scan) not in _source_scan_values(snapshot):
        raise FbsReplenishmentError("Скан источника не совпадает с заданием FBS.")
    expected_staging_scan = _normalize_scan(
        plan.staging_location.location_code or plan.staging_location.zone_code
    )
    movement = plan.client_movement_request
    expected_container_code = movement_staging_container_code(movement)
    if _normalize_scan(staging_container_scan) != _normalize_scan(expected_container_code):
        raise FbsReplenishmentError(
            f"Отсканируйте временный короб заявки {expected_container_code}."
        )
    if _normalize_scan(staging_scan) != expected_staging_scan:
        raise FbsReplenishmentError("Отсканирована другая зона подготовки FBS.")
    staging_container = ensure_movement_staging_container(
        movement=movement,
        location=plan.staging_location,
        created_by=actor,
    )

    reserve = WarehouseReserve.objects.select_for_update().get(
        pk=allocation.warehouse_reserve_id
    )
    qty = int(allocation.qty_planned or 0) - int(allocation.qty_staged or 0)
    if qty <= 0:
        raise FbsReplenishmentError("В задании нет количества для передачи кладовщику.")
    if int(snapshot.qty or 0) < qty or int(snapshot.other_reserved_qty or 0) < qty:
        raise FbsReplenishmentError("Общий остаток изменился и больше не покрывает резерв FBS.")

    now = timezone.now()
    snapshot.qty = int(snapshot.qty or 0) - qty
    snapshot.other_reserved_qty = int(snapshot.other_reserved_qty or 0) - qty
    snapshot.snapshot_version = int(snapshot.snapshot_version or 0) + 1
    if snapshot.qty == 0:
        snapshot.available_qty = 0
        snapshot.is_archived = True
    if snapshot.active_operation_id == plan.warehouse_operation_id:
        snapshot.active_operation = None
        snapshot.active_operation_type = ""
    reserve.qty_satisfied = reserve.qty_reserved
    reserve.status = WarehouseReserve.STATUS_SATISFIED
    reserve.save(update_fields=["qty_satisfied", "status", "updated_at"])
    event = WarehouseEvent.objects.create(
        agency=plan.agency,
        event_type="fbs_replenishment_staged",
        stock_context_type=CONTEXT_TYPE,
        stock_context_id=str(plan.id),
        container=snapshot.container,
        operation=plan.warehouse_operation,
        operation_task=allocation.warehouse_task,
        reserve=reserve,
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(plan.id),
        from_location=snapshot.location,
        to_location=plan.staging_location,
        from_zone_code=str(snapshot.zone_code or ""),
        to_zone_code=str(plan.staging_location.zone_code or ""),
        qty=qty,
        payload={
            "allocation_id": allocation.id,
            "source_snapshot_id": snapshot.id,
            "staging_location_id": plan.staging_location_id,
            "staging_location_code": plan.staging_location.location_code,
            "staging_container_id": staging_container.id,
            "staging_container_code": staging_container.container_code,
            "mode": plan.mode,
            "marking_code": snapshot.marking_code,
            "lot_code": _snapshot_lot_code(snapshot),
            "expiry_date": (
                _snapshot_expiry_date(snapshot).isoformat()
                if _snapshot_expiry_date(snapshot)
                else ""
            ),
        },
        performed_by=actor,
        performed_by_role="reachtruck",
        occurred_at=now,
    )
    snapshot.last_event = event
    snapshot.save(
        update_fields=[
            "qty",
            "available_qty",
            "other_reserved_qty",
            "snapshot_version",
            "active_operation",
            "active_operation_type",
            "is_archived",
            "last_event",
            "updated_at",
        ]
    )
    allocation.qty_staged = allocation.qty_planned
    allocation.status = FbsReplenishmentAllocation.STATUS_STAGED
    allocation.staged_by = actor
    allocation.staged_at = now
    allocation.save(
        update_fields=["qty_staged", "status", "staged_by", "staged_at", "updated_at"]
    )
    line = allocation.line
    line.status = FbsReplenishmentLine.STATUS_IN_PROGRESS
    line.save(update_fields=["status", "updated_at"])
    task = allocation.warehouse_task
    task.qty_done = int(
        task.fbs_replenishment_allocations.aggregate(total=Sum("qty_staged"))["total"]
        or 0
    )
    task.status = WarehouseOperationTask.STATUS_DONE
    task.started_at = task.started_at or now
    task.completed_at = now
    task.save(
        update_fields=["qty_done", "status", "started_at", "completed_at", "updated_at"]
    )
    all_staged = not FbsReplenishmentAllocation.objects.filter(line__plan=plan).exclude(
        status=FbsReplenishmentAllocation.STATUS_STAGED
    ).exists()
    plan.status = (
        FbsReplenishmentPlan.STATUS_AWAITING_PACK
        if all_staged
        else FbsReplenishmentPlan.STATUS_IN_PROGRESS
    )
    plan.started_at = plan.started_at or now
    plan.save(update_fields=["status", "started_at", "updated_at"])
    operation = plan.warehouse_operation
    operation.done_qty = int(
        FbsReplenishmentAllocation.objects.filter(line__plan=plan).aggregate(
            total=Sum("qty_staged")
        )["total"]
        or 0
    )
    operation.status = WarehouseOperation.STATUS_IN_PROGRESS
    operation.started_at = operation.started_at or now
    operation.save(update_fields=["done_qty", "status", "started_at", "updated_at"])
    from .client_movements import sync_client_movement_request_status
    from .reachtruck_bridge import sync_allocation_reachtruck_done

    sync_allocation_reachtruck_done(allocation)
    sync_client_movement_request_status(plan.client_movement_request_id, performed_by=actor)
    return allocation


@transaction.atomic
def open_prepared_box_for_allocation(
    *,
    allocation_id: int,
    box_scan: str,
    performed_by=None,
) -> FbsReplenishmentPreparedBox:
    _require_warehouse_writes()
    actor = _reachtruck_actor(performed_by)
    allocation = (
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related("line__plan")
        .get(pk=allocation_id)
    )
    plan = allocation.line.plan
    if not _uses_prepared_box_flow(plan):
        raise FbsReplenishmentError("Для задания не подготовлены постоянные FBS-короба.")
    if plan.assigned_to_id != actor.id:
        raise FbsReplenishmentError("Задание FBS не назначено этому водителю.")
    if allocation.status not in {
        FbsReplenishmentAllocation.STATUS_RESERVED,
        FbsReplenishmentAllocation.STATUS_IN_PROGRESS,
    }:
        raise FbsReplenishmentError("Товар по этому заданию уже собран.")
    prepared_box = (
        FbsReplenishmentPreparedBox.objects.select_for_update()
        .filter(plan=plan, box_code__iexact=str(box_scan or "").strip())
        .first()
    )
    if prepared_box is None:
        raise FbsReplenishmentError("QR не относится к напечатанным коробам этой заявки.")
    if prepared_box.status not in {
        FbsReplenishmentPreparedBox.STATUS_PRINTED,
        FbsReplenishmentPreparedBox.STATUS_FILLING,
    }:
        raise FbsReplenishmentError(
            f"Короб {prepared_box.box_code} уже закрыт или недоступен."
        )
    if prepared_box.status == FbsReplenishmentPreparedBox.STATUS_PRINTED:
        prepared_box.status = FbsReplenishmentPreparedBox.STATUS_FILLING
        prepared_box.save(update_fields=["status", "updated_at"])
    return prepared_box


@transaction.atomic
def record_fbs_movement_marking_scan(
    *,
    allocation_id: int,
    marking_scan: str,
    performed_by=None,
    prepared_box_id: int | None = None,
) -> WarehouseEvent:
    """Validate and audit one ChZ scan before counting the moved unit."""
    _require_warehouse_writes()
    actor = _reachtruck_actor(performed_by)
    allocation = (
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "line__plan__client_movement_request",
            "line__plan__warehouse_operation",
            "line__plan__staging_location",
            "line__sku_ref",
            "source_snapshot__container__current_location",
            "source_snapshot__location",
            "source_snapshot__sku_ref",
            "warehouse_reserve",
            "warehouse_task",
        )
        .get(pk=allocation_id)
    )
    plan = allocation.line.plan
    if plan.assigned_to_id != actor.id:
        raise FbsReplenishmentError("Задание FBS не назначено этому водителю.")
    if allocation.status not in {
        FbsReplenishmentAllocation.STATUS_RESERVED,
        FbsReplenishmentAllocation.STATUS_IN_PROGRESS,
    }:
        raise FbsReplenishmentError("Товар по этому заданию уже собран.")
    move_task = _movement_marking_task(allocation)
    movement_id = int(plan.client_movement_request_id or 0)

    raw_scan = str(marking_scan or "").strip()
    try:
        normalized = validate_import_marking_code(raw_scan)
    except MarkingCodeFormatError as exc:
        raise FbsReplenishmentError(f"Некорректный Честный знак: {exc}.") from exc
    if not normalized.startswith("01") or normalized[16:18] != "21":
        raise FbsReplenishmentError(
            "Отсканируйте полный Data Matrix Честного знака: "
            "01 + GTIN-14 + 21 + серийный номер."
        )
    identity = marking_code_identity(normalized)
    scanned_gtin = normalized[2:16]
    sku_id = int(allocation.line.sku_ref_id or allocation.source_snapshot.sku_ref_id or 0)
    if not sku_id:
        raise FbsReplenishmentError("У задания не найден SKU для проверки Честного знака.")
    barcode_values = {
        str(allocation.line.barcode or "").strip(),
        str(allocation.source_snapshot.barcode or "").strip(),
    }
    barcode_aliases = SKUBarcode.objects.filter(sku_id=sku_id)
    source_size = str(allocation.source_snapshot.size or "").strip()
    if source_size:
        barcode_aliases = barcode_aliases.filter(Q(size="") | Q(size__iexact=source_size))
    barcode_values.update(
        str(value or "").strip()
        for value in barcode_aliases.values_list("value", flat=True)
    )
    expected_gtins = {
        gtin
        for gtin in (_barcode_as_gtin14(value) for value in barcode_values)
        if gtin
    }
    if scanned_gtin not in expected_gtins:
        raise FbsReplenishmentError(
            "Честный знак относится к другому товару: GTIN Data Matrix "
            "не совпадает со ШК выбранного SKU. Единица не засчитана."
        )

    source_marking = str(allocation.source_snapshot.marking_code or "").strip()
    if source_marking:
        try:
            registered_source = validate_import_marking_code(source_marking)
        except MarkingCodeFormatError as exc:
            raise FbsReplenishmentError(
                "В исходном остатке записан некорректный КИЗ. "
                "Сначала проведите покодовую инвентаризацию короба."
            ) from exc
        if marking_code_identity(registered_source) != identity:
            raise FbsReplenishmentError(
                "Отсканирован Честный знак другой единицы. "
                "Возьмите КИЗ товара из указанного исходного короба."
            )

    duplicate_event = WarehouseEvent.objects.filter(
        agency_id=plan.agency_id,
        event_type=FBS_MOVEMENT_MARKING_EVENT_TYPE,
        payload__fbs_movement_id=movement_id,
        payload__marking_identity=identity,
    ).first()
    if duplicate_event is not None:
        raise FbsReplenishmentError(
            "ДУБЛЬ ЧЗ: этот Data Matrix уже отсканирован в текущем перемещении FBS. "
            "Повторный скан не засчитан."
        )
    scanned_for_allocation = WarehouseEvent.objects.filter(
        agency_id=plan.agency_id,
        event_type=FBS_MOVEMENT_MARKING_EVENT_TYPE,
        payload__allocation_id=allocation.id,
    ).count()
    if scanned_for_allocation >= int(allocation.qty_planned or 0):
        raise FbsReplenishmentError(
            "По заданию уже отсканировано требуемое количество Честных знаков."
        )

    balance_candidates = FbsStockBalance.objects.filter(
        agency_id=plan.agency_id,
    ).exclude(marking_code="").filter(_marking_lookup("marking_code", normalized))
    if any(
        marking_code_identity(value) == identity
        for value in balance_candidates.values_list("marking_code", flat=True)
    ):
        raise FbsReplenishmentError(
            "ДУБЛЬ ЧЗ: этот Data Matrix уже числится в остатках FBS. "
            "Повторный скан не засчитан."
        )

    other_source_candidates = (
        WarehouseStockSnapshot.objects.filter(
            agency_id=plan.agency_id,
            is_archived=False,
            qty__gt=0,
        )
        .exclude(pk=allocation.source_snapshot_id)
        .exclude(marking_code="")
        .filter(_marking_lookup("marking_code", normalized))
    )
    if any(
        marking_code_identity(value) == identity
        for value in other_source_candidates.values_list("marking_code", flat=True)
    ):
        raise FbsReplenishmentError(
            "Честный знак закреплен за другой складской единицей. "
            "Отсканируйте КИЗ из указанного исходного короба."
        )

    prepared_box = None
    if prepared_box_id:
        prepared_box = (
            FbsReplenishmentPreparedBox.objects.select_for_update(of=("self",))
            .select_related("physical_container__current_location")
            .filter(pk=prepared_box_id, plan=plan)
            .first()
        )
        if prepared_box is None:
            raise FbsReplenishmentError("Короб назначения не относится к этому заданию FBS.")

    destination_location = (
        prepared_box.physical_container.current_location
        if prepared_box is not None
        else plan.staging_location
    )
    now = timezone.now()
    return WarehouseEvent.objects.create(
        agency=plan.agency,
        event_type=FBS_MOVEMENT_MARKING_EVENT_TYPE,
        stock_context_type=CONTEXT_TYPE,
        stock_context_id=str(plan.id),
        container=(
            prepared_box.physical_container
            if prepared_box is not None
            else allocation.source_snapshot.container
        ),
        operation=plan.warehouse_operation,
        operation_task=allocation.warehouse_task,
        reserve=allocation.warehouse_reserve,
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(plan.id),
        from_location=allocation.source_snapshot.location,
        to_location=destination_location,
        from_zone_code=str(allocation.source_snapshot.zone_code or ""),
        to_zone_code=str(getattr(destination_location, "zone_code", "") or ""),
        qty=1,
        payload={
            "fbs_movement_id": movement_id,
            "fbs_plan_id": plan.id,
            "reachtruck_task_id": move_task.id,
            "allocation_id": allocation.id,
            "source_snapshot_id": allocation.source_snapshot_id,
            "prepared_box_id": int(prepared_box_id or 0),
            "product_barcode": str(allocation.line.barcode or "").strip(),
            "marking_code": normalized,
            "marking_identity": identity,
            "gtin": scanned_gtin,
        },
        performed_by=actor,
        performed_by_role="reachtruck",
        occurred_at=now,
    )


@transaction.atomic
def record_prepared_box_item_scan(
    *,
    allocation_id: int,
    prepared_box_id: int,
    item_scan: str,
    marking_scan: str = "",
    performed_by=None,
) -> FbsReplenishmentPreparedBoxItem:
    _require_warehouse_writes()
    actor = _reachtruck_actor(performed_by)
    allocation = (
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related("line__plan", "line")
        .get(pk=allocation_id)
    )
    plan = allocation.line.plan
    if plan.assigned_to_id != actor.id:
        raise FbsReplenishmentError("Задание FBS не назначено этому водителю.")
    if allocation.status not in {
        FbsReplenishmentAllocation.STATUS_RESERVED,
        FbsReplenishmentAllocation.STATUS_IN_PROGRESS,
    }:
        raise FbsReplenishmentError("Товар по этому заданию уже собран.")
    expected_barcode = str(allocation.line.barcode or "").strip()
    if not expected_barcode or not _normalize_scan(item_scan) == _normalize_scan(expected_barcode):
        raise FbsReplenishmentError(f"Ожидается ШК товара {expected_barcode}.")
    prepared_box = FbsReplenishmentPreparedBox.objects.select_for_update().get(
        pk=prepared_box_id,
        plan=plan,
    )
    if prepared_box.status != FbsReplenishmentPreparedBox.STATUS_FILLING:
        raise FbsReplenishmentError("Сначала отсканируйте открытый напечатанный короб.")
    scanned_for_allocation = int(
        FbsReplenishmentPreparedBoxItem.objects.filter(allocation=allocation).aggregate(
            total=Sum("qty_scanned")
        )["total"]
        or 0
    )
    if scanned_for_allocation >= int(allocation.qty_planned or 0):
        raise FbsReplenishmentError("По заданию уже отсканировано требуемое количество.")
    # Keep this guard in the write path as well as in the reachtruck UI so a
    # direct command cannot restore the retired movement-only ChZ requirement.
    if fbs_movement_marking_scan_required():
        if not str(marking_scan or "").strip():
            raise FbsReplenishmentError(
                "После ШК товара обязательно отсканируйте Data Matrix Честного знака."
            )
        record_fbs_movement_marking_scan(
            allocation_id=allocation.id,
            marking_scan=marking_scan,
            performed_by=actor,
            prepared_box_id=prepared_box.id,
        )
    item = (
        FbsReplenishmentPreparedBoxItem.objects.select_for_update()
        .filter(prepared_box=prepared_box, allocation=allocation)
        .first()
    )
    if item is None:
        item = FbsReplenishmentPreparedBoxItem.objects.create(
            prepared_box=prepared_box,
            allocation=allocation,
            qty_scanned=1,
        )
    else:
        item.qty_scanned = int(item.qty_scanned or 0) + 1
        item.save(update_fields=["qty_scanned", "updated_at"])
    prepared_box.scanned_qty = int(prepared_box.scanned_qty or 0) + 1
    prepared_box.save(update_fields=["scanned_qty", "updated_at"])
    if allocation.status == FbsReplenishmentAllocation.STATUS_RESERVED:
        allocation.status = FbsReplenishmentAllocation.STATUS_IN_PROGRESS
        allocation.save(update_fields=["status", "updated_at"])
    return item


@transaction.atomic
def close_prepared_box(
    *,
    prepared_box_id: int,
    box_scan: str,
    performed_by=None,
) -> FbsReplenishmentPreparedBox:
    _require_warehouse_writes()
    actor = _reachtruck_actor(performed_by)
    prepared_box = (
        FbsReplenishmentPreparedBox.objects.select_for_update(of=("self",))
        .select_related("plan__agency", "plan__warehouse_operation", "physical_container")
        .get(pk=prepared_box_id)
    )
    if prepared_box.plan.assigned_to_id not in {None, actor.id}:
        raise FbsReplenishmentError("План FBS назначен другому водителю.")
    if _normalize_scan(box_scan) != _normalize_scan(prepared_box.box_code):
        raise FbsReplenishmentError(f"Ожидается QR короба {prepared_box.box_code}.")
    if prepared_box.status in {
        FbsReplenishmentPreparedBox.STATUS_CLOSED,
        FbsReplenishmentPreparedBox.STATUS_PLACED,
    }:
        return prepared_box
    if prepared_box.status != FbsReplenishmentPreparedBox.STATUS_FILLING:
        raise FbsReplenishmentError("Пустой короб нельзя закрыть как заполненный.")
    item_qty = int(prepared_box.items.aggregate(total=Sum("qty_scanned"))["total"] or 0)
    if item_qty <= 0 or item_qty != int(prepared_box.scanned_qty or 0):
        raise FbsReplenishmentError("Состав короба не прошел сверку по отсканированному количеству.")
    now = timezone.now()
    prepared_box.status = FbsReplenishmentPreparedBox.STATUS_CLOSED
    prepared_box.closed_by = actor
    prepared_box.closed_at = now
    prepared_box.save(update_fields=["status", "closed_by", "closed_at", "updated_at"])
    WarehouseEvent.objects.create(
        agency=prepared_box.agency,
        event_type="fbs_replenishment_box_closed",
        stock_context_type=CONTEXT_TYPE,
        stock_context_id=str(prepared_box.plan_id),
        container=prepared_box.physical_container,
        operation=prepared_box.plan.warehouse_operation,
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(prepared_box.plan_id),
        from_location=prepared_box.physical_container.current_location,
        to_location=prepared_box.physical_container.current_location,
        from_zone_code=str(
            getattr(prepared_box.physical_container.current_location, "zone_code", "") or ""
        ),
        to_zone_code=str(
            getattr(prepared_box.physical_container.current_location, "zone_code", "") or ""
        ),
        qty=item_qty,
        payload={
            "prepared_box_id": prepared_box.id,
            "prepared_box_code": prepared_box.box_code,
            "scanned_qty": item_qty,
        },
        performed_by=actor,
        performed_by_role="reachtruck",
        occurred_at=now,
    )
    return prepared_box


def _archive_unused_prepared_boxes(
    *,
    plan: FbsReplenishmentPlan,
) -> tuple[FbsReplenishmentPreparedBox, ...]:
    unused = list(
        FbsReplenishmentPreparedBox.objects.select_for_update()
        .select_related("physical_container")
        .filter(
            plan=plan,
            status=FbsReplenishmentPreparedBox.STATUS_PRINTED,
            scanned_qty=0,
        )
        .order_by("sequence_no", "id")
    )
    now = timezone.now()
    for prepared_box in unused:
        prepared_box.status = FbsReplenishmentPreparedBox.STATUS_UNUSED
        prepared_box.closed_at = now
        prepared_box.save(update_fields=["status", "closed_at", "updated_at"])
        physical_box = prepared_box.physical_container
        physical_box.status = WarehouseContainer.STATUS_ARCHIVED
        physical_box.save(update_fields=["status", "updated_at"])
    return tuple(unused)


@transaction.atomic
def stage_prepared_box_allocation(
    *,
    allocation_id: int,
    performed_by=None,
) -> FbsReplenishmentAllocation:
    """Consumes the general-stock reserve after every required unit scan was recorded."""
    _require_warehouse_writes()
    actor = _reachtruck_actor(performed_by)
    allocation = (
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "line__plan__agency",
            "line__plan__staging_location",
            "line__plan__warehouse_operation",
            "source_snapshot__container",
            "source_snapshot__location",
            "warehouse_task",
        )
        .get(pk=allocation_id)
    )
    if allocation.status == FbsReplenishmentAllocation.STATUS_STAGED:
        return allocation
    plan = allocation.line.plan
    if not _uses_prepared_box_flow(plan):
        raise FbsReplenishmentError("Это задание не использует печатные FBS-короба.")
    if plan.assigned_to_id != actor.id:
        raise FbsReplenishmentError("Задание FBS не назначено этому водителю.")
    scanned_qty = int(
        FbsReplenishmentPreparedBoxItem.objects.filter(allocation=allocation).aggregate(
            total=Sum("qty_scanned")
        )["total"]
        or 0
    )
    if scanned_qty != int(allocation.qty_planned or 0):
        raise FbsReplenishmentError(
            f"Сверка товара не пройдена: отсканировано {scanned_qty} из {allocation.qty_planned}."
        )
    if FbsReplenishmentPreparedBoxItem.objects.filter(
        allocation=allocation,
        prepared_box__status__in=(
            FbsReplenishmentPreparedBox.STATUS_PRINTED,
            FbsReplenishmentPreparedBox.STATUS_UNUSED,
        ),
    ).exists():
        raise FbsReplenishmentError("Товар должен находиться в открытом или закрытом коробе.")
    snapshot = WarehouseStockSnapshot.objects.select_for_update(of=("self",)).get(
        pk=allocation.source_snapshot_id
    )
    reserve = WarehouseReserve.objects.select_for_update().get(
        pk=allocation.warehouse_reserve_id
    )
    qty = int(allocation.qty_planned or 0)
    if int(snapshot.qty or 0) < qty or int(snapshot.other_reserved_qty or 0) < qty:
        raise FbsReplenishmentError("Общий остаток изменился и больше не покрывает резерв FBS.")
    now = timezone.now()
    snapshot.qty = int(snapshot.qty or 0) - qty
    snapshot.other_reserved_qty = int(snapshot.other_reserved_qty or 0) - qty
    snapshot.snapshot_version = int(snapshot.snapshot_version or 0) + 1
    if snapshot.qty == 0:
        snapshot.available_qty = 0
        snapshot.is_archived = True
    if snapshot.active_operation_id == plan.warehouse_operation_id:
        snapshot.active_operation = None
        snapshot.active_operation_type = ""
    reserve.qty_satisfied = reserve.qty_reserved
    reserve.status = WarehouseReserve.STATUS_SATISFIED
    reserve.save(update_fields=["qty_satisfied", "status", "updated_at"])
    box_rows = list(
        FbsReplenishmentPreparedBoxItem.objects.filter(allocation=allocation)
        .select_related("prepared_box")
        .order_by("prepared_box__sequence_no", "id")
    )
    event = WarehouseEvent.objects.create(
        agency=plan.agency,
        event_type="fbs_replenishment_collected",
        stock_context_type=CONTEXT_TYPE,
        stock_context_id=str(plan.id),
        container=snapshot.container,
        operation=plan.warehouse_operation,
        operation_task=allocation.warehouse_task,
        reserve=reserve,
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(plan.id),
        from_location=snapshot.location,
        to_location=plan.staging_location,
        from_zone_code=str(snapshot.zone_code or ""),
        to_zone_code=str(plan.staging_location.zone_code or ""),
        qty=qty,
        payload={
            "allocation_id": allocation.id,
            "source_snapshot_id": snapshot.id,
            "prepared_boxes": [
                {
                    "id": row.prepared_box_id,
                    "code": row.prepared_box.box_code,
                    "qty": int(row.qty_scanned or 0),
                }
                for row in box_rows
            ],
            "mode": plan.mode,
            "marking_code": snapshot.marking_code,
            "lot_code": _snapshot_lot_code(snapshot),
            "expiry_date": (
                _snapshot_expiry_date(snapshot).isoformat()
                if _snapshot_expiry_date(snapshot)
                else ""
            ),
        },
        performed_by=actor,
        performed_by_role="reachtruck",
        occurred_at=now,
    )
    snapshot.last_event = event
    snapshot.save(
        update_fields=[
            "qty",
            "available_qty",
            "other_reserved_qty",
            "snapshot_version",
            "active_operation",
            "active_operation_type",
            "is_archived",
            "last_event",
            "updated_at",
        ]
    )
    allocation.qty_staged = qty
    allocation.status = FbsReplenishmentAllocation.STATUS_STAGED
    allocation.staged_by = actor
    allocation.staged_at = now
    allocation.save(
        update_fields=["qty_staged", "status", "staged_by", "staged_at", "updated_at"]
    )
    line = allocation.line
    line.status = FbsReplenishmentLine.STATUS_IN_PROGRESS
    line.save(update_fields=["status", "updated_at"])
    task = allocation.warehouse_task
    task.qty_done = int(
        task.fbs_replenishment_allocations.aggregate(total=Sum("qty_staged"))["total"] or 0
    )
    task.status = WarehouseOperationTask.STATUS_DONE
    task.started_at = task.started_at or now
    task.completed_at = now
    task.save(update_fields=["qty_done", "status", "started_at", "completed_at", "updated_at"])
    all_staged = not FbsReplenishmentAllocation.objects.filter(line__plan=plan).exclude(
        status__in=(
            FbsReplenishmentAllocation.STATUS_STAGED,
            FbsReplenishmentAllocation.STATUS_DONE,
        )
    ).exists()
    plan.status = FbsReplenishmentPlan.STATUS_IN_PROGRESS
    plan.started_at = plan.started_at or now
    plan.save(update_fields=["status", "started_at", "updated_at"])
    operation = plan.warehouse_operation
    operation.done_qty = int(
        FbsReplenishmentAllocation.objects.filter(line__plan=plan).aggregate(
            total=Sum("qty_staged")
        )["total"]
        or 0
    )
    operation.status = WarehouseOperation.STATUS_IN_PROGRESS
    operation.started_at = operation.started_at or now
    operation.save(update_fields=["done_qty", "status", "started_at", "updated_at"])
    from .client_movements import sync_client_movement_request_status
    from .reachtruck_bridge import (
        sync_allocation_reachtruck_done,
        sync_plan_reachtruck_box_closure,
        sync_plan_reachtruck_prepared_box_placements,
    )

    sync_allocation_reachtruck_done(allocation)
    if all_staged:
        if FbsReplenishmentPreparedBox.objects.filter(
            plan=plan,
            status=FbsReplenishmentPreparedBox.STATUS_FILLING,
            scanned_qty__gt=0,
        ).exists():
            sync_plan_reachtruck_box_closure(plan)
        else:
            _archive_unused_prepared_boxes(plan=plan)
            sync_plan_reachtruck_prepared_box_placements(plan)
    sync_client_movement_request_status(plan.client_movement_request_id, performed_by=actor)
    return allocation


@transaction.atomic
def finalize_prepared_box_plan(
    *,
    plan_id: int,
    performed_by=None,
) -> tuple[FbsReplenishmentPreparedBox, ...]:
    _require_warehouse_writes()
    actor = _reachtruck_actor(performed_by)
    plan = FbsReplenishmentPlan.objects.select_for_update(of=("self",)).get(pk=plan_id)
    if plan.assigned_to_id not in {None, actor.id}:
        raise FbsReplenishmentError("План FBS назначен другому водителю.")
    if FbsReplenishmentAllocation.objects.filter(line__plan=plan).exclude(
        status__in=(
            FbsReplenishmentAllocation.STATUS_STAGED,
            FbsReplenishmentAllocation.STATUS_DONE,
        )
    ).exists():
        raise FbsReplenishmentError("Сначала соберите весь товар по заявке.")
    if FbsReplenishmentPreparedBox.objects.filter(
        plan=plan,
        status=FbsReplenishmentPreparedBox.STATUS_FILLING,
    ).exists():
        raise FbsReplenishmentError("Закройте все заполненные короба их повторным сканированием.")
    _archive_unused_prepared_boxes(plan=plan)
    closed_boxes = tuple(
        FbsReplenishmentPreparedBox.objects.filter(
            plan=plan,
            status=FbsReplenishmentPreparedBox.STATUS_CLOSED,
            scanned_qty__gt=0,
        ).order_by("sequence_no", "id")
    )
    if not closed_boxes:
        raise FbsReplenishmentError("Не найдено ни одного заполненного короба.")
    from .reachtruck_bridge import sync_plan_reachtruck_prepared_box_placements

    sync_plan_reachtruck_prepared_box_placements(plan)
    return closed_boxes


@transaction.atomic
def pack_staged_item_plan(
    *,
    plan_id: int,
    packed_by=None,
    staging_container_scan: str,
    target_box_id: int | None = None,
    target_pallet_id: int | None = None,
) -> FbsBox:
    """Creates a labeled FBS box in staging without booking FBS stock yet."""
    _require_warehouse_writes()
    actor = _storekeeper_actor(packed_by)
    plan = (
        FbsReplenishmentPlan.objects.select_for_update(of=("self",))
        .select_related(
            "agency",
            "target_cell__location",
            "target_pallet",
            "target_box",
            "staging_location",
            "warehouse_operation",
        )
        .get(pk=plan_id)
    )
    if plan.target_box_id and plan.status in {
        FbsReplenishmentPlan.STATUS_IN_PROGRESS,
        FbsReplenishmentPlan.STATUS_DONE,
    }:
        return plan.target_box
    if target_box_id or target_pallet_id:
        raise FbsReplenishmentError(
            "Место и существующий короб заранее не выбираются. Создайте новый QR-короб."
        )
    if not _requires_storekeeper_packing(plan):
        raise FbsReplenishmentError("Этот план не ожидает укладки штучного товара.")
    if plan.status != FbsReplenishmentPlan.STATUS_AWAITING_PACK:
        raise FbsReplenishmentError("Сначала ричтрак должен передать весь товар кладовщику.")
    expected_container_code = movement_staging_container_code(
        plan.client_movement_request
    )
    if _normalize_scan(staging_container_scan) != _normalize_scan(
        expected_container_code
    ):
        raise FbsReplenishmentError(
            f"Отсканируйте временный короб заявки {expected_container_code}."
        )
    allocations = list(
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "line",
            "source_snapshot__container",
            "source_snapshot__location",
            "source_snapshot__sku_ref",
            "warehouse_reserve",
            "warehouse_task",
        )
        .filter(line__plan=plan)
        .order_by("id")
    )
    if not allocations or any(
        allocation.status != FbsReplenishmentAllocation.STATUS_STAGED
        or int(allocation.qty_staged or 0) != int(allocation.qty_planned or 0)
        for allocation in allocations
    ):
        raise FbsReplenishmentError("Не весь товар передан в зону подготовки FBS.")

    target_box = _create_staged_fbs_box(plan=plan, actor=actor)

    plan.target_box = target_box
    plan.target_pallet = target_box.pallet
    plan.target_cell = target_box.pallet.cell
    plan.status = FbsReplenishmentPlan.STATUS_IN_PROGRESS
    plan.assigned_to = None
    plan.completed_at = None
    plan.full_clean()
    plan.save(
        update_fields=[
            "target_cell",
            "target_pallet",
            "target_box",
            "status",
            "assigned_to",
            "completed_at",
            "updated_at",
        ]
    )
    FbsReplenishmentLine.objects.filter(plan=plan).update(
        target_box=target_box,
        updated_at=timezone.now(),
    )
    FbsReplenishmentAllocation.objects.filter(line__plan=plan).update(
        target_box=target_box,
        updated_at=timezone.now(),
    )
    now = timezone.now()
    WarehouseEvent.objects.create(
        agency=plan.agency,
        event_type="fbs_replenishment_box_prepared",
        stock_context_type=CONTEXT_TYPE,
        stock_context_id=str(plan.id),
        container=target_box.source_container,
        operation=plan.warehouse_operation,
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(plan.id),
        from_location=plan.staging_location,
        to_location=plan.staging_location,
        from_zone_code=str(plan.staging_location.zone_code or ""),
        to_zone_code=str(plan.staging_location.zone_code or ""),
        qty=sum(int(allocation.qty_staged or 0) for allocation in allocations),
        payload={
            "target_box_id": target_box.id,
            "target_box_code": target_box.box_code,
            "awaiting_reachtruck_placement": True,
        },
        performed_by=actor,
        performed_by_role="storekeeper",
        occurred_at=now,
    )
    if plan.client_movement_request_id:
        archive_movement_staging_container(movement=plan.client_movement_request)
        from .client_movements import sync_client_movement_request_status
        from .reachtruck_bridge import sync_plan_reachtruck_placement

        sync_plan_reachtruck_placement(plan)
        sync_client_movement_request_status(
            plan.client_movement_request_id,
            performed_by=actor,
        )
    return target_box


def _reachtruck_actor(user):
    actor = _authenticated_user(user)
    if actor is None:
        raise FbsReplenishmentError("Для размещения FBS-короба требуется водитель погрузчика.")
    if getattr(actor, "is_superuser", False):
        return actor
    from employees.models import Employee

    role = str(
        Employee.objects.filter(user=actor, is_active=True)
        .order_by("id")
        .values_list("role", flat=True)
        .first()
        or ""
    )
    if role not in {"reachtruck_driver", "head_manager", "director", "admin"}:
        raise FbsReplenishmentError("Разместить FBS-короб может только водитель погрузчика.")
    return actor


def _destination_pallet_for_prepared_box(
    *,
    prepared_box: FbsReplenishmentPreparedBox,
    destination_scan: str,
) -> tuple[WarehouseLocation, FbsPallet]:
    destination = _resolve_active_os_location(destination_scan)
    destination_cell, _created = FbsStorageCell.objects.get_or_create(
        location=destination,
        defaults={
            "cell_code": f"FBS@{os_location_code(row=destination.row_no, section=destination.section_no, tier=destination.tier_no, cell=destination.cell_no)}",
            "purpose": FbsStorageCell.PURPOSE_FLEX,
            "client_cluster": prepared_box.agency_id,
            "is_active": True,
        },
    )
    destination_cell = FbsStorageCell.objects.select_for_update().get(pk=destination_cell.pk)
    if not destination_cell.is_active:
        raise FbsReplenishmentError("Выбранная ячейка выключена для FBS.")
    pallet = (
        FbsPallet.objects.select_for_update(of=("self",))
        .select_related("warehouse_container", "cell__location")
        .filter(
            cell=destination_cell,
            status__in=(FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE),
        )
        .first()
    )
    if (
        pallet is not None
        and pallet.id != prepared_box.plan.target_pallet_id
    ):
        from .storage import release_unplaced_os_reservation

        if release_unplaced_os_reservation(pallet=pallet):
            pallet = None
    if pallet is not None:
        if pallet.agency_id != prepared_box.agency_id:
            raise FbsReplenishmentError(
                _occupied_fbs_destination_error(
                    destination=destination,
                    pallet=pallet,
                )
            )
        return destination, pallet

    if destination_cell.client_cluster != prepared_box.agency_id:
        destination_cell.client_cluster = prepared_box.agency_id
        destination_cell.save(update_fields=["client_cluster", "updated_at"])
    placeholder = (
        FbsPallet.objects.select_for_update(of=("self",))
        .select_related("warehouse_container", "cell__location")
        .filter(
            pk=prepared_box.plan.target_pallet_id,
            agency=prepared_box.agency,
            status__in=(FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE),
        )
        .first()
    )
    placeholder_is_dedicated = bool(
        placeholder
        and not FbsBox.objects.filter(
            pallet=placeholder,
            status__in=(FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE),
        ).exists()
        and not FbsReplenishmentPlan.objects.filter(
            target_pallet=placeholder,
            status__in=(
                FbsReplenishmentPlan.STATUS_PROPOSED,
                FbsReplenishmentPlan.STATUS_CONFIRMED,
                FbsReplenishmentPlan.STATUS_IN_PROGRESS,
                FbsReplenishmentPlan.STATUS_AWAITING_PACK,
            ),
        ).exclude(pk=prepared_box.plan_id).exists()
    )
    # A placeholder cell exists only to hold plan relations until the driver
    # scans the real address ("место определяется сканом ричтрака").  It was
    # released only for a placeholder serving a single plan, so a pallet shared
    # by several box plans kept the virtual cell for good: the stock then sits
    # at an address that does not exist, and free relocation cannot move it,
    # because a virtual zone is not an allowed source.  A virtual cell is never
    # a legitimate final place, so release it as soon as a real destination is
    # known, whether or not the placeholder is dedicated.
    placeholder_is_unplaced = bool(
        placeholder
        and is_virtual_fbs_plan_location(getattr(placeholder.cell, "location", None))
    )
    if placeholder_is_dedicated or placeholder_is_unplaced:
        placeholder.cell = destination_cell
        placeholder.full_clean()
        placeholder.save(update_fields=["cell", "updated_at"])
        if placeholder.warehouse_container_id:
            placeholder.warehouse_container.current_location = destination
            placeholder.warehouse_container.save(
                update_fields=["current_location", "updated_at"]
            )
        else:
            placeholder.warehouse_container = WarehouseContainer.objects.create(
                agency=prepared_box.agency,
                container_type=WarehouseContainer.TYPE_PALLET,
                container_code=placeholder.pallet_code,
                current_location=destination,
                status=WarehouseContainer.STATUS_ACTIVE,
                source_context_type=FBS_STORAGE_CONTEXT_TYPE,
                source_context_id=placeholder.pallet_code,
            )
            placeholder.save(update_fields=["warehouse_container", "updated_at"])
        return destination, placeholder
    from .storage import create_fbs_pallet

    try:
        pallet = create_fbs_pallet(
            agency=prepared_box.agency,
            cell=destination_cell,
            pallet_code=f"FBS-PAL-{prepared_box.agency_id}-{uuid4().hex[:10].upper()}",
        )
    except FbsStorageError as exc:
        raise FbsReplenishmentError(str(exc)) from exc
    return destination, pallet


def _refresh_prepared_box_plan_completion(
    *,
    plan: FbsReplenishmentPlan,
    actor,
    now,
) -> None:
    allocations = list(
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related("line", "warehouse_task")
        .filter(line__plan=plan)
        .order_by("id")
    )
    line_ids = set()
    for allocation in allocations:
        moved_qty = int(
            FbsReplenishmentPreparedBoxItem.objects.filter(allocation=allocation).aggregate(
                total=Sum("qty_placed")
            )["total"]
            or 0
        )
        allocation.qty_moved = moved_qty
        allocation.status = (
            FbsReplenishmentAllocation.STATUS_DONE
            if moved_qty == int(allocation.qty_planned or 0)
            else FbsReplenishmentAllocation.STATUS_STAGED
        )
        allocation.save(update_fields=["qty_moved", "status", "updated_at"])
        line_ids.add(allocation.line_id)
    for line in FbsReplenishmentLine.objects.select_for_update().filter(id__in=line_ids):
        line.qty_moved = int(line.allocations.aggregate(total=Sum("qty_moved"))["total"] or 0)
        line.status = (
            FbsReplenishmentLine.STATUS_DONE
            if line.qty_moved == int(line.qty_planned or 0)
            else FbsReplenishmentLine.STATUS_IN_PROGRESS
        )
        line.save(update_fields=["qty_moved", "status", "updated_at"])
    moved_qty = sum(int(allocation.qty_moved or 0) for allocation in allocations)
    pending_box_exists = FbsReplenishmentPreparedBox.objects.filter(
        plan=plan,
        status__in=(
            FbsReplenishmentPreparedBox.STATUS_PRINTED,
            FbsReplenishmentPreparedBox.STATUS_FILLING,
            FbsReplenishmentPreparedBox.STATUS_CLOSED,
        ),
    ).exists()
    complete = (
        bool(allocations)
        and moved_qty == int(plan.planned_qty or 0)
        and not pending_box_exists
    )
    plan.moved_qty = moved_qty
    plan.status = (
        FbsReplenishmentPlan.STATUS_DONE
        if complete
        else FbsReplenishmentPlan.STATUS_IN_PROGRESS
    )
    plan.completed_at = now if complete else None
    plan.save(update_fields=["moved_qty", "status", "completed_at", "updated_at"])
    operation = plan.warehouse_operation
    operation.done_qty = moved_qty
    operation.status = (
        WarehouseOperation.STATUS_DONE
        if complete
        else WarehouseOperation.STATUS_IN_PROGRESS
    )
    operation.completed_at = now if complete else None
    operation.save(update_fields=["done_qty", "status", "completed_at", "updated_at"])
    if plan.client_movement_request_id:
        from .client_movements import sync_client_movement_request_status

        sync_client_movement_request_status(
            plan.client_movement_request_id,
            performed_by=actor,
        )


@transaction.atomic
def complete_prepared_box_placement(
    *,
    prepared_box_id: int,
    box_scan: str,
    destination_scan: str,
    performed_by=None,
) -> FbsBox:
    """Places one non-empty permanent box and books only its scanned contents."""
    _require_warehouse_writes()
    actor = _reachtruck_actor(performed_by)
    prepared_box = (
        FbsReplenishmentPreparedBox.objects.select_for_update(of=("self",))
        .select_related(
            "agency",
            "plan__warehouse_operation",
            "plan__staging_location",
            "physical_container",
            "fbs_box",
        )
        .get(pk=prepared_box_id)
    )
    if _normalize_scan(box_scan) != _normalize_scan(prepared_box.box_code):
        raise FbsReplenishmentError(f"Ожидается QR короба {prepared_box.box_code}.")
    if prepared_box.status == FbsReplenishmentPreparedBox.STATUS_PLACED:
        return prepared_box.fbs_box
    if prepared_box.status != FbsReplenishmentPreparedBox.STATUS_CLOSED:
        raise FbsReplenishmentError("Разместить можно только закрытый непустой короб.")
    item_rows = list(
        FbsReplenishmentPreparedBoxItem.objects.select_for_update(of=("self",))
        .select_related(
            "allocation__line__plan",
            "allocation__source_snapshot__sku_ref",
            "allocation__warehouse_task",
            "allocation__warehouse_reserve",
        )
        .filter(prepared_box=prepared_box)
        .order_by("id")
    )
    if not item_rows or sum(int(row.qty_scanned or 0) for row in item_rows) != int(
        prepared_box.scanned_qty or 0
    ):
        raise FbsReplenishmentError("Состав короба не прошел сверку перед размещением.")
    if any(int(row.qty_placed or 0) for row in item_rows):
        raise FbsReplenishmentError("По коробу обнаружена незавершенная складская проводка.")
    destination, pallet = _destination_pallet_for_prepared_box(
        prepared_box=prepared_box,
        destination_scan=destination_scan,
    )
    from .storage import create_fbs_box

    try:
        target_box = create_fbs_box(
            agency=prepared_box.agency,
            pallet=pallet,
            box_code=prepared_box.box_code,
            source_container=prepared_box.physical_container,
        )
    except FbsStorageError as exc:
        raise FbsReplenishmentError(str(exc)) from exc
    physical_box = WarehouseContainer.objects.select_for_update().get(
        pk=prepared_box.physical_container_id
    )
    physical_box.current_location = destination
    physical_box.parent_container = pallet.warehouse_container
    physical_box.status = WarehouseContainer.STATUS_ACTIVE
    physical_box.source_context_type = FBS_PLACEMENT_CONTEXT_TYPE
    physical_box.source_context_id = str(prepared_box.plan_id)
    physical_box.save(
        update_fields=[
            "current_location",
            "parent_container",
            "status",
            "source_context_type",
            "source_context_id",
            "updated_at",
        ]
    )
    now = timezone.now()
    for item in item_rows:
        allocation = item.allocation
        snapshot = allocation.source_snapshot
        balance = _get_or_create_balance_for_box(
            target_box=target_box,
            allocation=allocation,
            snapshot=snapshot,
        )
        qty = int(item.qty_scanned or 0)
        pending_confirmation = _credit_replenishment_balance(
            balance,
            plan=prepared_box.plan,
            qty=qty,
        )
        event = WarehouseEvent.objects.create(
            agency=prepared_box.agency,
            event_type="fbs_replenishment_completed",
            stock_context_type=CONTEXT_TYPE,
            stock_context_id=str(prepared_box.plan_id),
            container=physical_box,
            operation=prepared_box.plan.warehouse_operation,
            operation_task=allocation.warehouse_task,
            reserve=allocation.warehouse_reserve,
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(prepared_box.plan_id),
            from_location=prepared_box.plan.staging_location,
            to_location=destination,
            from_zone_code=str(prepared_box.plan.staging_location.zone_code or ""),
            to_zone_code="OS",
            qty=qty,
            payload={
                "allocation_id": allocation.id,
                "source_snapshot_id": snapshot.id,
                "prepared_box_id": prepared_box.id,
                "target_balance_id": balance.id,
                "target_box_id": target_box.id,
                "target_box_code": target_box.box_code,
                "destination_scan": os_location_code(
                    row=destination.row_no,
                    section=destination.section_no,
                    tier=destination.tier_no,
                    cell=destination.cell_no,
                ),
                "placed_by_reachtruck": True,
                "marking_code": snapshot.marking_code,
                "lot_code": _snapshot_lot_code(snapshot),
                "expiry_date": (
                    _snapshot_expiry_date(snapshot).isoformat()
                    if _snapshot_expiry_date(snapshot)
                    else ""
                ),
                **_pending_confirmation_payload(
                    pending=pending_confirmation,
                    qty=qty,
                ),
            },
            performed_by=actor,
            performed_by_role="reachtruck",
            occurred_at=now,
        )
        item.qty_placed = qty
        item.target_balance = balance
        item.warehouse_event = event
        item.save(
            update_fields=[
                "qty_placed",
                "target_balance",
                "warehouse_event",
                "updated_at",
            ]
        )
    target_box.status = FbsBox.STATUS_ACTIVE
    target_box.save(update_fields=["status", "updated_at"])
    pallet.status = FbsPallet.STATUS_ACTIVE
    pallet.save(update_fields=["status", "updated_at"])
    prepared_box.fbs_box = target_box
    prepared_box.status = FbsReplenishmentPreparedBox.STATUS_PLACED
    prepared_box.placed_qty = prepared_box.scanned_qty
    prepared_box.placed_by = actor
    prepared_box.placed_at = now
    prepared_box.save(
        update_fields=[
            "fbs_box",
            "status",
            "placed_qty",
            "placed_by",
            "placed_at",
            "updated_at",
        ]
    )
    _refresh_prepared_box_plan_completion(
        plan=prepared_box.plan,
        actor=actor,
        now=now,
    )
    return target_box


@transaction.atomic
def complete_staged_item_plan_placement(
    *,
    plan_id: int,
    target_box_scan: str,
    destination_scan: str,
    performed_by=None,
) -> FbsBox:
    """Binds a prepared box to a free OS cell and only then books FBS stock."""
    _require_warehouse_writes()
    actor = _reachtruck_actor(performed_by)
    plan = (
        FbsReplenishmentPlan.objects.select_for_update(of=("self",))
        .select_related(
            "agency",
            "client_movement_request",
            "target_box__source_container",
            "target_pallet__warehouse_container",
            "target_cell__location",
            "staging_location",
            "warehouse_operation",
        )
        .get(pk=plan_id)
    )
    if plan.mode != FbsReplenishmentPlan.MODE_ITEM or not plan.target_box_id:
        raise FbsReplenishmentError("Для размещения не найден подготовленный FBS-короб.")
    if plan.status == FbsReplenishmentPlan.STATUS_DONE:
        return plan.target_box
    if plan.status != FbsReplenishmentPlan.STATUS_IN_PROGRESS:
        raise FbsReplenishmentError("FBS-короб еще не подготовлен кладовщиком.")
    if _normalize_scan(target_box_scan) != _normalize_scan(plan.target_box.box_code):
        raise FbsReplenishmentError(
            f"Отсканируйте QR подготовленного короба {plan.target_box.box_code}."
        )
    allocations = list(
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "line",
            "source_snapshot__container",
            "source_snapshot__sku_ref",
            "warehouse_reserve",
            "warehouse_task",
            "target_box",
        )
        .filter(line__plan=plan)
        .order_by("id")
    )
    staged_flow = bool(allocations) and all(
        allocation.status == FbsReplenishmentAllocation.STATUS_STAGED
        and int(allocation.qty_staged or 0) == int(allocation.qty_planned or 0)
        for allocation in allocations
    )
    legacy_flow = bool(allocations) and all(
        allocation.status == FbsReplenishmentAllocation.STATUS_DONE
        and int(allocation.qty_moved or 0) == int(allocation.qty_planned or 0)
        for allocation in allocations
    )
    if not staged_flow and not legacy_flow:
        raise FbsReplenishmentError("Не весь товар подготовлен для размещения FBS-короба.")

    destination = _bind_box_to_scanned_os_location(
        plan=plan,
        target_box=plan.target_box,
        destination_scan=destination_scan,
    )
    plan.refresh_from_db()
    if plan.assigned_to_id != actor.id:
        plan.assigned_to = actor
        plan.save(update_fields=["assigned_to", "updated_at"])
    target_box = FbsBox.objects.select_for_update().get(pk=plan.target_box_id)
    _validate_destination(plan, target_box)
    from .inventory import assert_box_unlocked

    assert_box_unlocked(target_box.id, for_execution=True)
    operation = plan.warehouse_operation
    operation.destination_location = destination
    operation.destination_zone_code = "OS"
    operation.status = WarehouseOperation.STATUS_IN_PROGRESS
    operation.completed_at = None
    operation.save(
        update_fields=[
            "destination_location",
            "destination_zone_code",
            "status",
            "completed_at",
            "updated_at",
        ]
    )
    now = timezone.now()
    if staged_flow:
        for allocation in allocations:
            allocation.target_box = target_box
            snapshot = allocation.source_snapshot
            balance = _get_or_create_balance(allocation=allocation, snapshot=snapshot)
            qty = int(allocation.qty_staged or 0)
            pending_confirmation = _credit_replenishment_balance(
                balance,
                plan=plan,
                qty=qty,
            )
            event = WarehouseEvent.objects.create(
                agency=plan.agency,
                event_type="fbs_replenishment_completed",
                stock_context_type=CONTEXT_TYPE,
                stock_context_id=str(plan.id),
                container=target_box.source_container,
                operation=operation,
                operation_task=allocation.warehouse_task,
                reserve=allocation.warehouse_reserve,
                source_document_type=SOURCE_DOCUMENT_TYPE,
                source_document_id=str(plan.id),
                from_location=plan.staging_location,
                to_location=destination,
                from_zone_code=str(plan.staging_location.zone_code or ""),
                to_zone_code="OS",
                qty=qty,
                payload={
                    "allocation_id": allocation.id,
                    "source_snapshot_id": snapshot.id,
                    "target_balance_id": balance.id,
                    "target_box_id": target_box.id,
                    "target_box_code": target_box.box_code,
                    "destination_scan": os_location_code(
                        row=destination.row_no,
                        section=destination.section_no,
                        tier=destination.tier_no,
                        cell=destination.cell_no,
                    ),
                    "placed_by_reachtruck": True,
                    "marking_code": snapshot.marking_code,
                    "lot_code": _snapshot_lot_code(snapshot),
                    "expiry_date": (
                        _snapshot_expiry_date(snapshot).isoformat()
                        if _snapshot_expiry_date(snapshot)
                        else ""
                    ),
                    **_pending_confirmation_payload(
                        pending=pending_confirmation,
                        qty=qty,
                    ),
                },
                performed_by=actor,
                performed_by_role="reachtruck",
                occurred_at=now,
            )
            allocation.qty_moved = allocation.qty_staged
            allocation.status = FbsReplenishmentAllocation.STATUS_DONE
            allocation.save(
                update_fields=["target_box", "qty_moved", "status", "updated_at"]
            )
            FbsStockMovement.objects.create(
                allocation=allocation,
                source_snapshot=snapshot,
                target_balance=balance,
                warehouse_event=event,
                qty=qty,
                performed_by=actor,
                occurred_at=now,
            )
            _refresh_completion_status(allocation, now, actor=actor)
    else:
        plan.assigned_to = actor
        plan.status = FbsReplenishmentPlan.STATUS_DONE
        plan.completed_at = now
        plan.save(update_fields=["assigned_to", "status", "completed_at", "updated_at"])
        operation.status = WarehouseOperation.STATUS_DONE
        operation.completed_at = now
        operation.save(update_fields=["status", "completed_at", "updated_at"])
        target_box.status = FbsBox.STATUS_ACTIVE
        target_box.save(update_fields=["status", "updated_at"])
        plan.target_pallet.status = FbsPallet.STATUS_ACTIVE
        plan.target_pallet.save(update_fields=["status", "updated_at"])
        WarehouseEvent.objects.create(
            agency=plan.agency,
            event_type="fbs_box_placement_completed",
            stock_context_type=CONTEXT_TYPE,
            stock_context_id=str(plan.id),
            container=target_box.source_container,
            operation=operation,
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(plan.id),
            from_location=plan.staging_location,
            to_location=destination,
            from_zone_code=str(plan.staging_location.zone_code or ""),
            to_zone_code="OS",
            qty=int(plan.moved_qty or 0),
            payload={
                "target_box_id": target_box.id,
                "target_box_code": target_box.box_code,
                "legacy_balance_retained": True,
            },
            performed_by=actor,
            performed_by_role="reachtruck",
            occurred_at=now,
        )
        from .client_movements import sync_client_movement_request_status

        sync_client_movement_request_status(
            plan.client_movement_request_id,
            performed_by=actor,
        )
    if plan.client_movement_request_id:
        archive_movement_staging_container(movement=plan.client_movement_request)
    return target_box


@transaction.atomic
def reopen_completed_item_plan_for_placement(
    *,
    plan_id: int,
    reopened_by=None,
) -> FbsReplenishmentPlan:
    """Queues a placement-only task for a box finalized by the previous flow."""
    _require_warehouse_writes()
    actor = _storekeeper_actor(reopened_by)
    plan = (
        FbsReplenishmentPlan.objects.select_for_update(of=("self",))
        .select_related(
            "agency",
            "client_movement_request",
            "target_box__source_container",
            "staging_location",
            "warehouse_operation",
        )
        .get(pk=plan_id)
    )
    if plan.mode != FbsReplenishmentPlan.MODE_ITEM or not plan.target_box_id:
        raise FbsReplenishmentError("План не содержит готового штучного FBS-короба.")
    if plan.status == FbsReplenishmentPlan.STATUS_IN_PROGRESS:
        from .reachtruck_bridge import sync_plan_reachtruck_placement

        sync_plan_reachtruck_placement(plan)
        return plan
    if plan.status != FbsReplenishmentPlan.STATUS_DONE:
        raise FbsReplenishmentError("Повторное размещение доступно только завершенному плану.")
    if not FbsStockBalance.objects.filter(box=plan.target_box, qty__gt=0).exists():
        raise FbsReplenishmentError("В готовом коробе не найден FBS-остаток.")
    if plan.target_box.source_container_id is None:
        physical_box = WarehouseContainer.objects.create(
            agency=plan.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code=plan.target_box.box_code,
            current_location=plan.staging_location,
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type=FBS_PLACEMENT_CONTEXT_TYPE,
            source_context_id=str(plan.id),
            created_by=actor,
        )
        plan.target_box.source_container = physical_box
        plan.target_box.save(update_fields=["source_container", "updated_at"])
    plan.status = FbsReplenishmentPlan.STATUS_IN_PROGRESS
    plan.completed_at = None
    plan.assigned_to = None
    plan.save(update_fields=["status", "completed_at", "assigned_to", "updated_at"])
    operation = plan.warehouse_operation
    operation.status = WarehouseOperation.STATUS_IN_PROGRESS
    operation.completed_at = None
    operation.save(update_fields=["status", "completed_at", "updated_at"])
    from .client_movements import sync_client_movement_request_status
    from .reachtruck_bridge import sync_plan_reachtruck_placement

    sync_plan_reachtruck_placement(plan)
    sync_client_movement_request_status(
        plan.client_movement_request_id,
        performed_by=actor,
    )
    return plan


def _complete_replenishment_allocation(
    *,
    allocation_id: int,
    source_scan: str,
    target_box_scan: str,
    performed_by=None,
    destination_preselected: bool = False,
    validate_target: bool = True,
    refresh_completion: bool = True,
    preserve_source_physical_place: bool = False,
    performed_by_role: str = "reachtruck",
) -> FbsStockMovement:
    _require_warehouse_writes()
    actor = _authenticated_user(performed_by)
    allocation = (
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "line__plan__agency",
            "line__plan__target_cell__location",
            "line__plan__target_pallet__warehouse_container",
            "line__plan__warehouse_operation",
            "line__source_container",
            "target_box__pallet",
            "warehouse_task",
        )
        .get(pk=allocation_id)
    )
    existing = FbsStockMovement.objects.filter(allocation=allocation).first()
    if existing is not None:
        return existing
    if allocation.status not in {
        FbsReplenishmentAllocation.STATUS_RESERVED,
        FbsReplenishmentAllocation.STATUS_IN_PROGRESS,
    }:
        raise FbsReplenishmentError("Распределение FBS нельзя завершить в текущем статусе.")

    plan = allocation.line.plan
    if _uses_prepared_box_flow(plan):
        raise FbsReplenishmentError(
            "Штучную заявку с печатными коробами можно выполнить только сканированием через ТСД."
        )
    if _requires_storekeeper_packing(plan):
        raise FbsReplenishmentError(
            "Штучную клиентскую заявку сначала передайте в зону подготовки FBS."
        )
    if actor is None or plan.assigned_to_id != actor.id:
        raise FbsReplenishmentError("Задание FBS не назначено этому сотруднику.")

    snapshot = (
        WarehouseStockSnapshot.objects.select_for_update(of=("self",))
        .select_related(
            "container__current_location",
            "container__parent_container__current_location",
            "location",
            "sku_ref",
        )
        .get(pk=allocation.source_snapshot_id)
    )
    if _normalize_scan(source_scan) not in _source_scan_values(snapshot):
        raise FbsReplenishmentError("Скан источника не совпадает с заданием FBS.")
    if not destination_preselected:
        allocation = _select_replenishment_destination(
            allocation=allocation,
            target_scan=target_box_scan,
            actor=actor,
        )
    plan = allocation.line.plan

    if validate_target:
        _validate_destination(plan, allocation.target_box)
        from .inventory import assert_box_unlocked

        assert_box_unlocked(allocation.target_box_id, for_execution=True)
    reserve = WarehouseReserve.objects.select_for_update().get(
        pk=allocation.warehouse_reserve_id
    )
    qty = int(allocation.qty_planned or 0) - int(allocation.qty_moved or 0)
    if qty <= 0:
        raise FbsReplenishmentError("В распределении FBS нет количества к перемещению.")
    if int(snapshot.qty or 0) < qty or int(snapshot.other_reserved_qty or 0) < qty:
        raise FbsReplenishmentError("Общий остаток изменился и больше не покрывает резерв FBS.")

    source_physical_location = _container_physical_location(
        snapshot.container,
        purpose="Нельзя завершить FBS-перемещение",
    )

    completion_location, physical_place_preserved = _completion_destination_location(
        plan=plan,
        snapshot=snapshot,
        preserve_source_physical_place=preserve_source_physical_place,
    )
    if physical_place_preserved:
        _apply_completion_destination(
            allocation=allocation,
            plan=plan,
            destination_location=completion_location,
        )

    balance = _get_or_create_balance(allocation=allocation, snapshot=snapshot)
    pending_confirmation = _credit_replenishment_balance(
        balance,
        plan=plan,
        qty=qty,
    )

    now = timezone.now()
    snapshot.qty = int(snapshot.qty or 0) - qty
    snapshot.other_reserved_qty = int(snapshot.other_reserved_qty or 0) - qty
    snapshot.snapshot_version = int(snapshot.snapshot_version or 0) + 1
    if snapshot.qty == 0:
        snapshot.available_qty = 0
        snapshot.is_archived = True
    if snapshot.active_operation_id == plan.warehouse_operation_id:
        snapshot.active_operation = None
        snapshot.active_operation_type = ""

    reserve.qty_satisfied = reserve.qty_reserved
    reserve.status = WarehouseReserve.STATUS_SATISFIED
    reserve.save(update_fields=["qty_satisfied", "status", "updated_at"])
    expiry_date = _snapshot_expiry_date(snapshot)
    event = WarehouseEvent.objects.create(
        agency=plan.agency,
        event_type="fbs_replenishment_completed",
        stock_context_type=CONTEXT_TYPE,
        stock_context_id=str(plan.id),
        container=snapshot.container,
        operation=plan.warehouse_operation,
        operation_task=allocation.warehouse_task,
        reserve=reserve,
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(plan.id),
        from_location=source_physical_location,
        to_location=completion_location,
        from_zone_code=str(source_physical_location.zone_code or ""),
        to_zone_code=str(completion_location.zone_code or ""),
        qty=qty,
        payload={
            "allocation_id": allocation.id,
            "source_snapshot_id": snapshot.id,
            "target_balance_id": balance.id,
            "target_box_id": allocation.target_box_id,
            "target_box_code": allocation.target_box.box_code,
            "mode": plan.mode,
            "marking_code": snapshot.marking_code,
            "lot_code": _snapshot_lot_code(snapshot),
            "expiry_date": expiry_date.isoformat() if expiry_date else "",
            "physical_place_preserved": physical_place_preserved,
            "technical_target_location_id": plan.target_cell.location_id,
            "source_location_id": source_physical_location.id,
            "source_location_code": str(source_physical_location.location_code or ""),
            "destination_location_id": completion_location.id,
            "destination_location_code": str(completion_location.location_code or ""),
            "responsible_user_id": actor.id,
            "responsible_user_name": str(
                actor.get_full_name() or actor.get_username() or actor.id
            ),
            "physical_place_confirmed_at": timezone.localtime(now).isoformat(),
            **_pending_confirmation_payload(
                pending=pending_confirmation,
                qty=qty,
            ),
        },
        performed_by=actor,
        performed_by_role=performed_by_role,
        occurred_at=now,
    )
    snapshot.last_event = event
    snapshot.save(
        update_fields=[
            "qty",
            "available_qty",
            "other_reserved_qty",
            "snapshot_version",
            "active_operation",
            "active_operation_type",
            "is_archived",
            "last_event",
            "updated_at",
        ]
    )
    allocation.qty_moved = allocation.qty_planned
    allocation.status = FbsReplenishmentAllocation.STATUS_DONE
    allocation.save(update_fields=["qty_moved", "status", "updated_at"])
    movement = FbsStockMovement.objects.create(
        allocation=allocation,
        source_snapshot=snapshot,
        target_balance=balance,
        warehouse_event=event,
        qty=qty,
        performed_by=actor,
        occurred_at=now,
    )
    if refresh_completion:
        _refresh_completion_status(
            allocation,
            now,
            actor=actor,
            preserve_source_physical_place=preserve_source_physical_place,
        )
    return movement


def _bind_full_source_pallets_in_place(
    *,
    allocations: Sequence[FbsReplenishmentAllocation],
    request_id: int,
) -> None:
    """Replace technical FBS placeholders with the physical source pallets."""
    pallet_groups: dict[int, list[FbsReplenishmentAllocation]] = defaultdict(list)
    for allocation in allocations:
        source_pallet_id = _full_source_pallet_id(allocation.line.plan)
        if source_pallet_id <= 0:
            raise FbsReplenishmentError(
                "Заявка содержит отдельные короба и требует ричтрака."
            )
        pallet_groups[source_pallet_id].append(allocation)

    for source_pallet_id, pallet_allocations in pallet_groups.items():
        target_pallet_ids = {
            int(allocation.line.plan.target_pallet_id or 0)
            for allocation in pallet_allocations
        }
        if len(target_pallet_ids) != 1 or 0 in target_pallet_ids:
            raise FbsReplenishmentError(
                "У полной исходной паллеты несколько мест назначения."
            )
        target_pallet = (
            FbsPallet.objects.select_for_update(of=("self",))
            .select_related("warehouse_container", "cell__location", "agency")
            .get(pk=next(iter(target_pallet_ids)))
        )
        source_pallet = (
            WarehouseContainer.objects.select_for_update(of=("self",))
            .select_related("current_location")
            .filter(
                pk=source_pallet_id,
                agency_id=target_pallet.agency_id,
                status=WarehouseContainer.STATUS_ACTIVE,
                container_type__in=(
                    WarehouseContainer.TYPE_PALLET,
                    WarehouseContainer.TYPE_MIXED_PALLET,
                ),
            )
            .first()
        )
        if source_pallet is None:
            raise FbsReplenishmentError("Исходная паллета больше не активна.")
        _container_physical_location(
            source_pallet,
            purpose="Нельзя провести паллету в FBS на текущем месте",
        )

        source_box_ids = {
            int(allocation.source_snapshot.container_id or 0)
            for allocation in pallet_allocations
        }
        active_source_box_ids = set(
            WarehouseContainer.objects.select_for_update()
            .filter(
                parent_container=source_pallet,
                agency_id=target_pallet.agency_id,
                status=WarehouseContainer.STATUS_ACTIVE,
                container_type=WarehouseContainer.TYPE_BOX,
            )
            .values_list("id", flat=True)
        )
        if 0 in source_box_ids or source_box_ids != active_source_box_ids:
            raise FbsReplenishmentError(
                "Состав исходной паллеты изменился. Логическая проводка остановлена."
            )

        target_boxes = list(
            FbsBox.objects.select_for_update(of=("self",))
            .filter(
                pallet=target_pallet,
                status__in=(FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE),
            )
            .select_related("source_container")
            .order_by("id")
        )
        if {
            int(box.source_container_id or 0) for box in target_boxes
        } != source_box_ids:
            raise FbsReplenishmentError(
                "Состав запланированной FBS-паллеты не совпадает с исходной паллетой."
            )

        if FbsPallet.objects.select_for_update(of=("self",)).filter(
            warehouse_container=source_pallet,
        ).exclude(pk=target_pallet.pk).exists():
            raise FbsReplenishmentError(
                "Исходная паллета уже привязана к другому остатку FBS."
            )
        if FbsPallet.objects.select_for_update(of=("self",)).filter(
            agency_id=target_pallet.agency_id,
            pallet_code__iexact=source_pallet.container_code,
        ).exclude(pk=target_pallet.pk).exists():
            raise FbsReplenishmentError(
                "QR исходной паллеты уже используется в другом остатке FBS."
            )

        placeholder_id = target_pallet.warehouse_container_id
        if placeholder_id is None:
            raise FbsReplenishmentError("Резерв места полной FBS-паллеты поврежден.")
        placeholder = WarehouseContainer.objects.select_for_update().get(
            pk=placeholder_id
        )
        if placeholder.id != source_pallet.id:
            if (
                WarehouseContainer.objects.filter(parent_container=placeholder)
                .exclude(status=WarehouseContainer.STATUS_ARCHIVED)
                .exists()
                or WarehouseStockSnapshot.objects.filter(
                    Q(container=placeholder) | Q(parent_container=placeholder),
                    is_archived=False,
                    qty__gt=0,
                ).exists()
            ):
                raise FbsReplenishmentError(
                    "В технической паллете назначения уже появился товар."
                )
            placeholder.current_location = None
            placeholder.parent_container = None
            placeholder.status = WarehouseContainer.STATUS_ARCHIVED
            placeholder.save(
                update_fields=[
                    "current_location",
                    "parent_container",
                    "status",
                    "updated_at",
                ]
            )

        target_pallet.warehouse_container = source_pallet
        target_pallet.pallet_code = source_pallet.container_code
        target_pallet.full_clean()
        target_pallet.save(
            update_fields=["warehouse_container", "pallet_code", "updated_at"]
        )
        source_pallet.source_context_type = FBS_STORAGE_CONTEXT_TYPE
        source_pallet.source_context_id = str(int(request_id))
        source_pallet.save(
            update_fields=["source_context_type", "source_context_id", "updated_at"]
        )
        WarehouseContainer.objects.filter(id__in=source_box_ids).update(
            source_context_type=FBS_PLACEMENT_CONTEXT_TYPE,
            source_context_id=str(int(request_id)),
            updated_at=timezone.now(),
        )


@transaction.atomic
def complete_full_pallet_client_movement_logically(
    *,
    allocation_ids: Sequence[int],
    performed_by=None,
) -> list[FbsStockMovement]:
    """Post a complete client pallet to FBS without moving its physical place."""
    _require_warehouse_writes()
    actor = _storekeeper_actor(performed_by)
    normalized_ids = list(dict.fromkeys(int(value) for value in allocation_ids))
    if not normalized_ids:
        raise FbsReplenishmentError("В паллетном FBS-перемещении нет распределений.")

    allocations = list(
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "line__plan__agency",
            "line__plan__target_cell__location",
            "line__plan__target_pallet__warehouse_container",
            "line__plan__target_box",
            "line__plan__warehouse_operation",
            "line__source_container__parent_container",
            "source_snapshot__container__parent_container",
            "source_snapshot__container__current_location",
            "source_snapshot__location",
            "target_box__pallet",
            "warehouse_task",
        )
        .filter(id__in=normalized_ids)
        .order_by("line__plan_id", "id")
    )
    if len(allocations) != len(normalized_ids):
        raise FbsReplenishmentError("Часть распределений паллетного FBS-перемещения не найдена.")

    plans_by_id = {
        int(allocation.line.plan_id): allocation.line.plan
        for allocation in allocations
    }
    plans = list(plans_by_id.values())
    if any(plan.mode != FbsReplenishmentPlan.MODE_BOX for plan in plans):
        raise FbsReplenishmentError("Без ричтрака можно провести только целую паллету.")
    if actor is None or any(plan.assigned_to_id != actor.id for plan in plans):
        raise FbsReplenishmentError("Паллетное перемещение не назначено кладовщику.")
    movement_ids = {int(plan.client_movement_request_id or 0) for plan in plans}
    if len(movement_ids) != 1 or 0 in movement_ids:
        raise FbsReplenishmentError("Планы относятся к разным клиентским FBS-заявкам.")

    source_pallet_ids = {_full_source_pallet_id(plan) for plan in plans}
    if 0 in source_pallet_ids:
        raise FbsReplenishmentError("Заявка содержит отдельные короба и требует ричтрака.")
    for allocation in allocations:
        source_parent_id = int(
            allocation.source_snapshot.container.parent_container_id or 0
        )
        expected_parent_id = _full_source_pallet_id(allocation.line.plan)
        if source_parent_id != expected_parent_id:
            raise FbsReplenishmentError(
                "Состав исходной паллеты изменился. Логическая проводка остановлена."
            )

    _bind_full_source_pallets_in_place(
        allocations=allocations,
        request_id=next(iter(movement_ids)),
    )

    existing_by_allocation = {
        row.allocation_id: row
        for row in FbsStockMovement.objects.filter(
            allocation_id__in=normalized_ids
        ).select_related("allocation")
    }
    if existing_by_allocation:
        if len(existing_by_allocation) == len(normalized_ids):
            return [existing_by_allocation[value] for value in normalized_ids]
        raise FbsReplenishmentError(
            "Паллетное FBS-перемещение проведено частично; требуется проверка склада."
        )

    movements_by_allocation: dict[int, FbsStockMovement] = {}
    allocations_by_plan: dict[int, list[FbsReplenishmentAllocation]] = defaultdict(list)
    for allocation in allocations:
        allocations_by_plan[int(allocation.line.plan_id)].append(allocation)

    for plan_allocations in allocations_by_plan.values():
        last_allocation = None
        for allocation in plan_allocations:
            movement = _complete_replenishment_allocation(
                allocation_id=allocation.id,
                source_scan=allocation.source_snapshot.container.container_code,
                target_box_scan=allocation.target_box.box_code,
                performed_by=actor,
                destination_preselected=True,
                validate_target=False,
                refresh_completion=False,
                preserve_source_physical_place=True,
                performed_by_role="storekeeper",
            )
            movements_by_allocation[allocation.id] = movement
            last_allocation = movement.allocation
        if last_allocation is not None:
            _refresh_completion_status(
                last_allocation,
                timezone.now(),
                actor=actor,
                line_ids={row.line_id for row in plan_allocations},
                preserve_source_physical_place=True,
                defer_request_sync=True,
            )

    from .client_movements import sync_client_movement_request_status

    sync_client_movement_request_status(
        next(iter(movement_ids)),
        performed_by=actor,
    )

    return [movements_by_allocation[value] for value in normalized_ids]


@transaction.atomic
def complete_replenishment_allocation(
    *,
    allocation_id: int,
    source_scan: str,
    target_box_scan: str,
    performed_by=None,
) -> FbsStockMovement:
    return _complete_replenishment_allocation(
        allocation_id=allocation_id,
        source_scan=source_scan,
        target_box_scan=target_box_scan,
        performed_by=performed_by,
    )


def _bulk_complete_box_allocations(
    *,
    allocations: Sequence[FbsReplenishmentAllocation],
    plan: FbsReplenishmentPlan,
    source_scan: str,
    actor,
) -> dict[int, FbsStockMovement]:
    """Post every stock row of one physical box with bounded query count."""
    if not allocations:
        return {}

    snapshot_ids = {int(row.source_snapshot_id) for row in allocations}
    reserve_ids = {int(row.warehouse_reserve_id) for row in allocations}
    snapshots = {
        int(row.id): row
        for row in (
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related(
                "container__current_location",
                "container__parent_container__current_location",
                "location",
                "sku_ref",
            )
            .filter(id__in=snapshot_ids)
            .order_by("id")
        )
    }
    reserves = {
        int(row.id): row
        for row in WarehouseReserve.objects.select_for_update(of=("self",)).filter(
            id__in=reserve_ids
        )
    }
    if len(snapshots) != len(snapshot_ids) or len(reserves) != len(reserve_ids):
        raise FbsReplenishmentError("Остаток или резерв FBS больше не найден.")

    qty_by_snapshot: dict[int, int] = defaultdict(int)
    allocation_rows = []
    for allocation in allocations:
        if allocation.status not in {
            FbsReplenishmentAllocation.STATUS_RESERVED,
            FbsReplenishmentAllocation.STATUS_IN_PROGRESS,
        }:
            raise FbsReplenishmentError(
                "Распределение FBS нельзя завершить в текущем статусе."
            )
        snapshot = snapshots[int(allocation.source_snapshot_id)]
        if _normalize_scan(source_scan) not in _source_scan_values(snapshot):
            raise FbsReplenishmentError("Скан источника не совпадает с заданием FBS.")
        qty = int(allocation.qty_planned or 0) - int(allocation.qty_moved or 0)
        if qty <= 0:
            raise FbsReplenishmentError("В распределении FBS нет количества к перемещению.")
        qty_by_snapshot[int(snapshot.id)] += qty
        allocation_rows.append((allocation, snapshot, qty))

    for snapshot_id, qty in qty_by_snapshot.items():
        snapshot = snapshots[snapshot_id]
        if int(snapshot.qty or 0) < qty or int(snapshot.other_reserved_qty or 0) < qty:
            raise FbsReplenishmentError(
                "Общий остаток изменился и больше не покрывает резерв FBS."
            )

    target_box = allocations[0].target_box
    identity_snapshots: dict[str, WarehouseStockSnapshot] = {}
    marking_identities: dict[str, str] = {}
    identity_by_allocation: dict[int, str] = {}
    for allocation, snapshot, _qty in allocation_rows:
        identity = _stock_identity(snapshot)
        identity_by_allocation[int(allocation.id)] = identity
        identity_snapshots.setdefault(identity, snapshot)
        marking_code = str(snapshot.marking_code or "").strip()
        if marking_code:
            previous_identity = marking_identities.setdefault(marking_code, identity)
            if previous_identity != identity:
                raise FbsReplenishmentError(
                    "Один код Честного знака относится к разным остаткам."
                )

    identity_keys = set(identity_snapshots)
    balance_lookup = Q(box=target_box, identity_key__in=identity_keys)
    if marking_identities:
        balance_lookup |= Q(
            agency=plan.agency,
            marking_code__in=set(marking_identities),
        )
    locked_balances = list(
        FbsStockBalance.objects.select_for_update(of=("self",))
        .filter(balance_lookup)
        .select_related("box", "sku_ref")
        .order_by("id")
    )
    balances_by_identity: dict[str, FbsStockBalance] = {}
    for balance in locked_balances:
        marking_code = str(balance.marking_code or "").strip()
        expected_identity = marking_identities.get(marking_code)
        if expected_identity and (
            int(balance.box_id) != int(target_box.id)
            or str(balance.identity_key) != expected_identity
        ):
            raise FbsReplenishmentError(
                "Код Честного знака уже находится в другом FBS-коробе."
            )
        if int(balance.box_id) == int(target_box.id):
            balances_by_identity[str(balance.identity_key)] = balance

    missing_balances = []
    for identity in sorted(identity_keys - set(balances_by_identity)):
        snapshot = identity_snapshots[identity]
        if snapshot.sku_ref_id and snapshot.sku_ref.agency_id not in {
            None,
            plan.agency_id,
        }:
            raise FbsReplenishmentError("SKU принадлежит другому клиенту.")
        balance = FbsStockBalance(
            agency=plan.agency,
            box=target_box,
            sku_ref=snapshot.sku_ref,
            identity_key=identity,
            sku_code=snapshot.sku_code,
            name=snapshot.name,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            marking_code=snapshot.marking_code,
            lot_code=_snapshot_lot_code(snapshot),
            expiry_date=_snapshot_expiry_date(snapshot),
        )
        balance.clean()
        missing_balances.append(balance)
    if missing_balances:
        FbsStockBalance.objects.bulk_create(missing_balances, batch_size=500)
        balances_by_identity.update(
            {
                str(row.identity_key): row
                for row in FbsStockBalance.objects.select_for_update(of=("self",))
                .filter(box=target_box, identity_key__in=identity_keys)
                .order_by("id")
            }
        )
    if set(balances_by_identity) != identity_keys:
        raise FbsReplenishmentError("Не удалось подготовить FBS-остатки назначения.")

    now = timezone.now()
    pending_confirmation = bool(plan.client_movement_request_id)
    balance_qty: dict[int, int] = defaultdict(int)
    events = []
    event_rows = []
    for allocation, snapshot, qty in allocation_rows:
        source_location = _container_physical_location(
            snapshot.container,
            purpose="Нельзя завершить FBS-перемещение",
        )
        completion_location, physical_place_preserved = _completion_destination_location(
            plan=plan,
            snapshot=snapshot,
        )
        if physical_place_preserved:
            _apply_completion_destination(
                allocation=allocation,
                plan=plan,
                destination_location=completion_location,
            )
        balance = balances_by_identity[identity_by_allocation[int(allocation.id)]]
        balance_qty[int(balance.id)] += qty
        expiry_date = _snapshot_expiry_date(snapshot)
        event = WarehouseEvent(
            agency=plan.agency,
            event_type="fbs_replenishment_completed",
            stock_context_type=CONTEXT_TYPE,
            stock_context_id=str(plan.id),
            container=snapshot.container,
            operation=plan.warehouse_operation,
            operation_task=allocation.warehouse_task,
            reserve=reserves[int(allocation.warehouse_reserve_id)],
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(plan.id),
            from_location=source_location,
            to_location=completion_location,
            from_zone_code=str(source_location.zone_code or ""),
            to_zone_code=str(completion_location.zone_code or ""),
            qty=qty,
            payload={
                "allocation_id": allocation.id,
                "source_snapshot_id": snapshot.id,
                "target_balance_id": balance.id,
                "target_box_id": target_box.id,
                "target_box_code": target_box.box_code,
                "mode": plan.mode,
                "marking_code": snapshot.marking_code,
                "lot_code": _snapshot_lot_code(snapshot),
                "expiry_date": expiry_date.isoformat() if expiry_date else "",
                "physical_place_preserved": physical_place_preserved,
                "technical_target_location_id": plan.target_cell.location_id,
                "source_location_id": source_location.id,
                "source_location_code": str(source_location.location_code or ""),
                "destination_location_id": completion_location.id,
                "destination_location_code": str(
                    completion_location.location_code or ""
                ),
                "responsible_user_id": actor.id,
                "responsible_user_name": str(
                    actor.get_full_name() or actor.get_username() or actor.id
                ),
                "physical_place_confirmed_at": timezone.localtime(now).isoformat(),
                **_pending_confirmation_payload(
                    pending=pending_confirmation,
                    qty=qty,
                ),
            },
            performed_by=actor,
            performed_by_role="reachtruck",
            occurred_at=now,
        )
        events.append(event)
        event_rows.append((allocation, snapshot, balance, qty, event))

    for balance in balances_by_identity.values():
        qty = balance_qty.get(int(balance.id), 0)
        if not qty:
            continue
        balance.qty = int(balance.qty or 0) + qty
        if not pending_confirmation:
            balance.available_qty = int(balance.available_qty or 0) + qty
        balance.updated_at = now
    changed_balances = [
        row for row in balances_by_identity.values() if balance_qty.get(int(row.id), 0)
    ]
    FbsStockBalance.objects.bulk_update(
        changed_balances,
        ["qty", "available_qty", "updated_at"],
        batch_size=500,
    )

    WarehouseEvent.objects.bulk_create(events, batch_size=500)
    if any(event.pk is None for event in events):
        raise FbsReplenishmentError("Не удалось записать аудит FBS-перемещения.")

    for allocation, snapshot, _balance, qty, event in event_rows:
        snapshot.qty = int(snapshot.qty or 0) - qty
        snapshot.other_reserved_qty = int(snapshot.other_reserved_qty or 0) - qty
        snapshot.snapshot_version = int(snapshot.snapshot_version or 0) + 1
        if snapshot.qty == 0:
            snapshot.available_qty = 0
            snapshot.is_archived = True
        if snapshot.active_operation_id == plan.warehouse_operation_id:
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
        snapshot.last_event = event
        snapshot.updated_at = now
        reserve = reserves[int(allocation.warehouse_reserve_id)]
        reserve.qty_satisfied = reserve.qty_reserved
        reserve.status = WarehouseReserve.STATUS_SATISFIED
        reserve.updated_at = now
        allocation.qty_moved = allocation.qty_planned
        allocation.status = FbsReplenishmentAllocation.STATUS_DONE
        allocation.updated_at = now

    WarehouseStockSnapshot.objects.bulk_update(
        list(snapshots.values()),
        [
            "qty",
            "available_qty",
            "other_reserved_qty",
            "snapshot_version",
            "active_operation",
            "active_operation_type",
            "is_archived",
            "last_event",
            "updated_at",
        ],
        batch_size=500,
    )
    WarehouseReserve.objects.bulk_update(
        list(reserves.values()),
        ["qty_satisfied", "status", "updated_at"],
        batch_size=500,
    )
    FbsReplenishmentAllocation.objects.bulk_update(
        list(allocations),
        ["qty_moved", "status", "updated_at"],
        batch_size=500,
    )

    movement_rows = [
        FbsStockMovement(
            allocation=allocation,
            source_snapshot=snapshot,
            target_balance=balance,
            warehouse_event=event,
            qty=qty,
            performed_by=actor,
            occurred_at=now,
        )
        for allocation, snapshot, balance, qty, event in event_rows
    ]
    FbsStockMovement.objects.bulk_create(movement_rows, batch_size=500)
    return {int(row.allocation_id): row for row in movement_rows}


@transaction.atomic
def complete_replenishment_box_allocations(
    *,
    allocation_ids: Sequence[int],
    source_scan: str,
    target_box_scan: str,
    performed_by=None,
) -> list[FbsStockMovement]:
    """Conduct one physical FBS box and refresh its plan only once."""
    _require_warehouse_writes()
    actor = _authenticated_user(performed_by)
    normalized_ids = list(dict.fromkeys(int(value) for value in allocation_ids))
    if not normalized_ids:
        raise FbsReplenishmentError("В задании FBS нет распределений для перемещения.")

    allocations = list(
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "line__plan__agency",
            "line__plan__target_cell__location",
            "line__plan__target_pallet__warehouse_container",
            "line__plan__warehouse_operation",
            "line__source_container",
            "source_snapshot__container",
            "target_box__pallet",
            "warehouse_task",
        )
        .filter(id__in=normalized_ids)
        .order_by("id")
    )
    if len(allocations) != len(normalized_ids):
        raise FbsReplenishmentError("Часть распределений FBS не найдена.")
    if len({row.line.plan_id for row in allocations}) != 1:
        raise FbsReplenishmentError("Один короб FBS не может относиться к разным планам.")
    if len({row.warehouse_task_id for row in allocations}) != 1:
        raise FbsReplenishmentError("Распределения FBS относятся к разным заданиям.")
    if len({row.source_snapshot.container_id for row in allocations}) != 1:
        raise FbsReplenishmentError("Распределения относятся к разным физическим коробам.")

    plan = allocations[0].line.plan
    if plan.mode != FbsReplenishmentPlan.MODE_BOX:
        raise FbsReplenishmentError("Пакетное проведение разрешено только для целого короба.")
    if actor is None or plan.assigned_to_id != actor.id:
        raise FbsReplenishmentError("Задание FBS не назначено этому сотруднику.")

    existing_by_allocation = {
        row.allocation_id: row
        for row in FbsStockMovement.objects.filter(
            allocation_id__in=normalized_ids
        ).select_related("allocation")
    }
    pending = [
        row for row in allocations if row.id not in existing_by_allocation
    ]
    if not pending:
        return [existing_by_allocation[value] for value in normalized_ids]

    selected = _select_replenishment_destination(
        allocation=pending[0],
        target_scan=target_box_scan,
        actor=actor,
    )
    _validate_destination(selected.line.plan, selected.target_box)
    from .inventory import assert_box_unlocked

    assert_box_unlocked(selected.target_box_id, for_execution=True)

    movements_by_allocation = dict(existing_by_allocation)
    pending = list(
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "line__plan__agency",
            "line__plan__target_cell__location",
            "line__plan__target_pallet__warehouse_container",
            "line__plan__warehouse_operation",
            "line__source_container",
            "source_snapshot__container",
            "target_box__pallet",
            "warehouse_task",
        )
        .filter(id__in=[row.id for row in pending])
        .order_by("id")
    )
    plan = pending[0].line.plan
    new_movements = _bulk_complete_box_allocations(
        allocations=pending,
        plan=plan,
        source_scan=source_scan,
        actor=actor,
    )
    movements_by_allocation.update(new_movements)

    if new_movements:
        last_allocation = new_movements[pending[-1].id].allocation
        _refresh_completion_status(
            last_allocation,
            timezone.now(),
            actor=actor,
            line_ids={row.line_id for row in allocations},
        )
    return [movements_by_allocation[value] for value in normalized_ids]


@transaction.atomic
def complete_replenishment_pallet_allocations(
    *,
    allocation_ids: Sequence[int],
    source_pallet_scan: str,
    target_scan: str,
    performed_by=None,
) -> list[FbsStockMovement]:
    """Conducts every box on one fully selected physical pallet after one pallet scan."""
    _require_warehouse_writes()
    actor = _authenticated_user(performed_by)
    normalized_ids = list(dict.fromkeys(int(value) for value in allocation_ids))
    if not normalized_ids:
        raise FbsReplenishmentError("В паллетном FBS-задании нет распределений.")
    allocations = list(
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "line__plan__agency",
            "line__plan__target_cell__location",
            "line__plan__target_pallet__warehouse_container",
            "line__plan__target_box",
            "line__plan__warehouse_operation",
            "line__source_container__parent_container",
            "source_snapshot__container__parent_container",
            "target_box__pallet",
            "warehouse_task",
        )
        .filter(id__in=normalized_ids)
        .order_by("id")
    )
    if len(allocations) != len(normalized_ids):
        raise FbsReplenishmentError("Часть распределений паллетного FBS-задания не найдена.")
    existing_by_allocation = {
        row.allocation_id: row
        for row in FbsStockMovement.objects.filter(
            allocation_id__in=normalized_ids
        ).select_related("allocation")
    }
    if existing_by_allocation:
        if len(existing_by_allocation) == len(normalized_ids):
            return [existing_by_allocation[value] for value in normalized_ids]
        raise FbsReplenishmentError(
            "Паллетное FBS-перемещение проведено частично; требуется проверка склада."
        )

    plans_by_id = {allocation.line.plan_id: allocation.line.plan for allocation in allocations}
    plans = list(plans_by_id.values())
    if any(plan.mode != FbsReplenishmentPlan.MODE_BOX for plan in plans):
        raise FbsReplenishmentError("Паллетой можно провести только целые физические короба.")
    if actor is None or any(plan.assigned_to_id != actor.id for plan in plans):
        raise FbsReplenishmentError("Все задания паллеты должны быть назначены этому сотруднику.")
    movement_ids = {int(plan.client_movement_request_id or 0) for plan in plans}
    if len(movement_ids) != 1 or 0 in movement_ids:
        raise FbsReplenishmentError("Паллетные задания относятся к разным FBS-заявкам.")
    source_pallet_ids = {_full_source_pallet_id(plan) for plan in plans}
    if len(source_pallet_ids) != 1 or 0 in source_pallet_ids:
        raise FbsReplenishmentError("В задании отсутствует признак полной исходной паллеты.")
    source_pallet_id = next(iter(source_pallet_ids))
    source_box_ids = {
        int(allocation.source_snapshot.container_id or 0) for allocation in allocations
    }
    if 0 in source_box_ids or any(
        int(allocation.source_snapshot.container.parent_container_id or 0)
        != source_pallet_id
        for allocation in allocations
    ):
        raise FbsReplenishmentError("Короба больше не принадлежат исходной паллете.")
    target_pallet_ids = {int(plan.target_pallet_id or 0) for plan in plans}
    if len(target_pallet_ids) != 1 or 0 in target_pallet_ids:
        raise FbsReplenishmentError("У полной исходной паллеты несколько мест назначения.")
    target_pallet = (
        FbsPallet.objects.select_for_update(of=("self",))
        .select_related("warehouse_container", "cell__location")
        .get(pk=next(iter(target_pallet_ids)))
    )
    target_boxes = list(
        FbsBox.objects.select_for_update(of=("self",))
        .filter(
            pallet=target_pallet,
            status__in=(FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE),
        )
        .select_related("source_container")
        .order_by("id")
    )
    if {int(box.source_container_id or 0) for box in target_boxes} != source_box_ids:
        raise FbsReplenishmentError(
            "Состав запланированной FBS-паллеты не совпадает с исходной паллетой."
        )

    source_pallet = WarehouseContainer.objects.select_for_update().get(pk=source_pallet_id)
    if str(source_pallet.container_code or "").strip().casefold() != str(
        source_pallet_scan or ""
    ).strip().casefold():
        raise FbsReplenishmentError("Скан паллеты не совпадает с заданием.")
    destination = _resolve_active_os_location(target_scan)
    destination_cell, _created = FbsStorageCell.objects.get_or_create(
        location=destination,
        defaults={
            "cell_code": (
                f"FBS@{os_location_code(row=destination.row_no, section=destination.section_no, tier=destination.tier_no, cell=destination.cell_no)}"
            ),
            "purpose": FbsStorageCell.PURPOSE_FLEX,
            "client_cluster": target_pallet.agency_id,
            "is_active": True,
        },
    )
    destination_cell = FbsStorageCell.objects.select_for_update().get(pk=destination_cell.pk)
    if not destination_cell.is_active:
        raise FbsReplenishmentError("Выбранная ячейка выключена для FBS.")
    conflicting_pallet = (
        FbsPallet.objects.select_for_update(of=("self",))
        .select_related("warehouse_container", "cell__location")
        .filter(
            cell=destination_cell,
            status__in=(FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE),
        )
        .exclude(pk=target_pallet.pk)
        .first()
    )
    merge_into_existing = False
    if conflicting_pallet is not None:
        from .storage import release_unplaced_os_reservation

        if release_unplaced_os_reservation(pallet=conflicting_pallet):
            conflicting_pallet = None
        elif conflicting_pallet.agency_id != target_pallet.agency_id:
            raise FbsReplenishmentError(
                _occupied_fbs_destination_error(
                    destination=destination,
                    pallet=conflicting_pallet,
                )
            )
        else:
            destination_container = conflicting_pallet.warehouse_container
            if (
                destination_container is None
                or destination_container.status != WarehouseContainer.STATUS_ACTIVE
                or destination_container.current_location_id != destination.id
                or str(destination_container.source_context_type or "").strip()
                != FBS_STORAGE_CONTEXT_TYPE
            ):
                raise FbsReplenishmentError(
                    "Существующая FBS-паллета в выбранной ячейке имеет неверную "
                    "физическую привязку."
                )
            merge_into_existing = True

    placeholder = target_pallet.warehouse_container
    if placeholder is None or placeholder.id == source_pallet.id:
        raise FbsReplenishmentError("Резерв места полной FBS-паллеты поврежден.")
    if (
        WarehouseContainer.objects.filter(parent_container=placeholder)
        .exclude(status=WarehouseContainer.STATUS_ARCHIVED)
        .exists()
        or WarehouseStockSnapshot.objects.filter(
            Q(container=placeholder) | Q(parent_container=placeholder),
            is_archived=False,
            qty__gt=0,
        ).exists()
    ):
        raise FbsReplenishmentError("В технической паллете назначения уже появился товар.")
    placeholder.current_location = None
    placeholder.parent_container = None
    placeholder.status = WarehouseContainer.STATUS_ARCHIVED
    placeholder.save(
        update_fields=["current_location", "parent_container", "status", "updated_at"]
    )

    if merge_into_existing:
        destination_pallet = conflicting_pallet
        for target_box in target_boxes:
            target_box.pallet = destination_pallet
            target_box.full_clean()
            target_box.save(update_fields=["pallet", "updated_at"])
        target_pallet.status = FbsPallet.STATUS_ARCHIVED
        target_pallet.save(update_fields=["status", "updated_at"])
        target_pallet = destination_pallet
    else:
        target_pallet.cell = destination_cell
        target_pallet.warehouse_container = source_pallet
        target_pallet.pallet_code = source_pallet.container_code
        target_pallet.full_clean()
        target_pallet.save(
            update_fields=[
                "cell",
                "warehouse_container",
                "pallet_code",
                "updated_at",
            ]
        )

    allocation_rows = [
        {
            "snapshot_id": allocation.source_snapshot_id,
            "container_id": allocation.source_snapshot.container_id,
            "qty": int(allocation.qty_planned or 0),
            "plan_id": int(allocation.line.plan_id or 0),
            "operation_id": int(allocation.line.plan.warehouse_operation_id or 0),
            "reserve_id": int(allocation.warehouse_reserve_id or 0),
        }
        for allocation in allocations
    ]
    try:
        WarehouseWritePathService.move_reserved_fbs_pallet(
            agency=target_pallet.agency,
            request_id=next(iter(movement_ids)),
            source_pallet_id=source_pallet.id,
            source_pallet_scan=source_pallet_scan,
            selected_box_ids=source_box_ids,
            allocation_rows=allocation_rows,
            destination=destination,
            performed_by=actor,
        )
    except WarehouseTransitionError as exc:
        raise FbsReplenishmentError(str(exc)) from exc

    for plan in plans:
        plan.target_cell = destination_cell
        plan.target_pallet = target_pallet
        if merge_into_existing:
            plan.target_box = next(
                box for box in target_boxes if box.id == plan.target_box_id
            )
        plan.full_clean()
        plan.save(update_fields=["target_cell", "target_pallet", "updated_at"])
        _set_operation_destination(plan=plan, target_box=plan.target_box)

    movements = []
    allocations_by_plan: dict[int, list[FbsReplenishmentAllocation]] = defaultdict(list)
    for allocation in allocations:
        movement = _complete_replenishment_allocation(
            allocation_id=allocation.id,
            source_scan=allocation.source_snapshot.container.container_code,
            target_box_scan=target_scan,
            performed_by=actor,
            destination_preselected=True,
            validate_target=True,
            refresh_completion=False,
        )
        movements.append(movement)
        allocations_by_plan[int(allocation.line.plan_id)].append(movement.allocation)

    for plan_allocations in allocations_by_plan.values():
        _refresh_completion_status(
            plan_allocations[-1],
            timezone.now(),
            actor=actor,
            line_ids={row.line_id for row in plan_allocations},
            defer_request_sync=True,
        )

    if merge_into_existing:
        source_pallet.refresh_from_db()
        if WarehouseContainer.objects.filter(
            parent_container=source_pallet,
            status=WarehouseContainer.STATUS_ACTIVE,
        ).exists():
            raise FbsReplenishmentError(
                "Не все короба исходной паллеты перенесены на выбранную FBS-паллету."
            )
        source_pallet.current_location = None
        source_pallet.parent_container = None
        source_pallet.status = WarehouseContainer.STATUS_ARCHIVED
        source_pallet.save(
            update_fields=["current_location", "parent_container", "status", "updated_at"]
        )

    movement_request_id = next(iter(movement_ids))
    from .client_movements import sync_client_movement_request_status
    from .reachtruck_bridge import sync_client_movement_reachtruck_request

    sync_client_movement_request_status(
        movement_request_id,
        performed_by=actor,
    )
    sync_client_movement_reachtruck_request(movement_request_id)
    return movements


@transaction.atomic
def cancel_replenishment_plan(*, plan_id: int, canceled_by=None) -> FbsReplenishmentPlan:
    _require_warehouse_writes()
    actor = _authenticated_user(canceled_by)
    plan = (
        FbsReplenishmentPlan.objects.select_for_update(of=("self",))
        .select_related("warehouse_operation", "target_cell__location")
        .get(pk=plan_id)
    )
    if plan.status == FbsReplenishmentPlan.STATUS_CANCELED:
        return plan
    if plan.status == FbsReplenishmentPlan.STATUS_DONE or plan.moved_qty > 0:
        raise FbsReplenishmentError("Нельзя отменить уже начатое перемещение FBS.")
    allocations = list(
        FbsReplenishmentAllocation.objects.select_for_update(of=("self",))
        .select_related("warehouse_reserve", "warehouse_task", "source_snapshot__location")
        .filter(line__plan=plan)
    )
    if any(int(allocation.qty_staged or 0) > 0 for allocation in allocations):
        raise FbsReplenishmentError(
            "Нельзя отменить план: товар уже передан кладовщику в зону подготовки FBS."
        )
    prepared_boxes = list(
        FbsReplenishmentPreparedBox.objects.select_for_update()
        .select_related("physical_container")
        .filter(plan=plan)
    )
    if any(int(box.scanned_qty or 0) > 0 for box in prepared_boxes):
        raise FbsReplenishmentError(
            "Нельзя отменить план: товар уже отсканирован в постоянный FBS-короб."
        )
    for allocation in allocations:
        if allocation.status == FbsReplenishmentAllocation.STATUS_CANCELED:
            continue
        snapshot = WarehouseStockSnapshot.objects.select_for_update().get(
            pk=allocation.source_snapshot_id
        )
        qty = int(allocation.qty_planned or 0) - int(allocation.qty_moved or 0)
        if qty > int(snapshot.other_reserved_qty or 0):
            raise FbsReplenishmentError("Нельзя снять поврежденный резерв FBS.")
        snapshot.available_qty = int(snapshot.available_qty or 0) + qty
        snapshot.other_reserved_qty = int(snapshot.other_reserved_qty or 0) - qty
        snapshot.snapshot_version = int(snapshot.snapshot_version or 0) + 1
        if snapshot.active_operation_id == plan.warehouse_operation_id:
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
        event = WarehouseEvent.objects.create(
            agency=plan.agency,
            event_type="fbs_replenishment_canceled",
            stock_context_type=CONTEXT_TYPE,
            stock_context_id=str(plan.id),
            container=snapshot.container,
            operation=plan.warehouse_operation,
            operation_task=allocation.warehouse_task,
            reserve=allocation.warehouse_reserve,
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(plan.id),
            from_location=snapshot.location,
            from_zone_code=str(snapshot.zone_code or ""),
            qty=qty,
            payload={"allocation_id": allocation.id},
            performed_by=actor,
            performed_by_role="storekeeper",
            occurred_at=timezone.now(),
        )
        snapshot.last_event = event
        snapshot.save(
            update_fields=[
                "available_qty",
                "other_reserved_qty",
                "snapshot_version",
                "active_operation",
                "active_operation_type",
                "last_event",
                "updated_at",
            ]
        )
        reserve = allocation.warehouse_reserve
        reserve.status = WarehouseReserve.STATUS_CANCELED
        reserve.released_by = actor
        reserve.save(update_fields=["status", "released_by", "updated_at"])
        allocation.status = FbsReplenishmentAllocation.STATUS_CANCELED
        allocation.save(update_fields=["status", "updated_at"])
    now = timezone.now()
    for prepared_box in prepared_boxes:
        prepared_box.status = FbsReplenishmentPreparedBox.STATUS_UNUSED
        prepared_box.closed_at = now
        prepared_box.save(update_fields=["status", "closed_at", "updated_at"])
        prepared_box.physical_container.status = WarehouseContainer.STATUS_ARCHIVED
        prepared_box.physical_container.save(update_fields=["status", "updated_at"])

    box_lines = list(
        FbsReplenishmentLine.objects.select_for_update(of=("self",))
        .select_related("target_box")
        .filter(
            plan=plan,
            source_container__isnull=False,
        )
    )
    for line in box_lines:
        target_box = FbsBox.objects.select_for_update().get(pk=line.target_box_id)
        if target_box.source_container_id == line.source_container_id:
            target_box.source_container = None
            target_box.save(update_fields=["source_container", "updated_at"])

    WarehouseOperationTask.objects.filter(operation=plan.warehouse_operation).update(
        status=WarehouseOperationTask.STATUS_CANCELED,
        updated_at=timezone.now(),
    )
    FbsReplenishmentLine.objects.filter(plan=plan).update(
        status=FbsReplenishmentLine.STATUS_CANCELED,
        updated_at=timezone.now(),
    )
    if plan.warehouse_operation_id:
        plan.warehouse_operation.status = WarehouseOperation.STATUS_CANCELED
        plan.warehouse_operation.save(update_fields=["status", "updated_at"])
    plan.status = FbsReplenishmentPlan.STATUS_CANCELED
    plan.canceled_at = timezone.now()
    plan.save(update_fields=["status", "canceled_at", "updated_at"])
    if plan.client_movement_request_id:
        from .client_movements import sync_client_movement_request_status
        from .reachtruck_bridge import sync_plan_reachtruck_canceled
        from .stock_sync import queue_agency_stock_exports

        sync_plan_reachtruck_canceled(plan)
        sync_client_movement_request_status(
            plan.client_movement_request_id, performed_by=actor
        )
        archive_movement_staging_container(movement=plan.client_movement_request)
        queue_agency_stock_exports(agency_id=plan.agency_id)
    return plan
