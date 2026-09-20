from __future__ import annotations

import hashlib
import re

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from fbs.barcode_aliases import normalize_barcode, same_sku_barcode_variant
from fbs.exceptions import FbsFeatureDisabled, FbsMovementError
from fbs.flags import feature_enabled
from fbs.models import (
    FbsBox,
    FbsInternalMovement,
    FbsOrderStockAllocation,
    FbsOrderTraceability,
    FbsPallet,
    FbsRack,
    FbsRackCell,
    FbsRackCellBinding,
    FbsRackContentMovement,
    FbsRackStagingBox,
    FbsReplenishmentPlan,
    FbsStockBalance,
    FbsStorageCell,
)
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseOperationTask,
)
from sklad.location_occupancy import general_warehouse_location_occupancy_message
from sklad.services.operational_locations import normalize_operational_location_scan

from .inventory import validate_boxes_unlocked
from .relocation_reservations import relocation_reservation_state


ACTIVE_BOX_STATUSES = (FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE)
ACTIVE_PALLET_STATUSES = (FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE)
ACTIVE_MOVEMENT_STATUSES = (
    FbsInternalMovement.STATUS_PROPOSED,
    FbsInternalMovement.STATUS_IN_PROGRESS,
    FbsInternalMovement.STATUS_BLOCKED,
)
ACTIVE_REPLENISHMENT_STATUSES = (
    FbsReplenishmentPlan.STATUS_PROPOSED,
    FbsReplenishmentPlan.STATUS_CONFIRMED,
    FbsReplenishmentPlan.STATUS_IN_PROGRESS,
    FbsReplenishmentPlan.STATUS_AWAITING_PACK,
    FbsReplenishmentPlan.STATUS_BLOCKED,
)
DIRECT_FBS_BOX_SOURCE_ZONES = frozenset({"FBS", "OS", "PR"})
AIM_SYMBOLOGY_PREFIX_RE = re.compile(r"^\][A-Z0-9]{2}")
TEXT_FNC1_RE = re.compile(r"\{FNC1\}", re.IGNORECASE)


def _actor(user):
    return user if getattr(user, "is_authenticated", False) else None


def _require_writes() -> None:
    if not feature_enabled("module"):
        raise FbsFeatureDisabled("Модуль FBS выключен.")
    if not feature_enabled("warehouse_writes"):
        raise FbsFeatureDisabled("Складские операции FBS выключены.")


def _normalized_box_scan(value: str) -> str:
    value = AIM_SYMBOLOGY_PREFIX_RE.sub("", str(value or "").strip()).strip()
    return value.upper()


def _canonical_item_scan(value: str) -> str:
    scan = str(value or "").strip()
    if scan[:3].casefold() == "]d2":
        scan = scan[3:]
    return TEXT_FNC1_RE.sub("\x1d", scan).strip("\x1d")


def _item_scan_fingerprint(value: str) -> str:
    return hashlib.sha256(_canonical_item_scan(value).encode("utf-8")).hexdigest()


def _resolve_source_box(scan_value: str) -> FbsBox:
    code = _normalized_box_scan(scan_value)
    if not code:
        raise FbsMovementError("Отсканируйте исходный FBS-короб.")
    matches = list(
        FbsBox.objects.filter(
            box_code__iexact=code,
            status__in=ACTIVE_BOX_STATUSES,
        )
        .select_related(
            "agency",
            "pallet__cell__location",
            "source_container__current_location",
        )
        .order_by("id")[:3]
    )
    if not matches:
        raise FbsMovementError("Активный FBS-короб по этому QR не найден.")
    if len(matches) > 1:
        raise FbsMovementError(
            "Этот код FBS-короба используется у нескольких клиентов. Требуется уникальный QR."
        )
    return matches[0]


def resolve_fbs_rack_scan(scan_value: str, *, lock: bool = False) -> FbsRack:
    code = normalize_operational_location_scan(scan_value)
    if not code:
        raise FbsMovementError("Отсканируйте PR-стеллаж назначения.")
    location_queryset = WarehouseLocation.objects
    if lock:
        location_queryset = location_queryset.select_for_update()
    location = location_queryset.filter(
        warehouse_code="MSK",
        location_code__iexact=code,
        zone_code="PR",
        is_active=True,
        is_fbs_visible=True,
        is_topology_visible=False,
    ).first()
    if location is None:
        raise FbsMovementError("Активный FBS PR-стеллаж по этому QR не найден.")
    queryset = FbsRack.objects.select_related("location")
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    rack = queryset.filter(
        location=location,
        is_active=True,
    ).first()
    if rack is None:
        raise FbsMovementError("Активный FBS PR-стеллаж по этому QR не найден.")
    return rack


def resolve_fbs_rack_cell_scan(scan_value: str, *, lock: bool = False) -> FbsRackCell:
    code = normalize_operational_location_scan(scan_value)
    if not code:
        raise FbsMovementError("Отсканируйте точную ячейку PR-стеллажа.")
    queryset = FbsRackCell.objects.select_related(
        "rack__location",
        "storage_cell__location",
    )
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    rack_cell = queryset.filter(
        storage_cell__location__warehouse_code="MSK",
        storage_cell__location__location_code__iexact=code,
        storage_cell__location__zone_code="PR",
        storage_cell__location__is_active=True,
        storage_cell__location__is_fbs_visible=True,
        storage_cell__is_active=True,
        rack__is_active=True,
        is_active=True,
    ).first()
    if rack_cell is None:
        exact_location = WarehouseLocation.objects.filter(
            warehouse_code="MSK",
            location_code__iexact=code,
            zone_code="PR",
            is_active=True,
            is_fbs_visible=True,
            is_topology_visible=False,
            fbs_rack__isnull=True,
        ).first()
        if exact_location is not None:
            from sklad.services.shared_location_catalog import ensure_exact_fbs_rack_cell

            rack_cell = ensure_exact_fbs_rack_cell(exact_location)
    if rack_cell is None:
        raise FbsMovementError("Активная ячейка FBS PR-стеллажа по этому QR не найдена.")
    return rack_cell


