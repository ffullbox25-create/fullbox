from __future__ import annotations

import re
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count

from sklad.models import (
    WarehouseContainer,
    WarehouseLocation,
    WarehouseOperationTask,
    WarehouseStockSnapshot,
)


OPERATIONAL_ZONE_KINDS = {
    "PR": WarehouseLocation.ZONE_KIND_RECEIVING,
    "OBR": WarehouseLocation.ZONE_KIND_PROCESSING,
    "OTG": WarehouseLocation.ZONE_KIND_SHIPPING,
    "OS": WarehouseLocation.ZONE_KIND_STORAGE,
    "MR": WarehouseLocation.ZONE_KIND_STORAGE,
}
OPERATIONAL_ZONE_LABELS = {
    "PR": "Приёмка",
    "OBR": "Обработка",
    "OTG": "Отгрузка",
    "OS": "Основной склад",
    "MR": "Между рядами",
}
CONCRETE_MOVEMENT_ZONES = frozenset({"PR", "OBR", "OTG", "OS", "US"})
ACTIVE_TASK_STATUSES = (
    WarehouseOperationTask.STATUS_CREATED,
    WarehouseOperationTask.STATUS_IN_PROGRESS,
)
LOCATION_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_/-]{2,63}$")
AIM_SYMBOLOGY_PREFIX_RE = re.compile(r"^\][A-Z0-9]{2}")


@dataclass(frozen=True)
class OperationalLocationOccupancy:
    current_containers: int
    pending_containers: int
    unregistered_container_codes: int
    occupied: int
    capacity: int
    available: int | None


def normalize_operational_location_scan(value: str) -> str:
    scan = AIM_SYMBOLOGY_PREFIX_RE.sub("", str(value or "").strip()).strip()
    if scan.upper().startswith("LOC:"):
        parts = scan.split(":", 2)
        scan = parts[-1] if len(parts) == 3 else scan[4:]
    return scan.strip().upper()


def normalize_operational_location_code(value: str) -> str:
    code = normalize_operational_location_scan(value)
    if not LOCATION_CODE_RE.fullmatch(code):
        raise ValidationError(
            "Код места должен содержать 3–64 символа: латинские буквы, цифры, дефис, подчёркивание или косую черту."
        )
    if code in OPERATIONAL_ZONE_KINDS:
        raise ValidationError(f"Код {code} зарезервирован для общей зоны.")
    return code


def is_concrete_movement_location(location: WarehouseLocation | None) -> bool:
    """Return whether a movement points at an identifiable physical place."""
    if location is None:
        return False
    zone = str(location.zone_code or "").strip().upper()
    if zone not in CONCRETE_MOVEMENT_ZONES:
        return True
    code = normalize_operational_location_scan(location.location_code)
    if not code or code == zone or code in CONCRETE_MOVEMENT_ZONES:
        return False
    return str(location.zone_kind or "").strip().lower() != WarehouseLocation.ZONE_KIND_VIRTUAL


def require_concrete_movement_location(
    location: WarehouseLocation | None,
    *,
    purpose: str = "перемещения",
) -> WarehouseLocation:
    if location is None:
        raise ValidationError(f"Для {purpose} выберите конкретное складское место с QR.")
    zone = str(location.zone_code or "").strip().upper()
    if zone in CONCRETE_MOVEMENT_ZONES and not is_concrete_movement_location(location):
        raise ValidationError(
            f"Общая зона {zone} запрещена для {purpose}. "
            "Отсканируйте конкретное место с QR, например A10-1-1."
        )
    if not location.is_active:
        raise ValidationError("Выбранное складское место выключено.")
    return location


def receiving_locations(*, warehouse_code: str = "MSK"):
    """Exact PR places of the general warehouse that receiving may offer.

    Receiving always accepts goods into the general warehouse. A place that
    participates in FBS is never offered to receiving: goods reach FBS only
    through a client movement request.
    """
    return (
        WarehouseLocation.objects.filter(
            warehouse_code=warehouse_code,
            zone_code__iexact="PR",
            is_active=True,
            is_topology_visible=False,
            is_fbs_visible=False,
        )
        .exclude(location_code__iexact="PR")
        .exclude(zone_kind=WarehouseLocation.ZONE_KIND_VIRTUAL)
    )


