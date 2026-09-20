from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from uuid import uuid4

from django.db import transaction
from django.db.models import F
from django.db.models import Count, Q, Sum
from django.utils import timezone

from fbs.exceptions import FbsFeatureDisabled, FbsPickingError, FbsScanMismatchError
from fbs.flags import feature_enabled
from fbs.integrations.contracts import WB_ADD_ORDER_TO_HANDOVER
from fbs.models import (
    FbsControllerPickTote,
    FbsControllerSession,
    FbsHandoverOrderAssignment,
    FbsIntegrationProfile,
    FbsMarketplaceCommand,
    FbsProblemToteItem,
    FbsOrder,
    FbsOrderLabel,
    FbsOrderStockAllocation,
    FbsOrderTraceability,
    FbsMarketplaceMetadataTransfer,
    FbsBox,
    FbsPickBatch,
    FbsPickException,
    FbsPickRestockLine,
    FbsPickRestockRequest,
    FbsPickRestockScan,
    FbsPickScanEvent,
    FbsPickTask,
    FbsPallet,
    FbsStockBalance,
    FbsStorageCell,
    FbsToteMovement,
)
from fbs.services.scanning import record_pick_scan_event
from processing_app.models import ProcessingPrintJob


@dataclass(frozen=True)
class FbsClientOperationsRow:
    agency_id: int
    agency_name: str
    open_orders: int
    queued_waves: int
    active_waves: int
    completed_orders: int
    overdue_orders: int
    problem_orders: int
    overdue_qty: int


@dataclass(frozen=True)
class FbsVerificationProblemResult:
    issue: FbsPickException
    batch_id: int
    order_id: int
    problem_box_id: int
    problem_box_code: str
    problem_cell_code: str
    moved_qty: int


@dataclass(frozen=True)
class FbsPickMissingQuantityResult:
    requested_qty: int
    released_qty: int
    affected_order_count: int
    source_quarantined: bool
    quarantined_available_qty: int
    issues: tuple[FbsPickException, ...]


@dataclass(frozen=True)
class FbsProblemToteLookupResult:
    item: FbsProblemToteItem | None
    message: str
    problem_tote_code: str
    target_check_tote_code: str

    @property
    def can_return(self) -> bool:
        return bool(
            self.item is not None
            and self.item.severity == FbsProblemToteItem.SEVERITY_NONCRITICAL
        )


PROBLEM_TOTE_CRITICAL_MESSAGE = (
    "Товар в проблемной таре имеет критическую проблему. Использовать нельзя"
)
PROBLEM_TOTE_NOT_FOUND_MESSAGE = (
    "Обратитесь к кладовщику (или непосредственному руководителю) для решения "
    "вопроса: товар был отсканирован, но не найден в коробе отгрузки"
)


def _require_writes() -> None:
    if not feature_enabled("module"):
        raise FbsFeatureDisabled("Модуль FBS выключен.")
    if not feature_enabled("warehouse_writes"):
        raise FbsFeatureDisabled("Складские операции FBS выключены.")


def _authenticated_user(user):
    return user if getattr(user, "is_authenticated", False) else None


def _source_protected_quantities(allocation):
    return {
        str(row.id): max(int(row.qty or 0) - int(row.available_qty or 0) - int(row.reserved_qty or 0), 0)
        for row in FbsStockBalance.objects.select_for_update().filter(
            agency_id=allocation.balance.agency_id, box_id=allocation.balance.box_id,
            barcode=allocation.balance.barcode,
        ).order_by("id")
    }


def _quarantine_pick_source_availability(
    allocation: FbsOrderStockAllocation,
) -> int:
    """Keep reported source stock unavailable until a warehouse recount."""
    balances = list(
        FbsStockBalance.objects.select_for_update()
        .filter(
            agency_id=allocation.balance.agency_id,
            box_id=allocation.balance.box_id,
            barcode=allocation.balance.barcode,
            available_qty__gt=0,
        )
        .order_by("id")
    )
    quarantined_qty = sum(int(balance.available_qty or 0) for balance in balances)
    now = timezone.now()
    for balance in balances:
        balance.available_qty = 0
        balance.updated_at = now
    if balances:
        FbsStockBalance.objects.bulk_update(
            balances,
            ["available_qty", "updated_at"],
        )
    return quarantined_qty


def _problem_tote_lookup_context(*, allocation_id: int, actor):
    selected = (
        FbsOrderStockAllocation.objects.select_related(
            "pick_task__batch",
            "order_item",
            "balance",
        )
        .filter(pk=allocation_id)
        .first()
    )
    if selected is None or selected.pick_task_id is None:
        raise FbsPickingError("Позиция не относится к волне FBS.")
    batch = selected.pick_task.batch
    if batch.verification_assigned_to_id != actor.id:
        raise FbsPickingError("Проверка волны назначена другому контролеру.")
    pick_context = (
        FbsControllerPickTote.objects.select_related(
            "session__problem_tote",
            "check_tote__tote",
            "tote",
        )
        .filter(
            pick_batch=batch,
            session__controller=actor,
            session__status="active",
            status__in=(
                FbsControllerPickTote.STATUS_PROCESSING,
                FbsControllerPickTote.STATUS_AWAITING_EMPTY,
            ),
        )
        .order_by("-id")
        .first()
    )
    if pick_context is None:
        raise FbsPickingError("Активная тара подбора этого заказа не найдена.")
    if pick_context.session.problem_tote_id is None:
        raise FbsPickingError("Сначала привяжите тару проблемных заказов.")
    return selected, pick_context


def _problem_tote_item_query(*, selected, pick_context):
    scan_values = {
        str(selected.balance.barcode or "").strip(),
        str(selected.order_item.barcode or "").strip(),
        str(selected.order_item.external_sku or "").strip(),
    }
    scan_values.discard("")
    identity_query = Q(order_item_id=selected.order_item_id)
    for value in sorted(scan_values):
        identity_query |= Q(scanned_value__iexact=value)
    return FbsProblemToteItem.objects.filter(
        session=pick_context.session,
        problem_tote=pick_context.session.problem_tote,
        status=FbsProblemToteItem.STATUS_IN_TOTE,
    ).filter(identity_query)


def lookup_problem_tote_item(
    *, allocation_id: int, actor
) -> FbsProblemToteLookupResult:
    authenticated_actor = _authenticated_user(actor)
    if authenticated_actor is None:
        raise FbsPickingError(
            "Для проверки проблемной тары нужен авторизованный контролер."
        )
    selected, pick_context = _problem_tote_lookup_context(
        allocation_id=allocation_id,
        actor=authenticated_actor,
    )
    item = (
        _problem_tote_item_query(selected=selected, pick_context=pick_context)
        .select_related("problem_tote")
        .order_by("severity", "reported_at", "id")
        .first()
    )
    has_physical_check_tote = bool(pick_context.check_tote.tote_id)
    target_code = (
        pick_context.check_tote.tote.barcode
        if has_physical_check_tote
        else pick_context.tote.barcode
    )
    if item is None:
        message = PROBLEM_TOTE_NOT_FOUND_MESSAGE
    elif item.severity == FbsProblemToteItem.SEVERITY_CRITICAL:
        message = PROBLEM_TOTE_CRITICAL_MESSAGE
    else:
        target_label = (
            "тару проверки" if has_physical_check_tote else "исходную тару подбора"
        )
        message = (
            "Товар найден в проблемной таре. Проблема некритическая. "
            f"Переложите товар в {target_label} {target_code}"
        )
    return FbsProblemToteLookupResult(
        item=item,
        message=message,
        problem_tote_code=pick_context.session.problem_tote.barcode,
        target_check_tote_code=target_code,
    )


