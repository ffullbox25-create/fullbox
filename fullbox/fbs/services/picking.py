from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
import re
import time
import unicodedata

from django.contrib.auth import get_user_model
from django.db import OperationalError, transaction
from django.db.models import (
    Case,
    Count,
    Exists,
    F,
    IntegerField,
    Max,
    OuterRef,
    Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from fbs.exceptions import (
    FbsError,
    FbsFeatureDisabled,
    FbsMarkingAlreadyUsedError,
    FbsPickingError,
    FbsScanMismatchError,
)
from fbs.flags import feature_enabled
from fbs.models import (
    FbsBox,
    FbsControllerToteOrder,
    FbsHandoverOrder,
    FbsHandoverOrderAssignment,
    FbsIntegrationProfile,
    FbsMarketplaceMetadataTransfer,
    FbsOrder,
    FbsOrderItem,
    FbsOrderLabel,
    FbsOrderStockAllocation,
    FbsOrderTraceability,
    FbsPallet,
    FbsPickBatch,
    FbsPickException,
    FbsProblemToteItem,
    FbsPickRestockRequest,
    FbsPickScanEvent,
    FbsPickingCart,
    FbsPickTask,
    FbsRackCellBinding,
    FbsPickVerificationProgress,
    FbsStockBalance,
    FbsWorkstation,
)
from fbs.order_audit import log_order_bulk_transition
from sku.models import Agency, SKUBarcode
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseOperationTask,
    WarehouseStockSnapshot,
)
from sklad.services.operational_locations import normalize_operational_location_scan
from sklad.topology import os_location_code

from ..barcode_aliases import (
    filter_normalized_barcodes,
    normalize_barcode,
    sku_barcode_alias_map,
)
from .traceability import (
    LEGACY_MARKING_ABSENCE_EXPECTED_VALUE,
    controller_legacy_marking_exception_allowed,
    controller_marking_scan_required,
    create_allocation_trace,
    legacy_marking_absence_confirmed,
    metadata_requirements,
    prepare_order_marketplace_metadata,
    set_allocation_trace_status,
)
from .controller_shift import CONTROLLER_SHIFT_HEARTBEAT_TTL
from .physical_locations import (
    fbs_box_is_reservable,
    fbs_box_physical_location,
    fbs_box_physical_location_code,
    fbs_box_physical_location_scan_values,
    fbs_box_reservable_q,
)
from .relocation_reservations import active_free_relocation_for_pick_tasks
from .handover import wb_handover_compatibility_key
from .scanning import record_pick_scan_event
from .wave_policy import MAX_WAVE_SIZE, MIN_WAVE_SIZE, enforce_profile_wave_limits
from .wave_queue import order_wave_queue_queryset


RESERVABLE_ORDER_STATUSES = (
    FbsOrder.STATUS_RECEIVED,
    FbsOrder.STATUS_AWAITING_STOCK,
)
ACTIVE_ALLOCATION_STATUSES = (
    FbsOrderStockAllocation.STATUS_RESERVED,
    FbsOrderStockAllocation.STATUS_PICKING,
)
MARKING_CARRIER_ALLOCATION_STATUSES = (
    FbsOrderStockAllocation.STATUS_RESERVED,
    FbsOrderStockAllocation.STATUS_PICKING,
    FbsOrderStockAllocation.STATUS_PICKED,
)
ACTIVE_TASK_STATUSES = (
    FbsPickTask.STATUS_QUEUED,
    FbsPickTask.STATUS_IN_PROGRESS,
)
ACTIVE_BATCH_STATUSES = (
    FbsPickBatch.STATUS_IN_PROGRESS,
    FbsPickBatch.STATUS_VERIFICATION,
)
CONTROLLER_ACTIVE_WAVE_LIMIT = 3
ACTIVE_CONTROLLER_RESTOCK_STATUSES = (
    FbsPickRestockRequest.STATUS_QUEUED,
    FbsPickRestockRequest.STATUS_IN_PROGRESS,
)
SEPARATED_PICK_RESTOCK_STATUSES = (
    FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
    FbsPickRestockRequest.STATUS_QUEUED,
    FbsPickRestockRequest.STATUS_IN_PROGRESS,
    FbsPickRestockRequest.STATUS_COMPLETED,
    FbsPickRestockRequest.STATUS_FAILED,
)
STOCK_SHORTAGE_POLICY_MARK = "mark"
STOCK_SHORTAGE_POLICY_REQUIRE_CONFIRMATION = "require_confirmation"
STOCK_SHORTAGE_POLICY_SKIP = "skip"
STOCK_SHORTAGE_POLICIES = {
    STOCK_SHORTAGE_POLICY_MARK,
    STOCK_SHORTAGE_POLICY_REQUIRE_CONFIRMATION,
    STOCK_SHORTAGE_POLICY_SKIP,
}
FIRST_PICK_TIER = 1
PICK_ROUTE_COORDINATE_FIELDS = ("tier_no", "section_no", "row_no", "cell_no")
PICK_ROUTE_MISSING_COORDINATE = 10**9
PICK_DEADLOCK_RETRY_ATTEMPTS = 3
PICK_DEADLOCK_RETRY_DELAY_SECONDS = 0.05


def _queue_agency_stock_exports_on_commit(*, agency_id: int) -> None:
    """Refresh every managed marketplace profile after stock availability changes."""
    agency_id = int(agency_id)

    def refresh_exports() -> None:
        # Keep the stock exporter import lazy: stock_sync also imports FBS models
        # used throughout this service, and reservation writes are a hot path.
        from .stock_sync import queue_agency_stock_exports

        queue_agency_stock_exports(agency_id=agency_id)

    transaction.on_commit(refresh_exports, robust=True)


@dataclass(frozen=True)
class FbsOrderReservationResult:
    order_id: int
    status: str
    reserved: bool
    reserved_qty: int
    allocations: tuple[FbsOrderStockAllocation, ...]
    errors: tuple[str, ...] = ()
    shortages: tuple[str, ...] = ()
    missing_qty: int = 0


