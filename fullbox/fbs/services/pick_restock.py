from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from fbs.exceptions import (
    FbsError,
    FbsFeatureDisabled,
    FbsPickingError,
    FbsScanMismatchError,
)
from fbs.flags import feature_enabled
from fbs.integrations.contracts import (
    OZON_SHIP_POSTING,
    WB_ADD_ORDER_TO_HANDOVER,
    WB_CANCEL_ORDER,
    WB_READ_HANDOVER_ORDER_IDS,
    WB_READ_ORDER_METADATA,
    WB_SET_ORDER_SGTINS,
)
from fbs.models import (
    FbsBox,
    FbsControllerCheckTote,
    FbsControllerToteOrder,
    FbsHandoverBatch,
    FbsHandoverOrder,
    FbsHandoverOrderAssignment,
    FbsIntegrationProfile,
    FbsMarketplaceCommand,
    FbsMarketplaceMetadataTransfer,
    FbsOrder,
    FbsOrderLabel,
    FbsOrderStockAllocation,
    FbsOrderTraceability,
    FbsPickBatch,
    FbsPickException,
    FbsPickingCart,
    FbsProblemToteItem,
    FbsPickRestockLine,
    FbsPickRestockRequest,
    FbsPickRestockScan,
    FbsPickTask,
    FbsPickVerificationProgress,
    FbsStockBalance,
    FbsToteBinding,
    FbsToteMovement,
)
from fbs.order_audit import log_order_bulk_transition
from sklad.models import WarehouseContainer, WarehouseEvent
from sklad.services.operational_locations import normalize_operational_location_scan
from sklad.topology import os_location_code

from .physical_locations import (
    fbs_box_physical_location,
    fbs_box_physical_location_code,
    fbs_box_physical_location_label,
    fbs_box_physical_location_scan_values,
    is_virtual_fbs_plan_location,
)


ACTIVE_PICK_RESTOCK_STATUSES = (
    FbsPickRestockRequest.STATUS_QUEUED,
    FbsPickRestockRequest.STATUS_IN_PROGRESS,
)
BLOCKING_PICK_RESTOCK_STATUSES = (
    FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
    FbsPickRestockRequest.STATUS_QUEUED,
    FbsPickRestockRequest.STATUS_IN_PROGRESS,
    FbsPickRestockRequest.STATUS_FAILED,
)
HANDOVER_BLOCKING_PICK_RESTOCK_STATUSES = (
    FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
    FbsPickRestockRequest.STATUS_FAILED,
)
PICK_RESTOCK_STALE_CLAIM_AFTER = timedelta(hours=1)
WB_CLIENT_CANCEL_STATUSES = {
    "cancel",
    "canceled",
    "cancelled",
    "cancel_by_client",
    "cancel_missed_call",
    "canceled_by_client",
    "canceled_by_missed_call",
    "decline",
    "declined_by_client",
    "defect",
}


def order_is_client_canceled_by_marketplace(order: FbsOrder) -> bool:
    """Return true only when the marketplace already reports client cancellation."""
    statuses = {
        str(order.marketplace_status or "").strip().casefold(),
        str(order.marketplace_substatus or "").strip().casefold(),
    }
    if order.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        return any("cancel" in status for status in statuses if status)
    if order.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        return bool(statuses & WB_CLIENT_CANCEL_STATUSES)
    return False

INVALID_KIZ_REROUTE_RE = re.compile(
    r":invalid-kiz-route:(rewave|quarantine):source-(\d+):order-(\d+)$"
)


@dataclass(frozen=True)
class FbsInvalidKizRerouteResult:
    order_id: int
    source_batch_id: int
    target_batch_id: int
    route: str
    status: str
    pick_batch_id: int | None = None


@dataclass(frozen=True)
class FbsNotFoundRequeueResult:
    request_id: int
    order_id: int
    status: str
    reserved: bool
    pick_batch_id: int | None = None


def _append_to_open_not_found_rewave(
    *,
    request: FbsPickRestockRequest,
    order: FbsOrder,
    reservation,
) -> FbsPickBatch | None:
    """Append a replacement order to a pristine replacement wave for this client."""
    planned_qty = int(reservation.reserved_qty or 0)
    if planned_qty <= 0:
        return None
    candidates = (
        FbsPickBatch.objects.select_for_update()
        .filter(
            agency_id=order.profile.agency_id,
            status=FbsPickBatch.STATUS_QUEUED,
            assigned_to__isnull=True,
            workstation__isnull=True,
            cart__isnull=True,
            claimed_at__isnull=True,
            started_at__isnull=True,
            picking_completed_at__isnull=True,
            verification_started_at__isnull=True,
            completed_at__isnull=True,
            canceled_at__isnull=True,
        )
        .exclude(pk=request.batch_id)
        .order_by("created_at", "id")
    )
    for batch in candidates:
        tasks = list(
            FbsPickTask.objects.select_for_update()
            .filter(batch=batch)
            .order_by("sort_order", "id")
        )
        if not tasks or len(tasks) >= 50:
            continue
        if any(task.status != FbsPickTask.STATUS_QUEUED for task in tasks):
            continue
        current_qty = sum(int(task.planned_qty or 0) for task in tasks)
        if current_qty + planned_qty > 100:
            continue
        task_order_ids = {task.order_id for task in tasks}
        rewave_order_ids = set(
            FbsPickRestockRequest.objects.filter(
                order_id__in=task_order_ids,
                reason_code=FbsPickRestockRequest.REASON_WRONG_PRODUCT,
                marketplace_action=FbsPickRestockRequest.MARKETPLACE_ACTION_NONE,
                status__in=(
                    FbsPickRestockRequest.STATUS_QUEUED,
                    FbsPickRestockRequest.STATUS_IN_PROGRESS,
                    FbsPickRestockRequest.STATUS_COMPLETED,
                ),
            )
            .exclude(batch_id=batch.id)
            .values_list("order_id", flat=True)
        )
        if rewave_order_ids != task_order_ids:
            continue

        allocation_ids = [allocation.id for allocation in reservation.allocations]
        allocations = list(
            FbsOrderStockAllocation.objects.select_for_update()
            .filter(
                id__in=allocation_ids,
                status=FbsOrderStockAllocation.STATUS_RESERVED,
                pick_task__isnull=True,
            )
            .order_by("id")
        )
        if (
            len(allocations) != len(allocation_ids)
            or sum(int(allocation.qty_reserved or 0) for allocation in allocations)
            != planned_qty
        ):
            raise FbsPickingError("Состав нового резерва изменился; повторите операцию.")
        task = FbsPickTask.objects.create(
            batch=batch,
            order=order,
            sort_order=max(int(item.sort_order or 0) for item in tasks) + 1,
            planned_qty=planned_qty,
        )
        for allocation in allocations:
            allocation.pick_task = task
        FbsOrderStockAllocation.objects.bulk_update(allocations, ["pick_task"])
        batch.planned_qty = current_qty + planned_qty
        batch.save(update_fields=["planned_qty", "updated_at"])
        order.internal_status = FbsOrder.STATUS_QUEUED_FOR_PICK
        order.updated_at = timezone.now()
        order.save(update_fields=["internal_status", "updated_at"])
        return batch
    return None


@transaction.atomic
def requeue_not_found_order(
    *, request_id: int, performed_by
) -> FbsNotFoundRequeueResult:
    """Reserve a replacement unit without mixing it with the old physical return."""
    _require_writes()
    actor = _actor(performed_by)
    request = (
        FbsPickRestockRequest.objects.select_for_update(of=("self",))
        .select_related("order__profile__agency")
        .get(pk=request_id)
    )
    if request.order_id is None or request.reason_code != FbsPickRestockRequest.REASON_WRONG_PRODUCT:
        raise FbsPickingError("Повторный подбор доступен только для заказа «Товар не найден».")
    if request.marketplace_action != FbsPickRestockRequest.MARKETPLACE_ACTION_NONE:
        raise FbsPickingError("Сначала дождитесь подтверждения маркетплейса.")
    if request.status not in {
        FbsPickRestockRequest.STATUS_QUEUED,
        FbsPickRestockRequest.STATUS_IN_PROGRESS,
        FbsPickRestockRequest.STATUS_COMPLETED,
    }:
        raise FbsPickingError("Возврат проблемного товара еще не готов к повторному подбору.")

    order = FbsOrder.objects.select_for_update().get(pk=request.order_id)
    replacement_task = (
        FbsPickTask.objects.select_related("batch")
        .filter(order=order)
        .exclude(batch_id=request.batch_id)
        .filter(
            status__in=(
                FbsPickTask.STATUS_QUEUED,
                FbsPickTask.STATUS_IN_PROGRESS,
                FbsPickTask.STATUS_PICKED,
            )
        )
        .order_by("-id")
        .first()
    )
    if replacement_task is not None:
        return FbsNotFoundRequeueResult(
            request_id=request.id,
            order_id=order.id,
            status=order.internal_status,
            reserved=True,
            pick_batch_id=replacement_task.batch_id,
        )

    previous_status = order.internal_status
    order.internal_status = FbsOrder.STATUS_AWAITING_STOCK
    order.hold_reason = "verification_not_found_rewave"
    order.problem_reason = (
        f"Повторный подбор после проверки: {request.reason}. "
        f"Проблемная единица возвращается отдельно по заданию #{request.id}."
    )
    order.save(
        update_fields=["internal_status", "hold_reason", "problem_reason", "updated_at"]
    )

    from .picking import create_pick_batches, reserve_order_stock

    reservation = reserve_order_stock(order_id=order.id, reserved_by=actor)
    pick_batch_id = None
    if reservation.reserved:
        batch = _append_to_open_not_found_rewave(
            request=request,
            order=order,
            reservation=reservation,
        )
        if batch is None:
            batches = create_pick_batches(
                agency=order.profile.agency,
                order_ids=[order.id],
                created_by=actor,
            )
            if not batches:
                raise FbsPickingError("Не удалось создать новую волну повторного подбора.")
            batch = batches[0]
        pick_batch_id = batch.id
    order.refresh_from_db()
    log_order_bulk_transition(
        [order],
        previous_internal_status={order.pk: previous_status},
        internal_status=order.internal_status,
        user=actor,
        source="verification_not_found_rewave",
        occurred_at=timezone.now(),
    )
    return FbsNotFoundRequeueResult(
        request_id=request.id,
        order_id=order.id,
        status=order.internal_status,
        reserved=bool(reservation.reserved),
        pick_batch_id=pick_batch_id,
    )


def invalid_kiz_reroute_context(batch: FbsHandoverBatch) -> dict[str, int | str] | None:
    match = INVALID_KIZ_REROUTE_RE.search(str(batch.compatibility_key or ""))
    if match is None:
        return None
    return {
        "route": match.group(1),
        "source_batch_id": int(match.group(2)),
        "order_id": int(match.group(3)),
    }


@dataclass(frozen=True)
class FbsPickRestockScanResult:
    request: FbsPickRestockRequest
    event: FbsPickRestockScan
    duplicate: bool = False


def _require_writes() -> None:
    if not feature_enabled("module"):
        raise FbsFeatureDisabled("Модуль FBS выключен.")
    if not feature_enabled("warehouse_writes"):
        raise FbsFeatureDisabled("Складские операции FBS выключены.")


def _actor(user):
    if not getattr(user, "is_authenticated", False):
        raise FbsPickingError("Для возврата отбора нужен авторизованный сотрудник.")
    return user


def _normalized(value) -> str:
    return str(value or "").strip().casefold()


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


def _cell_scan_candidates_for_cell(cell) -> set[str]:
    raw_candidates = {str(cell.cell_code or "")}
    location = getattr(cell, "location", None)
    if location is not None:
        raw_candidates.add(str(location.location_code or ""))
        if all(
            int(getattr(location, field, 0) or 0) > 0
            for field in ("row_no", "section_no", "tier_no", "cell_no")
        ):
            raw_candidates.add(
                os_location_code(
                    row=location.row_no,
                    section=location.section_no,
                    tier=location.tier_no,
                    cell=location.cell_no,
                )
            )
    return {_normalized_cell_scan(value) for value in raw_candidates if value}


def _physical_destination_for_box(box):
    location = fbs_box_physical_location(box)
    if location is None or is_virtual_fbs_plan_location(location):
        return None
    return location


def _cell_scan_candidates_for_box(box) -> set[str]:
    location = _physical_destination_for_box(box)
    if location is None:
        return set()
    raw_candidates = set(fbs_box_physical_location_scan_values(box))
    location_code = str(location.location_code or "").strip()
    zone_code = str(location.zone_code or "").strip()
    if location_code and _normalized_cell_scan(location_code) != _normalized_cell_scan(
        zone_code
    ):
        raw_candidates = {
            value
            for value in raw_candidates
            if _normalized_cell_scan(value) != _normalized_cell_scan(zone_code)
        }
    cell = box.pallet.cell
    if getattr(location, "pk", None) == getattr(cell, "location_id", None):
        raw_candidates.add(str(cell.cell_code or ""))
    return {_normalized_cell_scan(value) for value in raw_candidates if value}


def _cell_scan_candidates(line: FbsPickRestockLine) -> set[str]:
    return _cell_scan_candidates_for_box(line.source_box)


def _request_token(value) -> uuid.UUID:
    try:
        return uuid.UUID(str(value or ""))
    except (TypeError, ValueError, AttributeError) as exc:
        raise FbsPickingError("Повторите сканирование: защитный идентификатор устарел.") from exc


def _active_request_exists(batch_id: int, *, order_id: int | None = None) -> bool:
    requests = FbsPickRestockRequest.objects.filter(
        batch_id=batch_id,
        status__in=BLOCKING_PICK_RESTOCK_STATUSES,
    )
    if order_id is None:
        requests = requests.filter(order__isnull=True)
    else:
        requests = requests.filter(Q(order__isnull=True) | Q(order_id=order_id))
    return requests.exists()


def assert_no_active_pick_restock(
    batch_id: int, *, order_id: int | None = None
) -> None:
    if not _active_request_exists(batch_id, order_id=order_id):
        return
    if order_id is None:
        message = "По всей волне выполняется возврат ошибочного отбора."
    else:
        message = "Этот заказ исключен из проверки и передан в возврат отбора."
    raise FbsPickingError(message)