def require_receiving_location(location: WarehouseLocation) -> WarehouseLocation:
    code = str(location.location_code or "").strip()
    if str(location.zone_code or "").strip().upper() != "PR":
        raise ValidationError(
            f"Место {code} не относится к зоне приёмки. "
            "Приёмка размещает товар только в зону PR."
        )
    if location.is_fbs_visible:
        raise ValidationError(
            f"Место {code} относится к FBS. "
            "Приёмка размещает товар только на места общего склада в зоне PR."
        )
    return location


def _operational_zone(value: str) -> str:
    zone = str(value or "").strip().upper()
    if zone not in OPERATIONAL_ZONE_KINDS:
        raise ValidationError(
            "Дополнительное место можно создать только в рабочей зоне PR, OBR, OTG, OS или MR."
        )
    return zone


def operational_location_occupancy(
    location: WarehouseLocation,
) -> OperationalLocationOccupancy:
    current_container_ids = set(
        WarehouseContainer.objects.filter(
            current_location=location,
            status=WarehouseContainer.STATUS_ACTIVE,
            parent_container__isnull=True,
        ).values_list("id", flat=True)
    )
    pending_container_ids = set(
        WarehouseOperationTask.objects.filter(
            to_location=location,
            status__in=ACTIVE_TASK_STATUSES,
            container__isnull=False,
        ).values_list("container_id", flat=True)
    )
    pending_container_ids.difference_update(current_container_ids)
    unregistered_container_codes = (
        WarehouseStockSnapshot.objects.filter(
            location=location,
            is_archived=False,
            qty__gt=0,
            container__isnull=True,
        )
        .exclude(container_code="")
        .values("container_code")
        .annotate(rows=Count("id"))
        .count()
    )
    occupied = (
        len(current_container_ids)
        + len(pending_container_ids)
        + int(unregistered_container_codes or 0)
    )
    capacity = int(location.capacity_containers or 0)
    available = max(capacity - occupied, 0) if capacity else None
    return OperationalLocationOccupancy(
        current_containers=len(current_container_ids),
        pending_containers=len(pending_container_ids),
        unregistered_container_codes=int(unregistered_container_codes or 0),
        occupied=occupied,
        capacity=capacity,
        available=available,
    )


def validate_operational_location(
    location: WarehouseLocation,
    *,
    expected_zone: str,
    require_fbs: bool = False,
    required_slots: int = 0,
) -> OperationalLocationOccupancy:
    zone = _operational_zone(expected_zone)
    require_concrete_movement_location(location, purpose=f"операции в зоне {zone}")
    if not location.is_active:
        raise ValidationError("Место выключено начальником склада.")
    if str(location.zone_code or "").strip().upper() != zone:
        raise ValidationError(f"Место не относится к зоне {zone}.")
    if zone == "OTG" and not location.is_shipping:
        raise ValidationError("Место не разрешено для операций отгрузки.")
    if require_fbs and not location.is_fbs_visible:
        raise ValidationError("Место не разрешено для операций FBS.")
    occupancy = operational_location_occupancy(location)
    required = max(int(required_slots or 0), 0)
    if (
        required
        and occupancy.available is not None
        and required > occupancy.available
    ):
        raise ValidationError(
            f"В месте {location.location_code} свободно {occupancy.available}, "
            f"требуется {required}."
        )
    return occupancy


def resolve_operational_location_scan(
    scan_value: str,
    *,
    expected_zone: str,
    require_fbs: bool = False,
    required_slots: int = 0,
    lock: bool = False,
) -> WarehouseLocation:
    code = normalize_operational_location_scan(scan_value)
    queryset = WarehouseLocation.objects
    if lock:
        queryset = queryset.select_for_update()
    location = queryset.filter(
        warehouse_code="MSK",
        location_code__iexact=code,
    ).first()
    if location is None:
        raise ValidationError("QR места не найден в справочнике склада.")
    validate_operational_location(
        location,
        expected_zone=expected_zone,
        require_fbs=require_fbs,
        required_slots=required_slots,
    )
    return location