def pick_route_ordering(location_prefix: str) -> tuple:
    """Return the shared FBS route: tier, line, rack and cell.

    OS ``section_no`` is the line rank (0, A, B, C, D, E, ...).  A complete
    physical address always precedes a technical or incomplete location.
    """
    complete_address = Q()
    for field_name in PICK_ROUTE_COORDINATE_FIELDS:
        complete_address &= Q(**{f"{location_prefix}{field_name}__gt": 0})
    return (
        Case(
            When(complete_address, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        ),
        *(
            F(f"{location_prefix}{field_name}").asc(nulls_last=True)
            for field_name in PICK_ROUTE_COORDINATE_FIELDS
        ),
    )


def _physical_location_sort_key(allocation: FbsOrderStockAllocation) -> tuple:
    location = fbs_box_physical_location(allocation.balance.box)
    coordinates = tuple(
        int(getattr(location, field_name, 0) or 0)
        for field_name in PICK_ROUTE_COORDINATE_FIELDS
    )
    complete_address = all(value > 0 for value in coordinates)
    return (
        0 if complete_address else 1,
        *(
            coordinates
            if complete_address
            else tuple(
                value if value > 0 else PICK_ROUTE_MISSING_COORDINATE
                for value in coordinates
            )
        ),
        allocation.balance.box.box_code,
    )


@dataclass(frozen=True)
class FbsQueueStockShortage:
    order_id: int
    external_order_id: str
    missing_qty: int
    reasons: tuple[str, ...]


class FbsQueueStockConfirmationRequired(FbsPickingError):
    def __init__(self, shortages: tuple[FbsQueueStockShortage, ...]):
        self.shortages = shortages
        super().__init__(
            "Волна не запущена по причине отсутствия товара на остатках."
        )


@dataclass(frozen=True)
class FbsQueuePreparationResult:
    inspected_orders: int
    reserved_orders: int
    awaiting_stock_orders: int
    validation_failed_orders: int
    batches: tuple[FbsPickBatch, ...]
    tasks_created: int
    rejected_orders: tuple["FbsQueueOrderRejection", ...] = ()


@dataclass(frozen=True)
class FbsQueuedPickRegroupResult:
    batch_count: int
    moved_task_count: int
    emptied_batch_count: int
    skipped_batch_count: int = 0


@dataclass(frozen=True)
class FbsQueueOrderRejection:
    order_id: int
    external_order_id: str
    marketplace_status: str
    marketplace_substatus: str
    reason: str


@dataclass(frozen=True)
class FbsWorkstationRecommendation:
    workstation_id: int
    name: str
    barcode: str
    queued_waves: int
    capacity: int

    @property
    def available_slots(self) -> int:
        return max(self.capacity - self.queued_waves, 0)


def _active_controller_waves(*, workstation_id=None, controller_id=None):
    queryset = (
        FbsPickBatch.objects.select_for_update()
        .filter(
            status=FbsPickBatch.STATUS_VERIFICATION,
            picking_completed_at__isnull=False,
            cart__isnull=False,
            cart_released_at__isnull=True,
            completed_at__isnull=True,
        )
        .exclude(
            pk__in=FbsPickRestockRequest.objects.filter(
                status__in=ACTIVE_CONTROLLER_RESTOCK_STATUSES,
            ).values("batch_id"),
        )
    )
    if workstation_id is not None:
        queryset = queryset.filter(workstation_id=workstation_id)
    if controller_id is not None:
        queryset = queryset.filter(verification_assigned_to_id=controller_id)
    return queryset


def _assert_controller_workstation_capacity(
    *,
    workstation: FbsWorkstation,
    batch_id: int,
) -> None:
    occupied_count = (
        _active_controller_waves(workstation_id=workstation.id)
        .exclude(pk=batch_id)
        .count()
    )
    capacity = max(int(workstation.max_parallel_waves or 1), 1)
    if occupied_count >= capacity:
        if capacity == 1:
            tare_label = "единица тары"
        elif capacity <= 4:
            tare_label = "единицы тары"
        else:
            tare_label = "единиц тары"
        raise FbsPickingError(
            f"На рабочем месте уже {capacity} {tare_label}. "
            "Передайте волну после освобождения места."
        )


def _controller_workstation_loads(*, workstation_ids: list[int]) -> dict[int, int]:
    if not workstation_ids:
        return {}
    rows = (
        FbsPickBatch.objects.filter(
            workstation_id__in=workstation_ids,
            status=FbsPickBatch.STATUS_VERIFICATION,
            cart__isnull=False,
            cart_released_at__isnull=True,
            completed_at__isnull=True,
        )
        .exclude(
            pk__in=FbsPickRestockRequest.objects.filter(
                status__in=ACTIVE_CONTROLLER_RESTOCK_STATUSES,
            ).values("batch_id"),
        )
        .values("workstation_id")
        .annotate(total=Count("id"))
    )
    return {
        int(row["workstation_id"]): int(row["total"] or 0)
        for row in rows
    }


def _recommendation_for(
    *, workstation: FbsWorkstation, queued_waves: int
) -> FbsWorkstationRecommendation:
    return FbsWorkstationRecommendation(
        workstation_id=workstation.id,
        name=workstation.name,
        barcode=workstation.barcode,
        queued_waves=max(int(queued_waves or 0), 0),
        capacity=max(int(workstation.max_parallel_waves or 1), 1),
    )


def _select_handover_workstation_for_update() -> tuple[FbsWorkstation, int] | None:
    now = timezone.now()
    online_threshold = now - timedelta(seconds=60)
    controller_threshold = now - CONTROLLER_SHIFT_HEARTBEAT_TTL
    workstations = list(
        # Serialize capacity decisions on the workstation rows only.  The
        # related DeviceAgent is heartbeat-owned and must stay writable while
        # a controller wave is assigned.  The batch itself is locked by
        # ``assign_pick_handover_workstation``, so one wave/order set can still
        # belong to only one controller workstation.
        FbsWorkstation.objects.select_for_update(of=("self",))
        .select_related("device_agent")
        .filter(
            is_active=True,
            device_agent__last_seen__gte=online_threshold,
            shift_status=FbsWorkstation.SHIFT_AVAILABLE,
            shift_controller__isnull=False,
            shift_heartbeat_at__gte=controller_threshold,
        )
        .exclude(printer_name="")
        .order_by("id")
    )
    loads = _controller_workstation_loads(
        workstation_ids=[workstation.id for workstation in workstations]
    )
    available = [
        workstation
        for workstation in workstations
        if loads.get(workstation.id, 0)
        < max(int(workstation.max_parallel_waves or 1), 1)
    ]
    if not available:
        return None
    workstation = min(
        available,
        key=lambda item: (loads.get(item.id, 0), item.id),
    )
    return workstation, loads.get(workstation.id, 0)


@transaction.atomic
def assign_pick_handover_workstation(
    *, batch_id: int
) -> FbsWorkstationRecommendation | None:
    """Reserve the least-loaded online controller desk for a completed wave."""
    _require_picking_writes()
    batch = (
        FbsPickBatch.objects.select_for_update(of=("self",))
        .select_related("workstation")
        .get(pk=batch_id)
    )
    if batch.status != FbsPickBatch.STATUS_VERIFICATION:
        return None
    if int(batch.picked_qty or 0) != int(batch.planned_qty or 0):
        return None
    if batch.workstation_id is not None:
        loads = _controller_workstation_loads(
            workstation_ids=[batch.workstation_id]
        )
        return _recommendation_for(
            workstation=batch.workstation,
            queued_waves=loads.get(batch.workstation_id, 0),
        )

    selected = _select_handover_workstation_for_update()
    if selected is None:
        return None
    workstation, queue_before = selected
    batch.workstation = workstation
    batch.save(update_fields=["workstation", "updated_at"])
    return _recommendation_for(
        workstation=workstation,
        queued_waves=queue_before + 1,
    )


def format_queue_rejections(
    rejected_orders: tuple[FbsQueueOrderRejection, ...],
    *,
    limit: int = 8,
) -> str:
    rows = tuple(rejected_orders)
    if not rows:
        return ""
    visible = rows[: max(int(limit), 1)]
    message = f"Не включены в волну {len(rows)} заказ(ов): " + "; ".join(
        row.reason for row in visible
    )
    hidden_count = len(rows) - len(visible)
    if hidden_count:
        message += f"; еще {hidden_count} заказ(ов) не показано."
    return message


def _require_module() -> None:
    if not feature_enabled("module"):
        raise FbsFeatureDisabled("Модуль FBS выключен.")


def _require_picking_writes() -> None:
    _require_module()
    if not feature_enabled("warehouse_writes"):
        raise FbsFeatureDisabled("Операции отбора FBS выключены.")


def _authenticated_user(user):
    return user if getattr(user, "is_authenticated", False) else None


def _normalized(value: str) -> str:
    return str(value or "").strip().casefold()


def _allocation_product_barcodes(
    allocation: FbsOrderStockAllocation,
) -> tuple[str, ...]:
    """Return every product barcode accepted for this physical allocation."""
    barcodes = []
    normalized_seen = set()
    for raw_value in (
        allocation.balance.barcode,
        allocation.order_item.barcode,
    ):
        value = str(raw_value or "").strip()
        normalized = _normalized(value)
        if not normalized or normalized in normalized_seen:
            continue
        normalized_seen.add(normalized)
        barcodes.append(value)
    return tuple(barcodes)


_TEXT_FNC1_RE = re.compile(r"\{FNC1\}", re.IGNORECASE)
_KIZ_RE = re.compile(r"01\d{14}21[!-~](?:\x1d|[!-~])*")
_GTIN_LENGTHS = (8, 12, 13, 14)


def _canonicalize_kiz_scan(marking_scan: str) -> str:
    scanned_marking = str(marking_scan or "").strip()
    if scanned_marking[:3].casefold() == "]d2":
        scanned_marking = scanned_marking[3:]
    scanned_marking = _TEXT_FNC1_RE.sub("\x1d", scanned_marking)
    scanned_marking = scanned_marking.strip("\x1d")
    starts = [match.start() for match in re.finditer(r"01\d{14}21", scanned_marking)]
    if len(starts) == 2 and starts[0] == 0:
        first = scanned_marking[: starts[1]].strip("\x1d")
        second = scanned_marking[starts[1] :].strip("\x1d")
        first_compact = first.replace("\x1d", "")
        second_compact = second.replace("\x1d", "")
        # Some scanners emit the identification part and then the full payload
        # of the same physical Data Matrix in one event. Keep the longer copy
        # only when the shorter value is its exact prefix; two different codes
        # remain concatenated and are rejected by validation below.
        if len(second) > len(first) and second_compact.startswith(first_compact):
            return second
        if len(first) > len(second) and first_compact.startswith(second_compact):
            return first
    return scanned_marking


def _looks_like_kiz_scan(scan_value: str) -> bool:
    raw_value = str(scan_value or "").strip()
    canonical = _canonicalize_kiz_scan(raw_value)
    return raw_value[:3].casefold() == "]d2" or bool(
        len(canonical) >= 18 and re.match(r"^01\d{14}21", canonical)
    )


def _product_gtin14(product_barcode: str) -> str:
    value = str(product_barcode or "").strip()
    if not value.isdigit() or len(value) not in _GTIN_LENGTHS:
        return ""
    return value.zfill(14)


def _kiz_gtin14(marking_scan: str) -> str:
    canonical = _validate_kiz_scan(marking_scan)
    return canonical[2:16]


def resolve_verification_item_scan(
    allocation: FbsOrderStockAllocation,
    item_scan: str,
) -> tuple[str, str]:
    """Resolve a product scan, accepting a matching KIZ as a combined scan."""
    expected_barcodes = _allocation_product_barcodes(allocation)
    normalized_scan = _normalized(item_scan)
    for barcode in expected_barcodes:
        if normalized_scan == _normalized(barcode):
            return barcode, ""
    if not _looks_like_kiz_scan(item_scan):
        return "", ""
    canonical_marking = _validate_kiz_scan(item_scan)
    marking_gtin = canonical_marking[2:16]
    for barcode in expected_barcodes:
        if _product_gtin14(barcode) == marking_gtin:
            return barcode, canonical_marking
    raise FbsScanMismatchError(
        "Честный знак относится к другому товару. "
        "Скан не принят; отсканируйте нужный товар."
    )


def wb_optional_marking_available(item: FbsOrderItem) -> bool:
    """Return whether WB allows a physical KIZ even when it is not mandatory."""
    order = getattr(item, "order", None)
    profile = getattr(order, "profile", None)
    if (
        str(getattr(profile, "marketplace", "") or "").strip().casefold()
        != FbsIntegrationProfile.MARKETPLACE_WB
    ):
        return False
    requirements = item.requirements if isinstance(item.requirements, dict) else {}
    optional_meta = requirements.get("optional_meta")
    if isinstance(optional_meta, (list, tuple, set)) and any(
        str(value or "").strip().casefold() == "sgtin" for value in optional_meta
    ):
        return True
    wb_meta = requirements.get("wb_meta")
    wb_sgtin = wb_meta.get("sgtin") if isinstance(wb_meta, dict) else None
    if not isinstance(wb_sgtin, dict):
        return False
    return bool(
        wb_sgtin.get("available")
        and str(wb_sgtin.get("decision") or "").strip().casefold() == "optional"
    )


def _validate_kiz_scan(marking_scan: str, *, product_barcodes=()) -> str:
    scanned_marking = _canonicalize_kiz_scan(marking_scan)
    if not scanned_marking:
        raise FbsScanMismatchError("Отсканируйте КИЗ Честного знака повторно.")
    normalized_products = {
        _normalized(barcode) for barcode in product_barcodes if str(barcode or "").strip()
    }
    if _normalized(scanned_marking) in normalized_products:
        raise FbsScanMismatchError(
            "Отсканирован штрихкод товара вместо КИЗа. "
            "Отсканируйте Честный знак повторно."
        )
    if any("CYRILLIC" in unicodedata.name(char, "") for char in scanned_marking):
        raise FbsScanMismatchError(
            "КИЗ содержит русские буквы. Переключите раскладку ТСД на EN "
            "и отсканируйте один Data Matrix повторно."
        )
    if any(char != "\x1d" and not ("!" <= char <= "~") for char in scanned_marking):
        raise FbsScanMismatchError(
            "КИЗ содержит недопустимые символы. "
            "Отсканируйте Честный знак повторно."
        )
    if not 19 <= len(scanned_marking) <= 255:
        raise FbsScanMismatchError(
            "Неверная длина КИЗа. Отсканируйте Честный знак повторно."
        )
    if len(re.findall(r"01\d{14}21", scanned_marking)) > 1:
        raise FbsScanMismatchError(
            "В поле попали два КИЗа подряд. Отсканируйте только один Data Matrix "
            "и дождитесь результата проверки."
        )
    if _KIZ_RE.fullmatch(scanned_marking) is None:
        raise FbsScanMismatchError(
            "Неверный формат КИЗа Data Matrix. "
            "Отсканируйте Честный знак повторно."
        )
    return scanned_marking


def _restore_wb_kiz_gs_separators(
    marking_scan: str,
    *,
    reference_markings=(),
) -> str:
    """Restore lost GS separators in a WB tobacco/consumer KIZ when unambiguous.

    Some keyboard/scanner paths drop FNC1 (ASCII GS) while passing the rest of
    the Data Matrix payload unchanged.  Prefer an exact stored physical code;
    otherwise restore only the single valid GS1 layout with a 1..20 character
    AI 21 serial followed by fixed-length AI 91 and variable AI 92.
    """
    scanned_marking = _canonicalize_kiz_scan(marking_scan)
    if not scanned_marking or "\x1d" in scanned_marking:
        return scanned_marking

    matching_references = {
        reference
        for raw_reference in reference_markings
        if (reference := _canonicalize_kiz_scan(raw_reference))
        and "\x1d" in reference
        and reference.replace("\x1d", "") == scanned_marking
    }
    if len(matching_references) == 1:
        return matching_references.pop()

    candidates = []
    # 01 + GTIN14 + 21 consumes 18 characters. AI 21 is variable (1..20),
    # AI 91 contains four characters, then AI 92 carries the crypto tail.
    for ai91_offset in range(19, min(38, len(scanned_marking) - 9) + 1):
        if (
            scanned_marking[ai91_offset : ai91_offset + 2] == "91"
            and scanned_marking[ai91_offset + 6 : ai91_offset + 8] == "92"
        ):
            candidates.append(
                scanned_marking[:ai91_offset]
                + "\x1d"
                + scanned_marking[ai91_offset : ai91_offset + 6]
                + "\x1d"
                + scanned_marking[ai91_offset + 6 :]
            )
    if len(candidates) == 1:
        return candidates[0]
    return scanned_marking


def _confirmed_invalid_kiz_rewave_allows_queue(order: FbsOrder) -> bool:
    """Allow only the exact replacement-pick transition after a confirmed move."""
    from .pick_restock import invalid_kiz_reroute_context

    assignment = (
        FbsHandoverOrderAssignment.objects.select_related("batch")
        .filter(
            order=order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )
        .first()
    )
    if assignment is None:
        return False
    context = invalid_kiz_reroute_context(assignment.batch)
    if (
        context is None
        or context["route"] != "rewave"
        or int(context["order_id"]) != order.id
    ):
        return False
    source_batch_id = int(context["source_batch_id"])
    source_links = FbsHandoverOrder.objects.filter(
        order=order,
        box__batch_id=source_batch_id,
    )
    if source_links.exists() and not source_links.filter(
        status=FbsHandoverOrder.STATUS_EXCLUDED,
    ).exists():
        return False
    if not FbsOrderStockAllocation.objects.filter(
        order_item__order=order,
        status=FbsOrderStockAllocation.STATUS_PICKED,
        pick_task__status=FbsPickTask.STATUS_EXCEPTION,
        exceptions__status=FbsPickException.STATUS_OPEN,
    ).exists():
        return False
    if not FbsProblemToteItem.objects.filter(
        order=order,
        status=FbsProblemToteItem.STATUS_IN_TOTE,
        severity=FbsProblemToteItem.SEVERITY_CRITICAL,
    ).exists():
        return False
    return FbsMarketplaceMetadataTransfer.objects.filter(
        order_item__order=order,
        metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
        is_required=True,
        status=FbsMarketplaceMetadataTransfer.STATUS_CANCELED,
        last_error__icontains="невалидный КИЗ",
    ).exists()


def _marketplace_queue_error(
    order: FbsOrder,
    *,
    allow_confirmed_invalid_kiz_rewave: bool = False,
) -> str:
    from .sync import marketplace_order_queue_error

    error = marketplace_order_queue_error(order)
    if (
        error
        and allow_confirmed_invalid_kiz_rewave
        and _confirmed_invalid_kiz_rewave_allows_queue(order)
    ):
        return ""
    return error


def _queue_rejection(order: FbsOrder, reason: str) -> FbsQueueOrderRejection:
    return FbsQueueOrderRejection(
        order_id=order.id,
        external_order_id=order.external_order_id,
        marketplace_status=order.marketplace_status,
        marketplace_substatus=order.marketplace_substatus,
        reason=reason,
    )


def _required_marking_codes(item: FbsOrderItem) -> tuple[str, ...]:
    requirements = item.requirements if isinstance(item.requirements, dict) else {}
    collected: list[str] = []
    for field in ("wb_marking_codes", "marking_codes", "kiz_codes"):
        raw_codes = requirements.get(field) or ()
        if isinstance(raw_codes, str):
            raw_codes = [raw_codes]
        if not isinstance(raw_codes, (list, tuple, set)):
            continue
        codes = tuple(
            str(code or "").strip()
            for code in raw_codes
            if str(code or "").strip()
        )
        if len(codes) != len(set(codes)):
            raise FbsPickingError(f"В строке {item.external_line_id} повторяется КИЗ.")
        for code in codes:
            if code not in collected:
                collected.append(code)
    return tuple(collected)


def _assert_required_marking_code(item: FbsOrderItem, marking_code: str) -> None:
    required_codes = {_normalized(code) for code in _required_marking_codes(item)}
    if required_codes and _normalized(marking_code) not in required_codes:
        raise FbsPickingError("КИЗ не совпадает с кодом, переданным маркетплейсом.")


def _minimum_expiry(item: FbsOrderItem) -> date | None:
    requirements = item.requirements if isinstance(item.requirements, dict) else {}
    value = requirements.get("min_expiry_date")
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise FbsPickingError(
            f"В строке {item.external_line_id} указан неверный минимальный срок годности."
        ) from exc


def _reservation_result_from_existing(order: FbsOrder) -> FbsOrderReservationResult | None:
    allocations = tuple(
        FbsOrderStockAllocation.objects.filter(
            order_item__order=order,
            status__in=ACTIVE_ALLOCATION_STATUSES,
        ).order_by("order_item_id", "id")
    )
    if not allocations:
        return None
    if order.internal_status not in {
        FbsOrder.STATUS_RESERVED,
        FbsOrder.STATUS_QUEUED_FOR_PICK,
        FbsOrder.STATUS_PICKING,
    }:
        raise FbsPickingError("У заказа обнаружен активный резерв с несовместимым статусом.")
    return FbsOrderReservationResult(
        order_id=order.id,
        status=order.internal_status,
        reserved=True,
        reserved_qty=sum(allocation.qty_reserved for allocation in allocations),
        allocations=allocations,
    )


@transaction.atomic
def reserve_order_stock(
    *,
    order_id: int,
    reserved_by=None,
    allow_confirmed_invalid_kiz_rewave: bool = False,
) -> FbsOrderReservationResult:
    _require_picking_writes()
    order = (
        FbsOrder.objects.select_for_update(of=("self",))
        .select_related("profile__agency")
        .get(pk=order_id)
    )
    queue_error = _marketplace_queue_error(
        order,
        allow_confirmed_invalid_kiz_rewave=allow_confirmed_invalid_kiz_rewave,
    )
    if queue_error:
        raise FbsPickingError(queue_error)
    existing = _reservation_result_from_existing(order)
    if existing is not None:
        _queue_agency_stock_exports_on_commit(agency_id=order.profile.agency_id)
        return existing
    if order.internal_status not in RESERVABLE_ORDER_STATUSES:
        raise FbsPickingError("Заказ нельзя резервировать в текущем статусе.")

    items = list(
        FbsOrderItem.objects.select_for_update(of=("self",))
        .select_related("sku")
        .filter(order=order)
        .order_by("id")
    )
    errors = []
    if not items:
        errors.append("В заказе нет товарных строк.")
    for item in items:
        if item.sku_id is None:
            errors.append(f"Строка {item.external_line_id}: SKU не привязан.")
        elif item.sku.agency_id not in {None, order.profile.agency_id}:
            errors.append(f"Строка {item.external_line_id}: SKU принадлежит другому клиенту.")
        item_barcode = str(item.barcode or "").strip()
        if not item_barcode:
            errors.append(f"Строка {item.external_line_id}: штрихкод не указан.")
        elif item.sku_id:
            catalog_sku_ids = set(
                filter_normalized_barcodes(
                    SKUBarcode.objects.filter(
                        sku__agency_id=order.profile.agency_id,
                        sku__deleted=False,
                    ),
                    (item_barcode,),
                    field_name="value",
                ).values_list("sku_id", flat=True)
            )
            if item.sku_id not in catalog_sku_ids:
                errors.append(
                    f"Строка {item.external_line_id}: штрихкод не привязан к SKU клиента."
                )
            elif catalog_sku_ids != {item.sku_id}:
                errors.append(
                    f"Строка {item.external_line_id}: штрихкод без учета регистра "
                    "привязан к нескольким SKU клиента."
                )
        try:
            _minimum_expiry(item)
        except FbsPickingError as exc:
            errors.append(str(exc))
            continue
    if errors:
        order.internal_status = FbsOrder.STATUS_VALIDATION_FAILED
        order.save(update_fields=["internal_status", "updated_at"])
        return FbsOrderReservationResult(
            order_id=order.id,
            status=order.internal_status,
            reserved=False,
            reserved_qty=0,
            allocations=(),
            errors=tuple(errors),
        )

    barcode_aliases = sku_barcode_alias_map(
        (item.sku_id, item.barcode) for item in items
    )
    aliases_by_item_id = {
        item.id: barcode_aliases.get(
            (int(item.sku_id), normalize_barcode(item.barcode)),
            (str(item.barcode or "").strip(),),
        )
        for item in items
    }
    barcodes = {
        barcode
        for aliases in aliases_by_item_id.values()
        for barcode in aliases
        if barcode
    }
    from .inventory import (
        cancel_inventory_locks_for_order_reservation,
        with_fbs_lock_state,
    )

    balances_queryset = (
        FbsStockBalance.objects.select_for_update(of=("self",))
            .select_related(
                "box__pallet__cell__location",
                "box__source_container__current_location",
                "sku_ref",
            )
            .filter(
                agency=order.profile.agency,
                available_qty__gt=0,
            )
            .filter(fbs_box_reservable_q())
            .filter(Q(expiry_date__isnull=True) | Q(expiry_date__gte=timezone.localdate()))
            .order_by(
                F("expiry_date").asc(nulls_last=True),
                "box__pallet__cell__location__tier_no",
                "box__pallet__cell__cell_code",
                "box__box_code",
                "id",
            )
    )
    balances_with_lock_state = list(
        with_fbs_lock_state(
            filter_normalized_barcodes(balances_queryset, barcodes),
            ignore_internal_movement=True,
        )
    )
    inventory_locked_balance_keys = {
        (balance.sku_ref_id, _normalized(balance.barcode))
        for balance in balances_with_lock_state
        if balance._fbs_is_locked
    }
    balances = [balance for balance in balances_with_lock_state if not balance._fbs_is_locked]
    virtual_available = {}
    for balance in balances:
        virtual_available[balance.id] = int(balance.available_qty or 0)

    reservations: list[tuple[FbsOrderItem, FbsStockBalance, int]] = []
    shortages = []
    missing_qty = 0
    for item in items:
        min_expiry = _minimum_expiry(item)
        remaining = int(item.quantity or 0)
        item_barcode = _normalized(item.barcode)
        item_aliases = {
            _normalized(barcode)
            for barcode in aliases_by_item_id[item.id]
            if _normalized(barcode)
        }
        candidates = [
            balance
            for balance in balances
            if _normalized(balance.barcode) in item_aliases
            and (
                balance.sku_ref_id == item.sku_id
                or (
                    balance.sku_ref_id is None
                    and _normalized(balance.barcode) == item_barcode
                )
            )
        ]
        if min_expiry is not None:
            candidates = [
                balance
                for balance in candidates
                if balance.expiry_date is None or balance.expiry_date >= min_expiry
            ]
        if bool(getattr(item.sku, "honest_sign", False)):
            candidates.sort(
                key=lambda balance: 0
                if str(balance.marking_code or "").strip()
                else 1
            )
        for balance in candidates:
            if remaining <= 0:
                break
            available = virtual_available.get(balance.id, 0)
            if available <= 0:
                continue
            qty = min(remaining, available)
            if balance.marking_code:
                qty = min(qty, 1)
            reservations.append((item, balance, qty))
            virtual_available[balance.id] -= qty
            remaining -= qty
        if remaining > 0:
            missing_qty += remaining
            shortages.append(
                f"ШК {item.barcode} ({item.sku.sku_code}): не хватает {remaining} шт."
            )

    if shortages:
        blocking_balance_ids = {
            balance.id
            for balance in balances_with_lock_state
            if balance._fbs_is_locked
            and any(
                (
                    balance.sku_ref_id == item.sku_id
                    and _normalized(balance.barcode)
                    in {
                        _normalized(barcode)
                        for barcode in aliases_by_item_id[item.id]
                        if _normalized(barcode)
                    }
                )
                or (
                    balance.sku_ref_id is None
                    and _normalized(balance.barcode) == _normalized(item.barcode)
                )
                for item in items
            )
        }
        if blocking_balance_ids:
            canceled_session_ids = cancel_inventory_locks_for_order_reservation(
                balance_ids=blocking_balance_ids,
                order_id=order.id,
                external_order_id=order.external_order_id,
                canceled_by=reserved_by,
            )
            if canceled_session_ids:
                return reserve_order_stock(
                    order_id=order.id,
                    reserved_by=reserved_by,
                    allow_confirmed_invalid_kiz_rewave=(
                        allow_confirmed_invalid_kiz_rewave
                    ),
                )
        order.internal_status = FbsOrder.STATUS_AWAITING_STOCK
        if any(
            any(
                (item.sku_id, _normalized(barcode)) in inventory_locked_balance_keys
                or (
                    _normalized(barcode) == _normalized(item.barcode)
                    and (None, _normalized(barcode)) in inventory_locked_balance_keys
                )
                for barcode in aliases_by_item_id[item.id]
            )
            for item in items
        ):
            order.hold_reason = "inventory"
            shortages.append("Часть FBS-остатка временно закрыта инвентаризацией.")
        else:
            order.hold_reason = "stock_shortage"
        order.save(update_fields=["internal_status", "hold_reason", "updated_at"])
        return FbsOrderReservationResult(
            order_id=order.id,
            status=order.internal_status,
            reserved=False,
            reserved_qty=0,
            allocations=(),
            shortages=tuple(shortages),
            missing_qty=missing_qty,
        )

    allocations = []
    actor = _authenticated_user(reserved_by)
    for item, balance, qty in reservations:
        balance.available_qty = int(balance.available_qty or 0) - qty
        balance.reserved_qty = int(balance.reserved_qty or 0) + qty
        balance.save(update_fields=["available_qty", "reserved_qty", "updated_at"])
        allocation = FbsOrderStockAllocation.objects.create(
            order_item=item,
            balance=balance,
            qty_reserved=qty,
            reserved_by=actor,
        )
        create_allocation_trace(allocation)
        allocations.append(allocation)
    order.internal_status = FbsOrder.STATUS_RESERVED
    order.hold_reason = ""
    order.problem_reason = ""
    order.save(update_fields=["internal_status", "hold_reason", "problem_reason", "updated_at"])
    _queue_agency_stock_exports_on_commit(agency_id=order.profile.agency_id)
    return FbsOrderReservationResult(
        order_id=order.id,
        status=order.internal_status,
        reserved=True,
        reserved_qty=sum(allocation.qty_reserved for allocation in allocations),
        allocations=tuple(allocations),
    )


def pick_order_priority_key(order: FbsOrder) -> tuple[object, object, object, int]:
    """Return the canonical oldest-first priority for every FBS wave path.

    A marketplace deadline wins when it exists. Orders without a deadline are
    ordered by their marketplace creation time, with import time and the row id
    providing stable tie-breakers.
    """
    age_at = order.ordered_at or order.imported_at
    return (
        order.cutoff_at or age_at,
        age_at,
        order.imported_at,
        order.id,
    )


def order_by_pick_priority(queryset):
    """Apply the same oldest-first priority at the database boundary."""
    return queryset.order_by(
        Coalesce("cutoff_at", "ordered_at", "imported_at"),
        Coalesce("ordered_at", "imported_at"),
        "imported_at",
        "id",
    )


def _allocation_location_group_key(
    allocation: FbsOrderStockAllocation,
) -> tuple[object, ...] | None:
    """Return the physical cell and exact box used to keep a wave compact."""
    balance = getattr(allocation, "balance", None)
    box = getattr(balance, "box", None)
    if box is None:
        return None
    location = fbs_box_physical_location(box)
    if location is None:
        return None
    location_id = getattr(location, "pk", None)
    if location_id is None:
        location_id = getattr(location, "id", None)
    if location_id is not None:
        location_key = ("location", location_id)
    else:
        location_code = str(
            getattr(location, "location_code", "") or ""
        ).strip().upper()
        if location_code:
            location_key = ("code", location_code)
        else:
            coordinates = tuple(
                int(getattr(location, field, 0) or 0)
                for field in ("row_no", "section_no", "tier_no", "cell_no")
            )
            if not any(coordinates):
                return None
            location_key = ("coordinates", *coordinates)
    box_id = getattr(box, "pk", None) or getattr(box, "id", None)
    box_key = (
        ("box", box_id)
        if box_id is not None
        else ("box_code", str(getattr(box, "box_code", "") or "").strip().upper())
    )
    if not box_key[1]:
        return None
    return (location_key, box_key)


def _allocation_location_unit_counts(
    allocations,
) -> Counter[tuple[object, ...]]:
    counts: Counter[tuple[object, ...]] = Counter()
    for allocation in allocations:
        location_key = _allocation_location_group_key(allocation)
        if location_key is None:
            continue
        quantity = max(int(getattr(allocation, "qty_reserved", 0) or 0), 0)
        if quantity:
            counts[location_key] += quantity
    return counts


def _location_grouping_priority(
    order: FbsOrder,
    *,
    locations_by_order: dict[int, Counter[tuple[object, ...]]],
    current_location_counts: Counter[tuple[object, ...]],
) -> tuple:
    """Keep oldest orders first; optimize boxes and cells only on a tie."""
    order_locations = locations_by_order.get(order.id, Counter())
    current_cells = {location_key[0] for location_key in current_location_counts}
    shared_box_units = sum(
        quantity
        for location_key, quantity in order_locations.items()
        if location_key in current_location_counts
    )
    shared_boxes = sum(
        1 for location_key in order_locations if location_key in current_location_counts
    )
    shared_cell_units = sum(
        quantity
        for location_key, quantity in order_locations.items()
        if location_key[0] in current_cells
    )
    shared_cells = len(
        {
            location_key[0]
            for location_key in order_locations
            if location_key[0] in current_cells
        }
    )
    new_cells = len(
        {location_key[0] for location_key in order_locations}
        - current_cells
    )
    return (
        *pick_order_priority_key(order),
        -shared_box_units,
        -shared_boxes,
        -shared_cell_units,
        -shared_cells,
        new_cells,
    )


def _pick_order_handover_compatibility_key(order: FbsOrder) -> str:
    """Return the shipment identity that must stay homogeneous in one wave."""
    if order.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        return f"wb:{wb_handover_compatibility_key(order)}"
    return f"{order.profile.marketplace}:profile:{order.profile_id}"


def _pick_batch_handover_compatibility_keys(batch: FbsPickBatch) -> set[str]:
    order_ids = batch.tasks.exclude(
        status=FbsPickTask.STATUS_CANCELED
    ).values("order_id")
    orders = (
        FbsOrder.objects.select_related("profile")
        .prefetch_related("items")
        .filter(pk__in=order_ids)
    )
    return {_pick_order_handover_compatibility_key(order) for order in orders}


def _assert_pick_orders_handover_compatible(
    *, existing_tasks: list[FbsPickTask], orders: list[FbsOrder]
) -> None:
    order_ids = {
        *(task.order_id for task in existing_tasks),
        *(order.id for order in orders),
    }
    if not order_ids:
        return
    locked_orders = (
        FbsOrder.objects.select_related("profile")
        .prefetch_related("items")
        .filter(pk__in=order_ids)
    )
    compatibility_keys = {
        _pick_order_handover_compatibility_key(order)
        for order in locked_orders
    }
    if len(compatibility_keys) > 1:
        raise FbsPickingError(
            "В одну волну нельзя добавлять несовместимые заказы WB: "
            "B2B/B2C, cargoType или направление доставки должны быть раздельными."
        )


def _ordered_pick_batch_chunks(
    *,
    orders_by_profile: dict[int, list[FbsOrder]],
    allocations_by_order: dict[int, list[FbsOrderStockAllocation]],
    max_orders_per_batch: int,
    max_units_per_batch: int | None,
) -> tuple[tuple[int, int, list[FbsOrder]], ...]:
    batch_chunks: list[tuple[tuple, int, int, list[FbsOrder]]] = []
    for profile_id in sorted(orders_by_profile):
        profile_orders = orders_by_profile[profile_id]
        if not profile_orders:
            continue
        profile = profile_orders[0].profile
        agency_id = profile.agency_id
        effective_limits = enforce_profile_wave_limits(
            profile,
            requested_max_orders=max_orders_per_batch,
            requested_max_units=max_units_per_batch,
        )
        profile_max_orders = effective_limits.max_orders
        profile_max_units = effective_limits.max_units
        quantities_by_order = {
            order.id: sum(
                int(allocation.qty_reserved or 0)
                for allocation in allocations_by_order[order.id]
            )
            for order in profile_orders
        }
        locations_by_order = {
            order.id: _allocation_location_unit_counts(
                allocations_by_order.get(order.id, ())
            )
            for order in profile_orders
        }
        chunks: list[list[FbsOrder]] = []
        compatible_orders: dict[str, list[FbsOrder]] = defaultdict(list)
        for order in profile_orders:
            compatible_orders[_pick_order_handover_compatibility_key(order)].append(order)
        for compatibility_key in sorted(compatible_orders):
            pending = sorted(
                compatible_orders[compatibility_key],
                key=pick_order_priority_key,
            )
            while pending:
                seed = pending.pop(0)
                seed_qty = quantities_by_order[seed.id]
                if profile_max_units is not None and seed_qty > profile_max_units:
                    chunks.append([seed])
                    continue
                chunk = [seed]
                chunk_qty = seed_qty
                chunk_location_counts = Counter(locations_by_order[seed.id])
                while len(chunk) < profile_max_orders:
                    candidates = [
                        order
                        for order in pending
                        if (
                            profile_max_units is None
                            or chunk_qty + quantities_by_order[order.id]
                            <= profile_max_units
                        )
                    ]
                    if not candidates:
                        break
                    next_order = min(
                        candidates,
                        key=lambda order: _location_grouping_priority(
                            order,
                            locations_by_order=locations_by_order,
                            current_location_counts=chunk_location_counts,
                        ),
                    )
                    pending.remove(next_order)
                    chunk.append(next_order)
                    chunk_qty += quantities_by_order[next_order.id]
                    chunk_location_counts.update(locations_by_order[next_order.id])
                chunks.append(chunk)
        for profile_chunk in chunks:
            oldest_order = min(profile_chunk, key=pick_order_priority_key)
            oldest_key = pick_order_priority_key(oldest_order)
            batch_chunks.append(
                (oldest_key, agency_id, profile_id, profile_chunk)
            )
    return tuple(
        (agency_id, profile_id, chunk)
        for _oldest_key, agency_id, profile_id, chunk in sorted(
            batch_chunks
        )
    )


def _locked_unstarted_batch_tasks(
    batch: FbsPickBatch,
    *,
    strict: bool,
) -> list[FbsPickTask] | None:
    tasks = list(
        FbsPickTask.objects.select_for_update()
        .filter(batch=batch)
        .order_by("sort_order", "id")
    )
    unexpected = [
        task
        for task in tasks
        if task.status
        not in {FbsPickTask.STATUS_QUEUED, FbsPickTask.STATUS_CANCELED}
    ]
    if unexpected:
        if strict:
            raise FbsPickingError(
                f"Волна #{batch.id} уже содержит задания, взятые в работу."
            )
        return None
    return [task for task in tasks if task.status == FbsPickTask.STATUS_QUEUED]


def _unstarted_pick_batches():
    """Only untouched waves may receive additional orders, including in previews."""
    return (
        FbsPickBatch.objects.filter(
            status=FbsPickBatch.STATUS_QUEUED,
            assigned_to__isnull=True,
            workstation__isnull=True,
            cart__isnull=True,
            started_at__isnull=True,
            claimed_at__isnull=True,
            picking_completed_at__isnull=True,
            verification_assigned_to__isnull=True,
            verification_started_at__isnull=True,
            cart_released_at__isnull=True,
            completed_at__isnull=True,
            canceled_at__isnull=True,
            picked_qty=0,
        )
        .order_by("created_at", "id")
    )


def _pick_batch_is_untouched(batch: FbsPickBatch) -> bool:
    return not (
        batch.status != FbsPickBatch.STATUS_QUEUED
        or batch.assigned_to_id is not None
        or batch.workstation_id is not None
        or batch.cart_id is not None
        or batch.started_at is not None
        or batch.claimed_at is not None
        or batch.picking_completed_at is not None
        or batch.verification_assigned_to_id is not None
        or batch.verification_started_at is not None
        or batch.cart_released_at is not None
        or batch.completed_at is not None
        or batch.canceled_at is not None
        or batch.picked_qty
    )


def _reusable_queued_batches(
    *, agency_id: int, profile_id: int
) -> list[FbsPickBatch]:
    candidates = list(
        _unstarted_pick_batches().select_for_update().filter(agency_id=agency_id)
    )
    reusable = []
    for batch in candidates:
        profile_ids = set(
            batch.tasks.values_list("order__profile_id", flat=True)
        )
        if profile_ids == {profile_id}:
            reusable.append(batch)
    return reusable


def _reusable_batch_location_counts(
    tasks: list[FbsPickTask],
) -> Counter[tuple[object, ...]]:
    task_ids = [task.id for task in tasks]
    if not task_ids:
        return Counter()
    allocations = (
        FbsOrderStockAllocation.objects.select_related(
            "balance__box__pallet__cell__location",
            "balance__box__source_container__current_location",
        )
        .filter(
            pick_task_id__in=task_ids,
            status__in=ACTIVE_ALLOCATION_STATUSES,
        )
        .order_by("id")
    )
    return _allocation_location_unit_counts(allocations)


def _fill_reusable_pick_batches(
    *,
    profile_orders: list[FbsOrder],
    reusable_batches: list[FbsPickBatch],
    allocations_by_order: dict[int, list[FbsOrderStockAllocation]],
    max_orders_per_batch: int,
    max_units_per_batch: int | None,
    actor,
) -> tuple[list[FbsOrder], list[FbsPickBatch]]:
    """Fill untouched waves oldest-first and use cell overlap only as a tie-breaker."""
    pending = sorted(profile_orders, key=pick_order_priority_key)
    quantities_by_order = {
        order.id: sum(
            int(allocation.qty_reserved or 0)
            for allocation in allocations_by_order[order.id]
        )
        for order in pending
    }
    locations_by_order = {
        order.id: _allocation_location_unit_counts(
            allocations_by_order.get(order.id, ())
        )
        for order in pending
    }
    states = []
    for batch in reusable_batches:
        active_tasks = _locked_unstarted_batch_tasks(batch, strict=False)
        if active_tasks is None:
            continue
        compatibility_keys = _pick_batch_handover_compatibility_keys(batch)
        if len(compatibility_keys) != 1:
            continue
        order_slots = max_orders_per_batch - len(active_tasks)
        unit_slots = (
            None
            if max_units_per_batch is None
            else max_units_per_batch
            - sum(int(task.planned_qty or 0) for task in active_tasks)
        )
        if order_slots <= 0 or (unit_slots is not None and unit_slots <= 0):
            continue
        states.append(
            {
                "batch": batch,
                "order_slots": order_slots,
                "unit_slots": unit_slots,
                "location_counts": _reusable_batch_location_counts(active_tasks),
                "orders": [],
                "compatibility_key": next(iter(compatibility_keys)),
            }
        )

    while pending and states:
        best_choice = None
        for order_index, order in enumerate(pending):
            order_qty = quantities_by_order[order.id]
            if max_units_per_batch is not None and order_qty > max_units_per_batch:
                continue
            for state_index, state in enumerate(states):
                if (
                    _pick_order_handover_compatibility_key(order)
                    != state["compatibility_key"]
                ):
                    continue
                if state["order_slots"] <= 0:
                    continue
                if state["unit_slots"] is not None and order_qty > state["unit_slots"]:
                    continue
                priority = (
                    *_location_grouping_priority(
                        order,
                        locations_by_order=locations_by_order,
                        current_location_counts=state["location_counts"],
                    ),
                    state_index,
                    order_index,
                )
                if best_choice is None or priority < best_choice[0]:
                    best_choice = (priority, order, state)
        if best_choice is None:
            break
        _priority, order, state = best_choice
        pending.remove(order)
        state["orders"].append(order)
        state["order_slots"] -= 1
        if state["unit_slots"] is not None:
            state["unit_slots"] -= quantities_by_order[order.id]
        state["location_counts"].update(locations_by_order[order.id])

    changed_batches = []
    for state in states:
        if not state["orders"]:
            continue
        changed_batches.append(
            _attach_orders_to_pick_batch(
                batch=state["batch"],
                orders=state["orders"],
                allocations_by_order=allocations_by_order,
                actor=actor,
            )
        )
    return pending, changed_batches


def pick_wave_destination_choices(
    *, order_ids: list[int], max_orders_per_batch: int = 100,
    max_units_per_batch: int = 100,
) -> tuple[dict, ...]:
    """Read-only preview; the selected destination is checked again under locks."""
    selected_ids = tuple(dict.fromkeys(order_ids))
    orders = list(
        FbsOrder.objects.filter(pk__in=selected_ids).select_related(
            "profile__wave_policy"
        )
    )
    if not orders or len(orders) != len(selected_ids):
        return ()
    profile_ids = {order.profile_id for order in orders}
    if len(profile_ids) != 1:
        return ()
    effective_limits = enforce_profile_wave_limits(
        orders[0].profile,
        requested_max_orders=max_orders_per_batch,
        requested_max_units=max_units_per_batch,
    )
    max_orders_per_batch = effective_limits.max_orders
    max_units_per_batch = effective_limits.max_units
    units = FbsOrderItem.objects.filter(order_id__in=selected_ids).aggregate(
        total=Sum("quantity")
    )["total"] or 0
    if units <= 0:
        return ()
    choices = []
    candidates = _unstarted_pick_batches().filter(
        agency_id=orders[0].profile.agency_id,
    ).prefetch_related("tasks__order")
    for batch in candidates:
        tasks = list(batch.tasks.all())
        if {task.order.profile_id for task in tasks} != profile_ids:
            continue
        if any(task.status not in {FbsPickTask.STATUS_QUEUED, FbsPickTask.STATUS_CANCELED}
               for task in tasks):
            continue
        active = [task for task in tasks if task.status == FbsPickTask.STATUS_QUEUED]
        existing_units = sum(int(task.planned_qty or 0) for task in active)
        free_orders = max_orders_per_batch - len(active)
        free_units = (
            None
            if max_units_per_batch is None
            else max_units_per_batch - existing_units
        )
        if len(orders) > free_orders or (
            free_units is not None and units > free_units
        ):
            continue
        choices.append({
            "id": batch.id, "created_at": batch.created_at,
            "orders": len(active), "units": existing_units,
            "free_orders": free_orders, "free_units": free_units,
        })
    return tuple(choices)


def _append_to_selected_pick_batch(
    *, target_batch_id: int, orders_by_profile, allocations_by_order,
    max_orders_per_batch: int, max_units_per_batch: int | None, actor,
) -> FbsPickBatch:
    if len(orders_by_profile) != 1:
        raise FbsPickingError("Для добавления в волну выберите заказы одного кабинета клиента.")
    profile_id, orders = next(iter(orders_by_profile.items()))
    if not orders:
        raise FbsPickingError("Нет доступных заказов для добавления в волну.")
    batch = _unstarted_pick_batches().select_for_update().filter(
        pk=target_batch_id, agency_id=orders[0].profile.agency_id,
    ).first()
    if batch is None:
        raise FbsPickingError("Выбранная волна недоступна или уже взята в работу. Выберите другую волну.")
    tasks = _locked_unstarted_batch_tasks(batch, strict=True) or []
    if set(batch.tasks.values_list("order__profile_id", flat=True)) != {profile_id}:
        raise FbsPickingError("Выбранная волна относится к другому кабинету клиента.")
    units = sum(int(task.planned_qty or 0) for task in tasks)
    added_units = sum(int(allocation.qty_reserved or 0)
                      for order in orders for allocation in allocations_by_order[order.id])
    if len(tasks) + len(orders) > max_orders_per_batch or (
        max_units_per_batch is not None and units + added_units > max_units_per_batch
    ):
        raise FbsPickingError(
            "В выбранной волне недостаточно места для всех заказов. "
            "Создайте новую волну или уменьшите выбор. Заказы не добавлены."
        )
    return _attach_orders_to_pick_batch(
        batch=batch, orders=orders, allocations_by_order=allocations_by_order, actor=actor,
    )


def _resort_queued_batch_tasks(batch: FbsPickBatch) -> None:
    tasks = list(
        FbsPickTask.objects.select_for_update()
        .select_related("order")
        .filter(batch=batch, status=FbsPickTask.STATUS_QUEUED)
        .order_by("sort_order", "id")
    )
    if not tasks:
        return
    allocations_by_task: dict[int, list[FbsOrderStockAllocation]] = defaultdict(list)
    for allocation in (
        FbsOrderStockAllocation.objects.select_related(
            "balance__box__pallet__cell__location",
            "balance__box__source_container__current_location",
        )
        .filter(pick_task_id__in=[task.id for task in tasks])
        .order_by("id")
    ):
        allocations_by_task[allocation.pick_task_id].append(allocation)

    missing_route_key = (
        1,
        PICK_ROUTE_MISSING_COORDINATE,
        PICK_ROUTE_MISSING_COORDINATE,
        PICK_ROUTE_MISSING_COORDINATE,
        PICK_ROUTE_MISSING_COORDINATE,
        "",
    )
    tasks.sort(
        key=lambda task: (
            *pick_order_priority_key(task.order),
            min(
                (
                    _physical_location_sort_key(allocation)
                    for allocation in allocations_by_task.get(task.id, ())
                ),
                default=missing_route_key,
            ),
            task.order_id,
            task.id,
        )
    )
    changed = []
    for sort_order, task in enumerate(tasks, start=1):
        if task.sort_order != sort_order:
            task.sort_order = sort_order
            changed.append(task)
    if changed:
        FbsPickTask.objects.bulk_update(changed, ["sort_order"])


@transaction.atomic
def resort_queued_pick_batches_for_balances(balance_ids) -> tuple[int, ...]:
    """Refresh untouched wave routes after a reserved box changes address."""

    normalized_balance_ids = sorted({int(value) for value in balance_ids if value})
    if not normalized_balance_ids:
        return ()
    batch_ids = sorted(
        {
            int(value)
            for value in FbsOrderStockAllocation.objects.filter(
                balance_id__in=normalized_balance_ids,
                status=FbsOrderStockAllocation.STATUS_RESERVED,
                pick_task__status=FbsPickTask.STATUS_QUEUED,
                pick_task__batch__status=FbsPickBatch.STATUS_QUEUED,
            ).values_list("pick_task__batch_id", flat=True)
            if value
        }
    )
    rerouted: list[int] = []
    for batch in (
        FbsPickBatch.objects.select_for_update()
        .filter(id__in=batch_ids)
        .order_by("id")
    ):
        if not _pick_batch_is_untouched(batch):
            continue
        _resort_queued_batch_tasks(batch)
        rerouted.append(int(batch.id))
    return tuple(rerouted)


def _attach_orders_to_pick_batch(
    *,
    batch: FbsPickBatch,
    orders: list[FbsOrder],
    allocations_by_order: dict[int, list[FbsOrderStockAllocation]],
    actor,
) -> FbsPickBatch:
    existing_tasks = _locked_unstarted_batch_tasks(batch, strict=True) or []
    profile_ids = set(
        batch.tasks.values_list("order__profile_id", flat=True)
    ) | {order.profile_id for order in orders}
    if len(profile_ids) != 1:
        raise FbsPickingError(
            "В одну волну нельзя добавлять заказы разных кабинетов клиента."
        )
    _assert_pick_orders_handover_compatible(
        existing_tasks=existing_tasks,
        orders=orders,
    )
    tasks = [
        FbsPickTask(
            batch=batch,
            order=order,
            sort_order=len(existing_tasks) + sort_order,
            planned_qty=sum(
                int(allocation.qty_reserved or 0)
                for allocation in allocations_by_order[order.id]
            ),
        )
        for sort_order, order in enumerate(orders, start=1)
    ]
    FbsPickTask.objects.bulk_create(tasks)
    task_by_order_id = {task.order_id: task for task in tasks}
    batch_allocations = []
    now = timezone.now()
    for order in orders:
        task = task_by_order_id[order.id]
        for allocation in allocations_by_order[order.id]:
            allocation.pick_task = task
            batch_allocations.append(allocation)
        order.internal_status = FbsOrder.STATUS_QUEUED_FOR_PICK
        order.updated_at = now
    previous_statuses = {order.pk: FbsOrder.STATUS_RESERVED for order in orders}
    FbsOrderStockAllocation.objects.bulk_update(batch_allocations, ["pick_task"])
    FbsOrder.objects.bulk_update(orders, ["internal_status", "updated_at"])
    batch.planned_qty = sum(
        int(task.planned_qty or 0) for task in (*existing_tasks, *tasks)
    )
    batch.save(update_fields=["planned_qty", "updated_at"])
    _resort_queued_batch_tasks(batch)
    log_order_bulk_transition(
        orders,
        previous_internal_status=previous_statuses,
        internal_status=FbsOrder.STATUS_QUEUED_FOR_PICK,
        user=actor,
        source="prepare_pick_queue",
    )
    return batch


@transaction.atomic
def create_pick_batches(
    *,
    agency: Agency | None = None,
    order_ids: list[int] | tuple[int, ...] | None = None,
    max_orders_per_batch: int = 50,
    max_units_per_batch: int | None = 100,
    created_by=None,
    allow_confirmed_invalid_kiz_rewave: bool = False,
    reuse_queued_batches: bool = False,
    max_new_batches: int | None = None,
    target_batch_id: int | None = None,
) -> tuple[FbsPickBatch, ...]:
    _require_picking_writes()
    if target_batch_id is not None and (reuse_queued_batches or order_ids is None):
        raise FbsPickingError("Для выбранной волны нужен явный список заказов без автоматического распределения.")
    if not MIN_WAVE_SIZE <= int(max_orders_per_batch or 0) <= MAX_WAVE_SIZE:
        raise FbsPickingError("Размер волны должен быть от 1 до 150 заказов.")
    if max_units_per_batch is not None and not (
        MIN_WAVE_SIZE <= int(max_units_per_batch or 0) <= MAX_WAVE_SIZE
    ):
        raise FbsPickingError(
            "Лимит единиц в волне должен быть от 1 до 150 или без ограничения."
        )
    if max_new_batches is not None and max_new_batches < 0:
        raise FbsPickingError("Количество новых волн не может быть отрицательным.")
    orders_queryset = (
        FbsOrder.objects.select_for_update(of=("self",))
        .select_related("profile__agency", "profile__wave_policy")
        .prefetch_related("items")
        .filter(internal_status=FbsOrder.STATUS_RESERVED)
        .annotate(
            _has_active_pick_task=Exists(
                FbsPickTask.objects.filter(
                    order_id=OuterRef("pk"),
                    status__in=ACTIVE_TASK_STATUSES,
                )
            )
        )
        .filter(_has_active_pick_task=False)
    )
    orders_queryset = order_by_pick_priority(orders_queryset)
    if order_ids is not None:
        selected_order_ids = tuple(dict.fromkeys(int(order_id) for order_id in order_ids))
        if not selected_order_ids:
            return ()
        orders_queryset = orders_queryset.filter(pk__in=selected_order_ids)
    orders = list(orders_queryset)
    if agency is not None:
        orders = [order for order in orders if order.profile.agency_id == agency.id]
    blocked_orders = []
    for order in orders:
        queue_error = _marketplace_queue_error(
            order,
            allow_confirmed_invalid_kiz_rewave=allow_confirmed_invalid_kiz_rewave,
        )
        if queue_error:
            blocked_orders.append((order, queue_error))
    if blocked_orders and order_ids is not None:
        raise FbsPickingError(blocked_orders[0][1])
    blocked_order_ids = {order.id for order, _ in blocked_orders}
    orders = [order for order in orders if order.id not in blocked_order_ids]
    order_ids = [order.id for order in orders]
    allocations = list(
        FbsOrderStockAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "order_item",
            "balance__box__pallet__cell__location",
        )
        # source_container is nullable, so load its physical location outside
        # the row-locking query instead of adding a LEFT JOIN to it.
        .prefetch_related("balance__box__source_container__current_location")
        .filter(
            order_item__order_id__in=order_ids,
            status=FbsOrderStockAllocation.STATUS_RESERVED,
            pick_task__isnull=True,
        )
        .order_by("order_item__order_id", "id")
    )
    allocations_by_order: dict[int, list[FbsOrderStockAllocation]] = defaultdict(list)
    for allocation in allocations:
        allocations_by_order[allocation.order_item.order_id].append(allocation)

    orders_by_profile: dict[int, list[FbsOrder]] = defaultdict(list)
    for order in orders:
        order_allocations = allocations_by_order.get(order.id, ())
        if order_allocations:
            orders_by_profile[order.profile_id].append(order)

    actor = _authenticated_user(created_by)
    if target_batch_id is not None:
        if not orders or sum(len(group) for group in orders_by_profile.values()) != len(orders):
            raise FbsPickingError("Нет полного резерва для выбранных заказов. Обновите очередь.")
        profile = orders[0].profile
        effective_limits = enforce_profile_wave_limits(
            profile,
            requested_max_orders=max_orders_per_batch,
            requested_max_units=max_units_per_batch,
        )
        return (_append_to_selected_pick_batch(
            target_batch_id=target_batch_id, orders_by_profile=orders_by_profile,
            allocations_by_order=allocations_by_order,
            max_orders_per_batch=effective_limits.max_orders,
            max_units_per_batch=effective_limits.max_units, actor=actor,
        ),)
    batches = []
    remaining_orders_by_profile: dict[int, list[FbsOrder]] = {}
    for profile_id, profile_orders in orders_by_profile.items():
        profile = profile_orders[0].profile
        agency_id = profile.agency_id
        effective_limits = enforce_profile_wave_limits(
            profile,
            requested_max_orders=max_orders_per_batch,
            requested_max_units=max_units_per_batch,
        )
        pending = list(profile_orders)
        if reuse_queued_batches:
            pending, reused_batches = _fill_reusable_pick_batches(
                profile_orders=pending,
                reusable_batches=_reusable_queued_batches(
                    agency_id=agency_id,
                    profile_id=profile_id,
                ),
                allocations_by_order=allocations_by_order,
                max_orders_per_batch=effective_limits.max_orders,
                max_units_per_batch=effective_limits.max_units,
                actor=actor,
            )
            batches.extend(reused_batches)
        remaining_orders_by_profile[profile_id] = pending

    ordered_chunks = _ordered_pick_batch_chunks(
        orders_by_profile=remaining_orders_by_profile,
        allocations_by_order=allocations_by_order,
        max_orders_per_batch=max_orders_per_batch,
        max_units_per_batch=max_units_per_batch,
    )
    if max_new_batches is not None:
        ordered_chunks = ordered_chunks[:max_new_batches]
    for agency_id, _profile_id, chunk in ordered_chunks:
        batch = FbsPickBatch.objects.create(
            agency_id=agency_id,
            planned_qty=0,
            created_by=actor,
        )
        batches.append(
            _attach_orders_to_pick_batch(
                batch=batch,
                orders=chunk,
                allocations_by_order=allocations_by_order,
                actor=actor,
            )
        )
    return tuple(batches)