def _request_line_queryset(request_id: int):
    return (
        FbsPickRestockLine.objects.select_related(
            "request__batch",
            "allocation__pick_task__order__profile__agency",
            "allocation__order_item__sku",
            "source_balance",
            "source_box__pallet__cell__location",
            "source_box__source_container__current_location",
            "source_cell__location",
        )
        .filter(request_id=request_id)
        .order_by(
            "source_cell__location__row_no",
            "source_cell__location__section_no",
            "source_cell__location__tier_no",
            "source_cell__location__cell_no",
            "source_box__box_code",
            "id",
        )
    )


def _current_line_queryset(request_id: int):
    return _request_line_queryset(request_id).filter(returned_qty__lt=F("planned_qty"))


def _pickup_scan_state(request_id: int):
    latest_workstation_scan = (
        FbsPickRestockScan.objects.filter(
            request_id=request_id,
            stage=FbsPickRestockScan.STAGE_WORKSTATION,
            result=FbsPickRestockScan.RESULT_SUCCESS,
        )
        .order_by("-created_at", "-id")
        .first()
    )
    pickup_scans = FbsPickRestockScan.objects.none()
    if latest_workstation_scan is not None:
        # Controller verification can create pickup_item scans before the
        # return is handed to a picker. Only scans made after the picker has
        # confirmed the source tote/workstation belong to the current return.
        pickup_scans = FbsPickRestockScan.objects.filter(
            request_id=request_id,
            stage=FbsPickRestockScan.STAGE_PICKUP_ITEM,
            result=FbsPickRestockScan.RESULT_SUCCESS,
            line_id__isnull=False,
            created_at__gt=latest_workstation_scan.created_at,
        )
    scan_counts = {
        row["line_id"]: int(row["scanned"] or 0)
        for row in (
            pickup_scans.values("line_id").annotate(scanned=Count("id"))
        )
    }
    planned_total = 0
    scanned_total = 0
    pending_line = None
    pending_line_scanned = 0
    pending_line_planned = 0
    options = []
    for candidate in _current_line_queryset(request_id):
        planned = int(candidate.planned_qty or 0) - int(candidate.returned_qty or 0)
        scanned = min(int(scan_counts.get(candidate.id, 0)), planned)
        planned_total += planned
        scanned_total += scanned
        if scanned < planned:
            options.append(
                {
                    "line": candidate,
                    "scanned": scanned,
                    "planned": planned,
                    "remaining": planned - scanned,
                }
            )
        if pending_line is None and scanned < planned:
            pending_line = candidate
            pending_line_scanned = scanned
            pending_line_planned = planned
    return {
        "line": pending_line,
        "line_scanned": pending_line_scanned,
        "line_planned": pending_line_planned,
        "scanned": scanned_total,
        "planned": planned_total,
        "completed": pending_line is None,
        "options": options,
    }


def pick_restock_state(request_id: int) -> dict:
    request = FbsPickRestockRequest.objects.select_related(
        "batch__workstation",
        "assigned_to",
        "created_by",
        "source_tote__binding__workstation",
        "quarantine_box__pallet__cell__location",
        "quarantine_box__source_container__current_location",
    ).get(pk=request_id)
    source_workstation = request.batch.workstation
    destination_line = _current_line_queryset(request.id).first()
    if destination_line is None:
        return {
            "request": request,
            "line": None,
            "stage": "completed",
            "source_workstation": source_workstation,
            "source_tote": request.source_tote,
            "destination_box": request.quarantine_box,
            "destination_cell": (
                request.quarantine_box.pallet.cell if request.quarantine_box_id else None
            ),
            "is_quarantine": bool(request.quarantine_box_id),
            "workstation_confirmed": False,
            "pickup_confirmed": False,
            "pickup_scanned_qty": 0,
            "pickup_planned_qty": 0,
            "pickup_options": [],
            "box_planned_qty": 0,
            "box_returned_qty": 0,
        }
    pickup_state = _pickup_scan_state(request.id)
    pickup_line = pickup_state["line"]
    line = pickup_line or destination_line
    is_quarantine = bool(request.quarantine_box_id)
    destination_box = request.quarantine_box if is_quarantine else line.source_box
    from .restock_destinations import destination_event, replacement_box
    route_event = None if is_quarantine else destination_event(request.id, line.source_box_id)
    destination_box = replacement_box(route_event) or destination_box
    destination_cell = destination_box.pallet.cell
    destination_location = _physical_destination_for_box(destination_box)
    box_lines = (
        request.lines.all()
        if is_quarantine
        else request.lines.filter(source_box_id=line.source_box_id)
    )
    box_totals = box_lines.aggregate(
        planned=Sum("planned_qty"),
        returned=Sum("returned_qty"),
    )
    successful_box_scans = request.scans.filter(
        result=FbsPickRestockScan.RESULT_SUCCESS,
    )
    if not is_quarantine:
        successful_box_scans = successful_box_scans.filter(
            line__source_box_id=line.source_box_id
        )
    if route_event is not None:
        successful_box_scans = successful_box_scans.filter(created_at__gt=route_event.created_at)
    workstation_confirmed = request.scans.filter(
        stage=FbsPickRestockScan.STAGE_WORKSTATION,
        result=FbsPickRestockScan.RESULT_SUCCESS,
    ).exists()
    cell_confirmed = successful_box_scans.filter(
        stage=FbsPickRestockScan.STAGE_CELL
    ).exists()
    box_confirmed = successful_box_scans.filter(
        stage=FbsPickRestockScan.STAGE_BOX
    ).exists()
    stage = (
        FbsPickRestockScan.STAGE_WORKSTATION
        if not workstation_confirmed
        else FbsPickRestockScan.STAGE_PICKUP_ITEM
        if not pickup_state["completed"]
        else FbsPickRestockScan.STAGE_CELL
        if not cell_confirmed
        else FbsPickRestockScan.STAGE_BOX
        if not box_confirmed
        else FbsPickRestockScan.STAGE_ITEM
    )
    return {
        "request": request,
        "line": line,
        "stage": stage,
        "source_workstation": source_workstation,
        "source_tote": request.source_tote,
        "destination_box": destination_box,
        "destination_cell": destination_cell,
        "destination_location": destination_location,
        "destination_event_id": route_event.pk if route_event else None,
        "destination_location_code": (
            fbs_box_physical_location_code(destination_box)
            if destination_location is not None
            else ""
        ),
        "destination_location_label": (
            fbs_box_physical_location_label(destination_box)
            if destination_location is not None
            else "Физическое место не определено"
        ),
        "is_quarantine": is_quarantine,
        "workstation_confirmed": workstation_confirmed,
        "pickup_confirmed": pickup_state["completed"],
        "pickup_scanned_qty": pickup_state["scanned"],
        "pickup_planned_qty": pickup_state["planned"],
        "pickup_line_scanned_qty": pickup_state["line_scanned"],
        "pickup_line_planned_qty": pickup_state["line_planned"],
        "pickup_options": pickup_state["options"],
        "cell_confirmed": cell_confirmed,
        "box_confirmed": box_confirmed,
        "box_planned_qty": int(box_totals["planned"] or 0),
        "box_returned_qty": int(box_totals["returned"] or 0),
    }


@transaction.atomic
def create_pick_restock_request(
    *,
    batch_id: int,
    reason: str,
    created_by,
) -> FbsPickRestockRequest:
    _require_writes()
    actor = _actor(created_by)
    reason = str(reason or "").strip()
    if len(reason) < 10:
        raise FbsPickingError("Укажите причину возврата не короче 10 символов.")
    batch = (
        FbsPickBatch.objects.select_for_update()
        .select_related("agency")
        .get(pk=batch_id)
    )
    existing = (
        FbsPickRestockRequest.objects.select_for_update()
        .filter(batch=batch, order__isnull=True)
        .first()
    )
    if existing is not None:
        return existing
    if batch.status != FbsPickBatch.STATUS_VERIFICATION or batch.picking_completed_at is None:
        raise FbsPickingError("Возврат доступен только для полностью доставленной волны на проверке.")
    if int(batch.picked_qty or 0) <= 0:
        raise FbsPickingError("В волне нет физически отобранного товара.")
    if batch.tasks.exclude(status=FbsPickTask.STATUS_PICKED).exists():
        raise FbsPickingError("Не все задания волны завершены физическим отбором.")

    allocations = list(
        FbsOrderStockAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "pick_task__order__profile",
            "balance__box__pallet__cell",
        )
        .filter(
            pick_task__batch=batch,
            status=FbsOrderStockAllocation.STATUS_PICKED,
            qty_picked__gt=0,
        )
        .order_by("id")
    )
    planned_qty = sum(int(allocation.qty_picked or 0) for allocation in allocations)
    if not allocations or planned_qty != int(batch.picked_qty or 0):
        raise FbsPickingError("Факт отбора волны не совпадает с позициями. Нужна диагностика.")
    order_ids = sorted({allocation.pick_task.order_id for allocation in allocations})
    if FbsHandoverOrder.objects.filter(
        order_id__in=order_ids,
        status__in=(
            FbsHandoverOrder.STATUS_ACTIVE,
            FbsHandoverOrder.STATUS_RETURN_PENDING,
        ),
    ).exists():
        raise FbsPickingError("Один из заказов уже физически уложен в короб отгрузки.")
    if FbsOrderLabel.objects.filter(
        order_id__in=order_ids,
        status=FbsOrderLabel.STATUS_APPLIED,
    ).exists():
        raise FbsPickingError("Одна из этикеток уже подтверждена сканированием.")
    if FbsHandoverOrderAssignment.objects.filter(
        order_id__in=order_ids,
        status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
    ).exists():
        raise FbsPickingError("Один из заказов уже подтвержден в поставке маркетплейса.")
    if FbsMarketplaceMetadataTransfer.objects.filter(
        order_item__order_id__in=order_ids,
        status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED,
    ).exists():
        raise FbsPickingError("По одному из заказов КИЗ или срок уже подтвержден маркетплейсом.")
    if FbsMarketplaceCommand.objects.filter(
        order_id__in=order_ids,
        command_type__in=(WB_ADD_ORDER_TO_HANDOVER, OZON_SHIP_POSTING),
        status=FbsMarketplaceCommand.STATUS_CONFIRMED,
    ).exists():
        raise FbsPickingError("Один из заказов уже передан в поставку через API маркетплейса.")

    request = FbsPickRestockRequest.objects.create(
        batch=batch,
        reason=reason,
        planned_qty=planned_qty,
        created_by=actor,
    )
    FbsPickRestockLine.objects.bulk_create(
        [
            FbsPickRestockLine(
                request=request,
                allocation=allocation,
                source_balance=allocation.balance,
                source_box=allocation.balance.box,
                source_cell=allocation.balance.box.pallet.cell,
                planned_qty=int(allocation.qty_picked or 0),
            )
            for allocation in allocations
        ]
    )

    now = timezone.now()
    cancellation_note = f"Возврат ошибочного отбора #{request.id}: {reason}"
    FbsOrderLabel.objects.filter(order_id__in=order_ids).exclude(
        status=FbsOrderLabel.STATUS_APPLIED
    ).update(status=FbsOrderLabel.STATUS_CANCELED, error=cancellation_note, updated_at=now)
    FbsMarketplaceMetadataTransfer.objects.filter(
        order_item__order_id__in=order_ids
    ).exclude(status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED).update(
        status=FbsMarketplaceMetadataTransfer.STATUS_CANCELED,
        last_error=cancellation_note,
        updated_at=now,
    )
    FbsMarketplaceCommand.objects.filter(
        order_id__in=order_ids,
        status__in=(
            FbsMarketplaceCommand.STATUS_PENDING,
            FbsMarketplaceCommand.STATUS_SENT,
            FbsMarketplaceCommand.STATUS_RETRY,
        ),
    ).update(
        status=FbsMarketplaceCommand.STATUS_CANCELLED,
        next_attempt_at=None,
        error=cancellation_note,
        updated_at=now,
    )
    FbsHandoverOrderAssignment.objects.filter(order_id__in=order_ids).exclude(
        status=FbsHandoverOrderAssignment.STATUS_CONFIRMED
    ).update(
        status=FbsHandoverOrderAssignment.STATUS_CANCELED,
        error=cancellation_note,
        updated_at=now,
    )

    orders = list(
        FbsOrder.objects.select_for_update().select_related("profile__agency").filter(
            pk__in=order_ids
        )
    )
    previous_statuses = {order.pk: order.internal_status for order in orders}
    for order in orders:
        order.internal_status = FbsOrder.STATUS_EXCEPTION
        order.hold_reason = "pick_restock_pending"
        order.problem_reason = reason
        order.updated_at = now
    FbsOrder.objects.bulk_update(
        orders,
        ["internal_status", "hold_reason", "problem_reason", "updated_at"],
    )
    log_order_bulk_transition(
        orders,
        previous_internal_status=previous_statuses,
        internal_status=FbsOrder.STATUS_EXCEPTION,
        user=actor,
        source="create_pick_restock_request",
        occurred_at=now,
    )
    batch.verification_assigned_to = None
    batch.save(update_fields=["verification_assigned_to", "updated_at"])
    return request


def _order_restock_reason(reason_code: str, reason: str) -> tuple[str, str]:
    reason_code = str(reason_code or "").strip()
    labels = dict(FbsPickRestockRequest.REASON_CHOICES)
    if reason_code not in labels:
        raise FbsPickingError("Выберите причину исключения заказа.")
    reason = str(reason or "").strip()
    if reason_code == FbsPickRestockRequest.REASON_OTHER and not reason:
        raise FbsPickingError("Укажите комментарий: что произошло с заказом.")
    if not reason:
        reason = labels[reason_code]
    return reason_code, reason


