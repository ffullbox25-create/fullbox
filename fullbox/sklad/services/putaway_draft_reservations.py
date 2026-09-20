from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from sku.models import Agency
from sklad.models import WarehouseLocation, WarehouseOperation, WarehouseStockSnapshot
from sklad.location_occupancy import fbs_storage_occupied_rows

_ACTIVE_PUTAWAY_STATUSES = (
    WarehouseOperation.STATUS_CREATED,
    WarehouseOperation.STATUS_PLANNED,
    WarehouseOperation.STATUS_IN_PROGRESS,
    WarehouseOperation.STATUS_PARTIAL,
    WarehouseOperation.STATUS_BLOCKED,
)


def _int_value(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _text(value) -> str:
    return str(value or "").strip()


def _normalize_os_destination(raw) -> dict | None:
    source = raw if isinstance(raw, dict) else {}
    zone = _text(source.get("zone")).upper() or "PR"
    if zone != "OS":
        return None
    row = _int_value(source.get("row"))
    section = _int_value(source.get("section"))
    tier = _int_value(source.get("tier"))
    cell = _int_value(source.get("cell"))
    if not all((row, section, tier, cell)):
        return None
    return {
        "zone": "OS",
        "row": row,
        "section": section,
        "tier": tier,
        "cell": cell,
    }


def _location_key(destination: dict) -> tuple[int, int, int, int]:
    return (
        _int_value(destination.get("row")),
        _int_value(destination.get("section")),
        _int_value(destination.get("tier")),
        _int_value(destination.get("cell")),
    )


def _location_label(row: int, section: int, tier: int, cell: int) -> str:
    line_labels = {
        1: "0",
        2: "A",
        3: "B",
        4: "C",
        5: "D",
        6: "E",
        7: "F",
        8: "G",
        9: "I",
    }
    line_label = line_labels.get(section, str(section or ""))
    if line_label and row and tier and cell:
        return f"OS · Линия {line_label} · Стеллаж {row} · Этаж {tier} · Ячейка {cell}"
    return "OS · Основной склад"


@dataclass(frozen=True)
class PutawayDraftSyncResult:
    ok: bool
    error_message: str = ""
    reserved_rows: list[dict] = field(default_factory=list)
    conflict_rows: list[dict] = field(default_factory=list)
    released_count: int = 0
    expires_in_seconds: int = 20 * 60


class PutawayDraftReservationService:
    DRAFT_CONTEXT_TYPE = "putaway_draft"
    DRAFT_SOURCE_DOCUMENT_TYPE = "receiving"
    SESSION_TIMEOUT = timedelta(minutes=20)

    @classmethod
    def is_draft_operation(cls, operation: WarehouseOperation | None) -> bool:
        return (
            operation is not None
            and _text(operation.operation_type) == WarehouseOperation.TYPE_PUTAWAY
            and _text(operation.context_type) == cls.DRAFT_CONTEXT_TYPE
        )

    @classmethod
    def cleanup_expired(cls, *, now=None) -> int:
        timestamp = now or timezone.now()
        cutoff = timestamp - cls.SESSION_TIMEOUT
        qs = cls._draft_queryset().filter(updated_at__lt=cutoff)
        count = qs.count()
        if count:
            qs.delete()
        return count

    @classmethod
    def release(cls, *, draft_token: str) -> int:
        token = _text(draft_token)
        if not token:
            return 0
        qs = cls._draft_queryset(token=token)
        count = qs.count()
        if count:
            qs.delete()
        return count

    @classmethod
    def touch(cls, *, draft_token: str, now=None) -> int:
        token = _text(draft_token)
        if not token:
            return 0
        timestamp = now or timezone.now()
        return cls._draft_queryset(token=token).update(updated_at=timestamp)

    @classmethod
    @transaction.atomic
    def sync_receiving_putaway_draft(
        cls,
        *,
        draft_token: str,
        order_id: str,
        agency: Agency | None,
        rows,
        requested_by=None,
        requested_by_role: str = "",
    ) -> PutawayDraftSyncResult:
        cls.cleanup_expired()
        token = _text(draft_token)
        order_key = _text(order_id)
        if not token:
            return PutawayDraftSyncResult(ok=False, error_message="Не передан токен бронирования.")
        if not order_key:
            return PutawayDraftSyncResult(ok=False, error_message="Не удалось определить заявку приемки.")
        if agency is None:
            return PutawayDraftSyncResult(ok=False, error_message="Не удалось определить клиента приемки.")

        reserved_rows: list[dict] = []
        seen_keys: set[tuple[int, int, int, int]] = set()
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict):
                continue
            pallet_code = _text(item.get("pallet_code") or item.get("palletCode"))
            if not pallet_code:
                continue
            destination = _normalize_os_destination(item.get("destination") if isinstance(item.get("destination"), dict) else item)
            if destination is None:
                continue
            key = _location_key(destination)
            if key in seen_keys:
                return PutawayDraftSyncResult(
                    ok=False,
                    error_message=f"Ячейка OS уже выбрана для другой паллеты: {destination['row']}/{destination['section']}/{destination['tier']}/{destination['cell']}.",
                    conflict_rows=[
                        {
                            "pallet_code": pallet_code,
                            "destination": destination,
                            "reason": "duplicate_selection",
                        }
                    ],
                )
            seen_keys.add(key)
            reserved_rows.append(
                {
                    "pallet_code": pallet_code,
                    "destination": destination,
                    "os_key": key,
                }
            )

        occupied_keys = cls._occupied_os_keys(exclude_draft_token=token)
        for row in reserved_rows:
            if row["os_key"] in occupied_keys:
                destination = row["destination"]
                return PutawayDraftSyncResult(
                    ok=False,
                    error_message=(
                        f"Ячейка {_location_label(destination['row'], destination['section'], destination['tier'], destination['cell'])} "
                        "уже занята или зарезервирована другим пользователем."
                    ),
                    conflict_rows=[
                        {
                            "pallet_code": row["pallet_code"],
                            "destination": destination,
                            "reason": "occupied",
                        }
                    ],
                )

        locked_locations: dict[tuple[int, int, int, int], WarehouseLocation] = {}
        for row in sorted(reserved_rows, key=lambda item: item["os_key"]):
            destination = row["destination"]
            location = cls._ensure_os_location(
                row=destination["row"],
                section=destination["section"],
                tier=destination["tier"],
                cell=destination["cell"],
            )
            locked_locations[row["os_key"]] = WarehouseLocation.objects.select_for_update().get(
                pk=location.pk
            )

        occupied_keys = cls._occupied_os_keys(exclude_draft_token=token)
        for row in reserved_rows:
            if row["os_key"] in occupied_keys:
                destination = row["destination"]
                return PutawayDraftSyncResult(
                    ok=False,
                    error_message=(
                        f"Ячейка {_location_label(destination['row'], destination['section'], destination['tier'], destination['cell'])} "
                        "уже занята или зарезервирована другим пользователем."
                    ),
                    conflict_rows=[
                        {
                            "pallet_code": row["pallet_code"],
                            "destination": destination,
                            "reason": "occupied",
                        }
                    ],
                )

        cls.release(draft_token=token)
        for row in reserved_rows:
            destination = row["destination"]
            location = locked_locations[row["os_key"]]
            WarehouseOperation.objects.create(
                agency=agency,
                operation_type=WarehouseOperation.TYPE_PUTAWAY,
                context_type=cls.DRAFT_CONTEXT_TYPE,
                context_id=token,
                source_document_type=cls.DRAFT_SOURCE_DOCUMENT_TYPE,
                source_document_id=order_key,
                destination_location=location,
                destination_zone_code="OS",
                status=WarehouseOperation.STATUS_CREATED,
                requested_by=requested_by,
                requested_by_role=_text(requested_by_role),
                assigned_executor_role="reachtruck",
                comment=row["pallet_code"],
                planned_qty=0,
            )
        return PutawayDraftSyncResult(ok=True, reserved_rows=reserved_rows)

    @classmethod
    def _draft_queryset(cls, *, token: str = ""):
        qs = WarehouseOperation.objects.filter(
            operation_type=WarehouseOperation.TYPE_PUTAWAY,
            context_type=cls.DRAFT_CONTEXT_TYPE,
            status__in=_ACTIVE_PUTAWAY_STATUSES,
        )
        token = _text(token)
        if token:
            qs = qs.filter(context_id=token)
        return qs

    @classmethod
    def _occupied_os_keys(cls, *, exclude_draft_token: str = "") -> set[tuple[int, int, int, int]]:
        keys: set[tuple[int, int, int, int]] = set()
        for snapshot in (
            WarehouseStockSnapshot.objects.select_related("location")
            .filter(zone_code__iexact="OS", is_archived=False)
            .order_by("id")
        ):
            location = snapshot.location
            if location is None:
                continue
            key = (
                _int_value(location.row_no),
                _int_value(location.section_no),
                _int_value(location.tier_no),
                _int_value(location.cell_no),
            )
            if all(key):
                keys.add(key)
        operations = (
            WarehouseOperation.objects.select_related("destination_location")
            .filter(
                operation_type=WarehouseOperation.TYPE_PUTAWAY,
                status__in=_ACTIVE_PUTAWAY_STATUSES,
                destination_location__zone_code__iexact="OS",
            )
            .order_by("id")
        )
        draft_token = _text(exclude_draft_token)
        if draft_token:
            operations = operations.exclude(
                context_type=cls.DRAFT_CONTEXT_TYPE,
                context_id=draft_token,
            )
        for operation in operations:
            location = operation.destination_location
            if location is None:
                continue
            key = (
                _int_value(location.row_no),
                _int_value(location.section_no),
                _int_value(location.tier_no),
                _int_value(location.cell_no),
            )
            if all(key):
                keys.add(key)
        for row in fbs_storage_occupied_rows():
            key = (
                _int_value(row.get("row")),
                _int_value(row.get("section")),
                _int_value(row.get("tier")),
                _int_value(row.get("cell")),
            )
            if all(key):
                keys.add(key)
        return keys

    @classmethod
    def _ensure_os_location(cls, *, row: int, section: int, tier: int, cell: int) -> WarehouseLocation:
        location, _created = WarehouseLocation.objects.get_or_create(
            warehouse_code="MSK",
            zone_code="OS",
            row_no=int(row or 0),
            section_no=int(section or 0),
            tier_no=int(tier or 0),
            cell_no=int(cell or 0),
            defaults={
                "zone_kind": WarehouseLocation.ZONE_KIND_STORAGE,
                "location_code": "",
                "display_name": _location_label(int(row or 0), int(section or 0), int(tier or 0), int(cell or 0)),
                "is_active": True,
                "is_pickable": True,
                "is_storage": True,
            },
        )
        from .shared_location_catalog import ensure_shared_fbs_storage_cell

        ensure_shared_fbs_storage_cell(location)
        return location