@transaction.atomic
def merge_queued_pick_batches(
    *,
    primary_batch_id: int,
    merged_batch_ids: list[int] | tuple[int, ...],
    max_orders_per_batch: int = 100,
    max_units_per_batch: int | None = 100,
) -> FbsPickBatch:
    """Merge untouched queued waves of one client into the oldest target wave."""
    _require_picking_writes()
    if not MIN_WAVE_SIZE <= int(max_orders_per_batch or 0) <= MAX_WAVE_SIZE:
        raise FbsPickingError("Размер волны должен быть от 1 до 150 заказов.")
    if max_units_per_batch is not None and not (
        MIN_WAVE_SIZE <= int(max_units_per_batch or 0) <= MAX_WAVE_SIZE
    ):
        raise FbsPickingError(
            "Лимит единиц в волне должен быть от 1 до 150 или без ограничения."
        )
    primary_id = int(primary_batch_id)
    merged_ids = tuple(dict.fromkeys(int(batch_id) for batch_id in merged_batch_ids))
    if primary_id in merged_ids:
        raise FbsPickingError("Основная волна не должна повторяться в списке объединения.")
    batch_ids = (primary_id, *merged_ids)
    batches = list(
        FbsPickBatch.objects.select_for_update()
        .filter(pk__in=batch_ids)
        .order_by("created_at", "id")
    )
    if len(batches) != len(batch_ids):
        raise FbsPickingError("Одна из волн для объединения не найдена.")
    batch_by_id = {batch.id: batch for batch in batches}
    primary = batch_by_id[primary_id]
    if any(batch.agency_id != primary.agency_id for batch in batches):
        raise FbsPickingError("Объединять можно только волны одного клиента.")
    for batch in batches:
        if (
            batch.status != FbsPickBatch.STATUS_QUEUED
            or batch.assigned_to_id is not None
            or batch.workstation_id is not None
            or batch.cart_id is not None
            or batch.started_at is not None
            or batch.claimed_at is not None
            or batch.picking_completed_at is not None
            or batch.verification_assigned_to_id is not None
            or batch.verification_started_at is not None
            or batch.cart_released_at is not None
            or batch.completed_at is not None
            or batch.canceled_at is not None
            or batch.picked_qty
        ):
            raise FbsPickingError(
                f"Волна #{batch.id} уже взята в работу и не может быть объединена."
            )
    tasks_by_batch = {
        batch.id: (_locked_unstarted_batch_tasks(batch, strict=True) or [])
        for batch in batches
    }
    combined_tasks = [task for batch in batches for task in tasks_by_batch[batch.id]]
    profile_ids = set(
        FbsPickTask.objects.filter(batch_id__in=batch_ids).values_list(
            "order__profile_id", flat=True
        )
    )
    if len(profile_ids) != 1:
        raise FbsPickingError(
            "Объединять можно только волны одного кабинета клиента."
        )
    _assert_pick_orders_handover_compatible(
        existing_tasks=combined_tasks,
        orders=[],
    )
    profile = FbsIntegrationProfile.objects.select_related("wave_policy").get(
        pk=next(iter(profile_ids))
    )
    effective_limits = enforce_profile_wave_limits(
        profile,
        requested_max_orders=max_orders_per_batch,
        requested_max_units=max_units_per_batch,
    )
    max_orders_per_batch = effective_limits.max_orders
    max_units_per_batch = effective_limits.max_units
    if len(combined_tasks) > max_orders_per_batch:
        raise FbsPickingError(
            f"После объединения будет больше {max_orders_per_batch} заказов."
        )
    combined_qty = sum(int(task.planned_qty or 0) for task in combined_tasks)
    if (
        max_units_per_batch is not None
        and combined_qty > max_units_per_batch
    ):
        raise FbsPickingError(
            f"После объединения будет больше "
            f"{max_units_per_batch} единиц товара."
        )

    moved_tasks = [
        task
        for batch_id in merged_ids
        for task in tasks_by_batch[batch_id]
    ]
    for task in moved_tasks:
        task.batch = primary
    if moved_tasks:
        FbsPickTask.objects.bulk_update(moved_tasks, ["batch"])
    now = timezone.now()
    for batch_id in merged_ids:
        batch = batch_by_id[batch_id]
        batch.status = FbsPickBatch.STATUS_CANCELED
        batch.planned_qty = 0
        batch.canceled_at = now
        batch.save(
            update_fields=["status", "planned_qty", "canceled_at", "updated_at"]
        )
    primary.planned_qty = combined_qty
    primary.save(update_fields=["planned_qty", "updated_at"])
    _resort_queued_batch_tasks(primary)
    return primary


@transaction.atomic
def regroup_queued_pick_batches(
    *,
    profile_id: int,
    max_orders_per_batch: int,
    max_units_per_batch: int | None,
) -> FbsQueuedPickRegroupResult:
    """Repack untouched queued waves without recreating their batch numbers."""
    _require_picking_writes()
    profile = FbsIntegrationProfile.objects.select_related(
        "agency", "wave_policy"
    ).get(pk=profile_id)
    effective_limits = enforce_profile_wave_limits(
        profile,
        requested_max_orders=max_orders_per_batch,
        requested_max_units=max_units_per_batch,
    )
    batch_ids = FbsPickTask.objects.filter(
        batch__status=FbsPickBatch.STATUS_QUEUED,
        order__profile_id=profile.id,
    ).values("batch_id")
    batches = list(
        FbsPickBatch.objects.select_for_update()
        .filter(pk__in=batch_ids)
        .order_by("created_at", "id")
    )
    eligible: list[tuple[FbsPickBatch, list[FbsPickTask]]] = []
    skipped = 0
    for batch in batches:
        if batch.agency_id != profile.agency_id or not _pick_batch_is_untouched(batch):
            skipped += 1
            continue
        tasks = _locked_unstarted_batch_tasks(batch, strict=False)
        if tasks is None:
            skipped += 1
            continue
        active_profile_ids = {task.order.profile_id for task in tasks}
        if active_profile_ids and active_profile_ids != {profile.id}:
            skipped += 1
            continue
        eligible.append((batch, tasks))

    all_tasks = [task for _batch, tasks in eligible for task in tasks]
    if len(eligible) < 2 or not all_tasks:
        return FbsQueuedPickRegroupResult(
            batch_count=len(eligible),
            moved_task_count=0,
            emptied_batch_count=0,
            skipped_batch_count=skipped,
        )

    orders = {
        order.id: order
        for order in FbsOrder.objects.select_related(
            "profile__agency", "profile__wave_policy"
        ).prefetch_related("items").filter(pk__in=[task.order_id for task in all_tasks])
    }
    allocations = list(
        FbsOrderStockAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "order_item",
            "balance__box__pallet__cell__location",
        )
        .prefetch_related("balance__box__source_container__current_location")
        .filter(
            pick_task_id__in=[task.id for task in all_tasks],
            status__in=ACTIVE_ALLOCATION_STATUSES,
        )
        .order_by("order_item__order_id", "id")
    )
    allocations_by_order: dict[int, list[FbsOrderStockAllocation]] = defaultdict(list)
    for allocation in allocations:
        allocations_by_order[allocation.order_item.order_id].append(allocation)
    if any(
        task.order_id not in orders
        or not allocations_by_order.get(task.order_id)
        or sum(
            int(allocation.qty_reserved or 0)
            for allocation in allocations_by_order[task.order_id]
        )
        != int(task.planned_qty or 0)
        for task in all_tasks
    ):
        raise FbsPickingError(
            "Queued-волны содержат задание без полного активного резерва."
        )

    ordered_chunks = _ordered_pick_batch_chunks(
        orders_by_profile={
            profile.id: [orders[task.order_id] for task in all_tasks]
        },
        allocations_by_order=allocations_by_order,
        max_orders_per_batch=effective_limits.max_orders,
        max_units_per_batch=effective_limits.max_units,
    )
    chunks = [chunk for _agency_id, _profile_id, chunk in ordered_chunks]
    if len(chunks) > len(eligible):
        raise FbsPickingError(
            "Для перекомпоновки недостаточно существующих queued-волн."
        )

    target_batches = [batch for batch, _tasks in eligible[: len(chunks)]]
    target_by_order_id = {
        order.id: batch
        for batch, chunk in zip(target_batches, chunks)
        for order in chunk
    }
    moved_tasks = []
    for task in all_tasks:
        target = target_by_order_id[task.order_id]
        if task.batch_id != target.id:
            task.batch = target
            moved_tasks.append(task)
    if moved_tasks:
        FbsPickTask.objects.bulk_update(moved_tasks, ["batch"])

    for batch, chunk in zip(target_batches, chunks):
        batch.planned_qty = sum(
            int(task.planned_qty or 0)
            for task in all_tasks
            if target_by_order_id[task.order_id].id == batch.id
        )
        batch.save(update_fields=["planned_qty", "updated_at"])
        _resort_queued_batch_tasks(batch)

    now = timezone.now()
    emptied_batches = [batch for batch, _tasks in eligible[len(chunks) :]]
    for batch in emptied_batches:
        batch.status = FbsPickBatch.STATUS_CANCELED
        batch.planned_qty = 0
        batch.canceled_at = now
        batch.save(
            update_fields=["status", "planned_qty", "canceled_at", "updated_at"]
        )
    return FbsQueuedPickRegroupResult(
        batch_count=len(eligible),
        moved_task_count=len(moved_tasks),
        emptied_batch_count=len(emptied_batches),
        skipped_batch_count=skipped,
    )


@transaction.atomic
def _reserve_and_create_pick_queue(
    *,
    queueable_order_ids: list[int],
    current_orders: dict[int, FbsOrder],
    stock_shortage_policy: str,
    max_orders_per_batch: int,
    max_units_per_batch: int | None,
    create_batches: bool,
    reuse_queued_batches: bool = False,
    max_new_batches: int | None = None,
    target_batch_id: int | None = None,
    performed_by=None,
) -> tuple[int, int, int, tuple[FbsPickBatch, ...]]:
    reserved_orders = 0
    awaiting_stock_orders = 0
    validation_failed_orders = 0
    batch_order_ids = []
    stock_shortages = []

    for order_id in queueable_order_ids:
        if stock_shortage_policy == STOCK_SHORTAGE_POLICY_SKIP:
            with transaction.atomic():
                result = reserve_order_stock(
                    order_id=order_id,
                    reserved_by=performed_by,
                )
                if result.status == FbsOrder.STATUS_AWAITING_STOCK:
                    transaction.set_rollback(True)
        else:
            result = reserve_order_stock(
                order_id=order_id,
                reserved_by=performed_by,
            )

        if result.reserved:
            reserved_orders += 1
            batch_order_ids.append(order_id)
        elif result.status == FbsOrder.STATUS_AWAITING_STOCK:
            awaiting_stock_orders += 1
            order = current_orders[order_id]
            stock_shortages.append(
                FbsQueueStockShortage(
                    order_id=order.id,
                    external_order_id=order.external_order_id,
                    missing_qty=max(int(result.missing_qty or 0), 1),
                    reasons=result.shortages,
                )
            )
        elif result.status == FbsOrder.STATUS_VALIDATION_FAILED:
            validation_failed_orders += 1

    if (
        stock_shortages
        and stock_shortage_policy == STOCK_SHORTAGE_POLICY_REQUIRE_CONFIRMATION
    ):
        raise FbsQueueStockConfirmationRequired(tuple(stock_shortages))

    create_options = {}
    if reuse_queued_batches:
        create_options["reuse_queued_batches"] = True
    if max_new_batches is not None:
        create_options["max_new_batches"] = max_new_batches
    if target_batch_id is not None:
        create_options["target_batch_id"] = target_batch_id
    batches = (
        create_pick_batches(
            order_ids=batch_order_ids,
            max_orders_per_batch=max_orders_per_batch,
            max_units_per_batch=max_units_per_batch,
            created_by=performed_by,
            **create_options,
        )
        if create_batches
        else ()
    )
    return (
        reserved_orders,
        awaiting_stock_orders,
        validation_failed_orders,
        batches,
    )