@transaction.atomic
def return_problem_tote_item_to_check_tote(
    *,
    allocation_id: int,
    problem_item_id: int,
    problem_tote_scan: str,
    returned_by,
) -> FbsProblemToteItem:
    _require_writes()
    actor = _authenticated_user(returned_by)
    if actor is None:
        raise FbsPickingError("Для возврата товара нужен авторизованный контролер.")
    selected, pick_context = _problem_tote_lookup_context(
        allocation_id=allocation_id,
        actor=actor,
    )
    session = (
        FbsControllerSession.objects.select_for_update()
        .filter(
            pk=pick_context.session_id,
            controller=actor,
            status=FbsControllerSession.STATUS_ACTIVE,
        )
        .first()
    )
    if session is None:
        raise FbsPickingError("Смена контролера уже закрыта.")
    if session.problem_tote_id != pick_context.session.problem_tote_id:
        raise FbsPickingError("Проблемная тара смены была изменена. Повторите проверку.")
    pick_context.session = session
    item = (
        _problem_tote_item_query(selected=selected, pick_context=pick_context)
        .select_for_update()
        .filter(pk=problem_item_id)
        .first()
    )
    if item is None:
        raise FbsPickingError(PROBLEM_TOTE_NOT_FOUND_MESSAGE)
    if item.severity == FbsProblemToteItem.SEVERITY_CRITICAL:
        raise FbsPickingError(PROBLEM_TOTE_CRITICAL_MESSAGE)
    scanned_tote = str(problem_tote_scan or "").strip()
    expected_tote = str(item.problem_tote.barcode or "").strip()
    if scanned_tote.casefold() != expected_tote.casefold():
        raise FbsPickingError(
            f"Отсканируйте проблемную тару {item.problem_tote.barcode}."
        )
    now = timezone.now()
    item.status = FbsProblemToteItem.STATUS_RETURNED
    item.resolved_by = actor
    item.resolved_at = now
    item.save(update_fields=["status", "resolved_by", "resolved_at"])
    has_physical_check_tote = bool(pick_context.check_tote.tote_id)
    target_code = (
        pick_context.check_tote.tote.barcode
        if has_physical_check_tote
        else pick_context.tote.barcode
    )
    target_kind = "check_tote" if has_physical_check_tote else "pick_tote"
    FbsToteMovement.objects.create(
        tote=item.problem_tote,
        action=FbsToteMovement.ACTION_PLACE,
        source_kind="problem_tote",
        source_code=item.problem_tote.barcode,
        target_kind=target_kind,
        target_code=target_code,
        pick_batch=pick_context.pick_batch,
        controller_session=pick_context.session,
        quantity=item.quantity,
        details={
            "problem_item_id": item.id,
            "allocation_id": selected.id,
            "reason": item.reason,
            "severity": item.severity,
            "returned_to_check_tote": has_physical_check_tote,
            "returned_to_pick_tote": not has_physical_check_tote,
        },
        performed_by=actor,
    )
    return item