@transaction.atomic
def configure_fbs_rack(
    *,
    location_id: int,
    cell_count: int,
    created_by=None,
) -> FbsRack:
    """Create or resize exact scannable child cells below one additional PR location."""

    if not feature_enabled("module"):
        raise FbsFeatureDisabled("Модуль FBS выключен.")
    count = int(cell_count or 0)
    if count < 1 or count > 200:
        raise ValidationError("Количество ячеек FBS должно быть от 1 до 200.")
    location = WarehouseLocation.objects.select_for_update().get(pk=int(location_id))
    if (
        str(location.zone_code or "").strip().upper() != "PR"
        or location.is_topology_visible
        or not location.is_fbs_visible
        or not location.is_active
    ):
        raise ValidationError(
            "Ячейки FBS можно настроить только у активного дополнительного места PR, видимого в FBS."
        )
    actor = _actor(created_by)
    rack, created = FbsRack.objects.select_for_update().get_or_create(
        location=location,
        defaults={"created_by": actor, "is_active": True},
    )
    if created:
        rack.full_clean()
    elif not rack.is_active:
        rack.is_active = True
        rack.save(update_fields=["is_active", "updated_at"])

    current_cells = {
        int(cell.position): cell
        for cell in FbsRackCell.objects.select_for_update()
        .select_related("storage_cell__location")
        .filter(rack=rack)
        .order_by("position")
    }
    for position in range(1, count + 1):
        suffix = f"-{position:02d}"
        child_code = f"{location.location_code}{suffix}"
        if len(child_code) > 64:
            raise ValidationError(
                "Код PR-стеллажа слишком длинный для добавления номера ячейки."
            )
        rack_cell = current_cells.get(position)
        if rack_cell is None:
            if WarehouseLocation.objects.filter(
                warehouse_code=location.warehouse_code,
                location_code__iexact=child_code,
            ).exists():
                raise ValidationError(
                    f"Место {child_code} уже существует и не принадлежит этому FBS-стеллажу."
                )
            child_location = WarehouseLocation.objects.create(
                warehouse_code=location.warehouse_code,
                zone_code="PR",
                zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
                row_no=0,
                section_no=0,
                tier_no=0,
                cell_no=0,
                location_code=child_code,
                display_name=f"{location.display_name or location.location_code} · ячейка {position:02d}",
                capacity_containers=0,
                is_topology_visible=False,
                is_fbs_visible=True,
                is_active=True,
                is_storage=False,
                is_pickable=True,
                is_processing=False,
                is_shipping=False,
                is_loading=False,
            )
            storage_cell = FbsStorageCell(
                cell_code=child_code,
                location=child_location,
                purpose=FbsStorageCell.PURPOSE_PICK,
                client_cluster=0,
                is_active=True,
            )
            storage_cell.full_clean()
            storage_cell.save()
            rack_cell = FbsRackCell(
                rack=rack,
                storage_cell=storage_cell,
                position=position,
                is_active=True,
            )
            rack_cell.full_clean()
            rack_cell.save()
            current_cells[position] = rack_cell
        else:
            child_location = rack_cell.storage_cell.location
            location_updates = []
            for field, value in (
                ("is_active", True),
                ("is_fbs_visible", True),
                ("is_pickable", True),
            ):
                if getattr(child_location, field) != value:
                    setattr(child_location, field, value)
                    location_updates.append(field)
            if location_updates:
                child_location.save(update_fields=[*location_updates, "updated_at"])
            if not rack_cell.storage_cell.is_active:
                rack_cell.storage_cell.is_active = True
                rack_cell.storage_cell.save(update_fields=["is_active", "updated_at"])
            if not rack_cell.is_active:
                rack_cell.is_active = True
                rack_cell.save(update_fields=["is_active", "updated_at"])

    for position, rack_cell in current_cells.items():
        if position <= count or not rack_cell.is_active:
            continue
        occupied = FbsStockBalance.objects.filter(
            box__rack_cell_binding__rack_cell=rack_cell,
            qty__gt=0,
        ).exists()
        if occupied:
            raise ValidationError(
                f"Нельзя убрать ячейку {rack_cell.cell_code}: в ней есть FBS-остаток."
            )
        rack_cell.is_active = False
        rack_cell.save(update_fields=["is_active", "updated_at"])
        rack_cell.storage_cell.is_active = False
        rack_cell.storage_cell.save(update_fields=["is_active", "updated_at"])
        child_location = rack_cell.storage_cell.location
        child_location.is_active = False
        child_location.save(update_fields=["is_active", "updated_at"])
    return rack


def _binding_codes(rack_cell: FbsRackCell, agency_id: int) -> tuple[str, str]:
    stem = f"R{rack_cell.rack_id}-C{rack_cell.position:03d}-A{int(agency_id)}"
    return f"FBS-RACK-PAL-{stem}", f"FBS-RACK-BOX-{stem}"


def _get_or_create_binding(
    *,
    rack_cell: FbsRackCell,
    agency,
    actor,
    source_box: FbsBox | None = None,
) -> FbsRackCellBinding:
    binding = (
        FbsRackCellBinding.objects.select_for_update(of=("self",))
        .select_related("pallet", "box__source_container")
        .filter(rack_cell=rack_cell, agency=agency)
        .first()
    )
    target_location = rack_cell.storage_cell.location
    occupancy_message = general_warehouse_location_occupancy_message(target_location)
    if occupancy_message:
        raise FbsMovementError(f"Ячейка недоступна: {occupancy_message}")
    if binding is not None:
        if binding.pallet.status not in ACTIVE_PALLET_STATUSES:
            raise FbsMovementError("Системная FBS-паллета этой ячейки находится в архиве.")
        if binding.box.status not in ACTIVE_BOX_STATUSES:
            raise FbsMovementError("Системный FBS-короб этой ячейки находится в архиве.")
        container = binding.box.source_container
        if container is None:
            raise FbsMovementError("У системного короба ячейки отсутствует физический контейнер.")
        if container.current_location_id != target_location.id:
            raise FbsMovementError("Физический адрес системного короба ячейки поврежден.")
        return binding

    pallet_code, box_code = _binding_codes(rack_cell, agency.id)
    if FbsRackCellBinding.objects.filter(rack_cell=rack_cell, agency=agency).exists():
        raise FbsMovementError("Привязка клиента к ячейке уже создается. Повторите скан.")
    pallet_container = WarehouseContainer.objects.create(
        agency=agency,
        container_type=WarehouseContainer.TYPE_PALLET,
        container_code=pallet_code,
        current_location=target_location,
        status=WarehouseContainer.STATUS_ACTIVE,
        source_context_type="fbs_rack_cell",
        source_context_id=str(rack_cell.id),
        created_by=actor,
    )
    pallet = FbsPallet(
        agency=agency,
        pallet_code=pallet_code,
        cell=rack_cell.storage_cell,
        warehouse_container=pallet_container,
        max_boxes=1,
        is_rack_binding=True,
        status=FbsPallet.STATUS_ACTIVE,
    )
    pallet.full_clean()
    pallet.save()
    if source_box is None:
        box_container = WarehouseContainer.objects.create(
            agency=agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code=box_code,
            parent_container=pallet_container,
            current_location=target_location,
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type="fbs_rack_cell",
            source_context_id=str(rack_cell.id),
            created_by=actor,
        )
        box = FbsBox(
            agency=agency,
            pallet=pallet,
            box_code=box_code,
            source_container=box_container,
            status=FbsBox.STATUS_ACTIVE,
        )
        box.full_clean()
        box.save()
    else:
        box = (
            FbsBox.objects.select_for_update(of=("self",))
            .select_related("source_container")
            .get(pk=source_box.pk)
        )
        if box.agency_id != agency.id or box.status not in ACTIVE_BOX_STATUSES:
            raise FbsMovementError("Входящий FBS-короб недоступен для этой ячейки.")
        if FbsRackCellBinding.objects.filter(box=box).exists():
            raise FbsMovementError("Входящий FBS-короб уже привязан к другой PR-ячейке.")
        box_container = box.source_container
        if (
            box_container is None
            or box_container.agency_id != agency.id
            or box_container.container_type != WarehouseContainer.TYPE_BOX
            or box_container.status != WarehouseContainer.STATUS_ACTIVE
        ):
            raise FbsMovementError("Физический входящий короб поврежден или неактивен.")
        box.pallet = pallet
        box.status = FbsBox.STATUS_ACTIVE
        box.full_clean()
        box.save(update_fields=["pallet", "status", "updated_at"])
        box_container.parent_container = pallet_container
        box_container.current_location = target_location
        box_container.source_context_type = "fbs_rack_cell"
        box_container.source_context_id = str(rack_cell.id)
        box_container.save(
            update_fields=[
                "parent_container",
                "current_location",
                "source_context_type",
                "source_context_id",
                "updated_at",
            ]
        )
    binding = FbsRackCellBinding(
        rack_cell=rack_cell,
        agency=agency,
        pallet=pallet,
        box=box,
        created_by=actor,
    )
    binding.full_clean()
    try:
        binding.save()
    except IntegrityError as exc:
        raise FbsMovementError("Не удалось безопасно создать привязку клиента к ячейке.") from exc
    return binding