def prepare_pick_queue(
    *,
    limit: int = 200,
    order_ids: list[int] | tuple[int, ...] | None = None,
    single_agency_only: bool = False,
    max_orders_per_batch: int = 50,
    max_units_per_batch: int | None = 100,
    performed_by=None,
    marketplace_status_transport=None,
    stock_shortage_policy: str = STOCK_SHORTAGE_POLICY_MARK,
    create_batches: bool = True,
    reuse_queued_batches: bool = True,
    target_batch_id: int | None = None,
) -> FbsQueuePreparationResult:
    _require_picking_writes()
    if target_batch_id is not None and (not create_batches or reuse_queued_batches):
        raise FbsPickingError("Выбранную волну нельзя совмещать с автоматическим распределением.")
    if limit <= 0:
        raise FbsPickingError("Лимит заказов должен быть больше нуля.")
    if stock_shortage_policy not in STOCK_SHORTAGE_POLICIES:
        raise FbsPickingError("Неизвестный режим обработки отсутствующего товара.")
    selected_order_ids = None
    if order_ids is not None:
        selected_order_ids = tuple(dict.fromkeys(int(order_id) for order_id in order_ids))
        if len(selected_order_ids) > limit:
            raise FbsPickingError(
                f"Выбрано больше {limit} заказов. Уменьшите выбор или увеличьте лимит очереди."
            )
        selected_orders = FbsOrder.objects.filter(pk__in=selected_order_ids)
        if selected_orders.count() != len(selected_order_ids):
            raise FbsPickingError("Один из выбранных заказов не найден.")
        if single_agency_only:
            selected_agency_ids = list(
                selected_orders.order_by()
                .values_list("profile__agency_id", flat=True)
                .distinct()[:2]
            )
            if len(selected_agency_ids) > 1:
                raise FbsPickingError(
                    "В одну подачу разрешены заказы только одного клиента."
                )
        candidate_orders = selected_orders
    else:
        candidate_orders = FbsOrder.objects.filter(
            internal_status__in=RESERVABLE_ORDER_STATUSES,
            profile__is_active=True,
        )
    candidate_order_ids = list(
        order_by_pick_priority(candidate_orders).values_list("id", flat=True)[:limit]
    )
    if feature_enabled("status_pull"):
        try:
            from .sync import refresh_wb_order_statuses_for_wave

            refreshed_wb_order_ids = set(
                refresh_wb_order_statuses_for_wave(
                    order_ids=candidate_order_ids,
                    transport=marketplace_status_transport,
                )
            )
        except FbsError as exc:
            raise FbsPickingError(
                "Не удалось проверить актуальные статусы WB. Волна не создана, "
                f"резервы не изменены. Причина: {exc}"
            ) from exc
    else:
        refreshed_wb_order_ids = {
            order.id
            for order in FbsOrder.objects.select_related("profile").filter(
                pk__in=candidate_order_ids,
                profile__marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            )
        }

    current_orders = {
        order.id: order
        for order in FbsOrder.objects.select_related("profile").filter(
            pk__in=candidate_order_ids
        )
    }
    rejected_orders = []
    queueable_order_ids = []
    allowed_internal_statuses = {
        *RESERVABLE_ORDER_STATUSES,
        FbsOrder.STATUS_RESERVED,
    }
    for order_id in candidate_order_ids:
        order = current_orders[order_id]
        if (
            order.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
            and order.id not in refreshed_wb_order_ids
        ):
            rejected_orders.append(
                _queue_rejection(
                    order,
                    f"Заказ WB {order.external_order_id} не включен в волну: "
                    "WB не вернул актуальный supplierStatus/wbStatus.",
                )
            )
            continue
        queue_error = _marketplace_queue_error(order)
        if queue_error:
            rejected_orders.append(_queue_rejection(order, queue_error))
            continue
        if order.internal_status not in allowed_internal_statuses:
            rejected_orders.append(
                _queue_rejection(
                    order,
                    f"Заказ {order.external_order_id} не включен в волну: "
                    f"внутренний статус «{order.get_internal_status_display()}».",
                )
            )
            continue
        queueable_order_ids.append(order.id)

    for attempt in range(PICK_DEADLOCK_RETRY_ATTEMPTS):
        try:
            (
                reserved_orders,
                awaiting_stock_orders,
                validation_failed_orders,
                batches,
            ) = _reserve_and_create_pick_queue(
                queueable_order_ids=queueable_order_ids,
                current_orders=current_orders,
                stock_shortage_policy=stock_shortage_policy,
                max_orders_per_batch=max_orders_per_batch,
                max_units_per_batch=max_units_per_batch,
                create_batches=create_batches,
                reuse_queued_batches=reuse_queued_batches,
                target_batch_id=target_batch_id,
                performed_by=performed_by,
            )
            tasks_created = (
                FbsPickTask.objects.filter(
                    batch__in=batches,
                    order_id__in=queueable_order_ids,
                    status=FbsPickTask.STATUS_QUEUED,
                ).count()
                if create_batches
                else 0
            )
            break
        except OperationalError as exc:
            if not _is_deadlock_error(exc) or attempt + 1 >= PICK_DEADLOCK_RETRY_ATTEMPTS:
                raise
            time.sleep(PICK_DEADLOCK_RETRY_DELAY_SECONDS * (attempt + 1))
    return FbsQueuePreparationResult(
        inspected_orders=len(candidate_order_ids),
        reserved_orders=reserved_orders,
        awaiting_stock_orders=awaiting_stock_orders,
        validation_failed_orders=validation_failed_orders,
        batches=batches,
        tasks_created=tasks_created,
        rejected_orders=tuple(rejected_orders),
    )


def _resolve_pick_equipment(
    *,
    workstation_scan: str,
    cart_scan: str,
    for_update: bool,
) -> tuple[FbsWorkstation, FbsPickingCart]:
    workstation_scan = str(workstation_scan or "").strip().upper()
    cart_scan = str(cart_scan or "").strip().upper()
    if not workstation_scan or not cart_scan:
        raise FbsPickingError("Отсканируйте QR рабочего места и тары.")
    workstation_queryset = FbsWorkstation.objects
    cart_queryset = FbsPickingCart.objects
    if for_update:
        workstation_queryset = workstation_queryset.select_for_update()
        cart_queryset = cart_queryset.select_for_update()
    workstation = workstation_queryset.filter(
        barcode=workstation_scan,
        is_active=True,
    ).first()
    if workstation is None:
        raise FbsPickingError("Рабочее место FBS не найдено или отключено.")
    if not str(workstation.printer_name or "").strip():
        raise FbsPickingError("Для рабочего места не настроен принтер этикеток.")
    cart = cart_queryset.filter(barcode=cart_scan, is_active=True).first()
    if cart is None:
        raise FbsPickingError("Тара FBS не найдена или отключена.")
    return workstation, cart


def _resolve_pick_cart(*, cart_scan: str, for_update: bool) -> FbsPickingCart:
    cart_scan = str(cart_scan or "").strip().upper()
    if not cart_scan:
        raise FbsPickingError("Отсканируйте QR тары.")
    queryset = FbsPickingCart.objects
    if for_update:
        queryset = queryset.select_for_update()
    cart = queryset.filter(barcode=cart_scan, is_active=True).first()
    if cart is None:
        raise FbsPickingError("Тара FBS не найдена или отключена.")
    return cart


def _resolve_handover_workstation(
    *, workstation_scan: str, for_update: bool
) -> FbsWorkstation:
    workstation_scan = str(workstation_scan or "").strip().upper()
    if not workstation_scan:
        raise FbsPickingError("Отсканируйте QR рабочего места.")
    queryset = FbsWorkstation.objects
    if for_update:
        queryset = queryset.select_for_update()
    workstation = queryset.filter(barcode=workstation_scan, is_active=True).first()
    if workstation is None:
        raise FbsPickingError("Рабочее место FBS не найдено или отключено.")
    # The picker only hands the tote over to an active controller desk. Printer
    # availability is validated by the controller's actual printing operation.
    return workstation


def _lock_pick_actor(actor):
    return get_user_model().objects.select_for_update().get(pk=actor.pk)


@transaction.atomic
def validate_pick_equipment(
    *,
    assigned_to,
    workstation_scan: str,
    cart_scan: str,
) -> tuple[FbsWorkstation, FbsPickingCart]:
    _require_picking_writes()
    actor = _authenticated_user(assigned_to)
    if actor is None:
        raise FbsPickingError("Для привязки ТСД нужен авторизованный сборщик.")
    workstation, cart = _resolve_pick_equipment(
        workstation_scan=workstation_scan,
        cart_scan=cart_scan,
        for_update=True,
    )
    active_cart_batches = FbsPickBatch.objects.select_for_update().filter(
        cart=cart,
        status__in=ACTIVE_BATCH_STATUSES,
        cart_released_at__isnull=True,
    )
    if active_cart_batches.exclude(assigned_to=actor).exists():
        raise FbsPickingError("Тара уже используется в другой незавершённой волне.")
    if active_cart_batches.filter(
        assigned_to=actor,
        picking_completed_at__isnull=False,
    ).exists():
        raise FbsPickingError(
            "Тара уже передана контроллеру. Возьмите другую свободную тару."
        )
    return workstation, cart


def _active_pick_tasks_planned_qty(tasks) -> int:
    return sum(
        int(task.planned_qty or 0)
        for task in tasks
        if task.status != FbsPickTask.STATUS_CANCELED
    )


def _take_over_locked_pick_batch(
    *,
    batch: FbsPickBatch,
    actor,
    cart: FbsPickingCart,
) -> FbsPickBatch:
    """Transfer an unfinished wave to the picker holding its physical tote."""
    if (
        batch.status != FbsPickBatch.STATUS_IN_PROGRESS
        or batch.picking_completed_at is not None
        or batch.cart_released_at is not None
        or batch.cart_id != cart.id
    ):
        raise FbsPickingError("Эту волну уже нельзя передать другому сборщику.")
    if FbsPickBatch.objects.select_for_update().filter(
        assigned_to=actor,
        status__in=ACTIVE_BATCH_STATUSES,
        picking_completed_at__isnull=True,
    ).exclude(pk=batch.pk).exists():
        raise FbsPickingError("Сначала сдайте текущую волну и освободите тару.")

    open_tasks = list(
        FbsPickTask.objects.select_for_update()
        .filter(batch=batch, status__in=ACTIVE_TASK_STATUSES)
        .order_by("id")
    )
    if not open_tasks:
        raise FbsPickingError("В волне не осталось незавершённых заданий для передачи.")

    previous_picker = batch.assigned_to
    previous_name = (
        previous_picker.get_full_name() or previous_picker.username
        if previous_picker is not None
        else "не назначен"
    )
    actor_name = actor.get_full_name() or actor.username
    now = timezone.now()
    batch.assigned_to = actor
    batch.updated_at = now
    batch.save(update_fields=["assigned_to", "updated_at"])
    FbsPickTask.objects.filter(pk__in=[task.pk for task in open_tasks]).update(
        assigned_to=actor,
        updated_at=now,
    )
    record_pick_scan_event(
        batch_id=batch.id,
        stage=FbsPickScanEvent.STAGE_CART,
        result=FbsPickScanEvent.RESULT_SUCCESS,
        scan_value=cart.barcode,
        expected_value=cart.barcode,
        quantity_after=batch.picked_qty,
        message=(
            f"Незавершённая волна передана от сборщика {previous_name} "
            f"сборщику {actor_name}."
        ),
        created_by=actor,
    )
    from .totes import bind_pick_tote_to_picker

    bind_pick_tote_to_picker(batch_id=batch.id, performed_by=actor)
    return batch


def _claim_locked_pick_batch(
    *,
    batch: FbsPickBatch,
    actor,
    cart: FbsPickingCart,
) -> FbsPickBatch:
    if cart.owner_agency_id is not None and cart.owner_agency_id != batch.agency_id:
        raise FbsPickingError(
            "Эта тара принадлежит другому поставщику. "
            "Отсканируйте общескладскую тару или тару клиента этой волны."
        )
    if (
        batch.status in ACTIVE_BATCH_STATUSES
        and batch.picking_completed_at is None
        and batch.assigned_to_id == actor.id
    ):
        if batch.cart_id != cart.id:
            bound_cart = batch.cart
            bound_name = str(bound_cart.name or "").strip() or f"ID {bound_cart.id}"
            bound_barcode = str(bound_cart.barcode or "").strip()
            bound_label = (
                f"{bound_name} (QR: {bound_barcode})"
                if bound_barcode
                else bound_name
            )
            raise FbsPickingError(
                f"Волна #{batch.id} уже привязана к таре {bound_label}. "
                "Отсканируйте именно эту тару."
            )
        return batch
    if (
        batch.status == FbsPickBatch.STATUS_IN_PROGRESS
        and batch.picking_completed_at is None
        and batch.cart_released_at is None
        and batch.cart_id == cart.id
        and batch.assigned_to_id != actor.id
    ):
        return _take_over_locked_pick_batch(
            batch=batch,
            actor=actor,
            cart=cart,
        )
    if batch.status != FbsPickBatch.STATUS_QUEUED or batch.assigned_to_id is not None:
        raise FbsPickingError("Волна уже недоступна.")
    if FbsPickBatch.objects.select_for_update().filter(
        cart=cart,
        status__in=ACTIVE_BATCH_STATUSES,
        cart_released_at__isnull=True,
    ).exists():
        raise FbsPickingError(
            "Тара уже используется в другой незавершённой волне. "
            "Возьмите другую свободную тару."
        )
    if FbsPickBatch.objects.select_for_update().filter(
        assigned_to=actor,
        status__in=ACTIVE_BATCH_STATUSES,
        picking_completed_at__isnull=True,
    ).exists():
        raise FbsPickingError("Сначала сдайте текущую волну и освободите тару.")
    batch_tasks = list(
        FbsPickTask.objects.select_for_update()
        .filter(batch=batch)
        .order_by("sort_order", "id")
    )
    unexpected_tasks = [
        task
        for task in batch_tasks
        if task.status
        not in {FbsPickTask.STATUS_QUEUED, FbsPickTask.STATUS_CANCELED}
    ]
    if unexpected_tasks:
        raise FbsPickingError("Состав волны изменился; обновите очередь.")
    tasks = [
        task for task in batch_tasks if task.status == FbsPickTask.STATUS_QUEUED
    ]
    if not tasks:
        raise FbsPickingError("В волне не осталось заданий для сборки.")
    task_ids = [task.id for task in tasks]
    if active_free_relocation_for_pick_tasks(
        task_ids,
        lock_allocations=True,
    ):
        raise FbsPickingError(
            "Товар этой волны сейчас перемещается. "
            "Дождитесь сканирования нового точного места и повторите запуск."
        )
    active_planned_qty = _active_pick_tasks_planned_qty(tasks)
    now = timezone.now()
    batch.assigned_to = actor
    batch.workstation = None
    batch.cart = cart
    batch.status = FbsPickBatch.STATUS_IN_PROGRESS
    batch.planned_qty = active_planned_qty
    batch.claimed_at = now
    batch.started_at = now
    batch.save(
        update_fields=[
            "assigned_to",
            "workstation",
            "cart",
            "status",
            "planned_qty",
            "claimed_at",
            "started_at",
            "updated_at",
        ]
    )
    record_pick_scan_event(
        batch_id=batch.id,
        stage=FbsPickScanEvent.STAGE_CART,
        result=FbsPickScanEvent.RESULT_SUCCESS,
        scan_value=cart.barcode,
        expected_value=cart.barcode,
        message=cart.name,
        created_by=actor,
    )
    for task in tasks:
        task.assigned_to = actor
        task.status = FbsPickTask.STATUS_IN_PROGRESS
        task.claimed_at = now
        task.updated_at = now
    FbsPickTask.objects.bulk_update(tasks, ["assigned_to", "status", "claimed_at", "updated_at"])
    FbsOrderStockAllocation.objects.filter(
        pick_task_id__in=task_ids,
        status=FbsOrderStockAllocation.STATUS_RESERVED,
    ).update(status=FbsOrderStockAllocation.STATUS_PICKING)
    batch_orders = list(
        FbsOrder.objects.filter(pick_tasks__id__in=task_ids)
        .select_related("profile__agency")
        .distinct()
    )
    previous_statuses = {order.pk: order.internal_status for order in batch_orders}
    FbsOrder.objects.filter(pk__in=previous_statuses).update(
        internal_status=FbsOrder.STATUS_PICKING,
        updated_at=now,
    )
    log_order_bulk_transition(
        batch_orders,
        previous_internal_status=previous_statuses,
        internal_status=FbsOrder.STATUS_PICKING,
        user=actor,
        source="claim_pick_batch",
        occurred_at=now,
    )
    from .totes import bind_pick_tote_to_picker

    bind_pick_tote_to_picker(batch_id=batch.id, performed_by=actor)
    return batch


@transaction.atomic
def claim_pick_batch(
    *,
    batch_id: int,
    assigned_to,
    workstation_scan: str,
    cart_scan: str,
) -> FbsPickBatch:
    _require_picking_writes()
    actor = _authenticated_user(assigned_to)
    if actor is None:
        raise FbsPickingError("Для волны нужен авторизованный сборщик.")
    actor = _lock_pick_actor(actor)
    cart = _resolve_pick_cart(
        cart_scan=cart_scan,
        for_update=True,
    )
    batch = FbsPickBatch.objects.select_for_update(of=("self",)).get(pk=batch_id)
    return _claim_locked_pick_batch(
        batch=batch,
        actor=actor,
        cart=cart,
    )


@transaction.atomic
def claim_next_pick_batch(
    *,
    assigned_to,
    workstation_scan: str,
    cart_scan: str,
) -> FbsPickBatch:
    _require_picking_writes()
    actor = _authenticated_user(assigned_to)
    if actor is None:
        raise FbsPickingError("Для волны нужен авторизованный сборщик.")
    actor = _lock_pick_actor(actor)
    cart = _resolve_pick_cart(
        cart_scan=cart_scan,
        for_update=True,
    )
    active_batch = (
        FbsPickBatch.objects.select_for_update()
        .filter(
            assigned_to=actor,
            status__in=ACTIVE_BATCH_STATUSES,
            picking_completed_at__isnull=True,
        )
        .order_by("claimed_at", "id")
        .first()
    )
    if active_batch is not None:
        return _claim_locked_pick_batch(
            batch=active_batch,
            actor=actor,
            cart=cart,
        )
    cart_batch = (
        FbsPickBatch.objects.select_for_update()
        .filter(
            cart=cart,
            status=FbsPickBatch.STATUS_IN_PROGRESS,
            picking_completed_at__isnull=True,
            cart_released_at__isnull=True,
        )
        .order_by("claimed_at", "id")
        .first()
    )
    if cart_batch is not None:
        return _claim_locked_pick_batch(
            batch=cart_batch,
            actor=actor,
            cart=cart,
        )
    moving_batch_ids: list[int] = []
    while True:
        queue = order_wave_queue_queryset(
            FbsPickBatch.objects.select_for_update(skip_locked=True)
        )
        if cart.owner_agency_id is not None:
            queue = queue.filter(agency_id=cart.owner_agency_id)
        batch = (
            queue
            .exclude(pk__in=moving_batch_ids)
            .first()
        )
        if batch is None:
            if moving_batch_ids:
                raise FbsPickingError(
                    "Товар всех свободных волн сейчас перемещается. "
                    "Дождитесь сканирования нового точного места и повторите запуск."
                )
            raise FbsPickingError("Свободных волн в очереди сейчас нет.")

        task_ids = list(
            batch.tasks.filter(status=FbsPickTask.STATUS_QUEUED)
            .order_by("id")
            .values_list("id", flat=True)
        )
        if active_free_relocation_for_pick_tasks(
            task_ids,
            lock_allocations=True,
        ):
            moving_batch_ids.append(int(batch.id))
            continue
        return _claim_locked_pick_batch(
            batch=batch,
            actor=actor,
            cart=cart,
        )


def claim_pick_task(
    *,
    task_id: int,
    assigned_to,
    workstation_scan: str,
    cart_scan: str,
) -> FbsPickTask:
    task = FbsPickTask.objects.only("id", "batch_id").get(pk=task_id)
    claim_pick_batch(
        batch_id=task.batch_id,
        assigned_to=assigned_to,
        workstation_scan=workstation_scan,
        cart_scan=cart_scan,
    )
    return FbsPickTask.objects.select_related("order", "batch").get(pk=task_id)


def _assert_task_actor(task: FbsPickTask, performed_by) -> None:
    actor = _authenticated_user(performed_by)
    if actor is None or task.assigned_to_id != actor.id:
        raise FbsPickingError("Задание назначено другому сборщику.")
    if task.status != FbsPickTask.STATUS_IN_PROGRESS:
        raise FbsPickingError("Задание не находится в работе.")


@transaction.atomic
def handover_pick_batch_for_verification(
    *,
    batch_id: int,
    workstation_scan: str,
    performed_by,
) -> FbsPickBatch:
    _require_picking_writes()
    actor = _authenticated_user(performed_by)
    batch = (
        # PostgreSQL cannot lock nullable rows added by these outer joins.
        FbsPickBatch.objects.select_for_update(of=("self",))
        .select_related("workstation", "assigned_to")
        .get(pk=batch_id)
    )
    scan = str(workstation_scan or "").strip().upper()
    if actor is None or batch.assigned_to_id != actor.id:
        raise FbsPickingError("Волна назначена другому сборщику.")
    if batch.status != FbsPickBatch.STATUS_VERIFICATION:
        raise FbsPickingError("Сначала завершите отбор всех товаров волны.")
    if batch.tasks.exclude(
        status__in=(FbsPickTask.STATUS_PICKED, FbsPickTask.STATUS_CANCELED)
    ).exists():
        raise FbsPickingError("В волне остались незавершенные заказы.")
    if int(batch.picked_qty or 0) != int(batch.planned_qty or 0):
        raise FbsPickingError("Фактически собрано не все количество волны.")
    if batch.picking_completed_at is not None:
        expected = str(getattr(batch.workstation, "barcode", "") or "").strip().upper()
        if scan == expected:
            from .totes import bind_pick_tote_to_workstation

            bind_pick_tote_to_workstation(batch_id=batch.id, performed_by=actor)
            return batch
        record_pick_scan_event(
            batch_id=batch.id,
            stage=FbsPickScanEvent.STAGE_WAVE_HANDOVER,
            result=FbsPickScanEvent.RESULT_ERROR,
            scan_value=workstation_scan,
            expected_value=expected,
            message="Волна уже передана на другое рабочее место.",
            created_by=actor,
        )
        raise FbsPickingError("Волна уже передана на другое рабочее место.")
    try:
        recommendation = assign_pick_handover_workstation(batch_id=batch.id)
        batch.refresh_from_db(fields=["workstation", "updated_at"])
        if recommendation is not None and scan != recommendation.barcode:
            raise FbsPickingError(
                f"Система назначила рабочее место «{recommendation.name}». "
                "Отвезите тару туда и отсканируйте QR рабочего места."
            )
        workstation = (
            FbsWorkstation.objects.select_for_update().get(pk=batch.workstation_id)
            if batch.workstation_id is not None
            else _resolve_handover_workstation(
                workstation_scan=scan,
                for_update=True,
            )
        )
        _assert_controller_workstation_capacity(
            workstation=workstation,
            batch_id=batch.id,
        )
    except FbsPickingError as exc:
        record_pick_scan_event(
            batch_id=batch.id,
            stage=FbsPickScanEvent.STAGE_WAVE_HANDOVER,
            result=FbsPickScanEvent.RESULT_ERROR,
            scan_value=workstation_scan,
            expected_value=(
                str(getattr(batch.workstation, "barcode", "") or "")
                if batch.workstation_id
                else ""
            ),
            message=str(exc),
            created_by=actor,
        )
        raise
    now = timezone.now()
    batch.workstation = workstation
    batch.picking_completed_at = now
    batch.save(update_fields=["workstation", "picking_completed_at", "updated_at"])
    from .totes import bind_pick_tote_to_workstation

    bind_pick_tote_to_workstation(batch_id=batch.id, performed_by=actor)
    record_pick_scan_event(
        batch_id=batch.id,
        stage=FbsPickScanEvent.STAGE_WAVE_HANDOVER,
        result=FbsPickScanEvent.RESULT_SUCCESS,
        scan_value=scan,
        expected_value=workstation.barcode,
        message=f"Волна передана на рабочее место: {workstation.name}.",
        created_by=actor,
    )
    return batch