def _normalized_cell_scan(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
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


def _cell_scan_values(cell: FbsStorageCell) -> set[str]:
    location = cell.location
    values = {
        str(cell.cell_code or ""),
        str(location.location_code or ""),
        str(location.display_name or ""),
    }
    if all(
        int(getattr(location, field, 0) or 0) > 0
        for field in ("row_no", "section_no", "tier_no", "cell_no")
    ):
        values.add(
            f"{location.row_no}-{location.section_no}-{location.tier_no}-{location.cell_no}"
        )
    return {_normalized_cell_scan(value) for value in values if value}


def _assert_problem_box_is_quarantined(box: FbsBox) -> None:
    if FbsStockBalance.objects.filter(box=box).filter(
        Q(available_qty__gt=0) | Q(reserved_qty__gt=0)
    ).exists():
        raise FbsPickingError(
            "В этом FBS-коробе есть доступный или зарезервированный товар. "
            "Для проблемы отсканируйте отдельный пустой проблемный короб или ячейку."
        )


def _new_problem_box(*, cell: FbsStorageCell, agency_id: int) -> FbsBox:
    suffix = uuid4().hex[:10].upper()
    pallet = FbsPallet.objects.create(
        agency_id=agency_id,
        pallet_code=f"FBS-PROBLEM-PAL-{agency_id}-{cell.id}-{suffix}",
        cell=cell,
        max_boxes=10,
        status=FbsPallet.STATUS_ACTIVE,
    )
    return FbsBox.objects.create(
        agency_id=agency_id,
        pallet=pallet,
        box_code=f"FBS-PROBLEM-{agency_id}-{cell.id}-{suffix}",
        status=FbsBox.STATUS_ACTIVE,
    )


def _resolve_problem_box(*, agency_id: int, problem_place_scan: str) -> FbsBox:
    scan = str(problem_place_scan or "").strip()
    if not scan:
        raise FbsPickingError("Отсканируйте QR проблемного короба или FBS-ячейки.")

    direct_boxes = list(
        FbsBox.objects.select_for_update()
        .select_related("pallet__cell__location")
        .filter(
            agency_id=agency_id,
            box_code__iexact=scan,
            status=FbsBox.STATUS_ACTIVE,
            pallet__status=FbsPallet.STATUS_ACTIVE,
            pallet__cell__is_active=True,
            pallet__cell__location__is_active=True,
        )[:2]
    )
    if len(direct_boxes) == 1:
        box = direct_boxes[0]
        _assert_problem_box_is_quarantined(box)
        return box

    normalized_scan = _normalized_cell_scan(scan)
    cells = list(
        FbsStorageCell.objects.select_for_update()
        .select_related("location")
        .filter(is_active=True, location__is_active=True)
        .order_by("id")
    )
    matched_cells = [cell for cell in cells if normalized_scan in _cell_scan_values(cell)]
    if len(matched_cells) != 1:
        raise FbsPickingError("QR проблемного места не найден или определен неоднозначно.")
    cell = matched_cells[0]
    pallets = list(
        FbsPallet.objects.select_for_update()
        .filter(
            cell=cell,
            status__in=(FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE),
        )
        .order_by("id")
    )
    if not pallets:
        return _new_problem_box(cell=cell, agency_id=agency_id)
    if len(pallets) != 1 or pallets[0].agency_id != agency_id:
        raise FbsPickingError("Проблемное место занято FBS-паллетой другого клиента.")
    pallet = pallets[0]
    boxes = list(
        FbsBox.objects.select_for_update()
        .filter(
            pallet=pallet,
            agency_id=agency_id,
            status__in=(FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE),
        )
        .order_by("id")
    )
    for box in boxes:
        _assert_problem_box_is_quarantined(box)
    if boxes:
        box = boxes[0]
        if pallet.status != FbsPallet.STATUS_ACTIVE:
            pallet.status = FbsPallet.STATUS_ACTIVE
            pallet.save(update_fields=["status", "updated_at"])
        if box.status != FbsBox.STATUS_ACTIVE:
            box.status = FbsBox.STATUS_ACTIVE
            box.save(update_fields=["status", "updated_at"])
        return box
    suffix = uuid4().hex[:10].upper()
    if pallet.status != FbsPallet.STATUS_ACTIVE:
        pallet.status = FbsPallet.STATUS_ACTIVE
        pallet.save(update_fields=["status", "updated_at"])
    return FbsBox.objects.create(
        agency_id=agency_id,
        pallet=pallet,
        box_code=f"FBS-PROBLEM-{agency_id}-{cell.id}-{suffix}",
        status=FbsBox.STATUS_ACTIVE,
    )


def _problem_identity_key(*, balance: FbsStockBalance, trace) -> str:
    expiry_date = getattr(trace, "expiry_date", None) or balance.expiry_date
    payload = {
        "sku_ref_id": balance.sku_ref_id,
        "sku_code": balance.sku_code,
        "size": balance.size,
        "barcode": balance.barcode,
        "goods_type": balance.goods_type,
        "marking_code": balance.marking_code,
        "lot_code": getattr(trace, "lot_code", "") or balance.lot_code,
        "expiry_date": expiry_date.isoformat() if expiry_date else "",
        "availability": "problem",
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _restore_picked_qty_to_problem_box(
    *, allocation: FbsOrderStockAllocation, balance: FbsStockBalance, problem_box: FbsBox
) -> int:
    qty = int(allocation.qty_picked or 0)
    if qty <= 0:
        return 0
    trace = getattr(allocation, "traceability", None)
    expiry_date = getattr(trace, "expiry_date", None) or balance.expiry_date
    lot_code = getattr(trace, "lot_code", "") or balance.lot_code
    identity_key = _problem_identity_key(balance=balance, trace=trace)

    if balance.marking_code:
        if qty != 1 or int(balance.qty or 0) != 0 or int(balance.reserved_qty or 0) != 0:
            raise FbsPickingError("Состояние FBS-остатка КИЗ не позволяет поместить его в проблему.")
        balance.box = problem_box
        balance.identity_key = identity_key
        balance.lot_code = lot_code
        balance.expiry_date = expiry_date
        balance.qty = 1
        balance.available_qty = 0
        balance.save(
            update_fields=[
                "box",
                "identity_key",
                "lot_code",
                "expiry_date",
                "qty",
                "available_qty",
                "updated_at",
            ]
        )
        return 1

    target, _created = FbsStockBalance.objects.select_for_update().get_or_create(
        box=problem_box,
        identity_key=identity_key,
        defaults={
            "agency_id": balance.agency_id,
            "sku_ref_id": balance.sku_ref_id,
            "sku_code": balance.sku_code,
            "name": balance.name,
            "size": balance.size,
            "barcode": balance.barcode,
            "goods_type": balance.goods_type,
            "marking_code": "",
            "lot_code": lot_code,
            "expiry_date": expiry_date,
            "qty": 0,
            "available_qty": 0,
            "reserved_qty": 0,
        },
    )
    target.qty = F("qty") + qty
    target.save(update_fields=["qty", "updated_at"])
    return qty


def restore_restock_unit_to_problem_box(
    *,
    allocation: FbsOrderStockAllocation,
    balance: FbsStockBalance,
    problem_box: FbsBox,
) -> FbsStockBalance:
    """Restore one physically scanned unit into unavailable quarantine stock."""
    if int(allocation.qty_picked or 0) <= 0:
        raise FbsPickingError("Эта единица уже возвращена.")
    _assert_problem_box_is_quarantined(problem_box)
    trace = getattr(allocation, "traceability", None)
    expiry_date = getattr(trace, "expiry_date", None) or balance.expiry_date
    lot_code = getattr(trace, "lot_code", "") or balance.lot_code
    identity_key = _problem_identity_key(balance=balance, trace=trace)

    if balance.marking_code:
        if int(balance.qty or 0) != 0 or int(balance.reserved_qty or 0) != 0:
            raise FbsPickingError("Состояние FBS-остатка КИЗ не позволяет вернуть его в карантин.")
        balance.box = problem_box
        balance.identity_key = identity_key
        balance.lot_code = lot_code
        balance.expiry_date = expiry_date
        balance.qty = 1
        balance.available_qty = 0
        balance.full_clean()
        balance.save(
            update_fields=[
                "box",
                "identity_key",
                "lot_code",
                "expiry_date",
                "qty",
                "available_qty",
                "updated_at",
            ]
        )
        return balance

    target, _created = FbsStockBalance.objects.select_for_update().get_or_create(
        box=problem_box,
        identity_key=identity_key,
        defaults={
            "agency_id": balance.agency_id,
            "sku_ref_id": balance.sku_ref_id,
            "sku_code": balance.sku_code,
            "name": balance.name,
            "size": balance.size,
            "barcode": balance.barcode,
            "goods_type": balance.goods_type,
            "marking_code": "",
            "lot_code": lot_code,
            "expiry_date": expiry_date,
            "qty": 0,
            "available_qty": 0,
            "reserved_qty": 0,
        },
    )
    target.qty = int(target.qty or 0) + 1
    target.full_clean()
    target.save(update_fields=["qty", "updated_at"])
    return target


@transaction.atomic
def queue_verification_problem_restock(
    *,
    allocation_id: int,
    exception_type: str,
    reason: str,
    service_tote_scan: str,
    reported_by,
    order_scan: str = "",
    product_scans=(),
) -> FbsPickRestockRequest:
    """Put a controller problem into its service tote and queue picker restock."""
    _require_writes()
    actor = _authenticated_user(reported_by)
    if actor is None:
        raise FbsPickingError("Для фиксации проблемы нужен авторизованный контролер.")
    clean_product_scans = [
        str(value or "").strip()
        for value in (product_scans or ())
        if str(value or "").strip()
    ]
    clean_service_tote_scan = str(service_tote_scan or "").strip()
    if exception_type not in dict(FbsPickException.TYPE_CHOICES):
        raise FbsPickingError("Выберите причину проблемы товара.")

    selected = (
        FbsOrderStockAllocation.objects.select_for_update(of=("self",))
        .select_related("pick_task")
        .get(pk=allocation_id)
    )
    if selected.pick_task_id is None:
        raise FbsPickingError("Позиция не относится к волне FBS.")
    task = (
        FbsPickTask.objects.select_for_update()
        .select_related("batch", "order__profile")
        .get(pk=selected.pick_task_id)
    )
    batch = FbsPickBatch.objects.select_for_update().get(pk=task.batch_id)
    order = task.order
    if batch.verification_assigned_to_id != actor.id:
        raise FbsPickingError("Проверка волны назначена другому контролеру.")
    from .pick_restock import order_is_client_canceled_by_marketplace
    from .totes import (
        SERVICE_TOTE_CANCELED,
        SERVICE_TOTE_PROBLEM,
        controller_service_tote_for_actor,
    )

    is_client_canceled = order_is_client_canceled_by_marketplace(order)
    if not clean_service_tote_scan:
        raise FbsPickingError("Отсканируйте назначенную служебную тару.")
    service_tote_purpose = (
        SERVICE_TOTE_CANCELED if is_client_canceled else SERVICE_TOTE_PROBLEM
    )
    pick_context_session_id = (
        FbsControllerPickTote.objects.filter(pick_batch=batch)
        .values_list("session_id", flat=True)
        .first()
    )
    if pick_context_session_id is None:
        raise FbsPickingError("Активная тара подбора этого заказа не найдена.")
    session, service_tote = controller_service_tote_for_actor(
        actor=actor,
        purpose=service_tote_purpose,
        session_id=pick_context_session_id,
    )
    if clean_service_tote_scan.casefold() != service_tote.barcode.casefold():
        tote_label = (
            "тару отмененных заказов"
            if is_client_canceled
            else "проблемную тару"
        )
        raise FbsPickingError(
            f"Неверная служебная тара. Отсканируйте {tote_label} "
            f"{service_tote.barcode}."
        )
    if batch.workstation_id != session.workstation_id:
        raise FbsPickingError("Служебная тара привязана к другому рабочему месту.")
    pick_context = (
        FbsControllerPickTote.objects.select_for_update()
        .select_related("tote")
        .filter(pick_batch=batch, session=session)
        .first()
    )
    if pick_context is None:
        raise FbsPickingError("Активная тара подбора этого заказа не найдена.")

    existing = (
        FbsPickRestockRequest.objects.select_for_update()
        .filter(
            order_id=task.order_id,
            status__in=(
                FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
                FbsPickRestockRequest.STATUS_QUEUED,
                FbsPickRestockRequest.STATUS_IN_PROGRESS,
                FbsPickRestockRequest.STATUS_FAILED,
            ),
        )
        .first()
    )
    if existing is not None:
        if existing.source_tote_id != service_tote.id:
            raise FbsPickingError(
                "Возврат заказа уже связан с другой служебной тарой."
            )
        return existing

    if batch.status != FbsPickBatch.STATUS_VERIFICATION or batch.picking_completed_at is None:
        raise FbsPickingError("Волна еще не передана на проверку.")
    if task.status != FbsPickTask.STATUS_PICKED:
        raise FbsPickingError("Проблему можно зафиксировать только у отобранного заказа.")
    if FbsOrderLabel.objects.filter(
        order_id=task.order_id,
        status=FbsOrderLabel.STATUS_APPLIED,
    ).exists():
        raise FbsPickingError("Этикетка заказа уже подтверждена. Передайте заказ кладовщику.")
    if FbsMarketplaceMetadataTransfer.objects.filter(
        order_item__order_id=task.order_id,
        status__in=(
            FbsMarketplaceMetadataTransfer.STATUS_SENT,
            FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED,
        ),
    ).exists():
        raise FbsPickingError(
            "КИЗ или срок уже передан marketplace. Заказ требует решения кладовщика."
        )

    allocations = list(
        FbsOrderStockAllocation.objects.select_for_update(of=("self",))
        .select_related("traceability", "balance__box__pallet__cell")
        .filter(pick_task=task)
        .order_by("id")
    )
    open_shortage_issue = (
        FbsPickException.objects.select_for_update()
        .filter(
            task=task,
            exception_type=FbsPickException.TYPE_NOT_FOUND,
            status=FbsPickException.STATUS_OPEN,
        )
        .order_by("id")
        .first()
    )
    picked_allocations = [
        row
        for row in allocations
        if row.status == FbsOrderStockAllocation.STATUS_PICKED
        and int(row.qty_picked or 0) == int(row.qty_reserved or 0)
        and int(row.qty_picked or 0) > 0
    ]
    allowed_shortage_rows = bool(open_shortage_issue) and all(
        row in picked_allocations
        or (
            row.status == FbsOrderStockAllocation.STATUS_RELEASED
            and int(row.qty_picked or 0) == 0
        )
        for row in allocations
    )
    if (
        not picked_allocations
        or (
            not allowed_shortage_rows
            and len(picked_allocations) != len(allocations)
        )
    ):
        raise FbsPickingError("Не все товары заказа физически собраны для возврата.")

    matched_product_scans = []
    if is_client_canceled:
        expected_units = [
            row
            for row in picked_allocations
            for _ in range(int(row.qty_picked or 0))
        ]
        expected_qty = len(expected_units)
        if len(clean_product_scans) != expected_qty:
            raise FbsPickingError(
                "Отсканируйте все товары отмененного заказа: "
                f"нужно {expected_qty} шт., получено {len(clean_product_scans)}."
            )
        unmatched_units = list(expected_units)
        from .picking import resolve_verification_item_scan

        for scan_index, product_scan in enumerate(clean_product_scans, start=1):
            match = None
            for unit_index, candidate in enumerate(unmatched_units):
                try:
                    matched_barcode, marking_scan = resolve_verification_item_scan(
                        candidate,
                        product_scan,
                    )
                except FbsScanMismatchError:
                    continue
                if matched_barcode:
                    match = (
                        unmatched_units.pop(unit_index),
                        matched_barcode,
                        marking_scan,
                    )
                    break
            if match is None:
                raise FbsPickingError(
                    f"Скан {scan_index} не относится к товарам отмененного заказа. "
                    "Проверьте товар и повторите все сканы."
                )
            matched_allocation, matched_barcode, marking_scan = match
            matched_product_scans.append(
                (matched_allocation, product_scan, matched_barcode, marking_scan)
            )

    clean_reason = "" if is_client_canceled else str(reason or "").strip()
    reason_code = (
        FbsPickRestockRequest.REASON_CLIENT_CANCELED
        if is_client_canceled
        else {
            FbsPickException.TYPE_DAMAGED: FbsPickRestockRequest.REASON_DAMAGED,
            FbsPickException.TYPE_BARCODE: FbsPickRestockRequest.REASON_METADATA,
            FbsPickException.TYPE_NOT_FOUND: FbsPickRestockRequest.REASON_WRONG_PRODUCT,
        }.get(exception_type, FbsPickRestockRequest.REASON_OTHER)
    )
    reason_text = (
        "Заказ отменен маркетплейсом"
        if is_client_canceled
        else dict(FbsPickException.TYPE_CHOICES)[exception_type]
    )
    if clean_reason:
        reason_text = f"{reason_text}: {clean_reason}"

    handover_assignment = (
        FbsHandoverOrderAssignment.objects.select_for_update()
        .select_related("batch")
        .filter(order_id=order.id)
        .first()
    )
    wb_add_commands = list(
        FbsMarketplaceCommand.objects.select_for_update()
        .filter(
            order_id=order.id,
            command_type=WB_ADD_ORDER_TO_HANDOVER,
        )
        .order_by("id")
    )
    is_wb_order = (
        order.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
    )
    marketplace_may_contain_order = is_wb_order and (
        bool(
            handover_assignment is not None
            and handover_assignment.status
            == FbsHandoverOrderAssignment.STATUS_CONFIRMED
        )
        or any(
            command.status
            in {
                FbsMarketplaceCommand.STATUS_SENT,
                FbsMarketplaceCommand.STATUS_CONFIRMED,
                FbsMarketplaceCommand.STATUS_CONFLICT,
            }
            for command in wb_add_commands
        )
    )
    if marketplace_may_contain_order and not is_client_canceled:
        raise FbsPickingError(
            "Заказ уже передан или передается в поставку WB. "
            "Исключите его из карточки отгрузки: отмена продавцом может привести к штрафу."
        )
    wait_for_marketplace = bool(
        is_client_canceled
        and marketplace_may_contain_order
        and handover_assignment is not None
    )

    planned_qty = sum(int(row.qty_picked or 0) for row in picked_allocations)
    request = FbsPickRestockRequest.objects.create(
        batch=batch,
        order=order,
        handover_assignment=handover_assignment,
        source_tote=service_tote,
        status=(
            FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE
            if wait_for_marketplace
            else FbsPickRestockRequest.STATUS_QUEUED
        ),
        reason_code=reason_code,
        reason=reason_text,
        marketplace_action=(
            FbsPickRestockRequest.MARKETPLACE_ACTION_VERIFY_CANCEL
            if wait_for_marketplace
            else FbsPickRestockRequest.MARKETPLACE_ACTION_NONE
        ),
        planned_qty=planned_qty,
        created_by=actor,
    )
    FbsPickRestockLine.objects.bulk_create(
        [
            FbsPickRestockLine(
                request=request,
                allocation=row,
                source_balance=row.balance,
                source_box=row.balance.box,
                source_cell=row.balance.box.pallet.cell,
                planned_qty=int(row.qty_picked or 0),
            )
            for row in picked_allocations
        ]
    )
    if is_client_canceled:
        lines_by_allocation = {
            line.allocation_id: line
            for line in FbsPickRestockLine.objects.filter(request=request)
        }
        scanned_by_allocation = defaultdict(int)
        for matched_allocation, product_scan, matched_barcode, marking_scan in matched_product_scans:
            scanned_by_allocation[matched_allocation.id] += 1
            FbsPickRestockScan.objects.create(
                request=request,
                line=lines_by_allocation[matched_allocation.id],
                stage=FbsPickRestockScan.STAGE_PICKUP_ITEM,
                result=FbsPickRestockScan.RESULT_SUCCESS,
                scan_value=marking_scan or product_scan,
                expected_value=matched_barcode,
                quantity_after=scanned_by_allocation[matched_allocation.id],
                message="Товар отмененного заказа подтвержден контролером для служебной тары.",
                created_by=actor,
            )
    scan_confirmation = (
        f"Контролер подтвердил сканами все товары заказа "
        f"({len(matched_product_scans)} шт.) и "
        f"тару отмененных заказов {service_tote.barcode}."
        if is_client_canceled
        else f"Контролер подтвердил сканом служебную тару {service_tote.barcode}."
    )
    controller_issue_reason = f"{scan_confirmation} {clean_reason}".strip()
    if open_shortage_issue is not None:
        issue = open_shortage_issue
        if controller_issue_reason not in str(issue.reason or ""):
            issue.reason = " ".join(
                value
                for value in (str(issue.reason or "").strip(), controller_issue_reason)
                if value
            )
            issue.save(update_fields=["reason", "updated_at"])
    else:
        issue = FbsPickException(
            task=task,
            allocation=selected,
            exception_type=exception_type,
            reason=controller_issue_reason,
            created_by=actor,
        )
        issue.full_clean()
        issue.save()

    now = timezone.now()
    cancellation_note = (
        f"Заказ исключен контролером во время проверки: {reason_text}"
    )
    if handover_assignment is not None and not marketplace_may_contain_order:
        locally_cancellable_assignment_statuses = {
            FbsHandoverOrderAssignment.STATUS_PENDING,
            FbsHandoverOrderAssignment.STATUS_ERROR,
        }
        if is_client_canceled and not is_wb_order:
            locally_cancellable_assignment_statuses.add(
                FbsHandoverOrderAssignment.STATUS_CONFIRMED
            )
        if handover_assignment.status in locally_cancellable_assignment_statuses:
            handover_assignment.status = FbsHandoverOrderAssignment.STATUS_CANCELED
            handover_assignment.error = cancellation_note
            handover_assignment.confirmed_at = None
            handover_assignment.save(
                update_fields=["status", "error", "confirmed_at", "updated_at"]
            )
        for command in wb_add_commands:
            if command.status not in {
                FbsMarketplaceCommand.STATUS_PENDING,
                FbsMarketplaceCommand.STATUS_RETRY,
            }:
                continue
            command.status = FbsMarketplaceCommand.STATUS_CANCELLED
            command.error = cancellation_note
            command.next_attempt_at = None
            command.save(
                update_fields=["status", "error", "next_attempt_at", "updated_at"]
            )
    canceled_label_ids = list(FbsOrderLabel.objects.filter(
        order_id=task.order_id,
        status__in=(
            FbsOrderLabel.STATUS_REQUESTED,
            FbsOrderLabel.STATUS_READY,
            FbsOrderLabel.STATUS_ERROR,
        ),
    ).values_list("id", flat=True))
    FbsOrderLabel.objects.filter(id__in=canceled_label_ids).update(
        status=FbsOrderLabel.STATUS_CANCELED,
        error=cancellation_note,
        updated_at=now,
    )
    pending_print_jobs = ProcessingPrintJob.objects.filter(
        article=order.external_order_id,
        status=ProcessingPrintJob.STATUS_PENDING,
    )
    if canceled_label_ids:
        label_job_filter = Q()
        for label_id in canceled_label_ids:
            label_job_filter |= (
                Q(card_id__startswith=f"fbs:order-label:{label_id}:")
                | Q(card_id__startswith=f"fbs:ozon-order-qr:{label_id}:")
            )
        pending_print_jobs = pending_print_jobs.filter(label_job_filter)
    else:
        pending_print_jobs = pending_print_jobs.none()
    pending_print_jobs.update(
        status=ProcessingPrintJob.STATUS_FAILED,
        error="Заказ отменен маркетплейсом до применения этикетки; печать остановлена.",
        updated_at=now,
    )
    FbsMarketplaceMetadataTransfer.objects.filter(order_item__order_id=task.order_id).exclude(
        status__in=(
            FbsMarketplaceMetadataTransfer.STATUS_SENT,
            FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED,
        )
    ).update(status=FbsMarketplaceMetadataTransfer.STATUS_CANCELED, updated_at=now)
    task.status = FbsPickTask.STATUS_EXCEPTION
    task.save(update_fields=["status", "updated_at"])
    order.internal_status = FbsOrder.STATUS_EXCEPTION
    order.hold_reason = "pick_restock_pending"
    order.problem_reason = reason_text
    order.save(
        update_fields=["internal_status", "hold_reason", "problem_reason", "updated_at"]
    )
    FbsToteMovement.objects.create(
        tote=service_tote,
        action=FbsToteMovement.ACTION_PLACE,
        source_kind="pick_tote",
        source_code=pick_context.tote.barcode,
        target_kind=("canceled_tote" if is_client_canceled else "problem_tote"),
        target_code=service_tote.barcode,
        pick_batch=batch,
        controller_session=session,
        quantity=planned_qty,
        details={
            "request_id": request.id,
            "order_id": task.order_id,
            "physically_confirmed": True,
            "order_scan": "",
            "order_scan_required": False,
            "product_scan_count": len(matched_product_scans),
            "product_scans_required": is_client_canceled,
            "service_tote_scan": clean_service_tote_scan,
            "service_tote_kind": (
                "canceled_tote" if is_client_canceled else "problem_tote"
            ),
        },
        performed_by=actor,
    )

    from .picking import refresh_pick_batch_verification

    if wait_for_marketplace:
        from .marketplace import schedule_wb_order_exclusion

        schedule_wb_order_exclusion(request_id=request.id, requested_by=actor)

    if not is_client_canceled and exception_type == FbsPickException.TYPE_NOT_FOUND:
        from .pick_restock import requeue_not_found_order

        requeue_not_found_order(request_id=request.id, performed_by=actor)

    refresh_pick_batch_verification(batch_id=batch.id)
    return request


@transaction.atomic
def report_pick_exception(
    *,
    allocation_id: int,
    exception_type: str,
    reason: str,
    reported_by,
) -> FbsPickException:
    _require_writes()
    actor = _authenticated_user(reported_by)
    allocation = (
        FbsOrderStockAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "pick_task__order",
            "pick_task__batch",
            "order_item",
            "balance",
        )
        .get(pk=allocation_id)
    )
    if actor is None or allocation.pick_task_id is None:
        raise FbsPickingError("Позиция не назначена этому сборщику.")
    task = (
        FbsPickTask.objects.select_for_update()
        .select_related("order", "batch")
        .get(pk=allocation.pick_task_id)
    )
    allocation.pick_task = task
    if task.assigned_to_id != actor.id:
        raise FbsPickingError("Позиция не назначена этому сборщику.")
    if exception_type not in dict(FbsPickException.TYPE_CHOICES):
        raise FbsPickingError("Неизвестный тип проблемы отбора.")
    existing = FbsPickException.objects.filter(
        allocation=allocation,
        status=FbsPickException.STATUS_OPEN,
    ).first()
    if existing is not None:
        return existing
    if task.status != FbsPickTask.STATUS_IN_PROGRESS:
        raise FbsPickingError("Проблему можно открыть только по активному заданию.")
    released_order = None
    handled_not_found = False
    partial_shortage = False
    shortage_qty = max(
        int(allocation.qty_reserved or 0) - int(allocation.qty_picked or 0),
        0,
    )
    if exception_type == FbsPickException.TYPE_NOT_FOUND:
        from .picking import (
            release_order_reservation,
            release_pick_allocation_shortage,
        )

        protected_quantities = _source_protected_quantities(allocation)
        _quarantine_pick_source_availability(allocation)
        has_picked_allocations = FbsOrderStockAllocation.objects.filter(
            pick_task=task,
            status=FbsOrderStockAllocation.STATUS_PICKED,
        ).exists()
        if int(task.picked_qty or 0) == 0 and not has_picked_allocations:
            released_order = release_order_reservation(
                order_id=task.order_id,
                released_by=actor,
                cancel_order=False,
                unavailable_qty_by_allocation={allocation.id: shortage_qty},
            )
        else:
            release_pick_allocation_shortage(
                allocation_id=allocation.id,
                released_by=actor,
                missing_qty=shortage_qty,
                return_to_available=False,
            )
            partial_shortage = True
        handled_not_found = True
    issue = FbsPickException(
        task=task,
        allocation=allocation,
        exception_type=exception_type,
        reason=str(reason or "").strip(),
        created_by=actor,
    )
    issue.full_clean()
    issue.save()
    if handled_not_found:
        problem_order = released_order
        if problem_order is None:
            problem_order = FbsOrder.objects.select_for_update().get(pk=task.order_id)
        if partial_shortage:
            sku_code = str(
                allocation.balance.sku_code
                or allocation.order_item.external_sku
                or ""
            ).strip()
            barcode = str(
                allocation.balance.barcode or allocation.order_item.barcode or ""
            ).strip()
            position = ", ".join(
                value
                for value in (
                    f"артикул {sku_code}" if sku_code else "",
                    f"ШК {barcode}" if barcode else "",
                )
                if value
            )
            problem_order.problem_reason = (
                f"Недокомплект: {position or 'позиция заказа'} — "
                f"не найдено {shortage_qty} шт."
            )
        else:
            problem_order.problem_reason = issue.get_exception_type_display()
        if issue.reason:
            separator = " Комментарий: " if partial_shortage else ": "
            problem_order.problem_reason = (
                f"{problem_order.problem_reason}{separator}{issue.reason}"
            )
        problem_order.save(update_fields=["problem_reason", "updated_at"])
        from .inventory_workflow import enqueue_shortage_inventory
        enqueue_shortage_inventory(issues=[issue], reported_by=actor, protected_quantities=protected_quantities)
        return issue
    task.status = FbsPickTask.STATUS_EXCEPTION
    task.save(update_fields=["status", "updated_at"])
    order = task.order
    order.internal_status = FbsOrder.STATUS_EXCEPTION
    order.problem_reason = issue.get_exception_type_display()
    if issue.reason:
        order.problem_reason = f"{order.problem_reason}: {issue.reason}"
    order.save(update_fields=["internal_status", "problem_reason", "updated_at"])
    return issue


def _pick_missing_group_queryset(*, allocation: FbsOrderStockAllocation, actor_id: int):
    return (
        FbsOrderStockAllocation.objects.select_related(
            "pick_task__order",
            "pick_task__batch",
            "order_item",
            "balance__box",
        )
        .filter(
            pick_task__batch_id=allocation.pick_task.batch_id,
            pick_task__assigned_to_id=actor_id,
            pick_task__status=FbsPickTask.STATUS_IN_PROGRESS,
            status__in=(
                FbsOrderStockAllocation.STATUS_RESERVED,
                FbsOrderStockAllocation.STATUS_PICKING,
            ),
            balance__agency_id=allocation.balance.agency_id,
            balance__box_id=allocation.balance.box_id,
            balance__barcode=allocation.balance.barcode,
        )
        .order_by("pick_task__sort_order", "id")
    )


def pick_missing_group_quantity(*, allocation_id: int, assigned_to) -> int:
    """Return open units of the same SKU at the current physical FBS source."""
    actor = _authenticated_user(assigned_to)
    allocation = (
        FbsOrderStockAllocation.objects.select_related(
            "pick_task__batch",
            "balance__box",
        )
        .filter(pk=allocation_id)
        .first()
    )
    if actor is None or allocation is None or allocation.pick_task_id is None:
        return 0
    if allocation.pick_task.assigned_to_id != actor.id:
        return 0
    return sum(
        max(int(qty_reserved or 0) - int(qty_picked or 0), 0)
        for qty_reserved, qty_picked in _pick_missing_group_queryset(
            allocation=allocation,
            actor_id=actor.id,
        ).values_list("qty_reserved", "qty_picked")
    )


def _not_found_issue_reason(*, missing_qty: int, reason: str) -> str:
    quantity_reason = f"Не найдено {int(missing_qty)} шт."
    comment = str(reason or "").strip()
    return f"{quantity_reason} Комментарий: {comment}" if comment else quantity_reason


def _not_found_problem_reason(
    *,
    allocation: FbsOrderStockAllocation,
    missing_qty: int,
    reason: str,
    partial_shortage: bool,
) -> str:
    sku_code = str(
        allocation.balance.sku_code
        or allocation.order_item.external_sku
        or ""
    ).strip()
    barcode = str(
        allocation.balance.barcode or allocation.order_item.barcode or ""
    ).strip()
    position = ", ".join(
        value
        for value in (
            f"артикул {sku_code}" if sku_code else "",
            f"ШК {barcode}" if barcode else "",
        )
        if value
    )
    prefix = "Недокомплект" if partial_shortage else "Товар не найден"
    result = (
        f"{prefix}: {position or 'позиция заказа'} — "
        f"не найдено {int(missing_qty)} шт."
    )
    comment = str(reason or "").strip()
    return f"{result} Комментарий: {comment}" if comment else result


@transaction.atomic
def report_pick_missing_quantity(
    *,
    allocation_id: int,
    missing_qty: int,
    reason: str,
    reported_by,
) -> FbsPickMissingQuantityResult:
    """Release several same-SKU wave units reported missing at one FBS source."""
    _require_writes()
    actor = _authenticated_user(reported_by)
    allocation = (
        FbsOrderStockAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "pick_task__order",
            "pick_task__batch",
            "order_item",
            "balance__box",
        )
        .get(pk=allocation_id)
    )
    if actor is None or allocation.pick_task_id is None:
        raise FbsPickingError("Позиция не назначена этому сборщику.")
    task = (
        FbsPickTask.objects.select_for_update()
        .select_related("order", "batch")
        .get(pk=allocation.pick_task_id)
    )
    allocation.pick_task = task
    batch = FbsPickBatch.objects.select_for_update().get(pk=task.batch_id)
    task.batch = batch
    if task.assigned_to_id != actor.id:
        raise FbsPickingError("Позиция не назначена этому сборщику.")
    if (
        task.status != FbsPickTask.STATUS_IN_PROGRESS
        or batch.status != FbsPickBatch.STATUS_IN_PROGRESS
        or allocation.status
        not in (
            FbsOrderStockAllocation.STATUS_RESERVED,
            FbsOrderStockAllocation.STATUS_PICKING,
        )
    ):
        raise FbsPickingError("Позиция не находится в активной волне.")

    candidates = list(
        _pick_missing_group_queryset(allocation=allocation, actor_id=actor.id)
        .select_for_update(of=("self",))
    )
    candidates.sort(
        key=lambda row: (
            0 if row.id == allocation.id else 1,
            int(row.pick_task.sort_order or 0),
            row.id,
        )
    )
    group_qty = sum(
        max(int(row.qty_reserved or 0) - int(row.qty_picked or 0), 0)
        for row in candidates
    )
    requested_qty = int(missing_qty or 0)
    if requested_qty <= 0 or requested_qty > group_qty:
        raise FbsPickingError(
            f"Укажите отсутствующее количество от 1 до {group_qty} шт."
        )

    selected = []
    qty_left = requested_qty
    for row in candidates:
        remaining = max(
            int(row.qty_reserved or 0) - int(row.qty_picked or 0),
            0,
        )
        if remaining <= 0:
            continue
        selected_qty = min(remaining, qty_left)
        selected.append((row, selected_qty))
        qty_left -= selected_qty
        if qty_left == 0:
            break
    if qty_left:
        raise FbsPickingError("Состав отсутствующего количества изменился. Повторите.")

    task_ids = sorted({row.pick_task_id for row, _ in selected})
    locked_tasks = {
        row.id: row
        for row in FbsPickTask.objects.select_for_update()
        .select_related("order", "batch")
        .filter(id__in=task_ids)
        .order_by("id")
    }
    for selected_task in locked_tasks.values():
        if (
            selected_task.assigned_to_id != actor.id
            or selected_task.batch_id != batch.id
            or selected_task.status != FbsPickTask.STATUS_IN_PROGRESS
        ):
            raise FbsPickingError("Состав волны изменился. Повторите операцию.")

    protected_quantities = _source_protected_quantities(allocation)
    quarantined_available_qty = 0
    if requested_qty == group_qty:
        quarantined_available_qty = _quarantine_pick_source_availability(
            allocation
        )

    selected_by_task = defaultdict(list)
    for row, selected_qty in selected:
        selected_by_task[row.pick_task_id].append((row, selected_qty))

    issues = []
    affected_order_ids = set()
    for task_id in task_ids:
        selected_task = locked_tasks[task_id]
        rows = selected_by_task[task_id]
        task_missing_qty = sum(quantity for _, quantity in rows)
        has_picked_allocations = FbsOrderStockAllocation.objects.filter(
            pick_task_id=task_id,
            status=FbsOrderStockAllocation.STATUS_PICKED,
        ).exists()
        partial_shortage = bool(
            int(selected_task.picked_qty or 0)
            or has_picked_allocations
            or task_missing_qty < int(selected_task.planned_qty or 0)
        )
        if partial_shortage:
            from .picking import release_pick_allocation_shortage

            for selected_allocation, selected_qty in rows:
                shortage = release_pick_allocation_shortage(
                    allocation_id=selected_allocation.id,
                    released_by=actor,
                    missing_qty=selected_qty,
                    return_to_available=False,
                )
                issue = FbsPickException(
                    task=selected_task,
                    allocation=shortage,
                    exception_type=FbsPickException.TYPE_NOT_FOUND,
                    reason=_not_found_issue_reason(
                        missing_qty=selected_qty,
                        reason=reason,
                    ),
                    created_by=actor,
                )
                issue.full_clean()
                issue.save()
                issues.append(issue)
        else:
            from .picking import release_order_reservation

            release_order_reservation(
                order_id=selected_task.order_id,
                released_by=actor,
                cancel_order=False,
                unavailable_qty_by_allocation={
                    selected_allocation.id: selected_qty
                    for selected_allocation, selected_qty in rows
                },
            )
            for selected_allocation, selected_qty in rows:
                issue = FbsPickException(
                    task=selected_task,
                    allocation=selected_allocation,
                    exception_type=FbsPickException.TYPE_NOT_FOUND,
                    reason=_not_found_issue_reason(
                        missing_qty=selected_qty,
                        reason=reason,
                    ),
                    created_by=actor,
                )
                issue.full_clean()
                issue.save()
                issues.append(issue)

        problem_order = FbsOrder.objects.select_for_update().get(
            pk=selected_task.order_id
        )
        problem_order.problem_reason = _not_found_problem_reason(
            allocation=rows[0][0],
            missing_qty=task_missing_qty,
            reason=reason,
            partial_shortage=partial_shortage,
        )
        problem_order.save(update_fields=["problem_reason", "updated_at"])
        affected_order_ids.add(problem_order.id)

    from .inventory_workflow import enqueue_shortage_inventory
    enqueue_shortage_inventory(issues=issues, reported_by=actor, protected_quantities=protected_quantities)

    return FbsPickMissingQuantityResult(
        requested_qty=requested_qty,
        released_qty=sum(quantity for _, quantity in selected),
        affected_order_count=len(affected_order_ids),
        source_quarantined=requested_qty == group_qty,
        quarantined_available_qty=quarantined_available_qty,
        issues=tuple(issues),
    )