@transaction.atomic
def bind_client_movement_box_to_rack_cell(
    *,
    source_box: FbsBox,
    target_cell_scan: str,
    agency,
    performed_by,
) -> FbsRackCellBinding:
    """Bind one incoming client-movement box to an exact PR picking cell."""

    _require_writes()
    actor = _actor(performed_by)
    if actor is None:
        raise FbsMovementError("Не указан сотрудник, размещающий короб в PR-ячейку.")
    rack_cell = resolve_fbs_rack_cell_scan(target_cell_scan, lock=True)
    if FbsRackCellBinding.objects.select_for_update(of=("self",)).filter(
        rack_cell=rack_cell,
    ).exclude(agency=agency).exists():
        raise FbsMovementError(
            "В выбранной PR-ячейке уже находится товар другого клиента."
        )
    locked_box = (
        FbsBox.objects.select_for_update(of=("self",))
        .select_related("source_container")
        .get(pk=source_box.pk)
    )
    if locked_box.agency_id != agency.id:
        raise FbsMovementError("Входящий FBS-короб принадлежит другому клиенту.")
    return _get_or_create_binding(
        rack_cell=rack_cell,
        agency=agency,
        actor=actor,
        source_box=locked_box,
    )


def _assert_source_free(
    box: FbsBox,
    balances: list[FbsStockBalance],
    *,
    allow_untouched_reservations: bool = False,
) -> tuple[int, ...]:
    from .external_issues import assert_no_external_activity
    assert_no_external_activity(FbsStockBalance.objects.filter(box=box).values("id"))
    if not balances or sum(int(row.qty or 0) for row in balances) <= 0:
        raise FbsMovementError("В исходном FBS-коробе нет товара для перемещения.")
    queued_batch_ids: tuple[int, ...] = ()
    if allow_untouched_reservations:
        reservation_state = relocation_reservation_state(
            [row.id for row in balances],
            lock=True,
        )
        if reservation_state.blockers:
            raise FbsMovementError(reservation_state.blockers[0])
        queued_batch_ids = reservation_state.queued_batch_ids
        reserved_by_balance = {
            int(row["balance_id"]): int(row["qty"] or 0)
            for row in FbsOrderStockAllocation.objects.filter(
                balance_id__in=[balance.id for balance in balances],
                status__in=(
                    FbsOrderStockAllocation.STATUS_RESERVED,
                    FbsOrderStockAllocation.STATUS_PICKING,
                ),
            )
            .values("balance_id")
            .annotate(qty=Sum("qty_reserved"))
            .order_by()
        }
        for balance in balances:
            qty = int(balance.qty or 0)
            available_qty = int(balance.available_qty or 0)
            reserved_qty = int(balance.reserved_qty or 0)
            allocated_qty = int(reserved_by_balance.get(int(balance.id), 0) or 0)
            if available_qty + reserved_qty != qty or allocated_qty != reserved_qty:
                raise FbsMovementError(
                    "Остаток или резерв исходного короба не согласован с FBS-заказами. "
                    "Требуется проверка склада."
                )
    else:
        if any(
            int(row.reserved_qty or 0) > 0
            or int(row.available_qty or 0) != int(row.qty or 0)
            for row in balances
        ):
            raise FbsMovementError(
                "В исходном коробе есть зарезервированный или уже отбираемый товар."
            )
        if FbsOrderStockAllocation.objects.filter(
            balance__box=box,
            status__in=(
                FbsOrderStockAllocation.STATUS_RESERVED,
                FbsOrderStockAllocation.STATUS_PICKING,
            ),
        ).exists():
            raise FbsMovementError("Исходный короб участвует в активном FBS-отборе.")
    if FbsInternalMovement.objects.filter(
        Q(source_box=box) | Q(target_box=box),
        status__in=ACTIVE_MOVEMENT_STATUSES,
    ).exists():
        raise FbsMovementError("У исходного короба уже есть внутреннее перемещение.")
    if FbsReplenishmentPlan.objects.filter(
        target_box=box,
        status__in=ACTIVE_REPLENISHMENT_STATUSES,
    ).exists():
        raise FbsMovementError("Исходный короб участвует в действующем плане пополнения.")
    return queued_batch_ids


def _move_balance_row(
    *,
    source: FbsStockBalance,
    target_box: FbsBox,
    target_by_identity: dict[str, FbsStockBalance],
) -> None:
    from .external_issues import assert_no_external_activity
    assert_no_external_activity([source.pk])
    target = target_by_identity.get(source.identity_key)
    if target is not None:
        assert_no_external_activity([target.pk])
    if target is None:
        source.box = target_box
        source.save(update_fields=["box", "updated_at"])
        target_by_identity[source.identity_key] = source
        return
    if source.marking_code:
        raise FbsMovementError(
            "В ячейке уже есть строка с тем же идентификатором маркированного товара."
        )
    target.qty = int(target.qty or 0) + int(source.qty or 0)
    target.available_qty = int(target.available_qty or 0) + int(source.available_qty or 0)
    target.save(update_fields=["qty", "available_qty", "updated_at"])
    source.qty = 0
    source.available_qty = 0
    source.reserved_qty = 0
    source.save(update_fields=["qty", "available_qty", "reserved_qty", "updated_at"])


def _resolve_scanned_balance(
    *,
    balances: list[FbsStockBalance],
    item_scan: str,
    allow_marked_barcode: bool = False,
) -> FbsStockBalance:
    canonical_scan = _canonical_item_scan(item_scan)
    if not canonical_scan:
        raise FbsMovementError("Отсканируйте одну единицу товара: ШК или КИЗ.")

    marking_matches = [
        row
        for row in balances
        if str(row.marking_code or "").strip()
        and _canonical_item_scan(row.marking_code) == canonical_scan
    ]
    if len(marking_matches) == 1:
        return marking_matches[0]
    if len(marking_matches) > 1:
        raise FbsMovementError(
            "Один КИЗ найден в нескольких строках исходного короба. Перемещение отменено."
        )

    looks_like_kiz = bool(
        len(canonical_scan) >= 18
        and canonical_scan.startswith("01")
        and canonical_scan[2:16].isdigit()
        and canonical_scan[16:18] == "21"
    )
    if looks_like_kiz:
        raise FbsMovementError(
            "Этот КИЗ не числится в отсканированном исходном коробе. Перемещение отменено."
        )

    normalized_scan = normalize_barcode(canonical_scan)
    barcode_matches = []
    for row in balances:
        barcode = str(row.barcode or "").strip()
        if not barcode:
            continue
        if normalize_barcode(barcode) == normalized_scan or (
            row.sku_ref_id
            and same_sku_barcode_variant(
                sku_id=row.sku_ref_id,
                first=barcode,
                second=canonical_scan,
            )
        ):
            barcode_matches.append(row)
    if not barcode_matches:
        raise FbsMovementError(
            "Товар с таким ШК не числится в отсканированном исходном коробе."
        )
    marked_matches = [
        row for row in barcode_matches if str(row.marking_code or "").strip()
    ]
    if marked_matches and not allow_marked_barcode:
        raise FbsMovementError(
            "Для маркированного товара отсканируйте КИЗ Data Matrix, а не штрихкод товара."
        )
    if marked_matches:
        if len(marked_matches) != len(barcode_matches):
            raise FbsMovementError(
                "По этому ШК в исходном коробе одновременно найден маркированный и "
                "немаркированный товар. Требуется проверка кладовщика."
            )
        if any(int(row.qty or 0) != 1 for row in marked_matches):
            raise FbsMovementError(
                "Учёт маркированного товара в исходном коробе повреждён: один КИЗ "
                "должен соответствовать одной единице."
            )
        identity_groups = {
            (
                int(row.sku_ref_id or 0),
                str(row.sku_code or "").strip().casefold(),
                str(row.size or "").strip().casefold(),
                str(row.goods_type or "").strip().casefold(),
                str(row.lot_code or "").strip().casefold(),
                row.expiry_date,
            )
            for row in marked_matches
        }
        if len(identity_groups) > 1:
            raise FbsMovementError(
                "По этому ШК в исходном коробе найдено несколько товаров, партий "
                "или сроков годности. Однозначно выбрать единицу нельзя."
            )
        return min(
            marked_matches,
            key=lambda row: (
                0
                if int(row.available_qty or 0) > 0
                and int(row.reserved_qty or 0) == 0
                else 1,
                int(row.id or 0),
            ),
        )
    if len(barcode_matches) > 1:
        raise FbsMovementError(
            "По этому ШК в исходном коробе найдено несколько партий или учётных строк. "
            "Однозначно выбрать товар нельзя; требуется проверка кладовщика."
        )
    return barcode_matches[0]