@transaction.atomic
def claim_pick_batch_verification(*, batch_id: int, assigned_to) -> FbsPickBatch:
    _require_picking_writes()
    actor = _authenticated_user(assigned_to)
    if actor is None:
        raise FbsPickingError("Для проверки нужен авторизованный оператор.")
    get_user_model().objects.select_for_update().only("pk").get(pk=actor.pk)
    batch = FbsPickBatch.objects.select_for_update().get(pk=batch_id)
    from .pick_restock import assert_no_active_pick_restock

    assert_no_active_pick_restock(batch.id)
    if batch.status != FbsPickBatch.STATUS_VERIFICATION or batch.picking_completed_at is None:
        raise FbsPickingError("Сборщик еще не передал волну на рабочее место.")
    if batch.verification_assigned_to_id == actor.id:
        return batch
    if batch.verification_assigned_to_id is not None:
        raise FbsPickingError("Проверку этой волны уже выполняет другой оператор.")
    active_count = _active_controller_waves(controller_id=actor.id).count()
    if active_count >= CONTROLLER_ACTIVE_WAVE_LIMIT:
        raise FbsPickingError(
            "У контроллера уже открыты три проверки волн. "
            "Завершите одну из них, чтобы принять следующую тару."
        )
    batch.verification_assigned_to = actor
    batch.verification_started_at = timezone.now()
    batch.save(
        update_fields=["verification_assigned_to", "verification_started_at", "updated_at"]
    )
    return batch


@transaction.atomic
def claim_pick_batch_verification_by_cart(
    *,
    batch_id: int,
    assigned_to,
    workstation_id: int,
    cart_scan: str,
) -> FbsPickBatch:
    """Accept a fully picked cart at the controller desk and start verification."""
    _require_picking_writes()
    actor = _authenticated_user(assigned_to)
    if actor is None:
        raise FbsPickingError("Для проверки нужен авторизованный оператор.")

    batch = (
        FbsPickBatch.objects.select_for_update(of=("self",))
        .select_related("workstation", "cart")
        .get(pk=batch_id)
    )
    from .pick_restock import assert_no_active_pick_restock

    assert_no_active_pick_restock(batch.id)
    if batch.status != FbsPickBatch.STATUS_VERIFICATION:
        raise FbsPickingError("Сначала завершите отбор всех товаров волны.")
    if batch.tasks.exclude(
        status__in=(FbsPickTask.STATUS_PICKED, FbsPickTask.STATUS_CANCELED)
    ).exists():
        raise FbsPickingError("В волне остались незавершенные заказы.")
    if int(batch.picked_qty or 0) != int(batch.planned_qty or 0):
        raise FbsPickingError("Фактически собрано не все количество волны.")
    if batch.cart_id is None:
        raise FbsPickingError("У волны не назначена тара.")
    if not batch.cart.is_active:
        raise FbsPickingError("Тара отключена. Обратитесь к начальнику склада.")

    scan = str(cart_scan or "").strip().upper()
    expected_cart = str(batch.cart.barcode or "").strip().upper()
    if not scan or scan != expected_cart:
        raise FbsPickingError("QR тары не относится к этой волне.")

    try:
        workstation = FbsWorkstation.objects.select_for_update().get(
            pk=workstation_id,
            is_active=True,
        )
    except FbsWorkstation.DoesNotExist as exc:
        raise FbsPickingError("Рабочее место не настроено или выключено.") from exc
    if batch.workstation_id not in (None, workstation.id):
        raise FbsPickingError("Тара передана на другое рабочее место.")
    _assert_controller_workstation_capacity(
        workstation=workstation,
        batch_id=batch.id,
    )

    accepted_at_desk = batch.picking_completed_at is None
    if accepted_at_desk:
        batch.workstation = workstation
        batch.picking_completed_at = timezone.now()
        batch.save(
            update_fields=["workstation", "picking_completed_at", "updated_at"]
        )

    claimed = claim_pick_batch_verification(
        batch_id=batch.id,
        assigned_to=actor,
    )
    if accepted_at_desk:
        record_pick_scan_event(
            batch_id=batch.id,
            stage=FbsPickScanEvent.STAGE_WAVE_HANDOVER,
            result=FbsPickScanEvent.RESULT_SUCCESS,
            scan_value=scan,
            expected_value=expected_cart,
            message=(
                "Тара принята контроллером на рабочем месте: "
                f"{workstation.name}."
            ),
            created_by=actor,
        )
    return claimed


def pick_requires_box_scan(box: FbsBox) -> bool:
    """Only a verified per-client rack binding has no physical box to scan."""
    if not box.source_container_id or not box.pallet.is_rack_binding:
        return True
    return not FbsRackCellBinding.objects.filter(
        box_id=box.pk,
        pallet_id=box.pallet_id,
        agency_id=box.agency_id,
        pallet__agency_id=box.agency_id,
        pallet__is_rack_binding=True,
        pallet__cell_id=F("rack_cell__storage_cell_id"),
        box__source_container__agency_id=box.agency_id,
        box__source_container__source_context_type="fbs_rack_cell",
        box__source_container__current_location_id=F("rack_cell__storage_cell__location_id"),
    ).exists()


def _assert_box_scan(allocation: FbsOrderStockAllocation, box_scan: str) -> None:
    if not _normalized(box_scan) and not pick_requires_box_scan(allocation.balance.box):
        return
    if _normalized(allocation.balance.box.box_code) != _normalized(box_scan):
        raise FbsPickingError("Скан короба FBS не совпадает с заданием.")


def _normalized_cell_scan(value: str) -> str:
    normalized = unicodedata.normalize(
        "NFKC",
        normalize_operational_location_scan(value),
    ).strip().upper()
    normalized = normalized.translate(
        str.maketrans(
            {
                "А": "A",
                "В": "B",
                "С": "C",
                "Е": "E",
                "Н": "H",
                "К": "K",
                "М": "M",
                "О": "O",
                "Р": "P",
                "Т": "T",
                "Х": "X",
                "У": "Y",
            }
        )
    )
    normalized = re.sub(r"^FBS\s*[@:/-]?\s*", "", normalized)
    return "-".join(re.findall(r"[A-Z]+|\d+", normalized))


def _cell_scan_candidates(allocation: FbsOrderStockAllocation) -> set[str]:
    box = allocation.balance.box
    cell = box.pallet.cell
    location = fbs_box_physical_location(box)
    raw_candidates = set(fbs_box_physical_location_scan_values(box))
    if getattr(location, "pk", None) == getattr(cell, "location_id", None):
        raw_candidates.add(str(cell.cell_code or ""))
    return {_normalized_cell_scan(value) for value in raw_candidates if value}


def _assert_cell_scan(allocation: FbsOrderStockAllocation, cell_scan: str) -> None:
    if _normalized_cell_scan(cell_scan) not in _cell_scan_candidates(allocation):
        raise FbsPickingError("Скан ячейки не совпадает с физическим адресом маршрута.")


def _assert_item_scan(allocation: FbsOrderStockAllocation, item_scan: str) -> None:
    balance = allocation.balance
    normalized_scan = _normalized(item_scan)
    accepted = {
        _normalized(balance.barcode),
        _normalized(allocation.order_item.barcode),
    }
    accepted.discard("")
    if not normalized_scan or normalized_scan not in accepted:
        raise FbsPickingError("Штрихкод товара не совпадает с заданием.")


def _scan_event_context(allocation: FbsOrderStockAllocation) -> dict:
    return {
        "batch_id": allocation.pick_task.batch_id,
        "task_id": allocation.pick_task_id,
        "allocation_id": allocation.id,
    }


def _record_failed_allocation_scan(
    *, allocation_id: int, stage: str, scan_value: str, error, performed_by
) -> None:
    allocation = (
        FbsOrderStockAllocation.objects.select_related(
            "pick_task",
            "balance__box__pallet__cell__location",
            "balance__box__source_container__current_location",
        )
        .filter(pk=allocation_id, pick_task__isnull=False)
        .first()
    )
    if allocation is None:
        return
    expected = {
        FbsPickScanEvent.STAGE_CELL: fbs_box_physical_location_code(
            allocation.balance.box
        ),
        FbsPickScanEvent.STAGE_BOX: allocation.balance.box.box_code,
        FbsPickScanEvent.STAGE_PICK_ITEM: (
            allocation.balance.barcode or allocation.order_item.barcode
        ),
        FbsPickScanEvent.STAGE_VERIFY_ITEM: (
            allocation.balance.barcode or allocation.order_item.barcode
        ),
        FbsPickScanEvent.STAGE_VERIFY_MARKING: (
            allocation.balance.marking_code
            or "КИЗ Data Matrix: 01 + GTIN + 21 + серийный номер"
        ),
        FbsPickScanEvent.STAGE_VERIFY_EXPIRY: "Допустимый срок годности",
    }.get(stage, "")
    record_pick_scan_event(
        **_scan_event_context(allocation),
        stage=stage,
        result=FbsPickScanEvent.RESULT_ERROR,
        scan_value=scan_value,
        expected_value=expected,
        message=str(error),
        created_by=performed_by,
    )


def _swap_picked_marking_within_batch(
    *,
    allocation: FbsOrderStockAllocation,
    trace: FbsOrderTraceability,
    used_trace: FbsOrderTraceability,
    scanned_balance: FbsStockBalance,
    reserved_balance: FbsStockBalance,
    performed_by=None,
) -> str | None:
    """Обменять Честные знаки двух отобранных заказов одной волны.

    Единицы одного товара в таре физически взаимозаменяемы. Пока по обоим
    заказам ничего не передано маркетплейсу и ничего не отгружено,
    перестановка привязки не меняет ни остатки, ни обязательства перед WB.

    Возвращает текст для оператора либо None, если обмен недопустим —
    тогда вызывающий код поднимает прежнюю ошибку.
    """
    if used_trace.allocation_id is None or used_trace.allocation_id == allocation.pk:
        return None

    # Перечитываем встречную сторону под блокировкой: used_trace пришёл
    # без select_for_update, а мы его меняем.
    other = (
        FbsOrderStockAllocation.objects.select_for_update(of=("self",))
        .select_related("order_item__order__profile", "pick_task")
        .get(pk=used_trace.allocation_id)
    )
    used_trace = (
        FbsOrderTraceability.objects.select_for_update()
        .get(pk=used_trace.pk)
    )
    used_trace.allocation = other

    task = allocation.pick_task
    other_task = other.pick_task
    if task is None or other_task is None or task.batch_id != other_task.batch_id:
        return None

    for row in (allocation, other):
        if (
            row.status != FbsOrderStockAllocation.STATUS_PICKED
            or int(row.qty_reserved or 0) != 1
            or int(row.qty_picked or 0) != 1
        ):
            return None
    for row_trace in (trace, used_trace):
        if (
            row_trace.status != FbsOrderTraceability.STATUS_PICKED
            or int(row_trace.qty or 0) != 1
        ):
            return None

    if _normalized(allocation.order_item.barcode) != _normalized(
        other.order_item.barcode
    ):
        return None

    order = allocation.order_item.order
    other_order = other.order_item.order
    if order.pk == other_order.pk:
        return None
    if order.profile.marketplace != "wb" or other_order.profile.marketplace != "wb":
        return None
    if _required_marking_codes(allocation.order_item) or _required_marking_codes(
        other.order_item
    ):
        return None

    for row, row_trace, row_order in (
        (allocation, trace, order),
        (other, used_trace, other_order),
    ):
        if FbsPickVerificationProgress.objects.filter(allocation=row).exists():
            return None
        if row_trace.metadata_transfers.exists():
            return None
        if row_order.marketplace_labels.exists():
            return None
        if FbsHandoverOrderAssignment.objects.filter(order=row_order).exists():
            return None

    for balance in (scanned_balance, reserved_balance):
        if (
            int(balance.qty or 0) != 0
            or int(balance.available_qty or 0) != 0
            or int(balance.reserved_qty or 0) != 0
        ):
            return None

    other.balance = reserved_balance
    other.full_clean()
    other.save(update_fields=["balance", "updated_at"])
    used_trace.marking_code = reserved_balance.marking_code
    used_trace.lot_code = reserved_balance.lot_code
    used_trace.expiry_date = reserved_balance.expiry_date
    used_trace.full_clean()
    used_trace.save(
        update_fields=["marking_code", "lot_code", "expiry_date", "updated_at"]
    )

    allocation.balance = scanned_balance
    allocation.full_clean()
    allocation.save(update_fields=["balance", "updated_at"])
    trace.marking_code = scanned_balance.marking_code
    trace.lot_code = scanned_balance.lot_code
    trace.expiry_date = scanned_balance.expiry_date
    trace.full_clean()
    trace.save(
        update_fields=["marking_code", "lot_code", "expiry_date", "updated_at"]
    )

    from audit.models import OrderAuditEntry

    actor = _authenticated_user(performed_by)
    audit_payload = {
        "source": "fbs_controller_picked_marking_swap",
        "batch_id": task.batch_id,
        "current_order_id": order.pk,
        "current_external_order_id": order.external_order_id,
        "other_order_id": other_order.pk,
        "other_external_order_id": other_order.external_order_id,
        "current_allocation_id": allocation.pk,
        "other_allocation_id": other.pk,
        "current_marking": scanned_balance.marking_code,
        "other_marking": reserved_balance.marking_code,
        "skip_chat_bridge": True,
    }
    OrderAuditEntry.objects.create(
        order_id=str(order.pk),
        order_type="fbs_order",
        action="update",
        user=actor,
        agency=order.profile.agency,
        description=(
            "Честный знак закреплен за заказом на контроле FBS; прежний код "
            f"передан заказу {other_order.external_order_id} той же волны."
        ),
        payload={**audit_payload, "marking_swap_role": "verified_order"},
    )
    OrderAuditEntry.objects.create(
        order_id=str(other_order.pk),
        order_type="fbs_order",
        action="update",
        user=actor,
        agency=other_order.profile.agency,
        description=(
            "Честный знак заменен на равнозначный при контроле заказа "
            f"{order.external_order_id} той же волны."
        ),
        payload={**audit_payload, "marking_swap_role": "waiting_order"},
    )
    return (
        "Честный знак закреплен за этим заказом; прежний код передан заказу "
        f"{other_order.external_order_id}."
    )


def _validate_wb_pending_marking(
    *, allocation: FbsOrderStockAllocation, marking_scan: str
) -> str:
    """Validate a WB KIZ without binding it to an order.

    The successful scan is kept in the scan audit.  The durable KIZ -> order
    relation is created only after the controller scans the order label.
    """
    order = allocation.order_item.order
    if order.profile.marketplace != "wb":
        raise FbsPickingError("Отложенная привязка КИЗ поддерживается только для WB.")
    scanned_marking = _validate_kiz_scan(
        marking_scan,
        product_barcodes=_allocation_product_barcodes(allocation),
    )
    scanned_marking = _restore_wb_kiz_gs_separators(
        scanned_marking,
        reference_markings=(allocation.balance.marking_code,),
    )
    return _validate_kiz_scan(
        scanned_marking,
        product_barcodes=_allocation_product_barcodes(allocation),
    )


def _bind_wb_scanned_marking(
    *,
    allocation: FbsOrderStockAllocation,
    marking_scan: str,
    performed_by=None,
) -> tuple[FbsOrderTraceability, str, str]:
    """Bind the physical WB unit scanned at control to the picked allocation."""
    reserved_balance = allocation.balance
    reserved_marking = str(reserved_balance.marking_code or "").strip()
    scanned_marking = str(marking_scan or "").strip()
    trace = FbsOrderTraceability.objects.select_for_update().get(allocation=allocation)
    if not scanned_marking:
        raise FbsPickingError("Отсканируйте КИЗ фактически отобранной единицы.")
    order = allocation.order_item.order
    if order.profile.marketplace != "wb":
        raise FbsPickingError("КИЗ не совпадает с отобранной единицей.")

    if reserved_marking and scanned_marking == reserved_marking:
        trace.marking_code = scanned_marking
        trace.lot_code = reserved_balance.lot_code
        trace.expiry_date = reserved_balance.expiry_date
        trace.full_clean()
        trace.save(
            update_fields=["marking_code", "lot_code", "expiry_date", "updated_at"]
        )
        return trace, scanned_marking, "КИЗ закреплен после сканирования ШК заказа."

    candidate_id = (
        FbsStockBalance.objects.filter(
            agency_id=reserved_balance.agency_id,
            marking_code=scanned_marking,
        )
        .values_list("id", flat=True)
        .first()
    )
    if candidate_id is None:
        raise FbsPickingError("ЧЗ не найден в FBS-остатках этого клиента.")

    conflict_snapshot = (
        FbsOrderStockAllocation.objects.filter(
            balance_id=candidate_id,
            status__in=MARKING_CARRIER_ALLOCATION_STATUSES,
        )
        .exclude(pk=allocation.pk)
        .values("id", "order_item__order_id")
        .order_by("id")
        .first()
    )
    conflicting_order = None
    conflicting_allocation = None
    conflicting_trace = None
    if conflict_snapshot is not None:
        conflicting_order = (
            FbsOrder.objects.select_for_update(of=("self",))
            .select_related("profile__agency")
            .get(pk=conflict_snapshot["order_item__order_id"])
        )
        conflicting_allocation = (
            FbsOrderStockAllocation.objects.select_for_update(of=("self",))
            .select_related(
                "order_item__order__profile",
                "order_item__sku",
                "pick_task",
            )
            .filter(
                pk=conflict_snapshot["id"],
                status__in=MARKING_CARRIER_ALLOCATION_STATUSES,
            )
            .first()
        )
        if conflicting_allocation is not None:
            conflicting_trace = FbsOrderTraceability.objects.select_for_update().get(
                allocation=conflicting_allocation
            )

    locked_balances = {
        balance.id: balance
        for balance in FbsStockBalance.objects.select_for_update(of=("self",))
        .select_related(
            "box__pallet__cell",
            "box__source_container__current_location",
        )
        .filter(id__in=sorted({reserved_balance.id, candidate_id}))
        .order_by("id")
    }
    if reserved_balance.id not in locked_balances or candidate_id not in locked_balances:
        raise FbsPickingError("FBS-остаток изменился во время проверки ЧЗ.")
    reserved_balance = locked_balances[reserved_balance.id]
    scanned_balance = locked_balances[candidate_id]
    if scanned_balance.marking_code != scanned_marking:
        raise FbsPickingError("FBS-остаток изменился во время проверки ЧЗ.")
    expected_barcode = str(
        reserved_balance.barcode or allocation.order_item.barcode or ""
    ).strip()
    if _normalized(scanned_balance.barcode) != _normalized(expected_barcode):
        raise FbsPickingError("ЧЗ относится к другому товарному штрихкоду.")
    if (
        reserved_balance.sku_ref_id
        and scanned_balance.sku_ref_id
        and reserved_balance.sku_ref_id != scanned_balance.sku_ref_id
    ):
        raise FbsPickingError("ЧЗ относится к другому SKU клиента.")
    if scanned_balance.box_id != reserved_balance.box_id:
        raise FbsPickingError(
            "ЧЗ числится в другом коробе. Передайте единицу в складское исключение."
        )
    if not fbs_box_is_reservable(scanned_balance.box):
        raise FbsPickingError("ЧЗ находится в неактивном месте хранения.")
    if scanned_balance.expiry_date and scanned_balance.expiry_date < timezone.localdate():
        raise FbsPickingError("У отсканированной единицы истёк срок годности.")

    from .inventory import assert_balance_unlocked

    assert_balance_unlocked(reserved_balance.id, for_execution=True)
    assert_balance_unlocked(scanned_balance.id, for_execution=True)

    active_allocation_ids = list(
        FbsOrderStockAllocation.objects.filter(
            balance=scanned_balance,
            status__in=MARKING_CARRIER_ALLOCATION_STATUSES,
        )
        .exclude(pk=allocation.pk)
        .order_by("id")
        .values_list("id", flat=True)[:2]
    )
    if active_allocation_ids and (
        conflicting_allocation is None
        or active_allocation_ids != [conflicting_allocation.id]
    ):
        raise FbsPickingError("Резерв ЧЗ изменился. Повторите сканирование.")
    if not active_allocation_ids:
        conflicting_order = None
        conflicting_allocation = None
        conflicting_trace = None

    used_trace = (
        FbsOrderTraceability.objects.filter(
            marking_code=scanned_marking,
            status__in=(
                FbsOrderTraceability.STATUS_RESERVED,
                FbsOrderTraceability.STATUS_PICKED,
            ),
        )
        .exclude(allocation=allocation)
        .select_related("allocation__order_item__order")
        .order_by("id")
        .first()
    )
    if used_trace is not None:
        owner_order = used_trace.allocation.order_item.order
        current_order_id = allocation.order_item.order_id
        raise FbsMarkingAlreadyUsedError(
            marking_code=scanned_marking,
            owner_order_id=owner_order.external_order_id,
            same_order=owner_order.id == current_order_id,
        )

    if (
        int(allocation.qty_reserved or 0) != 1
        or int(allocation.qty_picked or 0) != 1
        or allocation.status != FbsOrderStockAllocation.STATUS_PICKED
    ):
        raise FbsPickingError(
            "Для привязки КИЗа требуется одна полностью отобранная единица товара."
        )
    replacement_conflicts = FbsOrderStockAllocation.objects.filter(
        balance=reserved_balance,
        status__in=MARKING_CARRIER_ALLOCATION_STATUSES,
    ).exclude(pk=allocation.pk)
    if conflicting_allocation is not None:
        replacement_conflicts = replacement_conflicts.exclude(
            pk=conflicting_allocation.pk
        )
    if reserved_marking and replacement_conflicts.exists():
        raise FbsPickingError("Технический ЧЗ уже используется другим резервом.")
    replacement_trace_ids = []
    if reserved_marking:
        replacement_trace_ids = list(
            FbsOrderTraceability.objects.filter(
                marking_code=reserved_marking,
                status__in=(
                    FbsOrderTraceability.STATUS_RESERVED,
                    FbsOrderTraceability.STATUS_PICKED,
                ),
            )
            .exclude(pk=trace.pk)
            .values_list("id", flat=True)[:1]
        )
    if replacement_trace_ids:
        raise FbsPickingError("Технический ЧЗ уже используется другим заказом.")

    binding_message = "КИЗ закреплен после сканирования ШК заказа."
    if conflicting_allocation is None:
        if int(scanned_balance.reserved_qty or 0) > 0:
            raise FbsPickingError("Технический резерв количества изменился.")
        if (
            int(scanned_balance.qty or 0) != 1
            or int(scanned_balance.available_qty or 0) != 1
        ):
            raise FbsPickingError("ЧЗ недоступен как свободная единица FBS-остатка.")
        reserved_balance.qty = int(reserved_balance.qty or 0) + 1
        reserved_balance.available_qty = int(reserved_balance.available_qty or 0) + 1
        scanned_balance.qty = int(scanned_balance.qty or 0) - 1
        scanned_balance.available_qty = int(scanned_balance.available_qty or 0) - 1
        reserved_balance.full_clean()
        scanned_balance.full_clean()
        reserved_balance.save(update_fields=["qty", "available_qty", "updated_at"])
        scanned_balance.save(update_fields=["qty", "available_qty", "updated_at"])
    else:
        if conflicting_order is None or conflicting_trace is None:
            raise FbsPickingError("Резерв ЧЗ изменился. Повторите сканирование.")
        if conflicting_order.profile.marketplace != "wb":
            raise FbsPickingError("Технический носитель занят заказом другого маркетплейса.")
        if (
            conflicting_allocation.status not in MARKING_CARRIER_ALLOCATION_STATUSES
            or int(conflicting_allocation.qty_reserved or 0) != 1
            or int(conflicting_trace.qty or 0) != 1
        ):
            raise FbsPickingError("Технический носитель количества изменился.")
        if conflicting_trace.marking_code:
            raise FbsPickingError("ЧЗ уже окончательно привязан к другому заказу.")
        if conflicting_trace.metadata_transfers.exists():
            raise FbsPickingError("По другому заказу уже подготовлены данные КИЗ.")
        minimum_expiry = _minimum_expiry(conflicting_allocation.order_item)
        if (
            reserved_balance.expiry_date
            and reserved_balance.expiry_date < timezone.localdate()
        ):
            raise FbsPickingError("Технический ЧЗ имеет истёкший срок годности.")
        if (
            minimum_expiry is not None
            and reserved_balance.expiry_date is not None
            and reserved_balance.expiry_date < minimum_expiry
        ):
            raise FbsPickingError("Технический ЧЗ не подходит по сроку второго заказа.")
        carrier_is_waiting = conflicting_allocation.status in (
            FbsOrderStockAllocation.STATUS_RESERVED,
            FbsOrderStockAllocation.STATUS_PICKING,
        )
        expected_scanned_state = (1, 0, 1) if carrier_is_waiting else (0, 0, 0)
        scanned_state = (
            int(scanned_balance.qty or 0),
            int(scanned_balance.available_qty or 0),
            int(scanned_balance.reserved_qty or 0),
        )
        if scanned_state != expected_scanned_state:
            raise FbsPickingError("Технический носитель количества изменился.")
        if carrier_is_waiting:
            reserved_balance.qty = int(reserved_balance.qty or 0) + 1
            reserved_balance.reserved_qty = int(reserved_balance.reserved_qty or 0) + 1
            scanned_balance.qty = int(scanned_balance.qty or 0) - 1
            scanned_balance.reserved_qty = int(scanned_balance.reserved_qty or 0) - 1
        reserved_balance.full_clean()
        scanned_balance.full_clean()
        balance_update_fields = ["qty", "available_qty", "reserved_qty", "updated_at"]
        reserved_balance.save(update_fields=balance_update_fields)
        scanned_balance.save(update_fields=balance_update_fields)

        conflicting_allocation.balance = reserved_balance
        conflicting_allocation.full_clean()
        conflicting_allocation.save(update_fields=["balance", "updated_at"])
        conflicting_trace.allocation = conflicting_allocation
        # У второго заказа остается только резерв количества. Его КИЗ будет
        # определен собственным контролером после сканирования ШК заказа.
        conflicting_trace.marking_code = ""
        conflicting_trace.lot_code = reserved_balance.lot_code
        conflicting_trace.expiry_date = reserved_balance.expiry_date
        conflicting_trace.full_clean()
        conflicting_trace.save(
            update_fields=["marking_code", "lot_code", "expiry_date", "updated_at"]
        )
        binding_message = (
            "КИЗ закреплен после сканирования ШК заказа; технический носитель "
            "количества передан заказу "
            f"{conflicting_order.external_order_id}."
        )

    allocation.balance = scanned_balance
    allocation.full_clean()
    allocation.save(update_fields=["balance", "updated_at"])

    trace.allocation = allocation
    trace.marking_code = scanned_balance.marking_code
    trace.lot_code = scanned_balance.lot_code
    trace.expiry_date = scanned_balance.expiry_date
    trace.full_clean()
    trace.save(
        update_fields=["marking_code", "lot_code", "expiry_date", "updated_at"]
    )
    if conflicting_allocation is not None:
        from audit.models import OrderAuditEntry

        actor = _authenticated_user(performed_by)
        audit_payload = {
            "source": "fbs_controller_marking_reservation_swap",
            "current_order_id": order.pk,
            "current_external_order_id": order.external_order_id,
            "other_order_id": conflicting_order.pk,
            "other_external_order_id": conflicting_order.external_order_id,
            "current_allocation_id": allocation.pk,
            "other_allocation_id": conflicting_allocation.pk,
            "technical_balance_id": reserved_balance.pk,
            "scanned_balance_id": scanned_balance.pk,
            "technical_marking": reserved_marking,
            "scanned_marking": scanned_marking,
            "skip_chat_bridge": True,
        }
        OrderAuditEntry.objects.create(
            order_id=str(order.pk),
            order_type="fbs_order",
            action="update",
            user=actor,
            agency=order.profile.agency,
            description=(
                "Фактический ЧЗ закреплен после сканирования ШК заказа; "
                "технический носитель количества "
                f"передан заказу {conflicting_order.external_order_id}."
            ),
            payload={**audit_payload, "reservation_swap_role": "verified_order"},
        )
        OrderAuditEntry.objects.create(
            order_id=str(conflicting_order.pk),
            order_type="fbs_order",
            action="update",
            user=actor,
            agency=conflicting_order.profile.agency,
            description=(
                "Технический носитель количества заменен при контроле заказа "
                f"{order.external_order_id}."
            ),
            payload={**audit_payload, "reservation_swap_role": "waiting_order"},
        )
    elif scanned_marking != reserved_marking:
        from audit.models import OrderAuditEntry

        actor = _authenticated_user(performed_by)
        OrderAuditEntry.objects.create(
            order_id=str(order.pk),
            order_type="fbs_order",
            action="update",
            user=actor,
            agency=order.profile.agency,
            description=(
                "Фактически отсканированный Честный знак закреплен за WB-заказом "
                "после сканирования этикетки заказа."
            ),
            payload={
                "source": "fbs_controller_marking_deferred_binding",
                "allocation_id": allocation.pk,
                "technical_balance_id": reserved_balance.pk,
                "scanned_balance_id": scanned_balance.pk,
                "technical_marking": reserved_marking,
                "scanned_marking": scanned_marking,
                "skip_chat_bridge": True,
            },
        )
    return trace, scanned_marking, binding_message