@transaction.atomic
def report_verification_problem(
    *,
    allocation_id: int,
    exception_type: str,
    reason: str,
    problem_place_scan: str,
    reported_by,
) -> FbsVerificationProblemResult:
    """Remove one order from controller verification into unavailable FBS stock."""
    _require_writes()
    actor = _authenticated_user(reported_by)
    if actor is None:
        raise FbsPickingError("Для фиксации проблемы нужен авторизованный контролер.")
    if exception_type not in dict(FbsPickException.TYPE_CHOICES):
        raise FbsPickingError("Выберите причину проблемы товара.")

    selected = (
        FbsOrderStockAllocation.objects.select_for_update(of=("self",))
        .select_related("pick_task")
        .get(pk=allocation_id)
    )
    if selected.pick_task_id is None:
        raise FbsPickingError("Позиция не относится к волне FBS.")
    task = (
        FbsPickTask.objects.select_for_update()
        .select_related("batch", "order__profile")
        .get(pk=selected.pick_task_id)
    )
    batch = FbsPickBatch.objects.select_for_update().get(pk=task.batch_id)
    if batch.verification_assigned_to_id != actor.id:
        raise FbsPickingError("Проверка волны назначена другому контролеру.")
    if batch.status != FbsPickBatch.STATUS_VERIFICATION or batch.picking_completed_at is None:
        raise FbsPickingError("Волна еще не передана на проверку.")
    if task.status != FbsPickTask.STATUS_PICKED:
        raise FbsPickingError("Проблему можно зафиксировать только у отобранного заказа.")
    if FbsOrderLabel.objects.filter(
        order_id=task.order_id,
        status=FbsOrderLabel.STATUS_APPLIED,
    ).exists():
        raise FbsPickingError("Этикетка заказа уже подтверждена. Передайте заказ кладовщику.")

    transferred_metadata = FbsMarketplaceMetadataTransfer.objects.filter(
        order_item__order_id=task.order_id,
        status__in=(
            FbsMarketplaceMetadataTransfer.STATUS_SENT,
            FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED,
        ),
    ).exists()
    if transferred_metadata:
        raise FbsPickingError(
            "КИЗ или срок уже передан marketplace. Заказ требует решения кладовщика."
        )

    allocations = list(
        FbsOrderStockAllocation.objects.select_for_update(of=("self",))
        .select_related("traceability")
        .filter(pick_task=task)
        .order_by("id")
    )
    if not allocations or any(
        row.status != FbsOrderStockAllocation.STATUS_PICKED
        or int(row.qty_picked or 0) != int(row.qty_reserved or 0)
        for row in allocations
    ):
        raise FbsPickingError("Не все товары заказа физически собраны для переноса в проблему.")

    problem_box = _resolve_problem_box(
        agency_id=task.order.profile.agency_id,
        problem_place_scan=problem_place_scan,
    )
    from .inventory import assert_box_unlocked

    assert_box_unlocked(problem_box.id, for_execution=True)
    locked_balances = {
        balance.id: balance
        for balance in FbsStockBalance.objects.select_for_update()
        .filter(id__in={row.balance_id for row in allocations})
        .order_by("id")
    }
    moved_qty = 0
    now = timezone.now()
    for row in allocations:
        moved_qty += _restore_picked_qty_to_problem_box(
            allocation=row,
            balance=locked_balances[row.balance_id],
            problem_box=problem_box,
        )
        row.status = FbsOrderStockAllocation.STATUS_CANCELED
        row.released_by = actor
        row.released_at = now
        row.save(update_fields=["status", "released_by", "released_at", "updated_at"])
        trace = getattr(row, "traceability", None)
        if trace is not None:
            trace.status = FbsOrderTraceability.STATUS_CANCELED
            trace.save(update_fields=["status", "updated_at"])

    issue_reason = (
        f"Проверка контролера; проблемное место {problem_box.pallet.cell.cell_code}; "
        f"короб {problem_box.box_code}; возвращено в недоступный FBS-остаток {moved_qty} шт."
    )
    clean_reason = str(reason or "").strip()
    if clean_reason:
        issue_reason = f"{issue_reason} Комментарий: {clean_reason}"
    issue = FbsPickException(
        task=task,
        allocation=selected,
        exception_type=exception_type,
        reason=issue_reason,
        created_by=actor,
    )
    issue.full_clean()
    issue.save()

    FbsOrderLabel.objects.filter(
        order_id=task.order_id,
        status__in=(
            FbsOrderLabel.STATUS_REQUESTED,
            FbsOrderLabel.STATUS_READY,
            FbsOrderLabel.STATUS_ERROR,
        ),
    ).update(status=FbsOrderLabel.STATUS_CANCELED, updated_at=now)
    FbsMarketplaceMetadataTransfer.objects.filter(order_item__order_id=task.order_id).exclude(
        status__in=(
            FbsMarketplaceMetadataTransfer.STATUS_SENT,
            FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED,
        )
    ).update(status=FbsMarketplaceMetadataTransfer.STATUS_CANCELED, updated_at=now)

    task.status = FbsPickTask.STATUS_EXCEPTION
    task.save(update_fields=["status", "updated_at"])
    order = task.order
    order.internal_status = FbsOrder.STATUS_EXCEPTION
    order.problem_reason = issue.get_exception_type_display()
    if clean_reason:
        order.problem_reason = f"{order.problem_reason}: {clean_reason}"
    order.save(update_fields=["internal_status", "problem_reason", "updated_at"])
    record_pick_scan_event(
        batch_id=batch.id,
        task_id=task.id,
        allocation_id=selected.id,
        stage=FbsPickScanEvent.STAGE_BOX,
        result=FbsPickScanEvent.RESULT_SUCCESS,
        scan_value=problem_place_scan,
        expected_value=problem_box.box_code,
        quantity_after=moved_qty,
        message=(
            f"Заказ исключен из проверки; {moved_qty} шт. перемещено в проблемный "
            f"FBS-короб {problem_box.box_code}."
        ),
        created_by=actor,
    )

    from .picking import refresh_pick_batch_verification

    refresh_pick_batch_verification(batch_id=batch.id)
    return FbsVerificationProblemResult(
        issue=issue,
        batch_id=batch.id,
        order_id=task.order_id,
        problem_box_id=problem_box.id,
        problem_box_code=problem_box.box_code,
        problem_cell_code=problem_box.pallet.cell.cell_code,
        moved_qty=moved_qty,
    )