@transaction.atomic
def request_invalid_kiz_reroute(
    *,
    handover_batch_id: int,
    order_id: int,
    route: str,
    problem_tote_scan: str,
    requested_by,
) -> FbsInvalidKizRerouteResult:
    """Move an active WB order to a safe supply without seller cancellation.

    The source shipment remains blocked by a ``return_pending`` physical link
    until WB confirms that the order is present in the replacement supply.
    """
    _require_writes()
    actor = _actor(requested_by)
    route = str(route or "").strip().casefold()
    if route not in {"rewave", "quarantine"}:
        raise FbsPickingError("Выберите: вернуть заказ в новую волну или в карантин.")

    source_batch = (
        FbsHandoverBatch.objects.select_for_update()
        .select_related("profile")
        .get(pk=handover_batch_id)
    )
    if source_batch.profile.marketplace != "wb":
        raise FbsPickingError("Перенос проблемного КИЗа доступен только для WB.")
    if source_batch.status not in {
        FbsHandoverBatch.STATUS_OPEN,
        FbsHandoverBatch.STATUS_READY,
    }:
        raise FbsPickingError("Отгрузка уже передана и недоступна для изменения.")
    if source_batch.marketplace_state in {
        FbsHandoverBatch.MARKETPLACE_DELIVERY_PENDING,
        FbsHandoverBatch.MARKETPLACE_COMPLETE,
    }:
        raise FbsPickingError("Поставка уже передается в WB; перенос заказа недоступен.")

    order = (
        FbsOrder.objects.select_for_update()
        .select_related("profile__agency")
        .get(pk=order_id)
    )
    if order.profile_id != source_batch.profile_id:
        raise FbsPickingError("Заказ относится к другому кабинету клиента.")
    assignment = (
        FbsHandoverOrderAssignment.objects.select_for_update()
        .select_related("batch")
        .filter(order=order)
        .first()
    )
    if assignment is None:
        raise FbsPickingError("Назначение заказа в поставку WB не найдено.")
    if assignment.batch_id != source_batch.id:
        existing_context = invalid_kiz_reroute_context(assignment.batch)
        if (
            existing_context is not None
            and int(existing_context["source_batch_id"]) == source_batch.id
            and int(existing_context["order_id"]) == order.id
        ):
            if str(existing_context["route"]) != route:
                raise FbsPickingError(
                    "Для заказа уже выбран другой маршрут решения проблемы КИЗа."
                )
            existing_problem_item = (
                FbsProblemToteItem.objects.select_for_update()
                .select_related("problem_tote")
                .filter(
                    order=order,
                    status=FbsProblemToteItem.STATUS_IN_TOTE,
                    severity=FbsProblemToteItem.SEVERITY_CRITICAL,
                )
                .order_by("-id")
                .first()
            )
            if existing_problem_item is None:
                raise FbsPickingError(
                    "Перенос уже начат, но физическое помещение товара в "
                    "проблемную тару не подтверждено. Обратитесь к начальнику склада."
                )
            if str(problem_tote_scan or "").strip().upper() != str(
                existing_problem_item.problem_tote.barcode or ""
            ).strip().upper():
                raise FbsPickingError(
                    "Повторно отсканируйте ту же проблемную тару: "
                    f"{existing_problem_item.problem_tote.barcode}."
                )
            active_pick_batch_id = (
                order.pick_tasks.filter(
                    status__in=(FbsPickTask.STATUS_QUEUED, FbsPickTask.STATUS_IN_PROGRESS)
                )
                .order_by("-id")
                .values_list("batch_id", flat=True)
                .first()
            )
            return FbsInvalidKizRerouteResult(
                order_id=order.id,
                source_batch_id=source_batch.id,
                target_batch_id=assignment.batch_id,
                route=route,
                status=("queued" if active_pick_batch_id else "moving"),
                pick_batch_id=active_pick_batch_id,
            )
        raise FbsPickingError("Заказ уже относится к другой поставке WB.")
    if assignment.status != FbsHandoverOrderAssignment.STATUS_CONFIRMED:
        raise FbsPickingError("Заказ еще не подтвержден в текущей поставке WB.")
    if order.internal_status not in {
        FbsOrder.STATUS_PICKED,
        FbsOrder.STATUS_READY_FOR_HANDOVER,
    }:
        raise FbsPickingError("Заказ нельзя вернуть в волну в текущем статусе.")

    handover_link = (
        FbsHandoverOrder.objects.select_for_update()
        .filter(
            order=order,
            box__batch=source_batch,
        )
        .first()
    )
    if (
        handover_link is not None
        and handover_link.status != FbsHandoverOrder.STATUS_ACTIVE
    ):
        raise FbsPickingError("Заказ уже исключен из короба этой отгрузки.")

    from .marketplace import is_final_wb_marking_rejection

    marking_transfers = list(
        FbsMarketplaceMetadataTransfer.objects.select_for_update(of=("self",))
        .select_related("traceability__allocation__pick_task")
        .filter(
            order_item__order=order,
            metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
        )
        .order_by("-id")
    )
    problem_transfers = [
        transfer
        for transfer in marking_transfers
        if is_final_wb_marking_rejection(transfer)
    ]
    if not problem_transfers:
        raise FbsPickingError(
            "Карантин или новая волна доступны только после окончательного "
            "отказа WB по КИЗу. Дождитесь решения площадки."
        )
    allocation = next(
        (
            transfer.traceability.allocation
            for transfer in problem_transfers
            if transfer.traceability_id
            and transfer.traceability.allocation.pick_task_id
            and transfer.traceability.allocation.status
            == FbsOrderStockAllocation.STATUS_PICKED
        ),
        None,
    )
    if allocation is None:
        raise FbsPickingError(
            "Невалидный КИЗ не связан с физически отобранной единицей заказа."
        )

    from .totes import record_invalid_kiz_problem_tote_item

    record_invalid_kiz_problem_tote_item(
        allocation_id=allocation.id,
        handover_batch_id=source_batch.id,
        route=route,
        problem_tote_scan=problem_tote_scan,
        performed_by=actor,
    )

    from .handover import wb_handover_compatibility_key

    marker = (
        f":invalid-kiz-route:{route}:source-{source_batch.id}:order-{order.id}"
    )
    base_key = wb_handover_compatibility_key(
        order,
        workstation_id=source_batch.boxes.filter(
            active_workstation__isnull=False
        ).values_list("active_workstation__id", flat=True).first(),
    )
    compatibility_key = f"{base_key[: max(0, 255 - len(marker))]}{marker}"
    now = timezone.now()
    target_batch = FbsHandoverBatch.objects.create(
        profile=source_batch.profile,
        external_name=(
            f"FULLBOX-{source_batch.profile_id}-{now:%Y%m%d-%H%M%S}-"
            f"KIZ-{source_batch.id}-{order.id}"
        )[:128],
        compatibility_key=compatibility_key,
        created_by=actor,
    )
    reason = (
        f"КИЗ заказа {order.external_order_id} не принят WB; "
        f"заказ переносится из отгрузки №{source_batch.id} без отмены продавцом."
    )
    if order.internal_status == FbsOrder.STATUS_READY_FOR_HANDOVER:
        previous_status = order.internal_status
        order.internal_status = FbsOrder.STATUS_PICKED
        order.hold_reason = "handover_kiz_move_pending"
        order.problem_reason = reason
        order.save(
            update_fields=[
                "internal_status",
                "hold_reason",
                "problem_reason",
                "updated_at",
            ]
        )
        log_order_bulk_transition(
            [order],
            previous_internal_status={order.pk: previous_status},
            internal_status=order.internal_status,
            user=actor,
            source="invalid_kiz_reroute_requested",
        )
    if handover_link is not None:
        handover_link.status = FbsHandoverOrder.STATUS_RETURN_PENDING
        handover_link.exclusion_reason = reason
        handover_link.save(update_fields=["status", "exclusion_reason"])
    assignment.batch = target_batch
    assignment.status = FbsHandoverOrderAssignment.STATUS_PENDING
    assignment.error = reason
    assignment.assigned_by = actor
    assignment.confirmed_at = None
    assignment.save(
        update_fields=[
            "batch",
            "status",
            "error",
            "assigned_by",
            "confirmed_at",
            "updated_at",
        ]
    )

    from .marketplace import schedule_wb_handover_order

    schedule_wb_handover_order(assignment_id=assignment.id, requested_by=actor)
    return FbsInvalidKizRerouteResult(
        order_id=order.id,
        source_batch_id=source_batch.id,
        target_batch_id=target_batch.id,
        route=route,
        status="moving",
    )


@transaction.atomic
def finalize_invalid_kiz_reroute(
    *, assignment_id: int
) -> FbsInvalidKizRerouteResult | None:
    """Finalize local stock routing after WB confirms the supply move."""
    assignment = (
        FbsHandoverOrderAssignment.objects.select_for_update(of=("self",))
        .select_related("batch", "order__profile__agency", "assigned_by")
        .get(pk=assignment_id)
    )
    context = invalid_kiz_reroute_context(assignment.batch)
    if context is None:
        return None
    if int(context["order_id"]) != assignment.order_id:
        raise FbsPickingError("Маршрут КИЗа содержит другой заказ.")
    if assignment.status != FbsHandoverOrderAssignment.STATUS_CONFIRMED:
        raise FbsPickingError("WB еще не подтвердил перенос заказа.")

    route = str(context["route"])
    source_batch_id = int(context["source_batch_id"])
    order = FbsOrder.objects.select_for_update().get(pk=assignment.order_id)
    active_pick_batch_id = (
        order.pick_tasks.filter(
            status__in=(FbsPickTask.STATUS_QUEUED, FbsPickTask.STATUS_IN_PROGRESS)
        )
        .order_by("-id")
        .values_list("batch_id", flat=True)
        .first()
    )
    source_link = (
        FbsHandoverOrder.objects.select_for_update()
        .filter(order=order, box__batch_id=source_batch_id)
        .first()
    )
    if (
        source_link is not None
        and source_link.status == FbsHandoverOrder.STATUS_EXCLUDED
        and (
            active_pick_batch_id
            or order.hold_reason
            in {"handover_kiz_quarantine", "handover_kiz_quarantine_no_stock"}
        )
    ):
        if not FbsProblemToteItem.objects.filter(
            order=order,
            status=FbsProblemToteItem.STATUS_IN_TOTE,
            severity=FbsProblemToteItem.SEVERITY_CRITICAL,
        ).exists():
            raise FbsPickingError(
                "Нельзя подтвердить завершение: проблемный товар больше не "
                "числится в физической таре."
            )
        return FbsInvalidKizRerouteResult(
            order_id=order.id,
            source_batch_id=source_batch_id,
            target_batch_id=assignment.batch_id,
            route=route,
            status=("queued" if active_pick_batch_id else "quarantine"),
            pick_batch_id=active_pick_batch_id,
        )

    transfers = list(
        FbsMarketplaceMetadataTransfer.objects.select_for_update(of=("self",))
        .select_related("traceability__allocation__pick_task")
        .filter(
            order_item__order=order,
            metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
        )
        .order_by("-id")
    )
    allocation = next(
        (
            transfer.traceability.allocation
            for transfer in transfers
            if transfer.traceability_id
            and transfer.traceability.allocation.pick_task_id
            and transfer.traceability.allocation.status
            == FbsOrderStockAllocation.STATUS_PICKED
        ),
        None,
    )
    if allocation is None:
        allocation = (
            FbsOrderStockAllocation.objects.select_for_update(of=("self",))
            .select_related("pick_task")
            .filter(
                order_item__order=order,
                status=FbsOrderStockAllocation.STATUS_PICKED,
                pick_task__isnull=False,
            )
            .order_by("-id")
            .first()
        )
    if allocation is None or allocation.pick_task_id is None:
        raise FbsPickingError("Физически отобранная единица проблемного заказа не найдена.")

    problem_scan = str(
        getattr(getattr(allocation, "traceability", None), "marking_code", "") or ""
    ).strip()
    problem_item = (
        FbsProblemToteItem.objects.select_for_update()
        .select_related("problem_tote", "session")
        .filter(
            order=order,
            order_item=allocation.order_item,
            scanned_value=problem_scan,
            status=FbsProblemToteItem.STATUS_IN_TOTE,
            severity=FbsProblemToteItem.SEVERITY_CRITICAL,
        )
        .order_by("-id")
        .first()
    )
    if problem_item is None:
        raise FbsPickingError(
            "Нельзя продолжить перенос: физическая проблемная единица не "
            "зарегистрирована в таре контролера."
        )
    movement = None
    for candidate in FbsToteMovement.objects.filter(
        tote=problem_item.problem_tote,
        controller_session=problem_item.session,
        handover_batch_id=source_batch_id,
        action=FbsToteMovement.ACTION_PLACE,
    ).order_by("-id"):
        details = candidate.details if isinstance(candidate.details, dict) else {}
        if (
            int(details.get("problem_item_id") or 0) == problem_item.id
            and int(details.get("allocation_id") or 0) == allocation.id
            and str(details.get("route") or "") == route
            and bool(details.get("physically_confirmed"))
        ):
            movement = candidate
            break
    if movement is None:
        raise FbsPickingError(
            "Нельзя продолжить перенос: отсутствует подтвержденное движение "
            "проблемного товара в тару."
        )

    now = timezone.now()
    route_label = "новую волну" if route == "rewave" else "карантин кладовщика"
    reason = (
        f"WB подтвердил перенос заказа {order.external_order_id} из отгрузки "
        f"№{source_batch_id} в отгрузку №{assignment.batch_id} без отмены заказа. "
        f"Причина: невалидный КИЗ. Маршрут: {route_label}."
    )
    for transfer in transfers:
        if transfer.status == FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED:
            continue
        transfer.status = FbsMarketplaceMetadataTransfer.STATUS_CANCELED
        transfer.last_error = reason
        transfer.save(update_fields=["status", "last_error", "updated_at"])
    FbsMarketplaceCommand.objects.select_for_update().filter(
        order=order,
        command_type__in=(WB_SET_ORDER_SGTINS, WB_READ_ORDER_METADATA),
        status__in=(
            FbsMarketplaceCommand.STATUS_PENDING,
            FbsMarketplaceCommand.STATUS_RETRY,
        ),
    ).update(
        status=FbsMarketplaceCommand.STATUS_CANCELLED,
        error=reason,
        next_attempt_at=None,
        updated_at=now,
    )

    task = FbsPickTask.objects.select_for_update().get(pk=allocation.pick_task_id)
    issue, created = FbsPickException.objects.select_for_update().get_or_create(
        allocation=allocation,
        status=FbsPickException.STATUS_OPEN,
        defaults={
            "task": task,
            "exception_type": FbsPickException.TYPE_OTHER,
            "reason": reason,
            "created_by": assignment.assigned_by,
        },
    )
    if not created and reason not in str(issue.reason or ""):
        issue.reason = " ".join(
            value for value in (str(issue.reason or "").strip(), reason) if value
        )
        issue.save(update_fields=["reason", "updated_at"])
    task.status = FbsPickTask.STATUS_EXCEPTION
    task.save(update_fields=["status", "updated_at"])

    if source_link is not None:
        source_link.status = FbsHandoverOrder.STATUS_EXCLUDED
        source_link.exclusion_reason = reason
        source_link.excluded_by = assignment.assigned_by
        source_link.excluded_at = now
        source_link.verified_label = None
        source_link.verified_by = None
        source_link.verified_at = None
        source_link.save(
            update_fields=[
                "status",
                "exclusion_reason",
                "excluded_by",
                "excluded_at",
                "verified_label",
                "verified_by",
                "verified_at",
            ]
        )

    previous_status = order.internal_status
    pick_batch_id = None
    if route == "rewave":
        order.internal_status = FbsOrder.STATUS_AWAITING_STOCK
        order.hold_reason = "handover_kiz_rewave"
        order.problem_reason = reason
        order.save(
            update_fields=[
                "internal_status",
                "hold_reason",
                "problem_reason",
                "updated_at",
            ]
        )
        from .picking import create_pick_batches, reserve_order_stock

        reservation = reserve_order_stock(
            order_id=order.id,
            reserved_by=assignment.assigned_by,
            allow_confirmed_invalid_kiz_rewave=True,
        )
        if reservation.reserved:
            for replacement in reservation.allocations:
                replacement_marking = str(
                    getattr(
                        getattr(replacement, "traceability", None),
                        "marking_code",
                        "",
                    )
                    or ""
                ).strip()
                same_serialized_balance = (
                    replacement.balance_id == allocation.balance_id
                    and bool(str(allocation.balance.marking_code or "").strip())
                )
                if (
                    same_serialized_balance
                    or (problem_scan and replacement_marking == problem_scan)
                ):
                    raise FbsPickingError(
                        "Проблемная физическая единица повторно попала в резерв. "
                        "Операция отменена; обратитесь к начальнику склада."
                    )
            batches = create_pick_batches(
                agency=order.profile.agency,
                order_ids=[order.id],
                created_by=assignment.assigned_by,
                allow_confirmed_invalid_kiz_rewave=True,
            )
            pick_batch_id = batches[0].id if batches else None
        if not pick_batch_id:
            order.refresh_from_db()
            order.internal_status = FbsOrder.STATUS_EXCEPTION
            order.hold_reason = "handover_kiz_quarantine_no_stock"
            order.problem_reason = (
                f"{reason} Свободного FBS-остатка для пересборки нет. "
                "Кладовщику: проверить товар и подготовить уведомление клиенту."
            )
            order.save(
                update_fields=[
                    "internal_status",
                    "hold_reason",
                    "problem_reason",
                    "updated_at",
                ]
            )
    else:
        order.internal_status = FbsOrder.STATUS_EXCEPTION
        order.hold_reason = "handover_kiz_quarantine"
        order.problem_reason = (
            f"{reason} Кладовщику: проверить проблемный товар и подготовить "
            "уведомление клиенту об отсутствии валидного остатка."
        )
        order.save(
            update_fields=[
                "internal_status",
                "hold_reason",
                "problem_reason",
                "updated_at",
            ]
        )

    order.refresh_from_db()
    log_order_bulk_transition(
        [order],
        previous_internal_status={order.pk: previous_status},
        internal_status=order.internal_status,
        user=assignment.assigned_by,
        source="finalize_invalid_kiz_reroute",
        occurred_at=now,
    )
    from .picking import refresh_pick_batch_verification

    refresh_pick_batch_verification(batch_id=task.batch_id)
    from .totes import release_confirmed_reroute_from_source_check_tote

    release_confirmed_reroute_from_source_check_tote(
        source_batch_id=source_batch_id,
        order_id=order.id,
        performed_by=assignment.assigned_by,
    )
    return FbsInvalidKizRerouteResult(
        order_id=order.id,
        source_batch_id=source_batch_id,
        target_batch_id=assignment.batch_id,
        route=route,
        status=("queued" if pick_batch_id else "quarantine"),
        pick_batch_id=pick_batch_id,
    )