def _move_one_balance_unit(
    *,
    source: FbsStockBalance,
    target_box: FbsBox,
    performed_by=None,
    allow_untouched_reservations: bool = False,
) -> dict[str, object]:
    qty = int(source.qty or 0)
    available_qty = int(source.available_qty or 0)
    reserved_qty = int(source.reserved_qty or 0)
    if qty <= 0:
        raise FbsMovementError("В исходном коробе нет единицы этого товара.")
    if not allow_untouched_reservations and (
        reserved_qty > 0 or available_qty != qty
    ):
        raise FbsMovementError("Эта учётная строка зарезервирована или уже участвует в отборе.")
    if allow_untouched_reservations and available_qty + reserved_qty != qty:
        raise FbsMovementError(
            "Остаток исходного короба не согласован с резервом. Требуется проверка склада."
        )

    reservation = None
    if allow_untouched_reservations and reserved_qty > 0:
        reservation = (
            FbsOrderStockAllocation.objects.select_for_update(of=("self",))
            .filter(
                balance=source,
                status=FbsOrderStockAllocation.STATUS_RESERVED,
                qty_picked=0,
            )
            .order_by("id")
            .first()
        )
        if reservation is None:
            raise FbsMovementError(
                "Резерв исходного короба не связан с неначатым FBS-заказом."
            )
    elif available_qty <= 0:
        raise FbsMovementError("В исходном коробе нет доступной единицы этого товара.")

    target = (
        FbsStockBalance.objects.select_for_update()
        .filter(box=target_box, identity_key=source.identity_key)
        .first()
    )
    if target is None and qty == 1:
        source.box = target_box
        source.save(update_fields=["box", "updated_at"])
        return {
            "target_balance_id": int(source.id),
            "reservation_moved": reservation is not None,
            "allocation_id": int(reservation.id) if reservation is not None else 0,
        }
    if source.marking_code:
        raise FbsMovementError("КИЗ уже существует в целевой ячейке. Перемещение отменено.")
    if target is None:
        target = FbsStockBalance.objects.create(
            agency=source.agency,
            box=target_box,
            sku_ref=source.sku_ref,
            identity_key=source.identity_key,
            sku_code=source.sku_code,
            name=source.name,
            size=source.size,
            barcode=source.barcode,
            goods_type=source.goods_type,
            marking_code="",
            lot_code=source.lot_code,
            expiry_date=source.expiry_date,
            qty=0,
            available_qty=0,
            reserved_qty=0,
        )
    target.qty = int(target.qty or 0) + 1
    source.qty = qty - 1
    if reservation is None:
        target.available_qty = int(target.available_qty or 0) + 1
        source.available_qty = available_qty - 1
        target.save(update_fields=["qty", "available_qty", "updated_at"])
        source.save(update_fields=["qty", "available_qty", "updated_at"])
        return {
            "target_balance_id": int(target.id),
            "reservation_moved": False,
            "allocation_id": 0,
        }

    source_trace = (
        FbsOrderTraceability.objects.select_for_update()
        .filter(allocation=reservation)
        .first()
    )
    if (
        source_trace is None
        or source_trace.status != FbsOrderTraceability.STATUS_RESERVED
        or int(source_trace.qty or 0) != int(reservation.qty_reserved or 0)
        or bool(source_trace.marking_code)
    ):
        raise FbsMovementError(
            "Трассировка резерва не согласована с FBS-заказом. Требуется проверка склада."
        )
    target_reservation = (
        FbsOrderStockAllocation.objects.select_for_update(of=("self",))
        .filter(
            order_item_id=reservation.order_item_id,
            balance=target,
            status=FbsOrderStockAllocation.STATUS_RESERVED,
        )
        .first()
    )
    if target_reservation is not None:
        target_trace = (
            FbsOrderTraceability.objects.select_for_update()
            .filter(allocation=target_reservation)
            .first()
        )
        if (
            target_reservation.pick_task_id != reservation.pick_task_id
            or int(target_reservation.qty_picked or 0) != 0
            or target_trace is None
            or target_trace.status != FbsOrderTraceability.STATUS_RESERVED
            or int(target_trace.qty or 0) != int(target_reservation.qty_reserved or 0)
            or bool(target_trace.marking_code)
        ):
            raise FbsMovementError(
                "Целевой резерв этого заказа изменился. Обновите экран и повторите операцию."
            )
        target_reservation.qty_reserved = int(target_reservation.qty_reserved or 0) + 1
        target_reservation.save(update_fields=["qty_reserved", "updated_at"])
        target_trace.qty = int(target_trace.qty or 0) + 1
        target_trace.save(update_fields=["qty", "updated_at"])
        if int(reservation.qty_reserved or 0) == 1:
            reservation.status = FbsOrderStockAllocation.STATUS_RELEASED
            reservation.released_by = _actor(performed_by)
            reservation.released_at = timezone.now()
            reservation.save(
                update_fields=["status", "released_by", "released_at", "updated_at"]
            )
            source_trace.status = FbsOrderTraceability.STATUS_RELEASED
            source_trace.save(update_fields=["status", "updated_at"])
        else:
            reservation.qty_reserved = int(reservation.qty_reserved or 0) - 1
            reservation.save(update_fields=["qty_reserved", "updated_at"])
            source_trace.qty = int(source_trace.qty or 0) - 1
            source_trace.save(update_fields=["qty", "updated_at"])
        moved_allocation_id = int(target_reservation.id)
    elif int(reservation.qty_reserved or 0) == 1:
        reservation.balance = target
        reservation.full_clean()
        reservation.save(update_fields=["balance", "updated_at"])
        moved_allocation_id = int(reservation.id)
    else:
        reservation.qty_reserved = int(reservation.qty_reserved or 0) - 1
        reservation.save(update_fields=["qty_reserved", "updated_at"])
        source_trace.qty = int(source_trace.qty or 0) - 1
        source_trace.save(update_fields=["qty", "updated_at"])
        moved_reservation = FbsOrderStockAllocation(
            order_item_id=reservation.order_item_id,
            balance=target,
            pick_task_id=reservation.pick_task_id,
            reserved_by_id=reservation.reserved_by_id,
            qty_reserved=1,
            qty_picked=0,
            status=FbsOrderStockAllocation.STATUS_RESERVED,
        )
        moved_reservation.full_clean()
        moved_reservation.save()
        FbsOrderTraceability.objects.create(
            allocation=moved_reservation,
            marking_code="",
            lot_code=source_trace.lot_code,
            expiry_date=source_trace.expiry_date,
            qty=1,
            status=FbsOrderTraceability.STATUS_RESERVED,
        )
        moved_allocation_id = int(moved_reservation.id)

    target.reserved_qty = int(target.reserved_qty or 0) + 1
    source.reserved_qty = reserved_qty - 1
    target.save(update_fields=["qty", "reserved_qty", "updated_at"])
    source.save(update_fields=["qty", "reserved_qty", "updated_at"])
    return {
        "target_balance_id": int(target.id),
        "reservation_moved": True,
        "allocation_id": moved_allocation_id,
    }