@transaction.atomic
def retry_pick_exception(*, exception_id: int, resolved_by) -> FbsPickException:
    _require_writes()
    actor = _authenticated_user(resolved_by)
    if actor is None:
        raise FbsPickingError("Не указан сотрудник, решивший проблему.")
    issue = (
        FbsPickException.objects.select_for_update()
        .select_related("task__order", "task__batch")
        .get(pk=exception_id)
    )
    if issue.status == FbsPickException.STATUS_RESOLVED:
        return issue
    if issue.inventory_session_id:
        raise FbsPickingError(
            "Проблема связана с инвентаризацией. Завершите проверку; "
            "повторный отбор выполняется через новый резерв, а не восстановление старого задания."
        )
    if issue.status != FbsPickException.STATUS_OPEN:
        raise FbsPickingError("Проблема уже закрыта.")
    if issue.task.batch.picking_completed_at is not None and (
        issue.allocation_id
        and issue.allocation.status == FbsOrderStockAllocation.STATUS_CANCELED
    ):
        raise FbsPickingError(
            "Проблема контролера уже перемещена в недоступный FBS-остаток; "
            "для повторного запуска требуется отдельное решение кладовщика."
        )
    issue.status = FbsPickException.STATUS_RESOLVED
    issue.resolved_by = actor
    issue.resolved_at = timezone.now()
    issue.save(update_fields=["status", "resolved_by", "resolved_at", "updated_at"])
    task = issue.task
    task.status = FbsPickTask.STATUS_IN_PROGRESS
    task.save(update_fields=["status", "updated_at"])
    order = task.order
    order.internal_status = FbsOrder.STATUS_PICKING
    order.problem_reason = ""
    order.save(update_fields=["internal_status", "problem_reason", "updated_at"])
    return issue