def select_operational_location(
    *,
    zone_code: str,
    require_fbs: bool = False,
    required_slots: int = 0,
    lock: bool = False,
) -> WarehouseLocation | None:
    zone = _operational_zone(zone_code)
    queryset = WarehouseLocation.objects.filter(
        warehouse_code="MSK",
        zone_code=zone,
        is_active=True,
        is_topology_visible=False,
    ).exclude(
        location_code__iexact=zone
    ).exclude(
        zone_kind=WarehouseLocation.ZONE_KIND_VIRTUAL
    )
    if require_fbs:
        queryset = queryset.filter(is_fbs_visible=True)
    if zone == "OTG":
        queryset = queryset.filter(is_shipping=True)
    if lock:
        queryset = queryset.select_for_update()
    candidates: list[tuple[int, int, str, int, WarehouseLocation]] = []
    for location in queryset.order_by("location_code", "id"):
        occupancy = operational_location_occupancy(location)
        required = max(int(required_slots or 0), 0)
        if (
            occupancy.available is not None
            and required > occupancy.available
        ):
            continue
        unlimited = 1 if occupancy.capacity == 0 else 0
        candidates.append(
            (
                unlimited,
                occupancy.occupied,
                str(location.location_code or ""),
                int(location.id or 0),
                location,
            )
        )
    return min(candidates, default=None, key=lambda row: row[:4])[-1] if candidates else None


@transaction.atomic
def create_operational_location(
    *,
    zone_code: str,
    location_code: str,
    display_name: str,
    capacity_containers: int,
    is_fbs_visible: bool,
    allow_mixed_client_pallets: bool = False,
    is_shipping: bool | None = None,
) -> WarehouseLocation:
    zone = _operational_zone(zone_code)
    code = normalize_operational_location_code(location_code)
    name = str(display_name or "").strip()
    if not name:
        raise ValidationError("Укажите понятное название места.")
    capacity = int(capacity_containers or 0)
    if capacity < 1:
        raise ValidationError("Вместимость дополнительного места должна быть не меньше 1.")
    if allow_mixed_client_pallets and not is_fbs_visible:
        raise ValidationError(
            "Смешивание паллет клиентов можно включить только для FBS-места."
        )
    shipping_enabled = zone == "OTG" if is_shipping is None else bool(is_shipping)
    location = WarehouseLocation(
        warehouse_code="MSK",
        zone_code=zone,
        zone_kind=OPERATIONAL_ZONE_KINDS[zone],
        row_no=0,
        section_no=0,
        tier_no=0,
        cell_no=0,
        location_code=code,
        display_name=name,
        capacity_containers=capacity,
        is_topology_visible=False,
        is_fbs_visible=bool(is_fbs_visible),
        allow_mixed_client_pallets=bool(allow_mixed_client_pallets),
        is_active=True,
        is_storage=zone in {"OS", "MR"},
        is_pickable=zone in {"OS", "MR"},
        is_processing=zone == "OBR",
        is_shipping=shipping_enabled,
        is_loading=False,
    )
    try:
        location.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError("Место с таким кодом уже существует.") from exc
    if zone == "PR" and location.is_fbs_visible:
        from .shared_location_catalog import ensure_exact_fbs_rack_cell

        ensure_exact_fbs_rack_cell(location)
    return location


