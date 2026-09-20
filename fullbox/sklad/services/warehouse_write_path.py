from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from marking.codes import (
    MarkingCodeFormatError,
    marking_code_identity,
    marking_code_variants,
    normalize_marking_code,
    validate_import_marking_code,
)
from sku.models import Agency, SKU, SKUBarcode
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseOperationTask,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sklad.location_occupancy import (
    FBS_STORAGE_CONTEXT_TYPE,
    fbs_storage_occupies_location,
    operational_location_occupancy_message,
    os_physical_container_occupancy_message,
)

from .putaway_draft_reservations import PutawayDraftReservationService
from .operational_locations import (
    require_concrete_movement_location,
    select_operational_location,
    validate_operational_location,
)
from .stock_availability import shipping_reserved_box_codes
from .problem_boxes import (
    PROBLEM_BOX_CONTEXT_TYPE,
    check_problem_box_snapshot,
    snapshot_box_code,
)
from .warehouse_events import WarehouseEventType
from .warehouse_transitions import WarehouseStateCode, WarehouseTransitionError, WarehouseTransitionService


ZONE_KIND_BY_CODE = {
    "PR": WarehouseLocation.ZONE_KIND_RECEIVING,
    "OS": WarehouseLocation.ZONE_KIND_STORAGE,
    "MR": WarehouseLocation.ZONE_KIND_STORAGE,
    "OBR": WarehouseLocation.ZONE_KIND_PROCESSING,
    "OTG": WarehouseLocation.ZONE_KIND_SHIPPING,
    "LOAD": WarehouseLocation.ZONE_KIND_LOADING,
    "VEH": WarehouseLocation.ZONE_KIND_VEHICLE,
}
_PUTAWAY_DESTINATION_BLOCKING_STATUSES = (
    WarehouseOperation.STATUS_CREATED,
    WarehouseOperation.STATUS_PLANNED,
    WarehouseOperation.STATUS_IN_PROGRESS,
    WarehouseOperation.STATUS_PARTIAL,
    WarehouseOperation.STATUS_BLOCKED,
)
_FREE_RELOCATION_SOURCE_STATES = {
    WarehouseStateCode.STORED.value,
    WarehouseStateCode.PLACED_IN_RECEIVING.value,
    WarehouseStateCode.IN_PROCESSING_ZONE.value,
    WarehouseStateCode.PLACED_AFTER_PROCESSING.value,
}
_FINAL_OPERATION_STATUSES = {
    WarehouseOperation.STATUS_DONE,
    WarehouseOperation.STATUS_CANCELED,
}
_FBS_MOVEMENT_RESERVE_CONTEXT = "fbs_client_movement"
_FBS_MOVEMENT_SOURCE_DOCUMENT = "fbs_movement"
_OPEN_FBS_MOVEMENT_RESERVE_STATUSES = (
    WarehouseReserve.STATUS_ACTIVE,
    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
    WarehouseReserve.STATUS_ALLOCATED,
)


@dataclass(frozen=True)
class WarehousePlacementResult:
    snapshot_ids: list[int]
    event_ids: list[int]


@dataclass(frozen=True)
class WarehouseCorrectionResult:
    snapshot_ids: list[int]
    event_ids: list[int]
    container_ids: list[int]
    box_codes: list[str]
    removed_qty: int


@dataclass(frozen=True)
class WarehouseUnitShortageResult:
    snapshot_ids: list[int]
    event_ids: list[int]
    operation_id: int
    expected_qty: int
    actual_qty: int
    missing_qty: int
    released_reserve_qty: int


class WarehouseWritePathService:
    BOX_CHARACTERISTIC_FIELDS = ("gross_weight_g", "width_mm", "height_mm", "depth_mm")

    @classmethod
    def concrete_movement_destination(
        cls,
        *,
        warehouse_code: str,
        zone_code: str,
        location_code: str = "",
        row_no: int = 0,
        section_no: int = 0,
        tier_no: int = 0,
        cell_no: int = 0,
        allow_legacy_generic_location: bool = False,
    ) -> WarehouseLocation:
        zone = str(zone_code or "").strip().upper()
        coordinates = (row_no, section_no, tier_no, cell_no)
        exact_code = str(location_code or "").strip()
        if (
            allow_legacy_generic_location
            and zone in {"PR", "OBR", "OTG"}
            and (not exact_code or exact_code.upper() == zone)
            and not any(int(value or 0) > 0 for value in coordinates)
        ):
            return cls.ensure_location(
                warehouse_code=warehouse_code,
                zone_code=zone,
                row_no=0,
                section_no=0,
                tier_no=0,
                cell_no=0,
            )
        if exact_code:
            destination = (
                WarehouseLocation.objects.select_for_update()
                .filter(
                    warehouse_code=warehouse_code,
                    zone_code=zone,
                    location_code__iexact=exact_code,
                )
                .first()
            )
            if destination is None:
                raise ValueError(f"Складское место {exact_code} не найдено в зоне {zone}.")
        elif zone in {"PR", "OBR", "OTG"} and not any(int(value or 0) > 0 for value in coordinates):
            destination = select_operational_location(zone_code=zone, lock=True)
            if destination is None:
                raise ValueError(
                    f"В зоне {zone} не настроено конкретное место с QR. "
                    "Начальник склада должен создать его в справочнике рабочих мест."
                )
        else:
            destination = cls.ensure_location(
                warehouse_code=warehouse_code,
                zone_code=zone,
                row_no=row_no,
                section_no=section_no,
                tier_no=tier_no,
                cell_no=cell_no,
            )
        try:
            require_concrete_movement_location(destination)
        except ValidationError as exc:
            raise ValueError("; ".join(exc.messages)) from exc
        return destination

    @classmethod
    def shipping_reserved_snapshot_ids(
        cls,
        *,
        agency: Agency | int,
        snapshots,
    ) -> set[int]:
        """Return source snapshots already claimed by an active shipment.

        Shipping reserves intentionally do not always mutate snapshot counters.
        A physical claim can therefore live in the legacy warehouse-reserve event
        or in ``ShippingReservationUnit``.  FBS write paths must consult both.
        """

        snapshot_rows = list(snapshots or [])
        if not snapshot_rows:
            return set()

        snapshot_ids = {int(snapshot.id) for snapshot in snapshot_rows if snapshot.id}
        snapshot_by_box: dict[str, set[int]] = {}
        snapshot_by_pallet: dict[str, set[int]] = {}
        display_box_by_key: dict[str, str] = {}
        for snapshot in snapshot_rows:
            box_code = str(
                snapshot.container_code
                or getattr(snapshot.container, "container_code", "")
                or ""
            ).strip()
            box_key = box_code.casefold()
            if box_key:
                snapshot_by_box.setdefault(box_key, set()).add(int(snapshot.id))
                display_box_by_key.setdefault(box_key, box_code)
            parent = getattr(snapshot, "parent_container", None)
            if parent is None and getattr(snapshot, "container", None) is not None:
                parent = getattr(snapshot.container, "parent_container", None)
            pallet_code = str(getattr(parent, "container_code", "") or "").strip()
            pallet_key = pallet_code.casefold()
            if pallet_key:
                snapshot_by_pallet.setdefault(pallet_key, set()).add(int(snapshot.id))

        blocked_ids: set[int] = set()
        agency_id = int(getattr(agency, "pk", agency) or 0)
        reserved_box_codes = shipping_reserved_box_codes(
            agency_id=agency_id,
            box_codes=list(display_box_by_key.values()),
        )
        for reserved_code in reserved_box_codes:
            blocked_ids.update(
                snapshot_by_box.get(str(reserved_code or "").strip().casefold(), set())
            )

        # Import lazily: ``shipping`` depends on warehouse models and importing it
        # while this module is initialised would create an application cycle.
        from shipping.models import ShippingReservationUnit
        from shipping.reservation_units import ACTIVE_RESERVATION_STATUSES

        units = ShippingReservationUnit.objects.filter(
            agency_id=agency_id,
            status__in=ACTIVE_RESERVATION_STATUSES,
        ).only(
            "snapshot_id",
            "box_code",
            "pallet_code",
            "reserve_mode",
            "payload",
        )
        for unit in units:
            if unit.snapshot_id and int(unit.snapshot_id) in snapshot_ids:
                blocked_ids.add(int(unit.snapshot_id))

            unit_box_key = str(unit.box_code or "").strip().casefold()
            if unit_box_key:
                blocked_ids.update(snapshot_by_box.get(unit_box_key, set()))

            payload = unit.payload if isinstance(unit.payload, dict) else {}
            for raw_id in payload.get("planned_snapshot_ids") or []:
                try:
                    planned_id = int(raw_id or 0)
                except (TypeError, ValueError):
                    continue
                if planned_id in snapshot_ids:
                    blocked_ids.add(planned_id)
            for raw_code in payload.get("planned_box_codes") or []:
                planned_key = str(raw_code or "").strip().casefold()
                if planned_key:
                    blocked_ids.update(snapshot_by_box.get(planned_key, set()))

            if unit.reserve_mode == ShippingReservationUnit.MODE_FULL_PALLET:
                pallet_key = str(unit.pallet_code or "").strip().casefold()
                if pallet_key:
                    blocked_ids.update(snapshot_by_pallet.get(pallet_key, set()))

        # Legacy OTG requests created before ShippingReservationUnit was
        # introduced can still own a physical pallet or fixed boxes through an
        # active MoveTask.  Treat those task payloads as exact claims too;
        # otherwise a whole-pallet FBS move can archive the source underneath
        # an already dispatched shipping task.
        from reachtruck.models import MoveTask

        active_otg_tasks = (
            MoveTask.objects.filter(
                request__agency_id=agency_id,
                status__in=(MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS),
            )
            .filter(Q(to_zone__iexact="OTG") | Q(request__destination_zone__iexact="OTG"))
            .only("pallet_code", "move_mode", "payload")
        )
        for task in active_otg_tasks:
            payload = task.payload if isinstance(task.payload, dict) else {}
            if not str(payload.get("shipping_order_id") or "").strip():
                continue

            task_box_keys = {
                str(code or "").strip().casefold()
                for field_name in (
                    "planned_box_codes",
                    "selected_box_codes",
                    "reserved_box_codes",
                    "requested_boxes",
                    "candidate_box_codes",
                )
                for code in (payload.get(field_name) or [])
                if str(code or "").strip()
            }
            requested_box_key = str(payload.get("requested_box") or "").strip().casefold()
            if requested_box_key:
                task_box_keys.add(requested_box_key)
            for box_key in task_box_keys:
                blocked_ids.update(snapshot_by_box.get(box_key, set()))

            # A pattern-based task with no fixed boxes has a live quota on its
            # source pallet.  A full-pallet task always owns the whole pallet.
            if not task_box_keys or task.move_mode == MoveTask.MODE_PALLET_FULL:
                pallet_key = str(
                    payload.get("pallet_code") or task.pallet_code or ""
                ).strip().casefold()
                if pallet_key:
                    blocked_ids.update(snapshot_by_pallet.get(pallet_key, set()))
        return blocked_ids

    @classmethod
    def assert_not_reserved_for_shipping(
        cls,
        *,
        agency: Agency | int,
        snapshots,
    ) -> None:
        snapshot_rows = list(snapshots or [])
        blocked_ids = cls.shipping_reserved_snapshot_ids(
            agency=agency,
            snapshots=snapshot_rows,
        )
        if not blocked_ids:
            return
        blocked_codes = sorted(
            {
                str(
                    snapshot.container_code
                    or getattr(snapshot.container, "container_code", "")
                    or snapshot.id
                ).strip()
                for snapshot in snapshot_rows
                if int(snapshot.id) in blocked_ids
            }
        )
        shown = ", ".join(blocked_codes[:5])
        if len(blocked_codes) > 5:
            shown = f"{shown} и ещё {len(blocked_codes) - 5}"
        raise WarehouseTransitionError(
            "Остаток уже закреплён за активной заявкой на отгрузку"
            + (f": {shown}." if shown else ".")
        )

    @classmethod
    def _assert_shipping_does_not_take_fbs_stock(cls, snapshots) -> None:
        """Whole-box shipping cannot consume stock held by the FBS contour.

        Callers hold snapshot row locks for the duration of the warehouse command.
        Check both counters and exact active claims, including legacy broken counters.
        """
        rows = list(snapshots)
        from .fbs_quantity_reserves import protect_sources, pool_reserves
        protect_sources(rows)
        if not rows:
            return
        claimed_ids = {
            int(payload.get("source_snapshot_id") or 0)
            for payload in WarehouseEvent.objects.filter(
                event_type="fbs_movement_reserved",
                reserve__reserve_type=WarehouseReserve.TYPE_FBS_MOVEMENT,
                reserve__status__in=_OPEN_FBS_MOVEMENT_RESERVE_STATUSES,
                payload__source_snapshot_id__in=[row.id for row in rows],
            ).exclude(reserve_id__in=pool_reserves().values('id')).values_list("payload", flat=True)
        }
        blocked = [
            row for row in rows
            if int(row.other_reserved_qty or 0) > 0 or row.id in claimed_ids
        ]
        if blocked:
            shown = "; ".join(
                f"короб {row.container_code or row.id}, ШК {row.barcode or '-'}"
                for row in blocked[:5]
            )
            raise WarehouseTransitionError(
                "Отгрузка заблокирована: товар зарезервирован для FBS или другой "
                f"складской операции ({shown}). Использовать чужой резерв нельзя."
            )

    @staticmethod
    def _parse_positive_int(value) -> int | None:
        if value in (None, ""):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _receiving_identity_value(value) -> str:
        return str(value or "").strip().casefold()

    @classmethod
    def _receiving_item_container_key(cls, item: dict) -> tuple[str, str]:
        box_code = cls._receiving_identity_value(item.get("box_code"))
        if box_code:
            return ("box", box_code)
        pallet_code = cls._receiving_identity_value(item.get("pallet_code"))
        if pallet_code:
            return ("pallet", pallet_code)
        return ("", "")

    @classmethod
    def _receiving_snapshot_container_key(cls, snapshot: WarehouseStockSnapshot) -> tuple[str, str]:
        container = snapshot.container
        if container and container.container_type == WarehouseContainer.TYPE_BOX:
            code = cls._receiving_identity_value(container.container_code)
            if code:
                return ("box", code)
        code = cls._receiving_identity_value(snapshot.container_code)
        if code and not snapshot.parent_container_id:
            return ("pallet", code)
        parent_code = cls._receiving_identity_value(getattr(snapshot.parent_container, "container_code", ""))
        if parent_code:
            return ("pallet", parent_code)
        container_code = cls._receiving_identity_value(getattr(container, "container_code", ""))
        if container_code:
            return ("pallet", container_code)
        return ("", "")

    @classmethod
    def _receiving_item_identity_key(cls, item: dict) -> tuple:
        container_kind, container_code = cls._receiving_item_container_key(item)
        return (
            container_kind,
            container_code,
            cls._receiving_identity_value(item.get("sku_code") or item.get("sku")),
            cls._receiving_identity_value(item.get("size")),
            cls._receiving_identity_value(item.get("barcode")),
            cls._receiving_identity_value(item.get("goods_type")),
            cls._receiving_identity_value(item.get("marking_code")),
            max(int(item.get("qty") or 0), 0),
        )

    @classmethod
    def _receiving_snapshot_identity_key(cls, snapshot: WarehouseStockSnapshot) -> tuple:
        container_kind, container_code = cls._receiving_snapshot_container_key(snapshot)
        return (
            container_kind,
            container_code,
            cls._receiving_identity_value(snapshot.sku_code),
            cls._receiving_identity_value(snapshot.size),
            cls._receiving_identity_value(snapshot.barcode),
            cls._receiving_identity_value(snapshot.goods_type),
            cls._receiving_identity_value(snapshot.marking_code),
            max(int(snapshot.qty or 0), 0),
        )

    @classmethod
    def _receiving_item_match_key(cls, item: dict) -> tuple:
        return cls._receiving_item_identity_key(item)[:-1]

    @classmethod
    def _receiving_snapshot_match_key(cls, snapshot: WarehouseStockSnapshot) -> tuple:
        return cls._receiving_snapshot_identity_key(snapshot)[:-1]

    @classmethod
    def _receiving_item_logical_key(cls, item: dict) -> tuple:
        identity = cls._receiving_item_identity_key(item)
        return identity[:4] + identity[5:7]

    @classmethod
    def _receiving_snapshot_logical_key(cls, snapshot: WarehouseStockSnapshot) -> tuple:
        identity = cls._receiving_snapshot_identity_key(snapshot)
        return identity[:4] + identity[5:7]

    @classmethod
    def _receiving_materialized_container_codes(
        cls,
        *,
        order_id: str,
        placement_payload: dict,
    ) -> tuple[set[str], set[str]]:
        """Return materialized pallets from this act and their boxes.

        A pallet is materialized once the storekeeper explicitly allows its
        placement.  Its live receiving snapshots may later move to storage,
        processing or FBS, so completion must rely on the immutable audit fact
        instead of requiring those rows to remain in the receiving context.
        """
        # Deferred import: the warehouse core must not depend on receiving at
        # module import time.
        from orders.services import ReceivingWorkflowService

        materialized = ReceivingWorkflowService.materialized_pallet_codes(
            order_id=str(order_id or "").strip(),
        )
        if not materialized:
            return set(), set()

        payload = placement_payload if isinstance(placement_payload, dict) else {}
        pallets = payload.get("act_pallets") if isinstance(payload.get("act_pallets"), list) else []
        pallet_codes: set[str] = set()
        box_codes: set[str] = set()
        for pallet in pallets:
            if not isinstance(pallet, dict):
                continue
            pallet_code = cls._receiving_identity_value(pallet.get("code"))
            if not pallet_code or pallet_code not in materialized:
                continue
            pallet_codes.add(pallet_code)
            for box_code in pallet.get("boxes") or []:
                normalized_box = cls._receiving_identity_value(box_code)
                if normalized_box:
                    box_codes.add(normalized_box)
        return pallet_codes, box_codes

    @staticmethod
    def _cancel_blank_receiving_barcode_matches(
        missing: Counter,
        unexpected: Counter,
    ) -> None:
        """Match a blank draft barcode to warehouse metadata without hiding real conflicts."""
        for expected_key in list(missing):
            if len(expected_key) < 8 or expected_key[4]:
                continue
            remaining = missing[expected_key]
            expected_without_barcode = expected_key[:4] + expected_key[5:]
            for actual_key in list(unexpected):
                if remaining <= 0:
                    break
                if len(actual_key) < 8:
                    continue
                if actual_key[:4] + actual_key[5:] != expected_without_barcode:
                    continue
                matched = min(remaining, unexpected[actual_key])
                remaining -= matched
                unexpected[actual_key] -= matched
                if unexpected[actual_key] <= 0:
                    del unexpected[actual_key]
            if remaining > 0:
                missing[expected_key] = remaining
            else:
                del missing[expected_key]

    @classmethod
    def _enrich_receiving_snapshot_from_item(
        cls,
        snapshot: WarehouseStockSnapshot,
        item: dict,
    ) -> bool:
        """Complete metadata that was unavailable during early pallet putaway.

        A sealed pallet may be placed before the receiving act is closed. The
        draft item can have an empty barcode at that point; final completion
        resolves it from client nomenclature. Treat that as metadata enrichment
        only when container, SKU, size, goods type and marking code still match.
        """
        current_barcode = str(snapshot.barcode or "").strip()
        next_barcode = str(item.get("barcode") or "").strip()
        if current_barcode and next_barcode and current_barcode.casefold() != next_barcode.casefold():
            return False
        if not cls._sync_receiving_snapshot_qty(snapshot, item):
            return False

        update_fields: list[str] = []
        if not current_barcode and next_barcode:
            snapshot.barcode = next_barcode
            update_fields.append("barcode")
        if not str(snapshot.name or "").strip() and str(item.get("name") or "").strip():
            snapshot.name = str(item.get("name") or "").strip()
            update_fields.append("name")
        sku_ref_id = item.get("sku_id")
        if not snapshot.sku_ref_id and sku_ref_id not in (None, ""):
            try:
                snapshot.sku_ref_id = int(sku_ref_id)
            except (TypeError, ValueError):
                pass
            else:
                update_fields.append("sku_ref")
        if update_fields:
            snapshot.snapshot_version = int(snapshot.snapshot_version or 1) + 1
            update_fields.extend(["snapshot_version", "updated_at"])
            snapshot.save(update_fields=update_fields)
        return True

    @classmethod
    def _expand_legacy_unmarked_receiving_snapshots(
        cls,
        *,
        snapshots: list[WarehouseStockSnapshot],
        items: list[dict],
    ) -> int:
        """Split an aggregate pre-completion row into marked unit rows.

        Flow pallets could historically be moved before a CZ receiving was
        closed. That path materialized one aggregate stock row per box/SKU,
        while final CZ completion writes one row per marking code. Preserve
        the assigned warehouse location and normalize only on an exact match.
        """
        expected_by_base: dict[tuple, list[dict]] = {}
        existing_marked = Counter()
        for item in items:
            identity = cls._receiving_item_identity_key(item)
            marking_code = identity[-2]
            qty = identity[-1]
            if not marking_code or qty != 1:
                continue
            expected_by_base.setdefault(identity[:-2], []).append(item)
        for snapshot in snapshots:
            identity = cls._receiving_snapshot_identity_key(snapshot)
            if identity[-2]:
                existing_marked[identity] += 1

        clones: list[WarehouseStockSnapshot] = []
        expanded = 0
        for base_key, expected_items in expected_by_base.items():
            remaining_items: list[dict] = []
            marked_left = Counter(existing_marked)
            for item in expected_items:
                identity = cls._receiving_item_identity_key(item)
                if marked_left[identity] > 0:
                    marked_left[identity] -= 1
                else:
                    remaining_items.append(item)
            source_rows = [
                snapshot
                for snapshot in snapshots
                if not str(snapshot.marking_code or "").strip()
                and cls._receiving_snapshot_identity_key(snapshot)[:-2] == base_key
            ]
            if not source_rows or not remaining_items:
                continue
            if sum(max(int(snapshot.qty or 0), 0) for snapshot in source_rows) != len(remaining_items):
                continue

            item_offset = 0
            prepared: list[tuple[WarehouseStockSnapshot, list[dict], list[dict[str, int]]]] = []
            valid = True
            for snapshot in source_rows:
                row_qty = max(int(snapshot.qty or 0), 0)
                row_items = remaining_items[item_offset : item_offset + row_qty]
                item_offset += row_qty
                reserved = {
                    "processing_reserved_qty": max(int(snapshot.processing_reserved_qty or 0), 0),
                    "shipping_reserved_qty": max(int(snapshot.shipping_reserved_qty or 0), 0),
                    "other_reserved_qty": max(int(snapshot.other_reserved_qty or 0), 0),
                }
                if sum(reserved.values()) > row_qty or len(row_items) != row_qty:
                    valid = False
                    break
                allocations = [
                    {
                        "processing_reserved_qty": 0,
                        "shipping_reserved_qty": 0,
                        "other_reserved_qty": 0,
                    }
                    for _ in row_items
                ]
                slot = 0
                for field, amount in reserved.items():
                    for _ in range(amount):
                        while slot < len(allocations) and sum(allocations[slot].values()) >= 1:
                            slot += 1
                        if slot >= len(allocations):
                            valid = False
                            break
                        allocations[slot][field] = 1
                        slot += 1
                    if not valid:
                        break
                if not valid:
                    break
                prepared.append((snapshot, row_items, allocations))
            if not valid or item_offset != len(remaining_items):
                continue

            for snapshot, row_items, allocations in prepared:
                original_version = int(snapshot.snapshot_version or 1)
                for index, (item, allocation) in enumerate(zip(row_items, allocations)):
                    marking_code = str(item.get("marking_code") or "").strip()
                    available_qty = 1 - sum(allocation.values())
                    if index == 0:
                        snapshot.marking_code = marking_code
                        snapshot.qty = 1
                        snapshot.available_qty = available_qty
                        snapshot.processing_reserved_qty = allocation["processing_reserved_qty"]
                        snapshot.shipping_reserved_qty = allocation["shipping_reserved_qty"]
                        snapshot.other_reserved_qty = allocation["other_reserved_qty"]
                        snapshot.snapshot_version = original_version + 1
                        snapshot.save(
                            update_fields=[
                                "marking_code", "qty", "available_qty",
                                "processing_reserved_qty", "shipping_reserved_qty",
                                "other_reserved_qty", "snapshot_version", "updated_at",
                            ]
                        )
                        continue
                    clones.append(
                        WarehouseStockSnapshot(
                            agency_id=snapshot.agency_id,
                            stock_unit_type=snapshot.stock_unit_type,
                            source_context_type=snapshot.source_context_type,
                            source_context_id=snapshot.source_context_id,
                            sku_ref_id=snapshot.sku_ref_id,
                            sku_code=snapshot.sku_code,
                            name=snapshot.name,
                            size=snapshot.size,
                            barcode=snapshot.barcode,
                            goods_type=snapshot.goods_type,
                            marking_code=marking_code,
                            qty=1,
                            available_qty=available_qty,
                            processing_reserved_qty=allocation["processing_reserved_qty"],
                            shipping_reserved_qty=allocation["shipping_reserved_qty"],
                            other_reserved_qty=allocation["other_reserved_qty"],
                            container_id=snapshot.container_id,
                            container_code=snapshot.container_code,
                            parent_container_id=snapshot.parent_container_id,
                            location_id=snapshot.location_id,
                            zone_code=snapshot.zone_code,
                            zone_kind=snapshot.zone_kind,
                            warehouse_state_code=snapshot.warehouse_state_code,
                            active_operation_id=snapshot.active_operation_id,
                            active_operation_type=snapshot.active_operation_type,
                            current_trip_id=snapshot.current_trip_id,
                            is_in_vehicle=snapshot.is_in_vehicle,
                            is_archived=snapshot.is_archived,
                            snapshot_version=original_version + 1,
                            last_event_id=snapshot.last_event_id,
                        )
                    )
                expanded += len(row_items)
        if clones:
            WarehouseStockSnapshot.objects.bulk_create(clones, batch_size=500)
        return expanded

    @classmethod
    def receiving_placement_coverage(
        cls,
        *,
        agency: Agency,
        order_id: str,
        placement_payload: dict,
    ) -> dict:
        order_key = str(order_id or "").strip()
        items = cls._receiving_items_from_placement_payload(
            order_id=order_key,
            placement_payload=placement_payload,
        )
        materialized_pallet_codes, materialized_box_codes = (
            cls._receiving_materialized_container_codes(
                order_id=order_key,
                placement_payload=placement_payload,
            )
        )
        expected = Counter()
        expected_labels: dict[tuple, str] = {}
        for item in items:
            pallet_key = cls._receiving_identity_value(item.get("pallet_code"))
            box_key = cls._receiving_identity_value(item.get("box_code"))
            if pallet_key in materialized_pallet_codes or box_key in materialized_box_codes:
                continue
            identity_key = cls._receiving_item_identity_key(item)
            expected[identity_key] += 1
            pallet_code = str(item.get("pallet_code") or "").strip()
            box_code = str(item.get("box_code") or "").strip()
            expected_labels.setdefault(
                identity_key,
                " / ".join(value for value in (pallet_code, box_code) if value)
                or "без контейнера",
            )

        snapshots = list(
            WarehouseStockSnapshot.objects.select_related(
                "container",
                "container__parent_container",
                "parent_container",
            )
            .filter(
                agency=agency,
                source_context_type="receiving",
                source_context_id=order_key,
                is_archived=False,
                qty__gt=0,
            )
            .order_by("id")
        )
        actual = Counter()
        actual_labels: dict[tuple, str] = {}
        for snapshot in snapshots:
            container = snapshot.container
            parent = snapshot.parent_container
            if parent is None and container is not None:
                parent = container.parent_container
            pallet_key = cls._receiving_identity_value(
                getattr(parent, "container_code", "")
            )
            if (
                not pallet_key
                and container is not None
                and container.container_type != WarehouseContainer.TYPE_BOX
            ):
                pallet_key = cls._receiving_identity_value(container.container_code)
            box_key = ""
            if container is not None and container.container_type == WarehouseContainer.TYPE_BOX:
                box_key = cls._receiving_identity_value(container.container_code)
            if pallet_key in materialized_pallet_codes or box_key in materialized_box_codes:
                continue
            identity_key = cls._receiving_snapshot_identity_key(snapshot)
            actual[identity_key] += 1
            pallet_code = str(
                getattr(snapshot.parent_container, "container_code", "") or ""
            ).strip()
            box_code = str(
                getattr(snapshot.container, "container_code", "")
                or snapshot.container_code
                or ""
            ).strip()
            actual_labels.setdefault(
                identity_key,
                " / ".join(value for value in (pallet_code, box_code) if value)
                or "без контейнера",
            )

        missing = expected - actual
        unexpected = actual - expected
        cls._cancel_blank_receiving_barcode_matches(missing, unexpected)

        def labels(counter: Counter, source: dict[tuple, str]) -> list[str]:
            result = []
            for identity_key, count in counter.items():
                label = source.get(identity_key) or str(identity_key[1] or "без контейнера")
                result.append(f"{label} x{count}" if count > 1 else label)
            return result

        return {
            "complete": not missing and not unexpected,
            "expected_count": sum(expected.values()),
            "actual_count": sum(actual.values()),
            "missing_count": sum(missing.values()),
            "unexpected_count": sum(unexpected.values()),
            "materialized_pallet_count": len(materialized_pallet_codes),
            "materialized_box_count": len(materialized_box_codes),
            "missing_labels": labels(missing, expected_labels),
            "unexpected_labels": labels(unexpected, actual_labels),
        }

    @classmethod
    def ensure_receiving_placement_complete(
        cls,
        *,
        agency: Agency,
        order_id: str,
        placement_payload: dict,
    ) -> dict:
        coverage = cls.receiving_placement_coverage(
            agency=agency,
            order_id=order_id,
            placement_payload=placement_payload,
        )
        if coverage["complete"]:
            return coverage
        details = []
        if coverage["missing_labels"]:
            details.append(f"отсутствуют: {', '.join(coverage['missing_labels'][:5])}")
        if coverage["unexpected_labels"]:
            details.append(f"лишние: {', '.join(coverage['unexpected_labels'][:5])}")
        raise ValueError(
            "Складские данные приемки не совпадают с закрытыми коробами"
            + (f" ({'; '.join(details)})" if details else "")
            + ". Изменения отменены."
        )

    @staticmethod
    def _receiving_item_qty(item: dict) -> int:
        try:
            return max(int(float(str(item.get("qty") or 0).replace(",", "."))), 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _sync_receiving_snapshot_qty(cls, snapshot: WarehouseStockSnapshot, item: dict) -> bool:
        next_qty = cls._receiving_item_qty(item)
        current_qty = max(int(snapshot.qty or 0), 0)
        if next_qty == current_qty:
            return True
        reserved_qty = (
            int(snapshot.processing_reserved_qty or 0)
            + int(snapshot.shipping_reserved_qty or 0)
            + int(snapshot.other_reserved_qty or 0)
        )
        if next_qty < reserved_qty:
            return False
        snapshot.qty = next_qty
        snapshot.available_qty = max(next_qty - reserved_qty, 0)
        snapshot.snapshot_version = int(snapshot.snapshot_version or 1) + 1
        snapshot.save(update_fields=["qty", "available_qty", "snapshot_version", "updated_at"])
        return True

    @classmethod
    def normalize_box_characteristics(cls, payload: dict | None) -> dict[str, int]:
        if not isinstance(payload, dict):
            return {}
        aliases = {
            "gross_weight_g": ("gross_weight_g", "weight_g", "weight_grams"),
            "width_mm": ("width_mm",),
            "height_mm": ("height_mm",),
            "depth_mm": ("depth_mm", "length_mm"),
        }
        normalized: dict[str, int] = {}
        for field, field_aliases in aliases.items():
            value = None
            for alias in field_aliases:
                candidate = cls._parse_positive_int(payload.get(alias))
                if candidate is not None:
                    value = candidate
                    break
            if value is not None:
                normalized[field] = value
        return normalized

    @classmethod
    def box_has_complete_characteristics(cls, payload: dict | None) -> bool:
        normalized = cls.normalize_box_characteristics(payload)
        return all(field in normalized for field in cls.BOX_CHARACTERISTIC_FIELDS)

    @classmethod
    @transaction.atomic
    def sync_box_characteristics(
        cls,
        *,
        agency: Agency | None,
        order_id: str,
        order_type: str,
        boxes: list[dict] | None,
        performed_by=None,
        clear_incomplete: bool = False,
    ) -> list[str]:
        if not agency:
            return []
        order_key = str(order_id or "").strip()
        normalized_order_type = str(order_type or "").strip()
        updated_codes: list[str] = []
        seen_codes: set[str] = set()
        prepared_boxes: list[dict] = []
        for raw_box in boxes or []:
            if not isinstance(raw_box, dict):
                continue
            box_code = str(raw_box.get("code") or "").strip()
            if not box_code or box_code in seen_codes:
                continue
            seen_codes.add(box_code)
            characteristics = cls.normalize_box_characteristics(raw_box)
            has_complete_characteristics = all(field in characteristics for field in cls.BOX_CHARACTERISTIC_FIELDS)
            prepared_boxes.append(
                {
                    "code": box_code,
                    "characteristics": characteristics,
                    "has_complete_characteristics": has_complete_characteristics,
                }
            )
        if not prepared_boxes:
            return updated_codes
        containers_by_code = {
            str(container.container_code or "").casefold(): container
            for container in WarehouseContainer.objects.filter(
                agency=agency,
                container_code__in=[item["code"] for item in prepared_boxes],
            )
        }
        containers_to_create: list[WarehouseContainer] = []
        containers_to_update: list[WarehouseContainer] = []
        update_field_names = {
            "container_type",
            "source_context_type",
            "source_context_id",
            *cls.BOX_CHARACTERISTIC_FIELDS,
            "updated_at",
        }
        now = timezone.now()
        for item in prepared_boxes:
            box_code = item["code"]
            characteristics = item["characteristics"]
            has_complete_characteristics = item["has_complete_characteristics"]
            container = containers_by_code.get(box_code.casefold())
            if not has_complete_characteristics:
                if clear_incomplete and container is not None:
                    changed = False
                    if container.container_type != WarehouseContainer.TYPE_BOX:
                        container.container_type = WarehouseContainer.TYPE_BOX
                        changed = True
                    if normalized_order_type and container.source_context_type != normalized_order_type:
                        container.source_context_type = normalized_order_type
                        changed = True
                    if order_key and container.source_context_id != order_key:
                        container.source_context_id = order_key
                        changed = True
                    for field in cls.BOX_CHARACTERISTIC_FIELDS:
                        if getattr(container, field) is not None:
                            setattr(container, field, None)
                            changed = True
                    if changed:
                        container.updated_at = now
                        containers_to_update.append(container)
                continue
            if container is None:
                container = WarehouseContainer(
                    agency=agency,
                    container_code=box_code,
                    container_type=WarehouseContainer.TYPE_BOX,
                    created_by=performed_by if getattr(performed_by, "is_authenticated", False) else None,
                    source_context_type=normalized_order_type,
                    source_context_id=order_key,
                    **characteristics,
                )
                containers_to_create.append(container)
                containers_by_code[box_code.casefold()] = container
                updated_codes.append(box_code)
                continue
            changed = False
            if container.container_type != WarehouseContainer.TYPE_BOX:
                container.container_type = WarehouseContainer.TYPE_BOX
                changed = True
            for field, value in characteristics.items():
                if getattr(container, field) != value:
                    setattr(container, field, value)
                    changed = True
            if normalized_order_type and container.source_context_type != normalized_order_type:
                container.source_context_type = normalized_order_type
                changed = True
            if order_key and container.source_context_id != order_key:
                container.source_context_id = order_key
                changed = True
            if changed:
                container.updated_at = now
                containers_to_update.append(container)
            updated_codes.append(box_code)
        if containers_to_create:
            WarehouseContainer.objects.bulk_create(containers_to_create, batch_size=500)
        if containers_to_update:
            WarehouseContainer.objects.bulk_update(containers_to_update, sorted(update_field_names), batch_size=500)
        return updated_codes

    @staticmethod
    def _shipping_snapshot_flow_qty(snapshot: WarehouseStockSnapshot) -> int:
        state_code = str(snapshot.warehouse_state_code or "").strip()
        if state_code in {
            WarehouseStateCode.IN_OTG.value,
            WarehouseStateCode.PALLETIZING.value,
            WarehouseStateCode.READY_FOR_LOADING.value,
            WarehouseStateCode.ASSIGNED_TO_TRIP.value,
            WarehouseStateCode.LOADING_IN_PROGRESS.value,
            WarehouseStateCode.LOADED_TO_VEHICLE.value,
        }:
            return int(snapshot.qty or 0)
        return int(snapshot.shipping_reserved_qty or snapshot.qty or 0)

    @classmethod
    def _archive_empty_source_pallets(cls, pallet_ids) -> int:
        normalized_ids = sorted({int(value) for value in pallet_ids or [] if int(value or 0) > 0})
        if not normalized_ids:
            return 0
        archived = 0
        pallets = WarehouseContainer.objects.select_for_update().filter(
            id__in=normalized_ids,
            container_type__in=[WarehouseContainer.TYPE_PALLET, WarehouseContainer.TYPE_MIXED_PALLET],
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        for pallet in pallets:
            source_has_children = WarehouseContainer.objects.filter(
                parent_container=pallet,
                status=WarehouseContainer.STATUS_ACTIVE,
            ).exists()
            source_has_snapshots = WarehouseStockSnapshot.objects.filter(
                Q(parent_container=pallet) | Q(container=pallet),
                is_archived=False,
                qty__gt=0,
            ).exclude(
                warehouse_state_code__in=(
                    WarehouseStateCode.PROCESSING_CONSUMED.value,
                    WarehouseStateCode.SHIPPED.value,
                    WarehouseStateCode.CANCELED.value,
                )
            ).exists()
            if source_has_children or source_has_snapshots:
                continue
            pallet.current_location = None
            pallet.parent_container = None
            pallet.status = WarehouseContainer.STATUS_ARCHIVED
            pallet.save(update_fields=["current_location", "parent_container", "status", "updated_at"])
            archived += 1
        return archived

    @classmethod
    def _archive_empty_shipping_source_containers(
        cls,
        *,
        agency: Agency,
        box_codes,
        pallet_code: str = "",
    ) -> tuple[int, int]:
        """Detach empty source boxes and release their now-empty pallet location."""
        normalized_box_codes = []
        seen_codes = set()
        for raw_code in box_codes or []:
            code = str(raw_code or "").strip()
            key = code.casefold()
            if not code or key in seen_codes:
                continue
            seen_codes.add(key)
            normalized_box_codes.append(code)

        pallet_ids = set()
        archived_boxes = 0
        if normalized_box_codes:
            box_code_filter = Q()
            for code in normalized_box_codes:
                box_code_filter |= Q(container_code__iexact=code)
            boxes = (
                WarehouseContainer.objects.select_for_update()
                .filter(
                    box_code_filter,
                    agency=agency,
                    container_type=WarehouseContainer.TYPE_BOX,
                    status=WarehouseContainer.STATUS_ACTIVE,
                )
                .order_by("id")
            )
            for box in boxes:
                linked_snapshots = WarehouseStockSnapshot.objects.filter(agency=agency).filter(
                    Q(container=box) | Q(container_code__iexact=box.container_code)
                )
                has_live_stock = linked_snapshots.filter(is_archived=False, qty__gt=0).exists()
                has_reserve = linked_snapshots.filter(
                    Q(processing_reserved_qty__gt=0)
                    | Q(shipping_reserved_qty__gt=0)
                    | Q(other_reserved_qty__gt=0)
                ).exists()
                has_open_operation = linked_snapshots.filter(active_operation__isnull=False).exclude(
                    active_operation__status__in=_FINAL_OPERATION_STATUSES
                ).exists()
                if has_live_stock or has_reserve or has_open_operation:
                    continue
                if box.parent_container_id:
                    pallet_ids.add(int(box.parent_container_id))
                box.current_location = None
                box.parent_container = None
                box.status = WarehouseContainer.STATUS_ARCHIVED
                box.save(
                    update_fields=["current_location", "parent_container", "status", "updated_at"]
                )
                archived_boxes += 1

        normalized_pallet_code = str(pallet_code or "").strip()
        if normalized_pallet_code:
            source_pallet_id = (
                WarehouseContainer.objects.select_for_update()
                .filter(
                    agency=agency,
                    container_code__iexact=normalized_pallet_code,
                    container_type__in=[
                        WarehouseContainer.TYPE_PALLET,
                        WarehouseContainer.TYPE_MIXED_PALLET,
                    ],
                    status=WarehouseContainer.STATUS_ACTIVE,
                )
                .values_list("id", flat=True)
                .first()
            )
            if source_pallet_id:
                pallet_ids.add(int(source_pallet_id))

        return archived_boxes, cls._archive_empty_source_pallets(pallet_ids)

    @staticmethod
    def _shipping_reserve_identity(row) -> tuple[str, str, str, str]:
        return (
            str(getattr(row, "sku_code", "") or "").strip(),
            str(getattr(row, "size", "") or "").strip(),
            str(getattr(row, "barcode", "") or "").strip(),
            str(getattr(row, "goods_type", "") or "").strip(),
        )

    @classmethod
    def _validated_shipping_reserves_for_ship(
        cls,
        *,
        snapshots: list[WarehouseStockSnapshot],
        reserves: list[WarehouseReserve],
    ) -> dict[tuple[str, str, str, str], list[WarehouseReserve]]:
        reserves_by_identity: dict[tuple[str, str, str, str], list[WarehouseReserve]] = {}
        for reserve in reserves:
            reserves_by_identity.setdefault(cls._shipping_reserve_identity(reserve), []).append(reserve)

        shipped_qty_by_identity: Counter = Counter()
        for snapshot in snapshots:
            shipped_qty_by_identity[cls._shipping_reserve_identity(snapshot)] += max(
                cls._shipping_snapshot_flow_qty(snapshot),
                0,
            )

        for identity, shipped_qty in shipped_qty_by_identity.items():
            matching_reserves = reserves_by_identity.get(identity, [])
            if not matching_reserves:
                # Keep compatibility with legacy orders that have no exact reserve
                # identity. The event remains unbound, as it was before this guard.
                continue
            satisfied_qty = sum(max(int(reserve.qty_satisfied or 0), 0) for reserve in matching_reserves)
            if satisfied_qty < shipped_qty:
                sku_code, size, barcode, goods_type = identity
                raise ValueError(
                    "Отгрузка заблокирована: товар не полностью отражён в резервах после прибытия в OTG "
                    f"(SKU {sku_code or '-'}, размер {size or '-'}, штрихкод {barcode or '-'}, "
                    f"тип {goods_type or '-'}; в резервах {satisfied_qty}, к отгрузке {shipped_qty})."
                )
        return reserves_by_identity

    @classmethod
    def validate_shipping_pallet_ownership(
        cls,
        *,
        agency: Agency,
        order_id: str,
        snapshots: list[WarehouseStockSnapshot] | None = None,
        require_pallets: bool = True,
        lock: bool = False,
    ) -> dict:
        order_key = str(order_id or "").strip()
        if not order_key:
            raise ValueError("Shipping order id is required for pallet validation")

        if snapshots is None:
            snapshots_qs = (
                WarehouseStockSnapshot.objects.select_related(
                    "active_operation",
                    "last_event",
                    "container",
                    "parent_container",
                )
                .filter(
                    agency=agency,
                    warehouse_state_code__in=[
                        WarehouseStateCode.IN_OTG.value,
                        WarehouseStateCode.PALLETIZING.value,
                        WarehouseStateCode.READY_FOR_LOADING.value,
                        WarehouseStateCode.ASSIGNED_TO_TRIP.value,
                        WarehouseStateCode.LOADING_IN_PROGRESS.value,
                        WarehouseStateCode.LOADED_TO_VEHICLE.value,
                    ],
                    is_archived=False,
                )
                .order_by("id")
            )
            if lock:
                snapshots_qs = snapshots_qs.select_for_update(of=("self",))
            snapshots = [
                snapshot
                for snapshot in snapshots_qs
                if cls._snapshot_matches_shipping_context(snapshot, order_key)
            ]
        else:
            snapshots = list(snapshots)

        if not snapshots:
            if require_pallets:
                raise ValueError("Заявка заблокирована: складские остатки заявки не найдены.")
            return {"pallet_ids": [], "pallet_codes": [], "pallet_count": 0, "snapshot_count": 0}

        missing_parent = [snapshot.id for snapshot in snapshots if not snapshot.parent_container_id]
        if missing_parent and require_pallets:
            raise ValueError("Заявка заблокирована: товар не размещен на паллетах.")

        parent_ids = sorted(
            {int(snapshot.parent_container_id) for snapshot in snapshots if snapshot.parent_container_id}
        )
        if not parent_ids:
            if require_pallets:
                raise ValueError("Заявка заблокирована: у заявки ноль паллет.")
            return {
                "pallet_ids": [],
                "pallet_codes": [],
                "pallet_count": 0,
                "snapshot_count": len(snapshots),
            }

        cached_parents = {
            int(snapshot.parent_container_id): snapshot._state.fields_cache.get("parent_container")
            for snapshot in snapshots
            if snapshot.parent_container_id
            and snapshot._state.fields_cache.get("parent_container") is not None
        }
        if not lock and set(cached_parents) == set(parent_ids):
            parents = [cached_parents[parent_id] for parent_id in parent_ids]
        else:
            parent_qs = WarehouseContainer.objects.filter(agency=agency, id__in=parent_ids).order_by("id")
            if lock:
                parent_qs = parent_qs.select_for_update()
            parents = list(parent_qs)
        if len(parents) != len(parent_ids):
            raise ValueError("Заявка заблокирована: одна из паллет не найдена.")

        allowed_pallet_types = {
            WarehouseContainer.TYPE_PALLET,
            WarehouseContainer.TYPE_MIXED_PALLET,
        }
        for parent in parents:
            code = str(parent.container_code or "").strip() or f"#{parent.id}"
            if parent.container_type not in allowed_pallet_types:
                raise ValueError(f"Заявка заблокирована: контейнер {code} не является паллетой.")
            owner_type = str(parent.source_context_type or "").strip().lower()
            owner_id = str(parent.source_context_id or "").strip()
            if owner_type != "shipping" or owner_id != order_key:
                raise ValueError(
                    f"Заявка заблокирована: паллета {code} принадлежит другой заявке."
                )

        for snapshot in snapshots:
            container = snapshot.container
            if (
                container is not None
                and container.container_type == WarehouseContainer.TYPE_BOX
                and container.parent_container_id != snapshot.parent_container_id
            ):
                raise ValueError(
                    "Заявка заблокирована: связь короба со складской паллетой не совпадает."
                )

        current_snapshot_ids = {int(snapshot.id) for snapshot in snapshots if snapshot.id}
        cached_parent_snapshots_available = not lock and all(
            hasattr(snapshot, "_shipping_parent_snapshots_cache")
            for snapshot in snapshots
            if snapshot.parent_container_id
        )
        if cached_parent_snapshots_available:
            foreign_snapshots_by_id = {
                int(candidate.id): candidate
                for snapshot in snapshots
                for candidate in snapshot._shipping_parent_snapshots_cache
                if candidate.id and int(candidate.id) not in current_snapshot_ids
            }
            foreign_snapshots = [
                foreign_snapshots_by_id[snapshot_id]
                for snapshot_id in sorted(foreign_snapshots_by_id)
            ]
        else:
            foreign_qs = (
                WarehouseStockSnapshot.objects.select_related("active_operation", "last_event")
                .filter(
                    agency=agency,
                    parent_container_id__in=parent_ids,
                    is_archived=False,
                )
                .exclude(id__in=current_snapshot_ids)
                .order_by("id")
            )
            if lock:
                foreign_qs = foreign_qs.select_for_update(of=("self",))
            foreign_snapshots = list(foreign_qs)
        for foreign_snapshot in foreign_snapshots:
            active_operation = foreign_snapshot.active_operation
            last_event = foreign_snapshot.last_event
            foreign_order_id = ""
            if (
                active_operation is not None
                and str(active_operation.context_type or "").strip().lower() == "shipping"
            ):
                foreign_order_id = str(active_operation.context_id or "").strip()
            elif (
                last_event is not None
                and str(last_event.stock_context_type or "").strip().lower() == "shipping"
            ):
                foreign_order_id = str(last_event.stock_context_id or "").strip()
            if foreign_order_id != order_key:
                parent = next(
                    (item for item in parents if item.id == foreign_snapshot.parent_container_id),
                    None,
                )
                code = str(getattr(parent, "container_code", "") or "").strip() or f"#{foreign_snapshot.parent_container_id}"
                raise ValueError(
                    f"Заявка заблокирована: паллета {code} используется в нескольких заявках."
                )

        return {
            "pallet_ids": parent_ids,
            "pallet_codes": [str(parent.container_code or "").strip() for parent in parents],
            "pallet_count": len(parent_ids),
            "snapshot_count": len(snapshots),
        }

    @staticmethod
    def _shipping_rebind_target_qty(snapshot: WarehouseStockSnapshot) -> int:
        state_code = str(snapshot.warehouse_state_code or "").strip()
        if state_code in {
            WarehouseStateCode.IN_OTG.value,
            WarehouseStateCode.PALLETIZING.value,
            WarehouseStateCode.READY_FOR_LOADING.value,
            WarehouseStateCode.ASSIGNED_TO_TRIP.value,
            WarehouseStateCode.LOADING_IN_PROGRESS.value,
            WarehouseStateCode.LOADED_TO_VEHICLE.value,
        }:
            return int(snapshot.qty or 0)
        return int(snapshot.available_qty or 0)

    @classmethod
    def _mark_shipping_reserve_arrived_to_otg(
        cls,
        *,
        snapshot: WarehouseStockSnapshot,
        order_id: str,
    ) -> None:
        cls._mark_shipping_reserves_arrived_to_otg(
            snapshots=[snapshot],
            order_id=order_id,
        )

    @classmethod
    def _mark_shipping_reserves_arrived_to_otg(
        cls,
        *,
        snapshots: list[WarehouseStockSnapshot],
        order_id: str,
    ) -> None:
        """Satisfy shipping reserves for an OTG arrival without per-box queries."""
        arrival_snapshots = [
            snapshot
            for snapshot in snapshots
            if snapshot is not None and cls._shipping_snapshot_flow_qty(snapshot) > 0
        ]
        if not arrival_snapshots:
            return

        order_key = str(order_id or "").strip()
        active_statuses = [
            WarehouseReserve.STATUS_ACTIVE,
            WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
            WarehouseReserve.STATUS_ALLOCATED,
            WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
            WarehouseReserve.STATUS_SATISFIED,
        ]
        snapshots_by_agency: dict[int, list[WarehouseStockSnapshot]] = {}
        for snapshot in arrival_snapshots:
            snapshots_by_agency.setdefault(int(snapshot.agency_id), []).append(snapshot)

        for agency_id, agency_snapshots in snapshots_by_agency.items():
            reserves = list(
                WarehouseReserve.objects.select_for_update()
                .filter(
                    agency_id=agency_id,
                    reserve_type=WarehouseReserve.TYPE_SHIPPING,
                    context_type="shipping",
                    context_id=order_key,
                    status__in=active_statuses,
                )
                .order_by("id")
            )
            if not reserves:
                continue

            exact_reserves_by_key: dict[tuple[str, str, str], list[WarehouseReserve]] = {}
            barcode_reserves_by_key: dict[tuple[str, str, str], list[WarehouseReserve]] = {}
            # A reserve created from an order line without a goods type would never
            # meet typed stock: both keys above carry the goods type, so ``('0', '')``
            # cannot match ``('0', 'gv')`` and the reserve stays open forever while
            # the goods already wait in OTG.  Index those reserves without the type
            # as well and use it only after the typed lookups miss.
            untyped_reserves_by_sku: dict[tuple[str, str], list[WarehouseReserve]] = {}
            untyped_reserves_by_barcode: dict[tuple[str, str], list[WarehouseReserve]] = {}
            for reserve in reserves:
                reserve_size = str(reserve.size or "")
                common_key = (
                    reserve_size,
                    str(reserve.goods_type or ""),
                )
                exact_reserves_by_key.setdefault(
                    (*common_key, str(reserve.sku_code or "")),
                    [],
                ).append(reserve)
                barcode_reserves_by_key.setdefault(
                    (*common_key, str(reserve.barcode or "")),
                    [],
                ).append(reserve)
                if not str(reserve.goods_type or "").strip():
                    untyped_reserves_by_sku.setdefault(
                        (reserve_size, str(reserve.sku_code or "")),
                        [],
                    ).append(reserve)
                    untyped_reserves_by_barcode.setdefault(
                        (reserve_size, str(reserve.barcode or "")),
                        [],
                    ).append(reserve)

            reserve_snapshot_id_by_reserve = cls._reserve_snapshot_id_map(reserves)
            changed_reserves: dict[int, WarehouseReserve] = {}
            for snapshot in agency_snapshots:
                remaining_qty = cls._shipping_snapshot_flow_qty(snapshot)
                common_key = (
                    str(snapshot.size or ""),
                    str(snapshot.goods_type or ""),
                )
                exact_reserves = exact_reserves_by_key.get(
                    (*common_key, str(snapshot.sku_code or "")),
                    [],
                )
                snapshot_size = str(snapshot.size or "")
                snapshot_barcode = str(snapshot.barcode or "").strip()
                if any(cls._reserve_open_qty(reserve) > 0 for reserve in exact_reserves):
                    matching_reserves = exact_reserves
                elif snapshot_barcode and any(
                    cls._reserve_open_qty(reserve) > 0
                    for reserve in barcode_reserves_by_key.get(
                        (*common_key, snapshot_barcode), []
                    )
                ):
                    # Legacy stock can retain an old SKU after nomenclature correction.
                    # The barcode remains the stable identity inside one shipping order.
                    matching_reserves = barcode_reserves_by_key.get(
                        (*common_key, snapshot_barcode),
                        [],
                    )
                else:
                    # Last resort inside the same order: a reserve that carries no
                    # goods type at all.  Typed stock can satisfy it, and nothing
                    # else ever will.
                    untyped_reserves = untyped_reserves_by_sku.get(
                        (snapshot_size, str(snapshot.sku_code or "")),
                        [],
                    )
                    if not any(
                        cls._reserve_open_qty(reserve) > 0 for reserve in untyped_reserves
                    ) and snapshot_barcode:
                        untyped_reserves = untyped_reserves_by_barcode.get(
                            (snapshot_size, snapshot_barcode),
                            [],
                        )
                    if any(
                        cls._reserve_open_qty(reserve) > 0 for reserve in untyped_reserves
                    ):
                        matching_reserves = untyped_reserves
                    elif snapshot_barcode:
                        matching_reserves = barcode_reserves_by_key.get(
                            (*common_key, snapshot_barcode),
                            [],
                        )
                    else:
                        matching_reserves = exact_reserves

                snapshot_id = int(snapshot.id or 0)
                snapshot_sku_key = str(snapshot.sku_code or "").strip().casefold()
                ordered_reserves = sorted(
                    matching_reserves,
                    key=lambda reserve: (
                        0
                        if int(reserve_snapshot_id_by_reserve.get(int(reserve.id or 0)) or 0)
                        == snapshot_id
                        else 1,
                        0
                        if str(reserve.sku_code or "").strip().casefold() == snapshot_sku_key
                        else 1,
                        int(reserve.id or 0),
                    ),
                )
                for reserve in ordered_reserves:
                    if remaining_qty <= 0:
                        break
                    open_qty = cls._reserve_open_qty(reserve)
                    if open_qty <= 0:
                        continue
                    satisfied_qty = min(open_qty, remaining_qty)
                    remaining_qty -= satisfied_qty
                    reserve.qty_allocated = max(
                        int(reserve.qty_allocated or 0),
                        int(reserve.qty_satisfied or 0) + satisfied_qty,
                    )
                    reserve.qty_satisfied = min(
                        int(reserve.qty_reserved or 0),
                        int(reserve.qty_satisfied or 0) + satisfied_qty,
                    )
                    if int(reserve.qty_satisfied or 0) >= int(reserve.qty_reserved or 0):
                        reserve.status = WarehouseReserve.STATUS_SATISFIED
                    elif int(reserve.qty_satisfied or 0) > 0:
                        reserve.status = WarehouseReserve.STATUS_PARTIALLY_SATISFIED
                    elif int(reserve.qty_allocated or 0) >= int(reserve.qty_reserved or 0):
                        reserve.status = WarehouseReserve.STATUS_ALLOCATED
                    elif int(reserve.qty_allocated or 0) > 0:
                        reserve.status = WarehouseReserve.STATUS_PARTIALLY_ALLOCATED
                    else:
                        reserve.status = WarehouseReserve.STATUS_ACTIVE
                    reserve.updated_at = timezone.now()
                    changed_reserves[int(reserve.id)] = reserve

            if changed_reserves:
                WarehouseReserve.objects.bulk_update(
                    list(changed_reserves.values()),
                    ["qty_allocated", "qty_satisfied", "status", "updated_at"],
                )

    @classmethod
    @transaction.atomic
    def mark_shipping_boxes_arrived_to_otg(
        cls,
        *,
        agency: Agency,
        order_id: str,
        box_codes: list[str],
        performed_by=None,
        box_payloads: dict[str, dict] | None = None,
        destination_location_code: str = "",
        allow_legacy_generic_destination: bool = False,
    ) -> int:
        order_key = str(order_id or "").strip()
        if not agency or not order_key:
            return 0
        normalized_codes: list[str] = []
        seen_codes: set[str] = set()
        for raw_code in box_codes or []:
            code = str(raw_code or "").strip()
            key = code.lower()
            if not code or key in seen_codes:
                continue
            seen_codes.add(key)
            normalized_codes.append(code)
        if not normalized_codes:
            return 0

        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("container", "location")
            .filter(
                agency=agency,
                is_archived=False,
                qty__gt=0,
            )
            .filter(Q(container_code__in=normalized_codes) | Q(container__container_code__in=normalized_codes))
            .order_by("id")
        )
        source_pallet_ids = {
            int(snapshot.parent_container_id)
            for snapshot in snapshots
            if snapshot.parent_container_id
        }
        total_marked = 0
        now = timezone.now()
        destination = cls.concrete_movement_destination(
            warehouse_code="MSK",
            zone_code="OTG",
            location_code=destination_location_code,
            allow_legacy_generic_location=allow_legacy_generic_destination,
        )
        box_payloads = box_payloads or {}
        snapshot_codes = {
            str(snapshot.container_code or getattr(snapshot.container, "container_code", "") or "").strip().lower()
            for snapshot in snapshots
            if str(snapshot.container_code or getattr(snapshot.container, "container_code", "") or "").strip()
        }
        recovered_snapshots: list[WarehouseStockSnapshot] = []
        for code in normalized_codes:
            box_key = code.lower()
            if box_key in snapshot_codes:
                continue
            box_payload = box_payloads.get(box_key)
            if not isinstance(box_payload, dict):
                continue
            items = [dict(item) for item in (box_payload.get("items") or []) if isinstance(item, dict)]
            if not items:
                continue
            container, _created = WarehouseContainer.objects.get_or_create(
                agency=agency,
                container_code=code,
                defaults={
                    "container_type": WarehouseContainer.TYPE_BOX,
                    "current_location": destination,
                    "created_by": performed_by if getattr(performed_by, "is_authenticated", False) else None,
                    "source_context_type": "shipping",
                    "source_context_id": order_key,
                },
            )
            container_updates: list[str] = []
            if container.container_type != WarehouseContainer.TYPE_BOX:
                container.container_type = WarehouseContainer.TYPE_BOX
                container_updates.append("container_type")
            if container.current_location_id != destination.id:
                container.current_location = destination
                container_updates.append("current_location")
            if container.parent_container_id is not None:
                container.parent_container = None
                container_updates.append("parent_container")
            if str(container.source_context_type or "").strip() != "shipping":
                container.source_context_type = "shipping"
                container_updates.append("source_context_type")
            if str(container.source_context_id or "").strip() != order_key:
                container.source_context_id = order_key
                container_updates.append("source_context_id")
            if container_updates:
                container.save(update_fields=container_updates + ["updated_at"])
            for item in items:
                try:
                    qty = int(str(item.get("qty") or item.get("actual_qty") or item.get("count") or 0).strip())
                except (TypeError, ValueError):
                    qty = 0
                if qty <= 0:
                    continue
                event = WarehouseEvent.objects.create(
                    agency=agency,
                    event_type=WarehouseEventType.OTG_ARRIVED.value,
                    stock_context_type="shipping",
                    stock_context_id=order_key,
                    container=container,
                    from_location=None,
                    to_location=destination,
                    from_zone_code="",
                    to_zone_code=destination.zone_code,
                    qty=qty,
                    performed_by=performed_by,
                    performed_by_role=cls._role_of(performed_by) or "reachtruck",
                    occurred_at=now,
                    payload={
                        "whole_box_shipping_pick": True,
                        "recovered_from_mobile_placement_payload": True,
                        "box_code": code,
                    },
                )
                snapshot = WarehouseStockSnapshot.objects.create(
                    agency=agency,
                    stock_unit_type="item",
                    source_context_type=str(box_payload.get("source_context_type") or item.get("source_context_type") or "").strip(),
                    source_context_id=str(box_payload.get("source_context_id") or item.get("source_context_id") or "").strip(),
                    sku_code=str(item.get("sku_code") or item.get("sku") or "").strip(),
                    name=str(item.get("name") or "").strip(),
                    size=str(item.get("size") or "").strip(),
                    barcode=str(item.get("barcode") or "").strip(),
                    goods_type=str(item.get("goods_type") or "").strip(),
                    marking_code=str(item.get("marking_code") or "").strip(),
                    qty=qty,
                    available_qty=0,
                    processing_reserved_qty=0,
                    shipping_reserved_qty=0,
                    other_reserved_qty=0,
                    container=container,
                    container_code=code,
                    parent_container=None,
                    location=destination,
                    zone_code=destination.zone_code,
                    zone_kind=destination.zone_kind,
                    warehouse_state_code=WarehouseStateCode.IN_OTG.value,
                    active_operation=None,
                    active_operation_type="",
                    current_trip_id="",
                    is_in_vehicle=False,
                    last_event=event,
                )
                recovered_snapshots.append(snapshot)
                total_marked += qty
        cls._mark_shipping_reserves_arrived_to_otg(
            snapshots=[*snapshots, *recovered_snapshots],
            order_id=order_key,
        )
        for snapshot in snapshots:
            moved_qty = cls._shipping_snapshot_flow_qty(snapshot)
            if moved_qty <= 0:
                continue
            source_location = snapshot.location
            source_zone_code = snapshot.zone_code
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.OTG_ARRIVED.value,
                stock_context_type="shipping",
                stock_context_id=order_key,
                container=snapshot.container,
                from_location=source_location,
                to_location=destination,
                from_zone_code=source_zone_code,
                to_zone_code=destination.zone_code,
                qty=moved_qty,
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by) or "reachtruck",
                occurred_at=now,
                payload={
                    "whole_box_shipping_pick": True,
                    "box_code": str(snapshot.container_code or getattr(snapshot.container, "container_code", "") or ""),
                },
            )
            if str(snapshot.warehouse_state_code or "").strip() == WarehouseStateCode.PROCESSING_CONSUMED.value:
                event.payload = {
                    **dict(event.payload or {}),
                    "restored_confirmed_shipping_box": True,
                    "previous_warehouse_state_code": WarehouseStateCode.PROCESSING_CONSUMED.value,
                }
                event.save(update_fields=["payload", "updated_at"])
            container = snapshot.container
            if container is not None and str(container.container_type or "").strip() == WarehouseContainer.TYPE_BOX:
                container_updates: list[str] = []
                if container.parent_container_id is not None:
                    container.parent_container = None
                    container_updates.append("parent_container")
                if container.current_location_id != destination.id:
                    container.current_location = destination
                    container_updates.append("current_location")
                if container_updates:
                    container.save(update_fields=container_updates + ["updated_at"])
            snapshot.location = destination
            snapshot.zone_code = destination.zone_code
            snapshot.zone_kind = destination.zone_kind
            snapshot.shipping_reserved_qty = 0
            snapshot.processing_reserved_qty = 0
            snapshot.other_reserved_qty = 0
            snapshot.available_qty = 0
            snapshot.warehouse_state_code = WarehouseStateCode.IN_OTG.value
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
            if snapshot.parent_container_id is not None:
                snapshot.parent_container = None
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "location",
                    "zone_code",
                    "zone_kind",
                    "shipping_reserved_qty",
                    "processing_reserved_qty",
                    "other_reserved_qty",
                    "available_qty",
                    "warehouse_state_code",
                    "active_operation",
                    "active_operation_type",
                    "parent_container",
                    "last_event",
                    "updated_at",
                ]
            )
            total_marked += moved_qty
        cls._archive_empty_source_pallets(source_pallet_ids)
        return total_marked

    @classmethod
    def ensure_putaway_destination_available(
        cls,
        *,
        destination: WarehouseLocation | None,
        exclude_operation_id: int | None = None,
        exclude_container_code: str = "",
        exclude_draft_token: str = "",
        exclude_context_type: str = "",
        exclude_context_id: str = "",
    ) -> None:
        if destination is None:
            raise ValueError("Putaway destination is required")
        try:
            require_concrete_movement_location(destination, purpose="размещения товара")
        except ValidationError as exc:
            raise ValueError("; ".join(exc.messages)) from exc
        if str(destination.zone_code or "").strip().upper() != "OS":
            return
        if transaction.get_connection().in_atomic_block:
            destination = WarehouseLocation.objects.select_for_update().get(pk=destination.pk)
        PutawayDraftReservationService.cleanup_expired()

        location_label = cls._location_display_name(
            str(destination.zone_code or "").strip().upper(),
            int(destination.row_no or 0),
            int(destination.section_no or 0),
            int(destination.tier_no or 0),
            int(destination.cell_no or 0),
        )
        normalized_container_code = str(exclude_container_code or "").strip()

        occupied_snapshots = WarehouseStockSnapshot.objects.filter(
            location=destination,
            zone_code__iexact="OS",
            is_archived=False,
            qty__gt=0,
        ).exclude(
            warehouse_state_code__in=(
                WarehouseStateCode.PROCESSING_CONSUMED.value,
                WarehouseStateCode.SHIPPED.value,
                WarehouseStateCode.CANCELED.value,
            )
        )
        if normalized_container_code:
            occupied_snapshots = occupied_snapshots.exclude(
                models.Q(container_code__iexact=normalized_container_code)
                | models.Q(container__container_code__iexact=normalized_container_code)
                | models.Q(parent_container__container_code__iexact=normalized_container_code)
            )
        if occupied_snapshots.exists():
            raise ValueError(f"Место хранения {location_label} уже занято на складе.")

        if fbs_storage_occupies_location(destination):
            raise ValueError(
                f"Место хранения {location_label} занято или зарезервировано под FBS."
            )

        # Match the inspection screen even when an active physical pallet has
        # no snapshot rows.  The destination row is locked above, so competing
        # receiving/free-move commands cannot claim it simultaneously.
        container_message = os_physical_container_occupancy_message(
            destination, exclude_container_code=normalized_container_code,
        )
        if container_message:
            raise ValueError(container_message)

        reserved_operations = WarehouseOperation.objects.filter(
            operation_type=WarehouseOperation.TYPE_PUTAWAY,
            destination_location=destination,
            status__in=_PUTAWAY_DESTINATION_BLOCKING_STATUSES,
        )
        if exclude_operation_id:
            reserved_operations = reserved_operations.exclude(id=int(exclude_operation_id))
        if normalized_container_code:
            reserved_operations = reserved_operations.exclude(
                tasks__container__container_code__iexact=normalized_container_code
            ).distinct()
        normalized_draft_token = str(exclude_draft_token or "").strip()
        if normalized_draft_token:
            reserved_operations = reserved_operations.exclude(
                context_type=PutawayDraftReservationService.DRAFT_CONTEXT_TYPE,
                context_id=normalized_draft_token,
            )
        normalized_context_type = str(exclude_context_type or "").strip().lower()
        normalized_context_id = str(exclude_context_id or "").strip()
        if normalized_context_type and normalized_context_id:
            reserved_operations = reserved_operations.exclude(
                context_type__iexact=normalized_context_type,
                context_id=normalized_context_id,
            )
        if reserved_operations.exists():
            raise ValueError(
                f"Место хранения {location_label} уже зарезервировано другой заявкой ричтрака."
            )

    @classmethod
    @transaction.atomic
    def clear_receiving_context(
        cls,
        *,
        agency: Agency,
        order_id: str,
    ) -> None:
        order_key = str(order_id or "").strip()
        if not order_key:
            return

        snapshots = list(
            WarehouseStockSnapshot.objects.filter(
                agency=agency,
                source_context_type="receiving",
                source_context_id=order_key,
            ).values_list("id", flat=True)
        )
        operations = list(
            WarehouseOperation.objects.filter(
                agency=agency,
                context_type="receiving",
                context_id=order_key,
            ).values_list("id", flat=True)
        )
        reserves = list(
            WarehouseReserve.objects.filter(
                agency=agency,
                context_type="receiving",
                context_id=order_key,
            ).values_list("id", flat=True)
        )
        containers = list(
            WarehouseContainer.objects.filter(
                agency=agency,
                source_context_type="receiving",
                source_context_id=order_key,
            ).values_list("id", flat=True)
        )

        if snapshots:
            WarehouseStockSnapshot.objects.filter(id__in=snapshots).delete()
        if operations:
            task_ids = list(
                WarehouseOperationTask.objects.filter(operation_id__in=operations).values_list("id", flat=True)
            )
            if task_ids:
                WarehouseEvent.objects.filter(operation_task_id__in=task_ids).delete()
                WarehouseOperationTask.objects.filter(id__in=task_ids).delete()
            WarehouseEvent.objects.filter(operation_id__in=operations).delete()
            WarehouseOperation.objects.filter(id__in=operations).delete()
        if reserves:
            WarehouseEvent.objects.filter(reserve_id__in=reserves).delete()
            WarehouseReserve.objects.filter(id__in=reserves).delete()
        WarehouseEvent.objects.filter(
            agency=agency,
            stock_context_type="receiving",
            stock_context_id=order_key,
        ).delete()
        if containers:
            WarehouseContainer.objects.filter(id__in=containers).delete()

    @staticmethod
    def _reserve_open_qty(reserve: WarehouseReserve) -> int:
        return max(int(reserve.qty_reserved or 0) - int(reserve.qty_satisfied or 0), 0)

    @staticmethod
    def _reserve_matches_snapshot(reserve: WarehouseReserve, snapshot: WarehouseStockSnapshot) -> bool:
        return (
            int(reserve.agency_id or 0) == int(snapshot.agency_id or 0)
            and str(reserve.sku_code or "").strip() == str(snapshot.sku_code or "").strip()
            and str(reserve.size or "").strip() == str(snapshot.size or "").strip()
            and str(reserve.barcode or "").strip() == str(snapshot.barcode or "").strip()
            and str(reserve.goods_type or "").strip() == str(snapshot.goods_type or "").strip()
        )

    @staticmethod
    def _shipping_reserve_snapshot_needs_rebind(snapshot: WarehouseStockSnapshot | None) -> bool:
        if snapshot is None or bool(getattr(snapshot, "is_archived", False)):
            return True
        zone_code = str(getattr(snapshot, "zone_code", "") or "").strip().upper()
        if zone_code in {"OBR", "OTG", "LOAD", "VEH"}:
            return True
        state_code = str(getattr(snapshot, "warehouse_state_code", "") or "").strip()
        return state_code in {
            WarehouseStateCode.MOVING_TO_PROCESSING.value,
            WarehouseStateCode.IN_PROCESSING_ZONE.value,
            WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
            WarehouseStateCode.MOVING_TO_OTG.value,
            WarehouseStateCode.IN_OTG.value,
            WarehouseStateCode.PALLETIZING.value,
            WarehouseStateCode.READY_FOR_LOADING.value,
            WarehouseStateCode.ASSIGNED_TO_TRIP.value,
            WarehouseStateCode.LOADING_IN_PROGRESS.value,
            WarehouseStateCode.LOADED_TO_VEHICLE.value,
            WarehouseStateCode.SHIPPED.value,
            WarehouseStateCode.PARTIALLY_SHIPPED.value,
        }

    @classmethod
    def _snapshot_is_in_otg_or_later(cls, snapshot: WarehouseStockSnapshot | None) -> bool:
        if snapshot is None:
            return False
        zone_code = str(getattr(snapshot, "zone_code", "") or "").strip().upper()
        if zone_code == "OTG":
            return True
        state_code = str(getattr(snapshot, "warehouse_state_code", "") or "").strip()
        return state_code in {
            WarehouseStateCode.MOVING_TO_OTG.value,
            WarehouseStateCode.IN_OTG.value,
            WarehouseStateCode.PALLETIZING.value,
            WarehouseStateCode.READY_FOR_LOADING.value,
            WarehouseStateCode.ASSIGNED_TO_TRIP.value,
            WarehouseStateCode.LOADING_IN_PROGRESS.value,
            WarehouseStateCode.LOADED_TO_VEHICLE.value,
            WarehouseStateCode.SHIPPED.value,
            WarehouseStateCode.PARTIALLY_SHIPPED.value,
        }

    @classmethod
    def _find_arrived_otg_snapshot_for_reserve(
        cls,
        *,
        agency: Agency,
        snapshot: WarehouseStockSnapshot | None,
    ) -> WarehouseStockSnapshot | None:
        container_code = str(
            getattr(getattr(snapshot, "container", None), "container_code", "") or ""
        ).strip()
        if not container_code:
            return None
        candidates = (
            WarehouseStockSnapshot.objects.select_related("container", "location", "active_operation", "last_event")
            .filter(
                agency=agency,
                container__container_code=container_code,
                is_archived=False,
            )
            .order_by("-id")
        )
        for candidate in candidates:
            if cls._snapshot_is_in_otg_or_later(candidate):
                return candidate
        return None

    @classmethod
    def _reserve_snapshot_id_map(cls, reserves: list[WarehouseReserve]) -> dict[int, int]:
        reserve_ids = [int(reserve.id) for reserve in reserves if int(reserve.id or 0) > 0]
        if not reserve_ids:
            return {}
        snapshot_by_reserve: dict[int, int] = {}
        for row in (
            WarehouseEvent.objects.filter(reserve_id__in=reserve_ids)
            .exclude(payload__isnull=True)
            .values("reserve_id", "payload")
            .order_by("id")
        ):
            reserve_id = int(row.get("reserve_id") or 0)
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            snapshot_id = int(payload.get("snapshot_id") or 0)
            if reserve_id and snapshot_id:
                snapshot_by_reserve[reserve_id] = snapshot_id
        return snapshot_by_reserve

    @classmethod
    def _apply_shipping_reserve_rebind(
        cls,
        *,
        reserve: WarehouseReserve,
        from_snapshot: WarehouseStockSnapshot | None,
        to_snapshot: WarehouseStockSnapshot,
        qty: int,
        performed_by=None,
        source_document_id: str = "",
    ) -> None:
        qty_to_move = max(int(qty or 0), 0)
        if qty_to_move <= 0:
            return
        cls._assert_shipping_does_not_take_fbs_stock([to_snapshot])
        if from_snapshot is not None and int(from_snapshot.id or 0) == int(to_snapshot.id or 0):
            return
        rebind_operation = None
        if (
            from_snapshot is not None
            and from_snapshot.active_operation_id
            and from_snapshot.active_operation is not None
            and from_snapshot.active_operation.operation_type == WarehouseOperation.TYPE_MOVE_TO_OTG
            and str(from_snapshot.active_operation.context_type or "").strip().lower() == "shipping"
            and str(from_snapshot.active_operation.context_id or "").strip() == str(reserve.context_id or "").strip()
        ):
            rebind_operation = from_snapshot.active_operation
        if from_snapshot is not None and int(from_snapshot.shipping_reserved_qty or 0) < qty_to_move:
            raise ValueError("РќРµРґРѕСЃС‚Р°С‚РѕС‡РЅРѕ shipping reserve РЅР° РёСЃС…РѕРґРЅРѕРј РєРѕСЂРѕР±Рµ РґР»СЏ РїРµСЂРµРЅРѕСЃР°.")
        target_capacity_qty = cls._shipping_rebind_target_qty(to_snapshot)
        target_available_qty = int(to_snapshot.available_qty or 0)
        if target_capacity_qty < qty_to_move:
            raise ValueError("РќР° С†РµР»РµРІРѕРј РєРѕСЂРѕР±Рµ РЅРµРґРѕСЃС‚Р°С‚РѕС‡РЅРѕ РґРѕСЃС‚СѓРїРЅРѕРіРѕ РєРѕР»РёС‡РµСЃС‚РІР° РґР»СЏ РїРµСЂРµРЅРѕСЃР° СЂРµР·РµСЂРІР°.")
        if from_snapshot is not None:
            try:
                release_transition = WarehouseTransitionService.apply_event(
                    from_snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                    WarehouseEventType.SHIPPING_RESERVE_RELEASED,
                )
                next_released_state = release_transition.code.value
            except ValueError:
                next_released_state = from_snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value
            release_event = WarehouseEvent.objects.create(
                agency=from_snapshot.agency,
                event_type=WarehouseEventType.SHIPPING_RESERVE_RELEASED.value,
                stock_context_type=reserve.context_type,
                stock_context_id=reserve.context_id,
                container=from_snapshot.container,
                reserve=reserve,
                source_document_type=reserve.source_document_type,
                source_document_id=source_document_id or reserve.source_document_id,
                from_location=from_snapshot.location,
                to_location=from_snapshot.location,
                from_zone_code=from_snapshot.zone_code,
                to_zone_code=from_snapshot.zone_code,
                qty=qty_to_move,
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by),
                occurred_at=timezone.now(),
                payload={
                    "snapshot_id": from_snapshot.id,
                    "box_code": str(from_snapshot.container_code or ""),
                    "rebound_to_snapshot_id": to_snapshot.id,
                    "rebound_to_box_code": str(to_snapshot.container_code or ""),
                },
            )
            from_snapshot.shipping_reserved_qty -= qty_to_move
            from_snapshot.available_qty = min(int(from_snapshot.qty or 0), int(from_snapshot.available_qty or 0) + qty_to_move)
            if int(from_snapshot.shipping_reserved_qty or 0) <= 0:
                if int(from_snapshot.processing_reserved_qty or 0) > 0:
                    from_snapshot.warehouse_state_code = WarehouseStateCode.RESERVED_FOR_PROCESSING.value
                else:
                    from_snapshot.warehouse_state_code = (
                        cls._state_for_location(from_snapshot.location)
                        if from_snapshot.location is not None
                        else next_released_state
                    )
                if rebind_operation is not None:
                    from_snapshot.active_operation = None
                    from_snapshot.active_operation_type = ""
            from_snapshot.last_event = release_event
            from_snapshot_update_fields = [
                "shipping_reserved_qty",
                "available_qty",
                "warehouse_state_code",
                "last_event",
            ]
            if rebind_operation is not None and int(from_snapshot.shipping_reserved_qty or 0) <= 0:
                from_snapshot_update_fields.extend(["active_operation", "active_operation_type"])
            from_snapshot.save(update_fields=from_snapshot_update_fields + ["updated_at"])

        try:
            reserve_transition = WarehouseTransitionService.apply_event(
                to_snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.SHIPPING_RESERVED,
            )
            next_reserved_state = reserve_transition.code.value
        except ValueError:
            next_reserved_state = to_snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value
        reserve_event = WarehouseEvent.objects.create(
            agency=to_snapshot.agency,
            event_type=WarehouseEventType.SHIPPING_RESERVED.value,
            stock_context_type=reserve.context_type,
            stock_context_id=reserve.context_id,
            container=to_snapshot.container,
            reserve=reserve,
            source_document_type=reserve.source_document_type,
            source_document_id=source_document_id or reserve.source_document_id,
            from_location=to_snapshot.location,
            to_location=to_snapshot.location,
            from_zone_code=to_snapshot.zone_code,
            to_zone_code=to_snapshot.zone_code,
            qty=qty_to_move,
            performed_by=performed_by,
            performed_by_role=cls._role_of(performed_by),
            occurred_at=timezone.now(),
            payload={
                "snapshot_id": to_snapshot.id,
                "box_code": str(to_snapshot.container_code or ""),
                "rebound_from_snapshot_id": int(from_snapshot.id or 0) if from_snapshot is not None else 0,
                "rebound_from_box_code": str(from_snapshot.container_code or "") if from_snapshot is not None else "",
            },
        )
        to_snapshot.shipping_reserved_qty += qty_to_move
        to_snapshot.available_qty = max(target_available_qty - qty_to_move, 0)
        to_snapshot.warehouse_state_code = next_reserved_state
        to_snapshot.last_event = reserve_event
        to_snapshot_update_fields = [
            "shipping_reserved_qty",
            "available_qty",
            "warehouse_state_code",
            "last_event",
        ]
        if rebind_operation is not None and int(to_snapshot.active_operation_id or 0) != int(rebind_operation.id or 0):
            to_snapshot.active_operation = rebind_operation
            to_snapshot.active_operation_type = rebind_operation.operation_type
            to_snapshot_update_fields.extend(["active_operation", "active_operation_type"])
        to_snapshot.save(update_fields=to_snapshot_update_fields + ["updated_at"])
        if (
            rebind_operation is not None
            and from_snapshot is not None
            and from_snapshot.container_id
            and to_snapshot.container_id
            and int(from_snapshot.container_id) != int(to_snapshot.container_id)
        ):
            WarehouseOperationTask.objects.filter(
                operation=rebind_operation,
                container_id=from_snapshot.container_id,
                status__in=[
                    WarehouseOperationTask.STATUS_CREATED,
                    WarehouseOperationTask.STATUS_IN_PROGRESS,
                ],
            ).update(
                container=to_snapshot.container,
                updated_at=timezone.now(),
            )

    @classmethod
    def _release_reserves_from_snapshots(
        cls,
        *,
        reserves: list[WarehouseReserve],
        reserved_qty_field: str,
        release_event_type: WarehouseEventType,
        performed_by=None,
        release_bound_qty: bool = False,
    ) -> None:
        if not reserves:
            return
        snapshot_id_by_reserve = cls._reserve_snapshot_id_map(reserves)
        snapshot_ids = {snapshot_id for snapshot_id in snapshot_id_by_reserve.values() if snapshot_id}
        snapshots_by_id = {
            int(snapshot.id): snapshot
            for snapshot in WarehouseStockSnapshot.objects.select_for_update().filter(id__in=snapshot_ids)
        }
        for reserve in reserves:
            snapshot_id = snapshot_id_by_reserve.get(int(reserve.id or 0))
            bound_snapshot = snapshots_by_id.get(snapshot_id) if snapshot_id else None
            remaining_to_release = cls._reserve_open_qty(reserve)
            if release_bound_qty and bound_snapshot is not None:
                remaining_to_release = max(
                    remaining_to_release,
                    min(
                        int(getattr(bound_snapshot, reserved_qty_field, 0) or 0),
                        int(reserve.qty_reserved or 0),
                    ),
                )
            if remaining_to_release <= 0:
                continue
            candidates: list[WarehouseStockSnapshot] = []
            if bound_snapshot is not None:
                candidates.append(bound_snapshot)
            if not candidates:
                candidates = list(
                    WarehouseStockSnapshot.objects.select_for_update()
                    .filter(
                        agency=reserve.agency,
                        sku_code=reserve.sku_code,
                        size=reserve.size,
                        barcode=reserve.barcode,
                        goods_type=reserve.goods_type,
                        is_archived=False,
                    )
                    .order_by("id")
                )
            for snapshot in candidates:
                if not cls._reserve_matches_snapshot(reserve, snapshot):
                    continue
                snapshot_reserved_qty = int(getattr(snapshot, reserved_qty_field, 0) or 0)
                if snapshot_reserved_qty <= 0:
                    continue
                released_qty = min(snapshot_reserved_qty, remaining_to_release)
                try:
                    transition = WarehouseTransitionService.apply_event(
                        snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                        release_event_type,
                    )
                    next_state = transition.code.value
                except ValueError:
                    next_state = snapshot.warehouse_state_code
                release_event = WarehouseEvent.objects.create(
                    agency=snapshot.agency,
                    event_type=release_event_type.value,
                    stock_context_type=reserve.context_type,
                    stock_context_id=reserve.context_id,
                    container=snapshot.container,
                    reserve=reserve,
                    from_location=snapshot.location,
                    to_location=snapshot.location,
                    from_zone_code=snapshot.zone_code,
                    to_zone_code=snapshot.zone_code,
                    qty=released_qty,
                    performed_by=performed_by,
                    performed_by_role=cls._role_of(performed_by),
                    occurred_at=timezone.now(),
                    payload={"snapshot_id": snapshot.id},
                )
                setattr(snapshot, reserved_qty_field, snapshot_reserved_qty - released_qty)
                snapshot.available_qty = min(int(snapshot.qty or 0), int(snapshot.available_qty or 0) + released_qty)
                if int(getattr(snapshot, reserved_qty_field, 0) or 0) <= 0:
                    if int(snapshot.shipping_reserved_qty or 0) > 0:
                        snapshot.warehouse_state_code = WarehouseStateCode.RESERVED_FOR_SHIPPING.value
                    elif int(snapshot.processing_reserved_qty or 0) > 0:
                        snapshot.warehouse_state_code = WarehouseStateCode.RESERVED_FOR_PROCESSING.value
                    else:
                        snapshot.warehouse_state_code = (
                            cls._state_for_location(snapshot.location)
                            if snapshot.location is not None
                            else next_state
                        )
                snapshot.last_event = release_event
                snapshot.save(
                    update_fields=[
                        reserved_qty_field,
                        "available_qty",
                        "warehouse_state_code",
                        "last_event",
                        "updated_at",
                    ]
                )
                remaining_to_release -= released_qty
                if remaining_to_release <= 0:
                    break

    @classmethod
    @transaction.atomic
    def replace_processing_reserves(
        cls,
        *,
        agency: Agency,
        order_id: str,
        items: list[dict],
        created_by=None,
        source_document_type: str = "processing_order",
        source_document_id: str = "",
    ) -> list[WarehouseReserve]:
        order_key = str(order_id or "").strip()
        if not order_key:
            return []
        active_reserves = list(
            WarehouseReserve.objects.filter(
                agency=agency,
                reserve_type=WarehouseReserve.TYPE_PROCESSING,
                context_type="processing",
                context_id=order_key,
                status__in=[
                    WarehouseReserve.STATUS_ACTIVE,
                    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
                    WarehouseReserve.STATUS_ALLOCATED,
                    WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
                    WarehouseReserve.STATUS_SATISFIED,
                ],
            ).order_by("id")
        )
        cls._release_reserves_from_snapshots(
            reserves=active_reserves,
            reserved_qty_field="processing_reserved_qty",
            release_event_type=WarehouseEventType.PROCESSING_RESERVE_RELEASED,
            performed_by=created_by,
        )
        if active_reserves:
            WarehouseReserve.objects.filter(id__in=[reserve.id for reserve in active_reserves]).update(
                status=WarehouseReserve.STATUS_RELEASED,
                released_by=created_by if getattr(created_by, "is_authenticated", False) else None,
                updated_at=timezone.now(),
            )
        if not items:
            return []
        return cls.reserve_for_processing(
            agency=agency,
            order_id=order_key,
            items=items,
            created_by=created_by,
            source_document_type=source_document_type,
            source_document_id=source_document_id,
        )

    @classmethod
    def _drop_already_materialized_receiving_items(
        cls,
        *,
        order_id: str,
        placement_payload: dict,
        items: list[dict],
    ) -> tuple[list[dict], bool]:
        """Убрать позиции паллет, которые уже поставлены на остатки в приемке.

        Остаток по паллете создается один раз — при разрешении размещения.
        Акт размещения после этого только фиксирует документ и не должен
        приходовать тот же товар повторно. Проверка по живым строкам остатка
        здесь не годится: уехавший товар оставляет архивные строки с qty=0.
        """
        if not items:
            return items, False
        materialized, materialized_box_codes = cls._receiving_materialized_container_codes(
            order_id=order_id,
            placement_payload=placement_payload,
        )
        if not materialized:
            return items, False
        kept: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                kept.append(item)
                continue
            pallet_code = str(item.get("pallet_code") or "").strip().casefold()
            box_code = str(item.get("box_code") or "").strip().casefold()
            if pallet_code and pallet_code in materialized:
                continue
            if box_code and box_code in materialized_box_codes:
                continue
            kept.append(item)
        return kept, len(kept) != len(items)

    @classmethod
    @transaction.atomic
    def sync_receiving_placement(
        cls,
        *,
        agency: Agency,
        order_id: str,
        placement_payload: dict,
        performed_by=None,
        warehouse_code: str = "MSK",
        preserve_existing_snapshots: bool = False,
        raise_on_conflicts: bool = False,
    ) -> WarehousePlacementResult:
        order_key = str(order_id or "").strip()
        if not order_key:
            raise ValueError("order_id is required for receiving placement sync")

        items = cls._receiving_items_from_placement_payload(
            order_id=order_key,
            placement_payload=placement_payload,
        )
        items, has_materialized = cls._drop_already_materialized_receiving_items(
            order_id=order_key,
            placement_payload=placement_payload,
            items=items,
        )
        if has_materialized:
            # По приемке есть паллеты, уже поставленные на остатки при выдаче
            # разрешения. Полное пересоздание (clear_receiving_context удаляет
            # строки приемки) стерло бы этот товар со склада, поэтому такой акт
            # работает только в режиме сохранения существующих строк.
            preserve_existing_snapshots = True
        if preserve_existing_snapshots:
            snapshot_queryset = (
                WarehouseStockSnapshot.objects.select_related("container", "parent_container")
                .filter(
                    agency=agency,
                    source_context_type="receiving",
                    source_context_id=order_key,
                    is_archived=False,
                    qty__gt=0,
                )
                .order_by("id")
            )
            existing_snapshots = list(snapshot_queryset)
            if cls._expand_legacy_unmarked_receiving_snapshots(
                snapshots=existing_snapshots,
                items=items,
            ):
                existing_snapshots = list(snapshot_queryset.all())
            existing_item_keys: set[tuple] = set()
            existing_container_keys: set[tuple[str, str]] = set()
            existing_snapshots_by_match_key: dict[tuple, list[WarehouseStockSnapshot]] = {}
            existing_snapshots_by_logical_key: dict[tuple, list[WarehouseStockSnapshot]] = {}
            for snapshot in existing_snapshots:
                identity_key = cls._receiving_snapshot_identity_key(snapshot)
                container_key = cls._receiving_snapshot_container_key(snapshot)
                existing_item_keys.add(identity_key)
                existing_snapshots_by_match_key.setdefault(
                    cls._receiving_snapshot_match_key(snapshot),
                    [],
                ).append(snapshot)
                existing_snapshots_by_logical_key.setdefault(
                    cls._receiving_snapshot_logical_key(snapshot),
                    [],
                ).append(snapshot)
                if container_key[0] and container_key[1]:
                    existing_container_keys.add(container_key)
            if existing_item_keys or existing_container_keys:
                next_items: list[dict] = []
                conflicts: list[str] = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    identity_key = cls._receiving_item_identity_key(item)
                    if identity_key in existing_item_keys:
                        continue
                    matching_snapshots = existing_snapshots_by_match_key.get(
                        cls._receiving_item_match_key(item),
                        [],
                    )
                    if matching_snapshots:
                        if len(matching_snapshots) == 1 and cls._sync_receiving_snapshot_qty(
                            matching_snapshots[0],
                            item,
                        ):
                            continue
                        conflicts.append(str(item.get("box_code") or item.get("pallet_code") or "").strip())
                        continue
                    logical_snapshots = existing_snapshots_by_logical_key.get(
                        cls._receiving_item_logical_key(item),
                        [],
                    )
                    if logical_snapshots:
                        if len(logical_snapshots) == 1 and cls._enrich_receiving_snapshot_from_item(
                            logical_snapshots[0],
                            item,
                        ):
                            continue
                        conflicts.append(str(item.get("box_code") or item.get("pallet_code") or "").strip())
                        continue
                    container_key = cls._receiving_item_container_key(item)
                    if container_key[0] and container_key[1] and container_key in existing_container_keys:
                        conflicts.append(str(item.get("box_code") or item.get("pallet_code") or "").strip())
                        continue
                    next_items.append(item)
                if conflicts and raise_on_conflicts:
                    conflict_counts = Counter(code or "без контейнера" for code in conflicts)
                    conflict_labels = [
                        f"{code} (строк: {count})" if count > 1 else code
                        for code, count in conflict_counts.items()
                    ]
                    shown = ", ".join(conflict_labels[:5])
                    if len(conflict_labels) > 5:
                        shown = f"{shown} и еще {len(conflict_labels) - 5}"
                    raise ValueError(f"Состав склада отличается от акта приемки по контейнерам: {shown}.")
                items = next_items
        else:
            cls.clear_receiving_context(agency=agency, order_id=order_key)
        if not items:
            return WarehousePlacementResult(snapshot_ids=[], event_ids=[])
        result = cls.create_receiving_placement(
            agency=agency,
            order_id=order_key,
            items=items,
            performed_by=performed_by,
            warehouse_code=warehouse_code,
            source_document_type="placement_act",
            source_document_id=order_key,
            stock_context_type="receiving",
            receiving_location_code=str(
                (placement_payload or {}).get("receiving_location_code") or ""
            ).strip(),
        )
        cls.sync_box_characteristics(
            agency=agency,
            order_id=order_key,
            order_type="receiving",
            boxes=placement_payload.get("act_boxes") if isinstance(placement_payload, dict) else [],
            performed_by=performed_by,
        )
        return result

    @classmethod
    @transaction.atomic
    def create_receiving_placement(
        cls,
        *,
        agency: Agency,
        order_id: str,
        items: list[dict],
        performed_by=None,
        warehouse_code: str = "MSK",
        source_document_type: str = "receiving_order",
        source_document_id: str = "",
        stock_context_type: str = "receiving",
        respect_item_location: bool = False,
        warehouse_state_code: str = "",
        receiving_location_code: str = "",
    ) -> WarehousePlacementResult:
        if not items:
            return WarehousePlacementResult(snapshot_ids=[], event_ids=[])

        order_key = str(order_id or "").strip()
        if not order_key:
            raise ValueError("order_id is required for receiving placement")

        source_document_id = str(source_document_id or order_key).strip()
        stock_context_type = str(stock_context_type or "receiving").strip()
        forced_state_code = str(warehouse_state_code or "").strip()
        if str(receiving_location_code or "").strip():
            receiving_location = cls.concrete_movement_destination(
                warehouse_code=warehouse_code,
                zone_code="PR",
                location_code=receiving_location_code,
            )
            occupancy_error = operational_location_occupancy_message(
                receiving_location,
                exclude_context_type=stock_context_type,
                exclude_context_id=order_key,
            )
            if occupancy_error:
                raise ValueError(occupancy_error)
        else:
            receiving_location = cls.ensure_location(
                warehouse_code=warehouse_code,
                zone_code="PR",
                row_no=0,
                section_no=0,
                tier_no=0,
                cell_no=0,
            )

        receiving_event = WarehouseEvent.objects.create(
            agency=agency,
            event_type=WarehouseEventType.RECEIVING_ARRIVED.value,
            stock_context_type=stock_context_type,
            stock_context_id=order_key,
            source_document_type=source_document_type,
            source_document_id=source_document_id,
            to_location=receiving_location,
            to_zone_code=receiving_location.zone_code,
            qty=sum(max(int(item.get("qty") or 0), 0) for item in items),
            performed_by=performed_by,
            performed_by_role=cls._role_of(performed_by),
            occurred_at=timezone.now(),
            payload={"item_count": len(items)},
        )

        snapshot_ids: list[int] = []
        event_ids: list[int] = [receiving_event.id]
        barcode_values = {
            cls._normalize_reserve_lookup_text(item.get("barcode"))
            for item in items
            if isinstance(item, dict) and cls._normalize_reserve_lookup_text(item.get("barcode"))
        }
        sku_values = {
            str(item.get("sku_code") or item.get("sku") or "").strip()
            for item in items
            if isinstance(item, dict) and str(item.get("sku_code") or item.get("sku") or "").strip()
        }
        sku_ref_cache: dict[tuple[str, str], SKU] = {}
        if barcode_values or sku_values:
            sku_query = Q()
            if barcode_values:
                sku_query |= Q(code__in=barcode_values)
            if sku_values:
                sku_query |= Q(sku_code__in=sku_values)
            for sku_ref in SKU.objects.filter(agency=agency, deleted=False).filter(sku_query).order_by("id"):
                code_key = cls._normalize_reserve_lookup_text(getattr(sku_ref, "code", ""))
                sku_key = str(getattr(sku_ref, "sku_code", "") or "").strip()
                if code_key:
                    sku_ref_cache.setdefault(("code", code_key), sku_ref)
                if sku_key:
                    sku_ref_cache.setdefault(("sku", sku_key), sku_ref)
        container_codes: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            pallet_code = str(item.get("pallet_code") or "").strip()
            box_code = str(item.get("box_code") or "").strip()
            if pallet_code:
                container_codes.add(pallet_code)
            if box_code:
                container_codes.add(box_code)
        container_cache = {
            str(container.container_code or "").strip(): container
            for container in WarehouseContainer.objects.filter(
                agency=agency,
                container_code__in=list(container_codes),
            )
        }
        containers_to_update: list[WarehouseContainer] = []
        placement_records = []
        for item in items:
            qty = max(int(item.get("qty") or 0), 0)
            if qty <= 0:
                continue
            target_location = (
                cls._location_from_item(
                    item=item,
                    fallback=receiving_location,
                    warehouse_code=warehouse_code,
                )
                if respect_item_location
                else receiving_location
            )
            sku_ref = cls._resolve_sku_ref(agency=agency, item=item, sku_ref_cache=sku_ref_cache)
            container = cls._resolve_container(
                agency=agency,
                item=item,
                current_location=target_location,
                performed_by=performed_by,
                container_cache=container_cache,
                containers_to_update=containers_to_update,
            )
            transition = WarehouseTransitionService.apply_event(
                WarehouseStateCode.RECEIVED_UNPLACED,
                WarehouseEventType.PLACEMENT_COMPLETED,
            )
            placement_records.append((item, qty, target_location, sku_ref, container, transition))

        placement_events = [
            WarehouseEvent(
                agency=agency,
                event_type=WarehouseEventType.PLACEMENT_COMPLETED.value,
                stock_context_type=stock_context_type,
                stock_context_id=order_key,
                container=container,
                source_document_type=source_document_type,
                source_document_id=source_document_id,
                to_location=target_location,
                to_zone_code=target_location.zone_code,
                qty=qty,
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by),
                occurred_at=timezone.now(),
                payload={
                    "sku_code": str(item.get("sku_code") or item.get("sku") or "").strip(),
                    "name": str(item.get("name") or "").strip(),
                    "size": str(item.get("size") or "").strip(),
                    "barcode": str(item.get("barcode") or "").strip(),
                    "goods_type": str(item.get("goods_type") or "").strip(),
                    "marking_code": str(item.get("marking_code") or "").strip(),
                },
            )
            for item, qty, target_location, sku_ref, container, transition in placement_records
        ]
        if placement_events:
            WarehouseEvent.objects.bulk_create(placement_events, batch_size=500)
            event_ids.extend(event.id for event in placement_events if event.id)

        snapshots = [
            WarehouseStockSnapshot(
                agency=agency,
                stock_unit_type="item",
                source_context_type=stock_context_type,
                source_context_id=order_key,
                sku_ref=sku_ref,
                sku_code=str(item.get("sku_code") or item.get("sku") or "").strip(),
                name=str(item.get("name") or getattr(sku_ref, "name", "") or "").strip(),
                size=str(item.get("size") or "").strip(),
                barcode=str(item.get("barcode") or "").strip(),
                goods_type=str(item.get("goods_type") or "").strip(),
                marking_code=str(item.get("marking_code") or "").strip(),
                qty=qty,
                available_qty=qty,
                container=container,
                container_code=container.container_code if container else "",
                parent_container=container.parent_container if container else None,
                location=target_location,
                zone_code=target_location.zone_code,
                zone_kind=target_location.zone_kind,
                warehouse_state_code=(
                    forced_state_code
                    or (
                        transition.code.value
                        if target_location.zone_code == "PR"
                        else cls._state_for_location(target_location)
                    )
                ),
                last_event=placement_event,
            )
            for (item, qty, target_location, sku_ref, container, transition), placement_event in zip(
                placement_records,
                placement_events,
            )
        ]
        if snapshots:
            WarehouseStockSnapshot.objects.bulk_create(snapshots, batch_size=500)
            snapshot_ids.extend(snapshot.id for snapshot in snapshots if snapshot.id)

        if containers_to_update:
            container_updates = {
                container.pk: container
                for container in containers_to_update
                if container.pk
            }
            if container_updates:
                WarehouseContainer.objects.bulk_update(
                    list(container_updates.values()),
                    [
                        "current_location",
                        "parent_container",
                        "source_context_type",
                        "source_context_id",
                        "updated_at",
                    ],
                    batch_size=500,
                )

        return WarehousePlacementResult(snapshot_ids=snapshot_ids, event_ids=event_ids)

    @classmethod
    @transaction.atomic
    def archive_phantom_receiving_boxes(
        cls,
        *,
        agency: Agency,
        order_id: str,
        box_codes: list[str],
        reason: str,
        performed_by=None,
        apply: bool = True,
    ) -> WarehouseCorrectionResult:
        """Archive boxes never physically received while retaining their history."""
        order_key = str(order_id or "").strip()
        normalized_reason = str(reason or "").strip()
        normalized_codes: list[str] = []
        seen_codes: set[str] = set()
        for raw_code in box_codes or []:
            code = str(raw_code or "").strip()
            folded = code.casefold()
            if code and folded not in seen_codes:
                normalized_codes.append(code)
                seen_codes.add(folded)
        if not agency or not order_key:
            raise ValueError("Agency and receiving order are required for stock correction.")
        if not normalized_codes:
            raise ValueError("At least one box code is required for stock correction.")
        if not normalized_reason:
            raise ValueError("Correction reason is required.")

        containers = list(
            WarehouseContainer.objects.select_for_update()
            .filter(agency=agency, container_code__in=normalized_codes)
            .order_by("id")
        )
        containers_by_code = {
            str(container.container_code or "").strip().casefold(): container
            for container in containers
        }
        missing_codes = [
            code for code in normalized_codes if code.casefold() not in containers_by_code
        ]
        if missing_codes:
            raise ValueError(f"Warehouse boxes not found: {', '.join(missing_codes)}")

        wrong_containers = [
            container.container_code
            for container in containers
            if container.container_type != WarehouseContainer.TYPE_BOX
            or str(container.source_context_type or "").strip() != "receiving"
            or str(container.source_context_id or "").strip() != order_key
        ]
        if wrong_containers:
            raise ValueError(
                "Boxes do not belong to the specified receiving order: "
                + ", ".join(wrong_containers)
            )

        container_ids = [container.id for container in containers]
        live_snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update()
            .filter(container_id__in=container_ids, is_archived=False, qty__gt=0)
            .order_by("container_id", "id")
        )
        snapshots_by_container: dict[int, list[WarehouseStockSnapshot]] = {}
        for snapshot in live_snapshots:
            snapshots_by_container.setdefault(int(snapshot.container_id), []).append(snapshot)

        invalid_snapshot_codes: list[str] = []
        for container in containers:
            matched = snapshots_by_container.get(int(container.id), [])
            if len(matched) != 1:
                invalid_snapshot_codes.append(container.container_code)
                continue
            snapshot = matched[0]
            if (
                snapshot.agency_id != agency.id
                or str(snapshot.source_context_type or "").strip() != "receiving"
                or str(snapshot.source_context_id or "").strip() != order_key
            ):
                invalid_snapshot_codes.append(container.container_code)
        if invalid_snapshot_codes:
            raise ValueError(
                "Each corrected box must have exactly one live snapshot from this receiving: "
                + ", ".join(invalid_snapshot_codes)
            )

        blocking_codes: list[str] = []
        for snapshot in live_snapshots:
            reserved_qty = (
                int(snapshot.processing_reserved_qty or 0)
                + int(snapshot.shipping_reserved_qty or 0)
                + int(snapshot.other_reserved_qty or 0)
            )
            if (
                snapshot.active_operation_id
                or reserved_qty
                or snapshot.is_in_vehicle
                or str(snapshot.current_trip_id or "").strip()
            ):
                blocking_codes.append(snapshot.container_code or snapshot.container.container_code)
        active_task_container_ids = set(
            WarehouseOperationTask.objects.select_for_update()
            .filter(
                container_id__in=container_ids,
                status__in={
                    WarehouseOperationTask.STATUS_CREATED,
                    WarehouseOperationTask.STATUS_IN_PROGRESS,
                },
            )
            .values_list("container_id", flat=True)
        )
        blocking_codes.extend(
            container.container_code
            for container in containers
            if container.id in active_task_container_ids
        )
        if blocking_codes:
            raise ValueError(
                "Stock correction is blocked by an active reserve, trip, or operation: "
                + ", ".join(sorted(set(blocking_codes)))
            )

        if not apply:
            return WarehouseCorrectionResult(
                snapshot_ids=[snapshot.id for snapshot in live_snapshots],
                event_ids=[],
                container_ids=container_ids,
                box_codes=normalized_codes,
                removed_qty=sum(int(snapshot.qty or 0) for snapshot in live_snapshots),
            )

        now = timezone.now()
        event_ids: list[int] = []
        snapshot_ids: list[int] = []
        removed_qty = 0
        actor = performed_by if getattr(performed_by, "is_authenticated", False) else None
        for snapshot in live_snapshots:
            box_code = str(snapshot.container_code or snapshot.container.container_code or "").strip()
            before = {
                "snapshot_id": snapshot.id,
                "container_id": snapshot.container_id,
                "qty": int(snapshot.qty or 0),
                "available_qty": int(snapshot.available_qty or 0),
                "processing_reserved_qty": int(snapshot.processing_reserved_qty or 0),
                "shipping_reserved_qty": int(snapshot.shipping_reserved_qty or 0),
                "other_reserved_qty": int(snapshot.other_reserved_qty or 0),
                "warehouse_state_code": snapshot.warehouse_state_code,
                "location_id": snapshot.location_id,
                "location_code": str(getattr(snapshot.location, "location_code", "") or ""),
                "parent_container_id": snapshot.parent_container_id,
                "last_event_id": snapshot.last_event_id,
            }
            event = WarehouseEvent.objects.create(
                agency=agency,
                event_type=WarehouseEventType.STOCK_CORRECTED.value,
                stock_context_type="receiving",
                stock_context_id=order_key,
                container=snapshot.container,
                source_document_type="receiving_correction",
                source_document_id=order_key,
                from_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                qty=int(snapshot.qty or 0),
                performed_by=actor,
                performed_by_role=cls._role_of(actor),
                occurred_at=now,
                payload={
                    "correction_kind": "phantom_receiving_box",
                    "box_code": box_code,
                    "reason": normalized_reason,
                    "before": before,
                },
            )
            removed_qty += int(snapshot.qty or 0)
            snapshot.qty = 0
            snapshot.available_qty = 0
            snapshot.processing_reserved_qty = 0
            snapshot.shipping_reserved_qty = 0
            snapshot.other_reserved_qty = 0
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
            snapshot.current_trip_id = ""
            snapshot.is_in_vehicle = False
            snapshot.is_archived = True
            snapshot.last_event = event
            snapshot.snapshot_version = int(snapshot.snapshot_version or 1) + 1
            snapshot.save(
                update_fields=[
                    "qty",
                    "available_qty",
                    "processing_reserved_qty",
                    "shipping_reserved_qty",
                    "other_reserved_qty",
                    "active_operation",
                    "active_operation_type",
                    "current_trip_id",
                    "is_in_vehicle",
                    "is_archived",
                    "last_event",
                    "snapshot_version",
                    "updated_at",
                ]
            )
            snapshot_ids.append(snapshot.id)
            event_ids.append(event.id)

        WarehouseContainer.objects.filter(id__in=container_ids).update(
            status=WarehouseContainer.STATUS_ARCHIVED,
            updated_at=now,
        )
        return WarehouseCorrectionResult(
            snapshot_ids=snapshot_ids,
            event_ids=event_ids,
            container_ids=container_ids,
            box_codes=normalized_codes,
            removed_qty=removed_qty,
        )

    @classmethod
    @transaction.atomic
    def request_putaway_for_receiving(
        cls,
        *,
        agency: Agency,
        order_id: str,
        container_codes: list[str] | None = None,
        destination_zone_code: str = "OS",
        destination_row_no: int = 0,
        destination_section_no: int = 0,
        destination_tier_no: int = 0,
        destination_cell_no: int = 0,
        requested_by=None,
        requested_by_role: str = "storekeeper",
        warehouse_code: str = "MSK",
        source_document_type: str = "",
        source_document_id: str = "",
        exclude_draft_token: str = "",
    ) -> WarehouseOperation:
        order_key = str(order_id or "").strip()
        snapshot_query = WarehouseStockSnapshot.objects.select_related("location", "container", "parent_container").filter(
            agency=agency,
            source_context_type="receiving",
            source_context_id=order_key,
            warehouse_state_code=WarehouseStateCode.PLACED_IN_RECEIVING.value,
            is_archived=False,
        )
        normalized_container_codes = [
            str(value or "").strip()
            for value in (container_codes or [])
            if str(value or "").strip()
        ]
        if normalized_container_codes:
            snapshot_query = snapshot_query.filter(
                models.Q(container_code__in=normalized_container_codes)
                | models.Q(container__container_code__in=normalized_container_codes)
                | models.Q(parent_container__container_code__in=normalized_container_codes)
            )
        snapshots = list(snapshot_query.order_by("id"))
        if not snapshots:
            raise ValueError("No receiving snapshots ready for putaway")

        source_location = snapshots[0].location or cls.ensure_location(warehouse_code=warehouse_code, zone_code="PR")
        destination = cls.ensure_location(
            warehouse_code=warehouse_code,
            zone_code=destination_zone_code,
            row_no=destination_row_no,
            section_no=destination_section_no,
            tier_no=destination_tier_no,
            cell_no=destination_cell_no,
        )
        cls.ensure_putaway_destination_available(
            destination=destination,
            exclude_container_code=normalized_container_codes[0] if len(normalized_container_codes) == 1 else "",
            exclude_draft_token=exclude_draft_token,
        )
        operation = WarehouseOperation.objects.create(
            agency=agency,
            operation_type=WarehouseOperation.TYPE_PUTAWAY,
            context_type="receiving",
            context_id=order_key,
            source_location=source_location,
            destination_location=destination,
            source_document_type=str(source_document_type or "").strip(),
            source_document_id=str(source_document_id or "").strip(),
            source_zone_code=source_location.zone_code,
            destination_zone_code=destination.zone_code,
            status=WarehouseOperation.STATUS_PLANNED,
            requested_by=requested_by,
            requested_by_role=requested_by_role,
            assigned_executor_role="reachtruck",
            planned_qty=sum(int(snapshot.qty or 0) for snapshot in snapshots),
        )
        WarehouseEvent.objects.create(
            agency=agency,
            event_type=WarehouseEventType.PUTAWAY_REQUESTED.value,
            stock_context_type="receiving",
            stock_context_id=order_key,
            operation=operation,
            from_location=source_location,
            to_location=destination,
            from_zone_code=source_location.zone_code,
            to_zone_code=destination.zone_code,
            qty=operation.planned_qty,
            performed_by=requested_by,
            performed_by_role=requested_by_role,
            occurred_at=timezone.now(),
        )
        grouped_tasks: dict[tuple[str, int], dict] = {}
        for snapshot in snapshots:
            move_container = snapshot.parent_container or snapshot.container
            if move_container:
                task_key = ("container", int(move_container.id))
            else:
                task_key = ("snapshot", int(snapshot.id))
            task_bucket = grouped_tasks.setdefault(
                task_key,
                {
                    "container": move_container,
                    "from_location": snapshot.location,
                    "from_zone_code": snapshot.zone_code,
                    "qty_planned": 0,
                    "snapshot_ids": [],
                    "container_code": (
                        move_container.container_code
                        if move_container
                        else snapshot.container_code
                    ),
                },
            )
            task_bucket["qty_planned"] += int(snapshot.qty or 0)
            task_bucket["snapshot_ids"].append(int(snapshot.id))
        for task_bucket in grouped_tasks.values():
            task_container = task_bucket["container"]
            WarehouseOperationTask.objects.create(
                operation=operation,
                task_type=(
                    WarehouseOperationTask.TYPE_PALLET_MOVE
                    if task_container
                    and task_container.container_type
                    in {WarehouseContainer.TYPE_PALLET, WarehouseContainer.TYPE_MIXED_PALLET}
                    else WarehouseOperationTask.TYPE_BOX_MOVE
                ),
                container=task_container,
                from_location=task_bucket["from_location"],
                to_location=destination,
                from_zone_code=task_bucket["from_zone_code"],
                to_zone_code=destination.zone_code,
                qty_planned=int(task_bucket["qty_planned"] or 0),
                status=WarehouseOperationTask.STATUS_CREATED,
                executor_role="reachtruck",
                payload={
                    "snapshot_ids": task_bucket["snapshot_ids"],
                    "container_code": task_bucket["container_code"],
                },
            )
        for snapshot in snapshots:
            snapshot.active_operation = operation
            snapshot.active_operation_type = operation.operation_type
            snapshot.save(update_fields=["active_operation", "active_operation_type", "updated_at"])
        return operation

    @classmethod
    @transaction.atomic
    def complete_putaway_operation(
        cls,
        *,
        operation: WarehouseOperation,
        performed_by=None,
    ) -> WarehouseOperation:
        operation = WarehouseOperation.objects.select_for_update().get(pk=operation.pk)
        if operation.operation_type != WarehouseOperation.TYPE_PUTAWAY:
            raise ValueError("Only putaway operations can be completed by this write-path")
        if operation.status == WarehouseOperation.STATUS_DONE:
            return operation
        if operation.status == WarehouseOperation.STATUS_CANCELED:
            raise ValueError("Canceled putaway operation cannot be completed")
        destination = operation.destination_location
        if destination is None:
            raise ValueError("Putaway operation must have destination_location")
        # Use the same location-before-stock lock order as free relocation.
        destination = WarehouseLocation.objects.select_for_update().get(pk=destination.pk)

        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",)).select_related("container", "parent_container")
            .filter(active_operation=operation, is_archived=False)
            .order_by("id")
        )
        # A cell may have changed since planning. Recheck under the location
        # lock immediately before the physical placement is recorded.
        moving_codes = {
            (snapshot.parent_container or snapshot.container).container_code
            for snapshot in snapshots
            if snapshot.parent_container or snapshot.container
        }
        if any(snapshot.agency_id != operation.agency_id for snapshot in snapshots):
            raise ValueError("Размещение товаров разных клиентов запрещено.")
        cls.ensure_putaway_destination_available(
            destination=destination,
            exclude_operation_id=operation.id,
            exclude_container_code=next(iter(moving_codes)) if len(moving_codes) == 1 else "",
        )
        total_done = 0
        touched_container_ids: set[int] = set()
        for snapshot in snapshots:
            move_container = snapshot.parent_container or snapshot.container
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.PUTAWAY_COMPLETED,
                operation_type=operation.operation_type,
                zone_to=destination.zone_code,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.PUTAWAY_COMPLETED.value,
                stock_context_type=snapshot.source_context_type,
                stock_context_id=snapshot.source_context_id,
                container=move_container,
                operation=operation,
                from_location=snapshot.location,
                to_location=destination,
                from_zone_code=snapshot.zone_code,
                to_zone_code=destination.zone_code,
                qty=int(snapshot.qty or 0),
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by) or "reachtruck",
                occurred_at=timezone.now(),
            )
            if snapshot.container_id:
                touched_container_ids.add(int(snapshot.container_id))
            if snapshot.parent_container_id:
                touched_container_ids.add(int(snapshot.parent_container_id))
            snapshot.location = destination
            snapshot.zone_code = destination.zone_code
            snapshot.zone_kind = destination.zone_kind
            snapshot.warehouse_state_code = transition.code.value
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "location",
                    "zone_code",
                    "zone_kind",
                    "warehouse_state_code",
                    "active_operation",
                    "active_operation_type",
                    "last_event",
                    "updated_at",
                ]
            )
            total_done += int(snapshot.qty or 0)

        if touched_container_ids:
            WarehouseContainer.objects.filter(id__in=touched_container_ids).update(
                current_location=destination,
                updated_at=timezone.now(),
            )

        operation.status = WarehouseOperation.STATUS_DONE
        operation.done_qty = total_done
        operation.completed_at = timezone.now()
        operation.save(update_fields=["status", "done_qty", "completed_at", "updated_at"])

        operation.tasks.update(
            status=WarehouseOperationTask.STATUS_DONE,
            qty_done=models.F("qty_planned"),
            completed_at=timezone.now(),
            updated_at=timezone.now(),
        )
        return operation

    @classmethod
    @transaction.atomic
    def start_free_pallet_relocation(
        cls,
        *,
        agency: Agency,
        pallet_code: str,
        performed_by=None,
        expected_location_id: int | None = None,
        warehouse_code: str = "MSK",
    ) -> WarehouseOperation:
        normalized_pallet = str(pallet_code or "").strip()
        if not agency or not normalized_pallet:
            raise ValueError("Pallet code is required for free relocation")
        pallet_container = (
            WarehouseContainer.objects.filter(
                agency=agency,
                container_code__iexact=normalized_pallet,
                status=WarehouseContainer.STATUS_ACTIVE,
                container_type__in=[WarehouseContainer.TYPE_PALLET, WarehouseContainer.TYPE_MIXED_PALLET],
            )
            .order_by("id")
            .first()
        )
        if pallet_container is None:
            raise ValueError("Свободное перемещение доступно только для паллеты.")
        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("container", "parent_container", "location", "active_operation")
            .filter(agency=agency, is_archived=False, qty__gt=0)
            .filter(
                Q(container_code__iexact=normalized_pallet)
                | Q(container__container_code__iexact=normalized_pallet)
                | Q(parent_container__container_code__iexact=normalized_pallet)
            )
            .order_by("id")
        )
        if not snapshots:
            raise ValueError("No warehouse snapshots found for free relocation")
        source_location = snapshots[0].location
        if source_location is None:
            source_location = cls.ensure_location(warehouse_code=warehouse_code, zone_code="OS")
        if expected_location_id and int(source_location.id or 0) != int(expected_location_id or 0):
            raise ValueError("Pallet location changed before relocation start")

        location_ids = {int(snapshot.location_id or 0) for snapshot in snapshots}
        if len(location_ids) > 1:
            raise ValueError("Pallet snapshots are split across multiple locations")
        blocking_operation_ids = []
        for snapshot in snapshots:
            if not snapshot.active_operation_id:
                continue
            operation = snapshot.active_operation
            if operation and str(operation.status or "").strip() in _FINAL_OPERATION_STATUSES:
                continue
            blocking_operation_ids.append(int(snapshot.active_operation_id))
        if blocking_operation_ids:
            raise ValueError("У паллеты уже есть активная складская операция.")
        if any(
            int(snapshot.processing_reserved_qty or 0) > 0
            or int(snapshot.shipping_reserved_qty or 0) > 0
            or int(snapshot.other_reserved_qty or 0) > 0
            for snapshot in snapshots
        ):
            raise ValueError("Pallet has active reservations")
        if any(str(snapshot.warehouse_state_code or "").strip() not in _FREE_RELOCATION_SOURCE_STATES for snapshot in snapshots):
            raise ValueError("Pallet source state is not available for free relocation")

        now = timezone.now()
        total_qty = sum(int(snapshot.qty or 0) for snapshot in snapshots)
        operation = WarehouseOperation.objects.create(
            agency=agency,
            operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
            context_type="reachtruck_free",
            context_id=normalized_pallet,
            source_location=source_location,
            source_zone_code=source_location.zone_code,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
            requested_by=performed_by,
            requested_by_role=cls._role_of(performed_by) or "reachtruck",
            assigned_executor_role="reachtruck",
            planned_qty=total_qty,
            started_at=now,
            comment="Free pallet relocation",
        )
        move_container = None
        for snapshot in snapshots:
            candidate = snapshot.parent_container or snapshot.container
            if candidate and str(candidate.container_code or "").strip().lower() == normalized_pallet.lower():
                move_container = candidate
                break
        if move_container is None:
            move_container = snapshots[0].parent_container or snapshots[0].container
        WarehouseOperationTask.objects.create(
            operation=operation,
            task_type=(
                WarehouseOperationTask.TYPE_PALLET_MOVE
                if move_container
                and move_container.container_type
                in {WarehouseContainer.TYPE_PALLET, WarehouseContainer.TYPE_MIXED_PALLET}
                else WarehouseOperationTask.TYPE_BOX_MOVE
            ),
            container=move_container,
            from_location=source_location,
            from_zone_code=source_location.zone_code,
            qty_planned=total_qty,
            status=WarehouseOperationTask.STATUS_IN_PROGRESS,
            executor_role="reachtruck",
            payload={
                "snapshot_ids": [int(snapshot.id) for snapshot in snapshots],
                "container_code": normalized_pallet,
                "free_move": True,
            },
            started_at=now,
        )
        for snapshot in snapshots:
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.MOVEMENT_STARTED,
                operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.MOVEMENT_STARTED.value,
                stock_context_type=snapshot.source_context_type,
                stock_context_id=snapshot.source_context_id,
                container=snapshot.parent_container or snapshot.container,
                operation=operation,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code=snapshot.zone_code,
                qty=int(snapshot.qty or 0),
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by) or "reachtruck",
                occurred_at=now,
                payload={"pallet_code": normalized_pallet, "snapshot_id": snapshot.id, "free_move": True},
            )
            snapshot.active_operation = operation
            snapshot.active_operation_type = operation.operation_type
            snapshot.warehouse_state_code = transition.code.value
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "active_operation",
                    "active_operation_type",
                    "warehouse_state_code",
                    "last_event",
                    "updated_at",
                ]
            )
        return operation

    @classmethod
    @transaction.atomic
    def complete_free_pallet_relocation(
        cls,
        *,
        operation_id: int,
        destination_zone_code: str,
        destination_location_code: str = "",
        destination_row_no: int = 0,
        destination_section_no: int = 0,
        destination_tier_no: int = 0,
        destination_cell_no: int = 0,
        performed_by=None,
        warehouse_code: str = "MSK",
    ) -> WarehouseOperation:
        operation = WarehouseOperation.objects.select_for_update().get(id=int(operation_id))
        if operation.operation_type != WarehouseOperation.TYPE_INTERNAL_RELOCATION:
            raise ValueError("Only internal relocation can be completed by this write-path")
        if operation.status != WarehouseOperation.STATUS_IN_PROGRESS:
            raise ValueError("Free relocation is not in progress")
        normalized_pallet = str(operation.context_id or "").strip()
        destination = cls.concrete_movement_destination(
            warehouse_code=warehouse_code,
            zone_code=str(destination_zone_code or "").strip().upper(),
            location_code=destination_location_code,
            row_no=destination_row_no,
            section_no=destination_section_no,
            tier_no=destination_tier_no,
            cell_no=destination_cell_no,
        )
        if destination.zone_code not in {"OS", "MR"}:
            raise ValueError("Free relocation destination must be OS or MR")
        is_shared_named_os = bool(
            str(destination.zone_code or "").strip().upper() == "OS"
            and destination.is_storage
            and not destination.is_topology_visible
        )
        if is_shared_named_os:
            try:
                validate_operational_location(
                    destination,
                    expected_zone="OS",
                    required_slots=1,
                )
            except ValidationError as exc:
                raise ValueError("; ".join(exc.messages)) from exc
        else:
            cls.ensure_putaway_destination_available(
                destination=destination,
                exclude_operation_id=operation.id,
                exclude_container_code=normalized_pallet,
            )
        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("container", "parent_container", "location")
            .filter(active_operation=operation, is_archived=False, qty__gt=0)
            .order_by("id")
        )
        if not snapshots:
            raise ValueError("No active snapshots found for free relocation")
        if all(int(snapshot.location_id or 0) == int(destination.id or 0) for snapshot in snapshots):
            raise ValueError("Destination is the current pallet location")

        now = timezone.now()
        total_done = 0
        touched_container_ids: set[int] = set()
        for snapshot in snapshots:
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.MOVEMENT_COMPLETED,
                operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
                zone_to=destination.zone_code,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.MOVEMENT_COMPLETED.value,
                stock_context_type=snapshot.source_context_type,
                stock_context_id=snapshot.source_context_id,
                container=snapshot.parent_container or snapshot.container,
                operation=operation,
                from_location=snapshot.location,
                to_location=destination,
                from_zone_code=snapshot.zone_code,
                to_zone_code=destination.zone_code,
                qty=int(snapshot.qty or 0),
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by) or "reachtruck",
                occurred_at=now,
                payload={"pallet_code": normalized_pallet, "snapshot_id": snapshot.id, "free_move": True},
            )
            if snapshot.container_id:
                touched_container_ids.add(int(snapshot.container_id))
            if snapshot.parent_container_id:
                touched_container_ids.add(int(snapshot.parent_container_id))
            snapshot.location = destination
            snapshot.zone_code = destination.zone_code
            snapshot.zone_kind = destination.zone_kind
            snapshot.warehouse_state_code = transition.code.value
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "location",
                    "zone_code",
                    "zone_kind",
                    "warehouse_state_code",
                    "active_operation",
                    "active_operation_type",
                    "last_event",
                    "updated_at",
                ]
            )
            total_done += int(snapshot.qty or 0)

        if touched_container_ids:
            WarehouseContainer.objects.filter(id__in=touched_container_ids).update(
                current_location=destination,
                updated_at=now,
            )

        operation.destination_location = destination
        operation.destination_zone_code = destination.zone_code
        operation.status = WarehouseOperation.STATUS_DONE
        operation.done_qty = total_done
        operation.completed_at = now
        operation.save(
            update_fields=[
                "destination_location",
                "destination_zone_code",
                "status",
                "done_qty",
                "completed_at",
                "updated_at",
            ]
        )
        operation.tasks.update(
            to_location=destination,
            to_zone_code=destination.zone_code,
            status=WarehouseOperationTask.STATUS_DONE,
            qty_done=models.F("qty_planned"),
            completed_at=now,
            updated_at=now,
        )
        return operation

    @classmethod
    @transaction.atomic
    def complete_inventory_box_relocation(
        cls,
        *,
        source_pallet_code: str,
        destination_pallet_code: str,
        box_codes: list[str],
        performed_by=None,
        context_id: str = "",
    ) -> WarehouseOperation:
        source_code = str(source_pallet_code or "").strip()
        destination_code = str(destination_pallet_code or "").strip()
        normalized_boxes: list[str] = []
        seen_boxes: set[str] = set()
        for raw_code in box_codes or []:
            code = str(raw_code or "").strip()
            key = code.lower()
            if code and key not in seen_boxes:
                seen_boxes.add(key)
                normalized_boxes.append(code)
        if not source_code or not destination_code or not normalized_boxes:
            raise ValueError("Source pallet, destination pallet and boxes are required")
        if source_code.lower() == destination_code.lower():
            raise ValueError("Source and destination pallets must be different")

        source_pallet = (
            WarehouseContainer.objects.select_for_update(of=("self",))
            .filter(
                container_code__iexact=source_code,
                container_type__in=[WarehouseContainer.TYPE_PALLET, WarehouseContainer.TYPE_MIXED_PALLET],
                status=WarehouseContainer.STATUS_ACTIVE,
            )
            .order_by("id")
            .first()
        )
        destination_pallet = (
            WarehouseContainer.objects.select_for_update(of=("self",))
            .filter(
                container_code__iexact=destination_code,
                container_type__in=[WarehouseContainer.TYPE_PALLET, WarehouseContainer.TYPE_MIXED_PALLET],
                status=WarehouseContainer.STATUS_ACTIVE,
            )
            .select_related("current_location")
            .order_by("id")
            .first()
        )
        if source_pallet is None:
            raise ValueError("Source pallet was not found")
        if destination_pallet is None:
            raise ValueError("Destination pallet was not found")

        box_query = Q()
        for code in normalized_boxes:
            box_query |= Q(container__container_code__iexact=code) | Q(container_code__iexact=code)
        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("agency", "container", "parent_container", "location", "active_operation")
            .filter(is_archived=False, qty__gt=0)
            .filter(box_query)
            .filter(
                Q(parent_container__container_code__iexact=source_code)
                | Q(container__parent_container__container_code__iexact=source_code)
            )
            .order_by("container__container_code", "container_code", "id")
        )
        if not snapshots:
            raise ValueError("No selected box snapshots found")

        def snapshot_box_code(snapshot: WarehouseStockSnapshot) -> str:
            container = snapshot.container
            if container is not None and container.container_type == WarehouseContainer.TYPE_BOX:
                return str(container.container_code or "").strip()
            return str(snapshot.container_code or "").strip()

        found_codes = {snapshot_box_code(snapshot).lower() for snapshot in snapshots if snapshot_box_code(snapshot)}
        missing_codes = [code for code in normalized_boxes if code.lower() not in found_codes]
        if missing_codes:
            raise ValueError(f"Selected boxes are not on source pallet: {', '.join(missing_codes)}")
        blocking_operation_ids: set[int] = set()
        for snapshot in snapshots:
            if not snapshot.active_operation_id:
                continue
            active_operation = snapshot.active_operation
            if active_operation and str(active_operation.status or "").strip() in _FINAL_OPERATION_STATUSES:
                continue
            blocking_operation_ids.add(int(snapshot.active_operation_id))
        if blocking_operation_ids:
            raise ValueError("Selected boxes have active warehouse operations")

        destination_snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("location")
            .filter(is_archived=False, qty__gt=0)
            .filter(
                Q(parent_container__container_code__iexact=destination_code)
                | Q(container__parent_container__container_code__iexact=destination_code)
                | Q(container_code__iexact=destination_code)
                | Q(container__container_code__iexact=destination_code)
            )
            .order_by("id")[:1]
        )
        destination_location = (
            destination_snapshots[0].location
            if destination_snapshots
            else destination_pallet.current_location
        )
        if destination_location is None:
            raise ValueError("Destination pallet has no warehouse location")

        now = timezone.now()
        source_location = snapshots[0].location
        operation_agency = snapshots[0].agency
        operation = WarehouseOperation.objects.create(
            agency=operation_agency,
            operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
            context_type="reachtruck_box_move",
            context_id=str(context_id or ""),
            source_location=source_location,
            destination_location=destination_location,
            source_zone_code=str(getattr(source_location, "zone_code", "") or ""),
            destination_zone_code=destination_location.zone_code,
            status=WarehouseOperation.STATUS_DONE,
            requested_by=performed_by,
            requested_by_role=cls._role_of(performed_by) or "reachtruck",
            assigned_executor_role="reachtruck",
            planned_qty=sum(int(snapshot.qty or 0) for snapshot in snapshots),
            done_qty=sum(int(snapshot.qty or 0) for snapshot in snapshots),
            started_at=now,
            completed_at=now,
            comment="Inventory box relocation",
        )

        snapshots_by_box: dict[str, list[WarehouseStockSnapshot]] = {}
        for snapshot in snapshots:
            snapshots_by_box.setdefault(snapshot_box_code(snapshot), []).append(snapshot)

        tasks_by_box: dict[str, WarehouseOperationTask] = {}
        for box_code, box_snapshots in snapshots_by_box.items():
            first_snapshot = box_snapshots[0]
            tasks_by_box[box_code.lower()] = WarehouseOperationTask.objects.create(
                operation=operation,
                task_type=WarehouseOperationTask.TYPE_BOX_MOVE,
                container=first_snapshot.container,
                from_location=first_snapshot.location,
                to_location=destination_location,
                from_zone_code=first_snapshot.zone_code,
                to_zone_code=destination_location.zone_code,
                qty_planned=sum(int(snapshot.qty or 0) for snapshot in box_snapshots),
                qty_done=sum(int(snapshot.qty or 0) for snapshot in box_snapshots),
                status=WarehouseOperationTask.STATUS_DONE,
                assigned_to=performed_by if getattr(performed_by, "is_authenticated", False) else None,
                assigned_to_name=(
                    getattr(performed_by, "get_full_name", lambda: "")()
                    or getattr(performed_by, "username", "")
                    or ""
                ),
                executor_role="reachtruck",
                payload={
                    "box_code": box_code,
                    "source_pallet_code": source_code,
                    "destination_pallet_code": destination_code,
                    "snapshot_ids": [int(snapshot.id) for snapshot in box_snapshots],
                    "inventory_box_move": True,
                },
                started_at=now,
                completed_at=now,
            )

        touched_box_container_ids: set[int] = set()
        for snapshot in snapshots:
            box_code = snapshot_box_code(snapshot)
            operation_task = tasks_by_box.get(box_code.lower())
            try:
                transition = WarehouseTransitionService.apply_event(
                    snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                    WarehouseEventType.MOVEMENT_COMPLETED,
                    operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
                    zone_to=destination_location.zone_code,
                )
                next_state_code = transition.code.value
            except WarehouseTransitionError:
                next_state_code = snapshot.warehouse_state_code
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.MOVEMENT_COMPLETED.value,
                stock_context_type=snapshot.source_context_type,
                stock_context_id=snapshot.source_context_id,
                container=snapshot.container,
                operation=operation,
                operation_task=operation_task,
                from_location=snapshot.location,
                to_location=destination_location,
                from_zone_code=snapshot.zone_code,
                to_zone_code=destination_location.zone_code,
                qty=int(snapshot.qty or 0),
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by) or "reachtruck",
                occurred_at=now,
                payload={
                    "box_code": box_code,
                    "source_pallet_code": source_code,
                    "destination_pallet_code": destination_code,
                    "inventory_box_move": True,
                },
            )
            if snapshot.container_id:
                touched_box_container_ids.add(int(snapshot.container_id))
            snapshot.parent_container = destination_pallet
            snapshot.location = destination_location
            snapshot.zone_code = destination_location.zone_code
            snapshot.zone_kind = destination_location.zone_kind
            snapshot.warehouse_state_code = next_state_code
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "parent_container",
                    "location",
                    "zone_code",
                    "zone_kind",
                    "warehouse_state_code",
                    "active_operation",
                    "active_operation_type",
                    "last_event",
                    "updated_at",
                ]
            )

        if touched_box_container_ids:
            WarehouseContainer.objects.filter(id__in=touched_box_container_ids).update(
                parent_container=destination_pallet,
                current_location=destination_location,
                updated_at=now,
            )
        if destination_pallet.current_location_id != destination_location.id:
            destination_pallet.current_location = destination_location
            destination_pallet.save(update_fields=["current_location", "updated_at"])

        source_has_children = WarehouseContainer.objects.filter(
            parent_container=source_pallet,
            status=WarehouseContainer.STATUS_ACTIVE,
        ).exists()
        source_has_snapshots = WarehouseStockSnapshot.objects.filter(
            parent_container=source_pallet,
            is_archived=False,
            qty__gt=0,
        ).exists()
        if not source_has_children and not source_has_snapshots:
            source_pallet.current_location = None
            source_pallet.parent_container = None
            source_pallet.status = WarehouseContainer.STATUS_ARCHIVED
            source_pallet.save(update_fields=["current_location", "parent_container", "status", "updated_at"])

        return operation

    @classmethod
    @transaction.atomic
    def cancel_free_pallet_relocation(
        cls,
        *,
        operation_id: int,
        performed_by=None,
    ) -> WarehouseOperation | None:
        operation = WarehouseOperation.objects.select_for_update().filter(id=int(operation_id)).first()
        if operation is None or operation.operation_type != WarehouseOperation.TYPE_INTERNAL_RELOCATION:
            return operation
        if operation.status in {WarehouseOperation.STATUS_DONE, WarehouseOperation.STATUS_CANCELED}:
            return operation
        now = timezone.now()
        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("container", "parent_container", "location")
            .filter(active_operation=operation, is_archived=False)
            .order_by("id")
        )
        normalized_pallet = str(operation.context_id or "").strip()
        for snapshot in snapshots:
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.MOVEMENT_CANCELED.value,
                stock_context_type=snapshot.source_context_type,
                stock_context_id=snapshot.source_context_id,
                container=snapshot.parent_container or snapshot.container,
                operation=operation,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code=snapshot.zone_code,
                qty=int(snapshot.qty or 0),
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by) or "reachtruck",
                occurred_at=now,
                payload={"pallet_code": normalized_pallet, "snapshot_id": snapshot.id, "free_move": True},
            )
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
            snapshot.last_event = event
            snapshot.save(update_fields=["active_operation", "active_operation_type", "last_event", "updated_at"])
        operation.status = WarehouseOperation.STATUS_CANCELED
        operation.completed_at = now
        operation.save(update_fields=["status", "completed_at", "updated_at"])
        operation.tasks.update(
            status=WarehouseOperationTask.STATUS_CANCELED,
            completed_at=now,
            updated_at=now,
        )
        return operation

    @classmethod
    @transaction.atomic
    def complete_processing_output_return_to_storage(
        cls,
        *,
        agency: Agency,
        order_id: str,
        pallet_code: str,
        destination_zone_code: str = "OS",
        destination_row_no: int = 0,
        destination_section_no: int = 0,
        destination_tier_no: int = 0,
        destination_cell_no: int = 0,
        performed_by=None,
        warehouse_code: str = "MSK",
    ) -> WarehouseOperation | None:
        order_key = str(order_id or "").strip()
        normalized_pallet = str(pallet_code or "").strip()
        if not agency or not order_key or not normalized_pallet:
            return None
        destination = cls.ensure_location(
            warehouse_code=warehouse_code,
            zone_code=str(destination_zone_code or "OS").strip() or "OS",
            row_no=destination_row_no,
            section_no=destination_section_no,
            tier_no=destination_tier_no,
            cell_no=destination_cell_no,
        )
        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("container", "parent_container", "location")
            .filter(
                agency=agency,
                source_context_type="processing",
                source_context_id=order_key,
                is_archived=False,
                qty__gt=0,
            )
            .filter(
                Q(container_code=normalized_pallet)
                | Q(container__container_code=normalized_pallet)
                | Q(parent_container__container_code=normalized_pallet)
            )
            .order_by("id")
        )
        if not snapshots:
            pallet_container = (
                WarehouseContainer.objects.select_for_update(of=("self",))
                .select_related("current_location")
                .filter(agency=agency, container_code=normalized_pallet)
                .order_by("id")
                .first()
            )
            if pallet_container is None:
                return None
            container_ids = list(
                WarehouseContainer.objects.filter(
                    Q(id=pallet_container.id) | Q(parent_container_id=pallet_container.id)
                ).values_list("id", flat=True)
            )
            has_active_stock = (
                WarehouseStockSnapshot.objects.select_for_update(of=("self",))
                .filter(agency=agency, is_archived=False, qty__gt=0)
                .filter(
                    Q(container_id__in=container_ids)
                    | Q(parent_container_id=pallet_container.id)
                    | Q(container_code=normalized_pallet)
                )
                .exists()
            )
            if has_active_stock:
                raise ValueError(
                    "Паллета содержит активный складской остаток другого контура; "
                    "автоматическое завершение возврата запрещено."
                )
            source_location = pallet_container.current_location
            source_zone_code = str(
                getattr(source_location, "zone_code", "") or ""
            ).strip().upper()
            if source_zone_code and source_zone_code != "OBR":
                raise ValueError(
                    f"Паллета {normalized_pallet} находится не в зоне OBR."
                )

            now = timezone.now()
            operation = WarehouseOperation.objects.create(
                agency=agency,
                operation_type=WarehouseOperation.TYPE_PUTAWAY,
                context_type="processing",
                context_id=order_key,
                source_location=source_location,
                destination_location=destination,
                source_document_type="processing",
                source_document_id=order_key,
                source_zone_code=source_zone_code or "OBR",
                destination_zone_code=destination.zone_code,
                status=WarehouseOperation.STATUS_DONE,
                requested_by=performed_by,
                requested_by_role=cls._role_of(performed_by) or "reachtruck",
                assigned_executor_role="reachtruck",
                planned_qty=0,
                done_qty=0,
                started_at=now,
                completed_at=now,
                comment="Возврат контейнера обработки без активного складского остатка",
            )
            WarehouseOperationTask.objects.create(
                operation=operation,
                task_type=WarehouseOperationTask.TYPE_PALLET_MOVE,
                container=pallet_container,
                from_location=source_location,
                to_location=destination,
                from_zone_code=source_zone_code or "OBR",
                to_zone_code=destination.zone_code,
                qty_planned=0,
                qty_done=0,
                status=WarehouseOperationTask.STATUS_DONE,
                executor_role="reachtruck",
                payload={
                    "container_code": normalized_pallet,
                    "processing_order_id": order_key,
                    "container_only_return": True,
                },
                started_at=now,
                completed_at=now,
            )
            WarehouseEvent.objects.create(
                agency=agency,
                event_type=WarehouseEventType.MOVEMENT_COMPLETED.value,
                stock_context_type="processing",
                stock_context_id=order_key,
                container=pallet_container,
                operation=operation,
                from_location=source_location,
                to_location=destination,
                from_zone_code=source_zone_code or "OBR",
                to_zone_code=destination.zone_code,
                qty=0,
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by) or "reachtruck",
                occurred_at=now,
                payload={
                    "pallet_code": normalized_pallet,
                    "processing_order_id": order_key,
                    "container_only_return": True,
                },
            )
            WarehouseContainer.objects.filter(id__in=container_ids).update(
                current_location=destination,
                updated_at=now,
            )
            return operation
        if all(
            str(snapshot.zone_code or "").strip().upper() == str(destination.zone_code or "").strip().upper()
            and str(snapshot.warehouse_state_code or "").strip() == WarehouseStateCode.STORED.value
            for snapshot in snapshots
        ):
            return None

        source_location = snapshots[0].location
        operation = WarehouseOperation.objects.create(
            agency=agency,
            operation_type=WarehouseOperation.TYPE_PUTAWAY,
            context_type="processing",
            context_id=order_key,
            source_location=source_location,
            destination_location=destination,
            source_document_type="processing",
            source_document_id=order_key,
            source_zone_code=str(snapshots[0].zone_code or "").strip(),
            destination_zone_code=destination.zone_code,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
            requested_by=performed_by,
            requested_by_role=cls._role_of(performed_by) or "reachtruck",
            assigned_executor_role="reachtruck",
            planned_qty=sum(int(snapshot.qty or 0) for snapshot in snapshots),
            started_at=timezone.now(),
            comment="Р’РѕР·РІСЂР°С‚ СЂРµР·СѓР»СЊС‚Р°С‚Р° РѕР±СЂР°Р±РѕС‚РєРё РЅР° СЃРєР»Р°Рґ",
        )
        move_container = snapshots[0].parent_container or snapshots[0].container
        WarehouseOperationTask.objects.create(
            operation=operation,
            task_type=(
                WarehouseOperationTask.TYPE_PALLET_MOVE
                if move_container
                and move_container.container_type
                in {WarehouseContainer.TYPE_PALLET, WarehouseContainer.TYPE_MIXED_PALLET}
                else WarehouseOperationTask.TYPE_BOX_MOVE
            ),
            container=move_container,
            from_location=source_location,
            to_location=destination,
            from_zone_code=operation.source_zone_code,
            to_zone_code=destination.zone_code,
            qty_planned=operation.planned_qty,
            status=WarehouseOperationTask.STATUS_IN_PROGRESS,
            executor_role="reachtruck",
            payload={
                "snapshot_ids": [snapshot.id for snapshot in snapshots],
                "container_code": normalized_pallet,
                "processing_order_id": order_key,
            },
            started_at=timezone.now(),
        )

        total_done = 0
        touched_container_ids: set[int] = set()
        for snapshot in snapshots:
            current_code = str(snapshot.warehouse_state_code or "").strip()
            event_type = WarehouseEventType.STOCK_RETURNED_TO_STORAGE
            operation_type = operation.operation_type
            try:
                transition = WarehouseTransitionService.apply_event(
                    current_code,
                    event_type,
                    operation_type=operation_type,
                    zone_to=destination.zone_code,
                )
            except ValueError:
                event_type = WarehouseEventType.MOVEMENT_COMPLETED
                try:
                    transition = WarehouseTransitionService.apply_event(
                        current_code,
                        event_type,
                        operation_type=operation_type,
                        zone_to=destination.zone_code,
                    )
                except ValueError:
                    transition = WarehouseTransitionService.apply_event(
                        WarehouseStateCode.PLACED_AFTER_PROCESSING.value,
                        event_type,
                        operation_type=operation_type,
                        zone_to=destination.zone_code,
                    )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=event_type.value,
                stock_context_type="",
                stock_context_id="",
                container=snapshot.parent_container or snapshot.container,
                operation=operation,
                from_location=snapshot.location,
                to_location=destination,
                from_zone_code=snapshot.zone_code,
                to_zone_code=destination.zone_code,
                qty=int(snapshot.qty or 0),
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by) or "reachtruck",
                occurred_at=timezone.now(),
                payload={
                    "pallet_code": normalized_pallet,
                    "processing_order_id": order_key,
                    "snapshot_id": snapshot.id,
                    "source_context_type": snapshot.source_context_type,
                    "source_context_id": snapshot.source_context_id,
                },
            )
            if snapshot.container_id:
                touched_container_ids.add(int(snapshot.container_id))
            if snapshot.parent_container_id:
                touched_container_ids.add(int(snapshot.parent_container_id))
            snapshot.location = destination
            snapshot.zone_code = destination.zone_code
            snapshot.zone_kind = destination.zone_kind
            snapshot.warehouse_state_code = transition.code.value
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "location",
                    "zone_code",
                    "zone_kind",
                    "warehouse_state_code",
                    "active_operation",
                    "active_operation_type",
                    "last_event",
                    "updated_at",
                ]
            )
            total_done += int(snapshot.qty or 0)

        if touched_container_ids:
            WarehouseContainer.objects.filter(id__in=touched_container_ids).update(
                current_location=destination,
                updated_at=timezone.now(),
            )

        operation.status = WarehouseOperation.STATUS_DONE
        operation.done_qty = total_done
        operation.completed_at = timezone.now()
        operation.save(update_fields=["status", "done_qty", "completed_at", "updated_at"])
        operation.tasks.update(
            status=WarehouseOperationTask.STATUS_DONE,
            qty_done=models.F("qty_planned"),
            completed_at=timezone.now(),
            updated_at=timezone.now(),
        )
        return operation

    @classmethod
    @transaction.atomic
    def release_canceled_shipping_stock_to_available(
        cls,
        *,
        agency: Agency,
        order_id: str,
        performed_by=None,
    ) -> int:
        """Expose canceled OTG stock immediately without faking its location."""
        order_key = str(order_id or "").strip()
        if not agency or not order_key:
            return 0
        flow_states = [
            WarehouseStateCode.IN_OTG.value,
            WarehouseStateCode.PALLETIZING.value,
            WarehouseStateCode.READY_FOR_LOADING.value,
        ]
        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("container", "location", "last_event")
            .filter(
                agency=agency,
                last_event__stock_context_type="shipping",
                last_event__stock_context_id=order_key,
                warehouse_state_code__in=flow_states,
                is_archived=False,
                qty__gt=0,
            )
            .order_by("id")
        )
        now = timezone.now()
        box_codes: set[str] = set()
        for snapshot in snapshots:
            box_code = str(snapshot.container_code or "").strip()
            if snapshot.container is not None:
                box_code = str(snapshot.container.container_code or box_code).strip()
            if box_code:
                box_codes.add(box_code.lower())
            next_available = max(
                int(snapshot.qty or 0)
                - int(snapshot.processing_reserved_qty or 0)
                - int(snapshot.other_reserved_qty or 0),
                0,
            )
            if (
                int(snapshot.shipping_reserved_qty or 0) == 0
                and int(snapshot.available_qty or 0) == next_available
            ):
                continue
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.SHIPPING_RESERVE_RELEASED.value,
                stock_context_type="shipping",
                stock_context_id=order_key,
                container=snapshot.container,
                source_document_type="shipping",
                source_document_id=order_key,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code=snapshot.zone_code,
                qty=int(snapshot.qty or 0),
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by),
                occurred_at=now,
                payload={
                    "snapshot_id": snapshot.id,
                    "canceled_shipping_available_immediately_v1": True,
                },
            )
            snapshot.shipping_reserved_qty = 0
            snapshot.available_qty = next_available
            snapshot.last_event = event
            snapshot.snapshot_version = int(snapshot.snapshot_version or 0) + 1
            snapshot.save(
                update_fields=[
                    "shipping_reserved_qty",
                    "available_qty",
                    "last_event",
                    "snapshot_version",
                    "updated_at",
                ]
            )
        return len(box_codes)

    @classmethod
    @transaction.atomic
    def complete_canceled_shipping_return_to_storage(
        cls,
        *,
        agency: Agency,
        order_id: str,
        pallet_code: str,
        box_codes: list[str] | None = None,
        destination_zone_code: str = "OS",
        destination_row_no: int = 0,
        destination_section_no: int = 0,
        destination_tier_no: int = 0,
        destination_cell_no: int = 0,
        performed_by=None,
        warehouse_code: str = "MSK",
    ) -> WarehouseOperation | None:
        order_key = str(order_id or "").strip()
        normalized_pallet = str(pallet_code or "").strip()
        if not agency or not order_key or not normalized_pallet:
            return None
        destination = cls.ensure_location(
            warehouse_code=warehouse_code,
            zone_code=str(destination_zone_code or "OS").strip() or "OS",
            row_no=destination_row_no,
            section_no=destination_section_no,
            tier_no=destination_tier_no,
            cell_no=destination_cell_no,
        )
        normalized_box_codes = {
            str(code or "").strip().lower()
            for code in (box_codes or [])
            if str(code or "").strip()
        }
        return_pallet = (
            WarehouseContainer.objects.select_for_update()
            .filter(
                agency=agency,
                container_code=normalized_pallet,
                source_context_type="shipping",
                source_context_id=order_key,
            )
            .first()
        )
        if return_pallet is None:
            raise ValueError(f"Возвратная паллета {normalized_pallet} не найдена для заявки {order_key}.")
        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("container", "parent_container", "location", "last_event")
            .filter(
                agency=agency,
                is_archived=False,
                qty__gt=0,
                parent_container=return_pallet,
            )
            .order_by("id")
        )
        if normalized_box_codes:
            snapshots = [
                snapshot
                for snapshot in snapshots
                if str(snapshot.container_code or "").strip().lower() in normalized_box_codes
                or str(getattr(snapshot.container, "container_code", "") or "").strip().lower()
                in normalized_box_codes
            ]
        if not snapshots:
            raise ValueError(f"На возвратной паллете {normalized_pallet} не найдены короба из задания.")
        if all(
            str(snapshot.zone_code or "").strip().upper() == str(destination.zone_code or "").strip().upper()
            and str(snapshot.warehouse_state_code or "").strip() == WarehouseStateCode.STORED.value
            for snapshot in snapshots
        ):
            return None

        source_location = snapshots[0].location
        now = timezone.now()
        operation = WarehouseOperation.objects.create(
            agency=agency,
            operation_type=WarehouseOperation.TYPE_RETURN_TO_STORAGE,
            context_type="shipping",
            context_id=order_key,
            source_location=source_location,
            destination_location=destination,
            source_document_type="shipping",
            source_document_id=order_key,
            source_zone_code=str(snapshots[0].zone_code or "").strip(),
            destination_zone_code=destination.zone_code,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
            requested_by=performed_by,
            requested_by_role=cls._role_of(performed_by) or "reachtruck",
            assigned_executor_role="reachtruck",
            planned_qty=sum(int(snapshot.qty or 0) for snapshot in snapshots),
            started_at=now,
            comment="Возврат паллеты отмененной отгрузки из OTG на хранение",
        )
        move_container = snapshots[0].parent_container or snapshots[0].container
        operation_task = WarehouseOperationTask.objects.create(
            operation=operation,
            task_type=WarehouseOperationTask.TYPE_PALLET_MOVE,
            container=move_container,
            from_location=source_location,
            to_location=destination,
            from_zone_code=operation.source_zone_code,
            to_zone_code=destination.zone_code,
            qty_planned=operation.planned_qty,
            status=WarehouseOperationTask.STATUS_IN_PROGRESS,
            executor_role="reachtruck",
            payload={
                "snapshot_ids": [snapshot.id for snapshot in snapshots],
                "container_code": normalized_pallet,
                "shipping_order_id": order_key,
                "canceled_shipping_return_v1": True,
            },
            started_at=now,
        )

        total_done = 0
        touched_container_ids: set[int] = set()
        for snapshot in snapshots:
            transition = WarehouseTransitionService.apply_event(
                str(snapshot.warehouse_state_code or "").strip(),
                WarehouseEventType.STOCK_RETURNED_TO_STORAGE,
                operation_type=WarehouseOperation.TYPE_RETURN_TO_STORAGE,
                zone_to=destination.zone_code,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.STOCK_RETURNED_TO_STORAGE.value,
                stock_context_type="warehouse_return",
                stock_context_id=normalized_pallet,
                container=snapshot.parent_container or snapshot.container,
                operation=operation,
                operation_task=operation_task,
                source_document_type="shipping",
                source_document_id=order_key,
                from_location=snapshot.location,
                to_location=destination,
                from_zone_code=snapshot.zone_code,
                to_zone_code=destination.zone_code,
                qty=int(snapshot.qty or 0),
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by) or "reachtruck",
                occurred_at=now,
                payload={
                    "pallet_code": normalized_pallet,
                    "shipping_order_id": order_key,
                    "snapshot_id": snapshot.id,
                    "canceled_shipping_return_v1": True,
                },
            )
            if snapshot.container_id:
                touched_container_ids.add(int(snapshot.container_id))
            if snapshot.parent_container_id:
                touched_container_ids.add(int(snapshot.parent_container_id))
            snapshot.location = destination
            snapshot.zone_code = destination.zone_code
            snapshot.zone_kind = destination.zone_kind
            if int(snapshot.shipping_reserved_qty or 0) > 0:
                snapshot.warehouse_state_code = WarehouseStateCode.RESERVED_FOR_SHIPPING.value
            elif int(snapshot.processing_reserved_qty or 0) > 0:
                snapshot.warehouse_state_code = WarehouseStateCode.RESERVED_FOR_PROCESSING.value
            else:
                snapshot.warehouse_state_code = transition.code.value
            snapshot.available_qty = max(
                int(snapshot.qty or 0)
                - int(snapshot.processing_reserved_qty or 0)
                - int(snapshot.shipping_reserved_qty or 0)
                - int(snapshot.other_reserved_qty or 0),
                0,
            )
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "location",
                    "zone_code",
                    "zone_kind",
                    "warehouse_state_code",
                    "available_qty",
                    "active_operation",
                    "active_operation_type",
                    "last_event",
                    "updated_at",
                ]
            )
            total_done += int(snapshot.qty or 0)

        if touched_container_ids:
            WarehouseContainer.objects.filter(id__in=touched_container_ids).update(
                current_location=destination,
                updated_at=now,
            )
        WarehouseContainer.objects.filter(pk=return_pallet.pk).update(
            source_context_type="",
            source_context_id="",
            updated_at=now,
        )
        operation.status = WarehouseOperation.STATUS_DONE
        operation.done_qty = total_done
        operation.completed_at = now
        operation.save(update_fields=["status", "done_qty", "completed_at", "updated_at"])
        operation_task.status = WarehouseOperationTask.STATUS_DONE
        operation_task.qty_done = operation_task.qty_planned
        operation_task.completed_at = now
        operation_task.save(update_fields=["status", "qty_done", "completed_at", "updated_at"])
        return operation

    @classmethod
    @transaction.atomic
    def reserve_for_processing(
        cls,
        *,
        agency: Agency,
        order_id: str,
        items: list[dict],
        created_by=None,
        source_document_type: str = "processing_order",
        source_document_id: str = "",
    ) -> list[WarehouseReserve]:
        from .fbs_quantity_reserves import protect_new_claims
        items = list(items)
        protect_new_claims(agency, items)
        order_key = str(order_id or "").strip()
        if not order_key:
            raise ValueError("order_id is required for processing reserve")
        source_document_id = str(source_document_id or order_key).strip()
        reserves: list[WarehouseReserve] = []
        for item in items:
            qty = max(int(item.get("qty") or 0), 0)
            if qty <= 0:
                continue
            allocations = cls._match_snapshots_for_processing_reserve(
                agency=agency,
                item=item,
                required_qty=qty,
            )
            for snapshot, reserved_qty in allocations:
                transition = WarehouseTransitionService.apply_event(
                    snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                    WarehouseEventType.PROCESSING_RESERVED,
                )
                reserve = WarehouseReserve.objects.create(
                    agency=agency,
                    reserve_type=WarehouseReserve.TYPE_PROCESSING,
                    context_type="processing",
                    context_id=order_key,
                    sku_ref=snapshot.sku_ref,
                    sku_code=snapshot.sku_code,
                    size=snapshot.size,
                    barcode=snapshot.barcode,
                    goods_type=snapshot.goods_type,
                    marking_code=snapshot.marking_code,
                    qty_reserved=reserved_qty,
                    status=WarehouseReserve.STATUS_ACTIVE,
                    source_document_type=source_document_type,
                    source_document_id=source_document_id,
                    created_by=created_by,
                )
                event = WarehouseEvent.objects.create(
                    agency=agency,
                    event_type=WarehouseEventType.PROCESSING_RESERVED.value,
                    stock_context_type="processing",
                    stock_context_id=order_key,
                    container=snapshot.container,
                    reserve=reserve,
                    source_document_type=source_document_type,
                    source_document_id=source_document_id,
                    from_location=snapshot.location,
                    to_location=snapshot.location,
                    from_zone_code=snapshot.zone_code,
                    to_zone_code=snapshot.zone_code,
                    qty=reserved_qty,
                    performed_by=created_by,
                    performed_by_role=cls._role_of(created_by),
                    occurred_at=timezone.now(),
                    payload={"snapshot_id": snapshot.id},
                )
                snapshot.processing_reserved_qty += reserved_qty
                snapshot.available_qty -= reserved_qty
                snapshot.warehouse_state_code = transition.code.value
                snapshot.last_event = event
                snapshot.save(
                    update_fields=[
                        "processing_reserved_qty",
                        "available_qty",
                        "warehouse_state_code",
                        "last_event",
                        "updated_at",
                    ]
                )
                reserves.append(reserve)
        return reserves

    @classmethod
    @transaction.atomic
    def request_move_to_processing(
        cls,
        *,
        agency: Agency,
        order_id: str,
        container_codes: list[str] | None = None,
        destination_row_no: int = 0,
        destination_section_no: int = 0,
        destination_tier_no: int = 0,
        destination_cell_no: int = 0,
        destination_location_code: str = "",
        allow_legacy_generic_destination: bool = False,
        requested_by=None,
        requested_by_role: str = "processing_lead",
        warehouse_code: str = "MSK",
        source_document_type: str = "",
        source_document_id: str = "",
    ) -> WarehouseOperation:
        order_key = str(order_id or "").strip()
        snapshot_query = WarehouseStockSnapshot.objects.select_related("location", "container").filter(
            agency=agency,
            warehouse_state_code=WarehouseStateCode.RESERVED_FOR_PROCESSING.value,
            processing_reserved_qty__gt=0,
            is_archived=False,
        )
        normalized_container_codes = [
            str(value or "").strip()
            for value in (container_codes or [])
            if str(value or "").strip()
        ]
        if normalized_container_codes:
            snapshot_query = snapshot_query.filter(
                Q(container_code__in=normalized_container_codes)
                | Q(parent_container__container_code__in=normalized_container_codes)
            )
        snapshots = list(snapshot_query.order_by("id"))
        snapshots = [snapshot for snapshot in snapshots if cls._snapshot_matches_processing_context(snapshot, order_key)]
        if not snapshots:
            raise ValueError("No processing-reserved snapshots ready for move to OBR")

        source_location = snapshots[0].location or cls.ensure_location(warehouse_code=warehouse_code, zone_code="OS")
        destination = cls.concrete_movement_destination(
            warehouse_code=warehouse_code,
            zone_code="OBR",
            location_code=destination_location_code,
            row_no=destination_row_no,
            section_no=destination_section_no,
            tier_no=destination_tier_no,
            cell_no=destination_cell_no,
            allow_legacy_generic_location=allow_legacy_generic_destination,
        )
        operation = WarehouseOperation.objects.create(
            agency=agency,
            operation_type=WarehouseOperation.TYPE_MOVE_TO_PROCESSING,
            context_type="processing",
            context_id=order_key,
            source_location=source_location,
            destination_location=destination,
            source_document_type=str(source_document_type or "").strip(),
            source_document_id=str(source_document_id or "").strip(),
            source_zone_code=source_location.zone_code,
            destination_zone_code=destination.zone_code,
            status=WarehouseOperation.STATUS_PLANNED,
            requested_by=requested_by,
            requested_by_role=requested_by_role,
            assigned_executor_role="reachtruck",
            planned_qty=sum(int(snapshot.processing_reserved_qty or 0) for snapshot in snapshots),
        )
        WarehouseEvent.objects.create(
            agency=agency,
            event_type=WarehouseEventType.MOVEMENT_REQUESTED.value,
            stock_context_type="processing",
            stock_context_id=order_key,
            operation=operation,
            from_location=source_location,
            to_location=destination,
            from_zone_code=source_location.zone_code,
            to_zone_code=destination.zone_code,
            qty=operation.planned_qty,
            performed_by=requested_by,
            performed_by_role=requested_by_role,
            occurred_at=timezone.now(),
        )
        grouped_tasks: dict[tuple[str, int], dict] = {}
        selected_container_codes = {code.lower() for code in normalized_container_codes}
        for snapshot in snapshots:
            move_container = snapshot.container
            if (
                snapshot.parent_container
                and str(snapshot.parent_container.container_code or "").strip().lower() in selected_container_codes
            ):
                move_container = snapshot.parent_container
            elif (
                snapshot.container
                and str(snapshot.container.container_code or "").strip().lower() in selected_container_codes
            ):
                move_container = snapshot.container
            elif snapshot.parent_container and not selected_container_codes:
                move_container = snapshot.parent_container

            if move_container:
                task_key = ("container", int(move_container.id))
            else:
                task_key = ("snapshot", int(snapshot.id))
            task_bucket = grouped_tasks.setdefault(
                task_key,
                {
                    "container": move_container,
                    "from_location": snapshot.location,
                    "from_zone_code": snapshot.zone_code,
                    "qty_planned": 0,
                    "snapshot_ids": [],
                    "container_code": (
                        move_container.container_code
                        if move_container
                        else snapshot.container_code
                    ),
                },
            )
            task_bucket["qty_planned"] += int(snapshot.processing_reserved_qty or snapshot.qty or 0)
            task_bucket["snapshot_ids"].append(int(snapshot.id))
        for task_bucket in grouped_tasks.values():
            task_container = task_bucket["container"]
            WarehouseOperationTask.objects.create(
                operation=operation,
                task_type=(
                    WarehouseOperationTask.TYPE_PALLET_MOVE
                    if task_container
                    and task_container.container_type
                    in {WarehouseContainer.TYPE_PALLET, WarehouseContainer.TYPE_MIXED_PALLET}
                    else WarehouseOperationTask.TYPE_BOX_MOVE
                ),
                container=task_container,
                from_location=task_bucket["from_location"],
                to_location=destination,
                from_zone_code=task_bucket["from_zone_code"],
                to_zone_code=destination.zone_code,
                qty_planned=int(task_bucket["qty_planned"] or 0),
                status=WarehouseOperationTask.STATUS_CREATED,
                executor_role="reachtruck",
                payload={
                    "snapshot_ids": task_bucket["snapshot_ids"],
                    "container_code": task_bucket["container_code"],
                },
            )
        for snapshot in snapshots:
            snapshot.active_operation = operation
            snapshot.active_operation_type = operation.operation_type
            snapshot.save(update_fields=["active_operation", "active_operation_type", "updated_at"])
        return operation

    @classmethod
    @transaction.atomic
    def start_move_to_processing(
        cls,
        *,
        operation: WarehouseOperation,
        performed_by=None,
    ) -> WarehouseOperation:
        if operation.operation_type != WarehouseOperation.TYPE_MOVE_TO_PROCESSING:
            raise ValueError("Only move_to_processing operations can be started by this write-path")
        now = timezone.now()
        operation.status = WarehouseOperation.STATUS_IN_PROGRESS
        operation.started_at = operation.started_at or now
        operation.save(update_fields=["status", "started_at", "updated_at"])
        for snapshot in WarehouseStockSnapshot.objects.filter(active_operation=operation, is_archived=False).order_by("id"):
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.MOVEMENT_STARTED,
                operation_type=operation.operation_type,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.MOVEMENT_STARTED.value,
                stock_context_type="processing",
                stock_context_id=operation.context_id,
                container=snapshot.container,
                operation=operation,
                from_location=snapshot.location,
                to_location=operation.destination_location,
                from_zone_code=snapshot.zone_code,
                to_zone_code=operation.destination_zone_code,
                qty=int(snapshot.processing_reserved_qty or snapshot.qty or 0),
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by) or "reachtruck",
                occurred_at=now,
            )
            snapshot.warehouse_state_code = transition.code.value
            snapshot.last_event = event
            snapshot.save(update_fields=["warehouse_state_code", "last_event", "updated_at"])
        operation.tasks.update(
            status=WarehouseOperationTask.STATUS_IN_PROGRESS,
            started_at=now,
            updated_at=now,
        )
        return operation

    @classmethod
    @transaction.atomic
    def complete_move_to_processing(
        cls,
        *,
        operation: WarehouseOperation,
        performed_by=None,
    ) -> WarehouseOperation:
        if operation.operation_type != WarehouseOperation.TYPE_MOVE_TO_PROCESSING:
            raise ValueError("Only move_to_processing operations can be completed by this write-path")
        destination = operation.destination_location
        if destination is None:
            raise ValueError("Processing move operation must have destination_location")

        total_done = 0
        source_container_ids: set[int] = set()
        source_parent_container_ids: set[int] = set()
        for snapshot in WarehouseStockSnapshot.objects.filter(active_operation=operation, is_archived=False).order_by("id"):
            source_container = snapshot.container
            source_parent_container = snapshot.parent_container
            if source_container is not None and int(source_container.id or 0) > 0:
                source_container_ids.add(int(source_container.id))
            if source_parent_container is not None and int(source_parent_container.id or 0) > 0:
                source_parent_container_ids.add(int(source_parent_container.id))
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.PROCESSING_ZONE_ARRIVED,
                zone_to=destination.zone_code,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.PROCESSING_ZONE_ARRIVED.value,
                stock_context_type="processing",
                stock_context_id=operation.context_id,
                container=snapshot.container,
                operation=operation,
                from_location=snapshot.location,
                to_location=destination,
                from_zone_code=snapshot.zone_code,
                to_zone_code=destination.zone_code,
                qty=int(snapshot.processing_reserved_qty or snapshot.qty or 0),
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by) or "reachtruck",
                occurred_at=timezone.now(),
                payload={
                    "source_box_code": str(getattr(source_container, "container_code", "") or "").strip(),
                    "source_pallet_code": str(getattr(source_parent_container, "container_code", "") or "").strip(),
                },
            )
            snapshot.location = destination
            snapshot.zone_code = destination.zone_code
            snapshot.zone_kind = destination.zone_kind
            snapshot.source_context_type = "processing"
            snapshot.source_context_id = str(operation.context_id or "").strip()
            snapshot.warehouse_state_code = transition.code.value
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "location",
                    "zone_code",
                    "zone_kind",
                    "source_context_type",
                    "source_context_id",
                    "warehouse_state_code",
                    "last_event",
                    "updated_at",
                ]
            )
            total_done += int(snapshot.processing_reserved_qty or snapshot.qty or 0)
        if source_container_ids:
            WarehouseContainer.objects.filter(id__in=source_container_ids).update(
                current_location=None,
                parent_container=None,
                status=WarehouseContainer.STATUS_ARCHIVED,
                updated_at=timezone.now(),
            )
        if source_parent_container_ids:
            active_parent_ids = set(
                WarehouseContainer.objects.filter(
                    parent_container_id__in=source_parent_container_ids,
                    status=WarehouseContainer.STATUS_ACTIVE,
                ).values_list("parent_container_id", flat=True)
            )
            parent_ids_to_archive = source_parent_container_ids - {int(parent_id or 0) for parent_id in active_parent_ids}
            if parent_ids_to_archive:
                WarehouseContainer.objects.filter(id__in=parent_ids_to_archive).update(
                    current_location=None,
                    parent_container=None,
                    status=WarehouseContainer.STATUS_ARCHIVED,
                    updated_at=timezone.now(),
                )
        operation.status = WarehouseOperation.STATUS_DONE
        operation.done_qty = total_done
        operation.completed_at = timezone.now()
        operation.save(update_fields=["status", "done_qty", "completed_at", "updated_at"])
        operation.tasks.update(
            status=WarehouseOperationTask.STATUS_DONE,
            qty_done=models.F("qty_planned"),
            completed_at=timezone.now(),
            updated_at=timezone.now(),
        )
        return operation

    @staticmethod
    def _processing_rebind_target_qty(snapshot: WarehouseStockSnapshot) -> int:
        return max(int(snapshot.available_qty or 0), 0) + max(int(snapshot.processing_reserved_qty or 0), 0)

    @classmethod
    def _apply_processing_reserve_rebind(
        cls,
        *,
        reserve: WarehouseReserve,
        from_snapshot: WarehouseStockSnapshot | None,
        to_snapshot: WarehouseStockSnapshot,
        qty: int,
        performed_by=None,
        source_document_id: str = "",
    ) -> None:
        qty_to_move = max(int(qty or 0), 0)
        if qty_to_move <= 0:
            return
        if from_snapshot is not None and int(from_snapshot.id or 0) == int(to_snapshot.id or 0):
            return
        from .fbs_quantity_reserves import protect_sources
        protect_sources([to_snapshot])
        rebind_operation = None
        if (
            from_snapshot is not None
            and from_snapshot.active_operation_id
            and from_snapshot.active_operation is not None
            and from_snapshot.active_operation.operation_type == WarehouseOperation.TYPE_MOVE_TO_PROCESSING
            and str(from_snapshot.active_operation.context_type or "").strip().lower() == "processing"
            and str(from_snapshot.active_operation.context_id or "").strip() == str(reserve.context_id or "").strip()
        ):
            rebind_operation = from_snapshot.active_operation
        if from_snapshot is not None and int(from_snapshot.processing_reserved_qty or 0) < qty_to_move:
            raise ValueError("Not enough processing reserve on source snapshot for rebind.")
        target_capacity_qty = cls._processing_rebind_target_qty(to_snapshot)
        target_available_qty = int(to_snapshot.available_qty or 0)
        if target_capacity_qty < qty_to_move:
            raise ValueError("Not enough available qty on target snapshot for processing reserve rebind.")
        if from_snapshot is not None:
            try:
                release_transition = WarehouseTransitionService.apply_event(
                    from_snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                    WarehouseEventType.PROCESSING_RESERVE_RELEASED,
                )
                next_released_state = release_transition.code.value
            except ValueError:
                next_released_state = from_snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value
            release_event = WarehouseEvent.objects.create(
                agency=from_snapshot.agency,
                event_type=WarehouseEventType.PROCESSING_RESERVE_RELEASED.value,
                stock_context_type=reserve.context_type,
                stock_context_id=reserve.context_id,
                container=from_snapshot.container,
                reserve=reserve,
                source_document_type=reserve.source_document_type,
                source_document_id=source_document_id or reserve.source_document_id,
                from_location=from_snapshot.location,
                to_location=from_snapshot.location,
                from_zone_code=from_snapshot.zone_code,
                to_zone_code=from_snapshot.zone_code,
                qty=qty_to_move,
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by),
                occurred_at=timezone.now(),
                payload={
                    "snapshot_id": from_snapshot.id,
                    "box_code": str(from_snapshot.container_code or ""),
                    "rebound_to_snapshot_id": to_snapshot.id,
                    "rebound_to_box_code": str(to_snapshot.container_code or ""),
                },
            )
            from_snapshot.processing_reserved_qty -= qty_to_move
            from_snapshot.available_qty = min(
                int(from_snapshot.qty or 0),
                int(from_snapshot.available_qty or 0) + qty_to_move,
            )
            if int(from_snapshot.processing_reserved_qty or 0) <= 0:
                if int(from_snapshot.shipping_reserved_qty or 0) > 0:
                    from_snapshot.warehouse_state_code = WarehouseStateCode.RESERVED_FOR_SHIPPING.value
                else:
                    from_snapshot.warehouse_state_code = (
                        cls._state_for_location(from_snapshot.location)
                        if from_snapshot.location is not None
                        else next_released_state
                    )
                if rebind_operation is not None:
                    from_snapshot.active_operation = None
                    from_snapshot.active_operation_type = ""
            from_snapshot.last_event = release_event
            from_snapshot_update_fields = [
                "processing_reserved_qty",
                "available_qty",
                "warehouse_state_code",
                "last_event",
            ]
            if rebind_operation is not None and int(from_snapshot.processing_reserved_qty or 0) <= 0:
                from_snapshot_update_fields.extend(["active_operation", "active_operation_type"])
            from_snapshot.save(update_fields=from_snapshot_update_fields + ["updated_at"])

        try:
            reserve_transition = WarehouseTransitionService.apply_event(
                to_snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.PROCESSING_RESERVED,
            )
            next_reserved_state = reserve_transition.code.value
        except ValueError:
            next_reserved_state = to_snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value
        reserve_event = WarehouseEvent.objects.create(
            agency=to_snapshot.agency,
            event_type=WarehouseEventType.PROCESSING_RESERVED.value,
            stock_context_type=reserve.context_type,
            stock_context_id=reserve.context_id,
            container=to_snapshot.container,
            reserve=reserve,
            source_document_type=reserve.source_document_type,
            source_document_id=source_document_id or reserve.source_document_id,
            from_location=to_snapshot.location,
            to_location=to_snapshot.location,
            from_zone_code=to_snapshot.zone_code,
            to_zone_code=to_snapshot.zone_code,
            qty=qty_to_move,
            performed_by=performed_by,
            performed_by_role=cls._role_of(performed_by),
            occurred_at=timezone.now(),
            payload={
                "snapshot_id": to_snapshot.id,
                "box_code": str(to_snapshot.container_code or ""),
                "rebound_from_snapshot_id": int(from_snapshot.id or 0) if from_snapshot is not None else 0,
                "rebound_from_box_code": str(from_snapshot.container_code or "") if from_snapshot is not None else "",
            },
        )
        to_snapshot.processing_reserved_qty += qty_to_move
        to_snapshot.available_qty = max(target_available_qty - qty_to_move, 0)
        to_snapshot.warehouse_state_code = next_reserved_state
        to_snapshot.last_event = reserve_event
        to_snapshot_update_fields = [
            "processing_reserved_qty",
            "available_qty",
            "warehouse_state_code",
            "last_event",
        ]
        if rebind_operation is not None and int(to_snapshot.active_operation_id or 0) != int(rebind_operation.id or 0):
            to_snapshot.active_operation = rebind_operation
            to_snapshot.active_operation_type = rebind_operation.operation_type
            to_snapshot_update_fields.extend(["active_operation", "active_operation_type"])
        to_snapshot.save(update_fields=to_snapshot_update_fields + ["updated_at"])
        if (
            rebind_operation is not None
            and from_snapshot is not None
            and from_snapshot.container_id
            and to_snapshot.container_id
            and int(from_snapshot.container_id) != int(to_snapshot.container_id)
        ):
            WarehouseOperationTask.objects.filter(
                operation=rebind_operation,
                container_id=from_snapshot.container_id,
                status__in=[
                    WarehouseOperationTask.STATUS_CREATED,
                    WarehouseOperationTask.STATUS_IN_PROGRESS,
                ],
            ).update(
                container=to_snapshot.container,
                updated_at=timezone.now(),
            )

    @classmethod
    def _ensure_processing_reserve_on_snapshot(
        cls,
        *,
        agency: Agency,
        order_id: str,
        snapshot: WarehouseStockSnapshot,
        qty: int,
        performed_by=None,
        source_document_id: str = "",
    ) -> None:
        order_key = str(order_id or "").strip()
        qty_required = max(int(qty or 0), 0)
        if not order_key or qty_required <= 0:
            return
        active_reserves = list(
            WarehouseReserve.objects.select_for_update()
            .filter(
                agency=agency,
                reserve_type=WarehouseReserve.TYPE_PROCESSING,
                context_type="processing",
                context_id=order_key,
                sku_code=snapshot.sku_code,
                size=snapshot.size,
                barcode=snapshot.barcode,
                goods_type=snapshot.goods_type,
                status__in=[
                    WarehouseReserve.STATUS_ACTIVE,
                    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
                    WarehouseReserve.STATUS_ALLOCATED,
                    WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
                    WarehouseReserve.STATUS_SATISFIED,
                ],
            )
            .order_by("id")
        )
        snapshot_id_by_reserve = cls._reserve_snapshot_id_map(active_reserves)
        current_for_order = 0
        source_snapshot_ids: set[int] = set()
        for reserve in active_reserves:
            reserve_open_qty = cls._reserve_open_qty(reserve)
            if reserve_open_qty <= 0:
                continue
            reserve_id = int(reserve.id or 0)
            reserve_snapshot_id = int(snapshot_id_by_reserve.get(reserve_id) or 0)
            if reserve_snapshot_id == int(snapshot.id or 0):
                current_for_order += reserve_open_qty
            elif reserve_snapshot_id > 0:
                source_snapshot_ids.add(reserve_snapshot_id)
        remaining = max(qty_required - current_for_order, 0)
        if remaining <= 0:
            return
        source_snapshots_by_id = {
            int(source_snapshot.id): source_snapshot
            for source_snapshot in WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("container", "location", "active_operation")
            .filter(id__in=source_snapshot_ids, agency=agency, is_archived=False)
        }
        for reserve in active_reserves:
            if remaining <= 0:
                break
            source_snapshot_id = int(snapshot_id_by_reserve.get(int(reserve.id or 0)) or 0)
            source_snapshot = source_snapshots_by_id.get(source_snapshot_id)
            if source_snapshot is None or int(source_snapshot.id or 0) == int(snapshot.id or 0):
                continue
            if not cls._reserve_matches_snapshot(reserve, source_snapshot):
                continue
            available_reserved_qty = int(source_snapshot.processing_reserved_qty or 0)
            if available_reserved_qty <= 0:
                continue
            reserve_open_qty = cls._reserve_open_qty(reserve)
            if reserve_open_qty <= 0:
                continue
            qty_to_move = min(remaining, available_reserved_qty, reserve_open_qty)
            if qty_to_move <= 0:
                continue
            cls._apply_processing_reserve_rebind(
                reserve=reserve,
                from_snapshot=source_snapshot,
                to_snapshot=snapshot,
                qty=qty_to_move,
                performed_by=performed_by,
                source_document_id=source_document_id or order_key,
            )
            remaining -= qty_to_move
        if remaining <= 0:
            return
        from .fbs_quantity_reserves import protect_new_claims
        protect_new_claims(agency, [dict(sku_code=snapshot.sku_code, size=snapshot.size,
            barcode=snapshot.barcode, goods_type=snapshot.goods_type, qty=remaining)])
        reserve = WarehouseReserve.objects.create(
            agency=agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_key,
            sku_ref=snapshot.sku_ref,
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            marking_code=snapshot.marking_code,
            qty_reserved=remaining,
            status=WarehouseReserve.STATUS_ACTIVE,
            source_document_type="stock_move",
            source_document_id=source_document_id or order_key,
            created_by=performed_by,
        )
        try:
            reserve_transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.PROCESSING_RESERVED,
            )
            next_reserved_state = reserve_transition.code.value
        except ValueError:
            next_reserved_state = snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value
        reserve_event = WarehouseEvent.objects.create(
            agency=agency,
            event_type=WarehouseEventType.PROCESSING_RESERVED.value,
            stock_context_type="processing",
            stock_context_id=order_key,
            container=snapshot.container,
            reserve=reserve,
            source_document_type="stock_move",
            source_document_id=source_document_id or order_key,
            from_location=snapshot.location,
            to_location=snapshot.location,
            from_zone_code=snapshot.zone_code,
            to_zone_code=snapshot.zone_code,
            qty=remaining,
            performed_by=performed_by,
            performed_by_role=cls._role_of(performed_by),
            occurred_at=timezone.now(),
            payload={"snapshot_id": snapshot.id, "box_code": str(snapshot.container_code or "")},
        )
        snapshot.processing_reserved_qty += remaining
        snapshot.available_qty = max(int(snapshot.available_qty or 0) - remaining, 0)
        snapshot.warehouse_state_code = next_reserved_state
        snapshot.last_event = reserve_event
        snapshot.save(
            update_fields=[
                "processing_reserved_qty",
                "available_qty",
                "warehouse_state_code",
                "last_event",
                "updated_at",
            ]
        )

    @classmethod
    def _archive_empty_processing_containers(
        cls,
        *,
        agency: Agency,
        container_ids: set[int],
        parent_container_ids: set[int],
    ) -> None:
        now = timezone.now()
        if container_ids:
            empty_container_ids = set(container_ids) - set(
                WarehouseStockSnapshot.objects.filter(
                    agency=agency,
                    is_archived=False,
                    container_id__in=container_ids,
                ).values_list("container_id", flat=True)
            )
            if empty_container_ids:
                WarehouseContainer.objects.filter(id__in=empty_container_ids).update(
                    current_location=None,
                    parent_container=None,
                    status=WarehouseContainer.STATUS_ARCHIVED,
                    updated_at=now,
                )
        if parent_container_ids:
            active_child_parent_ids = set(
                WarehouseContainer.objects.filter(
                    parent_container_id__in=parent_container_ids,
                    status=WarehouseContainer.STATUS_ACTIVE,
                ).values_list("parent_container_id", flat=True)
            )
            active_snapshot_parent_ids = set(
                WarehouseStockSnapshot.objects.filter(
                    agency=agency,
                    is_archived=False,
                    parent_container_id__in=parent_container_ids,
                ).values_list("parent_container_id", flat=True)
            )
            parent_ids_to_archive = (
                parent_container_ids
                - {int(parent_id or 0) for parent_id in active_child_parent_ids}
                - {int(parent_id or 0) for parent_id in active_snapshot_parent_ids}
            )
            if parent_ids_to_archive:
                WarehouseContainer.objects.filter(id__in=parent_ids_to_archive).update(
                    current_location=None,
                    parent_container=None,
                    status=WarehouseContainer.STATUS_ARCHIVED,
                    updated_at=now,
                )

    @classmethod
    def _lock_processing_move_operation_for_arrival(
        cls,
        *,
        operation_id: int | None,
        agency: Agency,
        order_id: str,
    ) -> WarehouseOperation | None:
        if not operation_id:
            return None
        operation = (
            WarehouseOperation.objects.select_for_update()
            .filter(id=int(operation_id))
            .first()
        )
        if operation is None:
            raise ValueError("Processing move operation was not found.")
        if operation.operation_type != WarehouseOperation.TYPE_MOVE_TO_PROCESSING:
            raise ValueError("Only move_to_processing operations can record OBR arrival.")
        if int(operation.agency_id or 0) != int(agency.id or 0):
            raise ValueError("Processing move operation belongs to another agency.")
        if str(operation.context_type or "").strip() != "processing":
            raise ValueError("Processing move operation has an invalid context type.")
        if str(operation.context_id or "").strip() != str(order_id or "").strip():
            raise ValueError("Processing move operation belongs to another processing order.")
        if operation.status in {
            WarehouseOperation.STATUS_BLOCKED,
            WarehouseOperation.STATUS_CANCELED,
        }:
            raise ValueError("Processing move operation cannot accept OBR arrival in its current status.")
        return operation

    @classmethod
    def _complete_processing_move_operation_for_arrival(
        cls,
        *,
        operation: WarehouseOperation | None,
        source_snapshot_ids: set[int],
        source_container_ids: set[int],
        source_parent_container_ids: set[int],
    ) -> WarehouseOperation | None:
        if operation is None:
            return None
        completed_snapshot_ids = {
            int(snapshot_id)
            for snapshot_id in source_snapshot_ids
            if int(snapshot_id or 0) > 0
        }
        completed_container_ids = {
            int(container_id)
            for container_id in source_container_ids | source_parent_container_ids
            if int(container_id or 0) > 0
        }
        operation_tasks = list(
            WarehouseOperationTask.objects.select_for_update()
            .filter(operation=operation)
            .order_by("id")
        )
        matched_tasks: list[WarehouseOperationTask] = []
        for operation_task in operation_tasks:
            payload = operation_task.payload if isinstance(operation_task.payload, dict) else {}
            payload_snapshot_ids = {
                int(snapshot_id)
                for snapshot_id in (payload.get("snapshot_ids") or [])
                if str(snapshot_id or "").isdigit() and int(snapshot_id) > 0
            }
            if (
                int(operation_task.container_id or 0) in completed_container_ids
                or bool(payload_snapshot_ids & completed_snapshot_ids)
            ):
                matched_tasks.append(operation_task)
        if not matched_tasks:
            raise ValueError("OBR arrival does not match any task of the processing move operation.")
        if any(
            operation_task.status
            in {
                WarehouseOperationTask.STATUS_FAILED,
                WarehouseOperationTask.STATUS_CANCELED,
            }
            for operation_task in matched_tasks
        ):
            raise ValueError("OBR arrival points to a closed processing move task.")

        now = timezone.now()
        for operation_task in matched_tasks:
            if operation_task.status == WarehouseOperationTask.STATUS_DONE:
                continue
            operation_task.status = WarehouseOperationTask.STATUS_DONE
            operation_task.qty_done = int(operation_task.qty_planned or 0)
            operation_task.started_at = operation_task.started_at or now
            operation_task.completed_at = now
            operation_task.save(
                update_fields=[
                    "status",
                    "qty_done",
                    "started_at",
                    "completed_at",
                    "updated_at",
                ]
            )

        operation_tasks = list(
            WarehouseOperationTask.objects.select_for_update()
            .filter(operation=operation)
            .order_by("id")
        )
        done_qty = sum(
            int(operation_task.qty_done or 0)
            for operation_task in operation_tasks
            if operation_task.status == WarehouseOperationTask.STATUS_DONE
        )
        open_statuses = {
            WarehouseOperationTask.STATUS_CREATED,
            WarehouseOperationTask.STATUS_IN_PROGRESS,
        }
        has_open_tasks = any(
            operation_task.status in open_statuses
            for operation_task in operation_tasks
        )
        has_non_done_terminal_tasks = any(
            operation_task.status
            in {
                WarehouseOperationTask.STATUS_FAILED,
                WarehouseOperationTask.STATUS_CANCELED,
            }
            for operation_task in operation_tasks
        )
        all_tasks_done = bool(operation_tasks) and all(
            operation_task.status == WarehouseOperationTask.STATUS_DONE
            for operation_task in operation_tasks
        )

        operation.done_qty = done_qty
        operation.started_at = operation.started_at or now
        if all_tasks_done and done_qty == int(operation.planned_qty or 0):
            operation.status = WarehouseOperation.STATUS_DONE
            operation.completed_at = operation.completed_at or now
        elif done_qty > 0:
            operation.status = WarehouseOperation.STATUS_PARTIAL
            operation.completed_at = now if not has_open_tasks and has_non_done_terminal_tasks else None
        else:
            operation.status = WarehouseOperation.STATUS_IN_PROGRESS
            operation.completed_at = None
        operation.save(
            update_fields=[
                "status",
                "done_qty",
                "started_at",
                "completed_at",
                "updated_at",
            ]
        )
        return operation


    @classmethod
    def _mark_processing_reserves_arrived_to_obr(
        cls,
        *,
        snapshots: list[WarehouseStockSnapshot],
        order_id: str,
    ) -> None:
        """Satisfy only reserves bound to processing stock that actually reached OBR."""
        arrival_snapshots = [
            snapshot
            for snapshot in snapshots
            if snapshot is not None
            and min(
                max(int(snapshot.processing_reserved_qty or 0), 0),
                max(int(snapshot.qty or 0), 0),
            )
            > 0
        ]
        if not arrival_snapshots:
            return

        order_key = str(order_id or "").strip()
        active_statuses = [
            WarehouseReserve.STATUS_ACTIVE,
            WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
            WarehouseReserve.STATUS_ALLOCATED,
            WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
            WarehouseReserve.STATUS_SATISFIED,
        ]
        snapshots_by_agency: dict[int, list[WarehouseStockSnapshot]] = {}
        for snapshot in arrival_snapshots:
            snapshots_by_agency.setdefault(int(snapshot.agency_id), []).append(snapshot)

        for agency_id, agency_snapshots in snapshots_by_agency.items():
            reserves = list(
                WarehouseReserve.objects.select_for_update()
                .filter(
                    agency_id=agency_id,
                    reserve_type=WarehouseReserve.TYPE_PROCESSING,
                    context_type="processing",
                    context_id=order_key,
                    status__in=active_statuses,
                )
                .order_by("id")
            )
            if not reserves:
                continue

            reserve_snapshot_id_by_reserve = cls._reserve_snapshot_id_map(reserves)
            changed_reserves: dict[int, WarehouseReserve] = {}
            for snapshot in agency_snapshots:
                remaining_qty = min(
                    max(int(snapshot.processing_reserved_qty or 0), 0),
                    max(int(snapshot.qty or 0), 0),
                )
                if remaining_qty <= 0:
                    continue

                exact_reserves = [
                    reserve
                    for reserve in reserves
                    if cls._reserve_matches_snapshot(reserve, snapshot)
                ]
                matching_reserves = exact_reserves
                if (
                    not any(cls._reserve_open_qty(reserve) > 0 for reserve in exact_reserves)
                    and str(snapshot.barcode or "").strip()
                ):
                    snapshot_size = str(snapshot.size or "").strip()
                    snapshot_goods_type = str(snapshot.goods_type or "").strip()
                    snapshot_barcode = str(snapshot.barcode or "").strip()
                    matching_reserves = [
                        reserve
                        for reserve in reserves
                        if str(reserve.size or "").strip() == snapshot_size
                        and str(reserve.goods_type or "").strip() == snapshot_goods_type
                        and str(reserve.barcode or "").strip() == snapshot_barcode
                    ]

                snapshot_id = int(snapshot.id or 0)
                bound_reserves = [
                    reserve
                    for reserve in matching_reserves
                    if int(
                        reserve_snapshot_id_by_reserve.get(int(reserve.id or 0)) or 0
                    )
                    == snapshot_id
                ]
                if bound_reserves:
                    ordered_reserves = bound_reserves
                else:
                    # Legacy reserves can be missing the snapshot link. They are safe
                    # fallbacks, but a reserve linked to another box must stay untouched.
                    ordered_reserves = [
                        reserve
                        for reserve in matching_reserves
                        if int(
                            reserve_snapshot_id_by_reserve.get(int(reserve.id or 0)) or 0
                        )
                        == 0
                    ]

                for reserve in ordered_reserves:
                    if remaining_qty <= 0:
                        break
                    open_qty = cls._reserve_open_qty(reserve)
                    if open_qty <= 0:
                        continue
                    satisfied_qty = min(open_qty, remaining_qty)
                    remaining_qty -= satisfied_qty
                    reserve.qty_allocated = max(
                        int(reserve.qty_allocated or 0),
                        int(reserve.qty_satisfied or 0) + satisfied_qty,
                    )
                    reserve.qty_satisfied = min(
                        int(reserve.qty_reserved or 0),
                        int(reserve.qty_satisfied or 0) + satisfied_qty,
                    )
                    if int(reserve.qty_satisfied or 0) >= int(reserve.qty_reserved or 0):
                        reserve.status = WarehouseReserve.STATUS_SATISFIED
                    elif int(reserve.qty_satisfied or 0) > 0:
                        reserve.status = WarehouseReserve.STATUS_PARTIALLY_SATISFIED
                    elif int(reserve.qty_allocated or 0) >= int(reserve.qty_reserved or 0):
                        reserve.status = WarehouseReserve.STATUS_ALLOCATED
                    elif int(reserve.qty_allocated or 0) > 0:
                        reserve.status = WarehouseReserve.STATUS_PARTIALLY_ALLOCATED
                    else:
                        reserve.status = WarehouseReserve.STATUS_ACTIVE
                    reserve.updated_at = timezone.now()
                    changed_reserves[int(reserve.id)] = reserve

            if changed_reserves:
                WarehouseReserve.objects.bulk_update(
                    list(changed_reserves.values()),
                    ["qty_allocated", "qty_satisfied", "status", "updated_at"],
                )

    @classmethod
    @transaction.atomic
    def mark_processing_boxes_arrived_to_obr(
        cls,
        *,
        agency: Agency,
        order_id: str,
        box_codes: list[str],
        destination_row_no: int = 0,
        destination_section_no: int = 0,
        destination_tier_no: int = 0,
        destination_cell_no: int = 0,
        destination_location_code: str = "",
        allow_legacy_generic_destination: bool = False,
        performed_by=None,
        warehouse_code: str = "MSK",
        source_document_id: str = "",
        operation_id: int | None = None,
    ) -> int:
        order_key = str(order_id or "").strip()
        document_key = str(source_document_id or "").strip()
        normalized_codes: list[str] = []
        seen_codes: set[str] = set()
        for raw_code in box_codes or []:
            code = str(raw_code or "").strip()
            key = code.lower()
            if not code or key in seen_codes:
                continue
            seen_codes.add(key)
            normalized_codes.append(code)
        if not order_key or not normalized_codes:
            return 0
        operation = cls._lock_processing_move_operation_for_arrival(
            operation_id=operation_id,
            agency=agency,
            order_id=order_key,
        )
        if document_key:
            repeated_events = list(WarehouseEvent.objects.filter(
                agency=agency,
                event_type=WarehouseEventType.PROCESSING_ZONE_ARRIVED.value,
                stock_context_type="processing",
                stock_context_id=order_key,
                source_document_type="reachtruck_processing_task",
                source_document_id=document_key,
            ).order_by("id"))
            if repeated_events:
                cls._complete_processing_move_operation_for_arrival(
                    operation=operation,
                    source_snapshot_ids={
                        int((event.payload or {}).get("snapshot_id") or 0)
                        for event in repeated_events
                        if int((event.payload or {}).get("snapshot_id") or 0) > 0
                    },
                    source_container_ids={
                        int(event.container_id)
                        for event in repeated_events
                        if int(event.container_id or 0) > 0
                    },
                    source_parent_container_ids=set(),
                )
                return sum(int(event.qty or 0) for event in repeated_events)
        if operation is not None and operation.status == WarehouseOperation.STATUS_DONE:
            raise ValueError("Processing move operation is already completed by another task.")
        destination = cls.concrete_movement_destination(
            warehouse_code=warehouse_code,
            zone_code="OBR",
            location_code=destination_location_code,
            row_no=destination_row_no,
            section_no=destination_section_no,
            tier_no=destination_tier_no,
            cell_no=destination_cell_no,
            allow_legacy_generic_location=allow_legacy_generic_destination,
        )
        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("container", "parent_container", "location", "active_operation")
            .filter(
                agency=agency,
                is_archived=False,
            )
            .filter(
                Q(container_code__in=normalized_codes)
                | Q(container__container_code__in=normalized_codes)
            )
            .order_by("id")
        )
        cls._mark_processing_reserves_arrived_to_obr(
            snapshots=snapshots,
            order_id=order_key,
        )
        total_done = 0
        source_snapshot_ids: set[int] = set()
        source_container_ids: set[int] = set()
        source_parent_container_ids: set[int] = set()
        for snapshot in snapshots:
            moved_qty = int(snapshot.qty or 0)
            if moved_qty <= 0:
                continue
            if operation is not None and int(snapshot.active_operation_id or 0) != int(operation.id or 0):
                raise ValueError("Scanned box is not assigned to this processing move operation.")
            source_snapshot_ids.add(int(snapshot.id))
            current_context_type = str(snapshot.source_context_type or "").strip()
            current_context_id = str(snapshot.source_context_id or "").strip()
            current_state = str(snapshot.warehouse_state_code or "").strip()
            if current_context_type == "processing" and current_context_id:
                is_active_processing_input = (
                    str(snapshot.zone_code or "").strip().upper() == "OBR"
                    and current_state
                    in {
                        WarehouseStateCode.IN_PROCESSING_ZONE.value,
                        WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
                        WarehouseStateCode.PROCESSING_CONSUMED.value,
                    }
                )
                if current_context_id != order_key and is_active_processing_input:
                    raise ValueError(
                        f"Короб уже закреплён за другой заявкой на обработку №{current_context_id}."
                    )
                if current_context_id == order_key and is_active_processing_input:
                    raise ValueError(
                        "Короб уже подан в OBR другим заданием. Повторная подача запрещена."
                    )
            source_container = snapshot.container
            source_parent_container = snapshot.parent_container
            if source_container is not None and int(source_container.id or 0) > 0:
                source_container_ids.add(int(source_container.id))
            if source_parent_container is not None and int(source_parent_container.id or 0) > 0:
                source_parent_container_ids.add(int(source_parent_container.id))
            try:
                transition = WarehouseTransitionService.apply_event(
                    snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                    WarehouseEventType.PROCESSING_ZONE_ARRIVED,
                    zone_to=destination.zone_code,
                )
                next_state = transition.code.value
            except ValueError:
                next_state = WarehouseStateCode.IN_PROCESSING_ZONE.value
            event = WarehouseEvent.objects.create(
                agency=agency,
                event_type=WarehouseEventType.PROCESSING_ZONE_ARRIVED.value,
                stock_context_type="processing",
                stock_context_id=order_key,
                container=source_container,
                operation=operation,
                from_location=snapshot.location,
                to_location=destination,
                from_zone_code=snapshot.zone_code,
                to_zone_code=destination.zone_code,
                qty=moved_qty,
                source_document_type="reachtruck_processing_task" if document_key else "",
                source_document_id=document_key,
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by) or "reachtruck",
                occurred_at=timezone.now(),
                payload={
                    "source_box_code": str(getattr(source_container, "container_code", "") or snapshot.container_code or ""),
                    "source_pallet_code": str(getattr(source_parent_container, "container_code", "") or ""),
                    "fact_source": "reachtruck_scan",
                    "source_document_id": document_key,
                    "snapshot_id": int(snapshot.id or 0),
                },
            )
            snapshot.processing_reserved_qty = 0
            snapshot.location = destination
            snapshot.zone_code = destination.zone_code
            snapshot.zone_kind = destination.zone_kind
            snapshot.source_context_type = "processing"
            snapshot.source_context_id = order_key
            snapshot.warehouse_state_code = next_state
            snapshot.available_qty = 0
            snapshot.parent_container = None
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
            snapshot.last_event = event
            if source_container is not None:
                source_container.current_location = destination
                source_container.parent_container = None
                source_container.save(
                    update_fields=["current_location", "parent_container", "updated_at"]
                )
            snapshot.save(
                update_fields=[
                    "location",
                    "zone_code",
                    "zone_kind",
                    "source_context_type",
                    "source_context_id",
                    "warehouse_state_code",
                    "available_qty",
                    "processing_reserved_qty",
                    "parent_container",
                    "active_operation",
                    "active_operation_type",
                    "last_event",
                    "updated_at",
                ]
            )
            total_done += moved_qty
        cls._archive_empty_processing_containers(
            agency=agency,
            container_ids=source_container_ids,
            parent_container_ids=source_parent_container_ids,
        )
        cls._complete_processing_move_operation_for_arrival(
            operation=operation,
            source_snapshot_ids=source_snapshot_ids,
            source_container_ids=source_container_ids,
            source_parent_container_ids=source_parent_container_ids,
        )
        return total_done

    @classmethod
    @transaction.atomic
    def complete_partial_processing_pick_to_obr(
        cls,
        *,
        agency: Agency,
        order_id: str,
        source_pallet_code: str,
        picked_rows: list[dict],
        destination_row_no: int = 0,
        destination_section_no: int = 0,
        destination_tier_no: int = 0,
        destination_cell_no: int = 0,
        destination_location_code: str = "",
        allow_legacy_generic_destination: bool = False,
        performed_by=None,
        warehouse_code: str = "MSK",
        source_document_id: str = "",
        operation_id: int | None = None,
    ) -> list[int]:
        order_key = str(order_id or "").strip()
        document_key = str(source_document_id or "").strip()
        if not order_key:
            raise ValueError("order_id is required for partial processing pick")
        operation = cls._lock_processing_move_operation_for_arrival(
            operation_id=operation_id,
            agency=agency,
            order_id=order_key,
        )
        if document_key:
            repeated_events = list(
                WarehouseEvent.objects.filter(
                    agency=agency,
                    event_type=WarehouseEventType.PROCESSING_ZONE_ARRIVED.value,
                    stock_context_type="processing",
                    stock_context_id=order_key,
                    source_document_type="reachtruck_processing_task",
                    source_document_id=document_key,
                ).order_by("id")
            )
            if repeated_events:
                repeated_snapshot_ids = [
                    int((event.payload or {}).get("created_snapshot_id") or 0)
                    for event in repeated_events
                    if int((event.payload or {}).get("created_snapshot_id") or 0) > 0
                ]
                if not repeated_snapshot_ids:
                    repeated_snapshot_ids = list(
                        WarehouseStockSnapshot.objects.filter(
                            last_event_id__in=[event.id for event in repeated_events]
                        ).values_list("id", flat=True)
                    )
                if repeated_snapshot_ids:
                    cls._complete_processing_move_operation_for_arrival(
                        operation=operation,
                        source_snapshot_ids={
                            int((event.payload or {}).get("source_snapshot_id") or 0)
                            for event in repeated_events
                            if int((event.payload or {}).get("source_snapshot_id") or 0) > 0
                        },
                        source_container_ids={
                            int(event.container_id)
                            for event in repeated_events
                            if int(event.container_id or 0) > 0
                        },
                        source_parent_container_ids=set(),
                    )
                    return repeated_snapshot_ids
                raise ValueError("Повторное задание уже выполнено, но его складской факт повреждён.")
        if operation is not None and operation.status == WarehouseOperation.STATUS_DONE:
            raise ValueError("Processing move operation is already completed by another task.")
        destination = cls.concrete_movement_destination(
            warehouse_code=warehouse_code,
            zone_code="OBR",
            location_code=destination_location_code,
            row_no=destination_row_no,
            section_no=destination_section_no,
            tier_no=destination_tier_no,
            cell_no=destination_cell_no,
            allow_legacy_generic_location=allow_legacy_generic_destination,
        )
        cls.replace_processing_reserves(
            agency=agency,
            order_id=order_key,
            items=[],
            created_by=performed_by,
        )
        created_snapshot_ids: list[int] = []
        pallet_code = str(source_pallet_code or "").strip()
        source_snapshot_ids: set[int] = set()
        source_container_ids: set[int] = set()
        source_parent_container_ids: set[int] = set()

        for row in picked_rows or []:
            if not isinstance(row, dict):
                continue
            box_code = str(row.get("box_code") or "").strip()
            row_qty = max(int(row.get("picked_qty") or row.get("qty") or 0), 0)
            if row_qty <= 0:
                continue
            barcode_qty = {
                str(barcode or "").strip(): max(int(qty or 0), 0)
                for barcode, qty in dict(row.get("barcode_qty") or {}).items()
                if str(barcode or "").strip() and max(int(qty or 0), 0) > 0
            }
            if not barcode_qty:
                row_barcodes = [
                    str(value or "").strip()
                    for value in (row.get("barcodes") or row.get("requested_barcodes") or [])
                    if str(value or "").strip()
                ]
                if len(row_barcodes) == 1:
                    barcode_qty[row_barcodes[0]] = row_qty
            row_sku = str(row.get("sku_code") or row.get("requested_sku") or "").strip()
            pick_parts: list[tuple[str, int]] = list(barcode_qty.items())
            if not pick_parts:
                pick_parts = [("", row_qty)]
            for barcode, qty_to_pick in pick_parts:
                remaining = int(qty_to_pick)
                qs = (
                    WarehouseStockSnapshot.objects.select_for_update(of=("self",))
                    .select_related("container", "parent_container", "location", "active_operation")
                    .filter(
                        agency=agency,
                        is_archived=False,
                        warehouse_state_code__in=[
                            WarehouseStateCode.STORED.value,
                            WarehouseStateCode.PLACED_AFTER_PROCESSING.value,
                            WarehouseStateCode.RESERVED_FOR_PROCESSING.value,
                            WarehouseStateCode.MOVING_TO_PROCESSING.value,
                        ],
                    )
                    .order_by("-processing_reserved_qty", "id")
                )
                if barcode:
                    qs = qs.filter(barcode=barcode)
                elif row_sku:
                    qs = qs.filter(sku_code=row_sku)
                else:
                    raise ValueError("barcode or sku_code is required for partial processing pick")
                if box_code:
                    qs = qs.filter(
                        Q(container_code__iexact=box_code)
                        | Q(container__container_code__iexact=box_code)
                    )
                if pallet_code:
                    qs = qs.filter(
                        Q(parent_container__container_code__iexact=pallet_code)
                        | Q(container__parent_container__container_code__iexact=pallet_code)
                        | Q(container_code__iexact=box_code)
                    )
                snapshots = list(qs)
                if not snapshots:
                    raise ValueError("No source snapshot found for partial processing pick.")
                for snapshot in snapshots:
                    if remaining <= 0:
                        break
                    source_available_qty = int(snapshot.available_qty or 0)
                    if source_available_qty <= 0:
                        continue
                    take_qty = min(remaining, source_available_qty, int(snapshot.qty or 0))
                    if take_qty <= 0:
                        continue
                    if operation is not None and int(snapshot.active_operation_id or 0) != int(operation.id or 0):
                        raise ValueError("Picked stock is not assigned to this processing move operation.")
                    source_snapshot_ids.add(int(snapshot.id))
                    source_location = snapshot.location
                    source_container = snapshot.container
                    source_parent_container = snapshot.parent_container
                    if source_container is not None and int(source_container.id or 0) > 0:
                        source_container_ids.add(int(source_container.id))
                    if source_parent_container is not None and int(source_parent_container.id or 0) > 0:
                        source_parent_container_ids.add(int(source_parent_container.id))
                    source_event = WarehouseEvent.objects.create(
                        agency=agency,
                        event_type=WarehouseEventType.MOVEMENT_COMPLETED.value,
                        stock_context_type="processing",
                        stock_context_id=order_key,
                        container=source_container,
                        operation=operation,
                        from_location=source_location,
                        to_location=source_location,
                        from_zone_code=snapshot.zone_code,
                        to_zone_code=snapshot.zone_code,
                        qty=take_qty,
                        source_document_type="reachtruck_processing_task" if document_key else "",
                        source_document_id=document_key,
                        performed_by=performed_by,
                        performed_by_role=cls._role_of(performed_by) or "reachtruck",
                        occurred_at=timezone.now(),
                        payload={
                            "partial_processing_pick": True,
                            "source_box_code": box_code or str(getattr(source_container, "container_code", "") or ""),
                            "source_pallet_code": pallet_code,
                            "source_document_id": document_key,
                        },
                    )
                    loose_snapshot = WarehouseStockSnapshot.objects.create(
                        agency=agency,
                        stock_unit_type=snapshot.stock_unit_type or "item",
                        source_context_type="processing",
                        source_context_id=order_key,
                        sku_ref=snapshot.sku_ref,
                        sku_code=snapshot.sku_code,
                        name=snapshot.name,
                        size=snapshot.size,
                        barcode=snapshot.barcode,
                        goods_type=snapshot.goods_type,
                        marking_code=snapshot.marking_code,
                        qty=take_qty,
                        available_qty=0,
                        processing_reserved_qty=0,
                        shipping_reserved_qty=0,
                        other_reserved_qty=0,
                        container=None,
                        container_code="",
                        parent_container=None,
                        location=destination,
                        zone_code=destination.zone_code,
                        zone_kind=destination.zone_kind,
                        warehouse_state_code=WarehouseStateCode.IN_PROCESSING_ZONE.value,
                    )
                    arrived_event = WarehouseEvent.objects.create(
                        agency=agency,
                        event_type=WarehouseEventType.PROCESSING_ZONE_ARRIVED.value,
                        stock_context_type="processing",
                        stock_context_id=order_key,
                        container=None,
                        operation=operation,
                        from_location=source_location,
                        to_location=destination,
                        from_zone_code=str(getattr(source_location, "zone_code", "") or ""),
                        to_zone_code=destination.zone_code,
                        qty=take_qty,
                        source_document_type="reachtruck_processing_task" if document_key else "",
                        source_document_id=document_key,
                        performed_by=performed_by,
                        performed_by_role=cls._role_of(performed_by) or "reachtruck",
                        occurred_at=timezone.now(),
                        payload={
                            "partial_processing_pick": True,
                            "source_snapshot_id": snapshot.id,
                            "source_box_code": box_code or str(getattr(source_container, "container_code", "") or ""),
                            "source_pallet_code": pallet_code,
                            "fact_source": "reachtruck_scan",
                            "source_document_id": document_key,
                            "created_snapshot_id": int(loose_snapshot.id or 0),
                        },
                    )
                    loose_snapshot.warehouse_state_code = WarehouseStateCode.IN_PROCESSING_ZONE.value
                    loose_snapshot.active_operation = None
                    loose_snapshot.active_operation_type = ""
                    loose_snapshot.last_event = arrived_event
                    loose_snapshot.save(
                        update_fields=[
                            "warehouse_state_code",
                            "active_operation",
                            "active_operation_type",
                            "last_event",
                            "updated_at",
                        ]
                    )
                    snapshot.refresh_from_db(
                        fields=[
                            "processing_reserved_qty",
                            "available_qty",
                            "active_operation",
                            "active_operation_type",
                        ]
                    )
                    snapshot.qty = max(int(snapshot.qty or 0) - take_qty, 0)
                    snapshot.available_qty = max(
                        int(snapshot.qty or 0)
                        - int(snapshot.processing_reserved_qty or 0)
                        - int(snapshot.shipping_reserved_qty or 0)
                        - int(snapshot.other_reserved_qty or 0),
                        0,
                    )
                    if snapshot.qty <= 0:
                        snapshot.available_qty = 0
                        snapshot.is_archived = True
                    if int(snapshot.processing_reserved_qty or 0) <= 0:
                        snapshot.warehouse_state_code = (
                            cls._state_for_location(source_location)
                            if source_location is not None
                            else WarehouseStateCode.STORED.value
                        )
                        if (
                            snapshot.active_operation is not None
                            and snapshot.active_operation.operation_type == WarehouseOperation.TYPE_MOVE_TO_PROCESSING
                        ):
                            snapshot.active_operation = None
                            snapshot.active_operation_type = ""
                    snapshot.last_event = source_event
                    snapshot.save(
                        update_fields=[
                            "qty",
                            "available_qty",
                            "warehouse_state_code",
                            "is_archived",
                            "active_operation",
                            "active_operation_type",
                            "last_event",
                            "updated_at",
                        ]
                    )
                    created_snapshot_ids.append(int(loose_snapshot.id))
                    remaining -= take_qty
                if remaining > 0:
                    raise ValueError("Not enough source qty for partial processing pick.")
        cls._archive_empty_processing_containers(
            agency=agency,
            container_ids=source_container_ids,
            parent_container_ids=source_parent_container_ids,
        )
        if not created_snapshot_ids:
            raise ValueError("No OBR snapshots created for partial processing pick.")
        cls._complete_processing_move_operation_for_arrival(
            operation=operation,
            source_snapshot_ids=source_snapshot_ids,
            source_container_ids=source_container_ids,
            source_parent_container_ids=source_parent_container_ids,
        )
        return created_snapshot_ids

    @classmethod
    @transaction.atomic
    def start_processing(
        cls,
        *,
        agency: Agency,
        order_id: str,
        started_by=None,
        started_by_role: str = "processor",
    ) -> WarehouseOperation:
        order_key = str(order_id or "").strip()
        cls.replace_processing_reserves(
            agency=agency,
            order_id=order_key,
            items=[],
            created_by=started_by,
        )
        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",)).filter(
                agency=agency,
                source_context_type="processing",
                source_context_id=order_key,
                zone_code="OBR",
                warehouse_state_code=WarehouseStateCode.IN_PROCESSING_ZONE.value,
                is_archived=False,
            ).order_by("id")
        )
        if not snapshots:
            raise ValueError("No snapshots in processing zone ready to start processing")

        operation = WarehouseOperation.objects.create(
            agency=agency,
            operation_type=WarehouseOperation.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_key,
            source_location=snapshots[0].location,
            destination_location=snapshots[0].location,
            source_zone_code=snapshots[0].zone_code,
            destination_zone_code=snapshots[0].zone_code,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
            requested_by=started_by,
            requested_by_role=started_by_role,
            assigned_executor_role="processor",
            planned_qty=sum(int(snapshot.qty or 0) for snapshot in snapshots),
            started_at=timezone.now(),
        )
        now = timezone.now()
        for snapshot in snapshots:
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.PROCESSING_STARTED,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.PROCESSING_STARTED.value,
                stock_context_type="processing",
                stock_context_id=order_key,
                container=snapshot.container,
                operation=operation,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code=snapshot.zone_code,
                qty=int(snapshot.qty or 0),
                performed_by=started_by,
                performed_by_role=started_by_role or cls._role_of(started_by),
                occurred_at=now,
            )
            snapshot.warehouse_state_code = transition.code.value
            snapshot.active_operation = operation
            snapshot.active_operation_type = operation.operation_type
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "warehouse_state_code",
                    "active_operation",
                    "active_operation_type",
                    "last_event",
                    "updated_at",
                ]
            )
        return operation

    @classmethod
    @transaction.atomic
    def complete_processing(
        cls,
        *,
        operation: WarehouseOperation,
        performed_by=None,
        performed_by_role: str = "processor",
    ) -> WarehouseOperation:
        if operation.operation_type != WarehouseOperation.TYPE_PROCESSING:
            raise ValueError("Only processing operations can be completed by this write-path")
        from billing.warehouse_services import require_completion_facts

        require_completion_facts(
            client=operation.agency,
            order_type="processing",
            order_id=str(operation.context_id or "").strip(),
        )
        return cls.consume_processing_input(
            agency=operation.agency,
            order_id=str(operation.context_id or "").strip(),
            performed_by=performed_by,
            performed_by_role=performed_by_role,
            operation=operation,
        )

    @classmethod
    @transaction.atomic
    def validate_processing_input_output_balance(
        cls,
        *,
        agency: Agency,
        order_id: str,
        expected_output_qty: int,
        allow_obr_remainder: bool = False,
        allow_output_surplus: bool = False,
    ) -> tuple[list[int], int]:
        order_key = str(order_id or "").strip()
        expected_qty = max(int(expected_output_qty or 0), 0)
        if not agency or not order_key:
            raise ValueError("Не указана заявка для проверки складского факта обработки.")
        if expected_qty <= 0:
            raise ValueError("Количество обработанного товара должно быть больше нуля.")
        input_snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .filter(
                agency=agency,
                source_context_type="processing",
                source_context_id=order_key,
                zone_code="OBR",
                warehouse_state_code__in=[
                    WarehouseStateCode.IN_PROCESSING_ZONE.value,
                    WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
                ],
                is_archived=False,
                qty__gt=0,
            )
            .order_by("id")
        )
        input_qty = sum(int(snapshot.qty or 0) for snapshot in input_snapshots)
        if input_qty <= 0:
            raise ValueError(
                "Нет фактически отсканированного товара, поданного ричтракером в OBR."
            )
        if input_qty < expected_qty and not allow_output_surplus:
            raise ValueError(
                "Складской факт обработки не совпадает: "
                f"в OBR подано {input_qty}, обработано и размещено {expected_qty}."
            )
        if input_qty > expected_qty and not allow_obr_remainder:
            raise ValueError(
                "Складской факт обработки не совпадает: "
                f"в OBR подано {input_qty}, обработано и размещено {expected_qty}."
            )
        return [int(snapshot.id) for snapshot in input_snapshots], input_qty

    @classmethod
    @transaction.atomic
    def consume_processing_input(
        cls,
        *,
        agency: Agency,
        order_id: str,
        performed_by=None,
        performed_by_role: str = "processor",
        operation: WarehouseOperation | None = None,
        expected_output_qty: int | None = None,
        allow_obr_remainder: bool = False,
        allow_output_surplus: bool = False,
    ) -> WarehouseOperation | None:
        order_key = str(order_id or "").strip()
        if not agency or not order_key:
            return operation
        if operation is None:
            operation = (
                WarehouseOperation.objects.select_for_update()
                .filter(
                    agency=agency,
                    operation_type=WarehouseOperation.TYPE_PROCESSING,
                    context_type="processing",
                    context_id=order_key,
                    status__in=[
                        WarehouseOperation.STATUS_CREATED,
                        WarehouseOperation.STATUS_PLANNED,
                        WarehouseOperation.STATUS_IN_PROGRESS,
                        WarehouseOperation.STATUS_PARTIAL,
                        WarehouseOperation.STATUS_DONE,
                    ],
                )
                .order_by("-id")
                .first()
            )
        if operation is not None and operation.status == WarehouseOperation.STATUS_DONE:
            return operation
        validated_snapshot_ids: list[int] | None = None
        validated_input_qty: int | None = None
        if expected_output_qty is not None:
            validated_snapshot_ids, validated_input_qty = cls.validate_processing_input_output_balance(
                agency=agency,
                order_id=order_key,
                expected_output_qty=expected_output_qty,
                allow_obr_remainder=allow_obr_remainder,
                allow_output_surplus=allow_output_surplus,
            )
        cls.replace_processing_reserves(
            agency=agency,
            order_id=order_key,
            items=[],
            created_by=performed_by,
        )
        processing_snapshots_qs = (
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .filter(
                agency=agency,
                source_context_type="processing",
                source_context_id=order_key,
                zone_code="OBR",
                warehouse_state_code__in=[
                    WarehouseStateCode.IN_PROCESSING_ZONE.value,
                    WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
                ],
                is_archived=False,
            )
            .select_related("container", "parent_container", "location")
            .order_by("id")
        )
        if validated_snapshot_ids is not None:
            processing_snapshots_qs = processing_snapshots_qs.filter(id__in=validated_snapshot_ids)
        processing_snapshots = list(processing_snapshots_qs)
        if not processing_snapshots:
            if operation is None:
                return None
            raise ValueError("No scanned processing input found in OBR.")
        if operation is None:
            operation = WarehouseOperation.objects.create(
                agency=agency,
                operation_type=WarehouseOperation.TYPE_PROCESSING,
                context_type="processing",
                context_id=order_key,
                status=WarehouseOperation.STATUS_IN_PROGRESS,
                requested_by=performed_by,
                requested_by_role=performed_by_role or cls._role_of(performed_by),
                assigned_executor_role="processor",
                planned_qty=sum(int(snapshot.qty or 0) for snapshot in processing_snapshots),
                started_at=timezone.now(),
            )
        if expected_output_qty is not None:
            target_qty = max(int(expected_output_qty or 0), 0)
            if allow_output_surplus and validated_input_qty is not None:
                target_qty = min(target_qty, max(int(validated_input_qty or 0), 0))
        else:
            target_qty = sum(int(snapshot.qty or 0) for snapshot in processing_snapshots)
        remaining_to_consume = target_qty
        consumed_snapshot_ids: set[int] = set()
        total_done = 0

        def detach_consumed_box_if_empty(snapshot: WarehouseStockSnapshot) -> None:
            container = snapshot.container
            if container is None or str(container.container_type or "").strip() != WarehouseContainer.TYPE_BOX:
                return
            has_live_snapshots = (
                WarehouseStockSnapshot.objects.filter(container=container, is_archived=False, qty__gt=0)
                .exclude(warehouse_state_code=WarehouseStateCode.PROCESSING_CONSUMED.value)
                .exists()
            )
            if has_live_snapshots:
                return
            locked_container = WarehouseContainer.objects.select_for_update().filter(pk=container.pk).first()
            if locked_container is None:
                return
            container_updates: list[str] = []
            if locked_container.parent_container_id is not None:
                locked_container.parent_container = None
                container_updates.append("parent_container")
            if locked_container.current_location_id is not None:
                locked_container.current_location = None
                container_updates.append("current_location")
            if locked_container.status != WarehouseContainer.STATUS_ARCHIVED:
                locked_container.status = WarehouseContainer.STATUS_ARCHIVED
                container_updates.append("status")
            if container_updates:
                locked_container.save(update_fields=container_updates + ["updated_at"])

        def consume_snapshot(snapshot: WarehouseStockSnapshot, requested_qty: int) -> int:
            nonlocal total_done
            snapshot_id = int(snapshot.id or 0)
            if snapshot_id <= 0 or snapshot_id in consumed_snapshot_ids or requested_qty <= 0:
                return 0
            if str(snapshot.warehouse_state_code or "").strip() == WarehouseStateCode.PROCESSING_CONSUMED.value:
                source_pallet_id = snapshot.parent_container_id
                if snapshot.parent_container_id is not None:
                    snapshot.parent_container = None
                    snapshot.save(update_fields=["parent_container", "updated_at"])
                consumed_snapshot_ids.add(snapshot_id)
                detach_consumed_box_if_empty(snapshot)
                cls._archive_empty_source_pallets({source_pallet_id})
                return 0
            snapshot_qty = int(snapshot.qty or 0)
            if snapshot_qty <= 0:
                return 0
            qty_done = min(snapshot_qty, max(int(requested_qty or 0), 0))
            if qty_done <= 0:
                return 0
            if int(snapshot.shipping_reserved_qty or 0) > 0 or int(snapshot.other_reserved_qty or 0) > 0:
                raise ValueError("Processing input has a foreign warehouse reserve.")
            is_fully_consumed = qty_done == snapshot_qty
            source_pallet_id = snapshot.parent_container_id if is_fully_consumed else None
            next_state = str(snapshot.warehouse_state_code or "").strip()
            if is_fully_consumed:
                try:
                    transition = WarehouseTransitionService.apply_event(
                        snapshot.warehouse_state_code or WarehouseStateCode.STORED.value,
                        WarehouseEventType.PROCESSING_CONSUMED,
                    )
                    next_state = transition.code.value
                except ValueError:
                    next_state = WarehouseStateCode.PROCESSING_CONSUMED.value
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.PROCESSING_CONSUMED.value,
                stock_context_type="processing",
                stock_context_id=order_key,
                container=snapshot.container,
                operation=operation,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code=snapshot.zone_code,
                qty=qty_done,
                performed_by=performed_by,
                performed_by_role=performed_by_role or cls._role_of(performed_by),
                occurred_at=timezone.now(),
                payload={
                    "snapshot_id": snapshot.id,
                    "source_context_type": snapshot.source_context_type,
                    "source_context_id": snapshot.source_context_id,
                    "reason": "processing_input_consumed",
                    "partial": not is_fully_consumed,
                    "remainder_qty": snapshot_qty - qty_done,
                },
            )
            snapshot.available_qty = 0
            snapshot.processing_reserved_qty = 0
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
            snapshot.last_event = event
            update_fields = [
                "available_qty",
                "processing_reserved_qty",
                "active_operation",
                "active_operation_type",
                "last_event",
            ]
            if is_fully_consumed:
                snapshot.warehouse_state_code = next_state
                update_fields.append("warehouse_state_code")
                if snapshot.parent_container_id is not None:
                    snapshot.parent_container = None
                    update_fields.append("parent_container")
            else:
                snapshot.qty = snapshot_qty - qty_done
                update_fields.append("qty")
            snapshot.save(update_fields=update_fields + ["updated_at"])
            if is_fully_consumed:
                detach_consumed_box_if_empty(snapshot)
                cls._archive_empty_source_pallets({source_pallet_id})
            consumed_snapshot_ids.add(snapshot_id)
            total_done += qty_done
            return qty_done

        for snapshot in processing_snapshots:
            if remaining_to_consume <= 0:
                break
            consumed_qty = consume_snapshot(snapshot, remaining_to_consume)
            remaining_to_consume -= consumed_qty

        if remaining_to_consume > 0:
            raise ValueError(
                "Складской факт обработки изменился во время списания: "
                f"не хватает {remaining_to_consume} шт."
            )

        operation.status = WarehouseOperation.STATUS_DONE
        operation.done_qty = total_done
        if not operation.started_at:
            operation.started_at = timezone.now()
        operation.completed_at = timezone.now()
        operation.save(update_fields=["status", "done_qty", "started_at", "completed_at", "updated_at"])
        return operation

    @classmethod
    @transaction.atomic
    def start_processing_if_ready(
        cls,
        *,
        agency: Agency,
        order_id: str,
        started_by=None,
        started_by_role: str = "processor",
    ) -> WarehouseOperation | None:
        order_key = str(order_id or "").strip()
        if not order_key:
            return None
        existing = (
            WarehouseOperation.objects.filter(
                agency=agency,
                operation_type=WarehouseOperation.TYPE_PROCESSING,
                context_type="processing",
                context_id=order_key,
                status__in=[
                    WarehouseOperation.STATUS_CREATED,
                    WarehouseOperation.STATUS_PLANNED,
                    WarehouseOperation.STATUS_IN_PROGRESS,
                    WarehouseOperation.STATUS_PARTIAL,
                    WarehouseOperation.STATUS_DONE,
                ],
            )
            .order_by("-id")
            .first()
        )
        if existing:
            return existing
        try:
            return cls.start_processing(
                agency=agency,
                order_id=order_key,
                started_by=started_by,
                started_by_role=started_by_role,
            )
        except ValueError:
            return None

    @classmethod
    @transaction.atomic
    def complete_processing_if_started(
        cls,
        *,
        agency: Agency,
        order_id: str,
        performed_by=None,
        performed_by_role: str = "processor",
    ) -> WarehouseOperation | None:
        order_key = str(order_id or "").strip()
        if not order_key:
            return None
        from billing.warehouse_services import require_completion_facts

        # Финальное закрытие обработки доступно только после фиксации услуг.
        # Проверка стоит до изменения операции, чтобы прямой вызов write-path
        # не мог обойти экран завершения обработки.
        require_completion_facts(
            client=agency,
            order_type="processing",
            order_id=order_key,
        )
        operation = (
            WarehouseOperation.objects.filter(
                agency=agency,
                operation_type=WarehouseOperation.TYPE_PROCESSING,
                context_type="processing",
                context_id=order_key,
                status__in=[
                    WarehouseOperation.STATUS_IN_PROGRESS,
                    WarehouseOperation.STATUS_PARTIAL,
                ],
            )
            .order_by("-id")
            .first()
        )
        if operation is None:
            operation = cls.start_processing_if_ready(
                agency=agency,
                order_id=order_key,
                started_by=performed_by,
                started_by_role=performed_by_role,
            )
        if operation is None or operation.status == WarehouseOperation.STATUS_DONE:
            return operation
        try:
            return cls.complete_processing(
                operation=operation,
                performed_by=performed_by,
                performed_by_role=performed_by_role,
            )
        except ValueError:
            return None

    @classmethod
    @transaction.atomic
    def replace_shipping_reserves(
        cls,
        *,
        agency: Agency,
        order_id: str,
        items: list[dict],
        created_by=None,
        source_document_type: str = "shipping_order",
        source_document_id: str = "",
    ) -> list[WarehouseReserve]:
        order_key = str(order_id or "").strip()
        if not order_key:
            return []
        active_reserves = list(
            WarehouseReserve.objects.filter(
                agency=agency,
                reserve_type=WarehouseReserve.TYPE_SHIPPING,
                context_type="shipping",
                context_id=order_key,
                status__in=[
                    WarehouseReserve.STATUS_ACTIVE,
                    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
                    WarehouseReserve.STATUS_ALLOCATED,
                    WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
                    WarehouseReserve.STATUS_SATISFIED,
                ],
            ).order_by("id")
        )
        cls._release_reserves_from_snapshots(
            reserves=active_reserves,
            reserved_qty_field="shipping_reserved_qty",
            release_event_type=WarehouseEventType.SHIPPING_RESERVE_RELEASED,
            performed_by=created_by,
            release_bound_qty=True,
        )
        if active_reserves:
            WarehouseReserve.objects.filter(id__in=[reserve.id for reserve in active_reserves]).update(
                status=WarehouseReserve.STATUS_RELEASED,
                released_by=created_by if getattr(created_by, "is_authenticated", False) else None,
                updated_at=timezone.now(),
            )
        if not items:
            return []
        return cls.reserve_for_shipping(
            agency=agency,
            order_id=order_key,
            items=items,
            created_by=created_by,
            source_document_type=source_document_type,
            source_document_id=source_document_id,
        )

    @classmethod
    @transaction.atomic
    def reserve_for_shipping(
        cls,
        *,
        agency: Agency,
        order_id: str,
        items: list[dict],
        created_by=None,
        source_document_type: str = "shipping_order",
        source_document_id: str = "",
    ) -> list[WarehouseReserve]:
        from .fbs_quantity_reserves import protect_new_claims
        items = list(items)
        protect_new_claims(agency, items)
        order_key = str(order_id or "").strip()
        if not order_key:
            raise ValueError("order_id is required for shipping reserve")
        source_document_id = str(source_document_id or order_key).strip()
        reserves: list[WarehouseReserve] = []
        for item in items:
            qty = max(int(item.get("qty") or 0), 0)
            if qty <= 0:
                continue
            if bool(item.get("reserve_pool")):
                size = cls._normalize_reserve_lookup_text(item.get("size"))
                barcode = cls._normalize_reserve_lookup_text(item.get("barcode"))
                sku_code = str(item.get("sku_code") or item.get("sku") or barcode or "").strip()
                if not sku_code:
                    raise ValueError("sku_code or barcode is required for shipping reserve")
                goods_type = cls._normalize_reserve_lookup_text(item.get("goods_type"))
                sku_ref = None
                if barcode:
                    sku_ref = SKU.objects.filter(agency=agency, code=barcode, deleted=False).order_by("id").first()
                if sku_ref is None:
                    sku_ref = (
                        SKU.objects.filter(agency=agency, sku_code=sku_code, deleted=False)
                        .order_by("id")
                        .first()
                    )
                reserve = WarehouseReserve.objects.create(
                    agency=agency,
                    reserve_type=WarehouseReserve.TYPE_SHIPPING,
                    context_type="shipping",
                    context_id=order_key,
                    sku_ref=sku_ref,
                    sku_code=sku_code,
                    size=size,
                    barcode=barcode,
                    goods_type=goods_type,
                    marking_code="",
                    qty_reserved=qty,
                    status=WarehouseReserve.STATUS_ACTIVE,
                    source_document_type=source_document_type,
                    source_document_id=source_document_id,
                    created_by=created_by,
                )
                WarehouseEvent.objects.create(
                    agency=agency,
                    event_type=WarehouseEventType.SHIPPING_RESERVED.value,
                    stock_context_type="shipping",
                    stock_context_id=order_key,
                    reserve=reserve,
                    source_document_type=source_document_type,
                    source_document_id=source_document_id,
                    qty=qty,
                    performed_by=created_by,
                    performed_by_role=cls._role_of(created_by),
                    occurred_at=timezone.now(),
                    payload={
                        "reserve_scope": "pool",
                        "box_codes": cls._normalize_shipping_box_codes(item),
                    },
                )
                reserves.append(reserve)
                continue
            allocations = cls._match_snapshots_for_shipping_reserve(
                agency=agency,
                item=item,
                required_qty=qty,
            )
            for snapshot, reserved_qty in allocations:
                transition = WarehouseTransitionService.apply_event(
                    snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                    WarehouseEventType.SHIPPING_RESERVED,
                )
                reserve = WarehouseReserve.objects.create(
                    agency=agency,
                    reserve_type=WarehouseReserve.TYPE_SHIPPING,
                    context_type="shipping",
                    context_id=order_key,
                    sku_ref=snapshot.sku_ref,
                    sku_code=snapshot.sku_code,
                    size=snapshot.size,
                    barcode=snapshot.barcode,
                    goods_type=snapshot.goods_type,
                    marking_code=snapshot.marking_code,
                    qty_reserved=reserved_qty,
                    status=WarehouseReserve.STATUS_ACTIVE,
                    source_document_type=source_document_type,
                    source_document_id=source_document_id,
                    created_by=created_by,
                )
                event = WarehouseEvent.objects.create(
                    agency=agency,
                    event_type=WarehouseEventType.SHIPPING_RESERVED.value,
                    stock_context_type="shipping",
                    stock_context_id=order_key,
                    container=snapshot.container,
                    reserve=reserve,
                    source_document_type=source_document_type,
                    source_document_id=source_document_id,
                    from_location=snapshot.location,
                    to_location=snapshot.location,
                    from_zone_code=snapshot.zone_code,
                    to_zone_code=snapshot.zone_code,
                    qty=reserved_qty,
                    performed_by=created_by,
                    performed_by_role=cls._role_of(created_by),
                    occurred_at=timezone.now(),
                    payload={
                        "snapshot_id": snapshot.id,
                        "box_code": str(snapshot.container_code or ""),
                    },
                )
                snapshot.shipping_reserved_qty += reserved_qty
                snapshot.available_qty -= reserved_qty
                snapshot.warehouse_state_code = transition.code.value
                snapshot.last_event = event
                snapshot.save(
                    update_fields=[
                        "shipping_reserved_qty",
                        "available_qty",
                        "warehouse_state_code",
                        "last_event",
                        "updated_at",
                    ]
                )
                reserves.append(reserve)
        return reserves

    @classmethod
    @transaction.atomic
    def rebind_shipping_reserve_boxes(
        cls,
        *,
        agency: Agency,
        order_id: str,
        reserved_box_codes: list[str],
        actual_box_codes: list[str],
        performed_by=None,
        source_document_id: str = "",
    ) -> int:
        order_key = str(order_id or "").strip()
        if not order_key:
            raise ValueError("order_id is required for shipping reserve rebinding")

        def _normalize_codes(raw_codes: list[str]) -> list[str]:
            normalized: list[str] = []
            seen: set[str] = set()
            for raw_code in raw_codes or []:
                code = str(raw_code or "").strip()
                key = code.lower()
                if not code or key in seen:
                    continue
                seen.add(key)
                normalized.append(code)
            return normalized

        reserved_codes = _normalize_codes(reserved_box_codes)
        actual_codes = _normalize_codes(actual_box_codes)
        if not reserved_codes or not actual_codes:
            return 0

        active_reserves = list(
            WarehouseReserve.objects.filter(
                agency=agency,
                reserve_type=WarehouseReserve.TYPE_SHIPPING,
                context_type="shipping",
                context_id=order_key,
                status__in=[
                    WarehouseReserve.STATUS_ACTIVE,
                    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
                    WarehouseReserve.STATUS_ALLOCATED,
                    WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
                    WarehouseReserve.STATUS_SATISFIED,
                ],
            ).order_by("id")
        )
        if not active_reserves:
            return 0

        snapshot_id_by_reserve = cls._reserve_snapshot_id_map(active_reserves)
        reserve_payload_by_id: dict[int, dict] = {}
        for row in (
            WarehouseEvent.objects.filter(
                reserve_id__in=[int(reserve.id or 0) for reserve in active_reserves if int(reserve.id or 0) > 0],
                event_type=WarehouseEventType.SHIPPING_RESERVED.value,
            )
            .exclude(payload__isnull=True)
            .values("reserve_id", "payload")
            .order_by("id")
        ):
            reserve_id = int(row.get("reserve_id") or 0)
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if reserve_id > 0:
                reserve_payload_by_id[reserve_id] = payload
        snapshot_ids = {
            int(snapshot_id)
            for snapshot_id in snapshot_id_by_reserve.values()
            if int(snapshot_id or 0) > 0
        }
        reserved_snapshots_by_id = {
            int(snapshot.id): snapshot
            for snapshot in WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("container", "location")
            .filter(id__in=snapshot_ids, agency=agency, is_archived=False)
        }
        actual_snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("container", "location")
            .filter(
                agency=agency,
                container_code__in=actual_codes,
                is_archived=False,
            )
            .order_by("id")
        )
        actual_by_code: dict[str, list[WarehouseStockSnapshot]] = {}
        for snapshot in actual_snapshots:
            code = str(snapshot.container_code or "").strip().lower()
            if not code:
                continue
            actual_by_code.setdefault(code, []).append(snapshot)

        reserve_entries: list[dict] = []
        reserved_code_set = {code.lower() for code in reserved_codes}
        for reserve in active_reserves:
            reserve_id = int(reserve.id or 0)
            snapshot_id = int(snapshot_id_by_reserve.get(reserve_id) or 0)
            snapshot = reserved_snapshots_by_id.get(snapshot_id)
            payload = reserve_payload_by_id.get(reserve_id) or {}
            box_code = str((snapshot.container_code if snapshot is not None else payload.get("box_code")) or "").strip()
            if not box_code or box_code.lower() not in reserved_code_set:
                continue
            open_qty = cls._reserve_open_qty(reserve)
            if open_qty <= 0:
                continue
            reserve_entries.append(
                {
                    "reserve": reserve,
                    "snapshot": snapshot,
                    "box_code": box_code,
                    "signature": (
                        str((snapshot.sku_code if snapshot is not None else reserve.sku_code) or "").strip(),
                        str((snapshot.size if snapshot is not None else reserve.size) or "").strip(),
                        str((snapshot.barcode if snapshot is not None else reserve.barcode) or "").strip(),
                        str((snapshot.goods_type if snapshot is not None else reserve.goods_type) or "").strip(),
                        open_qty,
                    ),
                    "qty": open_qty,
                }
            )
        if not reserve_entries:
            return 0

        unchanged_entry_indexes: set[int] = set()
        unchanged_snapshot_ids: set[int] = set()
        for index, entry in enumerate(reserve_entries):
            snapshot = entry["snapshot"]
            if snapshot is None:
                continue
            source_snapshot_id = int(snapshot.id or 0)
            if source_snapshot_id <= 0:
                continue
            actual_snapshots_for_code = actual_by_code.get(entry["box_code"].lower()) or []
            if any(int(actual_snapshot.id or 0) == source_snapshot_id for actual_snapshot in actual_snapshots_for_code):
                unchanged_entry_indexes.add(index)
                unchanged_snapshot_ids.add(source_snapshot_id)

        reserve_by_snapshot_id: dict[int, list[WarehouseReserve]] = {}
        for reserve in active_reserves:
            snapshot_id = int(snapshot_id_by_reserve.get(int(reserve.id or 0)) or 0)
            if snapshot_id <= 0:
                continue
            reserve_by_snapshot_id.setdefault(snapshot_id, []).append(reserve)

        covered_entry_indexes: set[int] = set()
        covered_snapshot_ids: set[int] = set()
        existing_target_reserves_by_signature: dict[tuple[str, str, str, str, int], list[WarehouseStockSnapshot]] = {}
        for snapshot in actual_snapshots:
            snapshot_id = int(snapshot.id or 0)
            if snapshot_id <= 0 or snapshot_id in unchanged_snapshot_ids:
                continue
            for reserve in reserve_by_snapshot_id.get(snapshot_id) or []:
                open_qty = cls._reserve_open_qty(reserve)
                if open_qty <= 0:
                    continue
                signature = (
                    str(snapshot.sku_code or "").strip(),
                    str(snapshot.size or "").strip(),
                    str(snapshot.barcode or "").strip(),
                    str(snapshot.goods_type or "").strip(),
                    open_qty,
                )
                existing_target_reserves_by_signature.setdefault(signature, []).append(snapshot)

        for index, entry in enumerate(reserve_entries):
            if index in unchanged_entry_indexes:
                continue
            candidates = existing_target_reserves_by_signature.get(entry["signature"]) or []
            while candidates:
                snapshot = candidates.pop(0)
                snapshot_id = int(snapshot.id or 0)
                if snapshot_id <= 0 or snapshot_id in covered_snapshot_ids:
                    continue
                covered_entry_indexes.add(index)
                covered_snapshot_ids.add(snapshot_id)
                break

        remaining_entries = [
            entry
            for index, entry in enumerate(reserve_entries)
            if index not in unchanged_entry_indexes and index not in covered_entry_indexes
        ]
        target_snapshots = [
            snapshot
            for snapshots in actual_by_code.values()
            for snapshot in snapshots
            if int(snapshot.id or 0) not in unchanged_snapshot_ids
            and int(snapshot.id or 0) not in covered_snapshot_ids
        ]
        if not remaining_entries or not target_snapshots:
            return 0

        per_reserve_candidates: dict[int, list[WarehouseStockSnapshot]] = {}
        for index, entry in enumerate(remaining_entries):
            signature = entry["signature"]
            candidates = []
            for snapshot in target_snapshots:
                candidate_signature = (
                    str(snapshot.sku_code or "").strip(),
                    str(snapshot.size or "").strip(),
                    str(snapshot.barcode or "").strip(),
                    str(snapshot.goods_type or "").strip(),
                    int(entry["qty"] or 0),
                )
                if candidate_signature != signature:
                    continue
                if cls._shipping_rebind_target_qty(snapshot) < int(entry["qty"] or 0):
                    continue
                candidates.append(snapshot)
            if not candidates:
                raise ValueError("РќРµ СѓРґР°Р»РѕСЃСЊ РїРµСЂРµРЅРµСЃС‚Рё shipping reserve: РЅРµ РЅР°Р№РґРµРЅ СЌРєРІРёРІР°Р»РµРЅС‚РЅС‹Р№ РєРѕСЂРѕР±.")
            per_reserve_candidates[index] = candidates

        ordered_indexes = sorted(
            per_reserve_candidates.keys(),
            key=lambda idx: (
                len(per_reserve_candidates.get(idx) or []),
                str(remaining_entries[idx]["box_code"] or ""),
            ),
        )
        assignment: dict[int, WarehouseStockSnapshot] = {}
        target_remaining_qty_by_id: dict[int, int] = {
            int(snapshot.id or 0): cls._shipping_rebind_target_qty(snapshot)
            for snapshot in target_snapshots
            if int(snapshot.id or 0) > 0
        }

        def backtrack(position: int) -> bool:
            if position >= len(ordered_indexes):
                return True
            entry_idx = ordered_indexes[position]
            entry_qty = int(remaining_entries[entry_idx]["qty"] or 0)
            for snapshot in per_reserve_candidates.get(entry_idx) or []:
                snapshot_id = int(snapshot.id or 0)
                if snapshot_id <= 0 or target_remaining_qty_by_id.get(snapshot_id, 0) < entry_qty:
                    continue
                target_remaining_qty_by_id[snapshot_id] = target_remaining_qty_by_id.get(snapshot_id, 0) - entry_qty
                assignment[entry_idx] = snapshot
                if backtrack(position + 1):
                    return True
                assignment.pop(entry_idx, None)
                target_remaining_qty_by_id[snapshot_id] = target_remaining_qty_by_id.get(snapshot_id, 0) + entry_qty
            return False

        if not backtrack(0):
            raise ValueError("РќРµ СѓРґР°Р»РѕСЃСЊ РѕРґРЅРѕР·РЅР°С‡РЅРѕ РїРµСЂРµРЅРµСЃС‚Рё shipping reserve РЅР° С„Р°РєС‚РёС‡РµСЃРєРё РѕС‚РѕР±СЂР°РЅРЅС‹Рµ РєРѕСЂРѕР±Р°.")

        moved_count = 0
        for entry_idx, target_snapshot in assignment.items():
            entry = remaining_entries[entry_idx]
            cls._apply_shipping_reserve_rebind(
                reserve=entry["reserve"],
                from_snapshot=entry["snapshot"],
                to_snapshot=target_snapshot,
                qty=int(entry["qty"] or 0),
                performed_by=performed_by,
                source_document_id=source_document_id or order_key,
            )
            moved_count += 1
        return moved_count


    @classmethod
    @transaction.atomic
    def release_stale_shipping_reserve_boxes(
        cls,
        *,
        agency: Agency,
        order_id: str,
        stale_box_codes: list[str],
        actual_box_codes: list[str],
        performed_by=None,
        source_document_id: str = "",
    ) -> int:
        order_key = str(order_id or "").strip()
        if not agency or not order_key:
            return 0

        def _normalize_codes(raw_codes: list[str]) -> list[str]:
            normalized: list[str] = []
            seen: set[str] = set()
            for raw_code in raw_codes or []:
                code = str(raw_code or "").strip()
                key = code.lower()
                if not code or key in seen:
                    continue
                seen.add(key)
                normalized.append(code)
            return normalized

        stale_codes = _normalize_codes(stale_box_codes)
        actual_keys = {code.lower() for code in _normalize_codes(actual_box_codes)}
        stale_keys = {code.lower() for code in stale_codes if code.lower() not in actual_keys}
        if not stale_keys:
            return 0

        active_reserves = list(
            WarehouseReserve.objects.filter(
                agency=agency,
                reserve_type=WarehouseReserve.TYPE_SHIPPING,
                context_type="shipping",
                context_id=order_key,
                status__in=[
                    WarehouseReserve.STATUS_ACTIVE,
                    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
                    WarehouseReserve.STATUS_ALLOCATED,
                    WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
                ],
            ).order_by("id")
        )
        if not active_reserves:
            return 0

        snapshot_id_by_reserve = cls._reserve_snapshot_id_map(active_reserves)
        snapshot_ids = {
            int(snapshot_id)
            for snapshot_id in snapshot_id_by_reserve.values()
            if int(snapshot_id or 0) > 0
        }
        snapshots_by_id = {
            int(snapshot.id): snapshot
            for snapshot in WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("container", "location")
            .filter(id__in=snapshot_ids, agency=agency, is_archived=False)
        }

        reserves_to_release: list[WarehouseReserve] = []
        for reserve in active_reserves:
            snapshot_id = int(snapshot_id_by_reserve.get(int(reserve.id or 0)) or 0)
            snapshot = snapshots_by_id.get(snapshot_id)
            box_code = ""
            if snapshot is not None:
                box_code = str(snapshot.container_code or getattr(snapshot.container, "container_code", "") or "").strip()
            if box_code and box_code.lower() in stale_keys:
                reserves_to_release.append(reserve)

        if not reserves_to_release:
            return 0

        cls._release_reserves_from_snapshots(
            reserves=reserves_to_release,
            reserved_qty_field="shipping_reserved_qty",
            release_event_type=WarehouseEventType.SHIPPING_RESERVE_RELEASED,
            performed_by=performed_by,
        )
        WarehouseReserve.objects.filter(id__in=[reserve.id for reserve in reserves_to_release]).update(
            status=WarehouseReserve.STATUS_RELEASED,
            released_by=performed_by if getattr(performed_by, "is_authenticated", False) else None,
            updated_at=timezone.now(),
        )
        return len(reserves_to_release)

    @classmethod
    def _shipping_box_codes_from_text(cls, text: str | None) -> set[str]:
        import re

        value = str(text or "")
        if not value:
            return set()
        pattern = r"\b[A-ZА-ЯЁ]{2,5}-\d{4}-\d{6,}-[A-Za-zА-Яа-яЁё0-9]+\b"
        return {match.group(0).strip() for match in re.finditer(pattern, value) if match.group(0).strip()}

    @classmethod
    def _shipping_allowed_box_codes_for_order(cls, *, agency: Agency, order_key: str) -> set[str]:
        from django.apps import apps

        normalized_order_key = str(order_key or "").strip()
        if not normalized_order_key:
            return set()
        try:
            ShippingOrder = apps.get_model("shipping", "ShippingOrder")
            ShippingOrderItem = apps.get_model("shipping", "ShippingOrderItem")
        except LookupError:
            return set()
        order = (
            ShippingOrder.objects.filter(agency=agency, number=normalized_order_key)
            .only("id")
            .first()
        )
        if order is None:
            return set()
        box_codes: set[str] = set()
        for row in ShippingOrderItem.objects.filter(order=order).values("comment"):
            box_codes.update(cls._shipping_box_codes_from_text(row.get("comment")))
        return box_codes

    @classmethod
    def _shipping_box_composition_signatures(
        cls,
        *,
        agency: Agency,
        box_codes: set[str],
    ) -> set[tuple[tuple[str, str, str, str, int], ...]]:
        normalized_codes = {str(code or "").strip() for code in box_codes or set() if str(code or "").strip()}
        if not normalized_codes:
            return set()
        grouped: dict[str, list[tuple[str, str, str, str, int]]] = {}
        rows = (
            WarehouseStockSnapshot.objects.filter(
                agency=agency,
                container_code__in=normalized_codes,
                is_archived=False,
            )
            .values("container_code", "sku_code", "size", "barcode", "goods_type", "qty")
            .order_by("container_code", "sku_code", "size", "barcode", "goods_type", "id")
        )
        for row in rows:
            code = str(row.get("container_code") or "").strip()
            if not code:
                continue
            grouped.setdefault(code, []).append(
                (
                    str(row.get("sku_code") or "").strip(),
                    str(row.get("size") or "").strip(),
                    str(row.get("barcode") or "").strip(),
                    str(row.get("goods_type") or "").strip(),
                    int(row.get("qty") or 0),
                )
            )
        signatures: set[tuple[tuple[str, str, str, str, int], ...]] = set()
        for items in grouped.values():
            signature = tuple(sorted(item for item in items if int(item[4] or 0) > 0))
            if signature:
                signatures.add(signature)
        return signatures

    @classmethod
    def _shipping_box_composition_signature(
        cls,
        *,
        agency: Agency,
        box_code: str,
    ) -> tuple[tuple[str, str, str, str, int], ...]:
        normalized_code = str(box_code or "").strip()
        if not normalized_code:
            return tuple()
        rows = (
            WarehouseStockSnapshot.objects.filter(
                agency=agency,
                container_code=normalized_code,
                is_archived=False,
            )
            .values("sku_code", "size", "barcode", "goods_type", "qty")
            .order_by("sku_code", "size", "barcode", "goods_type", "id")
        )
        return tuple(
            sorted(
                (
                    str(row.get("sku_code") or "").strip(),
                    str(row.get("size") or "").strip(),
                    str(row.get("barcode") or "").strip(),
                    str(row.get("goods_type") or "").strip(),
                    int(row.get("qty") or 0),
                )
                for row in rows
                if int(row.get("qty") or 0) > 0
            )
        )

    @classmethod
    @transaction.atomic
    def refresh_shipping_reserve_box_bindings(
        cls,
        *,
        agency: Agency,
        order_id: str,
        performed_by=None,
        source_document_id: str = "",
    ) -> int:
        order_key = str(order_id or "").strip()
        if not order_key:
            return 0
        active_reserves = list(
            WarehouseReserve.objects.filter(
                agency=agency,
                reserve_type=WarehouseReserve.TYPE_SHIPPING,
                context_type="shipping",
                context_id=order_key,
                status__in=[
                    WarehouseReserve.STATUS_ACTIVE,
                    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
                    WarehouseReserve.STATUS_ALLOCATED,
                    WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
                    WarehouseReserve.STATUS_SATISFIED,
                ],
            ).order_by("id")
        )
        if not active_reserves:
            return 0
        allowed_box_codes = cls._shipping_allowed_box_codes_for_order(
            agency=agency,
            order_key=order_key,
        )
        allowed_box_signatures = cls._shipping_box_composition_signatures(
            agency=agency,
            box_codes=allowed_box_codes,
        )

        snapshot_id_by_reserve = cls._reserve_snapshot_id_map(active_reserves)
        snapshot_ids = {
            int(snapshot_id)
            for snapshot_id in snapshot_id_by_reserve.values()
            if int(snapshot_id or 0) > 0
        }
        snapshots_by_id = {
            int(snapshot.id): snapshot
            for snapshot in WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("container", "location", "active_operation", "last_event")
            .filter(id__in=snapshot_ids, agency=agency, is_archived=False)
        }

        moved_count = 0
        for reserve in active_reserves:
            open_qty = cls._reserve_open_qty(reserve)
            if open_qty <= 0:
                continue
            snapshot = snapshots_by_id.get(int(snapshot_id_by_reserve.get(int(reserve.id or 0)) or 0))
            arrived_snapshot = cls._find_arrived_otg_snapshot_for_reserve(
                agency=agency,
                snapshot=snapshot,
            )
            if arrived_snapshot is not None:
                cls._mark_shipping_reserve_arrived_to_otg(snapshot=arrived_snapshot, order_id=order_key)
                arrived_snapshot.shipping_reserved_qty = 0
                update_fields = ["shipping_reserved_qty", "updated_at"]
                if str(arrived_snapshot.zone_code or "").strip().upper() == "OTG":
                    arrived_snapshot.available_qty = 0
                    arrived_snapshot.warehouse_state_code = WarehouseStateCode.IN_OTG.value
                    arrived_snapshot.active_operation = None
                    arrived_snapshot.active_operation_type = ""
                    update_fields.extend(
                        [
                            "available_qty",
                            "warehouse_state_code",
                            "active_operation",
                            "active_operation_type",
                        ]
                    )
                arrived_snapshot.save(update_fields=update_fields)
                continue
            if (
                snapshot is not None
                and str(snapshot.zone_code or "").strip().upper() == "OTG"
                and cls._snapshot_matches_shipping_context(snapshot, order_key)
            ):
                cls._mark_shipping_reserve_arrived_to_otg(snapshot=snapshot, order_id=order_key)
                snapshot.shipping_reserved_qty = 0
                snapshot.available_qty = 0
                snapshot.warehouse_state_code = WarehouseStateCode.IN_OTG.value
                snapshot.active_operation = None
                snapshot.active_operation_type = ""
                snapshot.save(
                    update_fields=[
                        "shipping_reserved_qty",
                        "available_qty",
                        "warehouse_state_code",
                        "active_operation",
                        "active_operation_type",
                        "updated_at",
                    ]
                )
                continue
            if (
                snapshot is not None
                and str(snapshot.warehouse_state_code or "").strip() == WarehouseStateCode.MOVING_TO_OTG.value
                and cls._snapshot_matches_shipping_context(snapshot, order_key)
            ):
                continue
            if not cls._shipping_reserve_snapshot_needs_rebind(snapshot):
                continue

            target_base_qs = (
                WarehouseStockSnapshot.objects.select_for_update(of=("self",))
                .select_related("container", "location")
                .filter(
                    agency=agency,
                    sku_code=reserve.sku_code,
                    size=reserve.size,
                    barcode=reserve.barcode,
                    goods_type=reserve.goods_type,
                    available_qty__gte=open_qty,
                    is_archived=False,
                )
                .exclude(zone_code__iexact="OBR")
                .exclude(zone_code__iexact="OTG")
                .exclude(zone_code__iexact="LOAD")
                .exclude(zone_code__iexact="VEH")
            )
            if snapshot is not None:
                target_base_qs = target_base_qs.exclude(id=snapshot.id)

            target_snapshot = None
            if allowed_box_codes:
                target_snapshot = (
                    target_base_qs.filter(container_code__in=allowed_box_codes)
                    .order_by("available_qty", "id")
                    .first()
                )
                if target_snapshot is None and allowed_box_signatures:
                    for candidate in target_base_qs.order_by("available_qty", "id")[:200]:
                        candidate_signature = cls._shipping_box_composition_signature(
                            agency=agency,
                            box_code=str(candidate.container_code or ""),
                        )
                        if candidate_signature and candidate_signature in allowed_box_signatures:
                            target_snapshot = candidate
                            break
            else:
                target_snapshot = (
                    target_base_qs.filter(
                        warehouse_state_code__in=[
                            WarehouseStateCode.STORED.value,
                            WarehouseStateCode.PLACED_AFTER_PROCESSING.value,
                        ],
                    )
                    .order_by("available_qty", "id")
                    .first()
                )
            if target_snapshot is None:
                raise ValueError(
                    f"No free box available to rebind shipping reserve for {reserve.sku_code}"
                )

            remaining_qty = open_qty
            source_reserved_qty = int(getattr(snapshot, "shipping_reserved_qty", 0) or 0) if snapshot is not None else 0
            if snapshot is not None and source_reserved_qty > 0:
                qty_from_source = min(source_reserved_qty, remaining_qty)
                cls._apply_shipping_reserve_rebind(
                    reserve=reserve,
                    from_snapshot=snapshot,
                    to_snapshot=target_snapshot,
                    qty=qty_from_source,
                    performed_by=performed_by,
                    source_document_id=source_document_id or order_key,
                )
                remaining_qty -= qty_from_source
            if remaining_qty > 0:
                cls._apply_shipping_reserve_rebind(
                    reserve=reserve,
                    from_snapshot=None,
                    to_snapshot=target_snapshot,
                    qty=remaining_qty,
                    performed_by=performed_by,
                    source_document_id=source_document_id or order_key,
                )
            moved_count += 1
        return moved_count

    @classmethod
    @transaction.atomic
    def request_move_to_otg(
        cls,
        *,
        agency: Agency,
        order_id: str,
        destination_row_no: int = 0,
        destination_section_no: int = 0,
        destination_tier_no: int = 0,
        destination_cell_no: int = 0,
        destination_location_code: str = "",
        allow_legacy_generic_destination: bool = False,
        requested_by=None,
        requested_by_role: str = "storekeeper",
        warehouse_code: str = "MSK",
    ) -> WarehouseOperation:
        order_key = str(order_id or "").strip()
        cls.refresh_shipping_reserve_box_bindings(
            agency=agency,
            order_id=order_key,
            performed_by=requested_by,
            source_document_id=order_key,
        )
        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",)).select_related("location", "container")
            .filter(
                agency=agency,
                warehouse_state_code=WarehouseStateCode.RESERVED_FOR_SHIPPING.value,
                shipping_reserved_qty__gt=0,
                is_archived=False,
            )
            .order_by("id")
        )
        snapshots = [snapshot for snapshot in snapshots if cls._snapshot_matches_shipping_context(snapshot, order_key)]
        if not snapshots:
            raise ValueError("No shipping-reserved snapshots ready for move to OTG")
        cls._assert_shipping_does_not_take_fbs_stock(snapshots)

        source_location = snapshots[0].location or cls.ensure_location(warehouse_code=warehouse_code, zone_code="OS")
        destination = cls.concrete_movement_destination(
            warehouse_code=warehouse_code,
            zone_code="OTG",
            location_code=destination_location_code,
            row_no=destination_row_no,
            section_no=destination_section_no,
            tier_no=destination_tier_no,
            cell_no=destination_cell_no,
            allow_legacy_generic_location=allow_legacy_generic_destination,
        )
        operation = WarehouseOperation.objects.create(
            agency=agency,
            operation_type=WarehouseOperation.TYPE_MOVE_TO_OTG,
            context_type="shipping",
            context_id=order_key,
            source_location=source_location,
            destination_location=destination,
            source_zone_code=source_location.zone_code,
            destination_zone_code=destination.zone_code,
            status=WarehouseOperation.STATUS_PLANNED,
            requested_by=requested_by,
            requested_by_role=requested_by_role,
            assigned_executor_role="reachtruck",
            planned_qty=sum(int(snapshot.shipping_reserved_qty or 0) for snapshot in snapshots),
        )
        WarehouseEvent.objects.create(
            agency=agency,
            event_type=WarehouseEventType.OTG_REQUESTED.value,
            stock_context_type="shipping",
            stock_context_id=order_key,
            operation=operation,
            from_location=source_location,
            to_location=destination,
            from_zone_code=source_location.zone_code,
            to_zone_code=destination.zone_code,
            qty=operation.planned_qty,
            performed_by=requested_by,
            performed_by_role=requested_by_role,
            occurred_at=timezone.now(),
        )
        for snapshot in snapshots:
            WarehouseOperationTask.objects.create(
                operation=operation,
                task_type=WarehouseOperationTask.TYPE_PALLET_MOVE if snapshot.container_id else WarehouseOperationTask.TYPE_BOX_MOVE,
                container=snapshot.container,
                from_location=snapshot.location,
                to_location=destination,
                from_zone_code=snapshot.zone_code,
                to_zone_code=destination.zone_code,
                qty_planned=int(snapshot.shipping_reserved_qty or 0),
                status=WarehouseOperationTask.STATUS_CREATED,
                executor_role="reachtruck",
            )
            snapshot.active_operation = operation
            snapshot.active_operation_type = operation.operation_type
            snapshot.save(update_fields=["active_operation", "active_operation_type", "updated_at"])
        return operation

    @classmethod
    @transaction.atomic
    def request_move_to_otg_for_containers(
        cls,
        *,
        agency: Agency,
        order_id: str,
        container_codes: list[str],
        destination_row_no: int = 0,
        destination_section_no: int = 0,
        destination_tier_no: int = 0,
        destination_cell_no: int = 0,
        destination_location_code: str = "",
        allow_legacy_generic_destination: bool = False,
        requested_by=None,
        requested_by_role: str = "reachtruck",
        warehouse_code: str = "MSK",
    ) -> WarehouseOperation:
        order_key = str(order_id or "").strip()
        normalized_codes: list[str] = []
        seen_codes: set[str] = set()
        for raw_code in container_codes or []:
            code = str(raw_code or "").strip()
            key = code.lower()
            if not code or key in seen_codes:
                continue
            seen_codes.add(key)
            normalized_codes.append(code)
        if not normalized_codes:
            raise ValueError("No container codes provided for OTG move")

        container_ids = list(
            WarehouseContainer.objects.filter(
                agency=agency,
                container_code__in=normalized_codes,
            ).values_list("id", flat=True)
        )
        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("location", "container", "parent_container")
            .filter(
                Q(container_code__in=normalized_codes)
                | Q(container_id__in=container_ids)
                | Q(parent_container__container_code__in=normalized_codes)
                | Q(parent_container_id__in=container_ids),
                agency=agency,
                is_archived=False,
                warehouse_state_code__in=[
                    WarehouseStateCode.PLACED_IN_RECEIVING.value,
                    WarehouseStateCode.STORED.value,
                    WarehouseStateCode.RESERVED_FOR_SHIPPING.value,
                    WarehouseStateCode.MOVING_TO_OTG.value,
                    WarehouseStateCode.IN_PROCESSING_ZONE.value,
                    WarehouseStateCode.PLACED_AFTER_PROCESSING.value,
                ],
            )
            .order_by("id")
        )
        if not snapshots:
            raise ValueError("No warehouse snapshots found for OTG move")
        cls._assert_shipping_does_not_take_fbs_stock(snapshots)

        source_location = snapshots[0].location or cls.ensure_location(warehouse_code=warehouse_code, zone_code="OS")
        destination = cls.concrete_movement_destination(
            warehouse_code=warehouse_code,
            zone_code="OTG",
            location_code=destination_location_code,
            row_no=destination_row_no,
            section_no=destination_section_no,
            tier_no=destination_tier_no,
            cell_no=destination_cell_no,
            allow_legacy_generic_location=allow_legacy_generic_destination,
        )
        operation = WarehouseOperation.objects.create(
            agency=agency,
            operation_type=WarehouseOperation.TYPE_MOVE_TO_OTG,
            context_type="shipping",
            context_id=order_key,
            source_location=source_location,
            destination_location=destination,
            source_zone_code=source_location.zone_code,
            destination_zone_code=destination.zone_code,
            status=WarehouseOperation.STATUS_PLANNED,
            requested_by=requested_by,
            requested_by_role=requested_by_role,
            assigned_executor_role="reachtruck",
            planned_qty=sum(cls._shipping_snapshot_flow_qty(snapshot) for snapshot in snapshots),
        )
        WarehouseEvent.objects.create(
            agency=agency,
            event_type=WarehouseEventType.OTG_REQUESTED.value,
            stock_context_type="shipping",
            stock_context_id=order_key,
            operation=operation,
            from_location=source_location,
            to_location=destination,
            from_zone_code=source_location.zone_code,
            to_zone_code=destination.zone_code,
            qty=operation.planned_qty,
            performed_by=requested_by,
            performed_by_role=requested_by_role,
            occurred_at=timezone.now(),
            payload={"container_codes": normalized_codes},
        )
        operation_tasks: list[WarehouseOperationTask] = []
        snapshots_to_update: list[WarehouseStockSnapshot] = []
        now = timezone.now()
        for snapshot in snapshots:
            move_container = snapshot.container
            if snapshot.parent_container and str(snapshot.parent_container.container_code or "").strip().lower() in seen_codes:
                move_container = snapshot.parent_container
            operation_tasks.append(
                WarehouseOperationTask(
                    operation=operation,
                    task_type=(
                        WarehouseOperationTask.TYPE_PALLET_MOVE
                        if move_container
                        and move_container.container_type
                        in {WarehouseContainer.TYPE_PALLET, WarehouseContainer.TYPE_MIXED_PALLET}
                        else WarehouseOperationTask.TYPE_BOX_MOVE
                    ),
                    container=move_container,
                    from_location=snapshot.location,
                    to_location=destination,
                    from_zone_code=snapshot.zone_code,
                    to_zone_code=destination.zone_code,
                    qty_planned=cls._shipping_snapshot_flow_qty(snapshot),
                    status=WarehouseOperationTask.STATUS_CREATED,
                    executor_role="reachtruck",
                    payload={
                        "snapshot_ids": [int(snapshot.id)],
                        "container_code": (
                            move_container.container_code
                            if move_container
                            else snapshot.container_code
                        ),
                    },
                )
            )
            snapshot.active_operation = operation
            snapshot.active_operation_type = operation.operation_type
            snapshot.updated_at = now
            snapshots_to_update.append(snapshot)
        WarehouseOperationTask.objects.bulk_create(operation_tasks)
        WarehouseStockSnapshot.objects.bulk_update(
            snapshots_to_update,
            ["active_operation", "active_operation_type", "updated_at"],
        )
        return operation

    @classmethod
    @transaction.atomic
    def start_move_to_otg(
        cls,
        *,
        operation: WarehouseOperation,
        performed_by=None,
    ) -> WarehouseOperation:
        if operation.operation_type != WarehouseOperation.TYPE_MOVE_TO_OTG:
            raise ValueError("Only move_to_otg operations can be started by this write-path")
        now = timezone.now()
        operation.status = WarehouseOperation.STATUS_IN_PROGRESS
        operation.started_at = operation.started_at or now
        operation.save(update_fields=["status", "started_at", "updated_at"])
        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",)).select_related("agency", "container", "location")
            .filter(active_operation=operation, is_archived=False)
            .order_by("id")
        )
        cls._assert_shipping_does_not_take_fbs_stock(snapshots)
        events: list[WarehouseEvent] = []
        for snapshot in snapshots:
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.MOVEMENT_STARTED,
                operation_type=operation.operation_type,
            )
            events.append(
                WarehouseEvent(
                    agency=snapshot.agency,
                    event_type=WarehouseEventType.MOVEMENT_STARTED.value,
                    stock_context_type="shipping",
                    stock_context_id=operation.context_id,
                    container=snapshot.container,
                    operation=operation,
                    from_location=snapshot.location,
                    to_location=operation.destination_location,
                    from_zone_code=snapshot.zone_code,
                    to_zone_code=operation.destination_zone_code,
                    qty=cls._shipping_snapshot_flow_qty(snapshot),
                    performed_by=performed_by,
                    performed_by_role=cls._role_of(performed_by) or "reachtruck",
                    occurred_at=now,
                )
            )
            snapshot.warehouse_state_code = transition.code.value
            snapshot.updated_at = now
        WarehouseEvent.objects.bulk_create(events)
        for snapshot, event in zip(snapshots, events):
            snapshot.last_event = event
        WarehouseStockSnapshot.objects.bulk_update(
            snapshots,
            ["warehouse_state_code", "last_event", "updated_at"],
        )
        operation.tasks.update(
            status=WarehouseOperationTask.STATUS_IN_PROGRESS,
            started_at=now,
            updated_at=now,
        )
        return operation

    @classmethod
    @transaction.atomic
    def complete_move_to_otg(
        cls,
        *,
        operation: WarehouseOperation,
        performed_by=None,
        allow_legacy_generic_destination: bool = False,
    ) -> WarehouseOperation:
        if operation.operation_type != WarehouseOperation.TYPE_MOVE_TO_OTG:
            raise ValueError("Only move_to_otg operations can be completed by this write-path")
        destination = operation.destination_location
        if destination is None:
            raise ValueError("OTG move operation must have destination_location")
        if not allow_legacy_generic_destination:
            try:
                require_concrete_movement_location(destination, purpose="перемещения в OTG")
            except ValidationError as exc:
                raise ValueError("; ".join(exc.messages)) from exc
        total_done = 0
        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",)).select_related("agency", "container", "location", "parent_container")
            .filter(active_operation=operation, is_archived=False)
            .order_by("id")
        )
        cls._assert_shipping_does_not_take_fbs_stock(snapshots)
        cls._mark_shipping_reserves_arrived_to_otg(
            snapshots=snapshots,
            order_id=str(operation.context_id or "").strip(),
        )
        source_pallet_ids = {
            int(snapshot.parent_container_id)
            for snapshot in snapshots
            if snapshot.parent_container_id
        }
        now = timezone.now()
        events: list[WarehouseEvent] = []
        next_state_codes: list[str] = []
        for snapshot in snapshots:
            moved_qty = cls._shipping_snapshot_flow_qty(snapshot)
            previous_state_code = str(snapshot.warehouse_state_code or "").strip()
            if previous_state_code == WarehouseStateCode.PROCESSING_CONSUMED.value:
                next_state_code = WarehouseStateCode.IN_OTG.value
                event_payload = {
                    "restored_confirmed_shipping_box": True,
                    "previous_warehouse_state_code": previous_state_code,
                }
            else:
                transition = WarehouseTransitionService.apply_event(
                    snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                    WarehouseEventType.OTG_ARRIVED,
                    zone_to=destination.zone_code,
                )
                next_state_code = transition.code.value
                event_payload = {}
            events.append(
                WarehouseEvent(
                    agency=snapshot.agency,
                    event_type=WarehouseEventType.OTG_ARRIVED.value,
                    stock_context_type="shipping",
                    stock_context_id=operation.context_id,
                    container=snapshot.container,
                    operation=operation,
                    from_location=snapshot.location,
                    to_location=destination,
                    from_zone_code=snapshot.zone_code,
                    to_zone_code=destination.zone_code,
                    qty=moved_qty,
                    performed_by=performed_by,
                    performed_by_role=cls._role_of(performed_by) or "reachtruck",
                    occurred_at=now,
                    payload=event_payload,
                )
            )
            next_state_codes.append(next_state_code)
            total_done += moved_qty

        WarehouseEvent.objects.bulk_create(events)
        containers_to_update: dict[int, WarehouseContainer] = {}
        for snapshot, event, next_state_code in zip(snapshots, events, next_state_codes):
            container = snapshot.container
            if container is not None and str(container.container_type or "").strip() == WarehouseContainer.TYPE_BOX:
                container_changed = False
                if container.parent_container_id is not None:
                    container.parent_container = None
                    container_changed = True
                if container.current_location_id != destination.id:
                    container.current_location = destination
                    container_changed = True
                if container_changed:
                    container.updated_at = now
                    containers_to_update[int(container.id)] = container
            snapshot.location = destination
            snapshot.zone_code = destination.zone_code
            snapshot.zone_kind = destination.zone_kind
            snapshot.warehouse_state_code = next_state_code
            snapshot.shipping_reserved_qty = 0
            snapshot.available_qty = 0
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
            snapshot.last_event = event
            if snapshot.parent_container_id is not None:
                snapshot.parent_container = None
            snapshot.updated_at = now
        if containers_to_update:
            WarehouseContainer.objects.bulk_update(
                list(containers_to_update.values()),
                ["parent_container", "current_location", "updated_at"],
            )
        WarehouseStockSnapshot.objects.bulk_update(
            snapshots,
            [
                "location",
                "zone_code",
                "zone_kind",
                "warehouse_state_code",
                "shipping_reserved_qty",
                "available_qty",
                "active_operation",
                "active_operation_type",
                "last_event",
                "parent_container",
                "updated_at",
            ],
        )
        cls._archive_empty_source_pallets(source_pallet_ids)
        operation.status = WarehouseOperation.STATUS_DONE
        operation.done_qty = total_done
        operation.completed_at = timezone.now()
        operation.save(update_fields=["status", "done_qty", "completed_at", "updated_at"])
        operation.tasks.update(
            status=WarehouseOperationTask.STATUS_DONE,
            qty_done=models.F("qty_planned"),
            completed_at=timezone.now(),
            updated_at=timezone.now(),
        )
        return operation

    @classmethod
    @transaction.atomic
    def report_otg_unit_shortage(
        cls,
        *,
        agency: Agency,
        order_id: str,
        move_task_id: str,
        source_pallet_code: str,
        source_box_code: str,
        barcode: str,
        expected_qty: int,
        actual_qty: int,
        performed_by=None,
    ) -> WarehouseUnitShortageResult:
        """Reconcile a physically empty OTG source box with a unit shortage.

        Only the confirmed missing quantity is removed. The actually scanned
        quantity remains in the source snapshot until the normal OTG
        completion moves it, so the existing atomic pick path stays the only
        way to create stock in OTG.
        """
        order_key = str(order_id or "").strip()
        task_key = str(move_task_id or "").strip()
        pallet_code = str(source_pallet_code or "").strip()
        box_code = str(source_box_code or "").strip()
        barcode_value = str(barcode or "").strip()
        expected = max(int(expected_qty or 0), 0)
        actual = max(int(actual_qty or 0), 0)
        missing = expected - actual
        if not agency or not order_key or not task_key:
            raise ValueError("Для фиксации недостачи нужны заявка и задание OTG.")
        if not pallet_code or not box_code or not barcode_value:
            raise ValueError("Не определены паллета, короб или штрихкод недостачи.")
        if expected <= 0 or actual <= 0 or missing <= 0:
            raise ValueError("Фактическое количество должно быть меньше ожидаемого и больше нуля.")

        existing_event = (
            WarehouseEvent.objects.select_related("operation")
            .filter(
                agency=agency,
                event_type=WarehouseEventType.STOCK_CORRECTED.value,
                source_document_type="reachtruck_move",
                source_document_id=task_key,
                payload__correction_kind="otg_unit_shortage",
                payload__box_code__iexact=box_code,
                payload__barcode=barcode_value,
            )
            .order_by("id")
            .first()
        )
        if existing_event is not None:
            existing_payload = (
                existing_event.payload
                if isinstance(existing_event.payload, dict)
                else {}
            )
            if (
                int(existing_payload.get("expected_qty") or 0) != expected
                or int(existing_payload.get("actual_qty") or 0) != actual
                or int(existing_payload.get("missing_qty") or 0) != missing
            ):
                raise ValueError("По этому коробу уже зафиксирована другая недостача.")
            event_ids = list(
                WarehouseEvent.objects.filter(
                    operation_id=existing_event.operation_id,
                    event_type=WarehouseEventType.STOCK_CORRECTED.value,
                    payload__correction_kind="otg_unit_shortage",
                ).values_list("id", flat=True)
            )
            return WarehouseUnitShortageResult(
                snapshot_ids=[
                    int(value)
                    for value in (existing_payload.get("snapshot_ids") or [])
                    if int(value or 0) > 0
                ],
                event_ids=[int(value) for value in event_ids],
                operation_id=int(existing_event.operation_id or 0),
                expected_qty=expected,
                actual_qty=actual,
                missing_qty=missing,
                released_reserve_qty=int(
                    existing_payload.get("released_reserve_qty") or 0
                ),
            )

        snapshots_qs = (
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related(
                "container",
                "parent_container",
                "location",
                "active_operation",
            )
            .filter(
                agency=agency,
                is_archived=False,
                qty__gt=0,
                barcode=barcode_value,
            )
            .filter(
                Q(container_code__iexact=box_code)
                | Q(container__container_code__iexact=box_code)
            )
            .order_by("id")
        )
        if pallet_code:
            snapshots_qs = snapshots_qs.filter(
                Q(parent_container__container_code__iexact=pallet_code)
                | Q(container__parent_container__container_code__iexact=pallet_code)
            )
        snapshots = list(snapshots_qs)
        if not snapshots:
            raise ValueError(f"Товар {barcode_value} в коробе {box_code} не найден на остатках.")
        if any(str(snapshot.marking_code or "").strip() for snapshot in snapshots):
            raise ValueError("Для маркированного товара недостачу нужно фиксировать по конкретному КИЗ.")
        source_qty = sum(int(snapshot.qty or 0) for snapshot in snapshots)
        if source_qty != expected:
            raise ValueError(
                f"Остаток короба изменился: по учёту {source_qty} шт., ожидалось {expected} шт. Обновите задание."
            )
        for snapshot in snapshots:
            active_operation = snapshot.active_operation
            if (
                active_operation is not None
                and str(active_operation.status or "").strip()
                not in _FINAL_OPERATION_STATUSES
            ):
                raise ValueError("Короб уже участвует в другой складской операции.")
            if snapshot.is_in_vehicle or str(snapshot.current_trip_id or "").strip():
                raise ValueError("Короб уже включён в рейс или транспорт.")
            if int(snapshot.processing_reserved_qty or 0) > 0:
                raise ValueError("На коробе есть резерв обработки; недостачу OTG фиксировать нельзя.")
            if int(snapshot.other_reserved_qty or 0) > 0:
                raise ValueError("На коробе есть другой складской резерв; недостачу OTG фиксировать нельзя.")

        released_reserve_qty = 0
        release_remaining = missing
        reserve_statuses = {
            WarehouseReserve.STATUS_ACTIVE,
            WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
            WarehouseReserve.STATUS_ALLOCATED,
            WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
        }
        reserves = list(
            WarehouseReserve.objects.select_for_update()
            .filter(
                agency=agency,
                reserve_type=WarehouseReserve.TYPE_SHIPPING,
                context_type="shipping",
                context_id=order_key,
                barcode=barcode_value,
                status__in=reserve_statuses,
            )
            .order_by("-id")
        )
        for reserve in reserves:
            if release_remaining <= 0:
                break
            allocated_open = max(
                int(reserve.qty_allocated or 0)
                - int(reserve.qty_satisfied or 0),
                0,
            )
            released = min(allocated_open, release_remaining)
            if released <= 0:
                continue
            reserve.qty_allocated = max(
                int(reserve.qty_allocated or 0) - released,
                int(reserve.qty_satisfied or 0),
            )
            if int(reserve.qty_satisfied or 0) > 0:
                reserve.status = WarehouseReserve.STATUS_PARTIALLY_SATISFIED
            elif int(reserve.qty_allocated or 0) >= int(reserve.qty_reserved or 0):
                reserve.status = WarehouseReserve.STATUS_ALLOCATED
            elif int(reserve.qty_allocated or 0) > 0:
                reserve.status = WarehouseReserve.STATUS_PARTIALLY_ALLOCATED
            else:
                reserve.status = WarehouseReserve.STATUS_ACTIVE
            reserve.save(update_fields=["qty_allocated", "status", "updated_at"])
            release_remaining -= released
            released_reserve_qty += released

        actor = performed_by if getattr(performed_by, "is_authenticated", False) else None
        source_location = next(
            (snapshot.location for snapshot in snapshots if snapshot.location_id),
            None,
        )
        source_zone = str(
            getattr(source_location, "zone_code", "")
            or snapshots[0].zone_code
            or ""
        ).strip()
        operation = WarehouseOperation.objects.create(
            agency=agency,
            operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
            context_type="otg_unit_shortage_check",
            context_id=task_key,
            source_document_type="shipping",
            source_document_id=order_key,
            source_location=source_location,
            source_zone_code=source_zone,
            status=WarehouseOperation.STATUS_BLOCKED,
            requested_by=actor,
            requested_by_role=cls._role_of(actor) or "reachtruck_driver",
            assigned_executor_role="head_manager",
            comment=(
                f"Недостача {missing} шт. товара {barcode_value} в коробе {box_code}; "
                f"водитель подтвердил {actual} из {expected} шт."
            ),
            planned_qty=missing,
            done_qty=0,
            started_at=timezone.now(),
        )

        remaining = missing
        snapshot_ids: list[int] = []
        event_ids: list[int] = []
        now = timezone.now()
        ordered_snapshots = sorted(
            snapshots,
            key=lambda snapshot: (
                0 if int(snapshot.available_qty or 0) > 0 else 1,
                int(snapshot.id or 0),
            ),
        )
        all_snapshot_ids = [int(snapshot.id) for snapshot in snapshots]
        for snapshot in ordered_snapshots:
            if remaining <= 0:
                break
            removable = min(int(snapshot.qty or 0), remaining)
            if removable <= 0:
                continue
            before_qty = int(snapshot.qty or 0)
            before_available = int(snapshot.available_qty or 0)
            before_shipping_reserved = int(snapshot.shipping_reserved_qty or 0)
            after_qty = before_qty - removable
            after_shipping_reserved = min(before_shipping_reserved, after_qty)
            event = WarehouseEvent.objects.create(
                agency=agency,
                event_type=WarehouseEventType.STOCK_CORRECTED.value,
                stock_context_type="shipping",
                stock_context_id=order_key,
                container=snapshot.container,
                operation=operation,
                source_document_type="reachtruck_move",
                source_document_id=task_key,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code=snapshot.zone_code,
                qty=removable,
                performed_by=actor,
                performed_by_role=cls._role_of(actor) or "reachtruck_driver",
                occurred_at=now,
                payload={
                    "correction_kind": "otg_unit_shortage",
                    "order_id": order_key,
                    "move_task_id": task_key,
                    "pallet_code": pallet_code,
                    "box_code": box_code,
                    "barcode": barcode_value,
                    "expected_qty": expected,
                    "actual_qty": actual,
                    "missing_qty": missing,
                    "removed_from_snapshot_qty": removable,
                    "released_reserve_qty": released_reserve_qty,
                    "snapshot_ids": all_snapshot_ids,
                    "before": {
                        "qty": before_qty,
                        "available_qty": before_available,
                        "shipping_reserved_qty": before_shipping_reserved,
                    },
                    "after": {
                        "qty": after_qty,
                        "shipping_reserved_qty": after_shipping_reserved,
                    },
                },
            )
            snapshot.qty = after_qty
            snapshot.shipping_reserved_qty = after_shipping_reserved
            snapshot.available_qty = max(
                after_qty
                - int(snapshot.processing_reserved_qty or 0)
                - after_shipping_reserved
                - int(snapshot.other_reserved_qty or 0),
                0,
            )
            snapshot.is_archived = after_qty <= 0
            snapshot.last_event = event
            snapshot.snapshot_version = int(snapshot.snapshot_version or 1) + 1
            snapshot.save(
                update_fields=[
                    "qty",
                    "available_qty",
                    "shipping_reserved_qty",
                    "is_archived",
                    "last_event",
                    "snapshot_version",
                    "updated_at",
                ]
            )
            snapshot_ids.append(int(snapshot.id))
            event_ids.append(int(event.id))
            remaining -= removable
        if remaining > 0:
            raise ValueError("Не удалось атомарно зафиксировать полную недостачу.")

        return WarehouseUnitShortageResult(
            snapshot_ids=snapshot_ids,
            event_ids=event_ids,
            operation_id=int(operation.id),
            expected_qty=expected,
            actual_qty=actual,
            missing_qty=missing,
            released_reserve_qty=released_reserve_qty,
        )

    @staticmethod
    def _barcode_case_index(barcodes) -> dict[str, set[str]]:
        """Group barcode spellings by their case-insensitive key.

        Marketplace rework stores the product barcode in lower case
        (``ozn…``) while the client catalog keeps the original spelling
        (``OZN…``). A Honest Sign rule must never depend on that
        difference, so every lookup below is made on the folded key and
        the answer is returned in the spellings the caller actually uses.
        """
        index: dict[str, set[str]] = {}
        for value in barcodes:
            text = str(value or "").strip()
            if text:
                index.setdefault(text.lower(), set()).add(text)
        return index

    @staticmethod
    def _spellings_for(case_index: dict[str, set[str]], folded_values) -> set[str]:
        matched: set[str] = set()
        for value in folded_values:
            key = str(value or "").strip().lower()
            matched.update(case_index.get(key, ()))
        return matched

    @classmethod
    def _catalog_honest_sign_barcodes(
        cls, *, agency: Agency, case_index: dict[str, set[str]]
    ) -> set[str]:
        """Barcodes of the client's Honest Sign cards, matched ignoring case."""
        if not case_index:
            return set()
        folded = (
            SKUBarcode.objects.filter(
                sku__agency=agency,
                sku__honest_sign=True,
                sku__deleted=False,
            )
            .annotate(value_folded=Lower("value"))
            .filter(value_folded__in=set(case_index))
            .values_list("value_folded", flat=True)
        )
        return cls._spellings_for(case_index, folded)

    @classmethod
    def _physically_marked_barcodes(
        cls, *, agency: Agency, case_index: dict[str, set[str]]
    ) -> set[str]:
        """Barcodes whose live stock already carries a KIZ, ignoring case."""
        if not case_index:
            return set()
        folded = (
            WarehouseStockSnapshot.objects.filter(
                agency=agency,
                is_archived=False,
                qty__gt=0,
            )
            .exclude(marking_code="")
            .annotate(barcode_folded=Lower("barcode"))
            .filter(barcode_folded__in=set(case_index))
            .values_list("barcode_folded", flat=True)
        )
        return cls._spellings_for(case_index, folded)

    @classmethod
    def _marking_candidate_index(
        cls, order, barcodes: set[str] | None
    ) -> tuple[object, dict[str, set[str]]]:
        """Narrow the order rows to the requested barcodes, ignoring case."""
        candidate_qs = order.items.exclude(barcode="")
        requested_index = (
            cls._barcode_case_index(barcodes) if barcodes is not None else None
        )
        if requested_index is not None:
            if not requested_index:
                return candidate_qs.none(), {}
            candidate_qs = candidate_qs.annotate(
                barcode_folded=Lower("barcode")
            ).filter(barcode_folded__in=set(requested_index))
        case_index = cls._barcode_case_index(
            candidate_qs.values_list("barcode", flat=True)
        )
        if requested_index:
            # The caller checks membership with its own spelling, so keep it.
            for key, spellings in requested_index.items():
                if key in case_index:
                    case_index[key] |= spellings
        return candidate_qs, case_index

    @classmethod
    def _required_marking_barcodes_for_shipping(
        cls,
        *,
        agency: Agency,
        order_id: str,
        barcodes: set[str] | None = None,
    ) -> set[str]:
        from shipping.models import ShippingOrder

        order_key = str(order_id or "").strip()
        order = (
            ShippingOrder.objects.filter(agency=agency, number=order_key)
            .only("id", "delivery_type")
            .first()
        )
        if (
            order is None
            or order.delivery_type != ShippingOrder.DELIVERY_MARKETPLACE
        ):
            return set()
        candidate_qs, case_index = cls._marking_candidate_index(order, barcodes)
        if not case_index:
            return set()
        required_barcodes = cls._spellings_for(
            case_index,
            candidate_qs.filter(sku__honest_sign=True).values_list("barcode", flat=True),
        )
        # Legacy shipping rows can lack the SKU FK. The client's barcode
        # catalog remains the authoritative marking flag for those rows.
        required_barcodes.update(
            cls._catalog_honest_sign_barcodes(agency=agency, case_index=case_index)
        )
        # If the physical stock already carries a KIZ, require the same
        # reachtruck verification even when catalog data is incomplete.
        required_barcodes.update(
            cls._physically_marked_barcodes(agency=agency, case_index=case_index)
        )
        return required_barcodes

    @classmethod
    def _required_loose_packing_marking_barcodes(
        cls,
        *,
        agency: Agency,
        order_id: str,
        barcodes: set[str] | None = None,
    ) -> set[str]:
        from shipping.models import ShippingOrder

        order_key = str(order_id or "").strip()
        order = (
            ShippingOrder.objects.filter(agency=agency, number=order_key)
            .only("id", "shipping_discrepancy_payload")
            .first()
        )
        if order is None:
            return set()
        raw_shipping_payload = getattr(order, "shipping_discrepancy_payload", None)
        shipping_payload = (
            raw_shipping_payload if isinstance(raw_shipping_payload, dict) else {}
        )
        operational_overrides = shipping_payload.get("operational_overrides")
        operational_overrides = (
            operational_overrides if isinstance(operational_overrides, dict) else {}
        )
        marking_override = operational_overrides.get("loose_packing_marking_scan")
        if (
            isinstance(marking_override, dict)
            and marking_override.get("enabled") is True
            and str(marking_override.get("mode") or "").strip() == "barcode_only"
        ):
            return set()
        candidate_qs, case_index = cls._marking_candidate_index(order, barcodes)
        if not case_index:
            return set()
        catalog_barcodes = cls._spellings_for(
            case_index,
            candidate_qs.filter(sku__honest_sign=True).values_list("barcode", flat=True),
        )
        # Some legacy shipping rows have no SKU FK even though their product
        # barcode is registered in the client's SKU catalog.  The catalog
        # marking flag must still drive the warehouse scan requirement.
        catalog_barcodes.update(
            cls._catalog_honest_sign_barcodes(agency=agency, case_index=case_index)
        )
        physically_marked_barcodes = cls._physically_marked_barcodes(
            agency=agency, case_index=case_index
        )
        return catalog_barcodes | physically_marked_barcodes

    @classmethod
    def _validated_loose_shipping_marking_code(
        cls,
        *,
        agency: Agency,
        product_barcode: str,
        marking_code: str,
    ) -> tuple[str, str]:
        normalized_scan = normalize_marking_code(marking_code)
        product_code = str(product_barcode or "").strip()
        if (
            product_code
            and normalized_scan.startswith(product_code)
            and normalized_scan[len(product_code) :].startswith("01")
        ):
            raise ValueError(
                "Перед Data Matrix считан линейный штрихкод товара. "
                "Отсканируйте только квадратный Data Matrix этой единицы."
            )
        try:
            data_matrix = validate_import_marking_code(
                normalized_scan,
                product_barcode=product_barcode,
            )
        except MarkingCodeFormatError as exc:
            raise ValueError(f"Некорректный Data Matrix: {exc}.") from exc

        identity = marking_code_identity(data_matrix)
        # In loose shipping the product barcode selects the order line, while
        # the following physical Data Matrix scan identifies the packed unit.
        # These scans are intentionally independent: the Data Matrix must be
        # well-formed and unique, but its GTIN is not matched to the product EAN.
        return data_matrix, identity

    @classmethod
    def validate_partial_shipping_marking_scan(
        cls,
        *,
        agency: Agency,
        order_id: str,
        source_box_code: str,
        barcode: str,
        marking_code: str,
        source_pallet_code: str = "",
        allow_receiving_source: bool = False,
    ) -> dict:
        order_key = str(order_id or "").strip()
        box_code = str(source_box_code or "").strip()
        product_barcode = str(barcode or "").strip()
        raw_data_matrix = str(marking_code or "").strip()
        if not order_key or not box_code or not product_barcode or not raw_data_matrix:
            raise ValueError("Для проверки маркировки нужны заявка, короб, ШК товара и Data Matrix.")
        required = cls._required_marking_barcodes_for_shipping(
            agency=agency,
            order_id=order_key,
            barcodes={product_barcode},
        )
        if product_barcode not in required:
            raise ValueError("Эта позиция не требует сканирования Data Matrix в данной заявке.")
        data_matrix, data_matrix_identity = cls._validated_loose_shipping_marking_code(
            agency=agency,
            product_barcode=product_barcode,
            marking_code=raw_data_matrix,
        )
        source_states = [
            WarehouseStateCode.STORED.value,
            WarehouseStateCode.PLACED_AFTER_PROCESSING.value,
            WarehouseStateCode.RESERVED_FOR_SHIPPING.value,
        ]
        if allow_receiving_source:
            source_states.append(WarehouseStateCode.PLACED_IN_RECEIVING.value)
        source_qs = (
            WarehouseStockSnapshot.objects.select_related(
                "container__parent_container",
                "parent_container",
            )
            .filter(
                agency=agency,
                barcode=product_barcode,
                qty__gt=0,
                is_archived=False,
                warehouse_state_code__in=source_states,
            )
            .filter(
                Q(container_code__iexact=box_code)
                | Q(container__container_code__iexact=box_code)
            )
            .order_by("id")
        )
        pallet_code = str(source_pallet_code or "").strip()
        if pallet_code:
            source_qs = source_qs.filter(
                Q(parent_container__container_code__iexact=pallet_code)
                | Q(container__parent_container__container_code__iexact=pallet_code)
                | Q(container_code__iexact=box_code)
            )
        source_snapshots = list(source_qs)
        source_snapshots = [
            snapshot
            for snapshot in source_snapshots
            if min(
                int(snapshot.shipping_reserved_qty or 0)
                or int(snapshot.available_qty or 0),
                int(snapshot.qty or 0),
            )
            > 0
        ]
        source_matches = [
            snapshot
            for snapshot in source_snapshots
            if marking_code_identity(snapshot.marking_code) == data_matrix_identity
        ]
        if len(source_matches) > 1:
            raise ValueError(
                "Data Matrix продублирован в исходном коробе. "
                "Скан не засчитан; передайте короб на проверку."
            )
        selected_snapshot = source_matches[0] if source_matches else None

        possible_duplicates = (
            WarehouseStockSnapshot.objects.filter(
                agency=agency,
                is_archived=False,
                qty__gt=0,
            )
            .exclude(marking_code="")
            .filter(
                Q(marking_code__in=marking_code_variants(data_matrix))
                | Q(marking_code__startswith=data_matrix_identity)
            )
            .only("id", "marking_code")
        )
        if selected_snapshot is not None:
            possible_duplicates = possible_duplicates.exclude(id=selected_snapshot.id)
        if any(
            marking_code_identity(candidate.marking_code) == data_matrix_identity
            for candidate in possible_duplicates.iterator()
        ):
            raise ValueError(
                "Этот Data Matrix уже используется другой активной складской единицей. "
                "Скан не засчитан."
            )

        if selected_snapshot is None:
            shipped_candidates = (
                WarehouseStockSnapshot.objects.filter(
                    agency=agency,
                    warehouse_state_code=WarehouseStateCode.SHIPPED.value,
                )
                .exclude(marking_code="")
                .filter(
                    Q(marking_code__in=marking_code_variants(data_matrix))
                    | Q(marking_code__startswith=data_matrix_identity)
                )
                .select_related("last_event")
                .only(
                    "id",
                    "marking_code",
                    "last_event_id",
                    "last_event__stock_context_id",
                )
            )
            shipped_match = next(
                (
                    candidate
                    for candidate in shipped_candidates.iterator()
                    if marking_code_identity(candidate.marking_code)
                    == data_matrix_identity
                ),
                None,
            )
            if shipped_match is not None:
                shipment = str(
                    getattr(shipped_match.last_event, "stock_context_id", "") or ""
                ).strip()
                shipment_suffix = f" по заявке {shipment}" if shipment else ""
                raise ValueError(
                    f"Этот Data Matrix уже был отгружен{shipment_suffix}. "
                    "Скан не засчитан; возьмите другую единицу."
                )
            has_unmarked_source_capacity = any(
                not str(snapshot.marking_code or "").strip()
                for snapshot in source_snapshots
            )
            if not has_unmarked_source_capacity:
                raise ValueError(
                    f"Этот Data Matrix не относится к активному товару в коробе {box_code}. "
                    "Скан не засчитан; возьмите другую единицу."
                )

        # The source identity is checked again under locks when the task completes.
        return {
            "snapshot_id": int(selected_snapshot.id) if selected_snapshot else 0,
            "barcode": product_barcode,
            "marking_code": data_matrix,
            "marking_identity": data_matrix_identity,
            "box_code": box_code,
        }

    @classmethod
    def _expand_loose_shipping_marked_otg_units(
        cls,
        *,
        agency: Agency,
        order_id: str,
        barcode: str,
    ) -> int:
        """Normalize legacy aggregate OTG stock before per-unit CHZ packing."""
        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .filter(
                agency=agency,
                barcode=barcode,
                qty__gt=1,
                is_archived=False,
                container__isnull=True,
                container_code="",
                warehouse_state_code=WarehouseStateCode.IN_OTG.value,
                last_event__stock_context_type="shipping",
                last_event__stock_context_id=order_id,
            )
            .order_by("id")
        )
        clones: list[WarehouseStockSnapshot] = []
        expanded = 0
        for snapshot in snapshots:
            source_qty = int(snapshot.qty or 0)
            if any(
                int(value or 0) > 0
                for value in (
                    snapshot.available_qty,
                    snapshot.processing_reserved_qty,
                    snapshot.shipping_reserved_qty,
                    snapshot.other_reserved_qty,
                )
            ):
                raise ValueError(
                    "Агрегированная единица ЧЗ в OTG имеет активный остаток или резерв. "
                    "Упаковка остановлена для проверки."
                )
            source_version = int(snapshot.snapshot_version or 1)
            snapshot.qty = 1
            snapshot.snapshot_version = source_version + 1
            snapshot.save(update_fields=["qty", "snapshot_version", "updated_at"])
            for _ in range(source_qty - 1):
                clones.append(
                    WarehouseStockSnapshot(
                        agency_id=snapshot.agency_id,
                        stock_unit_type=snapshot.stock_unit_type or "item",
                        source_context_type=snapshot.source_context_type,
                        source_context_id=snapshot.source_context_id,
                        sku_ref_id=snapshot.sku_ref_id,
                        sku_code=snapshot.sku_code,
                        name=snapshot.name,
                        size=snapshot.size,
                        barcode=snapshot.barcode,
                        goods_type=snapshot.goods_type,
                        marking_code="",
                        qty=1,
                        available_qty=0,
                        processing_reserved_qty=0,
                        shipping_reserved_qty=0,
                        other_reserved_qty=0,
                        container_id=None,
                        container_code="",
                        parent_container_id=None,
                        location_id=snapshot.location_id,
                        zone_code=snapshot.zone_code,
                        zone_kind=snapshot.zone_kind,
                        warehouse_state_code=snapshot.warehouse_state_code,
                        active_operation_id=snapshot.active_operation_id,
                        active_operation_type=snapshot.active_operation_type,
                        current_trip_id=snapshot.current_trip_id,
                        is_in_vehicle=snapshot.is_in_vehicle,
                        is_archived=False,
                        snapshot_version=source_version + 1,
                        last_event_id=snapshot.last_event_id,
                    )
                )
            expanded += source_qty
        if clones:
            WarehouseStockSnapshot.objects.bulk_create(clones, batch_size=500)
        return expanded

    @classmethod
    def _shipping_marking_source_filter(cls, source_bindings):
        # Bind to the exact source snapshots AND their original containers.
        # A unit already moved to an output box cannot be consumed again.
        if source_bindings is None:
            return Q(container__isnull=True, container_code="")
        scope = Q(pk__in=[])
        for snapshot_id, container_id, container_code in source_bindings:
            scope |= Q(pk=int(snapshot_id), container_id=container_id,
                       container_code=str(container_code or ""))
        return scope

    @classmethod
    @transaction.atomic
    def validate_loose_shipping_marking_scan(
        cls,
        *,
        agency: Agency,
        order_id: str,
        barcode: str,
        marking_code: str,
        source_bindings=None,
    ) -> dict:
        order_key = str(order_id or "").strip()
        product_barcode = str(barcode or "").strip()
        if not order_key or not product_barcode or not str(marking_code or "").strip():
            raise ValueError("Для упаковки нужны заявка, ШК товара и Data Matrix.")
        required = cls._required_loose_packing_marking_barcodes(
            agency=agency,
            order_id=order_key,
            barcodes={product_barcode},
        )
        if product_barcode not in required:
            raise ValueError("Эта позиция не требует сканирования Data Matrix в данной заявке.")
        data_matrix, data_matrix_identity = cls._validated_loose_shipping_marking_code(
            agency=agency,
            product_barcode=product_barcode,
            marking_code=marking_code,
        )
        available = WarehouseStockSnapshot.objects.filter(
            cls._shipping_marking_source_filter(source_bindings),
            agency=agency,
            barcode=product_barcode,
            qty__gt=0,
            is_archived=False,
            is_in_vehicle=False,
            warehouse_state_code=WarehouseStateCode.IN_OTG.value,
            last_event__stock_context_type="shipping",
            last_event__stock_context_id=order_key,
        ).exists()
        if not available:
            raise ValueError(
                "В OTG не осталось доступных штучных единиц этого товара по заявке."
            )
        # The product barcode selects the requested shipping item. The next
        # physical Data Matrix scan is accepted as fact, exactly as in partial
        # picking. The concrete stock unit is locked and assigned when the box
        # is saved, so no receiving-time barcode/Data Matrix pair is rechecked.
        return {
            "snapshot_id": 0,
            "barcode": product_barcode,
            "marking_code": data_matrix,
            "marking_identity": data_matrix_identity,
        }

    @classmethod
    @transaction.atomic
    def assign_marked_shipping_unit_to_box(
        cls,
        *,
        agency: Agency,
        order_id: str,
        snapshot_id: int,
        barcode: str,
        marking_code: str,
        destination_container: WarehouseContainer,
        performed_by=None,
        source_bindings=None,
    ) -> int:
        order_key = str(order_id or "").strip()
        product_barcode = str(barcode or "").strip()
        if not order_key or not product_barcode or not str(marking_code or "").strip():
            raise ValueError("Для упаковки нужны заявка, ШК товара и Data Matrix.")
        required = cls._required_loose_packing_marking_barcodes(
            agency=agency,
            order_id=order_key,
            barcodes={product_barcode},
        )
        if product_barcode not in required:
            raise ValueError("Маркированная единица не относится к обязательному скану этой заявки.")
        data_matrix, data_matrix_identity = cls._validated_loose_shipping_marking_code(
            agency=agency,
            product_barcode=product_barcode,
            marking_code=marking_code,
        )

        Agency.objects.select_for_update().filter(pk=agency.pk).exists()
        if destination_container.agency_id != agency.pk:
            raise ValueError("Короб назначения принадлежит другому клиенту.")
        if source_bindings is not None and (
            destination_container.container_type != WarehouseContainer.TYPE_BOX
            or destination_container.status != WarehouseContainer.STATUS_ACTIVE
        ):
            raise ValueError("Для упаковки нужен активный короб назначения.")
        candidates = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("location")
            .filter(
                cls._shipping_marking_source_filter(source_bindings),
                agency=agency,
                barcode=product_barcode,
                qty__gt=0,
                is_archived=False,
                is_in_vehicle=False,
                warehouse_state_code=WarehouseStateCode.IN_OTG.value,
                last_event__stock_context_type="shipping",
                last_event__stock_context_id=order_key,
            )
            .order_by("id")
        )
        if not candidates:
            raise ValueError(
                "Маркированная единица изменилась или уже упакована. "
                "Обновите страницу и повторите сканирование."
            )

        if source_bindings is not None:
            from shipping.models import ShippingOrder
            if not ShippingOrder.objects.filter(
                agency=agency, number=order_key,
                status__in=[ShippingOrder.STATUS_PICKING, ShippingOrder.STATUS_PACKED],
            ).exists():
                raise ValueError("Заявка уже не доступна для упаковки.")
            source_containers = {row.container_id for row in candidates if row.container_id}
            if destination_container.pk in source_containers:
                raise ValueError("Для переупаковки нужен новый короб, отличный от исходного.")
            if WarehouseStockSnapshot.objects.filter(
                container_id__in=source_containers, is_archived=False, qty__gt=0,
            ).exclude(
                agency=agency, warehouse_state_code=WarehouseStateCode.IN_OTG.value,
                last_event__stock_context_type="shipping", last_event__stock_context_id=order_key,
                is_in_vehicle=False,
            ).exists():
                raise ValueError("В исходном коробе есть товар вне этой отгрузки. Переупаковка остановлена.")

        identity_matches = [
            candidate
            for candidate in candidates
            if marking_code_identity(candidate.marking_code) == data_matrix_identity
        ]
        if len(identity_matches) > 1:
            raise ValueError("Data Matrix продублирован в складском учёте. Упаковка остановлена для проверки.")
        requested_snapshot_id = int(snapshot_id or 0)
        requested_snapshot = next(
            (candidate for candidate in candidates if int(candidate.id) == requested_snapshot_id),
            None,
        )
        snapshot = identity_matches[0] if identity_matches else None
        if snapshot is None and requested_snapshot is not None and not str(requested_snapshot.marking_code or "").strip():
            snapshot = requested_snapshot
        if snapshot is None:
            snapshot = next(
                (candidate for candidate in candidates if not str(candidate.marking_code or "").strip()),
                None,
            )
        if snapshot is None:
            raise ValueError(
                "Для этого Data Matrix не найдена свободная единица товара. "
                "Обновите страницу и повторите сканирование."
            )

        possible_duplicates = (
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .filter(agency=agency, is_archived=False, qty__gt=0)
            .exclude(id=snapshot.id)
            .exclude(marking_code="")
            .filter(
                Q(marking_code__in=marking_code_variants(data_matrix))
                | Q(marking_code__startswith=data_matrix_identity)
            )
            .only("id", "marking_code")
        )
        if any(
            marking_code_identity(candidate.marking_code) == data_matrix_identity
            for candidate in possible_duplicates.iterator()
        ):
            raise ValueError("Этот Data Matrix уже используется другой активной единицей.")

        stored_marking_code = str(snapshot.marking_code or "").strip()
        source_qty = int(snapshot.qty or 0)
        if source_qty > 1 and stored_marking_code:
            raise ValueError(
                "Агрегированный товар ЧЗ содержит один Data Matrix для нескольких единиц. "
                "Упаковка остановлена для проверки."
            )
        if (source_qty > 1 or source_bindings is not None) and any(
            int(value or 0) > 0
            for value in (
                snapshot.available_qty,
                snapshot.processing_reserved_qty,
                snapshot.shipping_reserved_qty,
                snapshot.other_reserved_qty,
            )
        ):
            raise ValueError(
                "Агрегированный товар ЧЗ в OTG имеет активный остаток или резерв. "
                "Упаковка остановлена для проверки."
            )
        event = WarehouseEvent.objects.create(
            agency=agency,
            event_type=WarehouseEventType.MOVEMENT_COMPLETED.value,
            stock_context_type="shipping",
            stock_context_id=order_key,
            container=destination_container,
            from_location=snapshot.location,
            to_location=snapshot.location,
            from_zone_code=snapshot.zone_code,
            to_zone_code=snapshot.zone_code,
            qty=1,
            performed_by=performed_by,
            performed_by_role=cls._role_of(performed_by) or "storekeeper",
            occurred_at=timezone.now(),
            payload={
                "marked_shipping_packing": True,
                "source_snapshot_id": int(snapshot.id),
                "scanned_product_barcode": product_barcode,
                "snapshot_product_barcode": str(snapshot.barcode or "").strip(),
                "marking_code": data_matrix,
                "stored_source_marking_code": stored_marking_code,
                "destination_box_code": destination_container.container_code,
            },
        )
        next_version = int(snapshot.snapshot_version or 1) + 1
        if source_qty > 1:
            snapshot.qty = source_qty - 1
            snapshot.snapshot_version = next_version
            snapshot.save(update_fields=["qty", "snapshot_version", "updated_at"])
            marked_snapshot = WarehouseStockSnapshot.objects.create(
                agency_id=snapshot.agency_id,
                stock_unit_type=snapshot.stock_unit_type or "item",
                source_context_type=snapshot.source_context_type,
                source_context_id=snapshot.source_context_id,
                sku_ref_id=snapshot.sku_ref_id,
                sku_code=snapshot.sku_code,
                name=snapshot.name,
                size=snapshot.size,
                barcode=snapshot.barcode,
                goods_type=snapshot.goods_type,
                marking_code=data_matrix,
                qty=1,
                available_qty=0,
                processing_reserved_qty=0,
                shipping_reserved_qty=0,
                other_reserved_qty=0,
                container=destination_container,
                container_code=destination_container.container_code,
                parent_container=destination_container.parent_container,
                location_id=snapshot.location_id,
                zone_code=snapshot.zone_code,
                zone_kind=snapshot.zone_kind,
                warehouse_state_code=snapshot.warehouse_state_code,
                active_operation_id=snapshot.active_operation_id,
                active_operation_type=snapshot.active_operation_type,
                current_trip_id=snapshot.current_trip_id,
                is_in_vehicle=snapshot.is_in_vehicle,
                is_archived=False,
                snapshot_version=next_version,
                last_event=event,
            )
            return int(marked_snapshot.id)

        snapshot.container = destination_container
        snapshot.container_code = destination_container.container_code
        snapshot.parent_container = destination_container.parent_container
        snapshot.marking_code = data_matrix
        snapshot.last_event = event
        snapshot.snapshot_version = next_version
        snapshot.save(
            update_fields=[
                "container",
                "container_code",
                "parent_container",
                "marking_code",
                "last_event",
                "snapshot_version",
                "updated_at",
            ]
        )
        return int(snapshot.id)

    @classmethod
    @transaction.atomic
    def assign_preverified_marked_shipping_units_to_box(
        cls,
        *,
        agency: Agency,
        order_id: str,
        snapshot_ids: list[int],
        barcode: str,
        qty: int,
        destination_container: WarehouseContainer,
        performed_by=None,
    ) -> list[int]:
        """Pack KIZ units already verified by reachtruck without a second scan."""
        order_key = str(order_id or "").strip()
        product_barcode = str(barcode or "").strip()
        requested_qty = max(int(qty or 0), 0)
        candidate_ids = sorted(
            {
                parsed_id
                for snapshot_id in snapshot_ids or []
                if (parsed_id := cls._parse_positive_int(snapshot_id)) is not None
            }
        )
        if not order_key or not product_barcode or requested_qty <= 0 or not candidate_ids:
            raise ValueError(
                "Для упаковки по КИЗ ричтрака нужны заявка, ШК товара, количество и складские единицы."
            )
        required = cls._required_loose_packing_marking_barcodes(
            agency=agency,
            order_id=order_key,
            barcodes={product_barcode},
        )
        if product_barcode not in required:
            raise ValueError(
                "Повторный скан КИЗ можно пропустить только для маркированного товара этой заявки."
            )

        # Serialize KIZ reuse and duplicate checks for one client.
        Agency.objects.select_for_update().filter(pk=agency.pk).exists()
        candidates = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("location", "last_event")
            .filter(
                id__in=candidate_ids,
                agency=agency,
                barcode=product_barcode,
                qty=1,
                is_archived=False,
                container__isnull=True,
                container_code="",
                warehouse_state_code=WarehouseStateCode.IN_OTG.value,
                last_event__stock_context_type="shipping",
                last_event__stock_context_id=order_key,
            )
            .exclude(marking_code="")
            .order_by("id")
        )
        validated: list[tuple[WarehouseStockSnapshot, str, str]] = []
        seen_identities: set[str] = set()
        for snapshot in candidates:
            source_payload = (
                snapshot.last_event.payload
                if snapshot.last_event and isinstance(snapshot.last_event.payload, dict)
                else {}
            )
            if not (
                source_payload.get("partial_shipping_pick") is True
                and source_payload.get("marking_scan_recorded") is True
            ):
                raise ValueError(
                    "КИЗ единицы не подтверждён сканом ричтрака. Обновите страницу и отсканируйте Data Matrix."
                )
            try:
                data_matrix = validate_import_marking_code(
                    snapshot.marking_code,
                    product_barcode=product_barcode,
                )
            except MarkingCodeFormatError as exc:
                raise ValueError(f"Сохранён некорректный Data Matrix: {exc}.") from exc
            identity = marking_code_identity(data_matrix)
            if not identity or identity in seen_identities:
                raise ValueError(
                    "Среди КИЗ ричтрака найден повтор. Упаковка остановлена для проверки."
                )
            seen_identities.add(identity)
            validated.append((snapshot, data_matrix, identity))
        if len(validated) < requested_qty:
            raise ValueError(
                f"Для товара {product_barcode} ричтрак подтвердил КИЗ только для "
                f"{len(validated)} из {requested_qty} единиц. Остатки не изменены."
            )

        selected = validated[:requested_qty]
        selected_ids = [int(snapshot.id) for snapshot, _code, _identity in selected]
        selected_variants: set[str] = set()
        duplicate_filter = Q(pk__in=[])
        for _snapshot, data_matrix, identity in selected:
            selected_variants.update(marking_code_variants(data_matrix))
            duplicate_filter |= Q(marking_code__startswith=identity)
        possible_duplicates = (
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .filter(agency=agency, is_archived=False, qty__gt=0)
            .exclude(id__in=selected_ids)
            .exclude(marking_code="")
            .filter(Q(marking_code__in=selected_variants) | duplicate_filter)
            .only("id", "marking_code")
        )
        selected_identities = {identity for _snapshot, _code, identity in selected}
        if any(
            marking_code_identity(duplicate.marking_code) in selected_identities
            for duplicate in possible_duplicates.iterator()
        ):
            raise ValueError(
                "Один из КИЗ ричтрака уже используется другой активной складской единицей."
            )

        moved_ids: list[int] = []
        for snapshot, data_matrix, identity in selected:
            previous_event_id = int(snapshot.last_event_id or 0)
            event = WarehouseEvent.objects.create(
                agency=agency,
                event_type=WarehouseEventType.MOVEMENT_COMPLETED.value,
                stock_context_type="shipping",
                stock_context_id=order_key,
                container=destination_container,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code=snapshot.zone_code,
                qty=1,
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by) or "storekeeper",
                occurred_at=timezone.now(),
                payload={
                    "marked_shipping_packing": True,
                    "reachtruck_marking_scan_reused": True,
                    "source_snapshot_id": int(snapshot.id),
                    "source_reachtruck_event_id": previous_event_id,
                    "scanned_product_barcode": product_barcode,
                    "marking_code": data_matrix,
                    "marking_identity": identity,
                    "destination_box_code": destination_container.container_code,
                },
            )
            snapshot.container = destination_container
            snapshot.container_code = destination_container.container_code
            snapshot.parent_container = destination_container.parent_container
            snapshot.marking_code = data_matrix
            snapshot.last_event = event
            snapshot.snapshot_version = int(snapshot.snapshot_version or 1) + 1
            snapshot.save(
                update_fields=[
                    "container",
                    "container_code",
                    "parent_container",
                    "marking_code",
                    "last_event",
                    "snapshot_version",
                    "updated_at",
                ]
            )
            moved_ids.append(int(snapshot.id))
        return moved_ids

    @classmethod
    @transaction.atomic
    def complete_partial_shipping_pick_to_otg(
        cls,
        *,
        agency: Agency,
        order_id: str,
        source_pallet_code: str,
        picked_rows: list[dict],
        destination_row_no: int = 0,
        destination_section_no: int = 0,
        destination_tier_no: int = 0,
        destination_cell_no: int = 0,
        destination_location_code: str = "",
        allow_legacy_generic_destination: bool = False,
        performed_by=None,
        warehouse_code: str = "MSK",
        allow_receiving_source: bool = False,
    ) -> list[int]:
        order_key = str(order_id or "").strip()
        if not order_key:
            raise ValueError("order_id is required for partial shipping pick")
        destination = cls.concrete_movement_destination(
            warehouse_code=warehouse_code,
            zone_code="OTG",
            location_code=destination_location_code,
            row_no=destination_row_no,
            section_no=destination_section_no,
            tier_no=destination_tier_no,
            cell_no=destination_cell_no,
            allow_legacy_generic_location=allow_legacy_generic_destination,
        )
        created_snapshot_ids: list[int] = []
        pallet_code = str(source_pallet_code or "").strip()
        requested_barcodes = {
            str(barcode or "").strip()
            for row in picked_rows or []
            if isinstance(row, dict)
            for barcode, qty in dict(row.get("barcode_qty") or {}).items()
            if str(barcode or "").strip() and max(int(qty or 0), 0) > 0
        }
        required_marking_barcodes = cls._required_marking_barcodes_for_shipping(
            agency=agency,
            order_id=order_key,
            barcodes=requested_barcodes,
        )
        used_marking_identities: set[str] = set()
        if required_marking_barcodes:
            # Serialize raw Data Matrix registration for one client so two
            # concurrent tasks cannot create the same active code.
            Agency.objects.select_for_update().filter(pk=agency.pk).exists()

        for row in picked_rows or []:
            if not isinstance(row, dict):
                continue
            box_code = str(row.get("box_code") or "").strip()
            row_qty = max(int(row.get("picked_qty") or row.get("qty") or 0), 0)
            barcode_qty = {
                str(barcode or "").strip(): max(int(qty or 0), 0)
                for barcode, qty in dict(row.get("barcode_qty") or {}).items()
                if str(barcode or "").strip() and max(int(qty or 0), 0) > 0
            }
            if not box_code or row_qty <= 0:
                continue
            if not barcode_qty:
                raise ValueError("Р”Р»СЏ С‡Р°СЃС‚РёС‡РЅРѕРіРѕ РѕС‚Р±РѕСЂР° РІ OTG РЅСѓР¶РЅР° СЂР°Р·Р±РёРІРєР° РїРѕ РЁРљ.")
            if sum(barcode_qty.values()) != row_qty:
                raise ValueError("Р Р°Р·Р±РёРІРєР° РїРѕ РЁРљ РЅРµ СЃРѕРІРїР°РґР°РµС‚ СЃ РєРѕР»РёС‡РµСЃС‚РІРѕРј С‡Р°СЃС‚РёС‡РЅРѕРіРѕ РѕС‚Р±РѕСЂР°.")
            marking_units_by_barcode: dict[str, list[dict]] = {}
            for raw_unit in row.get("marking_units") or []:
                if not isinstance(raw_unit, dict):
                    continue
                unit_barcode = str(raw_unit.get("barcode") or "").strip()
                if unit_barcode:
                    marking_units_by_barcode.setdefault(unit_barcode, []).append(dict(raw_unit))

            for barcode, qty_to_pick in barcode_qty.items():
                remaining = int(qty_to_pick)
                requires_marking = barcode in required_marking_barcodes
                marking_units = marking_units_by_barcode.get(barcode, [])
                if requires_marking and len(marking_units) != remaining:
                    raise ValueError(
                        f"Для товара {barcode} нужно отсканировать {remaining} уникальных Data Matrix; "
                        f"получено {len(marking_units)}. Остатки не изменены."
                    )
                source_states = [
                    WarehouseStateCode.STORED.value,
                    WarehouseStateCode.PLACED_AFTER_PROCESSING.value,
                    WarehouseStateCode.RESERVED_FOR_SHIPPING.value,
                ]
                if allow_receiving_source:
                    source_states.append(WarehouseStateCode.PLACED_IN_RECEIVING.value)
                qs = (
                    WarehouseStockSnapshot.objects.select_for_update(of=("self",))
                    .select_related("container", "parent_container", "location", "active_operation")
                    .filter(
                        agency=agency,
                        barcode=barcode,
                        is_archived=False,
                        warehouse_state_code__in=source_states,
                    )
                    .filter(
                        Q(container_code__iexact=box_code)
                        | Q(container__container_code__iexact=box_code)
                    )
                    .order_by("-shipping_reserved_qty", "id")
                )
                if pallet_code:
                    qs = qs.filter(
                        Q(parent_container__container_code__iexact=pallet_code)
                        | Q(container__parent_container__container_code__iexact=pallet_code)
                        | Q(container_code__iexact=box_code)
                    )
                if requires_marking:
                    candidate_snapshots = list(qs)
                    allocated_by_snapshot: dict[int, int] = {}
                    snapshot_allocations: list[tuple[WarehouseStockSnapshot, str]] = []
                    for unit in marking_units:
                        raw_marking_code = str(unit.get("marking_code") or "").strip()
                        unit_box = str(unit.get("box_code") or box_code).strip()
                        if not raw_marking_code or unit_box.casefold() != box_code.casefold():
                            raise ValueError(
                                f"Некорректный скан Data Matrix для товара {barcode} в коробе {box_code}. "
                                "Остатки не изменены."
                            )
                        marking_code, marking_identity = cls._validated_loose_shipping_marking_code(
                            agency=agency,
                            product_barcode=barcode,
                            marking_code=raw_marking_code,
                        )
                        if marking_identity in used_marking_identities:
                            raise ValueError("Один Data Matrix нельзя использовать дважды в одной отгрузке.")
                        ordered_candidates = sorted(
                            candidate_snapshots,
                            key=lambda snapshot: (
                                marking_code_identity(snapshot.marking_code) != marking_identity,
                                -int(snapshot.shipping_reserved_qty or 0),
                                int(snapshot.id),
                            ),
                        )
                        selected_snapshot = next(
                            (
                                snapshot
                                for snapshot in ordered_candidates
                                if (
                                    min(
                                        int(snapshot.shipping_reserved_qty or 0)
                                        or int(snapshot.available_qty or 0),
                                        int(snapshot.qty or 0),
                                    )
                                    - int(allocated_by_snapshot.get(int(snapshot.id), 0) or 0)
                                )
                                > 0
                            ),
                            None,
                        )
                        if selected_snapshot is None:
                            raise ValueError(
                                f"В коробе {box_code} не хватает доступного товара {barcode}. "
                                "Остатки не изменены."
                            )
                        possible_duplicates = (
                            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
                            .filter(
                                agency=agency,
                                is_archived=False,
                                qty__gt=0,
                            )
                            .exclude(id=selected_snapshot.id)
                            .exclude(marking_code="")
                            .filter(
                                Q(marking_code__in=marking_code_variants(marking_code))
                                | Q(marking_code__startswith=marking_identity)
                            )
                            .only("id", "marking_code")
                        )
                        if any(
                            marking_code_identity(duplicate.marking_code) == marking_identity
                            for duplicate in possible_duplicates.iterator()
                        ):
                            raise ValueError(
                                f"Data Matrix {marking_code} уже используется другой активной единицей. "
                                "Остатки не изменены."
                            )
                        used_marking_identities.add(marking_identity)
                        allocated_by_snapshot[int(selected_snapshot.id)] = (
                            int(allocated_by_snapshot.get(int(selected_snapshot.id), 0) or 0) + 1
                        )
                        snapshot_allocations.append((selected_snapshot, marking_code))
                else:
                    snapshot_allocations = [(snapshot, "") for snapshot in qs]
                if not snapshot_allocations:
                    raise ValueError(f"РќРµ РЅР°Р№РґРµРЅ С‚РѕРІР°СЂ {barcode} РІ РєРѕСЂРѕР±Рµ {box_code}.")
                for snapshot, scanned_marking_code in snapshot_allocations:
                    if remaining <= 0:
                        break
                    is_receiving_source = (
                        snapshot.warehouse_state_code == WarehouseStateCode.PLACED_IN_RECEIVING.value
                    )
                    if is_receiving_source:
                        source_zone = str(getattr(snapshot.location, "zone_code", "") or "").strip().upper()
                        if not allow_receiving_source or source_zone != "PR":
                            raise ValueError("Partial shipping pick from this receiving source is not allowed.")
                        if int(snapshot.processing_reserved_qty or 0) > 0 or int(snapshot.shipping_reserved_qty or 0) > 0:
                            raise ValueError("Receiving source has an active reserve and cannot be picked for shipping.")
                        active_operation = snapshot.active_operation
                        if active_operation and active_operation.status not in _FINAL_OPERATION_STATUSES:
                            raise ValueError("Receiving source has an active warehouse operation.")
                    reserved_qty = int(snapshot.shipping_reserved_qty or 0)
                    source_available_qty = reserved_qty if reserved_qty > 0 else int(snapshot.available_qty or 0)
                    if source_available_qty <= 0:
                        continue
                    take_qty = (
                        1
                        if requires_marking
                        else min(remaining, source_available_qty, int(snapshot.qty or 0))
                    )
                    if take_qty <= 0:
                        continue
                    source_location = snapshot.location
                    stored_source_marking_code = str(snapshot.marking_code or "").strip()
                    source_event = WarehouseEvent.objects.create(
                        agency=agency,
                        event_type=WarehouseEventType.MOVEMENT_COMPLETED.value,
                        stock_context_type="shipping",
                        stock_context_id=order_key,
                        container=snapshot.container,
                        from_location=source_location,
                        to_location=source_location,
                        from_zone_code=snapshot.zone_code,
                        to_zone_code=snapshot.zone_code,
                        qty=take_qty,
                        performed_by=performed_by,
                        performed_by_role=cls._role_of(performed_by) or "reachtruck",
                        occurred_at=timezone.now(),
                        payload={
                            "partial_shipping_pick": True,
                            "source_box_code": box_code,
                            "source_pallet_code": pallet_code,
                            "marking_code": scanned_marking_code if requires_marking else "",
                            "stored_source_marking_code": stored_source_marking_code if requires_marking else "",
                            "marking_scan_recorded": bool(requires_marking),
                        },
                    )
                    snapshot.qty = max(int(snapshot.qty or 0) - take_qty, 0)
                    snapshot.shipping_reserved_qty = 0
                    snapshot.available_qty = max(
                        int(snapshot.qty or 0)
                        - int(snapshot.processing_reserved_qty or 0)
                        - int(snapshot.other_reserved_qty or 0),
                        0,
                    )
                    if snapshot.qty <= 0:
                        snapshot.available_qty = 0
                        snapshot.is_archived = True
                    elif (
                        requires_marking
                        and marking_code_identity(stored_source_marking_code)
                        == marking_code_identity(scanned_marking_code)
                    ):
                        # The scanned code moved to the new unit in OTG. Do not
                        # leave the same active code on an aggregated source row.
                        snapshot.marking_code = ""
                    if int(snapshot.shipping_reserved_qty or 0) <= 0:
                        snapshot.warehouse_state_code = cls._state_for_location(source_location) if source_location else WarehouseStateCode.STORED.value
                    snapshot.last_event = source_event
                    snapshot.save(
                        update_fields=[
                            "qty",
                            "shipping_reserved_qty",
                            "available_qty",
                            "warehouse_state_code",
                            "is_archived",
                            "marking_code",
                            "last_event",
                            "updated_at",
                        ]
                    )

                    arrived_event = WarehouseEvent.objects.create(
                        agency=agency,
                        event_type=WarehouseEventType.OTG_ARRIVED.value,
                        stock_context_type="shipping",
                        stock_context_id=order_key,
                        from_location=source_location,
                        to_location=destination,
                        from_zone_code=str(getattr(source_location, "zone_code", "") or ""),
                        to_zone_code=destination.zone_code,
                        qty=take_qty,
                        performed_by=performed_by,
                        performed_by_role=cls._role_of(performed_by) or "reachtruck",
                        occurred_at=timezone.now(),
                        payload={
                            "partial_shipping_pick": True,
                            "source_snapshot_id": snapshot.id,
                            "source_box_code": box_code,
                            "source_pallet_code": pallet_code,
                            "marking_code": scanned_marking_code if requires_marking else "",
                            "stored_source_marking_code": stored_source_marking_code if requires_marking else "",
                            "marking_scan_recorded": bool(requires_marking),
                        },
                    )
                    loose_snapshot = WarehouseStockSnapshot.objects.create(
                        agency=agency,
                        stock_unit_type=snapshot.stock_unit_type or "item",
                        source_context_type="shipping",
                        source_context_id=order_key,
                        sku_ref=snapshot.sku_ref,
                        sku_code=snapshot.sku_code,
                        name=snapshot.name,
                        size=snapshot.size,
                        barcode=snapshot.barcode,
                        goods_type=snapshot.goods_type,
                        marking_code=scanned_marking_code if requires_marking else snapshot.marking_code,
                        qty=take_qty,
                        available_qty=0,
                        processing_reserved_qty=0,
                        shipping_reserved_qty=0,
                        other_reserved_qty=0,
                        container=None,
                        container_code="",
                        parent_container=None,
                        location=destination,
                        zone_code=destination.zone_code,
                        zone_kind=destination.zone_kind,
                        warehouse_state_code=WarehouseStateCode.IN_OTG.value,
                        last_event=arrived_event,
                    )
                    cls._mark_shipping_reserve_arrived_to_otg(snapshot=loose_snapshot, order_id=order_key)
                    created_snapshot_ids.append(int(loose_snapshot.id))
                    remaining -= take_qty
                if remaining > 0:
                    raise ValueError(f"Р’ РєРѕСЂРѕР±Рµ {box_code} РЅРµ С…РІР°С‚Р°РµС‚ РґРѕСЃС‚СѓРїРЅРѕРіРѕ С‚РѕРІР°СЂР° {barcode}.")

        if not created_snapshot_ids:
            raise ValueError("РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕР·РґР°С‚СЊ С€С‚СѓС‡РЅС‹Р№ С‚РѕРІР°СЂ РІ OTG.")
        cls._archive_empty_shipping_source_containers(
            agency=agency,
            box_codes=[
                row.get("box_code")
                for row in picked_rows or []
                if isinstance(row, dict)
            ],
            pallet_code=pallet_code,
        )
        return created_snapshot_ids

    @classmethod
    @transaction.atomic
    def start_palletization(
        cls,
        *,
        agency: Agency,
        order_id: str,
        box_codes: list[str] | None = None,
        started_by=None,
        started_by_role: str = "storekeeper",
    ) -> WarehouseOperation:
        order_key = str(order_id or "").strip()
        snapshots_qs = WarehouseStockSnapshot.objects.select_for_update(of=("self",)).filter(
            agency=agency,
            warehouse_state_code=WarehouseStateCode.IN_OTG.value,
            is_archived=False,
        )
        context_filter = Q(
            active_operation__context_type="shipping",
            active_operation__context_id=order_key,
        ) | Q(
            last_event__stock_context_type="shipping",
            last_event__stock_context_id=order_key,
        )
        snapshots = list(snapshots_qs.filter(context_filter).order_by("id"))
        normalized_box_codes = {
            str(code or "").strip().lower()
            for code in (box_codes or [])
            if str(code or "").strip()
        }
        if not snapshots and normalized_box_codes:
            exact_candidates = snapshots_qs.select_related("container").order_by("id")
            snapshots = [
                snapshot
                for snapshot in exact_candidates
                if str(snapshot.container_code or "").strip().lower() in normalized_box_codes
                or str(getattr(snapshot.container, "container_code", "") or "").strip().lower()
                in normalized_box_codes
            ]
        if not snapshots:
            legacy_snapshots = snapshots_qs.select_related("active_operation", "last_event").order_by("id")
            snapshots = [
                snapshot
                for snapshot in legacy_snapshots
                if cls._snapshot_matches_shipping_context(snapshot, order_key)
            ]
        if not snapshots:
            raise ValueError("No snapshots in OTG ready for palletization")
        operation = WarehouseOperation.objects.create(
            agency_id=agency.pk,
            operation_type=WarehouseOperation.TYPE_PALLETIZATION,
            context_type="shipping",
            context_id=order_key,
            source_location_id=snapshots[0].location_id,
            destination_location_id=snapshots[0].location_id,
            source_zone_code=snapshots[0].zone_code,
            destination_zone_code=snapshots[0].zone_code,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
            requested_by=started_by,
            requested_by_role=started_by_role,
            assigned_executor_role="storekeeper",
            planned_qty=sum(cls._shipping_snapshot_flow_qty(snapshot) for snapshot in snapshots),
            started_at=timezone.now(),
        )
        now = timezone.now()
        performed_role = started_by_role or cls._role_of(started_by)
        transition = WarehouseTransitionService.apply_event(
            WarehouseStateCode.IN_OTG.value,
            WarehouseEventType.PALLETIZATION_STARTED,
        )
        events: list[WarehouseEvent] = []
        for snapshot in snapshots:
            events.append(
                WarehouseEvent(
                    agency_id=snapshot.agency_id,
                    event_type=WarehouseEventType.PALLETIZATION_STARTED.value,
                    stock_context_type="shipping",
                    stock_context_id=order_key,
                    container_id=snapshot.container_id,
                    operation=operation,
                    from_location_id=snapshot.location_id,
                    to_location_id=snapshot.location_id,
                    from_zone_code=snapshot.zone_code,
                    to_zone_code=snapshot.zone_code,
                    qty=cls._shipping_snapshot_flow_qty(snapshot),
                    performed_by=started_by,
                    performed_by_role=performed_role,
                    occurred_at=now,
                )
            )
            snapshot.warehouse_state_code = transition.code.value
            snapshot.active_operation = operation
            snapshot.active_operation_type = operation.operation_type
            snapshot.updated_at = now
        WarehouseEvent.objects.bulk_create(events, batch_size=500)
        for snapshot, event in zip(snapshots, events):
            snapshot.last_event = event
        WarehouseStockSnapshot.objects.bulk_update(
            snapshots,
            ["last_event"],
            batch_size=500,
        )
        WarehouseStockSnapshot.objects.filter(id__in=[snapshot.id for snapshot in snapshots]).update(
            warehouse_state_code=transition.code.value,
            active_operation=operation,
            active_operation_type=operation.operation_type,
            updated_at=now,
        )
        return operation

    @classmethod
    @transaction.atomic
    def complete_palletization(
        cls,
        *,
        operation: WarehouseOperation,
        performed_by=None,
        performed_by_role: str = "storekeeper",
    ) -> WarehouseOperation:
        if operation.operation_type != WarehouseOperation.TYPE_PALLETIZATION:
            raise ValueError("Only palletization operations can be completed by this write-path")
        from shipping.models import ShippingOrder

        order_key = str(operation.context_id or "").strip()
        shipping_order = (
            ShippingOrder.objects.select_for_update(of=("self",))
            .filter(agency=operation.agency, number=order_key)
            .only("status")
            .first()
        )
        if shipping_order is None:
            raise ValueError("Заявка отгрузки для паллетизации не найдена.")
        total_done = 0
        now = timezone.now()
        performed_role = performed_by_role or cls._role_of(performed_by)
        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("agency", "container", "parent_container", "location")
            .filter(active_operation=operation, is_archived=False)
            .order_by("id")
        )
        cls.validate_shipping_pallet_ownership(
            agency=operation.agency,
            order_id=operation.context_id,
            snapshots=snapshots,
            require_pallets=True,
            lock=True,
        )
        events: list[WarehouseEvent] = []
        target_state_codes: set[str] = set()
        for snapshot in snapshots:
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.PALLETIZATION_COMPLETED,
            )
            flow_qty = cls._shipping_snapshot_flow_qty(snapshot)
            events.append(
                WarehouseEvent(
                    agency=snapshot.agency,
                    event_type=WarehouseEventType.PALLETIZATION_COMPLETED.value,
                    stock_context_type="shipping",
                    stock_context_id=operation.context_id,
                    container=snapshot.container,
                    operation=operation,
                    from_location=snapshot.location,
                    to_location=snapshot.location,
                    from_zone_code=snapshot.zone_code,
                    to_zone_code=snapshot.zone_code,
                    qty=flow_qty,
                    performed_by=performed_by,
                    performed_by_role=performed_role,
                    occurred_at=now,
                )
            )
            snapshot.warehouse_state_code = transition.code.value
            target_state_codes.add(transition.code.value)
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
            snapshot.updated_at = now
            total_done += flow_qty
        if events:
            WarehouseEvent.objects.bulk_create(events, batch_size=500)
            for snapshot, event in zip(snapshots, events):
                snapshot.last_event = event
            if len(target_state_codes) == 1:
                WarehouseStockSnapshot.objects.bulk_update(
                    snapshots,
                    ["last_event"],
                    batch_size=500,
                )
                WarehouseStockSnapshot.objects.filter(id__in=[snapshot.id for snapshot in snapshots]).update(
                    warehouse_state_code=next(iter(target_state_codes)),
                    active_operation=None,
                    active_operation_type="",
                    updated_at=now,
                )
            else:
                WarehouseStockSnapshot.objects.bulk_update(
                    snapshots,
                    [
                        "warehouse_state_code",
                        "active_operation",
                        "active_operation_type",
                        "last_event",
                        "updated_at",
                    ],
                    batch_size=500,
                )
        operation.status = WarehouseOperation.STATUS_DONE
        operation.done_qty = total_done
        operation.completed_at = timezone.now()
        operation.save(update_fields=["status", "done_qty", "completed_at", "updated_at"])
        return operation

    @classmethod
    @transaction.atomic
    def assign_to_trip(
        cls,
        *,
        agency: Agency,
        order_id: str,
        trip_id: str,
        assigned_by=None,
        assigned_by_role: str = "logistician",
    ) -> list[int]:
        order_key = str(order_id or "").strip()
        trip_key = str(trip_id or "").strip()
        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("active_operation", "last_event", "container", "parent_container")
            .filter(
                agency=agency,
                warehouse_state_code=WarehouseStateCode.READY_FOR_LOADING.value,
                is_archived=False,
            ).order_by("id")
        )
        snapshots = [snapshot for snapshot in snapshots if cls._snapshot_matches_shipping_context(snapshot, order_key)]
        if not snapshots:
            raise ValueError("No snapshots ready for trip assignment")
        cls.validate_shipping_pallet_ownership(
            agency=agency,
            order_id=order_key,
            snapshots=snapshots,
            require_pallets=True,
            lock=True,
        )
        event_ids: list[int] = []
        for snapshot in snapshots:
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.ASSIGNED_TO_TRIP,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.ASSIGNED_TO_TRIP.value,
                stock_context_type="shipping",
                stock_context_id=order_key,
                container=snapshot.container,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code=snapshot.zone_code,
                qty=cls._shipping_snapshot_flow_qty(snapshot),
                performed_by=assigned_by,
                performed_by_role=assigned_by_role or cls._role_of(assigned_by),
                occurred_at=timezone.now(),
                payload={"trip_id": trip_key},
            )
            snapshot.warehouse_state_code = transition.code.value
            snapshot.current_trip_id = trip_key
            snapshot.last_event = event
            snapshot.save(update_fields=["warehouse_state_code", "current_trip_id", "last_event", "updated_at"])
            event_ids.append(event.id)
        return event_ids

    @classmethod
    @transaction.atomic
    def start_loading(
        cls,
        *,
        agency: Agency,
        order_id: str,
        trip_id: str,
        started_by=None,
        started_by_role: str = "logistician",
    ) -> WarehouseOperation:
        order_key = str(order_id or "").strip()
        trip_key = str(trip_id or "").strip()
        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("active_operation", "last_event", "container", "parent_container")
            .filter(
                agency=agency,
                warehouse_state_code=WarehouseStateCode.ASSIGNED_TO_TRIP.value,
                current_trip_id=trip_key,
                is_archived=False,
            ).order_by("id")
        )
        snapshots = [snapshot for snapshot in snapshots if cls._snapshot_matches_shipping_context(snapshot, order_key)]
        if not snapshots:
            raise ValueError("No assigned snapshots ready for loading")
        cls.validate_shipping_pallet_ownership(
            agency=agency,
            order_id=order_key,
            snapshots=snapshots,
            require_pallets=True,
            lock=True,
        )
        operation = WarehouseOperation.objects.create(
            agency=agency,
            operation_type=WarehouseOperation.TYPE_LOAD_TO_VEHICLE,
            context_type="shipping",
            context_id=order_key,
            source_location=snapshots[0].location,
            destination_location=snapshots[0].location,
            source_zone_code=snapshots[0].zone_code,
            destination_zone_code="VEH",
            status=WarehouseOperation.STATUS_IN_PROGRESS,
            requested_by=started_by,
            requested_by_role=started_by_role,
            assigned_executor_role="logistician",
            planned_qty=sum(cls._shipping_snapshot_flow_qty(snapshot) for snapshot in snapshots),
            started_at=timezone.now(),
            comment=f"trip:{trip_key}",
        )
        now = timezone.now()
        for snapshot in snapshots:
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.LOADING_STARTED,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.LOADING_STARTED.value,
                stock_context_type="shipping",
                stock_context_id=order_key,
                container=snapshot.container,
                operation=operation,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code="VEH",
                qty=cls._shipping_snapshot_flow_qty(snapshot),
                performed_by=started_by,
                performed_by_role=started_by_role or cls._role_of(started_by),
                occurred_at=now,
                payload={"trip_id": trip_key},
            )
            snapshot.warehouse_state_code = transition.code.value
            snapshot.active_operation = operation
            snapshot.active_operation_type = operation.operation_type
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "warehouse_state_code",
                    "active_operation",
                    "active_operation_type",
                    "last_event",
                    "updated_at",
                ]
            )
        return operation

    @classmethod
    @transaction.atomic
    def complete_loading(
        cls,
        *,
        operation: WarehouseOperation,
        performed_by=None,
        performed_by_role: str = "logistician",
    ) -> WarehouseOperation:
        if operation.operation_type != WarehouseOperation.TYPE_LOAD_TO_VEHICLE:
            raise ValueError("Only load_to_vehicle operations can be completed by this write-path")
        total_done = 0
        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("active_operation", "last_event", "container", "parent_container")
            .filter(active_operation=operation, is_archived=False)
            .order_by("id")
        )
        cls.validate_shipping_pallet_ownership(
            agency=operation.agency,
            order_id=operation.context_id,
            snapshots=snapshots,
            require_pallets=True,
            lock=True,
        )
        for snapshot in snapshots:
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.LOADED_TO_VEHICLE,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.LOADED_TO_VEHICLE.value,
                stock_context_type="shipping",
                stock_context_id=operation.context_id,
                container=snapshot.container,
                operation=operation,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code="VEH",
                qty=cls._shipping_snapshot_flow_qty(snapshot),
                performed_by=performed_by,
                performed_by_role=performed_by_role or cls._role_of(performed_by),
                occurred_at=timezone.now(),
                payload={"trip_id": snapshot.current_trip_id},
            )
            snapshot.warehouse_state_code = transition.code.value
            snapshot.is_in_vehicle = True
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "warehouse_state_code",
                    "is_in_vehicle",
                    "active_operation",
                    "active_operation_type",
                    "last_event",
                    "updated_at",
                ]
            )
            total_done += cls._shipping_snapshot_flow_qty(snapshot)
        operation.status = WarehouseOperation.STATUS_DONE
        operation.done_qty = total_done
        operation.completed_at = timezone.now()
        operation.save(update_fields=["status", "done_qty", "completed_at", "updated_at"])
        return operation

    @classmethod
    @transaction.atomic
    def ship_order_without_trip(
        cls,
        *,
        agency: Agency,
        order_id: str,
        performed_by=None,
        performed_by_role: str = "logistician",
    ) -> list[int]:
        order_key = str(order_id or "").strip()
        if not order_key:
            raise ValueError("Shipping order id is required")

        direct_state_codes = [
            WarehouseStateCode.IN_OTG.value,
            WarehouseStateCode.PALLETIZING.value,
            WarehouseStateCode.READY_FOR_LOADING.value,
            WarehouseStateCode.ASSIGNED_TO_TRIP.value,
            WarehouseStateCode.LOADING_IN_PROGRESS.value,
            WarehouseStateCode.LOADED_TO_VEHICLE.value,
        ]
        snapshots_qs = (
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("active_operation", "last_event", "container", "location")
            .filter(agency=agency, is_archived=False)
            .order_by("id")
        )
        snapshots = list(
            snapshots_qs.filter(
                warehouse_state_code__in=direct_state_codes,
                last_event__stock_context_type="shipping",
                last_event__stock_context_id=order_key,
            )
        )
        if not snapshots:
            snapshots = [
                snapshot
                for snapshot in snapshots_qs.filter(shipping_reserved_qty__gt=0)
                if cls._snapshot_matches_shipping_context(snapshot, order_key)
            ]
        if not snapshots:
            raise ValueError(
                "Для прямой отгрузки без рейса не найден зарезервированный товар в складском контуре."
            )
        cls._assert_shipping_does_not_take_fbs_stock(snapshots)

        reserves = list(
            WarehouseReserve.objects.select_for_update()
            .filter(
                agency=agency,
                reserve_type=WarehouseReserve.TYPE_SHIPPING,
                context_type="shipping",
                context_id=order_key,
                status__in=[
                    WarehouseReserve.STATUS_ACTIVE,
                    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
                    WarehouseReserve.STATUS_ALLOCATED,
                    WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
                    WarehouseReserve.STATUS_SATISFIED,
                ],
            )
            .order_by("id")
        )
        reserves_by_identity: dict[tuple[str, str, str, str], list[WarehouseReserve]] = {}
        for reserve in reserves:
            reserves_by_identity.setdefault(cls._shipping_reserve_identity(reserve), []).append(reserve)

        event_ids: list[int] = []
        for snapshot in snapshots:
            shipped_qty = cls._shipping_snapshot_flow_qty(snapshot)
            if shipped_qty <= 0:
                continue
            matching_reserves = reserves_by_identity.get(cls._shipping_reserve_identity(snapshot), [])
            reserve = matching_reserves[0] if matching_reserves else None
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.SHIPPED.value,
                stock_context_type="shipping",
                stock_context_id=order_key,
                container=snapshot.container,
                reserve=reserve,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code=snapshot.zone_code,
                qty=shipped_qty,
                performed_by=performed_by,
                performed_by_role=performed_by_role or cls._role_of(performed_by),
                occurred_at=timezone.now(),
                payload={
                    "trip_id": str(snapshot.current_trip_id or "").strip(),
                    "direct_without_linked_trip": True,
                },
            )
            remaining_qty = max(int(snapshot.qty or 0) - shipped_qty, 0)
            snapshot.qty = remaining_qty
            snapshot.shipping_reserved_qty = 0
            snapshot.available_qty = max(
                remaining_qty
                - int(snapshot.processing_reserved_qty or 0)
                - int(snapshot.shipping_reserved_qty or 0),
                0,
            )
            if remaining_qty <= 0:
                snapshot.warehouse_state_code = WarehouseStateCode.SHIPPED.value
                snapshot.is_archived = True
            elif int(snapshot.processing_reserved_qty or 0) > 0:
                snapshot.warehouse_state_code = WarehouseStateCode.RESERVED_FOR_PROCESSING.value
                snapshot.is_archived = False
            elif str(snapshot.zone_code or "").strip().upper() == "PR":
                snapshot.warehouse_state_code = WarehouseStateCode.PLACED_IN_RECEIVING.value
                snapshot.is_archived = False
            elif str(snapshot.zone_code or "").strip().upper() == "OTG":
                snapshot.warehouse_state_code = WarehouseStateCode.IN_OTG.value
                snapshot.is_archived = False
            else:
                snapshot.warehouse_state_code = WarehouseStateCode.STORED.value
                snapshot.is_archived = False
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "qty",
                    "shipping_reserved_qty",
                    "available_qty",
                    "warehouse_state_code",
                    "is_archived",
                    "last_event",
                    "updated_at",
                ]
            )
            event_ids.append(event.id)
        if not event_ids:
            raise ValueError("Для прямой отгрузки не найдено положительное количество товара.")
        return event_ids

    @classmethod
    @transaction.atomic
    def ship_order(
        cls,
        *,
        agency: Agency,
        order_id: str,
        trip_id: str,
        performed_by=None,
        performed_by_role: str = "logistician",
    ) -> list[int]:
        order_key = str(order_id or "").strip()
        trip_key = str(trip_id or "").strip()
        snapshot_context_filter = (
            Q(active_operation__context_type="shipping", active_operation__context_id=order_key)
            | Q(last_event__stock_context_type="shipping", last_event__stock_context_id=order_key)
        )
        snapshots_qs = (
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("active_operation", "last_event", "container", "location")
            .filter(
                agency=agency,
                warehouse_state_code=WarehouseStateCode.LOADED_TO_VEHICLE.value,
                current_trip_id=trip_key,
                is_archived=False,
            )
            .order_by("id")
        )
        snapshots = list(snapshots_qs.filter(snapshot_context_filter))
        if not snapshots:
            snapshots = [snapshot for snapshot in snapshots_qs if cls._snapshot_matches_shipping_context(snapshot, order_key)]
        if not snapshots:
            raise ValueError("No loaded snapshots ready for shipping")
        cls._assert_shipping_does_not_take_fbs_stock(snapshots)
        cls.validate_shipping_pallet_ownership(
            agency=agency,
            order_id=order_key,
            snapshots=snapshots,
            require_pallets=True,
            lock=True,
        )
        reserves = list(
            WarehouseReserve.objects.select_for_update()
            .filter(
                agency=agency,
                reserve_type=WarehouseReserve.TYPE_SHIPPING,
                context_type="shipping",
                context_id=order_key,
                status__in=[
                    WarehouseReserve.STATUS_ACTIVE,
                    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
                    WarehouseReserve.STATUS_ALLOCATED,
                    WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
                    WarehouseReserve.STATUS_SATISFIED,
                ],
            )
            .order_by("id")
        )
        reserves_by_identity = cls._validated_shipping_reserves_for_ship(
            snapshots=snapshots,
            reserves=reserves,
        )
        event_ids: list[int] = []
        for snapshot in snapshots:
            shipped_qty = cls._shipping_snapshot_flow_qty(snapshot)
            matching_reserves = reserves_by_identity.get(cls._shipping_reserve_identity(snapshot), [])
            reserve = matching_reserves[0] if matching_reserves else None
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.SHIPPED,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.SHIPPED.value,
                stock_context_type="shipping",
                stock_context_id=order_key,
                container=snapshot.container,
                reserve=reserve,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code="VEH",
                qty=shipped_qty,
                performed_by=performed_by,
                performed_by_role=performed_by_role or cls._role_of(performed_by),
                occurred_at=timezone.now(),
                payload={"trip_id": trip_key},
            )
            snapshot.shipping_reserved_qty = 0
            snapshot.available_qty = 0
            snapshot.warehouse_state_code = transition.code.value
            snapshot.is_archived = True
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "shipping_reserved_qty",
                    "available_qty",
                    "warehouse_state_code",
                    "is_archived",
                    "last_event",
                    "updated_at",
                ]
            )
            event_ids.append(event.id)
        return event_ids

    @classmethod
    def ensure_location(
        cls,
        *,
        warehouse_code: str = "MSK",
        zone_code: str,
        row_no: int = 0,
        section_no: int = 0,
        tier_no: int = 0,
        cell_no: int = 0,
    ) -> WarehouseLocation:
        zone = str(zone_code or "").strip().upper()
        kind = ZONE_KIND_BY_CODE.get(zone, WarehouseLocation.ZONE_KIND_VIRTUAL)
        defaults = {
            "zone_kind": kind,
            "location_code": cls._location_code(zone, row_no, section_no, tier_no, cell_no),
            "display_name": cls._location_display_name(zone, row_no, section_no, tier_no, cell_no),
            "is_active": True,
            "is_pickable": kind in {WarehouseLocation.ZONE_KIND_STORAGE, WarehouseLocation.ZONE_KIND_SHIPPING},
            "is_storage": kind == WarehouseLocation.ZONE_KIND_STORAGE,
            "is_processing": kind == WarehouseLocation.ZONE_KIND_PROCESSING,
            "is_shipping": kind == WarehouseLocation.ZONE_KIND_SHIPPING,
            "is_loading": kind == WarehouseLocation.ZONE_KIND_LOADING,
        }
        lookup = {
            "warehouse_code": warehouse_code,
            "zone_code": zone,
            "row_no": max(int(row_no or 0), 0),
            "section_no": max(int(section_no or 0), 0),
            "tier_no": max(int(tier_no or 0), 0),
            "cell_no": max(int(cell_no or 0), 0),
        }
        try:
            location, _ = WarehouseLocation.objects.get_or_create(
                **lookup,
                defaults=defaults,
            )
        except WarehouseLocation.MultipleObjectsReturned:
            # Legacy warehouse data may contain duplicate virtual locations.
            # Prefer the canonical (oldest) one instead of failing receiving.
            location = WarehouseLocation.objects.filter(**lookup).order_by("id").first()
            if location is None:
                raise
        if zone == "OS" and all(
            int(value or 0) > 0
            for value in (location.row_no, location.section_no, location.tier_no, location.cell_no)
        ):
            from .shared_location_catalog import ensure_shared_fbs_storage_cell

            ensure_shared_fbs_storage_cell(location)
        return location

    @classmethod
    def _resolve_container(
        cls,
        *,
        agency: Agency,
        item: dict,
        current_location: WarehouseLocation,
        performed_by=None,
        container_cache: dict[str, WarehouseContainer] | None = None,
        containers_to_update: list[WarehouseContainer] | None = None,
    ):
        pallet_code = str(item.get("pallet_code") or "").strip()
        box_code = str(item.get("box_code") or "").strip()
        order_key = str(item.get("order_id") or "").strip()
        if not pallet_code and not box_code:
            return None

        def ensure_container(
            *,
            container_code: str,
            container_type: str,
            parent_container: WarehouseContainer | None = None,
        ) -> WarehouseContainer:
            cache_key = str(container_code or "").strip()
            container = container_cache.get(cache_key) if container_cache is not None else None
            if container is None:
                if container_cache is None:
                    container, _ = WarehouseContainer.objects.get_or_create(
                        agency=agency,
                        container_code=container_code,
                        defaults={
                            "container_type": container_type,
                            "parent_container": parent_container,
                            "current_location": current_location,
                            "created_by": performed_by,
                            "source_context_type": "receiving",
                            "source_context_id": order_key,
                        },
                    )
                else:
                    container = WarehouseContainer.objects.create(
                        agency=agency,
                        container_code=container_code,
                        container_type=container_type,
                        parent_container=parent_container,
                        current_location=current_location,
                        created_by=performed_by,
                        source_context_type="receiving",
                        source_context_id=order_key,
                    )
                    container_cache[cache_key] = container
            update_fields: list[str] = []
            if container.current_location_id != current_location.id:
                container.current_location = current_location
                update_fields.append("current_location")
            if parent_container is not None and container.parent_container_id != parent_container.id:
                container.parent_container = parent_container
                update_fields.append("parent_container")
            if not container.source_context_type:
                container.source_context_type = "receiving"
                update_fields.append("source_context_type")
            if order_key and not container.source_context_id:
                container.source_context_id = order_key
                update_fields.append("source_context_id")
            if update_fields:
                container.updated_at = timezone.now()
                if containers_to_update is not None:
                    containers_to_update.append(container)
                else:
                    container.save(update_fields=[*update_fields, "updated_at"])
            return container

        pallet_container = None
        if pallet_code:
            pallet_container = ensure_container(
                container_code=pallet_code,
                container_type=WarehouseContainer.TYPE_PALLET,
            )
        if box_code:
            return ensure_container(
                container_code=box_code,
                container_type=WarehouseContainer.TYPE_BOX,
                parent_container=pallet_container,
            )
        return pallet_container

    @staticmethod
    def _resolve_sku_ref(*, agency: Agency, item: dict, sku_ref_cache: dict[tuple[str, str], SKU] | None = None):
        sku_ref = item.get("sku_ref")
        if isinstance(sku_ref, SKU):
            return sku_ref
        barcode = WarehouseWritePathService._normalize_reserve_lookup_text(item.get("barcode"))
        if barcode:
            if sku_ref_cache is not None:
                sku_ref = sku_ref_cache.get(("code", barcode))
                if sku_ref is not None:
                    return sku_ref
            sku_ref = SKU.objects.filter(agency=agency, code=barcode, deleted=False).order_by("id").first()
            if sku_ref is not None:
                return sku_ref
        sku_code = str(item.get("sku_code") or item.get("sku") or "").strip()
        if not sku_code:
            return None
        if sku_ref_cache is not None:
            sku_ref = sku_ref_cache.get(("sku", sku_code))
            if sku_ref is not None:
                return sku_ref
            return None
        return SKU.objects.filter(agency=agency, sku_code=sku_code, deleted=False).first()

    @staticmethod
    def _snapshot_matches_processing_context(snapshot: WarehouseStockSnapshot, order_id: str) -> bool:
        return WarehouseReserve.objects.filter(
            agency=snapshot.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            status__in=[
                WarehouseReserve.STATUS_ACTIVE,
                WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
                WarehouseReserve.STATUS_ALLOCATED,
                WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
                WarehouseReserve.STATUS_SATISFIED,
            ],
        ).exists()

    @staticmethod
    def _normalize_reserve_lookup_text(value) -> str:
        text = str(value or "").strip()
        if text in {"-", "вЂ“", "вЂ”"}:
            return ""
        return text

    @staticmethod
    def _filter_reserve_snapshot_identity(qs, *, sku_code: str, size: str, barcode: str, goods_type: str):
        if barcode:
            return qs.filter(barcode=barcode)
        return qs.filter(
            sku_code=sku_code,
            size=size,
            goods_type=goods_type,
        )

    @staticmethod
    def _match_snapshot_for_processing_reserve(*, agency: Agency, item: dict, required_qty: int) -> WarehouseStockSnapshot:
        normalize = WarehouseWritePathService._normalize_reserve_lookup_text
        sku_code = str(item.get("sku_code") or item.get("sku") or "").strip()
        size = normalize(item.get("size"))
        barcode = normalize(item.get("barcode"))
        goods_type = normalize(item.get("goods_type"))
        qs = WarehouseStockSnapshot.objects.filter(
            agency=agency,
            warehouse_state_code=WarehouseStateCode.STORED.value,
            is_archived=False,
        )
        qs = WarehouseWritePathService._filter_reserve_snapshot_identity(
            qs,
            sku_code=sku_code,
            size=size,
            barcode=barcode,
            goods_type=goods_type,
        ).order_by("id")
        for snapshot in qs:
            if int(snapshot.available_qty or 0) >= required_qty:
                return snapshot
        raise ValueError(f"No stored snapshot with enough available qty for {sku_code}")

    @staticmethod
    def _match_snapshots_for_processing_reserve(
        *,
        agency: Agency,
        item: dict,
        required_qty: int,
    ) -> list[tuple[WarehouseStockSnapshot, int]]:
        normalize = WarehouseWritePathService._normalize_reserve_lookup_text
        sku_code = str(item.get("sku_code") or item.get("sku") or "").strip()
        size = normalize(item.get("size"))
        barcode = normalize(item.get("barcode"))
        goods_type = normalize(item.get("goods_type"))
        box_codes = WarehouseWritePathService._normalize_shipping_box_codes(item)
        allow_partial_box_reserve = bool(item.get("allow_partial_box_reserve"))
        remaining_qty = max(int(required_qty or 0), 0)
        allocations: list[tuple[WarehouseStockSnapshot, int]] = []
        qs = WarehouseStockSnapshot.objects.select_for_update().filter(
            agency=agency,
            warehouse_state_code__in=[
                WarehouseStateCode.STORED.value,
                WarehouseStateCode.RESERVED_FOR_PROCESSING.value,
            ],
            is_archived=False,
        )
        qs = WarehouseWritePathService._filter_reserve_snapshot_identity(
            qs,
            sku_code=sku_code,
            size=size,
            barcode=barcode,
            goods_type=goods_type,
        ).order_by("id")
        shipping_box_codes = shipping_reserved_box_codes(agency=agency, box_codes=box_codes)
        if box_codes:
            shipping_keys = {
                str(code or "").strip().casefold()
                for code in shipping_box_codes
                if str(code or "").strip()
            }
            conflicts = [
                code for code in box_codes
                if str(code or "").strip().casefold() in shipping_keys
            ]
            if conflicts:
                shown = ", ".join(conflicts[:5])
                if len(conflicts) > 5:
                    shown = f"{shown} и еще {len(conflicts) - 5}"
                raise ValueError(
                    f"Выбранные короба уже зарезервированы для отгрузки: {shown}"
                )
        if shipping_box_codes:
            shipping_container_ids = list(
                WarehouseContainer.objects.filter(
                    agency=agency,
                    container_code__in=shipping_box_codes,
                ).values_list("id", flat=True)
            )
            qs = qs.exclude(
                Q(container_code__in=shipping_box_codes)
                | Q(container_id__in=shipping_container_ids)
            )
        if box_codes:
            container_ids = list(
                WarehouseContainer.objects.filter(
                    agency=agency,
                    container_code__in=box_codes,
                ).values_list("id", flat=True)
            )
            qs = qs.filter(
                Q(container_code__in=box_codes)
                | Q(container_id__in=container_ids)
            )
            total_available = sum(int(snapshot.available_qty or 0) for snapshot in qs)
            if total_available < remaining_qty:
                raise ValueError(
                    f"Selected boxes do not contain enough available qty for processing reserve: {sku_code}"
                )
            if total_available > remaining_qty and not allow_partial_box_reserve:
                raise ValueError(
                    f"Selected boxes contain more qty than requested for processing reserve: {sku_code}"
                )
        for snapshot in qs:
            available_qty = int(snapshot.available_qty or 0)
            if available_qty <= 0:
                continue
            reserved_qty = min(available_qty, remaining_qty)
            allocations.append((snapshot, reserved_qty))
            remaining_qty -= reserved_qty
            if remaining_qty <= 0:
                return allocations
        raise ValueError(f"No stored snapshots with enough available qty for {sku_code}")

    @staticmethod
    def _snapshot_matches_shipping_context(snapshot: WarehouseStockSnapshot, order_id: str) -> bool:
        order_key = str(order_id or "").strip()
        if not order_key:
            return False
        cached_context_ids = getattr(snapshot, "_shipping_context_ids_cache", None)
        if cached_context_ids is not None:
            return order_key in cached_context_ids
        active_operation = getattr(snapshot, "active_operation", None)
        if (
            active_operation is not None
            and str(active_operation.context_type or "").strip().lower() == "shipping"
        ):
            return str(active_operation.context_id or "").strip() == order_key
        last_event = getattr(snapshot, "last_event", None)
        if (
            last_event is not None
            and str(last_event.stock_context_type or "").strip().lower() == "shipping"
        ):
            return str(last_event.stock_context_id or "").strip() == order_key
        return WarehouseReserve.objects.filter(
            agency=snapshot.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id=order_key,
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            status__in=[
                WarehouseReserve.STATUS_ACTIVE,
                WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
                WarehouseReserve.STATUS_ALLOCATED,
                WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
                WarehouseReserve.STATUS_SATISFIED,
            ],
        ).exists()

    @staticmethod
    def _match_snapshot_for_shipping_reserve(*, agency: Agency, item: dict, required_qty: int) -> WarehouseStockSnapshot:
        normalize = WarehouseWritePathService._normalize_reserve_lookup_text
        sku_code = str(item.get("sku_code") or item.get("sku") or "").strip()
        size = normalize(item.get("size"))
        barcode = normalize(item.get("barcode"))
        goods_type = normalize(item.get("goods_type"))
        qs = WarehouseStockSnapshot.objects.filter(
            agency=agency,
            warehouse_state_code__in=[
                WarehouseStateCode.PLACED_IN_RECEIVING.value,
                WarehouseStateCode.STORED.value,
                WarehouseStateCode.PLACED_AFTER_PROCESSING.value,
                WarehouseStateCode.IN_OTG.value,
                WarehouseStateCode.PALLETIZING.value,
                WarehouseStateCode.READY_FOR_LOADING.value,
            ],
            is_archived=False,
        )
        qs = WarehouseWritePathService._filter_reserve_snapshot_identity(
            qs,
            sku_code=sku_code,
            size=size,
            barcode=barcode,
            goods_type=goods_type,
        ).order_by("id")
        for snapshot in qs:
            if int(snapshot.available_qty or 0) >= required_qty:
                return snapshot
        raise ValueError(f"No snapshot with enough available qty for shipping reserve: {sku_code}")

    @staticmethod
    def _match_snapshots_for_shipping_reserve(
        *,
        agency: Agency,
        item: dict,
        required_qty: int,
    ) -> list[tuple[WarehouseStockSnapshot, int]]:
        normalize = WarehouseWritePathService._normalize_reserve_lookup_text
        sku_code = str(item.get("sku_code") or item.get("sku") or "").strip()
        size = normalize(item.get("size"))
        barcode = normalize(item.get("barcode"))
        goods_type = normalize(item.get("goods_type"))
        box_codes = WarehouseWritePathService._normalize_shipping_box_codes(item)
        allow_partial_box_reserve = bool(item.get("allow_partial_box_reserve"))
        remaining_qty = max(int(required_qty or 0), 0)
        allocations: list[tuple[WarehouseStockSnapshot, int]] = []
        qs = WarehouseStockSnapshot.objects.select_for_update().filter(
            agency=agency,
            warehouse_state_code__in=[
                WarehouseStateCode.PLACED_IN_RECEIVING.value,
                WarehouseStateCode.STORED.value,
                WarehouseStateCode.PLACED_AFTER_PROCESSING.value,
                WarehouseStateCode.RESERVED_FOR_SHIPPING.value,
                WarehouseStateCode.IN_OTG.value,
                WarehouseStateCode.PALLETIZING.value,
                WarehouseStateCode.READY_FOR_LOADING.value,
            ],
            is_archived=False,
        )
        qs = WarehouseWritePathService._filter_reserve_snapshot_identity(
            qs,
            sku_code=sku_code,
            size=size,
            barcode=barcode,
            goods_type=goods_type,
        ).order_by("id")
        if box_codes:
            container_ids = list(
                WarehouseContainer.objects.filter(
                    agency=agency,
                    container_code__in=box_codes,
                ).values_list("id", flat=True)
            )
            qs = qs.filter(
                Q(container_code__in=box_codes)
                | Q(container_id__in=container_ids)
            )
            total_available = sum(int(snapshot.available_qty or 0) for snapshot in qs)
            if total_available < remaining_qty:
                raise ValueError(
                    f"Selected boxes do not contain enough available qty for shipping reserve: {sku_code}"
                )
            if total_available > remaining_qty and not allow_partial_box_reserve:
                raise ValueError(
                    f"Selected boxes contain more qty than requested for shipping reserve: {sku_code}"
                )
        else:
            single_snapshot = None
            single_snapshot_overshoot = None
            for snapshot in qs:
                available_qty = int(snapshot.available_qty or 0)
                if available_qty < remaining_qty:
                    continue
                overshoot = available_qty - remaining_qty
                if (
                    single_snapshot is None
                    or single_snapshot_overshoot is None
                    or overshoot < single_snapshot_overshoot
                    or (overshoot == single_snapshot_overshoot and int(snapshot.id or 0) < int(single_snapshot.id or 0))
                ):
                    single_snapshot = snapshot
                    single_snapshot_overshoot = overshoot
            if single_snapshot is not None:
                return [(single_snapshot, remaining_qty)]
        for snapshot in qs:
            available_qty = int(snapshot.available_qty or 0)
            if available_qty <= 0:
                continue
            reserved_qty = min(available_qty, remaining_qty)
            allocations.append((snapshot, reserved_qty))
            remaining_qty -= reserved_qty
            if remaining_qty <= 0:
                return allocations
        raise ValueError(f"No snapshots with enough available qty for shipping reserve: {sku_code}")

    @staticmethod
    def _normalize_shipping_box_codes(item: dict) -> list[str]:
        raw_codes = item.get("box_codes") or item.get("box_code") or []
        if isinstance(raw_codes, str):
            raw_values = raw_codes.split(",")
        else:
            raw_values = list(raw_codes)
        codes: list[str] = []
        seen: set[str] = set()
        for raw_code in raw_values:
            code = str(raw_code or "").strip()
            normalized = code.lower()
            if not code or normalized in seen:
                continue
            seen.add(normalized)
            codes.append(code)
        return codes

    @classmethod
    @transaction.atomic
    def start_problem_box_return(
        cls,
        *,
        snapshot_id: int,
        expected_last_event_id: int,
        performed_by=None,
    ) -> WarehouseOperation:
        snapshot = (
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("agency", "container", "parent_container", "location", "last_event", "active_operation")
            .filter(id=int(snapshot_id))
            .first()
        )
        if snapshot is None:
            raise ValueError("Проблемный короб не найден.")
        if int(snapshot.last_event_id or 0) != int(expected_last_event_id or 0):
            raise ValueError("Состояние короба уже изменилось. Обновите страницу.")

        check = check_problem_box_snapshot(snapshot)
        if not check.is_problem:
            raise ValueError(check.reason)

        current_operation = snapshot.active_operation
        if (
            current_operation is not None
            and current_operation.operation_type == WarehouseOperation.TYPE_RETURN_TO_STORAGE
            and current_operation.context_type == PROBLEM_BOX_CONTEXT_TYPE
            and str(current_operation.context_id or "").strip() == str(snapshot.id)
            and current_operation.status in _PUTAWAY_DESTINATION_BLOCKING_STATUSES
        ):
            task = current_operation.tasks.filter(
                status__in=[
                    WarehouseOperationTask.STATUS_CREATED,
                    WarehouseOperationTask.STATUS_IN_PROGRESS,
                ]
            ).first()
            task_payload = dict(task.payload or {}) if task is not None else {}
            if task and str(task_payload.get("reachtruck_move_id") or "").strip():
                return current_operation
            raise ValueError("Короб уже взят в проверку другим сотрудником.")

        actor = performed_by if getattr(performed_by, "is_authenticated", False) else None
        actor_name = (
            getattr(actor, "get_full_name", lambda: "")()
            or getattr(actor, "username", "")
            or ""
        )
        payload = snapshot.last_event.payload if isinstance(snapshot.last_event.payload, dict) else {}
        shipping_document = str(
            payload.get("operation_context_id")
            or payload.get("shipping_order_id")
            or payload.get("order_id")
            or ""
        ).strip()
        operation = WarehouseOperation.objects.create(
            agency=snapshot.agency,
            operation_type=WarehouseOperation.TYPE_RETURN_TO_STORAGE,
            context_type=PROBLEM_BOX_CONTEXT_TYPE,
            context_id=str(snapshot.id),
            source_location=snapshot.location,
            source_document_type="shipping",
            source_document_id=shipping_document,
            source_zone_code=str(snapshot.zone_code or "").strip(),
            destination_zone_code="OS",
            status=WarehouseOperation.STATUS_PLANNED,
            requested_by=actor,
            requested_by_role=cls._role_of(actor) or "storekeeper",
            assigned_executor_role="reachtruck",
            planned_qty=int(snapshot.qty or 0),
            comment="Размещение проблемного короба из OTG в основной склад",
        )
        operation_task = WarehouseOperationTask.objects.create(
            operation=operation,
            task_type=WarehouseOperationTask.TYPE_BOX_MOVE,
            container=snapshot.container,
            from_location=snapshot.location,
            from_zone_code=str(snapshot.zone_code or "").strip(),
            to_zone_code="OS",
            qty_planned=int(snapshot.qty or 0),
            status=WarehouseOperationTask.STATUS_CREATED,
            executor_role="reachtruck",
            payload={
                "problem_box_return": True,
                "snapshot_id": int(snapshot.id),
                "box_code": snapshot_box_code(snapshot),
                "expected_last_event_id": int(snapshot.last_event_id or 0),
                "shipping_document": shipping_document,
            },
        )
        cls._enqueue_problem_box_reachtruck_move(
            operation=operation,
            operation_task=operation_task,
            snapshot=snapshot,
            requested_by=actor,
            requested_by_name=actor_name,
        )
        snapshot.active_operation = operation
        snapshot.active_operation_type = WarehouseOperation.TYPE_RETURN_TO_STORAGE
        snapshot.snapshot_version = int(snapshot.snapshot_version or 1) + 1
        snapshot.save(
            update_fields=[
                "active_operation",
                "active_operation_type",
                "snapshot_version",
                "updated_at",
            ]
        )
        return operation

    @classmethod
    def _enqueue_problem_box_reachtruck_move(
        cls,
        *,
        operation: WarehouseOperation,
        operation_task: WarehouseOperationTask,
        snapshot: WarehouseStockSnapshot,
        requested_by=None,
        requested_by_name: str = "",
    ) -> str:
        task_payload = dict(operation_task.payload or {})
        existing_move_id = str(task_payload.get("reachtruck_move_id") or "").strip()
        if existing_move_id:
            from reachtruck.models import MoveTask

            if MoveTask.objects.filter(
                legacy_order_id=existing_move_id,
                status__in=[MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS],
            ).exists():
                return existing_move_id

        from reachtruck.services.move_requests import create_stock_move_task

        box_code = snapshot_box_code(snapshot)
        source = snapshot.location
        from_location = {
            "zone": str(getattr(source, "zone_code", "") or snapshot.zone_code or "OTG").strip().upper(),
            "row": int(getattr(source, "row_no", 0) or 0),
            "section": int(getattr(source, "section_no", 0) or 0),
            "tier": int(getattr(source, "tier_no", 0) or 0),
            "cell": int(getattr(source, "cell_no", 0) or 0),
        }
        move_payload = {
            "problem_box_return_v1": True,
            "warehouse_operation_id": int(operation.id),
            "warehouse_operation_task_id": int(operation_task.id),
            "problem_box_snapshot_id": int(snapshot.id),
            "expected_last_event_id": int(snapshot.last_event_id or 0),
            "problem_box_code": box_code,
            "shipping_document": str(operation.source_document_id or "").strip(),
            "task_category": "movement",
            "move_mode": "box_full",
            "pick_mode": "full",
            "pallet_code": box_code,
            "requested_box": box_code,
            "requested_boxes": [box_code],
            "requested_qty": int(snapshot.qty or 0),
            "requested_sku": str(snapshot.sku_code or "").strip(),
            "requested_barcodes": [str(snapshot.barcode or "").strip()] if str(snapshot.barcode or "").strip() else [],
            "requested_goods_type": str(snapshot.goods_type or "").strip(),
            "from_location": from_location,
            "to_location": {"zone": "OS", "row": 0, "section": 0, "tier": 0, "cell": 0},
            "from_label": str(getattr(source, "display_name", "") or getattr(source, "location_code", "") or "OTG").strip(),
            "to_label": "Основной склад · отсканируйте паллету хранения",
            "instruction": (
                f"Возьмите проблемный короб {box_code} в OTG, отсканируйте его, "
                "затем поставьте его на подходящую паллету хранения и отсканируйте паллету."
            ),
            "mobile_execution": {
                "source_confirmed": True,
                "pallet_confirmed": True,
                "problem_box_confirmed": False,
                "destination_confirmed": False,
                "boxes_scanned": [],
            },
        }
        move_id = create_stock_move_task(
            user=requested_by,
            agency=snapshot.agency,
            description=f"Размещение проблемного короба {box_code} из OTG в OS",
            payload=move_payload,
            requested_by_name=str(requested_by_name or "").strip(),
            requested_by_role="storekeeper",
        )
        task_payload["reachtruck_move_id"] = move_id
        task_payload["problem_box_return_v1"] = True
        operation_task.payload = task_payload
        operation_task.save(update_fields=["payload", "updated_at"])
        return move_id

    @classmethod
    @transaction.atomic
    def take_problem_box_return(
        cls,
        *,
        operation_id: int,
        operation_task_id: int,
        performed_by=None,
    ) -> WarehouseOperation:
        actor = performed_by if getattr(performed_by, "is_authenticated", False) else None
        if actor is None:
            raise ValueError("Сотрудник ричтрака не определен.")
        operation = (
            WarehouseOperation.objects.select_for_update()
            .filter(
                id=int(operation_id),
                operation_type=WarehouseOperation.TYPE_RETURN_TO_STORAGE,
                context_type=PROBLEM_BOX_CONTEXT_TYPE,
                status__in=_PUTAWAY_DESTINATION_BLOCKING_STATUSES,
            )
            .first()
        )
        if operation is None:
            raise ValueError("Операция проблемного короба уже закрыта или не найдена.")
        operation_task = (
            WarehouseOperationTask.objects.select_for_update()
            .filter(
                id=int(operation_task_id),
                operation=operation,
                status__in=[
                    WarehouseOperationTask.STATUS_CREATED,
                    WarehouseOperationTask.STATUS_IN_PROGRESS,
                ],
            )
            .first()
        )
        if operation_task is None:
            raise ValueError("Складское задание проблемного короба не найдено.")
        snapshot = (
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("container", "last_event", "active_operation")
            .filter(id=int(operation.context_id), active_operation=operation)
            .first()
        )
        if snapshot is None:
            raise ValueError("Проблемный короб больше не закреплен за этой операцией.")
        check = check_problem_box_snapshot(snapshot)
        if not check.is_problem:
            raise ValueError(check.reason)

        now = timezone.now()
        actor_name = (
            getattr(actor, "get_full_name", lambda: "")()
            or getattr(actor, "username", "")
            or ""
        )
        operation_task.status = WarehouseOperationTask.STATUS_IN_PROGRESS
        operation_task.assigned_to = actor
        operation_task.assigned_to_name = actor_name
        operation_task.executor_role = "reachtruck"
        operation_task.started_at = operation_task.started_at or now
        operation_task.save(
            update_fields=[
                "status",
                "assigned_to",
                "assigned_to_name",
                "executor_role",
                "started_at",
                "updated_at",
            ]
        )
        operation.status = WarehouseOperation.STATUS_IN_PROGRESS
        operation.assigned_executor_role = "reachtruck"
        operation.started_at = operation.started_at or now
        operation.save(update_fields=["status", "assigned_executor_role", "started_at", "updated_at"])
        return operation

    @classmethod
    @transaction.atomic
    def complete_problem_box_return(
        cls,
        *,
        operation_id: int,
        snapshot_id: int,
        scanned_box_code: str,
        destination_pallet_code: str,
        performed_by=None,
    ) -> WarehouseOperation:
        operation = (
            WarehouseOperation.objects.select_for_update()
            .select_related("agency")
            .filter(
                id=int(operation_id),
                operation_type=WarehouseOperation.TYPE_RETURN_TO_STORAGE,
                context_type=PROBLEM_BOX_CONTEXT_TYPE,
            )
            .first()
        )
        if operation is None:
            raise ValueError("Операция проверки короба не найдена.")
        if operation.status == WarehouseOperation.STATUS_DONE:
            return operation
        if operation.status not in _PUTAWAY_DESTINATION_BLOCKING_STATUSES:
            raise ValueError("Операция проверки короба уже закрыта.")
        if str(operation.context_id or "").strip() != str(int(snapshot_id)):
            raise ValueError("Операция относится к другому коробу.")

        task = (
            WarehouseOperationTask.objects.select_for_update()
            .filter(
                operation=operation,
                status__in=[
                    WarehouseOperationTask.STATUS_CREATED,
                    WarehouseOperationTask.STATUS_IN_PROGRESS,
                ],
            )
            .order_by("id")
            .first()
        )
        if task is None:
            raise ValueError("Задание проверки короба не найдено.")
        actor_id = int(getattr(performed_by, "id", 0) or 0)
        if int(task.assigned_to_id or 0) != actor_id:
            raise ValueError("Завершить проверку может сотрудник, который взял короб в работу.")

        snapshot = (
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("agency", "container", "parent_container", "location", "last_event", "active_operation")
            .filter(id=int(snapshot_id), active_operation=operation)
            .first()
        )
        if snapshot is None:
            raise ValueError("Короб больше не закреплен за этой проверкой.")
        check = check_problem_box_snapshot(snapshot)
        if not check.is_problem:
            raise ValueError(check.reason)

        expected_box_code = snapshot_box_code(snapshot)
        if not str(scanned_box_code or "").strip():
            raise ValueError("Отсканируйте ШК короба.")
        if str(scanned_box_code or "").strip().casefold() != expected_box_code.casefold():
            raise ValueError(f"Отсканирован другой короб. Ожидается {expected_box_code}.")

        pallet_code = str(destination_pallet_code or "").strip()
        if not pallet_code:
            raise ValueError("Отсканируйте паллету хранения.")
        destination_pallet = (
            WarehouseContainer.objects.select_for_update(of=("self",))
            .select_related("current_location")
            .filter(
                agency=snapshot.agency,
                container_code__iexact=pallet_code,
                container_type__in=[
                    WarehouseContainer.TYPE_PALLET,
                    WarehouseContainer.TYPE_MIXED_PALLET,
                ],
                status=WarehouseContainer.STATUS_ACTIVE,
                parent_container__isnull=True,
            )
            .order_by("id")
            .first()
        )
        if destination_pallet is None:
            foreign_pallet = WarehouseContainer.objects.filter(
                container_code__iexact=pallet_code,
                container_type__in=[
                    WarehouseContainer.TYPE_PALLET,
                    WarehouseContainer.TYPE_MIXED_PALLET,
                ],
                status=WarehouseContainer.STATUS_ACTIVE,
            ).exists()
            if foreign_pallet:
                raise ValueError("Эта паллета принадлежит другому клиенту или недоступна для размещения.")
            raise ValueError("Активная паллета хранения с таким ШК не найдена.")

        destination = destination_pallet.current_location
        if destination is None:
            raise ValueError("У отсканированной паллеты не указано место хранения.")
        if not destination.is_active:
            raise ValueError("Место отсканированной паллеты неактивно.")
        if str(destination.zone_code or "").strip().upper() != "OS":
            raise ValueError("Паллета должна находиться в основном складе OS.")
        if not destination.is_storage and destination.zone_kind != WarehouseLocation.ZONE_KIND_STORAGE:
            raise ValueError("Паллета находится не в ячейке хранения.")
        source_warehouse_code = str(getattr(snapshot.location, "warehouse_code", "") or "").strip()
        if source_warehouse_code and str(destination.warehouse_code or "").strip() != source_warehouse_code:
            raise ValueError("Паллета находится на другом складе.")

        pallet_stock_ids = list(
            WarehouseStockSnapshot.objects.filter(
                is_archived=False,
                qty__gt=0,
            )
            .filter(
                Q(parent_container=destination_pallet)
                | Q(container__parent_container=destination_pallet)
                | Q(container=destination_pallet)
            )
            .values_list("id", flat=True)
        )
        pallet_stock = list(
            WarehouseStockSnapshot.objects.select_for_update()
            .filter(id__in=pallet_stock_ids)
            .only(
                "id",
                "active_operation_id",
                "processing_reserved_qty",
                "shipping_reserved_qty",
                "other_reserved_qty",
            )
        )
        active_operation_ids = {
            int(stock.active_operation_id)
            for stock in pallet_stock
            if stock.active_operation_id
        }
        if active_operation_ids and WarehouseOperation.objects.filter(
            id__in=active_operation_ids,
            status__in=_PUTAWAY_DESTINATION_BLOCKING_STATUSES,
        ).exists():
            raise ValueError("Паллета уже участвует в другой складской операции.")
        if any(
            int(stock.processing_reserved_qty or 0) > 0
            or int(stock.shipping_reserved_qty or 0) > 0
            or int(stock.other_reserved_qty or 0) > 0
            for stock in pallet_stock
        ):
            raise ValueError("На паллете есть активный резерв; выберите другую паллету хранения.")

        try:
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.STOCK_RETURNED_TO_STORAGE,
                operation_type=WarehouseOperation.TYPE_RETURN_TO_STORAGE,
                zone_to=destination.zone_code,
            )
        except WarehouseTransitionError as exc:
            raise ValueError("Текущее складское состояние не допускает возврат короба.") from exc

        now = timezone.now()
        event = WarehouseEvent.objects.create(
            agency=snapshot.agency,
            event_type=WarehouseEventType.STOCK_RETURNED_TO_STORAGE.value,
            stock_context_type=snapshot.source_context_type,
            stock_context_id=snapshot.source_context_id,
            container=snapshot.container,
            operation=operation,
            operation_task=task,
            source_document_type=operation.source_document_type,
            source_document_id=operation.source_document_id,
            from_location=snapshot.location,
            to_location=destination,
            from_zone_code=snapshot.zone_code,
            to_zone_code=destination.zone_code,
            qty=int(snapshot.qty or 0),
            performed_by=performed_by,
            performed_by_role=cls._role_of(performed_by) or "storekeeper",
            occurred_at=now,
            payload={
                "problem_box_return": True,
                "snapshot_id": int(snapshot.id),
                "box_code": expected_box_code,
                "canceled_event_id": int(snapshot.last_event_id or 0),
                "destination_pallet_id": int(destination_pallet.id),
                "destination_pallet_code": destination_pallet.container_code,
                "destination_location_code": destination.location_code,
            },
        )
        snapshot.parent_container = destination_pallet
        snapshot.location = destination
        snapshot.zone_code = destination.zone_code
        snapshot.zone_kind = destination.zone_kind
        snapshot.warehouse_state_code = transition.code.value
        snapshot.available_qty = int(snapshot.qty or 0)
        snapshot.active_operation = None
        snapshot.active_operation_type = ""
        snapshot.last_event = event
        snapshot.snapshot_version = int(snapshot.snapshot_version or 1) + 1
        snapshot.save(
            update_fields=[
                "parent_container",
                "location",
                "zone_code",
                "zone_kind",
                "warehouse_state_code",
                "available_qty",
                "active_operation",
                "active_operation_type",
                "last_event",
                "snapshot_version",
                "updated_at",
            ]
        )
        if snapshot.container_id:
            WarehouseContainer.objects.filter(id=snapshot.container_id).update(
                parent_container=destination_pallet,
                current_location=destination,
                status=WarehouseContainer.STATUS_ACTIVE,
                updated_at=now,
            )

        task.to_location = destination
        task.to_zone_code = destination.zone_code
        task.qty_done = int(snapshot.qty or 0)
        task.status = WarehouseOperationTask.STATUS_DONE
        task.completed_at = now
        task_payload = dict(task.payload or {})
        task_payload["destination_pallet_id"] = int(destination_pallet.id)
        task_payload["destination_pallet_code"] = destination_pallet.container_code
        task.payload = task_payload
        task.save(
            update_fields=[
                "to_location",
                "to_zone_code",
                "qty_done",
                "status",
                "completed_at",
                "payload",
                "updated_at",
            ]
        )
        operation.destination_location = destination
        operation.destination_zone_code = destination.zone_code
        operation.done_qty = int(snapshot.qty or 0)
        operation.status = WarehouseOperation.STATUS_DONE
        operation.completed_at = now
        operation.save(
            update_fields=[
                "destination_location",
                "destination_zone_code",
                "done_qty",
                "status",
                "completed_at",
                "updated_at",
            ]
        )
        return operation

    @classmethod
    @transaction.atomic
    def cancel_problem_box_return(
        cls,
        *,
        operation_id: int,
        performed_by=None,
    ) -> WarehouseOperation:
        operation = (
            WarehouseOperation.objects.select_for_update()
            .filter(
                id=int(operation_id),
                operation_type=WarehouseOperation.TYPE_RETURN_TO_STORAGE,
                context_type=PROBLEM_BOX_CONTEXT_TYPE,
            )
            .first()
        )
        if operation is None:
            raise ValueError("Операция проверки короба не найдена.")
        if operation.status in _FINAL_OPERATION_STATUSES:
            return operation
        task = (
            WarehouseOperationTask.objects.select_for_update()
            .filter(operation=operation)
            .order_by("id")
            .first()
        )
        actor_id = int(getattr(performed_by, "id", 0) or 0)
        actor_role = cls._role_of(performed_by)
        privileged_roles = {"admin", "director", "developer", "head_manager"}
        if (
            task
            and task.assigned_to_id
            and int(task.assigned_to_id) != actor_id
            and actor_role not in privileged_roles
        ):
            raise ValueError("Освободить проверку может назначенный сотрудник или руководитель.")
        snapshot = (
            WarehouseStockSnapshot.objects.select_for_update()
            .filter(active_operation=operation, is_archived=False)
            .first()
        )
        now = timezone.now()
        if snapshot is not None:
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
            snapshot.snapshot_version = int(snapshot.snapshot_version or 1) + 1
            snapshot.save(
                update_fields=[
                    "active_operation",
                    "active_operation_type",
                    "snapshot_version",
                    "updated_at",
                ]
            )
        operation.status = WarehouseOperation.STATUS_CANCELED
        operation.completed_at = now
        operation.save(update_fields=["status", "completed_at", "updated_at"])
        operation.tasks.exclude(status=WarehouseOperationTask.STATUS_DONE).update(
            status=WarehouseOperationTask.STATUS_CANCELED,
            completed_at=now,
            updated_at=now,
        )
        task_payload = dict(task.payload or {}) if task and isinstance(task.payload, dict) else {}
        move_id = str(task_payload.get("reachtruck_move_id") or "").strip()
        if move_id:
            from reachtruck.models import MoveTask
            from reachtruck.services.move_requests import sync_task_status_by_legacy_order_id

            move_task = MoveTask.objects.filter(legacy_order_id=move_id).order_by("-id").first()
            if move_task and move_task.status not in {
                MoveTask.STATUS_DONE,
                MoveTask.STATUS_CANCELED,
                MoveTask.STATUS_FAILED,
            }:
                sync_task_status_by_legacy_order_id(
                    move_id,
                    status=MoveTask.STATUS_CANCELED,
                    assigned_to=performed_by,
                    assigned_to_name=(
                        getattr(performed_by, "get_full_name", lambda: "")()
                        or getattr(performed_by, "username", "")
                        or ""
                    ),
                )
        return operation

    @classmethod
    @transaction.atomic
    def move_reserved_fbs_pallet(
        cls,
        *,
        agency: Agency,
        request_id: int,
        source_pallet_id: int,
        source_pallet_scan: str,
        selected_box_ids,
        allocation_rows: list[dict],
        destination: WarehouseLocation,
        performed_by=None,
    ) -> dict:
        """Moves one fully selected physical pallet after strict composition checks."""
        actor = performed_by if getattr(performed_by, "is_authenticated", False) else None
        actor_role = cls._role_of(actor)
        if actor is None or (
            not getattr(actor, "is_superuser", False)
            and actor_role not in {"reachtruck_driver", "head_manager", "director", "admin"}
        ):
            raise WarehouseTransitionError(
                "Переместить целую паллету FBS может только водитель ричтрака или руководитель склада."
            )
        if int(request_id or 0) <= 0:
            raise WarehouseTransitionError("Не указана клиентская заявка FBS.")
        agency = Agency.objects.select_for_update().get(pk=agency.pk)
        normalized_box_ids = {
            int(value or 0) for value in selected_box_ids or [] if int(value or 0) > 0
        }
        if not normalized_box_ids:
            raise WarehouseTransitionError(
                "В паллетном FBS-перемещении не указаны физические короба."
            )
        source_pallet = (
            WarehouseContainer.objects.select_for_update()
            .filter(
                pk=int(source_pallet_id),
                agency=agency,
                status=WarehouseContainer.STATUS_ACTIVE,
                container_type__in=(
                    WarehouseContainer.TYPE_PALLET,
                    WarehouseContainer.TYPE_MIXED_PALLET,
                ),
            )
            .first()
        )
        if source_pallet is None:
            raise WarehouseTransitionError("Исходная паллета FBS больше не активна.")
        if str(source_pallet.container_code or "").strip().casefold() != str(
            source_pallet_scan or ""
        ).strip().casefold():
            raise WarehouseTransitionError("Скан паллеты не совпадает с FBS-заданием.")

        children = list(
            WarehouseContainer.objects.select_for_update()
            .filter(
                parent_container=source_pallet,
                agency=agency,
                status=WarehouseContainer.STATUS_ACTIVE,
                container_type=WarehouseContainer.TYPE_BOX,
            )
            .order_by("id")
        )
        active_child_ids = {child.id for child in children}
        if active_child_ids != normalized_box_ids:
            raise WarehouseTransitionError(
                "Состав паллеты изменился: перемещение паллеты целиком запрещено. "
                "Пересоберите заявку по фактическим коробам."
            )

        normalized_allocations: dict[int, dict] = {}
        allowed_operation_ids: set[int] = set()
        reserve_ids: set[int] = set()
        for raw in allocation_rows or []:
            snapshot_id = int(raw.get("snapshot_id") or 0)
            container_id = int(raw.get("container_id") or 0)
            qty = int(raw.get("qty") or 0)
            operation_id = int(raw.get("operation_id") or 0)
            plan_id = int(raw.get("plan_id") or 0)
            reserve_id = int(raw.get("reserve_id") or 0)
            if (
                snapshot_id <= 0
                or container_id not in normalized_box_ids
                or qty <= 0
                or operation_id <= 0
                or plan_id <= 0
                or reserve_id <= 0
                or snapshot_id in normalized_allocations
                or reserve_id in reserve_ids
            ):
                raise WarehouseTransitionError(
                    "Состав складских распределений паллетного FBS-перемещения поврежден."
                )
            normalized_allocations[snapshot_id] = {
                "container_id": container_id,
                "qty": qty,
                "operation_id": operation_id,
                "plan_id": plan_id,
                "reserve_id": reserve_id,
            }
            allowed_operation_ids.add(operation_id)
            reserve_ids.add(reserve_id)

        operations = {
            operation.id: operation
            for operation in WarehouseOperation.objects.select_for_update()
            .filter(
                id__in=allowed_operation_ids,
                agency=agency,
                operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
                context_type="fbs_replenishment",
                source_document_type="fbs_plan",
                status__in=(
                    WarehouseOperation.STATUS_PLANNED,
                    WarehouseOperation.STATUS_IN_PROGRESS,
                ),
            )
            .order_by("id")
        }
        if set(operations) != allowed_operation_ids:
            raise WarehouseTransitionError(
                "Операции паллетного FBS-перемещения больше не действуют."
            )
        reserves = {
            reserve.id: reserve
            for reserve in WarehouseReserve.objects.select_for_update()
            .filter(
                id__in=reserve_ids,
                agency=agency,
                reserve_type=WarehouseReserve.TYPE_MANUAL,
                context_type="fbs_replenishment",
                source_document_type="fbs_plan",
                status=WarehouseReserve.STATUS_ALLOCATED,
            )
            .order_by("id")
        }
        if set(reserves) != reserve_ids:
            raise WarehouseTransitionError(
                "Резервы паллетного FBS-перемещения больше не действуют."
            )
        for expected in normalized_allocations.values():
            operation = operations[expected["operation_id"]]
            reserve = reserves[expected["reserve_id"]]
            plan_id = str(expected["plan_id"])
            if (
                str(operation.context_id or "") != plan_id
                or str(operation.source_document_id or "") != plan_id
                or str(reserve.context_id or "") != plan_id
                or str(reserve.source_document_id or "") != plan_id
                or int(reserve.qty_reserved or 0) != expected["qty"]
                or int(reserve.qty_allocated or 0) != expected["qty"]
                or int(reserve.qty_satisfied or 0) != 0
            ):
                raise WarehouseTransitionError(
                    "Резерв или операция не совпадает с составом паллетного FBS-задания."
                )

        snapshots = list(
            WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("container", "location", "active_operation")
            .filter(
                agency=agency,
                container_id__in=normalized_box_ids,
                is_archived=False,
                qty__gt=0,
            )
            .order_by("id")
        )
        if not snapshots or {row.id for row in snapshots} != set(normalized_allocations):
            raise WarehouseTransitionError(
                "Точный резерв не покрывает весь товар исходной паллеты."
            )
        cls.assert_not_reserved_for_shipping(agency=agency, snapshots=snapshots)
        location_ids = {int(snapshot.location_id or 0) for snapshot in snapshots}
        if len(location_ids) != 1 or 0 in location_ids:
            raise WarehouseTransitionError(
                "Остатки исходной паллеты находятся в разных местах."
            )
        source_location_id = next(iter(location_ids))
        if source_pallet.current_location_id not in {None, source_location_id}:
            raise WarehouseTransitionError(
                "Фактическое место паллеты не совпадает с местом складского остатка."
            )
        if any(
            child.current_location_id not in {None, source_location_id}
            for child in children
        ):
            raise WarehouseTransitionError(
                "Часть коробов физически находится вне исходной паллеты."
            )
        for snapshot in snapshots:
            expected = normalized_allocations[snapshot.id]
            if (
                snapshot.container_id != expected["container_id"]
                or int(snapshot.qty or 0) != expected["qty"]
                or int(snapshot.processing_reserved_qty or 0) != 0
                or int(snapshot.shipping_reserved_qty or 0) != 0
                or int(snapshot.other_reserved_qty or 0) != expected["qty"]
                or snapshot.is_in_vehicle
                or int(snapshot.active_operation_id or 0) != expected["operation_id"]
            ):
                raise WarehouseTransitionError(
                    "Товар паллеты изменился, занят другой операцией или зарезервирован не полностью."
                )

        destination = WarehouseLocation.objects.select_for_update().get(pk=destination.pk)
        if (
            not destination.is_active
            or not destination.is_storage
            or str(destination.zone_code or "").strip().upper() != "OS"
        ):
            raise WarehouseTransitionError(
                "Паллетное FBS-перемещение можно завершить только в активной ячейке OS."
            )
        if destination.id == source_location_id:
            raise WarehouseTransitionError("Паллета уже находится в выбранной ячейке.")
        now = timezone.now()
        event_ids = []
        for snapshot in snapshots:
            event = WarehouseEvent.objects.create(
                agency=agency,
                event_type="fbs_full_pallet_moved",
                stock_context_type=_FBS_MOVEMENT_RESERVE_CONTEXT,
                stock_context_id=str(int(request_id)),
                container=source_pallet,
                operation=snapshot.active_operation,
                source_document_type=_FBS_MOVEMENT_SOURCE_DOCUMENT,
                source_document_id=str(int(request_id)),
                from_location=snapshot.location,
                to_location=destination,
                from_zone_code=str(snapshot.zone_code or ""),
                to_zone_code=str(destination.zone_code or ""),
                qty=int(snapshot.qty or 0),
                payload={
                    "source_pallet_id": source_pallet.id,
                    "source_pallet_code": source_pallet.container_code,
                    "source_box_id": snapshot.container_id,
                    "source_snapshot_id": snapshot.id,
                    "whole_pallet": True,
                },
                performed_by=actor,
                performed_by_role=cls._role_of(actor) or "reachtruck",
                occurred_at=now,
            )
            event_ids.append(event.id)

        source_pallet.current_location = destination
        source_pallet.source_context_type = FBS_STORAGE_CONTEXT_TYPE
        source_pallet.source_context_id = str(int(request_id))
        source_pallet.save(
            update_fields=[
                "current_location",
                "source_context_type",
                "source_context_id",
                "updated_at",
            ]
        )
        WarehouseContainer.objects.filter(id__in=normalized_box_ids).update(
            current_location=destination,
            source_context_type="fbs_placement",
            source_context_id=str(int(request_id)),
            updated_at=now,
        )
        return {
            "source_pallet_id": source_pallet.id,
            "source_pallet_code": source_pallet.container_code,
            "destination_id": destination.id,
            "box_ids": sorted(normalized_box_ids),
            "snapshot_ids": [snapshot.id for snapshot in snapshots],
            "event_ids": event_ids,
        }

    @classmethod
    def _fbs_movement_reserve_rows(
        cls,
        *,
        agency: Agency,
        request_id: int,
        lock: bool,
    ) -> list[WarehouseReserve]:
        queryset = WarehouseReserve.objects.filter(
            agency=agency,
            reserve_type=WarehouseReserve.TYPE_FBS_MOVEMENT,
            context_type=_FBS_MOVEMENT_RESERVE_CONTEXT,
            context_id=str(int(request_id)),
            status__in=_OPEN_FBS_MOVEMENT_RESERVE_STATUSES,
        ).order_by("id")
        if lock:
            queryset = queryset.select_for_update()
        return list(queryset)

    @classmethod
    def _fbs_movement_allocations_from_reserves(
        cls,
        reserves: list[WarehouseReserve],
    ) -> list[dict]:
        if not reserves:
            return []
        from .fbs_quantity_reserves import quantity_allocations
        quantity_rows = quantity_allocations(reserves)
        if quantity_rows:
            if len(quantity_rows) != len(reserves):
                raise WarehouseTransitionError('Смешаны количественный и точный резервы FBS.')
            return quantity_rows
        events = {
            event.reserve_id: event
            for event in WarehouseEvent.objects.filter(
                reserve_id__in=[reserve.id for reserve in reserves],
                event_type="fbs_movement_reserved",
            ).order_by("reserve_id", "id")
        }
        allocations = []
        for reserve in reserves:
            event = events.get(reserve.id)
            payload = dict(event.payload or {}) if event else {}
            snapshot_id = int(payload.get("source_snapshot_id") or 0)
            request_line_id = int(payload.get("request_line_id") or 0)
            if not snapshot_id or not request_line_id:
                raise WarehouseTransitionError(
                    "Резерв FBS-перемещения не связан со складским остатком или строкой заявки."
                )
            allocations.append(
                {
                    "reserve_id": reserve.id,
                    "snapshot_id": snapshot_id,
                    "request_line_id": request_line_id,
                    "container_id": int(payload.get("container_id") or 0),
                    "container_code": str(payload.get("container_code") or "").strip(),
                    "qty": int(reserve.qty_allocated or reserve.qty_reserved or 0),
                }
            )
        return allocations

    @classmethod
    def _validate_fbs_movement_reserved_stock(cls, *, agency, reserves, allocations):
        """Validate exact reserved sources, never replace them with another order's stock."""
        snapshots = {
            snapshot.id: snapshot
            for snapshot in WarehouseStockSnapshot.objects.select_for_update(of=("self",))
            .select_related("container", "location")
            .filter(id__in=[row["snapshot_id"] for row in allocations])
            .order_by("id")
        }
        reserves_by_id = {reserve.id: reserve for reserve in reserves}
        required = Counter()
        for row in allocations:
            required[row["snapshot_id"]] += int(row["qty"] or 0)
        shortages = []
        checked = set()
        for row in allocations:
            snapshot = snapshots.get(row["snapshot_id"])
            reserve = reserves_by_id[row["reserve_id"]]
            needed = required[row["snapshot_id"]]
            covered = 0
            if (
                snapshot is not None
                and snapshot.agency_id == agency.id
                and not snapshot.is_archived
                and not snapshot.is_in_vehicle
                and snapshot.container_id == row["container_id"]
                and cls._shipping_reserve_identity(snapshot) == cls._shipping_reserve_identity(reserve)
            ):
                # Own reserve is NOT free stock. Subtract all other claims first,
                # so a stale reserve cannot release nonexistent units into available_qty.
                other_claims = (
                    max(int(snapshot.available_qty or 0), 0)
                    + max(int(snapshot.shipping_reserved_qty or 0), 0)
                    + max(int(snapshot.processing_reserved_qty or 0), 0)
                    + max(int(snapshot.other_reserved_qty or 0) - needed, 0)
                )
                covered = max(0, min(
                    needed,
                    int(snapshot.other_reserved_qty or 0),
                    int(snapshot.qty or 0) - other_claims,
                ))
            if covered < needed and row["snapshot_id"] not in checked:
                checked.add(row["snapshot_id"])
                shortages.append(
                    f"артикул {reserve.sku_code or '-'}, ШК {reserve.barcode or '-'}, "
                    f"короб {row['container_code'] or row['container_id'] or '-'}: "
                    f"требуется {needed} шт., обеспечено остатком {covered} шт., "
                    f"не хватает {needed - covered} шт."
                )
        if shortages:
            raise WarehouseTransitionError(
                "Недостаточно товара на остатке для FBS-перемещения. "
                + "; ".join(shortages[:5])
                + (f"; ещё позиций с нехваткой: {len(shortages) - 5}" if len(shortages) > 5 else "")
                + ". Заявка не может быть передана на склад. Товар из других заявок не подбирается."
            )
        return snapshots

    @classmethod
    @transaction.atomic
    def fbs_movement_reserve_allocations(
        cls,
        *,
        agency: Agency,
        request_id: int,
        lock: bool = False,
    ) -> list[dict]:
        agency = Agency.objects.select_for_update().get(pk=agency.pk) if lock else agency
        reserves = cls._fbs_movement_reserve_rows(
            agency=agency,
            request_id=request_id,
            lock=lock,
        )
        allocations = cls._fbs_movement_allocations_from_reserves(reserves)
        if lock and allocations and allocations[0].get('reserve_scope') != 'quantity':
            snapshots = cls._validate_fbs_movement_reserved_stock(
                agency=agency, reserves=reserves, allocations=allocations,
            )
            cls.assert_not_reserved_for_shipping(agency=agency, snapshots=snapshots.values())
            if any(snapshot.active_operation_id for snapshot in snapshots.values()):
                raise WarehouseTransitionError(
                    "Товар FBS-заявки занят другой складской операцией. "
                    "Передача заявки на склад заблокирована."
                )
        return allocations

    @classmethod
    @transaction.atomic
    def reserve_for_fbs_movement(
        cls,
        *,
        agency: Agency,
        request_id: int,
        allocations: list[dict],
        whole_container_ids=None,
        created_by=None,
    ) -> list[dict]:
        agency = Agency.objects.select_for_update().get(pk=agency.pk)
        normalized_whole_container_ids = set()
        for value in whole_container_ids or []:
            try:
                container_id = int(value or 0)
            except (TypeError, ValueError):
                raise WarehouseTransitionError(
                    "Для целого короба FBS указан некорректный идентификатор."
                )
            if container_id > 0:
                normalized_whole_container_ids.add(container_id)
        normalized: list[dict] = []
        seen_snapshot_ids: set[int] = set()
        for raw in list(allocations or []):
            snapshot_id = int(raw.get("snapshot_id") or 0)
            request_line_id = int(raw.get("request_line_id") or 0)
            qty = int(raw.get("qty") or 0)
            if not snapshot_id or not request_line_id or qty <= 0:
                raise WarehouseTransitionError(
                    "Для резерва FBS нужны остаток, строка заявки и положительное количество."
                )
            if snapshot_id in seen_snapshot_ids:
                raise WarehouseTransitionError(
                    "Один складской остаток нельзя дважды включить в FBS-перемещение."
                )
            seen_snapshot_ids.add(snapshot_id)
            normalized.append(
                {
                    "snapshot_id": snapshot_id,
                    "request_line_id": request_line_id,
                    "qty": qty,
                    "container_id": int(raw.get("container_id") or 0),
                    "container_code": str(raw.get("container_code") or "").strip(),
                    "barcode": str(raw.get("barcode") or "").strip(),
                    "sku_id": int(raw.get("sku_id") or 0),
                }
            )
        if not normalized:
            raise WarehouseTransitionError("В FBS-перемещении нет товара для резерва.")

        existing = cls._fbs_movement_reserve_rows(
            agency=agency,
            request_id=request_id,
            lock=True,
        )
        if existing:
            current = cls._fbs_movement_allocations_from_reserves(existing)
            current_key = sorted(
                (row["snapshot_id"], row["request_line_id"], row["qty"])
                for row in current
            )
            expected_key = sorted(
                (row["snapshot_id"], row["request_line_id"], row["qty"])
                for row in normalized
            )
            if current_key != expected_key:
                raise WarehouseTransitionError(
                    "По заявке уже существует другой складской резерв FBS."
                )
            return cls.fbs_movement_reserve_allocations(
                agency=agency, request_id=request_id, lock=True,
            )

        snapshots = {
            snapshot.id: snapshot
            for snapshot in WarehouseStockSnapshot.objects.select_for_update(
                of=("self",)
            )
            .select_related("container", "location")
            .filter(id__in=seen_snapshot_ids)
        }
        if len(snapshots) != len(seen_snapshot_ids):
            raise WarehouseTransitionError("Часть складских остатков для FBS не найдена.")

        from .fbs_quantity_reserves import pool_reserves, assert_capacity, identity
        if pool_reserves(agency.id).exists():
            needed = Counter()
            for row in normalized:
                needed[identity(snapshots[row['snapshot_id']])] += row['qty']
            assert_capacity(agency.id, needed)

        cls.assert_not_reserved_for_shipping(
            agency=agency,
            snapshots=list(snapshots.values()),
        )

        from fbs.goods_types import (
            is_fbs_client_movement_source_stock,
            receiving_placement_allowed_snapshot_ids,
        )
        receiving_allowed_ids = receiving_placement_allowed_snapshot_ids(snapshots.values())

        if normalized_whole_container_ids:
            containers = list(
                WarehouseContainer.objects.select_for_update()
                .filter(id__in=normalized_whole_container_ids)
                .order_by("id")
            )
            if len(containers) != len(normalized_whole_container_ids):
                raise WarehouseTransitionError(
                    "Часть целых коробов FBS больше не существует."
                )
            if WarehouseContainer.objects.filter(
                id__in=normalized_whole_container_ids,
                fbs_box__isnull=False,
            ).exists():
                raise WarehouseTransitionError(
                    "Целый короб уже находится в контуре FBS."
                )
            if any(
                container.agency_id != agency.id
                or container.container_type != WarehouseContainer.TYPE_BOX
                or container.status != WarehouseContainer.STATUS_ACTIVE
                for container in containers
            ):
                raise WarehouseTransitionError(
                    "Целый короб FBS принадлежит другому клиенту или больше не активен."
                )
            whole_snapshots = list(
                WarehouseStockSnapshot.objects.select_for_update(of=("self",))
                .filter(
                    container_id__in=normalized_whole_container_ids,
                    is_archived=False,
                    qty__gt=0,
                )
                .order_by("id")
            )
            allocated_whole_rows = {
                row["snapshot_id"]: row
                for row in normalized
                if row["container_id"] in normalized_whole_container_ids
            }
            allocated_whole_container_ids = {
                row["container_id"] for row in allocated_whole_rows.values()
            }
            if (
                not whole_snapshots
                or allocated_whole_container_ids != normalized_whole_container_ids
                or set(allocated_whole_rows)
                != {snapshot.id for snapshot in whole_snapshots}
            ):
                raise WarehouseTransitionError(
                    "Точный резерв FBS должен включать весь состав выбранного короба."
                )
            fbs_zone = str(
                getattr(settings, "FBS_ZONE_CODE", "FBS") or "FBS"
            ).strip().upper()
            for snapshot in whole_snapshots:
                row = allocated_whole_rows[snapshot.id]
                if (
                    snapshot.agency_id != agency.id
                    or snapshot.container_id != row["container_id"]
                    or row["qty"] != int(snapshot.qty or 0)
                    or int(snapshot.available_qty or 0) != int(snapshot.qty or 0)
                    or int(snapshot.processing_reserved_qty or 0) != 0
                    or int(snapshot.shipping_reserved_qty or 0) != 0
                    or int(snapshot.other_reserved_qty or 0) != 0
                    or snapshot.active_operation_id is not None
                    or snapshot.is_in_vehicle
                    or str(snapshot.zone_code or "").strip().upper() == fbs_zone
                    or not is_fbs_client_movement_source_stock(
                        snapshot=snapshot,
                        receiving_allowed_ids=receiving_allowed_ids,
                        goods_type=snapshot.goods_type,
                        zone_kind=snapshot.zone_kind,
                        warehouse_state_code=snapshot.warehouse_state_code,
                        container_type=getattr(
                            snapshot.container, "container_type", ""
                        ),
                        container_code=getattr(
                            snapshot.container, "container_code", ""
                        ),
                        container_status=getattr(
                            snapshot.container, "status", ""
                        ),
                    )
                    or (
                        getattr(snapshot, "expiry_date", None) is not None
                        and snapshot.expiry_date < timezone.localdate()
                    )
                ):
                    raise WarehouseTransitionError(
                        "Целый короб FBS имеет резерв, активную операцию или недоступный остаток."
                    )

        now = timezone.now()
        actor = created_by if getattr(created_by, "is_authenticated", False) else None
        for row in normalized:
            snapshot = snapshots[row["snapshot_id"]]
            if snapshot.agency_id != agency.id:
                raise WarehouseTransitionError("Складской остаток принадлежит другому клиенту.")
            if snapshot.is_archived or snapshot.is_in_vehicle:
                raise WarehouseTransitionError("Складской остаток недоступен для FBS-перемещения.")
            if snapshot.active_operation_id is not None:
                raise WarehouseTransitionError("Складской остаток уже участвует в другой операции.")
            if not is_fbs_client_movement_source_stock(
                snapshot=snapshot,
                receiving_allowed_ids=receiving_allowed_ids,
                goods_type=snapshot.goods_type,
                zone_kind=snapshot.zone_kind,
                warehouse_state_code=snapshot.warehouse_state_code,
                container_type=getattr(snapshot.container, "container_type", ""),
                container_code=getattr(snapshot.container, "container_code", ""),
                container_status=getattr(snapshot.container, "status", ""),
            ):
                raise WarehouseTransitionError(
                    "В FBS можно резервировать только готовый товар из активного GV-короба: на хранении, после завершённой обработки или из приёмки с разрешённым размещением паллеты."
                )
            if int(snapshot.available_qty or 0) < row["qty"]:
                raise WarehouseTransitionError(
                    f"Недостаточно доступного остатка по ШК {snapshot.barcode}: "
                    f"нужно {row['qty']}, доступно {snapshot.available_qty}."
                )
            if row["barcode"] and str(snapshot.barcode or "").strip() != row["barcode"]:
                raise WarehouseTransitionError("Штрихкод складского остатка изменился.")
            if row["sku_id"] and snapshot.sku_ref_id != row["sku_id"]:
                raise WarehouseTransitionError("SKU складского остатка изменился.")
            if row["container_id"] and snapshot.container_id != row["container_id"]:
                raise WarehouseTransitionError("Короб складского остатка изменился.")

            reserve = WarehouseReserve.objects.create(
                agency=agency,
                reserve_type=WarehouseReserve.TYPE_FBS_MOVEMENT,
                context_type=_FBS_MOVEMENT_RESERVE_CONTEXT,
                context_id=str(int(request_id)),
                sku_ref_id=snapshot.sku_ref_id,
                sku_code=snapshot.sku_code,
                size=snapshot.size,
                barcode=snapshot.barcode,
                goods_type=snapshot.goods_type,
                marking_code=snapshot.marking_code,
                qty_reserved=row["qty"],
                qty_allocated=row["qty"],
                status=WarehouseReserve.STATUS_ALLOCATED,
                source_document_type=_FBS_MOVEMENT_SOURCE_DOCUMENT,
                source_document_id=str(int(request_id)),
                created_by=actor,
            )
            source_version = int(snapshot.snapshot_version or 0)
            snapshot.available_qty = int(snapshot.available_qty or 0) - row["qty"]
            snapshot.other_reserved_qty = int(snapshot.other_reserved_qty or 0) + row["qty"]
            snapshot.snapshot_version = source_version + 1
            event = WarehouseEvent.objects.create(
                agency=agency,
                event_type="fbs_movement_reserved",
                stock_context_type=_FBS_MOVEMENT_RESERVE_CONTEXT,
                stock_context_id=str(int(request_id)),
                container=snapshot.container,
                reserve=reserve,
                source_document_type=_FBS_MOVEMENT_SOURCE_DOCUMENT,
                source_document_id=str(int(request_id)),
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=str(snapshot.zone_code or ""),
                to_zone_code=str(snapshot.zone_code or ""),
                qty=row["qty"],
                payload={
                    "source_snapshot_id": snapshot.id,
                    "source_snapshot_version": source_version,
                    "request_line_id": row["request_line_id"],
                    "container_id": snapshot.container_id,
                    "container_code": str(
                        snapshot.container_code
                        or getattr(snapshot.container, "container_code", "")
                        or ""
                    ),
                },
                performed_by=actor,
                performed_by_role=cls._role_of(actor),
                occurred_at=now,
            )
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "available_qty",
                    "other_reserved_qty",
                    "snapshot_version",
                    "last_event",
                    "updated_at",
                ]
            )
        return cls.fbs_movement_reserve_allocations(
            agency=agency,
            request_id=request_id,
            lock=True,
        )

    @classmethod
    @transaction.atomic
    def release_fbs_movement_reserves(
        cls,
        *,
        agency: Agency,
        request_id: int,
        released_by=None,
        reason: str = "",
    ) -> list[dict]:
        agency = Agency.objects.select_for_update().get(pk=agency.pk)
        reserves = cls._fbs_movement_reserve_rows(
            agency=agency,
            request_id=request_id,
            lock=True,
        )
        allocations = cls._fbs_movement_allocations_from_reserves(reserves)
        if not allocations:
            return []
        if allocations[0].get('reserve_scope') == 'quantity':
            from .fbs_quantity_reserves import release_pool
            return release_pool(reserves, released_by, reason)
        snapshots = cls._validate_fbs_movement_reserved_stock(
            agency=agency, reserves=reserves, allocations=allocations,
        )
        actor = released_by if getattr(released_by, "is_authenticated", False) else None
        now = timezone.now()
        reserves_by_id = {reserve.id: reserve for reserve in reserves}
        for row in allocations:
            reserve = reserves_by_id[row["reserve_id"]]
            snapshot = snapshots[row["snapshot_id"]]
            qty = int(row["qty"] or 0)
            if snapshot.agency_id != agency.id:
                raise WarehouseTransitionError("Резерв FBS связан с остатком другого клиента.")
            if int(snapshot.other_reserved_qty or 0) < qty:
                raise WarehouseTransitionError(
                    "Складской резерв FBS не совпадает с текущим зарезервированным количеством."
                )
            if int(snapshot.available_qty or 0) + qty > int(snapshot.qty or 0):
                raise WarehouseTransitionError(
                    "Снятие резерва FBS нарушит баланс складского остатка."
                )
            source_version = int(snapshot.snapshot_version or 0)
            snapshot.available_qty = int(snapshot.available_qty or 0) + qty
            snapshot.other_reserved_qty = int(snapshot.other_reserved_qty or 0) - qty
            snapshot.snapshot_version = source_version + 1
            event = WarehouseEvent.objects.create(
                agency=agency,
                event_type="fbs_movement_reserve_released",
                stock_context_type=_FBS_MOVEMENT_RESERVE_CONTEXT,
                stock_context_id=str(int(request_id)),
                container=snapshot.container,
                reserve=reserve,
                source_document_type=_FBS_MOVEMENT_SOURCE_DOCUMENT,
                source_document_id=str(int(request_id)),
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=str(snapshot.zone_code or ""),
                to_zone_code=str(snapshot.zone_code or ""),
                qty=qty,
                payload={
                    "source_snapshot_id": snapshot.id,
                    "source_snapshot_version": source_version,
                    "request_line_id": row["request_line_id"],
                    "container_id": snapshot.container_id,
                    "container_code": row["container_code"],
                    "reason": str(reason or "").strip(),
                },
                performed_by=actor,
                performed_by_role=cls._role_of(actor),
                occurred_at=now,
            )
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "available_qty",
                    "other_reserved_qty",
                    "snapshot_version",
                    "last_event",
                    "updated_at",
                ]
            )
            reserve.status = WarehouseReserve.STATUS_RELEASED
            reserve.released_by = actor
            reserve.save(update_fields=["status", "released_by", "updated_at"])
        return allocations

    @staticmethod
    def _state_for_location(location: WarehouseLocation) -> str:
        zone = str(getattr(location, "zone_code", "") or "").strip().upper()
        if zone == "PR":
            return WarehouseStateCode.PLACED_IN_RECEIVING.value
        if zone in {"OS", "MR"}:
            return WarehouseStateCode.STORED.value
        if zone == "OBR":
            return WarehouseStateCode.IN_PROCESSING_ZONE.value
        if zone == "OTG":
            return WarehouseStateCode.IN_OTG.value
        return WarehouseStateCode.UNKNOWN.value

    @classmethod
    def _location_from_item(
        cls,
        *,
        item: dict,
        fallback: WarehouseLocation,
        warehouse_code: str,
    ) -> WarehouseLocation:
        location = item.get("location") if isinstance(item.get("location"), dict) else {}
        zone = str(location.get("zone") or location.get("zone_code") or "").strip().upper()
        if not zone:
            return fallback
        location_code = str(
            location.get("code") or location.get("location_code") or ""
        ).strip()
        if location_code:
            exact_location = (
                WarehouseLocation.objects.filter(
                    warehouse_code=warehouse_code,
                    zone_code=zone,
                    location_code__iexact=location_code,
                    is_active=True,
                )
                .order_by("id")
                .first()
            )
            if exact_location is None:
                raise ValueError(
                    f"Складское место {location_code} не найдено в зоне {zone}."
                )
            return exact_location
        coordinates = (
            int(location.get("row") or location.get("row_no") or 0),
            int(location.get("section") or location.get("section_no") or 0),
            int(location.get("tier") or location.get("tier_no") or 0),
            int(location.get("cell") or location.get("cell_no") or 0),
        )
        if (
            zone == str(fallback.zone_code or "").strip().upper()
            and not any(coordinates)
            and str(fallback.location_code or "").strip().upper() != zone
        ):
            return fallback
        return cls.ensure_location(
            warehouse_code=warehouse_code,
            zone_code=zone,
            row_no=coordinates[0],
            section_no=coordinates[1],
            tier_no=coordinates[2],
            cell_no=coordinates[3],
        )

    @staticmethod
    def _role_of(user) -> str:
        if not user:
            return ""
        employee = getattr(user, "employee_profile", None)
        if employee and getattr(employee, "role", ""):
            return str(employee.role).strip().lower()
        return ""

    @staticmethod
    def _location_code(zone: str, row_no: int, section_no: int, tier_no: int, cell_no: int) -> str:
        values = [str(max(int(value or 0), 0)) for value in (row_no, section_no, tier_no, cell_no)]
        if any(int(value) > 0 for value in values):
            return f"{zone}-{'-'.join(values)}"
        return zone

    @staticmethod
    def _location_display_name(zone: str, row_no: int, section_no: int, tier_no: int, cell_no: int) -> str:
        if zone == "PR":
            return "PR · Зона приемки"
        if zone == "OS":
            if all(int(value or 0) > 0 for value in (row_no, section_no, tier_no, cell_no)):
                return f"OS · Ряд {row_no} · Секция {section_no} · Ярус {tier_no} · Ячейка {cell_no}"
            return "OS · Основной склад"
        if zone == "MR":
            if int(row_no or 0) > 0:
                return f"MR · Ряд {row_no}"
            return "MR · Мезонин"
        if zone == "OBR":
            return "OBR · Зона обработки"
        if zone == "OTG":
            return "OTG · Зона отгрузки"
        return zone

    @classmethod
    def _receiving_items_from_placement_payload(
        cls,
        *,
        order_id: str,
        placement_payload: dict,
    ) -> list[dict]:
        payload = placement_payload if isinstance(placement_payload, dict) else {}
        boxes = payload.get("act_boxes") or []
        pallets = payload.get("act_pallets") or []
        if not isinstance(boxes, list):
            boxes = []
        if not isinstance(pallets, list):
            pallets = []
        default_goods_type = str(payload.get("goods_type") or "").strip()

        box_to_pallet: dict[str, str] = {}
        box_to_location: dict[str, dict] = {}
        items: list[dict] = []
        for pallet in pallets:
            if not isinstance(pallet, dict):
                continue
            pallet_code = str(pallet.get("code") or "").strip()
            if not pallet_code:
                continue
            pallet_goods_type = str(pallet.get("goods_type") or default_goods_type).strip()
            pallet_location = pallet.get("location") if isinstance(pallet.get("location"), dict) else {}
            for box_code in pallet.get("boxes") or []:
                normalized_box_code = str(box_code or "").strip()
                if normalized_box_code:
                    box_to_pallet[normalized_box_code] = pallet_code
                    box_to_location[normalized_box_code] = dict(pallet_location or {})
            for item in pallet.get("items") or []:
                normalized = cls._normalize_receiving_item(
                    item=item,
                    order_id=order_id,
                    pallet_code=pallet_code,
                    box_code="",
                    default_goods_type=pallet_goods_type,
                    location=pallet_location,
                )
                if normalized:
                    items.append(normalized)
        for box in boxes:
            if not isinstance(box, dict):
                continue
            box_code = str(box.get("code") or "").strip()
            pallet_code = box_to_pallet.get(box_code, "")
            box_goods_type = str(box.get("goods_type") or default_goods_type).strip()
            box_location = (
                box.get("location")
                if isinstance(box.get("location"), dict)
                else box_to_location.get(box_code, {})
            )
            for item in box.get("items") or []:
                normalized = cls._normalize_receiving_item(
                    item=item,
                    order_id=order_id,
                    pallet_code=pallet_code,
                    box_code=box_code,
                    default_goods_type=box_goods_type,
                    location=box_location,
                )
                if normalized:
                    items.append(normalized)
        return items

    @staticmethod
    def _normalize_receiving_item(
        *,
        item: dict,
        order_id: str,
        pallet_code: str,
        box_code: str,
        default_goods_type: str = "",
        location: dict | None = None,
    ) -> dict | None:
        if not isinstance(item, dict):
            return None
        qty = max(int(item.get("qty") or 0), 0)
        if qty <= 0:
            return None
        return {
            "order_id": str(order_id or "").strip(),
            "sku_code": str(item.get("sku_code") or item.get("sku") or "").strip(),
            "sku": str(item.get("sku") or item.get("sku_code") or "").strip(),
            "name": str(item.get("name") or "").strip(),
            "size": str(item.get("size") or "").strip(),
            "barcode": str(item.get("barcode") or "").strip(),
            "goods_type": str(item.get("goods_type") or default_goods_type or "").strip(),
            "marking_code": str(item.get("marking_code") or "").strip(),
            "qty": qty,
            "pallet_code": str(pallet_code or "").strip(),
            "box_code": str(box_code or "").strip(),
            "location": dict(location or {}),
        }