@transaction.atomic
def _create_order_pick_restock_request(
    *,
    batch_id: int,
    handover_batch_id: int,
    order_id: int,
    reason_code: str,
    reason: str,
    confirm_seller_cancel: bool,
    actor,
    allow_completed_batch_return: bool = False,
) -> FbsPickRestockRequest:
    """Start return of one marketplace-canceled picked order without changing stock."""
    _require_writes()
    reason_code, reason = _order_restock_reason(reason_code, reason)
    batch = (
        FbsPickBatch.objects.select_for_update()
        .select_related("agency")
        .get(pk=batch_id)
    )
    batch_can_return = batch.status == FbsPickBatch.STATUS_VERIFICATION or (
        allow_completed_batch_return and batch.status == FbsPickBatch.STATUS_DONE
    )
    if not batch_can_return or batch.picking_completed_at is None:
        raise FbsPickingError("Исключение доступно только для доставленной волны на проверке.")
    existing = (
        FbsPickRestockRequest.objects.select_related("order")
        .filter(
            batch=batch,
            order_id=int(order_id),
            status__in=BLOCKING_PICK_RESTOCK_STATUSES,
        )
        .order_by("created_at", "id")
        .first()
    )
    if existing is not None:
        return existing
    if FbsPickRestockRequest.objects.filter(
        batch=batch,
        order__isnull=True,
        status__in=BLOCKING_PICK_RESTOCK_STATUSES,
    ).exists():
        raise FbsPickingError("Сначала завершите общий возврат отбора этой волны.")

    order = (
        FbsOrder.objects.select_for_update()
        .select_related("profile")
        .get(pk=order_id)
    )
    marketplace = order.profile.marketplace
    if marketplace not in {
        FbsIntegrationProfile.MARKETPLACE_WB,
        FbsIntegrationProfile.MARKETPLACE_OZON,
    }:
        raise FbsPickingError("Возврат отмененного заказа для этой площадки не поддерживается.")
    assignment = (
        FbsHandoverOrderAssignment.objects.select_for_update()
        .select_related("batch__profile")
        .filter(order=order, batch_id=handover_batch_id)
        .first()
    )
    if assignment is None or assignment.batch.status not in {
        FbsHandoverBatch.STATUS_OPEN,
        FbsHandoverBatch.STATUS_READY,
    }:
        raise FbsPickingError("Активная отгрузка этого заказа не найдена.")
    if (
        marketplace == FbsIntegrationProfile.MARKETPLACE_WB
        and assignment.batch.marketplace_state
        in {
            FbsHandoverBatch.MARKETPLACE_DELIVERY_PENDING,
            FbsHandoverBatch.MARKETPLACE_COMPLETE,
        }
    ):
        raise FbsPickingError(
            "Поставка уже передана в WB; складской возврат после передачи недоступен."
        )
    assignment_failed = assignment.status == FbsHandoverOrderAssignment.STATUS_ERROR
    if assignment.status not in {
        FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        FbsHandoverOrderAssignment.STATUS_ERROR,
    }:
        raise FbsPickingError("Заказ еще не прошел сверку состава поставки WB.")
    if not order.pick_tasks.filter(batch=batch, status=FbsPickTask.STATUS_PICKED).exists():
        raise FbsPickingError("Заказ не относится к завершенному отбору этой волны.")
    handover_link = (
        FbsHandoverOrder.objects.select_for_update()
        .filter(order=order, status=FbsHandoverOrder.STATUS_ACTIVE)
        .first()
    )
    controller_tote_order = (
        FbsControllerToteOrder.objects.select_for_update(of=("self",))
        .filter(
            order=order,
            check_tote__handover_batch_id=assignment.batch_id,
            check_tote__status__in=(
                FbsControllerCheckTote.STATUS_OPEN,
                FbsControllerCheckTote.STATUS_WAITING_KIZ,
                FbsControllerCheckTote.STATUS_READY,
                FbsControllerCheckTote.STATUS_COMPOSITION,
            ),
            status__in=(
                FbsControllerToteOrder.STATUS_LABELED,
                FbsControllerToteOrder.STATUS_COMPOSITION,
                FbsControllerToteOrder.STATUS_PACKED,
            ),
        )
        .first()
    )
    controller_tote_return = bool(
        allow_completed_batch_return and controller_tote_order is not None
    )
    if handover_link is None and not assignment_failed and not controller_tote_return:
        raise FbsPickingError("Заказ не находится в активном коробе этой отгрузки.")
    if handover_link is not None and handover_link.box.batch_id != assignment.batch_id:
        raise FbsPickingError("Заказ находится в коробе другой отгрузки.")

    client_canceled = reason_code == FbsPickRestockRequest.REASON_CLIENT_CANCELED
    marketplace_reports_cancel = order_is_client_canceled_by_marketplace(order)
    if marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        if not client_canceled or not marketplace_reports_cancel:
            raise FbsPickingError(
                "Заказ Ozon не отменен клиентом. Склад не может отменять активные "
                "заказы — заказ должен быть отгружен."
            )
    if not assignment_failed:
        if client_canceled and not marketplace_reports_cancel:
            raise FbsPickingError(
                "Маркетплейс еще не подтвердил отмену клиентом. "
                "Обновите заказы и повторите проверку."
            )
        if not client_canceled and not confirm_seller_cancel:
            raise FbsPickingError(
                "Подтвердите отмену заказа продавцом. WB может применить штраф."
            )

    allocations = list(
        FbsOrderStockAllocation.objects.select_for_update(of=("self",))
        .select_related("pick_task", "balance__box__pallet__cell")
        .filter(
            pick_task__batch=batch,
            pick_task__order=order,
            status=FbsOrderStockAllocation.STATUS_PICKED,
            qty_picked__gt=0,
        )
        .order_by("id")
    )
    planned_qty = sum(int(allocation.qty_picked or 0) for allocation in allocations)
    if not allocations or planned_qty <= 0:
        raise FbsPickingError("По заказу нет физически отобранного товара для возврата.")

    ozon_client_cancel = bool(
        marketplace == FbsIntegrationProfile.MARKETPLACE_OZON and client_canceled
    )
    now = timezone.now()
    request = FbsPickRestockRequest.objects.create(
        batch=batch,
        order=order,
        handover_assignment=assignment,
        status=(
            FbsPickRestockRequest.STATUS_QUEUED
            if ozon_client_cancel
            else FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE
        ),
        reason_code=reason_code,
        reason=reason,
        marketplace_action=(
            FbsPickRestockRequest.MARKETPLACE_ACTION_NONE
            if ozon_client_cancel
            else FbsPickRestockRequest.MARKETPLACE_ACTION_VERIFY_CANCEL
            if assignment_failed or client_canceled
            else FbsPickRestockRequest.MARKETPLACE_ACTION_SELLER_CANCEL
        ),
        marketplace_confirmed_at=now if ozon_client_cancel else None,
        planned_qty=planned_qty,
        created_by=actor,
    )
    FbsPickRestockLine.objects.bulk_create(
        [
            FbsPickRestockLine(
                request=request,
                allocation=allocation,
                source_balance=allocation.balance,
                source_box=allocation.balance.box,
                source_cell=allocation.balance.box.pallet.cell,
                planned_qty=int(allocation.qty_picked or 0),
            )
            for allocation in allocations
        ]
    )
    if handover_link is not None and not ozon_client_cancel:
        handover_link.status = FbsHandoverOrder.STATUS_RETURN_PENDING
        handover_link.exclusion_reason = reason
        handover_link.save(update_fields=["status", "exclusion_reason"])

    previous_status = order.internal_status
    order.internal_status = FbsOrder.STATUS_EXCEPTION
    order.hold_reason = (
        "pick_restock_waiting_tote"
        if ozon_client_cancel
        else "handover_exclusion_waiting_marketplace"
    )
    order.problem_reason = reason
    order.save(
        update_fields=["internal_status", "hold_reason", "problem_reason", "updated_at"]
    )
    log_order_bulk_transition(
        [order],
        previous_internal_status={order.pk: previous_status},
        internal_status=FbsOrder.STATUS_EXCEPTION,
        user=actor,
        source="create_order_pick_restock_request",
        occurred_at=timezone.now(),
    )
    if marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        from .marketplace import schedule_wb_order_exclusion

        schedule_wb_order_exclusion(request_id=request.id, requested_by=actor)
    return request


def create_order_pick_restock_request(
    *,
    batch_id: int,
    handover_batch_id: int,
    order_id: int,
    reason_code: str,
    reason: str,
    confirm_seller_cancel: bool,
    created_by,
) -> FbsPickRestockRequest:
    """Create an operator-requested return after explicit user authorization."""
    return _create_order_pick_restock_request(
        batch_id=batch_id,
        handover_batch_id=handover_batch_id,
        order_id=order_id,
        reason_code=reason_code,
        reason=reason,
        confirm_seller_cancel=confirm_seller_cancel,
        actor=_actor(created_by),
        allow_completed_batch_return=False,
    )