def client_operations_report(*, now=None) -> tuple[FbsClientOperationsRow, ...]:
    now = now or timezone.now()
    active_statuses = (
        FbsOrder.STATUS_RECEIVED,
        FbsOrder.STATUS_AWAITING_STOCK,
        FbsOrder.STATUS_RESERVED,
        FbsOrder.STATUS_QUEUED_FOR_PICK,
        FbsOrder.STATUS_PICKING,
        FbsOrder.STATUS_PICKED,
        FbsOrder.STATUS_READY_FOR_HANDOVER,
    )
    order_rows = {
        row["profile__agency_id"]: row
        for row in FbsOrder.objects.values(
            "profile__agency_id",
            "profile__agency__agn_name",
        ).annotate(
            open_orders=Count(
                "id",
                filter=Q(internal_status__in=active_statuses),
                distinct=True,
            ),
            completed_orders=Count(
                "id",
                filter=Q(
                    internal_status__in=(
                        FbsOrder.STATUS_HANDED_OVER,
                        FbsOrder.STATUS_DELIVERED,
                    )
                ),
                distinct=True,
            ),
            overdue_orders=Count(
                "id",
                filter=Q(
                    cutoff_at__lt=now,
                    internal_status__in=active_statuses,
                ),
                distinct=True,
            ),
            problem_orders=Count(
                "id",
                filter=Q(internal_status=FbsOrder.STATUS_EXCEPTION),
                distinct=True,
            ),
            overdue_qty=Sum(
                "items__quantity",
                filter=Q(cutoff_at__lt=now, internal_status__in=active_statuses),
            ),
        )
    }
    wave_rows = {
        row["agency_id"]: row
        for row in FbsPickBatch.objects.values("agency_id").annotate(
            queued_waves=Count("id", filter=Q(status=FbsPickBatch.STATUS_QUEUED)),
            active_waves=Count("id", filter=Q(status=FbsPickBatch.STATUS_IN_PROGRESS)),
        )
    }
    agency_ids = sorted(set(order_rows) | set(wave_rows))
    return tuple(
        FbsClientOperationsRow(
            agency_id=agency_id,
            agency_name=str(order_rows.get(agency_id, {}).get("profile__agency__agn_name") or ""),
            open_orders=int(order_rows.get(agency_id, {}).get("open_orders") or 0),
            queued_waves=int(wave_rows.get(agency_id, {}).get("queued_waves") or 0),
            active_waves=int(wave_rows.get(agency_id, {}).get("active_waves") or 0),
            completed_orders=int(order_rows.get(agency_id, {}).get("completed_orders") or 0),
            overdue_orders=int(order_rows.get(agency_id, {}).get("overdue_orders") or 0),
            problem_orders=int(order_rows.get(agency_id, {}).get("problem_orders") or 0),
            overdue_qty=int(order_rows.get(agency_id, {}).get("overdue_qty") or 0),
        )
        for agency_id in agency_ids
    )