def _validated_direct_fbs_box_source_location(source_box: FbsBox) -> WarehouseLocation:
    """Trust the scanned FBS box's physical address, not its technical pallet."""
    source_container = source_box.source_container
    source_location = getattr(source_container, "current_location", None)
    if source_container is None or source_location is None:
        raise FbsMovementError("У исходного FBS-короба отсутствует физический адрес.")
    if source_container.status != WarehouseContainer.STATUS_ACTIVE:
        raise FbsMovementError("Физический контейнер исходного FBS-короба не активен.")
    source_zone = str(source_location.zone_code or "").strip().upper()
    if (
        not source_location.is_active
        or str(source_location.warehouse_code or "").strip().upper() != "MSK"
        or source_zone not in DIRECT_FBS_BOX_SOURCE_ZONES
    ):
        raise FbsMovementError(
            "Исходный FBS-короб находится вне разрешённых зон FBS, OS или PR."
        )
    return source_location


@transaction.atomic
def move_scanned_box_item_to_rack_cell(
    *,
    source_box_scan: str,
    target_cell_scan: str,
    item_scan: str,
    performed_by,
    idempotency_key: str,
) -> FbsRackContentMovement:
    """Move exactly one scanned, verified FBS unit into an exact PR rack cell."""

    _require_writes()
    actor = _actor(performed_by)
    if actor is None:
        raise FbsMovementError("Не указан сотрудник, выполняющий перемещение.")
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 64:
        raise FbsMovementError("Недействительный ключ операции. Обновите экран и повторите скан.")
    fingerprint = _item_scan_fingerprint(item_scan)
    if not _canonical_item_scan(item_scan):
        raise FbsMovementError("Отсканируйте одну единицу товара: ШК или КИЗ.")
    prior = (
        FbsRackContentMovement.objects.select_related(
            "source_box", "target_cell__storage_cell__location", "operation"
        )
        .filter(idempotency_key=key)
        .first()
    )
    if prior is not None:
        prior_event = WarehouseEvent.objects.filter(
            operation=prior.operation,
            event_type="fbs_rack_item_placed",
        ).order_by("id").first()
        prior_fingerprint = (
            str((prior_event.payload or {}).get("item_scan_fingerprint") or "")
            if prior_event is not None
            else ""
        )
        if (
            prior.source_box.box_code.upper() != _normalized_box_scan(source_box_scan)
            or prior.target_cell.cell_code.upper()
            != normalize_operational_location_scan(target_cell_scan)
            or prior_fingerprint != fingerprint
        ):
            raise FbsMovementError("Ключ операции уже использован для другого перемещения.")
        prior.source_remaining_qty = int(
            prior.source_box.stock_balances.aggregate(total=Sum("qty"))["total"] or 0
        )
        return prior

    source_match = _resolve_source_box(source_box_scan)
    source_box = (
        FbsBox.objects.select_for_update(of=("self",))
        .select_related(
            "agency",
            "source_container__current_location",
        )
        .get(pk=source_match.pk)
    )
    if FbsRackCellBinding.objects.filter(box=source_box).exists():
        raise FbsMovementError(
            "На этом экране доступно только размещение из обычного FBS-короба в PR-ячейку."
        )
    rack_cell = resolve_fbs_rack_cell_scan(target_cell_scan, lock=True)
    staging = (
        FbsRackStagingBox.objects.select_for_update()
        .select_related("rack")
        .filter(box=source_box, status=FbsRackStagingBox.STATUS_AWAITING)
        .first()
    )
    if staging is not None and staging.rack_id != rack_cell.rack_id:
        raise FbsMovementError(
            f"Короб доставлен к стеллажу {staging.rack.rack_code}. "
            "Отсканируйте ячейку именно этого стеллажа."
        )
    source_location = _validated_direct_fbs_box_source_location(source_box)
    if staging is not None and source_location.id != staging.rack.location_id:
        raise FbsMovementError("Физический адрес короба изменился после доставки к стеллажу.")
    if WarehouseOperationTask.objects.filter(
        container_id=source_box.source_container_id,
        status__in=(
            WarehouseOperationTask.STATUS_CREATED,
            WarehouseOperationTask.STATUS_IN_PROGRESS,
        ),
    ).exists():
        raise FbsMovementError("Исходный FBS-короб участвует в активной складской операции.")

    balances = list(
        FbsStockBalance.objects.select_for_update(of=("self",))
        .filter(box=source_box, qty__gt=0)
        .select_related("sku_ref")
        .order_by("id")
    )
    queued_batch_ids = _assert_source_free(
        source_box,
        balances,
        allow_untouched_reservations=True,
    )
    validate_boxes_unlocked([source_box.id], for_execution=True)
    source_balance = _resolve_scanned_balance(
        balances=balances,
        item_scan=item_scan,
        allow_marked_barcode=True,
    )
    marking_scan_verified = bool(
        str(source_balance.marking_code or "").strip()
        and _canonical_item_scan(source_balance.marking_code)
        == _canonical_item_scan(item_scan)
    )
    binding = _get_or_create_binding(
        rack_cell=rack_cell,
        agency=source_box.agency,
        actor=actor,
    )
    if binding.box_id == source_box.id:
        raise FbsMovementError("Исходный короб уже является системным коробом этой ячейки.")
    validate_boxes_unlocked([binding.box_id], for_execution=True)

    total_before = int(
        FbsStockBalance.objects.filter(
            box_id__in=(source_box.id, binding.box_id)
        ).aggregate(total=Sum("qty"))["total"]
        or 0
    )
    source_before = int(
        FbsStockBalance.objects.filter(box=source_box).aggregate(total=Sum("qty"))["total"]
        or 0
    )
    move_details = _move_one_balance_unit(
        source=source_balance,
        target_box=binding.box,
        performed_by=actor,
        allow_untouched_reservations=True,
    )
    source_after = int(
        FbsStockBalance.objects.filter(box=source_box).aggregate(total=Sum("qty"))["total"]
        or 0
    )
    total_after = int(
        FbsStockBalance.objects.filter(
            box_id__in=(source_box.id, binding.box_id)
        ).aggregate(total=Sum("qty"))["total"]
        or 0
    )
    if source_after != source_before - 1 or total_before != total_after:
        raise FbsMovementError("Контрольная сумма остатка не совпала. Перемещение отменено.")

    now = timezone.now()
    if source_after == 0:
        source_box.status = FbsBox.STATUS_ARCHIVED
        source_box.save(update_fields=["status", "updated_at"])
        source_box.source_container.status = WarehouseContainer.STATUS_ARCHIVED
        source_box.source_container.save(update_fields=["status", "updated_at"])
        if staging is not None:
            staging.status = FbsRackStagingBox.STATUS_PLACED
            staging.placed_by = actor
            staging.placed_at = now
            staging.save(
                update_fields=["status", "placed_by", "placed_at", "updated_at"]
            )

    target_location = rack_cell.storage_cell.location
    operation = WarehouseOperation.objects.create(
        agency=source_box.agency,
        operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
        context_type="fbs_rack_content_movement",
        context_id=key,
        source_document_type="fbs_box",
        source_document_id=str(source_box.id),
        source_location=source_location,
        destination_location=target_location,
        source_zone_code=str(getattr(source_location, "zone_code", "") or ""),
        destination_zone_code="PR",
        status=WarehouseOperation.STATUS_DONE,
        requested_by=actor,
        requested_by_role="picker",
        assigned_executor_role="picker",
        comment=(
            f"Поштучное размещение {source_balance.sku_code or source_balance.barcode} "
            f"из FBS-короба {source_box.box_code} в {rack_cell.cell_code}"
        ),
        planned_qty=1,
        done_qty=1,
        started_at=now,
        completed_at=now,
    )
    movement = FbsRackContentMovement(
        idempotency_key=key,
        agency=source_box.agency,
        source_box=source_box,
        target_cell=rack_cell,
        target_binding=binding,
        operation=operation,
        moved_qty=1,
        performed_by=actor,
    )
    movement.full_clean()
    movement.save()
    WarehouseEvent.objects.create(
        agency=source_box.agency,
        event_type="fbs_rack_item_placed",
        stock_context_type="fbs_rack_content_movement",
        stock_context_id=str(movement.id),
        container=source_box.source_container,
        operation=operation,
        source_document_type="fbs_box",
        source_document_id=str(source_box.id),
        from_location=source_location,
        to_location=target_location,
        from_zone_code=str(getattr(source_location, "zone_code", "") or ""),
        to_zone_code="PR",
        qty=1,
        payload={
            "free_move": True,
            "fbs_rack_move": True,
            "per_item": True,
            "source_box_id": int(source_box.id),
            "source_box_code": source_box.box_code,
            "source_location_mode": "physical_fbs_box",
            "source_location_id": int(source_location.id),
            "source_location_code": str(source_location.location_code or ""),
            "source_balance_id": int(source_balance.id),
            "target_balance_id": int(move_details["target_balance_id"]),
            "reservation_moved": bool(move_details["reservation_moved"]),
            "reservation_allocation_id": int(move_details["allocation_id"]),
            "queued_batch_ids": list(queued_batch_ids),
            "sku_code": source_balance.sku_code,
            "barcode": source_balance.barcode,
            "marking_verified": marking_scan_verified,
            "marking_resolution_mode": (
                "exact_kiz"
                if marking_scan_verified
                else "barcode_auto_selected"
                if str(source_balance.marking_code or "").strip()
                else "barcode"
            ),
            "item_scan_fingerprint": fingerprint,
            "rack_id": int(rack_cell.rack_id),
            "rack_code": rack_cell.rack.rack_code,
            "rack_cell_id": int(rack_cell.id),
            "rack_cell_code": rack_cell.cell_code,
            "target_box_id": int(binding.box_id),
            "target_box_code": binding.box.box_code,
            "agency_id": int(source_box.agency_id),
            "source_remaining_qty": source_after,
        },
        performed_by=actor,
        performed_by_role="picker",
        occurred_at=now,
    )
    movement.moved_sku_code = source_balance.sku_code
    movement.source_remaining_qty = source_after
    return movement