@transaction.atomic
def ensure_cancelled_order_pick_restock(
    *, order_id: int
) -> FbsPickRestockRequest | None:
    """Queue an idempotent physical return after the marketplace cancels a picked order.

    The sync only creates the return and verifies marketplace cancellation.
    A controller must still confirm the physical canceled-order tote before a
    picker can return stock, so no warehouse location changes happen here.
    """
    _require_writes()
    order = (
        FbsOrder.objects.select_for_update()
        .select_related("profile")
        .get(pk=order_id)
    )
    if not order_is_client_canceled_by_marketplace(order):
        return None
    if order.internal_status not in {
        FbsOrder.STATUS_PICKED,
        FbsOrder.STATUS_READY_FOR_HANDOVER,
        FbsOrder.STATUS_EXCEPTION,
        FbsOrder.STATUS_CANCELLED,
    }:
        return None
    existing = (
        order.pick_restock_requests.filter(
            status__in=(
                *BLOCKING_PICK_RESTOCK_STATUSES,
                FbsPickRestockRequest.STATUS_COMPLETED,
            ),
        )
        .order_by("created_at", "id")
        .first()
    )
    if existing is not None:
        return existing
    if not order.profile.is_active:
        return None
    if (
        order.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
        and (
            not feature_enabled("outbox")
            or not order.profile.outbox_enabled
        )
    ):
        return None

    task = (
        order.pick_tasks.select_related("batch")
        .filter(
            status=FbsPickTask.STATUS_PICKED,
            batch__status__in=(
                FbsPickBatch.STATUS_VERIFICATION,
                FbsPickBatch.STATUS_DONE,
            ),
            batch__picking_completed_at__isnull=False,
        )
        .order_by("-batch__picking_completed_at", "-id")
        .first()
    )
    if task is None:
        return None
    if not FbsOrderStockAllocation.objects.filter(
        pick_task=task,
        status=FbsOrderStockAllocation.STATUS_PICKED,
        qty_picked__gt=0,
    ).exists():
        return None
    assignment = (
        FbsHandoverOrderAssignment.objects.select_for_update()
        .select_related("batch")
        .filter(
            order=order,
            status__in={
                FbsHandoverOrderAssignment.STATUS_CONFIRMED,
                FbsHandoverOrderAssignment.STATUS_ERROR,
            },
            batch__status__in={
                FbsHandoverBatch.STATUS_OPEN,
                FbsHandoverBatch.STATUS_READY,
            },
        )
        .order_by("-id")
        .first()
    )
    if assignment is None:
        return None
    if (
        order.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
        and not str(assignment.batch.external_supply_id or "").strip()
    ):
        return None
    if (
        order.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
        and assignment.batch.marketplace_state
        in {
            FbsHandoverBatch.MARKETPLACE_DELIVERY_PENDING,
            FbsHandoverBatch.MARKETPLACE_COMPLETE,
        }
    ):
        return None
    handover_link = FbsHandoverOrder.objects.filter(
        order=order,
        box__batch_id=assignment.batch_id,
        status=FbsHandoverOrder.STATUS_ACTIVE,
    ).exists()
    controller_tote_order = FbsControllerToteOrder.objects.filter(
        order=order,
        check_tote__handover_batch_id=assignment.batch_id,
        check_tote__status__in=(
            FbsControllerCheckTote.STATUS_OPEN,
            FbsControllerCheckTote.STATUS_WAITING_KIZ,
            FbsControllerCheckTote.STATUS_READY,
            FbsControllerCheckTote.STATUS_COMPOSITION,
        ),
        status__in=(
            FbsControllerToteOrder.STATUS_LABELED,
            FbsControllerToteOrder.STATUS_COMPOSITION,
            FbsControllerToteOrder.STATUS_PACKED,
        ),
    ).exists()
    if (
        not handover_link
        and not controller_tote_order
        and assignment.status != FbsHandoverOrderAssignment.STATUS_ERROR
    ):
        return None

    return _create_order_pick_restock_request(
        batch_id=task.batch_id,
        handover_batch_id=assignment.batch_id,
        order_id=order.id,
        reason_code=FbsPickRestockRequest.REASON_CLIENT_CANCELED,
        reason=(
            f"{order.profile.get_marketplace_display()} подтвердил отмену заказа "
            "после физического отбора."
        ),
        confirm_seller_cancel=False,
        actor=None,
        allow_completed_batch_return=handover_link or controller_tote_order,
    )


@transaction.atomic
def quarantine_order_pick_restock_request(
    *,
    request_id: int,
    problem_place_scan: str,
    performed_by,
) -> FbsPickRestockRequest:
    """Finish a marketplace-approved exclusion in unavailable FBS stock."""
    _require_writes()
    actor = _actor(performed_by)
    request = (
        FbsPickRestockRequest.objects.select_for_update(of=("self",))
        .select_related("batch", "order", "handover_assignment")
        .get(pk=request_id)
    )
    if request.status == FbsPickRestockRequest.STATUS_COMPLETED:
        return request
    if request.order_id is None or request.handover_assignment_id is None:
        raise FbsPickingError("Задание не относится к проблемному заказу.")
    if request.status != FbsPickRestockRequest.STATUS_QUEUED:
        raise FbsPickingError("Сначала дождитесь подтверждения WB, что заказа нет в поставке.")
    if request.batch.verification_assigned_to_id != actor.id:
        raise FbsPickingError("Проблемный заказ должен обработать контролер этой волны.")

    line = (
        FbsPickRestockLine.objects.select_for_update(of=("self",))
        .select_related("allocation__pick_task")
        .filter(request=request)
        .order_by("id")
        .first()
    )
    if line is None or line.allocation.pick_task_id is None:
        raise FbsPickingError("В задании нет физически отобранного товара.")

    now = timezone.now()
    request.status = FbsPickRestockRequest.STATUS_COMPLETED
    request.returned_qty = request.planned_qty
    request.completed_at = now
    request.assigned_to = actor
    request.claimed_at = request.claimed_at or now
    request.save(
        update_fields=[
            "status",
            "returned_qty",
            "completed_at",
            "assigned_to",
            "claimed_at",
            "updated_at",
        ]
    )

    from .problems import report_verification_problem

    result = report_verification_problem(
        allocation_id=line.allocation_id,
        exception_type="other",
        reason=f"Заказ исключен из поставки WB. {request.reason}",
        problem_place_scan=problem_place_scan,
        reported_by=actor,
    )

    lines = list(
        FbsPickRestockLine.objects.select_for_update(of=("self",))
        .filter(request=request)
        .select_related("allocation__pick_task")
        .order_by("id")
    )
    allocation_ids = [row.allocation_id for row in lines]
    task_ids = {row.allocation.pick_task_id for row in lines}
    if len(task_ids) != 1:
        raise FbsPickingError("Возврат содержит позиции из разных заданий.")
    task_id = task_ids.pop()
    for row in lines:
        row.status = FbsPickRestockLine.STATUS_COMPLETED
        row.returned_qty = row.planned_qty
        row.completed_at = now
        row.save(update_fields=["status", "returned_qty", "completed_at", "updated_at"])

    FbsPickVerificationProgress.objects.select_for_update().filter(
        allocation_id__in=allocation_ids
    ).update(qty_verified=0, completed_at=None, updated_at=now)
    FbsOrderStockAllocation.objects.select_for_update().filter(
        id__in=allocation_ids
    ).update(qty_picked=0, updated_at=now)
    task = FbsPickTask.objects.select_for_update().get(pk=task_id)
    task.picked_qty = 0
    task.save(update_fields=["picked_qty", "updated_at"])
    batch = FbsPickBatch.objects.select_for_update().get(pk=request.batch_id)
    batch.picked_qty = max(0, int(batch.picked_qty or 0) - int(result.moved_qty or 0))
    batch.save(update_fields=["picked_qty", "updated_at"])

    assignment = FbsHandoverOrderAssignment.objects.select_for_update().get(
        pk=request.handover_assignment_id
    )
    assignment.status = FbsHandoverOrderAssignment.STATUS_CANCELED
    assignment.error = request.reason
    assignment.save(update_fields=["status", "error", "updated_at"])
    handover_link = (
        FbsHandoverOrder.objects.select_for_update()
        .filter(order_id=request.order_id)
        .first()
    )
    if handover_link is not None:
        handover_link.status = FbsHandoverOrder.STATUS_EXCLUDED
        handover_link.exclusion_reason = request.reason
        handover_link.excluded_by = actor
        handover_link.excluded_at = now
        handover_link.verified_label = None
        handover_link.verified_by = None
        handover_link.verified_at = None
        handover_link.save(
            update_fields=[
                "status",
                "exclusion_reason",
                "excluded_by",
                "excluded_at",
                "verified_label",
                "verified_by",
                "verified_at",
            ]
        )

    order = FbsOrder.objects.select_for_update().get(pk=request.order_id)
    order.internal_status = FbsOrder.STATUS_EXCEPTION
    order.hold_reason = "handover_order_quarantined"
    order.problem_reason = (
        f"Исключен из поставки WB и помещен в проблемный "
        f"FBS-короб {result.problem_box_code}. Причина: {request.reason}"
    )
    order.save(
        update_fields=["internal_status", "hold_reason", "problem_reason", "updated_at"]
    )
    FbsPickRestockScan.objects.create(
        request=request,
        line=lines[0],
        stage=FbsPickRestockScan.STAGE_BOX,
        result=FbsPickRestockScan.RESULT_SUCCESS,
        scan_value=str(problem_place_scan or "").strip(),
        expected_value=result.problem_box_code,
        quantity_after=request.returned_qty,
        message=(
            f"Заказ исключен; {result.moved_qty} шт. помещено в недоступный "
            f"FBS-короб {result.problem_box_code}."
        ),
        created_by=actor,
    )

    from .picking import refresh_pick_batch_verification

    refresh_pick_batch_verification(batch_id=batch.id)
    return request


def _release_order_pick_restock_to_queue(
    *,
    request: FbsPickRestockRequest,
    comment: str,
    performed_by=None,
) -> FbsPickRestockRequest:
    """Detach a physically isolated order without changing warehouse stock."""
    if request.order_id is None or request.handover_assignment_id is None:
        raise FbsPickingError("Задание не относится к проблемному заказу.")
    if request.status not in {
        FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
        FbsPickRestockRequest.STATUS_QUEUED,
        FbsPickRestockRequest.STATUS_IN_PROGRESS,
        FbsPickRestockRequest.STATUS_FAILED,
    }:
        raise FbsPickingError("Задание снятия заказа уже закрыто.")

    comment = str(comment or "").strip()
    reason = comment or str(request.reason or "").strip()
    if not reason:
        raise FbsPickingError("Укажите комментарий: что произошло с заказом.")
    actor = (
        performed_by
        if getattr(performed_by, "is_authenticated", False)
        else request.created_by
        if getattr(request.created_by, "is_authenticated", False)
        else None
    )
    now = timezone.now()
    if comment and request.reason != comment:
        request.reason = comment
        request.save(update_fields=["reason", "updated_at"])

    assignment = FbsHandoverOrderAssignment.objects.select_for_update().get(
        pk=request.handover_assignment_id
    )
    assignment.status = FbsHandoverOrderAssignment.STATUS_CANCELED
    assignment.error = reason
    assignment.save(update_fields=["status", "error", "updated_at"])

    handover_link = (
        FbsHandoverOrder.objects.select_for_update()
        .filter(
            order_id=request.order_id,
            box__batch_id=assignment.batch_id,
        )
        .first()
    )
    if handover_link is not None:
        handover_link.status = FbsHandoverOrder.STATUS_EXCLUDED
        handover_link.exclusion_reason = reason
        handover_link.excluded_by = actor
        handover_link.excluded_at = now
        handover_link.verified_label = None
        handover_link.verified_by = None
        handover_link.verified_at = None
        handover_link.save(
            update_fields=[
                "status",
                "exclusion_reason",
                "excluded_by",
                "excluded_at",
                "verified_label",
                "verified_by",
                "verified_at",
            ]
        )

    order = FbsOrder.objects.select_for_update().get(pk=request.order_id)
    order.internal_status = FbsOrder.STATUS_EXCEPTION
    order.hold_reason = {
        FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE:
            "handover_exclusion_waiting_marketplace",
        FbsPickRestockRequest.STATUS_FAILED: "handover_exclusion_failed",
    }.get(request.status, "pick_restock_pending")
    order.problem_reason = reason
    order.save(
        update_fields=["internal_status", "hold_reason", "problem_reason", "updated_at"]
    )
    return request


def _match_canceled_order_product_scans(
    *,
    request: FbsPickRestockRequest,
    product_scans,
):
    """Match every physical unit of a canceled order to its restock line."""
    clean_scans = [
        str(value or "").strip()
        for value in (product_scans or ())
        if str(value or "").strip()
    ]
    lines = list(
        FbsPickRestockLine.objects.select_for_update(of=("self",))
        .select_related(
            "allocation__balance",
            "allocation__order_item",
            "allocation__traceability",
        )
        .filter(request=request)
        .order_by("id")
    )
    expected_units = [
        line
        for line in lines
        for _ in range(int(line.planned_qty or 0))
    ]
    expected_qty = len(expected_units)
    if expected_qty <= 0:
        raise FbsPickingError(
            "По заказу нет состава для ручной проверки товара."
        )
    if len(clean_scans) != expected_qty:
        raise FbsPickingError(
            "Отсканируйте все товары отмененного заказа: "
            f"нужно {expected_qty} шт., получено {len(clean_scans)}."
        )

    from .picking import _canonicalize_kiz_scan, resolve_verification_item_scan

    unmatched_units = list(expected_units)
    matches = []
    for scan_index, product_scan in enumerate(clean_scans, start=1):
        match = None
        for unit_index, line in enumerate(unmatched_units):
            allocation = line.allocation
            try:
                matched_barcode, marking_scan = resolve_verification_item_scan(
                    allocation,
                    product_scan,
                )
            except FbsScanMismatchError:
                continue
            if not matched_barcode:
                continue
            traceability = getattr(allocation, "traceability", None)
            expected_marking = str(
                getattr(traceability, "marking_code", "")
                or allocation.balance.marking_code
                or ""
            ).strip()
            if expected_marking:
                if (
                    marking_scan
                    and _canonicalize_kiz_scan(expected_marking) != marking_scan
                ):
                    continue
            match = (
                unmatched_units.pop(unit_index),
                product_scan,
                expected_marking or matched_barcode,
                marking_scan,
                bool(expected_marking and not marking_scan),
            )
            break
        if match is None:
            raise FbsPickingError(
                f"Скан {scan_index} не относится к товарам отмененного заказа. "
                "Проверьте товар и повторите все сканы."
            )
        matches.append(match)
    return matches