def _bind_controller_marking_to_trace(
    *, allocation: FbsOrderStockAllocation, marking_scan: str
) -> tuple[FbsOrderTraceability, str, str]:
    scanned_marking = str(marking_scan or "").strip()
    if not scanned_marking:
        raise FbsPickingError("Отсканируйте КИЗ фактически проверяемой единицы.")
    trace = FbsOrderTraceability.objects.select_for_update().get(allocation=allocation)
    if int(trace.qty or 0) != 1:
        raise FbsPickingError(
            "Для нескольких маркированных единиц требуется поединичная трассировка."
        )
    required_codes = {
        _normalized(code) for code in _required_marking_codes(allocation.order_item)
    }
    if required_codes and _normalized(scanned_marking) not in required_codes:
        raise FbsPickingError("КИЗ не совпадает с кодом, переданным маркетплейсом.")
    if trace.marking_code:
        if _normalized(trace.marking_code) != _normalized(scanned_marking):
            raise FbsPickingError("КИЗ не совпадает с уже проверенной единицей.")
        return trace, trace.marking_code, ""
    used_trace = (
        FbsOrderTraceability.objects.filter(
            marking_code=scanned_marking,
            status__in=(
                FbsOrderTraceability.STATUS_RESERVED,
                FbsOrderTraceability.STATUS_PICKED,
            ),
        )
        .exclude(pk=trace.pk)
        .select_related("allocation__order_item__order")
        .order_by("id")
        .first()
    )
    if used_trace is not None:
        owner_order = used_trace.allocation.order_item.order
        raise FbsMarkingAlreadyUsedError(
            marking_code=scanned_marking,
            owner_order_id=owner_order.external_order_id,
            same_order=owner_order.id == allocation.order_item.order_id,
        )
    trace.marking_code = scanned_marking
    trace.full_clean()
    trace.save(update_fields=["marking_code", "updated_at"])
    return trace, scanned_marking, "КИЗ закреплен контролером за заказом."


def _split_unverified_ozon_unit(
    allocation: FbsOrderStockAllocation,
    trace: FbsOrderTraceability,
) -> None:
    """Keep this unit's ID and leave the other picked units for later scans.

    Called with the allocation and trace locked inside verification's atomic
    transaction. A rejected scan rolls back the split as well as the binding.
    The stock balance and the task/batch totals are deliberately not updated.
    """
    qty = int(allocation.qty_picked or 0)
    if qty <= 1:
        return
    if (
        allocation.status != FbsOrderStockAllocation.STATUS_PICKED
        or int(allocation.qty_reserved or 0) != qty
        or int(trace.qty or 0) != qty
        or trace.status != FbsOrderTraceability.STATUS_PICKED
        or trace.marking_code
        or FbsPickVerificationProgress.objects.filter(
            allocation=allocation, qty_verified__gt=0,
        ).exists()
        or trace.metadata_transfers.exists()
    ):
        raise FbsPickingError(
            "Нельзя выделить единицу для КИЗ: позиция уже частично подтверждена "
            "или её поштучный учёт изменился. Требуется проверка позиции."
        )

    remainder = FbsOrderStockAllocation.objects.create(
        order_item=allocation.order_item,
        balance=allocation.balance,
        pick_task=allocation.pick_task,
        reserved_by_id=allocation.reserved_by_id,
        picked_by_id=allocation.picked_by_id,
        picked_at=allocation.picked_at,
        qty_reserved=qty - 1,
        qty_picked=qty - 1,
        status=FbsOrderStockAllocation.STATUS_PICKED,
    )
    FbsOrderTraceability.objects.create(
        allocation=remainder,
        qty=qty - 1,
        lot_code=trace.lot_code,
        expiry_date=trace.expiry_date,
        status=FbsOrderTraceability.STATUS_PICKED,
    )
    allocation.qty_reserved = 1
    allocation.qty_picked = 1
    allocation.save(update_fields=["qty_reserved", "qty_picked", "updated_at"])
    trace.qty = 1
    trace.save(update_fields=["qty", "updated_at"])


def _bind_ozon_controller_marking_to_trace(
    *, allocation: FbsOrderStockAllocation, marking_scan: str
) -> tuple[FbsOrderTraceability, str, str]:
    """Bind the physical Ozon KIZ without changing the reserved stock balance."""
    order = allocation.order_item.order
    if order.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_OZON:
        raise FbsPickingError("Прямая привязка КИЗ доступна только для Ozon.")
    scanned_marking = str(marking_scan or "").strip()
    if not scanned_marking:
        raise FbsPickingError("Отсканируйте КИЗ фактически проверяемой единицы.")
    trace = FbsOrderTraceability.objects.select_for_update().get(allocation=allocation)
    _split_unverified_ozon_unit(allocation, trace)
    if int(trace.qty or 0) != 1:
        raise FbsPickingError(
            "Для нескольких маркированных единиц требуется поединичная трассировка."
        )
    if (
        allocation.status != FbsOrderStockAllocation.STATUS_PICKED
        or int(allocation.qty_reserved or 0) != 1
        or int(allocation.qty_picked or 0) != 1
    ):
        raise FbsPickingError(
            "Для привязки Ozon КИЗа требуется одна полностью отобранная единица."
        )
    required_codes = {
        _normalized(code) for code in _required_marking_codes(allocation.order_item)
    }
    if required_codes and _normalized(scanned_marking) not in required_codes:
        raise FbsPickingError("КИЗ не совпадает с кодом, переданным маркетплейсом.")
    if _normalized(trace.marking_code) == _normalized(scanned_marking):
        return trace, trace.marking_code, ""
    used_traces = list(
        FbsOrderTraceability.objects.filter(
            marking_code=scanned_marking,
            status__in=(
                FbsOrderTraceability.STATUS_RESERVED,
                FbsOrderTraceability.STATUS_PICKED,
            ),
        )
        .exclude(pk=trace.pk)
        .select_for_update(of=("self",))
        .select_related(
            "allocation__order_item__order__profile",
            "allocation__pick_task__batch",
        )
        .order_by("id")
        [:2]
    )
    binding_message = "Фактический КИЗ закреплен за заказом Ozon."
    if len(used_traces) > 1:
        owner_order = used_traces[0].allocation.order_item.order
        raise FbsMarkingAlreadyUsedError(
            marking_code=scanned_marking,
            owner_order_id=owner_order.external_order_id,
            same_order=owner_order.id == allocation.order_item.order_id,
        )
    if used_traces:
        used_trace = used_traces[0]
        other = (
            FbsOrderStockAllocation.objects.select_for_update(of=("self",))
            .select_related(
                "order_item__order__profile",
                "pick_task__batch",
            )
            .get(pk=used_trace.allocation_id)
        )
        used_trace.allocation = other
        owner_order = used_trace.allocation.order_item.order
        same_unverified_ozon_batch = (
            allocation.pick_task_id is not None
            and other.pick_task_id is not None
            and allocation.pick_task.batch_id == other.pick_task.batch_id
            and owner_order.profile.marketplace
            == FbsIntegrationProfile.MARKETPLACE_OZON
            and other.status == FbsOrderStockAllocation.STATUS_PICKED
            and int(other.qty_reserved or 0) == 1
            and int(other.qty_picked or 0) == 1
            and used_trace.status == FbsOrderTraceability.STATUS_PICKED
            and int(used_trace.qty or 0) == 1
            and not _required_marking_codes(other.order_item)
            and not FbsPickVerificationProgress.objects.filter(
                allocation=other,
                qty_verified__gt=0,
            ).exists()
            and not FbsPickScanEvent.objects.filter(
                allocation=other,
                stage=FbsPickScanEvent.STAGE_VERIFY_MARKING,
                result=FbsPickScanEvent.RESULT_SUCCESS,
            ).exists()
            and not used_trace.metadata_transfers.exists()
            and not owner_order.marketplace_labels.exists()
            and not FbsHandoverOrderAssignment.objects.filter(
                order=owner_order
            ).exists()
        )
        if not same_unverified_ozon_batch:
            raise FbsMarkingAlreadyUsedError(
                marking_code=scanned_marking,
                owner_order_id=owner_order.external_order_id,
                same_order=owner_order.id == allocation.order_item.order_id,
            )
        cleared = FbsOrderTraceability.objects.filter(
            pk=used_trace.pk,
            allocation=other,
            marking_code=scanned_marking,
            status=FbsOrderTraceability.STATUS_PICKED,
        ).update(marking_code="", updated_at=timezone.now())
        if cleared != 1:
            raise FbsPickingError(
                "Техническая привязка КИЗа изменилась. Повторите сканирование."
            )
        binding_message = (
            "Фактический КИЗ закреплен за заказом Ozon; техническая привязка "
            f"освобождена у непроверенного заказа {owner_order.external_order_id}."
        )
    # The balance marking is the technical code reserved before the picker
    # handles the physical unit.  Ozon must receive the controller's actual
    # Data Matrix, so this command intentionally replaces only the order trace.
    # The locked balance, its quantity and its reservation remain untouched.
    updated_at = timezone.now()
    updated = FbsOrderTraceability.objects.filter(
        pk=trace.pk,
        allocation=allocation,
        marking_code=trace.marking_code,
        status__in=(
            FbsOrderTraceability.STATUS_RESERVED,
            FbsOrderTraceability.STATUS_PICKED,
        ),
    ).update(marking_code=scanned_marking, updated_at=updated_at)
    if updated != 1:
        raise FbsPickingError("Трассировка КИЗа изменилась. Повторите сканирование.")
    trace.marking_code = scanned_marking
    trace.updated_at = updated_at
    return trace, scanned_marking, binding_message


def _bind_wb_controller_marking_to_order(
    *,
    allocation: FbsOrderStockAllocation,
    marking_scan: str,
    performed_by=None,
) -> tuple[FbsOrderTraceability, str, str]:
    """Bind the controller's physical WB KIZ without mutating FBS stock."""
    order = allocation.order_item.order
    if order.profile.marketplace != "wb":
        raise FbsPickingError("Прямая привязка КИЗ доступна только для WB.")
    scanned_marking = _validate_wb_pending_marking(
        allocation=allocation,
        marking_scan=marking_scan,
    )

    trace = FbsOrderTraceability.objects.select_for_update().get(
        allocation=allocation
    )
    if _normalized(trace.marking_code) == _normalized(scanned_marking):
        return trace, trace.marking_code, ""

    previous_marking = str(trace.marking_code or "").strip()
    trace.marking_code = scanned_marking
    trace.full_clean()
    trace.save(update_fields=["marking_code", "updated_at"])

    from audit.models import OrderAuditEntry

    actor = _authenticated_user(performed_by)
    OrderAuditEntry.objects.create(
        order_id=str(order.pk),
        order_type="fbs_order",
        action="update",
        user=actor,
        agency=order.profile.agency,
        description=(
            "Фактически отсканированный Честный знак закреплен за WB-заказом "
            "без сопоставления и перестановки FBS-остатков."
        ),
        payload={
            "source": "fbs_controller_wb_marking_direct",
            "allocation_id": allocation.id,
            "previous_marking": previous_marking,
            "scanned_marking": scanned_marking,
            "stock_mutated": False,
            "skip_chat_bridge": True,
        },
    )
    return trace, scanned_marking, "КИЗ закреплен за WB-заказом."


@transaction.atomic
def finalize_wb_order_markings(
    *, order_id: int, performed_by=None
) -> tuple[str, ...]:
    """Bind audited WB KIZ scans only after the order label is scanned."""
    order = (
        FbsOrder.objects.select_for_update(of=("self",))
        .select_related("profile")
        .get(pk=order_id)
    )
    if order.profile.marketplace != "wb":
        return ()
    allocations = list(
        FbsOrderStockAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "balance__box__pallet__cell",
            "order_item__order__profile",
            "order_item__sku",
            "pick_task__batch",
            "traceability",
            "verification_progress",
        )
        .filter(
            order_item__order=order,
            status=FbsOrderStockAllocation.STATUS_PICKED,
            qty_picked__gt=0,
        )
        .order_by("order_item_id", "id")
    )
    finalized: list[str] = []
    for allocation in allocations:
        requirements = metadata_requirements(allocation.order_item)
        progress = getattr(allocation, "verification_progress", None)
        legacy_absence = bool(
            progress is not None
            and legacy_marking_absence_confirmed(
                allocation,
                quantity_after=int(progress.qty_verified or 0),
            )
        )
        scan_event = None
        if progress is not None:
            scan_event = (
                FbsPickScanEvent.objects.select_for_update()
                .filter(
                    allocation=allocation,
                    stage=FbsPickScanEvent.STAGE_VERIFY_MARKING,
                    result=FbsPickScanEvent.RESULT_SUCCESS,
                    quantity_after=progress.qty_verified,
                )
                .exclude(scan_value="")
                .order_by("-id")
                .first()
            )
        requires_marking = bool(
            requirements.marking_required
            or allocation.balance.marking_code
            or scan_event is not None
        )
        if not requires_marking:
            continue
        if legacy_absence:
            if requirements.marketplace_marking_required:
                raise FbsPickingError(
                    "Площадка требует КИЗ; пропуск для старой партии запрещен."
                )
            continue
        if int(allocation.qty_picked or 0) != 1:
            raise FbsPickingError(
                "Для маркированного WB-товара требуется отдельный резерв на каждую единицу."
            )
        if progress is None:
            raise FbsPickingError("Проверка маркированного товара еще не завершена.")
        if int(progress.qty_verified or 0) != int(allocation.qty_picked or 0):
            raise FbsPickingError("Проверка маркированного товара еще не завершена.")
        trace = allocation.traceability
        if trace.marking_code:
            finalized.append(trace.marking_code)
            continue
        if scan_event is None:
            raise FbsPickingError(
                "Не найден подтвержденный скан КИЗ. Повторите проверку товара."
            )
        marking_scan = _validate_kiz_scan(
            scan_event.scan_value,
            product_barcodes=(
                allocation.balance.barcode,
                allocation.order_item.barcode,
            ),
        )
        trace, marking_scan, _ = _bind_wb_controller_marking_to_order(
            allocation=allocation,
            marking_scan=marking_scan,
            performed_by=performed_by,
        )
        finalized.append(str(trace.marking_code or marking_scan).strip())
    return tuple(finalized)


def validate_pick_cell_scan(*, allocation_id: int, cell_scan: str, performed_by) -> None:
    _require_picking_writes()
    allocation = (
        FbsOrderStockAllocation.objects.select_related(
            "pick_task",
            "balance__box__pallet__cell__location",
            "balance__box__source_container__current_location",
        )
        .get(pk=allocation_id)
    )
    try:
        if allocation.pick_task_id is None:
            raise FbsPickingError("Резерв не включен в задание отбора.")
        _assert_task_actor(allocation.pick_task, performed_by)
        if allocation.status != FbsOrderStockAllocation.STATUS_PICKING:
            raise FbsPickingError("Позиция не находится в отборе.")
        from .inventory import assert_balance_unlocked

        assert_balance_unlocked(allocation.balance_id, for_execution=True)
        _assert_cell_scan(allocation, cell_scan)
    except FbsError as exc:
        _record_failed_allocation_scan(
            allocation_id=allocation_id,
            stage=FbsPickScanEvent.STAGE_CELL,
            scan_value=cell_scan,
            error=exc,
            performed_by=performed_by,
        )
        raise
    record_pick_scan_event(
        **_scan_event_context(allocation),
        stage=FbsPickScanEvent.STAGE_CELL,
        result=FbsPickScanEvent.RESULT_SUCCESS,
        scan_value=cell_scan,
        expected_value=fbs_box_physical_location_code(allocation.balance.box),
        message="Ячейка подтверждена.",
        created_by=performed_by,
    )


def validate_pick_box_scan(*, allocation_id: int, box_scan: str, performed_by) -> None:
    _require_picking_writes()
    allocation = (
        FbsOrderStockAllocation.objects.select_related(
            "pick_task",
            "balance__box__pallet__cell__location",
            "balance__box__source_container__current_location",
        )
        .get(pk=allocation_id)
    )
    try:
        if allocation.pick_task_id is None:
            raise FbsPickingError("Резерв не включен в задание отбора.")
        _assert_task_actor(allocation.pick_task, performed_by)
        if allocation.status != FbsOrderStockAllocation.STATUS_PICKING:
            raise FbsPickingError("Позиция не находится в отборе.")
        from .inventory import assert_balance_unlocked

        assert_balance_unlocked(allocation.balance_id, for_execution=True)
        _assert_box_scan(allocation, box_scan)
    except FbsError as exc:
        _record_failed_allocation_scan(
            allocation_id=allocation_id,
            stage=FbsPickScanEvent.STAGE_BOX,
            scan_value=box_scan,
            error=exc,
            performed_by=performed_by,
        )
        raise
    record_pick_scan_event(
        **_scan_event_context(allocation),
        stage=FbsPickScanEvent.STAGE_BOX,
        result=FbsPickScanEvent.RESULT_SUCCESS,
        scan_value=box_scan,
        expected_value=allocation.balance.box.box_code,
        message=(
            "Короб подтвержден. Физический адрес: "
            f"{fbs_box_physical_location_code(allocation.balance.box)}."
        ),
        created_by=performed_by,
    )


def _finalize_pick_task_progress(
    *,
    task: FbsPickTask,
    actor,
    picked_qty_delta: int,
    planned_qty_reduction: int = 0,
    signal_sender,
    now=None,
) -> FbsPickBatch:
    now = now or timezone.now()
    picked_qty_delta = int(picked_qty_delta or 0)
    planned_qty_reduction = int(planned_qty_reduction or 0)
    next_task_picked_qty = int(task.picked_qty or 0) + picked_qty_delta
    next_task_planned_qty = int(task.planned_qty or 0) - planned_qty_reduction
    if next_task_planned_qty <= 0 or next_task_planned_qty < next_task_picked_qty:
        raise FbsPickingError("Состав задания FBS поврежден.")

    task.picked_qty = next_task_picked_qty
    task.planned_qty = next_task_planned_qty
    has_open_allocations = FbsOrderStockAllocation.objects.filter(
        pick_task=task,
        status__in=ACTIVE_ALLOCATION_STATUSES,
    ).exists()
    if not has_open_allocations:
        task.status = FbsPickTask.STATUS_PICKED
        task.completed_at = now
        task.order.internal_status = FbsOrder.STATUS_PICKED
        task.order.save(update_fields=["internal_status", "updated_at"])
        try:
            from fbs.signals import order_billing_ready

            responses = order_billing_ready.send_robust(
                sender=signal_sender,
                order=task.order,
                user=actor,
            )
            for _, response in responses:
                if isinstance(response, Exception):
                    raise response
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "Unable to sync FBS picking facts for order %s", task.order_id
            )
    task.save(
        update_fields=[
            "planned_qty",
            "picked_qty",
            "status",
            "completed_at",
            "updated_at",
        ]
    )

    batch = FbsPickBatch.objects.select_for_update().get(pk=task.batch_id)
    next_batch_planned_qty = int(batch.planned_qty or 0) - planned_qty_reduction
    next_batch_picked_qty = int(batch.picked_qty or 0) + picked_qty_delta
    if next_batch_planned_qty < next_batch_picked_qty:
        raise FbsPickingError("Состав волны FBS поврежден.")
    batch.planned_qty = next_batch_planned_qty
    batch.picked_qty = next_batch_picked_qty
    has_open_tasks = FbsPickTask.objects.filter(
        batch=batch,
        status__in=ACTIVE_TASK_STATUSES,
    ).exists()
    route_completed = not has_open_tasks and batch.picked_qty == batch.planned_qty
    if route_completed:
        batch.status = FbsPickBatch.STATUS_VERIFICATION
        batch.completed_at = None
    batch.save(
        update_fields=[
            "planned_qty",
            "picked_qty",
            "status",
            "completed_at",
            "updated_at",
        ]
    )
    if route_completed:
        assign_pick_handover_workstation(batch_id=batch.id)
    return batch


def _complete_pick_allocation_once(
    *,
    allocation_id: int,
    cell_scan: str,
    box_scan: str,
    item_scan: str,
    performed_by,
) -> FbsOrderStockAllocation:
    _require_picking_writes()
    try:
        with transaction.atomic():
            allocation = (
                FbsOrderStockAllocation.objects.select_for_update(of=("self",))
                .select_related(
                    "pick_task",
                    "balance__box__pallet__cell__location",
                    "order_item__sku",
                )
                .get(pk=allocation_id)
            )
            if allocation.status == FbsOrderStockAllocation.STATUS_PICKED:
                raise FbsPickingError("Позиция уже отобрана.")
            if allocation.pick_task_id is None:
                raise FbsPickingError("Резерв не включен в задание отбора.")
            task = (
                FbsPickTask.objects.select_for_update()
                .select_related("order", "batch")
                .get(pk=allocation.pick_task_id)
            )
            allocation.pick_task = task
            _assert_task_actor(task, performed_by)
            if allocation.status != FbsOrderStockAllocation.STATUS_PICKING:
                raise FbsPickingError("Позиция не находится в отборе.")
            balance = (
                FbsStockBalance.objects.select_for_update(of=("self",))
                .select_related(
                    "box__pallet__cell__location",
                )
                # Keep the nullable source-container path out of FOR UPDATE.
                .prefetch_related("box__source_container__current_location")
                .get(pk=allocation.balance_id)
            )
            from .inventory import assert_balance_unlocked

            assert_balance_unlocked(balance.id, for_execution=True)
            allocation.balance = balance
            if not pick_requires_box_scan(balance.box):
                _assert_cell_scan(allocation, cell_scan)
            _assert_box_scan(allocation, box_scan)
            _assert_item_scan(allocation, item_scan)
            remaining = int(allocation.qty_reserved or 0) - int(allocation.qty_picked or 0)
            if remaining <= 0:
                raise FbsPickingError("Позиция уже отобрана.")
            if balance.marking_code and remaining != 1:
                raise FbsPickingError("Один КИЗ должен соответствовать одной единице товара.")
            if int(balance.qty or 0) < 1 or int(balance.reserved_qty or 0) < 1:
                raise FbsPickingError("FBS-остаток изменился после резервирования.")

            now = timezone.now()
            actor = _authenticated_user(performed_by)
            balance.qty = int(balance.qty or 0) - 1
            balance.reserved_qty = int(balance.reserved_qty or 0) - 1
            balance.save(update_fields=["qty", "reserved_qty", "updated_at"])
            allocation.qty_picked = int(allocation.qty_picked or 0) + 1
            allocation.picked_by = actor
            allocation.status = (
                FbsOrderStockAllocation.STATUS_PICKED
                if allocation.qty_picked == allocation.qty_reserved
                else FbsOrderStockAllocation.STATUS_PICKING
            )
            if allocation.status == FbsOrderStockAllocation.STATUS_PICKED:
                allocation.picked_at = now
            allocation.save(
                update_fields=[
                    "qty_picked",
                    "status",
                    "picked_by",
                    "picked_at",
                    "updated_at",
                ]
            )
            if allocation.status == FbsOrderStockAllocation.STATUS_PICKED:
                set_allocation_trace_status(allocation, FbsOrderTraceability.STATUS_PICKED)

            if int(balance.qty or 0) <= 0:
                _archive_empty_fbs_box_after_pick(
                    box_id=balance.box_id,
                    allocation_id=allocation.id,
                    performed_by=actor,
                    occurred_at=now,
                )

            _finalize_pick_task_progress(
                task=task,
                actor=actor,
                picked_qty_delta=1,
                signal_sender=complete_pick_allocation,
                now=now,
            )
            record_pick_scan_event(
                **_scan_event_context(allocation),
                stage=FbsPickScanEvent.STAGE_PICK_ITEM,
                result=FbsPickScanEvent.RESULT_SUCCESS,
                scan_value=item_scan,
                expected_value=balance.marking_code or balance.barcode,
                quantity_after=allocation.qty_picked,
                message="Единица добавлена в тару.",
                created_by=performed_by,
            )
            return allocation
    except FbsError as exc:
        _record_failed_allocation_scan(
            allocation_id=allocation_id,
            stage=FbsPickScanEvent.STAGE_PICK_ITEM,
            scan_value=item_scan,
            error=exc,
            performed_by=performed_by,
        )
        raise