@transaction.atomic
def move_scanned_rack_item_to_rack_cell(
    *,
    source_cell_scan: str,
    target_cell_scan: str,
    item_scan: str,
    performed_by,
    idempotency_key: str,
) -> FbsRackContentMovement:
    """Move one verified free unit between two exact FBS PR rack cells."""

    _require_writes()
    actor = _actor(performed_by)
    if actor is None:
        raise FbsMovementError("Не указан сотрудник, выполняющий перемещение.")
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 64:
        raise FbsMovementError("Недействительный ключ операции. Обновите экран и повторите скан.")
    source_scan = normalize_operational_location_scan(source_cell_scan)
    target_scan = normalize_operational_location_scan(target_cell_scan)
    fingerprint = _item_scan_fingerprint(item_scan)
    if not source_scan:
        raise FbsMovementError("Отсканируйте исходную ячейку PR-стеллажа.")
    if not target_scan:
        raise FbsMovementError("Отсканируйте целевую ячейку PR-стеллажа.")
    if not _canonical_item_scan(item_scan):
        raise FbsMovementError("Отсканируйте одну единицу товара: ШК или КИЗ.")

    prior = (
        FbsRackContentMovement.objects.select_related(
            "source_box",
            "target_cell__storage_cell__location",
            "operation",
        )
        .filter(idempotency_key=key)
        .first()
    )
    if prior is not None:
        prior_source_binding = (
            FbsRackCellBinding.objects.select_related(
                "rack_cell__storage_cell__location",
            )
            .filter(box=prior.source_box)
            .first()
        )
        prior_event = WarehouseEvent.objects.filter(
            operation=prior.operation,
            event_type="fbs_rack_item_relocated",
        ).order_by("id").first()
        prior_fingerprint = (
            str((prior_event.payload or {}).get("item_scan_fingerprint") or "")
            if prior_event is not None
            else ""
        )
        if (
            prior_source_binding is None
            or prior_source_binding.rack_cell.cell_code.upper() != source_scan
            or prior.target_cell.cell_code.upper() != target_scan
            or prior_fingerprint != fingerprint
        ):
            raise FbsMovementError("Ключ операции уже использован для другого перемещения.")
        prior.source_cell_code = prior_source_binding.rack_cell.cell_code
        prior.source_cell_remaining_qty = int(
            FbsStockBalance.objects.filter(
                box__rack_cell_binding__rack_cell=prior_source_binding.rack_cell,
            ).aggregate(total=Sum("qty"))["total"]
            or 0
        )
        return prior

    source_candidate = resolve_fbs_rack_cell_scan(source_cell_scan)
    target_candidate = resolve_fbs_rack_cell_scan(target_cell_scan)
    if source_candidate.pk == target_candidate.pk:
        raise FbsMovementError("Исходная и целевая ячейки совпадают.")
    locked_cells = list(
        FbsRackCell.objects.select_for_update(of=("self",))
        .select_related(
            "rack__location",
            "storage_cell__location",
        )
        .filter(pk__in=(source_candidate.pk, target_candidate.pk))
        .order_by("pk")
    )
    cells_by_id = {cell.pk: cell for cell in locked_cells}
    source_cell = cells_by_id.get(source_candidate.pk)
    target_cell = cells_by_id.get(target_candidate.pk)
    if source_cell is None or target_cell is None:
        raise FbsMovementError("Одна из ячеек изменилась во время сканирования. Повторите операцию.")
    if not source_cell.is_active or not target_cell.is_active:
        raise FbsMovementError("Одна из выбранных PR-ячеек выключена.")

    candidate_balances = list(
        FbsStockBalance.objects.filter(
            box__rack_cell_binding__rack_cell=source_cell,
            box__status__in=ACTIVE_BOX_STATUSES,
            box__pallet__status__in=ACTIVE_PALLET_STATUSES,
            qty__gt=0,
        )
        .select_related("box", "sku_ref")
        .order_by("id")
    )
    if not candidate_balances:
        raise FbsMovementError("В исходной ячейке нет товара для перемещения.")
    candidate_balance = _resolve_scanned_balance(
        balances=candidate_balances,
        item_scan=item_scan,
    )
    source_box = (
        FbsBox.objects.select_for_update(of=("self",))
        .select_related(
            "agency",
            "pallet__cell__location",
            "source_container__current_location",
        )
        .get(pk=candidate_balance.box_id)
    )
    source_binding = (
        FbsRackCellBinding.objects.select_for_update(of=("self",))
        .select_related("rack_cell__storage_cell__location")
        .filter(
            rack_cell=source_cell,
            box=source_box,
            agency=source_box.agency,
        )
        .first()
    )
    if source_binding is None:
        raise FbsMovementError("Учётная привязка товара к исходной ячейке изменилась.")
    source_location = source_cell.storage_cell.location
    source_container = source_box.source_container
    if source_container is None or source_container.current_location_id != source_location.id:
        raise FbsMovementError("Физический адрес системного короба исходной ячейки поврежден.")
    if source_container.status != WarehouseContainer.STATUS_ACTIVE:
        raise FbsMovementError("Системный короб исходной ячейки неактивен.")
    if WarehouseOperationTask.objects.filter(
        container_id=source_box.source_container_id,
        status__in=(
            WarehouseOperationTask.STATUS_CREATED,
            WarehouseOperationTask.STATUS_IN_PROGRESS,
        ),
    ).exists():
        raise FbsMovementError("Товар исходной ячейки участвует в активной складской операции.")

    balances = list(
        FbsStockBalance.objects.select_for_update(of=("self",))
        .filter(box=source_box, qty__gt=0)
        .select_related("sku_ref")
        .order_by("id")
    )
    queued_batch_ids = _assert_source_free(
        source_box,
        balances,
        allow_untouched_reservations=True,
    )
    validate_boxes_unlocked([source_box.id], for_execution=True)
    source_balance = _resolve_scanned_balance(balances=balances, item_scan=item_scan)
    target_binding = _get_or_create_binding(
        rack_cell=target_cell,
        agency=source_box.agency,
        actor=actor,
    )
    validate_boxes_unlocked([target_binding.box_id], for_execution=True)

    total_before = int(
        FbsStockBalance.objects.filter(
            box_id__in=(source_box.id, target_binding.box_id),
        ).aggregate(total=Sum("qty"))["total"]
        or 0
    )
    source_before = int(
        FbsStockBalance.objects.filter(box=source_box).aggregate(total=Sum("qty"))["total"]
        or 0
    )
    move_details = _move_one_balance_unit(
        source=source_balance,
        target_box=target_binding.box,
        performed_by=actor,
        allow_untouched_reservations=True,
    )
    source_after = int(
        FbsStockBalance.objects.filter(box=source_box).aggregate(total=Sum("qty"))["total"]
        or 0
    )
    total_after = int(
        FbsStockBalance.objects.filter(
            box_id__in=(source_box.id, target_binding.box_id),
        ).aggregate(total=Sum("qty"))["total"]
        or 0
    )
    if source_after != source_before - 1 or total_before != total_after:
        raise FbsMovementError("Контрольная сумма остатка не совпала. Перемещение отменено.")

    source_cell_after = int(
        FbsStockBalance.objects.filter(
            box__rack_cell_binding__rack_cell=source_cell,
        ).aggregate(total=Sum("qty"))["total"]
        or 0
    )
    now = timezone.now()
    target_location = target_cell.storage_cell.location
    operation = WarehouseOperation.objects.create(
        agency=source_box.agency,
        operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
        context_type="fbs_rack_content_movement",
        context_id=key,
        source_document_type="fbs_box",
        source_document_id=str(source_box.id),
        source_location=source_location,
        destination_location=target_location,
        source_zone_code="PR",
        destination_zone_code="PR",
        status=WarehouseOperation.STATUS_DONE,
        requested_by=actor,
        requested_by_role="picker",
        assigned_executor_role="picker",
        comment=(
            f"Поштучное перемещение {source_balance.sku_code or source_balance.barcode} "
            f"из PR-ячейки {source_cell.cell_code} в {target_cell.cell_code}"
        ),
        planned_qty=1,
        done_qty=1,
        started_at=now,
        completed_at=now,
    )
    movement = FbsRackContentMovement(
        idempotency_key=key,
        agency=source_box.agency,
        source_box=source_box,
        target_cell=target_cell,
        target_binding=target_binding,
        operation=operation,
        moved_qty=1,
        performed_by=actor,
    )
    movement.full_clean()
    movement.save()
    WarehouseEvent.objects.create(
        agency=source_box.agency,
        event_type="fbs_rack_item_relocated",
        stock_context_type="fbs_rack_content_movement",
        stock_context_id=str(movement.id),
        container=source_box.source_container,
        operation=operation,
        source_document_type="fbs_box",
        source_document_id=str(source_box.id),
        from_location=source_location,
        to_location=target_location,
        from_zone_code="PR",
        to_zone_code="PR",
        qty=1,
        payload={
            "free_move": True,
            "fbs_rack_move": True,
            "rack_to_rack": True,
            "per_item": True,
            "source_box_id": int(source_box.id),
            "source_box_code": source_box.box_code,
            "source_balance_id": int(source_balance.id),
            "target_balance_id": int(move_details["target_balance_id"]),
            "reservation_moved": bool(move_details["reservation_moved"]),
            "reservation_allocation_id": int(move_details["allocation_id"]),
            "queued_batch_ids": list(queued_batch_ids),
            "source_rack_id": int(source_cell.rack_id),
            "source_rack_code": source_cell.rack.rack_code,
            "source_rack_cell_id": int(source_cell.id),
            "source_rack_cell_code": source_cell.cell_code,
            "sku_code": source_balance.sku_code,
            "barcode": source_balance.barcode,
            "marking_verified": bool(source_balance.marking_code),
            "item_scan_fingerprint": fingerprint,
            "target_rack_id": int(target_cell.rack_id),
            "target_rack_code": target_cell.rack.rack_code,
            "target_rack_cell_id": int(target_cell.id),
            "target_rack_cell_code": target_cell.cell_code,
            "target_box_id": int(target_binding.box_id),
            "target_box_code": target_binding.box.box_code,
            "agency_id": int(source_box.agency_id),
            "source_box_remaining_qty": source_after,
            "source_cell_remaining_qty": source_cell_after,
        },
        performed_by=actor,
        performed_by_role="picker",
        occurred_at=now,
    )
    movement.moved_sku_code = source_balance.sku_code
    movement.source_cell_code = source_cell.cell_code
    movement.source_cell_remaining_qty = source_cell_after
    return movement