@transaction.atomic
def release_order_pick_restock_to_queue(
    *,
    request_id: int,
    comment: str,
    confirm_physical: bool = False,
    order_scan: str = "",
    product_scans=None,
    canceled_tote_scan: str = "",
    handover_batch_id: int | None = None,
    performed_by,
) -> FbsPickRestockRequest:
    """Record exact canceled-tote placement before stock is physically returned."""
    _require_writes()
    actor = _actor(performed_by)
    comment = str(comment or "").strip()
    if not confirm_physical:
        raise FbsPickingError(
            "Подтвердите, что весь заказ помещен в тару отмененных заказов."
        )
    request = (
        FbsPickRestockRequest.objects.select_for_update(of=("self",))
        .select_related(
            "batch",
            "created_by",
            "handover_assignment",
            "order__profile",
            "source_tote",
        )
        .get(pk=request_id)
    )
    if request.status not in {
        FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
        FbsPickRestockRequest.STATUS_QUEUED,
        FbsPickRestockRequest.STATUS_IN_PROGRESS,
        FbsPickRestockRequest.STATUS_FAILED,
    }:
        raise FbsPickingError("Задание снятия заказа уже закрыто.")
    if handover_batch_id is not None and (
        request.handover_assignment_id is None
        or request.handover_assignment.batch_id != int(handover_batch_id)
    ):
        raise FbsPickingError("Заказ относится к другой отгрузке.")
    product_confirmation = product_scans is not None
    matched_product_scans = []
    if product_confirmation:
        if (
            request.order_id is None
            or request.reason_code
            != FbsPickRestockRequest.REASON_CLIENT_CANCELED
            or not order_is_client_canceled_by_marketplace(request.order)
        ):
            raise FbsPickingError(
                "Ручная проверка товара доступна только для заказа, "
                "отмененного маркетплейсом."
            )
        matched_product_scans = _match_canceled_order_product_scans(
            request=request,
            product_scans=product_scans,
        )
    ozon_order = bool(
        request.order_id
        and request.order.profile.marketplace
        == FbsIntegrationProfile.MARKETPLACE_OZON
    )
    if ozon_order and not product_confirmation:
        if not order_is_client_canceled_by_marketplace(request.order):
            raise FbsPickingError(
                "Заказ Ozon не отменен клиентом. Склад не может убрать его из отгрузки."
            )
        clean_order_scan = str(order_scan or "").strip()
        latest_label = (
            FbsOrderLabel.objects.filter(order_id=request.order_id)
            .order_by("-requested_at", "-id")
            .first()
        )
        expected_order_scan = str(getattr(latest_label, "barcode", "") or "").strip()
        if not expected_order_scan:
            raise FbsPickingError(
                "У отмененного заказа нет QR-этикетки. Обратитесь к руководителю смены."
            )
        if clean_order_scan != expected_order_scan:
            raise FbsPickingError(
                "Отсканирован другой заказ. Отсканируйте QR отмененного заказа."
            )
    from .totes import SERVICE_TOTE_CANCELED, controller_service_tote_for_actor

    session, canceled_tote = controller_service_tote_for_actor(
        actor=actor,
        purpose=SERVICE_TOTE_CANCELED,
        workstation_id=request.batch.workstation_id,
    )
    clean_canceled_tote_scan = str(canceled_tote_scan or "").strip()
    if clean_canceled_tote_scan.casefold() != str(
        canceled_tote.barcode or ""
    ).strip().casefold():
        raise FbsPickingError(
            "Неверная тара возврата. Отсканируйте назначенную тару отмененных "
            f"заказов {canceled_tote.barcode}."
        )
    if request.batch.workstation_id != session.workstation_id:
        raise FbsPickingError("Тара отмененных заказов привязана к другому рабочему месту.")
    if request.source_tote_id not in {None, canceled_tote.id}:
        raise FbsPickingError("Возврат уже связан с другой физической тарой.")
    if request.source_tote_id == canceled_tote.id and FbsToteMovement.objects.filter(
        tote=canceled_tote,
        target_kind="canceled_tote",
        details__request_id=request.id,
    ).exists():
        return request
    request = _release_order_pick_restock_to_queue(
        request=request,
        comment=comment,
        performed_by=actor,
    )
    request.source_tote = canceled_tote
    request.save(update_fields=["source_tote", "updated_at"])
    scanned_by_line = {}
    for (
        line,
        product_scan,
        expected_value,
        marking_scan,
        marking_resolved_by_order,
    ) in matched_product_scans:
        scanned_by_line[line.id] = scanned_by_line.get(line.id, 0) + 1
        FbsPickRestockScan.objects.create(
            request=request,
            line=line,
            stage=FbsPickRestockScan.STAGE_PICKUP_ITEM,
            result=FbsPickRestockScan.RESULT_SUCCESS,
            scan_value=marking_scan or product_scan,
            expected_value=expected_value,
            quantity_after=scanned_by_line[line.id],
            message=(
                "Штрихкод товара подтвержден; КИЗ выбран из привязки "
                "отмененного заказа."
                if marking_resolved_by_order
                else "Товар отмененного заказа подтвержден контролером "
                "вместо отсутствующей этикетки заказа."
            ),
            created_by=actor,
        )
    latest_label = None
    latest_print_job = None
    if product_confirmation and request.order_id:
        latest_label = (
            FbsOrderLabel.objects.filter(order_id=request.order_id)
            .order_by("-requested_at", "-id")
            .first()
        )
        if latest_label is not None:
            from processing_app.models import ProcessingPrintJob

            latest_print_job = (
                ProcessingPrintJob.objects.filter(
                    Q(card_id__startswith=f"fbs:order-label:{latest_label.id}:")
                    | Q(card_id__startswith=f"fbs:ozon-order-qr:{latest_label.id}:")
                )
                .order_by("-id")
                .first()
            )
    FbsToteMovement.objects.create(
        tote=canceled_tote,
        action=FbsToteMovement.ACTION_PLACE,
        source_kind="handover",
        source_code=str(request.handover_assignment_id or request.batch_id),
        target_kind="canceled_tote",
        target_code=canceled_tote.barcode,
        pick_batch=request.batch,
        controller_session=session,
        handover_batch_id=(
            request.handover_assignment.batch_id
            if request.handover_assignment_id
            else None
        ),
        quantity=request.planned_qty,
        details={
            "request_id": request.id,
            "order_id": request.order_id,
            "physically_confirmed": True,
            "identification_mode": (
                "product_scans_missing_label"
                if product_confirmation
                else "order_label"
            ),
            "order_scan": (
                "" if product_confirmation else str(order_scan or "").strip()
            ),
            "product_scan_count": len(matched_product_scans),
            "marking_resolution_mode": (
                "order_traceability"
                if any(match[4] for match in matched_product_scans)
                else "scanned_or_not_required"
            ),
            "label_id": getattr(latest_label, "id", None),
            "label_status": str(getattr(latest_label, "status", "") or ""),
            "print_job_id": getattr(latest_print_job, "id", None),
            "print_job_status": str(
                getattr(latest_print_job, "status", "") or ""
            ),
            "service_tote_scan": clean_canceled_tote_scan,
        },
        performed_by=actor,
    )
    controller_tote_rows = list(
        FbsControllerToteOrder.objects.select_for_update(of=("self",))
        .filter(
            order_id=request.order_id,
            status__in=(
                FbsControllerToteOrder.STATUS_LABELED,
                FbsControllerToteOrder.STATUS_COMPOSITION,
                FbsControllerToteOrder.STATUS_PACKED,
            ),
        )
        .values_list("id", "check_tote_id")
    )
    if controller_tote_rows:
        now = timezone.now()
        FbsControllerToteOrder.objects.filter(
            pk__in=[row_id for row_id, _check_tote_id in controller_tote_rows]
        ).update(
            status=FbsControllerToteOrder.STATUS_REMOVED,
            updated_at=now,
        )
        from .totes import refresh_check_tote_status

        for check_tote_id in sorted(
            {check_tote_id for _row_id, check_tote_id in controller_tote_rows}
        ):
            refresh_check_tote_status(check_tote_id=check_tote_id)
    return request


@transaction.atomic
def confirm_marketplace_rejected_order_return(
    *,
    request_id: int,
    canceled_tote_scan: str,
    performed_by,
) -> FbsPickRestockRequest:
    """Isolate a WB-rejected order so controller work can continue safely."""
    _require_writes()
    actor = _actor(performed_by)
    request = (
        FbsPickRestockRequest.objects.select_for_update(of=("self",))
        .select_related("batch", "order__profile", "handover_assignment", "source_tote")
        .get(pk=request_id)
    )
    if request.order_id is None or request.handover_assignment_id is None:
        raise FbsPickingError("Задание не относится к отклоненному заказу.")
    if request.reason_code != FbsPickRestockRequest.REASON_MARKETPLACE:
        raise FbsPickingError("Возврат создан не по отказу маркетплейса.")
    if request.order.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        raise FbsPickingError("Этот сценарий возврата доступен только для WB.")
    if request.batch.verification_assigned_to_id != actor.id:
        raise FbsPickingError("Возврат должен подтвердить контролер этой волны.")
    if request.status not in {
        FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
        FbsPickRestockRequest.STATUS_QUEUED,
        FbsPickRestockRequest.STATUS_FAILED,
    }:
        raise FbsPickingError("Задание снятия заказа уже закрыто.")

    from .totes import SERVICE_TOTE_CANCELED, controller_service_tote_for_actor

    session, canceled_tote = controller_service_tote_for_actor(
        actor=actor,
        purpose=SERVICE_TOTE_CANCELED,
        workstation_id=request.batch.workstation_id,
    )
    clean_tote_scan = str(canceled_tote_scan or "").strip()
    if clean_tote_scan.casefold() != str(canceled_tote.barcode or "").strip().casefold():
        raise FbsPickingError(
            "Неверная тара возврата. Отсканируйте назначенную тару отмененных "
            f"заказов {canceled_tote.barcode}."
        )
    if request.batch.workstation_id != session.workstation_id:
        raise FbsPickingError(
            "Тара отмененных заказов привязана к другому рабочему месту."
        )
    if request.source_tote_id not in {None, canceled_tote.id}:
        raise FbsPickingError("Возврат уже связан с другой физической тарой.")

    return release_order_pick_restock_to_queue(
        request_id=request.id,
        comment=request.reason,
        confirm_physical=True,
        canceled_tote_scan=clean_tote_scan,
        performed_by=actor,
    )


@transaction.atomic
def confirm_ozon_canceled_order_return(
    *,
    handover_batch_id: int,
    order_id: int,
    order_scan: str,
    canceled_tote_scan: str,
    comment: str,
    performed_by,
) -> FbsPickRestockRequest:
    """Route an Ozon-client-canceled order to the controller's canceled tote."""
    _require_writes()
    actor = _actor(performed_by)
    handover_batch = (
        FbsHandoverBatch.objects.select_for_update()
        .select_related("profile")
        .get(pk=handover_batch_id)
    )
    if handover_batch.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_OZON:
        raise FbsPickingError("Это действие доступно только для отгрузок Ozon.")
    if handover_batch.status not in {
        FbsHandoverBatch.STATUS_OPEN,
        FbsHandoverBatch.STATUS_READY,
    }:
        raise FbsPickingError("Из этой отгрузки уже нельзя вернуть отмененный заказ.")
    order = (
        FbsOrder.objects.select_for_update()
        .select_related("profile")
        .get(pk=order_id)
    )
    if order.profile_id != handover_batch.profile_id:
        raise FbsPickingError("Заказ относится к другому кабинету Ozon.")
    if not order_is_client_canceled_by_marketplace(order):
        raise FbsPickingError(
            "Заказ Ozon не отменен клиентом. Склад не может отменять или убирать "
            "активные заказы — заказ должен быть отгружен."
        )
    assignment = (
        FbsHandoverOrderAssignment.objects.select_for_update()
        .filter(batch=handover_batch, order=order)
        .first()
    )
    if assignment is None:
        raise FbsPickingError("Отмененный заказ не найден в этой отгрузке.")
    request = ensure_cancelled_order_pick_restock(order_id=order.id)
    if request is None or request.handover_assignment_id != assignment.id:
        raise FbsPickingError(
            "Не удалось подготовить физический возврат отмененного заказа."
        )
    clean_comment = str(comment or "").strip()
    reason = "Заказ отменен клиентом в Ozon."
    if clean_comment:
        reason = f"{reason} {clean_comment}"
    return release_order_pick_restock_to_queue(
        request_id=request.id,
        comment=reason,
        confirm_physical=True,
        order_scan=order_scan,
        canceled_tote_scan=canceled_tote_scan,
        performed_by=actor,
    )


@transaction.atomic
def queue_order_pick_restock_after_marketplace(
    *, request_id: int, confirmed_by=None
) -> FbsPickRestockRequest:
    """Queue physical return only after WB confirms the order left the supply."""
    request = (
        FbsPickRestockRequest.objects.select_for_update(of=("self",))
        .select_related("order", "handover_assignment", "batch")
        .get(pk=request_id)
    )
    if request.order_id is None:
        raise FbsPickingError("Задание не является возвратом отдельного заказа.")
    if request.status == FbsPickRestockRequest.STATUS_QUEUED:
        return _release_order_pick_restock_to_queue(
            request=request,
            comment=request.reason,
            performed_by=confirmed_by,
        )
    if request.status in {
        FbsPickRestockRequest.STATUS_IN_PROGRESS,
        FbsPickRestockRequest.STATUS_COMPLETED,
    }:
        return request
    if request.status not in {
        FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
        FbsPickRestockRequest.STATUS_FAILED,
    }:
        raise FbsPickingError("Задание исключения уже закрыто.")
    now = timezone.now()
    request.status = FbsPickRestockRequest.STATUS_QUEUED
    request.marketplace_confirmed_at = now
    request.save(
        update_fields=["status", "marketplace_confirmed_at", "updated_at"]
    )
    order = request.order
    order.internal_status = FbsOrder.STATUS_EXCEPTION
    order.hold_reason = "pick_restock_pending"
    order.problem_reason = request.reason
    order.save(
        update_fields=["internal_status", "hold_reason", "problem_reason", "updated_at"]
    )
    note = f"Заказ исключен из поставки WB; возврат отбора #{request.id}: {request.reason}"
    FbsOrderLabel.objects.filter(order=order).update(
        status=FbsOrderLabel.STATUS_CANCELED,
        error=note,
        updated_at=now,
    )
    FbsMarketplaceMetadataTransfer.objects.filter(order_item__order=order).exclude(
        status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED
    ).update(
        status=FbsMarketplaceMetadataTransfer.STATUS_CANCELED,
        last_error=note,
        updated_at=now,
    )
    FbsMarketplaceCommand.objects.filter(
        order=order,
        status__in=(
            FbsMarketplaceCommand.STATUS_PENDING,
            FbsMarketplaceCommand.STATUS_SENT,
            FbsMarketplaceCommand.STATUS_RETRY,
        ),
    ).exclude(
        command_type__in=(WB_CANCEL_ORDER, WB_READ_HANDOVER_ORDER_IDS)
    ).update(
        status=FbsMarketplaceCommand.STATUS_CANCELLED,
        next_attempt_at=None,
        error=note,
        updated_at=now,
    )
    return _release_order_pick_restock_to_queue(
        request=request,
        comment=request.reason,
        performed_by=confirmed_by,
    )