@transaction.atomic
def create_operational_locations(rows: list[dict]) -> list[WarehouseLocation]:
    """Create a validated location import as one all-or-nothing operation."""
    if not rows:
        raise ValidationError("В файле нет строк с местами.")
    if len(rows) > 1000:
        raise ValidationError("За один импорт можно создать не больше 1000 мест.")
    normalized_codes: set[str] = set()
    created: list[WarehouseLocation] = []
    for row_number, row in enumerate(rows, start=2):
        try:
            code = normalize_operational_location_code(row.get("location_code", ""))
            if code in normalized_codes:
                raise ValidationError(f"Код {code} повторяется в файле.")
            normalized_codes.add(code)
            created.append(
                create_operational_location(
                    zone_code=row.get("zone_code", ""),
                    location_code=code,
                    display_name=row.get("display_name", ""),
                    capacity_containers=row.get("capacity_containers", 0),
                    is_fbs_visible=bool(row.get("is_fbs_visible", False)),
                    allow_mixed_client_pallets=bool(
                        row.get("allow_mixed_client_pallets", False)
                    ),
                    is_shipping=row.get("is_shipping"),
                )
            )
        except (ValidationError, ValueError, TypeError) as exc:
            message = "; ".join(getattr(exc, "messages", ()) or (str(exc),))
            raise ValidationError(f"Строка {row_number}: {message}") from exc
    return created


@transaction.atomic
def update_operational_location(
    *,
    location_id: int,
    display_name: str,
    capacity_containers: int,
    is_fbs_visible: bool,
    is_active: bool,
    allow_mixed_client_pallets: bool = False,
    is_shipping: bool | None = None,
) -> WarehouseLocation:
    location = WarehouseLocation.objects.select_for_update().get(pk=location_id)
    if location.is_topology_visible or location.zone_code not in OPERATIONAL_ZONE_KINDS:
        raise ValidationError("Изменять можно только дополнительные места рабочих зон.")
    name = str(display_name or "").strip()
    if not name:
        raise ValidationError("Укажите понятное название места.")
    capacity = int(capacity_containers or 0)
    if capacity < 1:
        raise ValidationError("Вместимость дополнительного места должна быть не меньше 1.")
    if allow_mixed_client_pallets and not is_fbs_visible:
        raise ValidationError(
            "Смешивание паллет клиентов можно включить только для FBS-места."
        )
    shipping_enabled = location.is_shipping if is_shipping is None else bool(is_shipping)
    occupancy = operational_location_occupancy(location)
    open_fbs_work = (
        location.fbs_staging_replenishment_plans.exclude(
            status__in=("done", "canceled")
        ).exists()
        or location.fbs_handover_batches.filter(
            status__in=("open", "ready", "problem")
        ).exists()
    )
    from fbs.models import FbsRack

    has_active_fbs_rack = FbsRack.objects.filter(
        location=location,
        is_active=True,
    ).exists()
    if capacity < occupancy.occupied:
        raise ValidationError(
            f"Нельзя уменьшить вместимость ниже текущей занятости: {occupancy.occupied}."
        )
    if not is_active and occupancy.occupied:
        raise ValidationError(
            f"Сначала освободите место: сейчас занято {occupancy.occupied}."
        )
    if open_fbs_work and (not is_active or not is_fbs_visible):
        raise ValidationError(
            "Место используется незавершённой FBS-операцией. Сначала завершите её."
        )
    if has_active_fbs_rack and (not is_active or not is_fbs_visible):
        raise ValidationError(
            "У места настроены активные ячейки FBS. Нельзя выключить место или скрыть его из FBS."
        )
    location.display_name = name
    location.capacity_containers = capacity
    location.is_fbs_visible = bool(is_fbs_visible)
    location.allow_mixed_client_pallets = bool(allow_mixed_client_pallets)
    location.is_shipping = shipping_enabled
    location.is_active = bool(is_active)
    location.save(
        update_fields=[
            "display_name",
            "capacity_containers",
            "is_fbs_visible",
            "allow_mixed_client_pallets",
            "is_shipping",
            "is_active",
            "updated_at",
        ]
    )
    if location.zone_code == "PR" and location.is_active and location.is_fbs_visible:
        from .shared_location_catalog import ensure_exact_fbs_rack_cell

        ensure_exact_fbs_rack_cell(location)
    return location