@transaction.atomic
def move_all_box_contents_to_rack_cell(
    *,
    source_box_scan: str,
    target_cell_scan: str,
    performed_by,
    idempotency_key: str,
) -> FbsRackContentMovement:
    """Move all currently free FBS balance from one box into an exact PR rack cell."""

    _require_writes()
    actor = _actor(performed_by)
    if actor is None:
        raise FbsMovementError("Не указан сотрудник, выполняющий перемещение.")
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 64:
        raise FbsMovementError("Недействительный ключ операции. Обновите экран и повторите скан.")
    prior = (
        FbsRackContentMovement.objects.select_related(
            "source_box", "target_cell__storage_cell__location"
        )
        .filter(idempotency_key=key)
        .first()
    )
    if prior is not None:
        if (
            prior.source_box.box_code.upper() != _normalized_box_scan(source_box_scan)
            or prior.target_cell.cell_code.upper()
            != normalize_operational_location_scan(target_cell_scan)
        ):
            raise FbsMovementError("Ключ операции уже использован для другого перемещения.")
        return prior

    source_match = _resolve_source_box(source_box_scan)
    source_box = (
        FbsBox.objects.select_for_update(of=("self",))
        .select_related(
            "agency",
            "pallet__cell__location",
            "source_container__current_location",
        )
        .get(pk=source_match.pk)
    )
    if FbsRackCellBinding.objects.filter(box=source_box).exists():
        raise FbsMovementError(
            "На этом экране доступно только размещение из обычного FBS-короба в PR-ячейку."
        )
    rack_cell = resolve_fbs_rack_cell_scan(target_cell_scan, lock=True)
    staging = (
        FbsRackStagingBox.objects.select_for_update()
        .filter(box=source_box, status=FbsRackStagingBox.STATUS_AWAITING)
        .first()
    )
    if staging is not None and staging.rack_id != rack_cell.rack_id:
        raise FbsMovementError(
            f"Короб доставлен к стеллажу {staging.rack.rack_code}. "
            "Отсканируйте ячейку именно этого стеллажа."
        )
    source_location = getattr(source_box.source_container, "current_location", None)
    if source_box.source_container_id is None or source_location is None:
        raise FbsMovementError("У исходного FBS-короба отсутствует физический адрес.")
    if staging is not None and getattr(source_location, "id", None) != staging.rack.location_id:
        raise FbsMovementError("Физический адрес короба изменился после доставки к стеллажу.")
    if staging is None:
        logical_location = (
            getattr(source_box.pallet.warehouse_container, "current_location", None)
            or source_box.pallet.cell.location
        )
        if getattr(logical_location, "id", None) != source_location.id:
            raise FbsMovementError(
                "Физический адрес FBS-короба не совпадает с его учётной паллетой."
            )
    if WarehouseOperationTask.objects.filter(
        container_id=source_box.source_container_id,
        status__in=(
            WarehouseOperationTask.STATUS_CREATED,
            WarehouseOperationTask.STATUS_IN_PROGRESS,
        ),
    ).exists():
        raise FbsMovementError("Исходный FBS-короб участвует в активной складской операции.")

    balances = list(
        FbsStockBalance.objects.select_for_update(of=("self",))
        .filter(box=source_box, qty__gt=0)
        .select_related("sku_ref")
        .order_by("id")
    )
    _assert_source_free(source_box, balances)
    validate_boxes_unlocked([source_box.id], for_execution=True)
    binding = _get_or_create_binding(
        rack_cell=rack_cell,
        agency=source_box.agency,
        actor=actor,
    )
    if binding.box_id == source_box.id:
        raise FbsMovementError("Исходный короб уже является системным коробом этой ячейки.")
    validate_boxes_unlocked([binding.box_id], for_execution=True)
    target_balances = list(
        FbsStockBalance.objects.select_for_update()
        .filter(box=binding.box)
        .order_by("id")
    )
    target_by_identity = {row.identity_key: row for row in target_balances}
    total_before = int(
        FbsStockBalance.objects.filter(
            box_id__in=(source_box.id, binding.box_id)
        ).aggregate(total=Sum("qty"))["total"]
        or 0
    )
    moved_qty = sum(int(row.qty or 0) for row in balances)
    for balance in balances:
        _move_balance_row(
            source=balance,
            target_box=binding.box,
            target_by_identity=target_by_identity,
        )
    source_after = int(
        FbsStockBalance.objects.filter(box=source_box).aggregate(total=Sum("qty"))["total"]
        or 0
    )
    total_after = int(
        FbsStockBalance.objects.filter(
            box_id__in=(source_box.id, binding.box_id)
        ).aggregate(total=Sum("qty"))["total"]
        or 0
    )
    if source_after != 0 or total_before != total_after:
        raise FbsMovementError("Контрольная сумма остатка не совпала. Перемещение отменено.")

    source_is_rack_binding = FbsRackCellBinding.objects.filter(box=source_box).exists()
    if not source_is_rack_binding:
        source_box.status = FbsBox.STATUS_ARCHIVED
        source_box.save(update_fields=["status", "updated_at"])
        source_box.source_container.status = WarehouseContainer.STATUS_ARCHIVED
        source_box.source_container.save(update_fields=["status", "updated_at"])

    now = timezone.now()
    target_location = rack_cell.storage_cell.location
    operation = WarehouseOperation.objects.create(
        agency=source_box.agency,
        operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
        context_type="fbs_rack_content_movement",
        context_id=key,
        source_document_type="fbs_box",
        source_document_id=str(source_box.id),
        source_location=source_location,
        destination_location=target_location,
        source_zone_code=str(getattr(source_location, "zone_code", "") or ""),
        destination_zone_code="PR",
        status=WarehouseOperation.STATUS_DONE,
        requested_by=actor,
        requested_by_role="picker",
        assigned_executor_role="picker",
        comment=(
            f"Свободное размещение всего содержимого FBS-короба {source_box.box_code} "
            f"в {rack_cell.cell_code}"
        ),
        planned_qty=moved_qty,
        done_qty=moved_qty,
        started_at=now,
        completed_at=now,
    )
    movement = FbsRackContentMovement(
        idempotency_key=key,
        agency=source_box.agency,
        source_box=source_box,
        target_cell=rack_cell,
        target_binding=binding,
        operation=operation,
        moved_qty=moved_qty,
        performed_by=actor,
    )
    movement.full_clean()
    movement.save()
    WarehouseEvent.objects.create(
        agency=source_box.agency,
        event_type="fbs_rack_contents_placed",
        stock_context_type="fbs_rack_content_movement",
        stock_context_id=str(movement.id),
        container=source_box.source_container,
        operation=operation,
        source_document_type="fbs_box",
        source_document_id=str(source_box.id),
        from_location=source_location,
        to_location=target_location,
        from_zone_code=str(getattr(source_location, "zone_code", "") or ""),
        to_zone_code="PR",
        qty=moved_qty,
        payload={
            "free_move": True,
            "fbs_rack_move": True,
            "source_box_id": int(source_box.id),
            "source_box_code": source_box.box_code,
            "rack_id": int(rack_cell.rack_id),
            "rack_code": rack_cell.rack.rack_code,
            "rack_cell_id": int(rack_cell.id),
            "rack_cell_code": rack_cell.cell_code,
            "target_box_id": int(binding.box_id),
            "target_box_code": binding.box.box_code,
            "agency_id": int(source_box.agency_id),
        },
        performed_by=actor,
        performed_by_role="picker",
        occurred_at=now,
    )
    if staging is not None:
        staging.status = FbsRackStagingBox.STATUS_PLACED
        staging.placed_by = actor
        staging.placed_at = now
        staging.save(
            update_fields=["status", "placed_by", "placed_at", "updated_at"]
        )
    return movement