@transaction.atomic
def fail_order_pick_restock_marketplace(
    *, request_id: int, error: str
) -> FbsPickRestockRequest:
    request = FbsPickRestockRequest.objects.select_for_update().get(pk=request_id)
    if request.status == FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE:
        request.status = FbsPickRestockRequest.STATUS_FAILED
        request.save(update_fields=["status", "updated_at"])
        if request.order_id:
            FbsOrder.objects.filter(pk=request.order_id).update(
                hold_reason="handover_exclusion_failed",
                problem_reason=str(error or request.reason)[:2000],
                updated_at=timezone.now(),
            )
    return request


@transaction.atomic
def retry_order_pick_restock_marketplace(
    *, request_id: int, requested_by
) -> FbsPickRestockRequest:
    actor = _actor(requested_by)
    request = FbsPickRestockRequest.objects.select_for_update().get(pk=request_id)
    if request.status != FbsPickRestockRequest.STATUS_FAILED:
        raise FbsPickingError("Повтор доступен только после ошибки исключения.")
    request.status = FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE
    request.save(update_fields=["status", "updated_at"])
    from .marketplace import schedule_wb_order_exclusion

    schedule_wb_order_exclusion(
        request_id=request.id,
        requested_by=actor,
        force_retry=True,
    )
    return request