def _container_has_live_warehouse_state(*, container_id: int) -> bool:
    live_snapshot = (
        Q(qty__gt=0)
        | Q(available_qty__gt=0)
        | Q(processing_reserved_qty__gt=0)
        | Q(shipping_reserved_qty__gt=0)
        | Q(other_reserved_qty__gt=0)
    )
    if WarehouseStockSnapshot.objects.filter(
        Q(container_id=container_id) | Q(parent_container_id=container_id),
        is_archived=False,
    ).filter(live_snapshot).exists():
        return True
    return WarehouseOperationTask.objects.filter(
        Q(container_id=container_id) | Q(container__parent_container_id=container_id),
        status__in=(
            WarehouseOperationTask.STATUS_CREATED,
            WarehouseOperationTask.STATUS_IN_PROGRESS,
        ),
    ).exists()


def _container_has_live_fbs_state(*, container_id: int) -> bool:
    live_balance = (
        Q(qty__gt=0) | Q(available_qty__gt=0) | Q(reserved_qty__gt=0)
    )
    return FbsStockBalance.objects.filter(
        Q(box__source_container_id=container_id)
        | Q(box__source_container__parent_container_id=container_id)
    ).filter(live_balance).exists()


def _release_empty_warehouse_container(
    *,
    container: WarehouseContainer,
    stock_context_id: str,
    performed_by,
    occurred_at,
    payload: dict,
) -> bool:
    if (
        container.status == WarehouseContainer.STATUS_ARCHIVED
        and container.current_location_id is None
        and container.parent_container_id is None
    ):
        return True
    if _container_has_live_warehouse_state(container_id=container.id):
        return False
    if _container_has_live_fbs_state(container_id=container.id):
        return False
    if container.child_containers.filter(
        status=WarehouseContainer.STATUS_ACTIVE
    ).exists():
        return False

    from_location_id = container.current_location_id
    from_zone_code = str(
        getattr(container.current_location, "zone_code", "") or ""
    )
    WarehouseEvent.objects.create(
        agency_id=container.agency_id,
        event_type="fbs_empty_container_released",
        stock_context_type="fbs_pick",
        stock_context_id=stock_context_id,
        container=container,
        from_location_id=from_location_id,
        from_zone_code=from_zone_code,
        qty=0,
        payload=payload,
        performed_by=performed_by,
        occurred_at=occurred_at,
    )
    container.current_location_id = None
    container.parent_container_id = None
    container.status = WarehouseContainer.STATUS_ARCHIVED
    container.save(
        update_fields=[
            "current_location",
            "parent_container",
            "status",
            "updated_at",
        ]
    )
    return True


@transaction.atomic
def _archive_empty_fbs_box_after_pick(
    *,
    box_id: int,
    allocation_id: int | None = None,
    performed_by=None,
    occurred_at=None,
) -> bool:
    """Release an empty FBS box and empty non-rack parent pallets atomically."""
    box = (
        FbsBox.objects.select_for_update(of=("self",))
        .select_related(
            "pallet__warehouse_container__current_location",
            "source_container__current_location",
            "source_container__parent_container__current_location",
        )
        .get(pk=box_id)
    )
    if box.pallet.is_rack_binding:
        return False

    from fbs.models import FbsExternalIssueLine
    if FbsExternalIssueLine.objects.filter(balance__box_id=box.id, issue__status__in=("reserved", "picking")).exists():
        return False

    balances = list(
        FbsStockBalance.objects.select_for_update(of=("self",))
        .filter(box_id=box.id)
        .only("id", "qty", "available_qty", "reserved_qty")
        .order_by("id")
    )
    if any(
        int(row.qty or 0) > 0
        or int(row.available_qty or 0) > 0
        or int(row.reserved_qty or 0) > 0
        for row in balances
    ):
        return False
    if FbsOrderStockAllocation.objects.filter(
        balance__box_id=box.id,
        status__in=ACTIVE_ALLOCATION_STATUSES,
    ).exists():
        return False

    actor = _authenticated_user(performed_by)
    released_at = occurred_at or timezone.now()
    stock_context_id = str(allocation_id or box.id)
    physical_parent_id = None
    if box.source_container_id:
        source_container = (
            WarehouseContainer.objects.select_for_update(of=("self",))
            .select_related("current_location")
            .get(pk=box.source_container_id)
        )
        physical_parent_id = source_container.parent_container_id
        if _container_has_live_warehouse_state(container_id=source_container.id):
            return False
        if source_container.child_containers.filter(
            status=WarehouseContainer.STATUS_ACTIVE
        ).exists():
            return False

    if box.status != FbsBox.STATUS_ARCHIVED:
        box.status = FbsBox.STATUS_ARCHIVED
        box.save(update_fields=["status", "updated_at"])
    if box.source_container_id:
        _release_empty_warehouse_container(
            container=source_container,
            stock_context_id=stock_context_id,
            performed_by=actor,
            occurred_at=released_at,
            payload={
                "reason": "fbs_box_depleted_after_pick",
                "fbs_box_id": box.id,
                "fbs_box_code": box.box_code,
            },
        )

    pallet = FbsPallet.objects.select_for_update().get(pk=box.pallet_id)
    pallet_is_empty = not pallet.boxes.filter(
        status__in=(FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE)
    ).exists()
    if pallet_is_empty:
        pallet.status = FbsPallet.STATUS_ARCHIVED
        pallet.save(update_fields=["status", "updated_at"])

    candidate_container_ids = {
        container_id
        for container_id in (
            physical_parent_id,
            pallet.warehouse_container_id if pallet_is_empty else None,
        )
        if container_id
    }
    for container_id in sorted(candidate_container_ids):
        container = (
            WarehouseContainer.objects.select_for_update(of=("self",))
            .select_related("current_location")
            .get(pk=container_id)
        )
        _release_empty_warehouse_container(
            container=container,
            stock_context_id=stock_context_id,
            performed_by=actor,
            occurred_at=released_at,
            payload={
                "reason": "fbs_pallet_depleted_after_pick",
                "fbs_box_id": box.id,
                "fbs_box_code": box.box_code,
                "fbs_pallet_id": pallet.id,
                "fbs_pallet_code": pallet.pallet_code,
            },
        )
    return True


def _is_deadlock_error(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if str(getattr(current, "sqlstate", "") or "") == "40P01":
            return True
        current = current.__cause__ or current.__context__
    return False


def complete_pick_allocation(
    *,
    allocation_id: int,
    cell_scan: str,
    box_scan: str,
    item_scan: str,
    performed_by,
) -> FbsOrderStockAllocation:
    """Complete one scan and transparently replay a rolled-back deadlock victim."""
    for attempt in range(PICK_DEADLOCK_RETRY_ATTEMPTS):
        try:
            return _complete_pick_allocation_once(
                allocation_id=allocation_id,
                cell_scan=cell_scan,
                box_scan=box_scan,
                item_scan=item_scan,
                performed_by=performed_by,
            )
        except OperationalError as exc:
            if not _is_deadlock_error(exc):
                raise
            if attempt + 1 < PICK_DEADLOCK_RETRY_ATTEMPTS:
                time.sleep(PICK_DEADLOCK_RETRY_DELAY_SECONDS * (attempt + 1))
                continue
            error = FbsPickingError(
                "Склад параллельно обновляет этот товар. "
                "Операция не проведена; повторите сканирование."
            )
            _record_failed_allocation_scan(
                allocation_id=allocation_id,
                stage=FbsPickScanEvent.STAGE_PICK_ITEM,
                scan_value=item_scan,
                error=error,
                performed_by=performed_by,
            )
            raise error from exc
    raise AssertionError("Недостижимое состояние повтора FBS-отбора.")


def active_partial_ozon_verification_task(
    batch: FbsPickBatch,
) -> FbsPickTask | None:
    """Keep the controller on the Ozon bundle they have already started.

    Product barcodes are deliberately resolved across a whole tote.  That is
    convenient for single-item orders, but it is unsafe for a multi-item order:
    a shared barcode can otherwise be credited to an earlier order and leave
    the physical bundle open.  The most recently touched partial bundle is the
    one that is currently in the controller's hands.
    """
    return (
        FbsPickTask.objects.filter(
            batch=batch,
            status=FbsPickTask.STATUS_PICKED,
            order__profile__marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            allocations__status=FbsOrderStockAllocation.STATUS_PICKED,
            allocations__qty_picked__gt=0,
        )
        .exclude(
            order__pick_restock_requests__status__in=(
                SEPARATED_PICK_RESTOCK_STATUSES
            )
        )
        .annotate(
            controller_picked_qty=Sum("allocations__qty_picked"),
            controller_verified_qty=Sum(
                "allocations__verification_progress__qty_verified"
            ),
            controller_last_verified_at=Max(
                "allocations__verification_progress__updated_at"
            ),
        )
        .filter(
            controller_picked_qty__gt=1,
            controller_verified_qty__gt=0,
            controller_verified_qty__lt=F("controller_picked_qty"),
        )
        .order_by("-controller_last_verified_at", "sort_order", "id")
        .first()
    )


def _partial_ozon_order_expected_barcodes(task: FbsPickTask) -> tuple[str, ...]:
    pending = (
        FbsOrderStockAllocation.objects.filter(
            pick_task=task,
            status=FbsOrderStockAllocation.STATUS_PICKED,
            qty_picked__gt=0,
        )
        .filter(
            Q(verification_progress__isnull=True)
            | Q(verification_progress__qty_verified__lt=F("qty_picked"))
        )
        .values_list("order_item__barcode", "balance__barcode")
    )
    return tuple(
        dict.fromkeys(
            str(order_barcode or balance_barcode or "").strip()
            for order_barcode, balance_barcode in pending
            if str(order_barcode or balance_barcode or "").strip()
        )
    )


def _raise_partial_ozon_order_scan_mismatch(
    task: FbsPickTask,
    *,
    scanned_value: str,
) -> None:
    expected = _partial_ozon_order_expected_barcodes(task)
    expected_display = " или ".join(expected) or "ШК оставшегося товара"
    raise FbsScanMismatchError(
        f"Сначала завершите набор заказа №{task.order.external_order_id}. "
        f"Отсканирован {scanned_value}; нужен {expected_display}. "
        "Скан не засчитан другому заказу."
    )


def find_pick_verification_allocation(
    *,
    batch_id: int,
    item_scan: str,
    performed_by,
) -> FbsOrderStockAllocation:
    """Resolve the next unverified unit by its physical product barcode."""
    _require_picking_writes()
    actor = _authenticated_user(performed_by)
    scanned_value = _normalized(item_scan)
    if not scanned_value:
        raise FbsPickingError("Отсканируйте штрихкод товара.")
    batch = FbsPickBatch.objects.get(pk=batch_id)
    if actor is None or batch.verification_assigned_to_id != actor.id:
        raise FbsPickingError("Проверка волны назначена другому оператору.")
    if batch.status != FbsPickBatch.STATUS_VERIFICATION:
        raise FbsPickingError("Волна еще не передана на проверку.")
    if batch.picking_completed_at is None:
        raise FbsPickingError("Сборщик еще не сдал волну на рабочее место.")

    active_bundle_task = active_partial_ozon_verification_task(batch)

    matching = (
        FbsOrderStockAllocation.objects.select_related(
            "pick_task__batch",
            "pick_task__order",
            "balance",
            "order_item",
            "verification_progress",
        )
        .filter(
            pick_task__batch=batch,
            pick_task__status=FbsPickTask.STATUS_PICKED,
            status=FbsOrderStockAllocation.STATUS_PICKED,
            qty_picked__gt=0,
        )
        .exclude(
            pick_task__order__pick_restock_requests__status__in=(
                SEPARATED_PICK_RESTOCK_STATUSES
            )
        )
        .order_by("pick_task__sort_order", "pick_task_id", "id")
    )
    if active_bundle_task is not None:
        matching = matching.filter(pick_task=active_bundle_task)
    if _looks_like_kiz_scan(item_scan):
        marking_gtin = _kiz_gtin14(item_scan)
        barcode_candidates = {
            marking_gtin[-length:]
            for length in _GTIN_LENGTHS
            if not marking_gtin[:-length].strip("0")
        }
        product_query = Q()
        for candidate in barcode_candidates:
            product_query |= Q(balance__barcode__iexact=candidate)
            product_query |= Q(order_item__barcode__iexact=candidate)
        matching = matching.filter(product_query)
        if not matching.exists():
            raise FbsScanMismatchError(
                "Честный знак относится к другому товару. "
                "Скан не принят; отсканируйте нужный товар."
            )
    else:
        matching = matching.filter(
            Q(balance__barcode__iexact=scanned_value)
            | Q(order_item__barcode__iexact=scanned_value)
        )
    allocation = (
        matching.filter(
            Q(verification_progress__isnull=True)
            | Q(verification_progress__qty_verified__lt=F("qty_picked"))
        )
        .order_by("pick_task__sort_order", "pick_task_id", "id")
        .first()
    )
    if allocation is not None:
        return allocation
    if active_bundle_task is not None:
        _raise_partial_ozon_order_scan_mismatch(
            active_bundle_task,
            scanned_value=scanned_value,
        )
    if not matching.exists():
        label_code = str(item_scan or "").strip()
        label_code_without_prefix = label_code.lstrip("*")
        label_candidates = {
            value
            for value in (
                label_code,
                label_code_without_prefix,
                f"*{label_code_without_prefix}" if label_code_without_prefix else "",
            )
            if value
        }
        label_query = Q()
        for candidate in label_candidates:
            label_query |= Q(barcode__iexact=candidate)
        scanned_label = (
            FbsOrderLabel.objects.select_related("order")
            .filter(
                order_id__in=batch.tasks.filter(
                    status=FbsPickTask.STATUS_PICKED,
                ).values("order_id")
            )
            .filter(label_query)
            .order_by("-requested_at", "-id")
            .first()
        )
        if scanned_label is not None:
            expected_barcodes = list(
                FbsOrderStockAllocation.objects.filter(
                    pick_task__batch=batch,
                    pick_task__status=FbsPickTask.STATUS_PICKED,
                    status=FbsOrderStockAllocation.STATUS_PICKED,
                    qty_picked__gt=0,
                )
                .exclude(
                    pick_task__order__pick_restock_requests__status__in=(
                        SEPARATED_PICK_RESTOCK_STATUSES
                    )
                )
                .filter(
                    Q(verification_progress__isnull=True)
                    | Q(verification_progress__qty_verified__lt=F("qty_picked"))
                )
                .annotate(
                    verification_barcode=F("balance__barcode"),
                )
                .values_list("verification_barcode", flat=True)
                .exclude(verification_barcode="")
                .distinct()
                .order_by("verification_barcode")[:4]
            )
            expected_display = ", ".join(expected_barcodes)
            expected_hint = (
                f" Ожидаемый ШК товара: {expected_display}."
                if expected_display
                else " Отсканируйте числовой ШК товара, указанный слева."
            )
            raise FbsPickingError(
                "Отсканирована QR-этикетка WB заказа "
                f"№{scanned_label.order.external_order_id}, а сейчас нужен ШК товара."
                f"{expected_hint} QR заказа повторно сканировать не нужно."
            )
        raise FbsPickingError(
            "Товар с таким штрихкодом отсутствует в текущей таре."
        )
    raise FbsPickingError(
        "Все единицы товара с этим штрихкодом уже проверены."
    )


def _pending_verified_order_label_exists(batch: FbsPickBatch) -> bool:
    """Keep one physical order in hand until its marketplace QR is scanned."""
    unverified_allocations = (
        FbsOrderStockAllocation.objects.filter(
            pick_task__batch=batch,
            pick_task__order_id=OuterRef("order_id"),
            pick_task__status=FbsPickTask.STATUS_PICKED,
            status=FbsOrderStockAllocation.STATUS_PICKED,
            qty_picked__gt=0,
        )
        .filter(
            Q(verification_progress__isnull=True)
            | Q(verification_progress__qty_verified__lt=F("qty_picked"))
        )
        .values("pk")
    )
    return (
        FbsOrderLabel.objects.filter(
            order__pick_tasks__batch=batch,
            status__in=(
                FbsOrderLabel.STATUS_REQUESTED,
                FbsOrderLabel.STATUS_READY,
                FbsOrderLabel.STATUS_ERROR,
            ),
        )
        .exclude(
            order__pick_restock_requests__status__in=(
                SEPARATED_PICK_RESTOCK_STATUSES
            )
        )
        .exclude(
            controller_tote_orders__pick_tote__pick_batch=batch,
            controller_tote_orders__status__in=(
                FbsControllerToteOrder.STATUS_LABELED,
                FbsControllerToteOrder.STATUS_COMPOSITION,
                FbsControllerToteOrder.STATUS_PACKED,
            ),
        )
        .annotate(has_unverified_allocations=Exists(unverified_allocations))
        .filter(has_unverified_allocations=False)
        .exists()
    )


def _verify_pick_allocation_unit_once(
    *,
    allocation_id: int,
    item_scan: str,
    marking_scan: str = "",
    expiry_date_value: str = "",
    legacy_marking_absent: bool = False,
    performed_by,
) -> FbsPickVerificationProgress:
    _require_picking_writes()
    failure_stage = FbsPickScanEvent.STAGE_VERIFY_ITEM
    failure_scan_value = item_scan
    try:
        with transaction.atomic():
            allocation = (
                FbsOrderStockAllocation.objects.select_for_update(of=("self",))
                .select_related(
                    "pick_task__batch",
                    "pick_task__order",
                    "balance__box__pallet__cell",
                    "order_item__order__profile",
                    "order_item__sku",
                    "traceability",
                )
                .get(pk=allocation_id)
            )
            task = allocation.pick_task
            from .pick_restock import assert_no_active_pick_restock

            if task is not None:
                assert_no_active_pick_restock(task.batch_id, order_id=task.order_id)
            actor = _authenticated_user(performed_by)
            if (
                actor is None
                or task is None
                or task.batch.verification_assigned_to_id != actor.id
            ):
                raise FbsPickingError("Проверка волны назначена другому оператору.")
            if task.batch.status != FbsPickBatch.STATUS_VERIFICATION:
                raise FbsPickingError("Волна еще не передана на проверку.")
            if task.batch.picking_completed_at is None:
                raise FbsPickingError("Сборщик еще не сдал волну на рабочее место.")
            if task.status != FbsPickTask.STATUS_PICKED:
                raise FbsPickingError("Отбор заказа еще не завершен.")
            if allocation.status != FbsOrderStockAllocation.STATUS_PICKED:
                raise FbsPickingError("Позиция еще не отобрана полностью.")
            from fbs.models import FbsOrderLabel

            pending_label_exists = _pending_verified_order_label_exists(task.batch)
            if pending_label_exists:
                raise FbsPickingError(
                    "Сначала подтвердите marketplace-этикетку уже проверенного заказа."
                )
            expected_barcodes = _allocation_product_barcodes(allocation)
            matched_item_barcode, inferred_marking = resolve_verification_item_scan(
                allocation,
                item_scan,
            )
            if not expected_barcodes or not matched_item_barcode:
                scanned_display = str(item_scan or "").strip() or "пустой скан"
                expected_display = " или ".join(expected_barcodes) or "не задан"
                raise FbsPickingError(
                    f"Отсканирован ШК {scanned_display}. "
                    f"Ожидался ШК {expected_display}."
                )
            expected_barcode = " / ".join(expected_barcodes)
            if inferred_marking:
                if marking_scan and _canonicalize_kiz_scan(marking_scan) != inferred_marking:
                    raise FbsScanMismatchError(
                        "Отсканированы разные КИЗы на этапах товара и Честного знака. "
                        "Повторите сканирование нужного КИЗа."
                    )
                marking_scan = inferred_marking
            requirements = metadata_requirements(allocation.order_item)
            trace = allocation.traceability
            reserved_marking = str(allocation.balance.marking_code or "").strip()
            marketplace = task.order.profile.marketplace
            defer_wb_marking = marketplace == FbsIntegrationProfile.MARKETPLACE_WB
            supplied_wb_marking = bool(str(marking_scan or "").strip()) and defer_wb_marking
            marking_binding_message = ""
            validated_marking = ""
            marking_scan_required = controller_marking_scan_required(allocation)
            legacy_absence_accepted = False
            if legacy_marking_absent:
                failure_stage = FbsPickScanEvent.STAGE_VERIFY_MARKING
                failure_scan_value = ""
                if str(marking_scan or "").strip():
                    raise FbsPickingError(
                        "Выберите одно действие: отсканировать КИЗ или подтвердить "
                        "старую партию без физического ЧЗ."
                    )
                if not controller_legacy_marking_exception_allowed(allocation):
                    raise FbsPickingError(
                        "Пропуск ЧЗ запрещен: площадка требует КИЗ либо в остатке "
                        "уже указан физический код."
                    )
                legacy_absence_accepted = True
            elif marking_scan_required or supplied_wb_marking:
                failure_stage = FbsPickScanEvent.STAGE_VERIFY_MARKING
                failure_scan_value = marking_scan
                validated_marking = _validate_kiz_scan(
                    marking_scan,
                    product_barcodes=(
                        allocation.balance.barcode,
                        allocation.order_item.barcode,
                    ),
                )
            if validated_marking and defer_wb_marking:
                validated_marking = _validate_wb_pending_marking(
                    allocation=allocation,
                    marking_scan=validated_marking,
                )
                marking_binding_message = (
                    "КИЗ проверен. Привязка будет выполнена после сканирования "
                    "ШК заказа."
                )
            elif (
                validated_marking
                and marketplace == FbsIntegrationProfile.MARKETPLACE_OZON
            ):
                trace, reserved_marking, marking_binding_message = (
                    _bind_ozon_controller_marking_to_trace(
                        allocation=allocation,
                        marking_scan=validated_marking,
                    )
                )
            elif validated_marking and reserved_marking:
                trace, reserved_marking, marking_binding_message = _bind_wb_scanned_marking(
                    allocation=allocation,
                    marking_scan=validated_marking,
                    performed_by=performed_by,
                )
            elif validated_marking and requirements.marking_required:
                trace, reserved_marking, marking_binding_message = (
                    _bind_controller_marking_to_trace(
                        allocation=allocation,
                        marking_scan=validated_marking,
                    )
                )
            verified_marking = (
                validated_marking
                if defer_wb_marking and validated_marking
                else str(trace.marking_code or "").strip()
            )
            entered_expiry = str(expiry_date_value or "").strip()
            parsed_expiry = None
            if requirements.expiry_required or entered_expiry:
                failure_stage = FbsPickScanEvent.STAGE_VERIFY_EXPIRY
                failure_scan_value = entered_expiry
            if entered_expiry:
                try:
                    parsed_expiry = date.fromisoformat(entered_expiry)
                except ValueError as exc:
                    raise FbsPickingError("Укажите корректный срок годности.") from exc
            if requirements.expiry_required and trace.expiry_date is None:
                if parsed_expiry is None:
                    raise FbsPickingError("Укажите обязательный срок годности.")
                if parsed_expiry < timezone.localdate():
                    raise FbsPickingError("Срок годности уже истек.")
                minimum_expiry = _minimum_expiry(allocation.order_item)
                if minimum_expiry is not None and parsed_expiry < minimum_expiry:
                    raise FbsPickingError(
                        f"Срок годности должен быть не ранее {minimum_expiry:%d.%m.%Y}."
                    )
                trace.expiry_date = parsed_expiry
                trace.save(update_fields=["expiry_date", "updated_at"])
            elif parsed_expiry is not None and trace.expiry_date != parsed_expiry:
                raise FbsPickingError("Срок годности не совпадает с FBS-партией.")
            failure_stage = FbsPickScanEvent.STAGE_VERIFY_ITEM
            failure_scan_value = item_scan
            progress, _ = FbsPickVerificationProgress.objects.select_for_update().get_or_create(
                allocation=allocation
            )
            if int(progress.qty_verified or 0) >= int(allocation.qty_picked or 0):
                raise FbsPickingError("Все единицы этой позиции уже проверены.")
            now = timezone.now()
            progress.qty_verified = int(progress.qty_verified or 0) + 1
            progress.verified_by = actor
            progress.started_at = progress.started_at or now
            if progress.qty_verified == allocation.qty_picked:
                progress.completed_at = now
            progress.full_clean()
            progress.save(
                update_fields=[
                    "qty_verified",
                    "verified_by",
                    "started_at",
                    "completed_at",
                    "updated_at",
                ]
            )
            record_pick_scan_event(
                **_scan_event_context(allocation),
                stage=FbsPickScanEvent.STAGE_VERIFY_ITEM,
                result=FbsPickScanEvent.RESULT_SUCCESS,
                scan_value=item_scan,
                expected_value=expected_barcode,
                quantity_after=progress.qty_verified,
                message=(
                    "Штрихкод товара и КИЗ проверены."
                    if verified_marking
                    else (
                        "Штрихкод товара проверен; отсутствие ЧЗ старой партии "
                        "подтверждено контролером."
                        if legacy_absence_accepted
                        else "Штрихкод товара проверен."
                    )
                ),
                created_by=performed_by,
            )
            if verified_marking:
                record_pick_scan_event(
                    **_scan_event_context(allocation),
                    stage=FbsPickScanEvent.STAGE_VERIFY_MARKING,
                    result=FbsPickScanEvent.RESULT_SUCCESS,
                    scan_value=validated_marking,
                    expected_value=(
                        verified_marking
                        if defer_wb_marking
                        else reserved_marking or verified_marking
                    ),
                    quantity_after=progress.qty_verified,
                    message=marking_binding_message or "КИЗ единицы проверен.",
                    created_by=performed_by,
                )
            elif legacy_absence_accepted:
                record_pick_scan_event(
                    **_scan_event_context(allocation),
                    stage=FbsPickScanEvent.STAGE_VERIFY_MARKING,
                    result=FbsPickScanEvent.RESULT_SUCCESS,
                    scan_value="",
                    expected_value=LEGACY_MARKING_ABSENCE_EXPECTED_VALUE,
                    quantity_after=progress.qty_verified,
                    message=(
                        "Контролер подтвердил отсутствие физического ЧЗ: партия "
                        "введена до 01.10.2025. КИЗ маркетплейсу не передается."
                    ),
                    created_by=performed_by,
                )
            if requirements.expiry_required:
                minimum_expiry = _minimum_expiry(allocation.order_item)
                record_pick_scan_event(
                    **_scan_event_context(allocation),
                    stage=FbsPickScanEvent.STAGE_VERIFY_EXPIRY,
                    result=FbsPickScanEvent.RESULT_SUCCESS,
                    scan_value=trace.expiry_date.isoformat() if trace.expiry_date else "",
                    expected_value=minimum_expiry.isoformat() if minimum_expiry else "",
                    quantity_after=progress.qty_verified,
                    message="Срок годности подтвержден контролером.",
                    created_by=performed_by,
                )
            # This gate used to look only at the current wave's task.  An order
            # whose positions are split across waves -- a re-wave after an
            # invalid KIZ, say -- could therefore reach the label request while
            # positions picked by another wave were still unverified.
            # ``ensure_order_label_request`` catches that case today, but the
            # gate must not lean on the layer below it.  Require both: this task
            # finished, and every live allocation of the order finished,
            # whichever wave picked it.  Released and canceled allocations are
            # left out exactly as the label guard leaves them out, so a re-wave
            # is not blocked by the reservation it replaced.
            unverified_units = Q(verification_progress__isnull=True) | Q(
                verification_progress__qty_verified__lt=F("qty_picked")
            )
            order_has_unverified = (
                FbsOrderStockAllocation.objects.filter(pick_task=task)
                .filter(unverified_units)
                .exists()
                or FbsOrderStockAllocation.objects.filter(
                    order_item__order_id=task.order_id,
                    pick_task__isnull=False,
                )
                .exclude(
                    status__in=(
                        FbsOrderStockAllocation.STATUS_RELEASED,
                        FbsOrderStockAllocation.STATUS_CANCELED,
                    )
                )
                .filter(unverified_units)
                .exists()
            )
            if not order_has_unverified:
                # A normal freshly checked order cannot have a handover yet.
                # Avoid locking the shared controller tote on every scan while
                # the marketplace worker assigns the previous order. Re-wave
                # orders with an existing assignment still use the alignment
                # guard before any background work can continue.
                if FbsHandoverOrderAssignment.objects.filter(
                    order_id=task.order_id
                ).exists():
                    from .totes import align_controller_pick_tote_to_existing_handover

                    align_controller_pick_tote_to_existing_handover(
                        batch_id=task.batch_id,
                        order_id=task.order_id,
                        performed_by=actor,
                    )
                batch_has_unverified = FbsOrderStockAllocation.objects.filter(
                    pick_task__batch_id=task.batch_id,
                    qty_picked__gt=0,
                ).filter(
                    Q(verification_progress__isnull=True)
                    | Q(verification_progress__qty_verified__lt=F("qty_picked"))
                ).exists()
                if not batch_has_unverified:
                    release_pick_cart_if_verification_complete(batch_id=task.batch_id)
                from .labels import ensure_order_label_request

                if task.order.profile.marketplace != "wb":
                    prepare_order_marketplace_metadata(
                        order_id=task.order_id,
                        pick_task_id=task.id,
                    )
                ensure_order_label_request(order_id=task.order_id, requested_by=actor)
            return progress
    except FbsError as exc:
        _record_failed_allocation_scan(
            allocation_id=allocation_id,
            stage=failure_stage,
            scan_value=failure_scan_value,
            error=exc,
            performed_by=performed_by,
        )
        raise


def verify_pick_allocation_unit(
    *,
    allocation_id: int,
    item_scan: str,
    marking_scan: str = "",
    expiry_date_value: str = "",
    legacy_marking_absent: bool = False,
    performed_by,
) -> FbsPickVerificationProgress:
    """Verify one unit and replay only a fully rolled-back deadlock victim."""
    for attempt in range(PICK_DEADLOCK_RETRY_ATTEMPTS):
        try:
            return _verify_pick_allocation_unit_once(
                allocation_id=allocation_id,
                item_scan=item_scan,
                marking_scan=marking_scan,
                expiry_date_value=expiry_date_value,
                legacy_marking_absent=legacy_marking_absent,
                performed_by=performed_by,
            )
        except OperationalError as exc:
            if not _is_deadlock_error(exc):
                raise
            if attempt + 1 < PICK_DEADLOCK_RETRY_ATTEMPTS:
                time.sleep(PICK_DEADLOCK_RETRY_DELAY_SECONDS * (attempt + 1))
                continue
            error = FbsPickingError(
                "Система параллельно обновляет заказ. "
                "Операция не проведена; повторите сканирование."
            )
            _record_failed_allocation_scan(
                allocation_id=allocation_id,
                stage=FbsPickScanEvent.STAGE_VERIFY_ITEM,
                scan_value=item_scan,
                error=error,
                performed_by=performed_by,
            )
            raise error from exc
    raise AssertionError("Недостижимое состояние повтора FBS-проверки.")


@transaction.atomic
def release_pick_cart_if_verification_complete(*, batch_id: int) -> bool:
    """Legacy release; new controller flow waits for explicit empty confirmation."""
    batch = FbsPickBatch.objects.select_for_update().get(pk=batch_id)
    if batch.cart_released_at is not None:
        return False
    if (
        batch.status != FbsPickBatch.STATUS_VERIFICATION
        or batch.cart_id is None
        or batch.picking_completed_at is None
        or batch.verification_started_at is None
    ):
        return False
    if batch.tasks.filter(
        status__in=(FbsPickTask.STATUS_QUEUED, FbsPickTask.STATUS_IN_PROGRESS)
    ).exists():
        return False
    allocations = FbsOrderStockAllocation.objects.filter(
        pick_task__batch_id=batch.id,
        qty_picked__gt=0,
    )
    if not allocations.exists():
        return False
    if allocations.filter(
        Q(verification_progress__isnull=True)
        | Q(verification_progress__qty_verified__lt=F("qty_picked"))
    ).exists():
        return False
    from fbs.models import FbsControllerPickTote
    from .totes import mark_pick_tote_awaiting_empty

    controller_flow = FbsControllerPickTote.objects.filter(
        pick_batch_id=batch.id
    ).exists()
    if controller_flow:
        mark_pick_tote_awaiting_empty(pick_batch_id=batch.id)
        return False
    batch.cart_released_at = timezone.now()
    batch.save(update_fields=["cart_released_at", "updated_at"])
    return True


def _release_pick_tote_after_batch_done(batch: FbsPickBatch) -> None:
    """Освободить тару подбора сразу после закрытия волны.

    Волна закрывается разными путями — обычной проверкой, выводом заказа через
    «Проблемный товар», отменой. Раньше тару пересматривал только скан последней
    этикетки, поэтому на остальных путях тележка навсегда оставалась числиться
    у контролера.
    """
    from .totes import auto_release_pick_tote_if_complete

    auto_release_pick_tote_if_complete(pick_batch_id=batch.id)


def _preconfirmed_ozon_controller_order_ids(
    *, batch: FbsPickBatch, order_ids: list[int]
) -> set[int]:
    """Return Ozon orders whose preloaded QR completed the controller step.

    Ozon's official package label can arrive later, after marketplace marking
    checks.  The controller step is complete once the current verification
    scanned the exact preloaded order barcode and put that order in its check
    tote.  Shipment readiness remains guarded separately by the official label
    and marketplace metadata transfer statuses.
    """
    if not order_ids or batch.verification_started_at is None:
        return set()

    successful_scans: dict[int, set[tuple[str, str]]] = defaultdict(set)
    for order_id, scan_value, expected_value in FbsPickScanEvent.objects.filter(
        batch=batch,
        task__order_id__in=order_ids,
        stage=FbsPickScanEvent.STAGE_ORDER_LABEL,
        result=FbsPickScanEvent.RESULT_SUCCESS,
        created_at__gte=batch.verification_started_at,
    ).values_list("task__order_id", "scan_value", "expected_value"):
        successful_scans[int(order_id)].add(
            (
                str(scan_value or "").strip(),
                str(expected_value or "").strip(),
            )
        )

    if not successful_scans:
        return set()

    from .labels import is_preloaded_ozon_order_label

    completed_order_ids: set[int] = set()
    tote_orders = (
        FbsControllerToteOrder.objects.filter(
            pick_tote__pick_batch=batch,
            order_id__in=successful_scans,
            status__in=(
                FbsControllerToteOrder.STATUS_LABELED,
                FbsControllerToteOrder.STATUS_COMPOSITION,
                FbsControllerToteOrder.STATUS_PACKED,
            ),
        )
        .select_related("label")
        .order_by("id")
    )
    for tote_order in tote_orders:
        label = tote_order.label
        if not is_preloaded_ozon_order_label(label):
            continue
        barcode = str(label.barcode or "").strip()
        if (barcode, barcode) in successful_scans.get(tote_order.order_id, set()):
            completed_order_ids.add(tote_order.order_id)
    return completed_order_ids


@transaction.atomic
def refresh_pick_batch_verification(*, batch_id: int) -> FbsPickBatch:
    batch = FbsPickBatch.objects.select_for_update().get(pk=batch_id)
    if batch.status == FbsPickBatch.STATUS_DONE:
        return batch
    if batch.status != FbsPickBatch.STATUS_VERIFICATION:
        return batch
    if batch.tasks.filter(
        status__in=(FbsPickTask.STATUS_QUEUED, FbsPickTask.STATUS_IN_PROGRESS)
    ).exists():
        return batch
    successful_tasks = batch.tasks.filter(
        status=FbsPickTask.STATUS_PICKED
    ).exclude(
        order__pick_restock_requests__status__in=SEPARATED_PICK_RESTOCK_STATUSES
    )
    order_ids = list(successful_tasks.values_list("order_id", flat=True))
    if not order_ids:
        batch.status = FbsPickBatch.STATUS_DONE
        batch.completed_at = timezone.now()
        batch.save(update_fields=["status", "completed_at", "updated_at"])
        _release_pick_tote_after_batch_done(batch)
        return batch
    batch_allocations = FbsOrderStockAllocation.objects.filter(pick_task__in=successful_tasks)
    if batch_allocations.exclude(status=FbsOrderStockAllocation.STATUS_PICKED).exists():
        return batch
    if batch_allocations.filter(
        Q(verification_progress__isnull=True)
        | Q(verification_progress__qty_verified__lt=F("qty_picked"))
    ).exists():
        return batch
    applied_order_ids = set(
        FbsOrderLabel.objects.filter(
            order_id__in=order_ids,
            status=FbsOrderLabel.STATUS_APPLIED,
        ).values_list("order_id", flat=True)
    )
    controller_completed_order_ids = applied_order_ids | (
        _preconfirmed_ozon_controller_order_ids(
            batch=batch,
            order_ids=order_ids,
        )
    )
    if controller_completed_order_ids != set(order_ids):
        return batch
    # Marketplace label/marking confirmation is deliberately not a controller
    # completion gate.  The check-tote and handover services keep those errors
    # visible and block shipment until they are resolved.
    batch.status = FbsPickBatch.STATUS_DONE
    batch.completed_at = timezone.now()
    batch.save(update_fields=["status", "completed_at", "updated_at"])
    _release_pick_tote_after_batch_done(batch)
    return batch


@transaction.atomic
def release_pick_allocation_shortage(
    *,
    allocation_id: int,
    released_by=None,
    missing_qty: int | None = None,
    return_to_available: bool = True,
) -> FbsOrderStockAllocation:
    """Release an exact unpicked quantity while preserving picked units."""
    _require_picking_writes()
    allocation = (
        FbsOrderStockAllocation.objects.select_for_update(of=("self",))
        .select_related("order_item", "balance", "pick_task")
        .get(pk=allocation_id)
    )
    if allocation.pick_task_id is None:
        raise FbsPickingError("Резерв не включен в задание отбора.")
    task = (
        FbsPickTask.objects.select_for_update()
        .select_related("order", "batch")
        .get(pk=allocation.pick_task_id)
    )
    allocation.pick_task = task
    _assert_task_actor(task, released_by)
    if task.status != FbsPickTask.STATUS_IN_PROGRESS:
        raise FbsPickingError("Позиция не находится в активном задании.")
    if allocation.status not in ACTIVE_ALLOCATION_STATUSES:
        raise FbsPickingError("У позиции нет освобождаемого резерва.")

    remaining = int(allocation.qty_reserved or 0) - int(allocation.qty_picked or 0)
    if remaining <= 0:
        raise FbsPickingError("У позиции нет освобождаемого резерва.")
    release_qty = remaining if missing_qty is None else int(missing_qty or 0)
    if release_qty <= 0 or release_qty > remaining:
        raise FbsPickingError(
            f"Укажите отсутствующее количество от 1 до {remaining} шт."
        )
    balance = FbsStockBalance.objects.select_for_update().get(pk=allocation.balance_id)
    if int(balance.reserved_qty or 0) < release_qty:
        raise FbsPickingError("Резерв FBS-остатка поврежден.")

    now = timezone.now()
    actor = _authenticated_user(released_by)
    if return_to_available:
        balance.available_qty = int(balance.available_qty or 0) + release_qty
    balance.reserved_qty = int(balance.reserved_qty or 0) - release_qty
    balance.save(update_fields=["available_qty", "reserved_qty", "updated_at"])

    picked_qty = int(allocation.qty_picked or 0)
    retained_qty = int(allocation.qty_reserved or 0) - release_qty
    if retained_qty:
        trace = FbsOrderTraceability.objects.select_for_update().get(
            allocation=allocation
        )
        allocation.qty_reserved = retained_qty
        if retained_qty == picked_qty:
            allocation.status = FbsOrderStockAllocation.STATUS_PICKED
            allocation.picked_at = allocation.picked_at or now
        allocation.save(
            update_fields=["qty_reserved", "status", "picked_at", "updated_at"]
        )
        trace.qty = retained_qty
        trace.status = (
            FbsOrderTraceability.STATUS_PICKED
            if allocation.status == FbsOrderStockAllocation.STATUS_PICKED
            else FbsOrderTraceability.STATUS_RESERVED
        )
        trace.save(update_fields=["qty", "status", "updated_at"])
        shortage = FbsOrderStockAllocation.objects.create(
            order_item_id=allocation.order_item_id,
            balance=balance,
            pick_task=task,
            reserved_by_id=allocation.reserved_by_id,
            qty_reserved=release_qty,
            qty_picked=0,
            status=FbsOrderStockAllocation.STATUS_RELEASED,
            released_by=actor,
            released_at=now,
        )
        FbsOrderTraceability.objects.create(
            allocation=shortage,
            marking_code=trace.marking_code,
            lot_code=trace.lot_code,
            expiry_date=trace.expiry_date,
            qty=release_qty,
            status=FbsOrderTraceability.STATUS_RELEASED,
        )
    else:
        shortage = allocation
        shortage.status = FbsOrderStockAllocation.STATUS_RELEASED
        shortage.released_by = actor
        shortage.released_at = now
        shortage.save(
            update_fields=["status", "released_by", "released_at", "updated_at"]
        )
        set_allocation_trace_status(shortage, FbsOrderTraceability.STATUS_RELEASED)

    _finalize_pick_task_progress(
        task=task,
        actor=actor,
        picked_qty_delta=0,
        planned_qty_reduction=release_qty,
        signal_sender=release_pick_allocation_shortage,
        now=now,
    )
    if return_to_available:
        _queue_agency_stock_exports_on_commit(agency_id=balance.agency_id)
    return shortage


@transaction.atomic
def release_order_reservation(
    *,
    order_id: int,
    released_by=None,
    cancel_order: bool = False,
    unavailable_qty_by_allocation: dict[int, int] | None = None,
) -> FbsOrder:
    _require_picking_writes()
    order = FbsOrder.objects.select_for_update().get(pk=order_id)
    if order.internal_status not in {
        FbsOrder.STATUS_RESERVED,
        FbsOrder.STATUS_QUEUED_FOR_PICK,
        FbsOrder.STATUS_PICKING,
    }:
        raise FbsPickingError("У заказа нет освобождаемого резерва.")
    allocations = list(
        FbsOrderStockAllocation.objects.select_for_update(of=("self",))
        .select_related("balance", "pick_task__batch")
        .filter(order_item__order=order, status__in=ACTIVE_ALLOCATION_STATUSES)
        .order_by("id")
    )
    if not allocations:
        raise FbsPickingError("У заказа нет освобождаемого резерва.")
    if any(allocation.qty_picked for allocation in allocations):
        raise FbsPickingError("Нельзя освободить резерв после начала физического отбора.")
    unavailable_qty_by_allocation = {
        int(allocation_id): int(quantity or 0)
        for allocation_id, quantity in (unavailable_qty_by_allocation or {}).items()
    }
    unknown_allocation_ids = set(unavailable_qty_by_allocation).difference(
        allocation.id for allocation in allocations
    )
    if unknown_allocation_ids:
        raise FbsPickingError("Отсутствующая позиция не относится к этому заказу.")
    for allocation in allocations:
        remaining = int(allocation.qty_reserved or 0) - int(allocation.qty_picked or 0)
        unavailable_qty = unavailable_qty_by_allocation.get(allocation.id, 0)
        if unavailable_qty < 0 or unavailable_qty > remaining:
            raise FbsPickingError("Некорректное отсутствующее количество позиции.")
    now = timezone.now()
    actor = _authenticated_user(released_by)
    task_ids = set()
    batch_ids = set()
    returned_to_available_qty = 0
    for allocation in allocations:
        remaining = int(allocation.qty_reserved or 0) - int(allocation.qty_picked or 0)
        balance = FbsStockBalance.objects.select_for_update().get(pk=allocation.balance_id)
        if int(balance.reserved_qty or 0) < remaining:
            raise FbsPickingError("Резерв FBS-остатка поврежден.")
        unavailable_qty = unavailable_qty_by_allocation.get(allocation.id, 0)
        balance.available_qty = (
            int(balance.available_qty or 0) + remaining - unavailable_qty
        )
        returned_to_available_qty += remaining - unavailable_qty
        balance.reserved_qty = int(balance.reserved_qty or 0) - remaining
        balance.save(update_fields=["available_qty", "reserved_qty", "updated_at"])
        allocation.status = (
            FbsOrderStockAllocation.STATUS_CANCELED
            if cancel_order
            else FbsOrderStockAllocation.STATUS_RELEASED
        )
        allocation.released_by = actor
        allocation.released_at = now
        allocation.save(
            update_fields=["status", "released_by", "released_at", "updated_at"]
        )
        trace_status = (
            FbsOrderTraceability.STATUS_CANCELED
            if cancel_order
            else FbsOrderTraceability.STATUS_RELEASED
        )
        set_allocation_trace_status(allocation, trace_status)
        if allocation.pick_task_id:
            task_ids.add(allocation.pick_task_id)
            batch_ids.add(allocation.pick_task.batch_id)
    FbsPickTask.objects.filter(id__in=task_ids).update(
        status=FbsPickTask.STATUS_CANCELED,
        canceled_at=now,
        updated_at=now,
    )
    verification_batch_ids = []
    for batch_id in sorted(batch_ids):
        batch = FbsPickBatch.objects.select_for_update().get(pk=batch_id)
        batch_tasks = list(
            FbsPickTask.objects.select_for_update()
            .filter(batch_id=batch_id)
            .order_by("sort_order", "id")
        )
        canceled_picked_qty = sum(
            int(task.picked_qty or 0)
            for task in batch_tasks
            if task.id in task_ids
        )
        batch.picked_qty = max(
            0,
            int(batch.picked_qty or 0) - canceled_picked_qty,
        )
        active_planned_qty = _active_pick_tasks_planned_qty(batch_tasks)
        if active_planned_qty < int(batch.picked_qty or 0):
            raise FbsPickingError("Состав волны FBS поврежден.")
        batch.planned_qty = active_planned_qty
        update_fields = ["planned_qty", "picked_qty", "updated_at"]
        has_active_tasks = any(
            task.status in ACTIVE_TASK_STATUSES for task in batch_tasks
        )
        has_picked_tasks = any(
            task.status == FbsPickTask.STATUS_PICKED for task in batch_tasks
        )
        remaining_tasks = [
            task
            for task in batch_tasks
            if task.status != FbsPickTask.STATUS_CANCELED
        ]
        route_completed = bool(remaining_tasks) and all(
            task.status == FbsPickTask.STATUS_PICKED for task in remaining_tasks
        ) and int(batch.picked_qty or 0) == active_planned_qty
        if route_completed:
            batch.status = FbsPickBatch.STATUS_VERIFICATION
            batch.completed_at = None
            batch.canceled_at = None
            update_fields.extend(["status", "completed_at", "canceled_at"])
            verification_batch_ids.append(batch.id)
        elif not has_active_tasks and not has_picked_tasks:
            batch.status = FbsPickBatch.STATUS_CANCELED
            batch.canceled_at = now
            update_fields.extend(["status", "canceled_at"])
        batch.save(update_fields=update_fields)
    for batch_id in verification_batch_ids:
        assign_pick_handover_workstation(batch_id=batch_id)
    order.internal_status = (
        FbsOrder.STATUS_CANCELLED if cancel_order else FbsOrder.STATUS_AWAITING_STOCK
    )
    order.save(update_fields=["internal_status", "updated_at"])
    if returned_to_available_qty > 0:
        _queue_agency_stock_exports_on_commit(
            agency_id=allocations[0].balance.agency_id
        )
    return order


def release_non_first_tier_reservations(*, limit: int = 200, released_by=None) -> int:
    """Return legacy unstarted reservations to the queue before a scheduled wave.

    The operation repairs both unbatched reservations and queued waves that have
    not physically started.  Any in-progress/picked task is intentionally left
    untouched.  The ordinary reservation pass can then either reserve the order
    from tier 1 or leave it awaiting replenishment.
    """
    _require_picking_writes()
    if limit <= 0:
        return 0
    unsafe_task = FbsPickTask.objects.filter(
        order_id=OuterRef("pk"),
        status__in=(
            FbsPickTask.STATUS_IN_PROGRESS,
            FbsPickTask.STATUS_PICKED,
            FbsPickTask.STATUS_EXCEPTION,
        ),
    )
    upper_reservation = FbsOrderStockAllocation.objects.filter(
        order_item__order_id=OuterRef("pk"),
        status=FbsOrderStockAllocation.STATUS_RESERVED,
    ).exclude(
        balance__box__pallet__cell__location__tier_no=FIRST_PICK_TIER,
    )
    repair_orders = (
        FbsOrder.objects.filter(
            internal_status__in=(
                FbsOrder.STATUS_RESERVED,
                FbsOrder.STATUS_QUEUED_FOR_PICK,
            ),
        )
        .annotate(
            _has_unsafe_pick_task=Exists(unsafe_task),
            _has_upper_reservation=Exists(upper_reservation),
        )
        .filter(_has_unsafe_pick_task=False, _has_upper_reservation=True)
    )
    order_ids = list(
        order_by_pick_priority(repair_orders)
        .values_list("id", flat=True)[:limit]
    )
    released = 0
    for order_id in order_ids:
        release_order_reservation(order_id=order_id, released_by=released_by)
        released += 1
    return released