def _release_stale_unstarted_pick_restock_request(
    *, request: FbsPickRestockRequest, now
) -> bool:
    """Return an abandoned, untouched tote task to the common queue."""
    if int(request.returned_qty or 0) > 0:
        return False
    if request.scans.filter(result=FbsPickRestockScan.RESULT_SUCCESS).exists():
        return False
    if request.lines.exclude(
        status=FbsPickRestockLine.STATUS_PENDING,
        returned_qty=0,
    ).exists():
        return False

    latest_scan_at = (
        request.scans.order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    activity = [
        value
        for value in (request.claimed_at, request.updated_at, latest_scan_at)
        if value is not None
    ]
    if not activity or max(activity) >= now - PICK_RESTOCK_STALE_CLAIM_AFTER:
        return False

    request.status = FbsPickRestockRequest.STATUS_QUEUED
    request.assigned_to = None
    request.claimed_at = None
    request.save(update_fields=["status", "assigned_to", "claimed_at", "updated_at"])
    return True


@transaction.atomic
def claim_pick_restock_request(
    *, request_id: int, assigned_to
) -> FbsPickRestockRequest:
    _require_writes()
    actor = _actor(assigned_to)
    request = FbsPickRestockRequest.objects.select_for_update().get(pk=request_id)
    if request.status == FbsPickRestockRequest.STATUS_COMPLETED:
        return request
    if request.status == FbsPickRestockRequest.STATUS_CANCELED:
        raise FbsPickingError("Задание возврата отменено.")
    if request.status in {
        FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
        FbsPickRestockRequest.STATUS_FAILED,
    }:
        raise FbsPickingError("Сначала дождитесь подтверждения исключения маркетплейсом.")
    if request.order_id is not None and request.source_tote_id is None:
        raise FbsPickingError("Контролер еще не подтвердил физическую тару возврата.")
    if request.source_tote_id is not None:
        # Serialize claims for one physical tote before inspecting its tasks.
        FbsPickingCart.objects.select_for_update().only("pk").get(
            pk=request.source_tote_id
        )
        siblings = list(
            FbsPickRestockRequest.objects.select_for_update()
            .filter(
                source_tote_id=request.source_tote_id,
                status=FbsPickRestockRequest.STATUS_IN_PROGRESS,
            )
            .exclude(pk=request.pk)
            .exclude(assigned_to=actor)
            .order_by("id")
        )
        now = timezone.now()
        for sibling in siblings:
            if not _release_stale_unstarted_pick_restock_request(
                request=sibling,
                now=now,
            ):
                raise FbsPickingError(
                    f"Тару уже разбирает другой комплектовщик по заданию #{sibling.id}."
                )
    if request.assigned_to_id not in {None, actor.id}:
        raise FbsPickingError("Задание возврата уже взял другой подборщик.")
    if request.assigned_to_id is None:
        request.assigned_to = actor
        request.claimed_at = timezone.now()
    request.status = FbsPickRestockRequest.STATUS_IN_PROGRESS
    request.save(update_fields=["assigned_to", "claimed_at", "status", "updated_at"])
    return request


def _record_error_scan(
    *,
    request_id: int,
    line_id: int | None,
    stage: str,
    scan_value: str,
    expected_value: str,
    message: str,
    request_token: uuid.UUID,
    actor,
) -> None:
    try:
        FbsPickRestockScan.objects.create(
            request_id=request_id,
            line_id=line_id,
            stage=stage if stage in dict(FbsPickRestockScan.STAGE_CHOICES) else FbsPickRestockScan.STAGE_ITEM,
            result=FbsPickRestockScan.RESULT_ERROR,
            scan_value=str(scan_value or ""),
            expected_value=str(expected_value or ""),
            message=message,
            request_token=request_token,
            created_by=actor,
        )
    except IntegrityError:
        pass


def _existing_scan_result(
    *, request_id: int, request_token: uuid.UUID
) -> FbsPickRestockScanResult | None:
    event = FbsPickRestockScan.objects.select_related("request").filter(
        request_token=request_token
    ).first()
    if event is None:
        return None
    if event.request_id != request_id:
        raise FbsPickingError("Защитный идентификатор уже использован в другом задании.")
    if event.result == FbsPickRestockScan.RESULT_ERROR:
        raise FbsPickingError(event.message or "Сканирование не принято.")
    return FbsPickRestockScanResult(request=event.request, event=event, duplicate=True)


def scan_pick_restock(
    *,
    request_id: int,
    stage: str,
    scan_value: str,
    request_token,
    performed_by,
    destination_event_id=None,
    pickup_line_id=None,
) -> FbsPickRestockScanResult:
    _require_writes()
    actor = _actor(performed_by)
    token = _request_token(request_token)
    duplicate = _existing_scan_result(request_id=request_id, request_token=token)
    if duplicate is not None:
        return duplicate
    line_id = None
    expected_value = ""
    try:
        with transaction.atomic():
            request = (
                FbsPickRestockRequest.objects.select_for_update(of=("self",))
                .select_related(
                    "batch__workstation",
                    "source_tote",
                    "quarantine_box__pallet__cell__location",
                )
                .get(pk=request_id)
            )
            if request.status == FbsPickRestockRequest.STATUS_COMPLETED:
                raise FbsPickingError("Задание возврата уже завершено.")
            if request.status != FbsPickRestockRequest.STATUS_IN_PROGRESS:
                raise FbsPickingError("Сначала возьмите задание возврата.")
            if request.assigned_to_id != actor.id:
                raise FbsPickingError("Задание возврата назначено другому подборщику.")
            state = pick_restock_state(request.id)
            line = state["line"]
            if line is None:
                raise FbsPickingError("В задании не осталось товара для возврата.")
            line_id = line.id
            required_stage = state["stage"]
            if required_stage in {"cell", "box", "item"} and str(destination_event_id or "") != str(state.get("destination_event_id") or ""):
                raise FbsPickingError("Место возврата изменилось. Обновите экран и повторите сканирование.")
            if stage != required_stage:
                raise FbsPickingError("Экран задания изменился. Повторите сканирование.")
            selected_option = None
            if stage == FbsPickRestockScan.STAGE_PICKUP_ITEM:
                selected_option = next(
                    (
                        option
                        for option in state.get("pickup_options", [])
                        if str(option["line"].id) == str(pickup_line_id or "")
                    ),
                    None,
                )
                if selected_option is None:
                    raise FbsPickingError(
                        "Сначала выберите товар из списка, затем отсканируйте его штрихкод."
                    )
                line = selected_option["line"]
                line_id = line.id
            scan_value = str(scan_value or "").strip()
            if not scan_value:
                raise FbsPickingError("Скан не получен. Повторите сканирование.")

            now = timezone.now()
            if stage == FbsPickRestockScan.STAGE_WORKSTATION:
                if request.source_tote_id is not None:
                    binding = (
                        FbsToteBinding.objects.select_for_update(of=("self",))
                        .select_related("workstation", "controller_session")
                        .filter(tote_id=request.source_tote_id)
                        .first()
                    )
                    allowed_binding_states = {FbsToteBinding.STATE_AT_CONTROL}
                    if (
                        binding is not None
                        and binding.controller_session_id is not None
                        and request.source_tote_id
                        == binding.controller_session.unknown_tote_id
                    ):
                        allowed_binding_states.add(FbsToteBinding.STATE_UNKNOWN)
                    if (
                        binding is None
                        or binding.state not in allowed_binding_states
                        or binding.workstation_id is None
                        or binding.controller_session_id is None
                        or binding.controller_session.status
                        != binding.controller_session.STATUS_ACTIVE
                        or request.source_tote_id
                        not in {
                            binding.controller_session.problem_tote_id,
                            binding.controller_session.canceled_tote_id,
                        }
                    ):
                        raise FbsPickingError(
                            "Исходная служебная тара перемещена. Обратитесь к контролеру."
                        )
                    expected_value = request.source_tote.barcode
                    if _normalized(scan_value) != _normalized(expected_value):
                        raise FbsPickingError(
                            "Неверная тара. Отсканируйте указанную служебную тару."
                        )
                    message = (
                        f"Тара {request.source_tote.name} подтверждена. "
                        f"Заберите {int(request.planned_qty or 0) - int(request.returned_qty or 0)} шт."
                    )
                    event = FbsPickRestockScan.objects.create(
                        request=request,
                        line=None,
                        stage=stage,
                        result=FbsPickRestockScan.RESULT_SUCCESS,
                        scan_value=scan_value,
                        expected_value=expected_value,
                        quantity_after=request.returned_qty,
                        message=message,
                        request_token=token,
                        created_by=actor,
                    )
                    return FbsPickRestockScanResult(request=request, event=event)
                workstation = request.batch.workstation
                if workstation is None:
                    raise FbsPickingError(
                        "Для возврата не указан исходный рабочий стол. Обратитесь к кладовщику."
                    )
                expected_value = workstation.barcode
                if _normalized(scan_value) != _normalized(expected_value):
                    raise FbsPickingError(
                        f"Неверный стол. Подойдите к {workstation.name} и отсканируйте его QR."
                    )
                message = (
                    f"Стол {workstation.name} подтвержден. "
                    f"Заберите {int(request.planned_qty or 0) - int(request.returned_qty or 0)} шт."
                )
                event = FbsPickRestockScan.objects.create(
                    request=request,
                    line=None,
                    stage=stage,
                    result=FbsPickRestockScan.RESULT_SUCCESS,
                    scan_value=scan_value,
                    expected_value=expected_value,
                    quantity_after=request.returned_qty,
                    message=message,
                    request_token=token,
                    created_by=actor,
                )
                return FbsPickRestockScanResult(request=request, event=event)

            if stage == FbsPickRestockScan.STAGE_PICKUP_ITEM:
                expected_value = line.source_balance.barcode or line.allocation.order_item.barcode
                if _normalized(scan_value) != _normalized(expected_value):
                    raise FbsPickingError(
                        "Неверный товар из тары. Отсканируйте товар текущей позиции."
                        if request.source_tote_id
                        else "Неверный товар со стола. Отсканируйте товар текущей позиции."
                    )
                quantity_after = int(selected_option["scanned"] or 0) + 1
                message = (
                    ("Товар взят из тары: " if request.source_tote_id else "Товар снят со стола: ")
                    + f"{quantity_after}/{int(selected_option['planned'] or 0)} шт."
                )
                event = FbsPickRestockScan.objects.create(
                    request=request,
                    line=line,
                    stage=stage,
                    result=FbsPickRestockScan.RESULT_SUCCESS,
                    scan_value=scan_value,
                    expected_value=expected_value,
                    quantity_after=quantity_after,
                    message=message,
                    request_token=token,
                    created_by=actor,
                )
                return FbsPickRestockScanResult(request=request, event=event)

            allocation = (
                FbsOrderStockAllocation.objects.select_for_update(of=("self",))
                .select_related("pick_task__order__profile__agency", "order_item", "traceability")
                .get(pk=line.allocation_id)
            )
            task = (
                FbsPickTask.objects.select_for_update()
                .select_related("order__profile__agency")
                .get(pk=allocation.pick_task_id)
            )
            batch = FbsPickBatch.objects.select_for_update().get(pk=request.batch_id)
            balance = (
                FbsStockBalance.objects.select_for_update()
                .select_related("box__pallet__cell__location")
                .get(pk=line.source_balance_id)
            )
            is_quarantine = request.quarantine_box_id is not None
            destination_box = (
                FbsBox.objects.select_for_update()
                .select_related("pallet__cell__location")
                .get(pk=request.quarantine_box_id)
                if is_quarantine
                else FbsBox.objects.select_for_update(of=("self",)).select_related("pallet__cell__location").get(pk=state["destination_box"].pk)
                if state.get("destination_event_id")
                else balance.box
            )
            destination_cell = destination_box.pallet.cell
            if destination_box.source_container_id:
                destination_box.source_container = (
                    WarehouseContainer.objects.select_for_update(of=("self",))
                    .select_related("current_location")
                    .get(pk=destination_box.source_container_id)
                )
            destination_location = _physical_destination_for_box(destination_box)
            rerouted = bool(state.get("destination_event_id"))
            if not rerouted and (balance.box_id != line.source_box_id or balance.box.pallet.cell_id != line.source_cell_id):
                raise FbsPickingError("Исходный FBS-короб был перемещен. Нужна проверка кладовщика.")
            if not rerouted and (
                balance.box.status not in (FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE)
                or balance.box.pallet.status not in ("planned", "active")
                or not balance.box.pallet.cell.is_active
            ):
                raise FbsPickingError("Исходный FBS-короб или ячейка недоступны.")
            if (
                destination_box.agency_id != task.order.profile.agency_id
                or destination_box.status not in (FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE)
                or destination_box.pallet.status not in ("planned", "active")
                or not destination_cell.is_active
            ):
                raise FbsPickingError("Карантинный FBS-короб или ячейка недоступны.")
            # A return goes back into its existing physical box. PR is not a
            # storage zone, but can be the recorded place of that original box.
            source_container = destination_box.source_container
            original_box_in_receiving = (
                not is_quarantine
                and destination_location is not None
                and destination_location.zone_code == "PR"
                and destination_location.zone_kind == "receiving"
                and source_container is not None
                and source_container.current_location_id == destination_location.pk
                and source_container.agency_id == destination_box.agency_id
                and source_container.status == "active"
            )
            if (
                destination_location is None
                or not destination_location.is_active
                or not (destination_location.is_storage or original_box_in_receiving)
            ):
                raise FbsPickingError(
                    "Физическое место короба возврата не определено. Обратитесь к кладовщику."
                )
            if rerouted:
                from .restock_destinations import assert_destination, destination_event
                route_event = destination_event(request.id, line.source_box_id)
                assert_destination(destination_box, task.order.profile.agency_id, selected_location_id=route_event.payload["destination_location_id"])
            from .inventory import assert_balance_unlocked, assert_box_unlocked

            assert_balance_unlocked(balance.id, for_execution=True)
            assert_box_unlocked(balance.box_id, for_execution=True)
            if is_quarantine or rerouted:
                assert_box_unlocked(destination_box.id, for_execution=True)

            if stage == FbsPickRestockScan.STAGE_CELL:
                expected_value = fbs_box_physical_location_code(destination_box)
                if _normalized_cell_scan(scan_value) not in _cell_scan_candidates_for_box(
                    destination_box
                ):
                    raise FbsPickingError("Неверная ячейка. Отсканируйте указанное место возврата.")
                message = (
                    "Карантинная ячейка подтверждена."
                    if is_quarantine
                    else "Исходная ячейка подтверждена."
                )
            elif stage == FbsPickRestockScan.STAGE_BOX:
                expected_value = destination_box.box_code
                if _normalized(scan_value) != _normalized(expected_value):
                    raise FbsPickingError("Неверный короб. Отсканируйте указанный короб возврата.")
                message = (
                    "Карантинный короб подтвержден."
                    if is_quarantine
                    else "Исходный короб подтвержден."
                )
            else:
                expected_value = balance.barcode or allocation.order_item.barcode
                if _normalized(scan_value) != _normalized(expected_value):
                    raise FbsPickingError("Неверный товар. Отсканируйте товар текущей позиции.")
                if int(allocation.qty_picked or 0) <= 0:
                    raise FbsPickingError("Эта единица уже возвращена.")
                if balance.marking_code and int(balance.qty or 0) > 0:
                    raise FbsPickingError("КИЗ уже числится в FBS-остатке.")

                if is_quarantine:
                    from .problems import restore_restock_unit_to_problem_box

                    restored_balance = restore_restock_unit_to_problem_box(
                        allocation=allocation,
                        balance=balance,
                        problem_box=destination_box,
                    )
                elif destination_box.pk != balance.box_id:
                    from .restock_destinations import restore_to_destination
                    restored_balance = restore_to_destination(allocation=allocation, balance=balance, box=destination_box)
                else:
                    balance.qty = int(balance.qty or 0) + 1
                    balance.available_qty = int(balance.available_qty or 0) + 1
                    balance.full_clean()
                    balance.save(update_fields=["qty", "available_qty", "updated_at"])
                    restored_balance = balance

                line.returned_qty = int(line.returned_qty or 0) + 1
                line.status = (
                    FbsPickRestockLine.STATUS_COMPLETED
                    if line.returned_qty == line.planned_qty
                    else FbsPickRestockLine.STATUS_IN_PROGRESS
                )
                line.completed_at = now if line.status == FbsPickRestockLine.STATUS_COMPLETED else None
                line.save(
                    update_fields=["returned_qty", "status", "completed_at", "updated_at"]
                )

                allocation.qty_picked = int(allocation.qty_picked or 0) - 1
                if allocation.qty_picked == 0:
                    allocation.status = FbsOrderStockAllocation.STATUS_CANCELED
                    allocation.released_by = actor
                    allocation.released_at = now
                allocation.save(
                    update_fields=[
                        "qty_picked",
                        "status",
                        "released_by",
                        "released_at",
                        "updated_at",
                    ]
                )
                progress = FbsPickVerificationProgress.objects.select_for_update().filter(
                    allocation=allocation
                ).first()
                if progress is not None and int(progress.qty_verified or 0) > allocation.qty_picked:
                    progress.qty_verified = allocation.qty_picked
                    progress.completed_at = None
                    progress.save(update_fields=["qty_verified", "completed_at", "updated_at"])
                trace = getattr(allocation, "traceability", None)
                if trace is not None and allocation.qty_picked == 0:
                    trace.status = FbsOrderTraceability.STATUS_CANCELED
                    trace.save(update_fields=["status", "updated_at"])

                task.picked_qty = int(task.picked_qty or 0) - 1
                task_completed = task.picked_qty == 0
                if task_completed:
                    if is_quarantine:
                        task.status = FbsPickTask.STATUS_EXCEPTION
                        task.canceled_at = None
                    else:
                        task.status = FbsPickTask.STATUS_CANCELED
                        task.canceled_at = now
                task.save(update_fields=["picked_qty", "status", "canceled_at", "updated_at"])

                batch.picked_qty = int(batch.picked_qty or 0) - 1
                request.returned_qty = int(request.returned_qty or 0) + 1
                request_completed = request.returned_qty == request.planned_qty
                order_specific = request.order_id is not None
                if request_completed:
                    request.status = FbsPickRestockRequest.STATUS_COMPLETED
                    request.completed_at = now
                    if not order_specific:
                        batch.status = FbsPickBatch.STATUS_CANCELED
                        batch.canceled_at = now
                        batch.verification_assigned_to = None
                request.save(
                    update_fields=["returned_qty", "status", "completed_at", "updated_at"]
                )
                batch_update_fields = ["picked_qty", "updated_at"]
                if request_completed and not order_specific:
                    batch_update_fields.extend(
                        ["status", "canceled_at", "verification_assigned_to"]
                    )
                batch.save(update_fields=batch_update_fields)

                if (order_specific and request_completed) or (
                    not order_specific and task_completed
                ):
                    order = (
                        FbsOrder.objects.select_for_update().get(pk=request.order_id)
                        if order_specific
                        else task.order
                    )
                    if order_specific and not is_quarantine:
                        FbsPickTask.objects.filter(
                            batch=batch,
                            order=order,
                            picked_qty=0,
                        ).update(
                            status=FbsPickTask.STATUS_CANCELED,
                            canceled_at=now,
                            updated_at=now,
                        )
                        assignment = (
                            FbsHandoverOrderAssignment.objects.select_for_update()
                            .filter(pk=request.handover_assignment_id)
                            .first()
                        )
                        if assignment is not None:
                            assignment.status = FbsHandoverOrderAssignment.STATUS_CANCELED
                            assignment.error = request.reason
                            assignment.save(update_fields=["status", "error", "updated_at"])
                        handover_link = (
                            FbsHandoverOrder.objects.select_for_update()
                            .filter(order=order, box__batch_id=assignment.batch_id)
                            .first()
                            if assignment is not None
                            else None
                        )
                        if handover_link is not None:
                            handover_link.status = FbsHandoverOrder.STATUS_EXCLUDED
                            handover_link.exclusion_reason = request.reason
                            handover_link.excluded_by = actor
                            handover_link.excluded_at = now
                            handover_link.verified_label = None
                            handover_link.verified_by = None
                            handover_link.verified_at = None
                            handover_link.save(
                                update_fields=[
                                    "status",
                                    "exclusion_reason",
                                    "excluded_by",
                                    "excluded_at",
                                    "verified_label",
                                    "verified_by",
                                    "verified_at",
                                ]
                            )
                    previous_status = order.internal_status
                    preserve_not_found_rewave = bool(
                        request.reason_code
                        == FbsPickRestockRequest.REASON_WRONG_PRODUCT
                        and order.internal_status
                        in {
                            FbsOrder.STATUS_VALIDATION_FAILED,
                            FbsOrder.STATUS_AWAITING_STOCK,
                            FbsOrder.STATUS_RESERVED,
                            FbsOrder.STATUS_QUEUED_FOR_PICK,
                            FbsOrder.STATUS_PICKING,
                            FbsOrder.STATUS_PICKED,
                            FbsOrder.STATUS_READY_FOR_HANDOVER,
                        }
                    )
                    if is_quarantine:
                        order.internal_status = FbsOrder.STATUS_EXCEPTION
                        order.hold_reason = "verification_problem_quarantined"
                        order.problem_reason = (
                            f"Товар помещен в недоступный карантинный FBS-короб "
                            f"{destination_box.box_code} по заданию #{request.id}. "
                            f"Причина: {request.reason}"
                        )
                        order.save(
                            update_fields=[
                                "internal_status",
                                "hold_reason",
                                "problem_reason",
                                "updated_at",
                            ]
                        )
                    elif not preserve_not_found_rewave:
                        order.internal_status = FbsOrder.STATUS_CANCELLED
                        order.hold_reason = ""
                        order.problem_reason = (
                            f"Отбор возвращен в исходный FBS-короб по заданию #{request.id}. "
                            f"Причина: {request.reason}"
                        )
                        order.save(
                            update_fields=[
                                "internal_status",
                                "hold_reason",
                                "problem_reason",
                                "updated_at",
                            ]
                        )
                        log_order_bulk_transition(
                            [order],
                            previous_internal_status={order.pk: previous_status},
                            internal_status=FbsOrder.STATUS_CANCELLED,
                            user=actor,
                            source=(
                                "complete_order_pick_restock"
                                if order_specific
                                else "complete_pick_restock_unit"
                            ),
                            occurred_at=now,
                        )

                location = destination_location
                WarehouseEvent.objects.create(
                    agency=restored_balance.agency,
                    event_type=(
                        "fbs_pick_quarantined"
                        if is_quarantine
                        else "fbs_pick_restocked"
                    ),
                    stock_context_type="fbs_pick_restock",
                    stock_context_id=str(request.id),
                    container=destination_box.source_container,
                    source_document_type="fbs_pick_restock",
                    source_document_id=str(request.id),
                    to_location=location,
                    to_zone_code=str(location.zone_code or ""),
                    qty=1,
                    payload={
                        "request_id": request.id,
                        "batch_id": batch.id,
                        "line_id": line.id,
                        "allocation_id": allocation.id,
                        "order_id": task.order_id,
                        "source_box_id": line.source_box_id,
                        "source_cell_id": line.source_cell_id,
                        "destination_box_id": destination_box.id,
                        "destination_event_id": state.get("destination_event_id"),
                        "destination_cell_id": destination_cell.id,
                        "source_tote_id": request.source_tote_id,
                        "quarantine": is_quarantine,
                        "barcode": restored_balance.barcode,
                        "marking_code": restored_balance.marking_code,
                        "scan_mode": "product_barcode",
                        "scanned_marking_code": "",
                        "restored_marking_code": restored_balance.marking_code,
                    },
                    performed_by=actor,
                    performed_by_role="picker",
                    occurred_at=now,
                )
                if (
                    request.status == FbsPickRestockRequest.STATUS_COMPLETED
                    and request.reason_code
                    == FbsPickRestockRequest.REASON_WRONG_PRODUCT
                    and not is_quarantine
                ):
                    requeue_not_found_order(
                        request_id=request.id,
                        performed_by=actor,
                    )
                if is_quarantine:
                    message = (
                        "Единица помещена в карантинный FBS-короб. "
                        "Доступный остаток не увеличен."
                    )
                else:
                    message = (
                        "Единица возвращена в исходный FBS-короб. "
                        "ЧЗ восстановлен по составу короба."
                        if balance.marking_code
                        else "Единица возвращена в подтверждённый FBS-короб."
                    )

            event = FbsPickRestockScan.objects.create(
                request=request,
                line=line,
                stage=stage,
                result=FbsPickRestockScan.RESULT_SUCCESS,
                scan_value=scan_value,
                expected_value=expected_value,
                quantity_after=request.returned_qty,
                message=message,
                request_token=token,
                created_by=actor,
            )
            if (
                stage == FbsPickRestockScan.STAGE_ITEM
                and request.order_id is not None
                and request.status == FbsPickRestockRequest.STATUS_COMPLETED
            ):
                from .picking import refresh_pick_batch_verification

                refresh_pick_batch_verification(batch_id=request.batch_id)
            return FbsPickRestockScanResult(request=request, event=event)
    except IntegrityError:
        duplicate = _existing_scan_result(request_id=request_id, request_token=token)
        if duplicate is not None:
            return duplicate
        raise
    except FbsError as exc:
        _record_error_scan(
            request_id=request_id,
            line_id=line_id,
            stage=stage,
            scan_value=str(scan_value or ""),
            expected_value=expected_value,
            message=str(exc),
            request_token=token,
            actor=actor,
        )
        raise
