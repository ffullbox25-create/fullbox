import logging
import re
from collections import defaultdict
from datetime import datetime, time as day_time, timedelta
from functools import wraps
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import (
    BigIntegerField,
    CharField,
    Count,
    Exists,
    F,
    IntegerField,
    Max,
    Min,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce, TruncDate
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET, require_POST

from openpyxl import Workbook

from employees.access import (
    get_request_employee,
    get_request_role,
    resolve_cabinet_url,
    role_required,
)
from sklad.models import WarehouseStockSnapshot
from sku.models import Agency

from .barcode_aliases import (
    filter_normalized_barcodes,
    normalize_barcode,
    sku_barcode_alias_map,
)
from .exceptions import FbsError
from .flags import feature_enabled, module_enabled
from .goods_types import fbs_ready_goods_type_q
from .models import (
    FbsBox,
    FbsClientMovementRequest,
    FbsClientMovementRequestLine,
    FbsControllerToteOrder,
    FbsHandoverOrder,
    FbsHandoverOrderAssignment,
    FbsInternalMovement,
    FbsInventoryScan,
    FbsIntegrationProfile,
    FbsMarketplaceCommand,
    FbsMarketplaceEvent,
    FbsMarketplaceMetadataTransfer,
    FbsOrder,
    FbsOrderItem,
    FbsOrderLabel,
    FbsOrderStockAllocation,
    FbsOrderTraceability,
    FbsPickBatch,
    FbsPickException,
    FbsPickRestockLine,
    FbsPickRestockRequest,
    FbsPickRestockScan,
    FbsPickScanEvent,
    FbsPickTask,
    FbsPallet,
    FbsPickingCart,
    FbsProblemToteItem,
    FbsReplenishmentAllocation,
    FbsReplenishmentLine,
    FbsReplenishmentPlan,
    FbsReplenishmentPreparedBox,
    FbsReplenishmentPreparedBoxItem,
    FbsStockBalance,
    FbsStockMovement,
    FbsStorageCell,
    FbsSyncCursor,
    FbsToteMovement,
    FbsToteBinding,
    FbsToteZone,
    FbsUnknownToteItem,
    FbsWorkstation,
)
from .integrations.http import marketplace_credentials_status
from .order_audit import order_audit_timeline
from .readiness import add_equipment_readiness, build_fbs_readiness_report
from .services import (
    ACTIVE_PICK_RESTOCK_STATUSES,
    pack_staged_item_plan,
    prepare_pick_queue,
    pull_profile_orders,
)
from .services.client_movements import (
    RECEIVING_SOURCE_FILE_PREFIX,
    accept_client_movement_request,
    cancel_client_movement_by_warehouse,
    confirm_client_movement_by_warehouse,
    movement_box_candidate_summaries,
)
from .services.receiving_movements import (
    create_receiving_fbs_movement_request,
    receiving_pallet_movement_options,
)
from .services.client_profiles import (
    client_stock_overview,
    update_client_safety_stock_qty,
)
from .services.equipment import (
    create_fbs_picking_carts,
    update_fbs_picking_cart,
)
from .services.picking import (
    ACTIVE_TASK_STATUSES,
    FbsQueueStockConfirmationRequired,
    RESERVABLE_ORDER_STATUSES,
    STOCK_SHORTAGE_POLICY_REQUIRE_CONFIRMATION,
    STOCK_SHORTAGE_POLICY_SKIP,
    format_queue_rejections,
    order_by_pick_priority,
    pick_order_priority_key,
    pick_wave_destination_choices,
)
from .services.wave_queue import (
    decorate_wave_queue_rows,
    order_wave_queue_queryset,
    reorder_queued_wave,
)
from .services.physical_locations import (
    fbs_box_physical_location_label,
    fbs_box_reservable_q,
)
from .services.sync import (
    marketplace_terminal_order_q,
    marketplace_order_queue_error,
    wb_confirm_order_recovery_states,
)
from .staging import movement_staging_container_code
from .workspace import workspace_shell_enabled


logger = logging.getLogger(__name__)

OPERATOR_ROLES = ("storekeeper", "head_manager", "director", "admin")
ORDER_DETAIL_ROLES = (*OPERATOR_ROLES, "fbs_controller")
QUEUE_PAGE_SIZES = (25, 50, 100, 200, 500)
QUEUE_LIMITS = (1, 25, 50, 100, 200, 500)
MANUAL_ORDER_SYNC_LIMIT = 10000
QUEUEABLE_ORDER_STATUSES = (*RESERVABLE_ORDER_STATUSES, FbsOrder.STATUS_RESERVED)
STOCK_HISTORY_DIRECTION_CHOICES = (
    ("arrival", "Приход в FBS"),
    ("outgoing", "Расход из FBS"),
)
STOCK_HISTORY_VALUE_FIELDS = (
    "event_at",
    "event_id",
    "direction",
    "agency_id",
    "agency_name",
    "sku_code",
    "external_sku",
    "product_name",
    "barcode",
    "qty",
    "source_container",
    "source_zone",
    "fbs_box",
    "fbs_pallet",
    "fbs_cell",
    "plan_id",
    "request_id",
    "order_id",
    "external_order_id",
    "marketplace",
    "order_status",
    "pick_batch_id",
)
ACTIVE_WAVE_STATUSES = (
    FbsPickBatch.STATUS_QUEUED,
    FbsPickBatch.STATUS_IN_PROGRESS,
    FbsPickBatch.STATUS_VERIFICATION,
)
PROBLEM_TRANSFER_STATUSES = (
    FbsMarketplaceMetadataTransfer.STATUS_FAILED,
    FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
)
ACTIVE_REPLENISHMENT_LINE_STATUSES = (
    FbsReplenishmentLine.STATUS_PROPOSED,
    FbsReplenishmentLine.STATUS_RESERVED,
    FbsReplenishmentLine.STATUS_IN_PROGRESS,
)
PENDING_CLIENT_MOVEMENT_STATUSES = (
    FbsClientMovementRequest.STATUS_SUBMITTED,
    FbsClientMovementRequest.STATUS_APPROVED,
)
SECURED_ALLOCATION_STATUSES = (
    FbsOrderStockAllocation.STATUS_RESERVED,
    FbsOrderStockAllocation.STATUS_PICKING,
    FbsOrderStockAllocation.STATUS_PICKED,
)
AVAILABILITY_PRIORITY = {
    "secured": 0,
    "fbs_available": 1,
    "in_movement": 2,
    "needs_replenishment": 3,
    "unavailable": 4,
    "no_barcode": 5,
}
AVAILABILITY_LABELS = {
    "secured": "Обеспечен резервом",
    "fbs_available": "Доступно в FBS",
    "in_movement": "Ожидает перемещение",
    "needs_replenishment": "Нужен подсорт",
    "unavailable": "Нет на остатках",
    "no_barcode": "Нет штрихкода",
}
AVAILABILITY_FILTER_CHOICES = (
    ("ready", "Доступно"),
    ("risk", "Может не хватить"),
    ("unavailable", "Нет на остатках"),
)
AVAILABILITY_FILTER_STATES = {
    "ready": {"secured", "fbs_available"},
    "risk": {"in_movement", "needs_replenishment"},
    "in_movement": {"in_movement"},
    "needs_replenishment": {"needs_replenishment"},
    "unavailable": {"unavailable", "no_barcode"},
}
OPERATOR_INTERNAL_STATUS_LABELS = {
    **dict(FbsOrder.STATUS_CHOICES),
    FbsOrder.STATUS_RECEIVED: "Новый",
}
OPERATOR_STATUS_CHOICES = tuple(
    (value, OPERATOR_INTERNAL_STATUS_LABELS[value])
    for value, _label in FbsOrder.STATUS_CHOICES
    if value
    not in (
        FbsOrder.STATUS_CANCELLED,
        FbsOrder.STATUS_EXCEPTION,
        FbsOrder.STATUS_DELIVERED,
        FbsOrder.STATUS_HANDED_OVER,
        FbsOrder.STATUS_RETURNED,
    )
)
OPERATOR_ARCHIVE_ORDER_STATUSES = (
    FbsOrder.STATUS_HANDED_OVER,
    FbsOrder.STATUS_DELIVERED,
    FbsOrder.STATUS_RETURNED,
    FbsOrder.STATUS_CANCELLED,
)
OPERATOR_ARCHIVE_STATUS_CHOICES = tuple(
    (value, OPERATOR_INTERNAL_STATUS_LABELS[value])
    for value in OPERATOR_ARCHIVE_ORDER_STATUSES
)
OPERATOR_HIDDEN_ORDER_STATUSES = (
    FbsOrder.STATUS_CANCELLED,
    FbsOrder.STATUS_EXCEPTION,
    FbsOrder.STATUS_DELIVERED,
    FbsOrder.STATUS_HANDED_OVER,
    FbsOrder.STATUS_RETURNED,
)
ORDER_ATTENTION_STALE_HOURS = 2
ORDER_ATTENTION_STALE_UNSTARTED = "stale_unstarted"
ORDER_ATTENTION_PREVIOUS_DAY = "previous_day_unpicked"
ORDER_ATTENTION_VALUES = {
    ORDER_ATTENTION_STALE_UNSTARTED,
    ORDER_ATTENTION_PREVIOUS_DAY,
}
ORDER_ATTENTION_NOT_STARTED_STATUSES = (
    FbsOrder.STATUS_RECEIVED,
    FbsOrder.STATUS_VALIDATION_FAILED,
    FbsOrder.STATUS_AWAITING_STOCK,
    FbsOrder.STATUS_RESERVED,
)
ORDER_ATTENTION_NOT_PICKED_STATUSES = (
    *ORDER_ATTENTION_NOT_STARTED_STATUSES,
    FbsOrder.STATUS_QUEUED_FOR_PICK,
    FbsOrder.STATUS_PICKING,
)
MARKETPLACE_STATUS_LABELS = {
    "new": "Новый",
    "confirm": "Подтвержден",
    "complete": "Завершен",
    "waiting": "Ожидает обработки",
    "sorted": "Отсортирован",
    "sold": "Продан",
    "ready_for_pickup": "Готов к выдаче",
    "defect": "Брак",
    "cancel": "Отменен",
    "cancelled": "Отменен",
    "canceled": "Отменен",
    "cancel_by_client": "Отменен клиентом",
    "cancelled_by_client": "Отменен клиентом",
    "canceled_by_client": "Отменен клиентом",
    "cancel_missed_call": "Отменен: не дозвонились",
    "cancelled_by_missed_call": "Отменен: не дозвонились",
    "canceled_by_missed_call": "Отменен: не дозвонились",
    "decline": "Отклонен",
    "declined_by_client": "Отказ клиента",
    "rejected": "Отклонен",
    "awaiting_registration": "Ожидает регистрации",
    "acceptance_in_progress": "Принимается площадкой",
    "awaiting_approve": "Ожидает подтверждения",
    "awaiting_packaging": "Ожидает сборки",
    "awaiting_deliver": "Ожидает отгрузки",
    "arbitration": "Арбитраж",
    "client_arbitration": "Арбитраж с клиентом",
    "delivering": "Доставляется",
    "driver_pickup": "Передан водителю",
    "delivered": "Доставлен",
    "not_accepted": "Не принят",
    "posting_acceptance_in_progress": "Заказ принимается",
    "posting_created": "Заказ создан",
    "posting_transferring_to_delivery": "Передается в доставку",
    "posting_in_carriage": "Принят к перевозке",
    "posting_not_in_carriage": "Не принят к перевозке",
    "posting_transferred_to_courier_service": "Передан курьерской службе",
    "posting_in_courier_service": "В курьерской службе",
    "posting_on_way_to_city": "Следует в город получателя",
    "posting_in_pickup_point": "В пункте выдачи",
    "posting_transferred_to_driver": "Передан водителю",
    "posting_driver_pick_up": "Водитель забрал заказ",
    "posting_delivered": "Доставлен",
    "posting_received": "Получен клиентом",
    "posting_canceled": "Отменен",
    "posting_cancelled": "Отменен",
    "posting_in_client_arbitration": "Арбитраж с клиентом",
}


PHOTO_PAYLOAD_KEYS = (
    "photo",
    "photo_url",
    "image",
    "image_url",
    "picture",
    "picture_url",
    "primary_image",
    "primary_image_url",
)


def _first_photo_from_payload(payload) -> str:
    if isinstance(payload, dict):
        for key in PHOTO_PAYLOAD_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = _first_photo_from_payload(value)
                if nested:
                    return nested
        for value in payload.values():
            nested = _first_photo_from_payload(value)
            if nested:
                return nested
    elif isinstance(payload, list):
        for value in payload:
            nested = _first_photo_from_payload(value)
            if nested:
                return nested
    return ""


def _decorate_order_item_photos(items, *, order_payload=None) -> None:
    for item in items:
        item.display_name = (
            str(item.product_name or "").strip()
            or str(getattr(item.sku, "name", "") or "").strip()
            or "Без названия"
        )
        photo_url = str(getattr(item.sku, "img", "") or "").strip()
        if not photo_url and item.sku_id:
            photos = list(item.sku.photos.all())
            photo = photos[0] if photos else None
            photo_url = str(getattr(photo, "url", "") or "").strip()
        if not photo_url:
            photo_url = _first_photo_from_payload(item.raw_payload)
        if not photo_url:
            photo_url = _first_photo_from_payload(order_payload)
        item.photo_url = photo_url


def _order_handover_summary(handover):
    if handover is None:
        return None
    box = handover.box
    batch = box.batch
    boxes = list(batch.boxes.all())
    order_links = list(FbsHandoverOrder.objects.filter(box__batch=batch).select_related("order"))
    order_ids = {link.order_id for link in order_links}
    units = int(
        FbsOrderItem.objects.filter(order_id__in=order_ids).aggregate(total=Sum("quantity"))["total"]
        or 0
    )
    accepted_boxes = sum(row.status == row.STATUS_ACCEPTED for row in boxes)
    problem_boxes = sum(row.status == row.STATUS_PROBLEM for row in boxes)
    if batch.status == batch.STATUS_ACCEPTED and boxes:
        acceptance_label = "Принята полностью"
    elif accepted_boxes:
        acceptance_label = "Принята частично"
    elif problem_boxes or batch.status == batch.STATUS_PROBLEM:
        acceptance_label = "Есть отклонение"
    elif batch.status == batch.STATUS_DISPATCHED:
        acceptance_label = "Ожидает приемки"
    else:
        acceptance_label = "Не передана"
    return SimpleNamespace(
        batch=batch,
        box=box,
        box_count=len(boxes),
        accepted_box_count=accepted_boxes,
        problem_box_count=problem_boxes,
        order_count=len(order_ids),
        unit_count=units,
        acceptance_label=acceptance_label,
    )


def _presentation_completed_marketplace_order_q():
    """Read-only UI completion marker; does not reconcile local order state."""
    return marketplace_terminal_order_q() | Q(
        profile__marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
        marketplace_status__iexact="delivered",
    )


def _order_handover_check(assignment, handover, *, marketplace_terminal=False):
    if assignment is None:
        return None
    if assignment.status in {
        FbsHandoverOrderAssignment.STATUS_ERROR,
        FbsHandoverOrderAssignment.STATUS_CANCELED,
    }:
        state = "problem"
        label = "Не ОК"
        detail = assignment.error or assignment.get_status_display()
    elif assignment.status == FbsHandoverOrderAssignment.STATUS_PENDING:
        state = "in_progress"
        label = "Сверяется с WB"
        detail = assignment.error or "Ожидается подтверждение состава поставки WB."
    elif handover is not None and handover.verified_at is not None:
        state = "done"
        label = "ОК · Проверен"
        detail = f"Проверен в коробе {handover.box.qr_code}."
    elif handover is not None:
        state = "in_progress"
        label = "Ожидает проверки"
        detail = f"Нужен контрольный скан · короб {handover.box.qr_code}."
    else:
        state = "in_progress"
        label = "Ожидает упаковки"
        detail = "Заказ подтвержден в поставке WB, но еще не добавлен в короб."
    if marketplace_terminal and state != "done":
        state = "problem"
        label = "Расхождение статусов"
        detail = (
            "Маркетплейс уже завершил заказ, но контроль отгрузки в Fullbox не завершён. "
            "Кладовщику: сверить историю с менеджером. Не собирайте и не отгружайте заказ повторно."
        )
    return SimpleNamespace(
        assignment=assignment,
        batch=assignment.batch,
        state=state,
        label=label,
        detail=detail,
    )


def _fbs_actor_label(user, *, fallback="Система") -> str:
    if user is None:
        return fallback
    full_name = str(user.get_full_name() or "").strip()
    return full_name or str(user.get_username() or "").strip() or fallback


def _fbs_tote_picker_label(user, *, fallback="—") -> str:
    """Prefer the employee-card name over the technical login in tote rows."""
    if user is None:
        return fallback
    employee = getattr(user, "employee_profile", None)
    employee_name = str(getattr(employee, "full_name", "") or "").strip()
    return employee_name or _fbs_actor_label(user, fallback=fallback)


def _fbs_tote_label(tote) -> str:
    if tote is None:
        return "Не использовалась"
    name = str(tote.name or "").strip()
    barcode = str(tote.barcode or "").strip()
    return " · ".join(value for value in (name, barcode) if value) or "Тара без названия"


def _allocation_source_label(allocation) -> str:
    box = allocation.balance.box
    pallet = box.pallet
    cell = pallet.cell
    location = str(cell.warehouse_location_label or "").strip() or str(cell.cell_code or "-")
    return f"Ячейка {location} · паллета {pallet.pallet_code} · короб {box.box_code}"


def _order_route_rows(
    order,
    *,
    allocations,
    tasks,
    tote_orders,
    handover_assignment,
    handover,
    restock_requests,
    latest_marketplace_event,
):
    """Build a read-only order passport from immutable operational facts."""
    rows = []

    def add(*, at, stage, place, container, actor, status, detail="", state="done"):
        rows.append(
            SimpleNamespace(
                at=at,
                stage=stage,
                place=place or "-",
                container=container or "-",
                actor=actor or "Система",
                status=status or "-",
                detail=detail or "",
                state=state,
            )
        )

    tasks_by_id = {task.id: task for task in tasks}
    for allocation in allocations:
        task = tasks_by_id.get(allocation.pick_task_id)
        pick_tote = task.batch.cart if task is not None else None
        source = _allocation_source_label(allocation)
        add(
            at=allocation.reserved_at,
            stage="Резерв товара",
            place=source,
            container=f"ШК {allocation.balance.barcode}",
            actor=_fbs_actor_label(allocation.reserved_by),
            status=f"{allocation.qty_reserved} шт. · {allocation.get_status_display()}",
        )
        add(
            at=allocation.picked_at,
            stage="Физический отбор",
            place=source,
            container=_fbs_tote_label(pick_tote),
            actor=_fbs_actor_label(
                allocation.picked_by or (task.assigned_to if task is not None else None),
                fallback="Сборщик не указан",
            ),
            status=(
                f"Отобрано {allocation.qty_picked} из {allocation.qty_reserved} шт."
                if allocation.picked_at
                else "Ожидает отбора"
            ),
            state="done" if allocation.picked_at else "pending",
        )

    seen_batches = set()
    for task in tasks:
        if task.batch_id in seen_batches:
            continue
        seen_batches.add(task.batch_id)
        batch = task.batch
        if batch.picking_completed_at:
            add(
                at=batch.picking_completed_at,
                stage="Тара передана на контроль",
                place=(str(batch.workstation) if batch.workstation else "Рабочее место не указано"),
                container=_fbs_tote_label(batch.cart),
                actor=_fbs_actor_label(batch.assigned_to, fallback="Сборщик не указан"),
                status=f"Волна #{batch.id} · {batch.get_status_display()}",
            )

    for tote_order in tote_orders:
        pick_tote = _fbs_tote_label(tote_order.pick_tote.tote)
        control_tote = _fbs_tote_label(tote_order.check_tote.tote)
        if tote_order.check_tote.tote_id is None:
            control_tote = tote_order.control_destination or "Логический поток контроля"
        add(
            at=tote_order.label_confirmed_at,
            stage="Контроль заказа и этикетки",
            place=(
                str(tote_order.pick_tote.session.workstation)
                if tote_order.pick_tote.session.workstation_id
                else "Рабочее место контролёра"
            ),
            container=f"Из {pick_tote} → {control_tote}",
            actor=_fbs_actor_label(tote_order.label_confirmed_by, fallback="Контролёр не указан"),
            status=tote_order.get_status_display(),
            detail=f"Этикетка {tote_order.label.barcode or tote_order.label.external_label_id or '-'}",
        )
        if tote_order.composition_checked_at:
            add(
                at=tote_order.composition_checked_at,
                stage="Проверка состава",
                place=(
                    str(tote_order.pick_tote.session.workstation)
                    if tote_order.pick_tote.session.workstation_id
                    else "Рабочее место контролёра"
                ),
                container=control_tote,
                actor=_fbs_actor_label(
                    tote_order.composition_checked_by,
                    fallback="Контролёр не указан",
                ),
                status=f"Проверено {tote_order.units} шт.",
            )

    if handover_assignment is not None:
        batch = handover_assignment.batch
        add(
            at=handover_assignment.created_at,
            stage="Назначение в отгрузку",
            place=f"Отгрузка #{batch.id}",
            container=batch.external_supply_id or "Поставка Fullbox",
            actor=_fbs_actor_label(handover_assignment.assigned_by),
            status=handover_assignment.get_status_display(),
            detail=handover_assignment.error,
            state=(
                "problem"
                if handover_assignment.status
                in {
                    FbsHandoverOrderAssignment.STATUS_ERROR,
                    FbsHandoverOrderAssignment.STATUS_CANCELED,
                }
                else "done"
            ),
        )
        if handover_assignment.confirmed_at:
            add(
                at=handover_assignment.confirmed_at,
                stage="Поставка подтверждена площадкой",
                place=f"Отгрузка #{batch.id}",
                container=batch.external_supply_id or "Поставка Fullbox",
                actor="Маркетплейс",
                status=handover_assignment.get_status_display(),
            )

    if handover is not None:
        box = handover.box
        batch = box.batch
        add(
            at=handover.added_at,
            stage="Укладка в транспортный короб",
            place=f"Отгрузка #{batch.id}",
            container=box.qr_code,
            actor=_fbs_actor_label(handover.added_by, fallback="Контролёр не указан"),
            status=handover.get_status_display(),
        )
        if handover.verified_at:
            add(
                at=handover.verified_at,
                stage="Контрольный скан заказа",
                place=f"Отгрузка #{batch.id}",
                container=box.qr_code,
                actor=_fbs_actor_label(handover.verified_by, fallback="Контролёр не указан"),
                status="Заказ проверен",
                detail=(
                    handover.verified_label.barcode
                    if handover.verified_label_id
                    else "Этикетка не связана"
                ),
            )
        if box.scanned_at:
            add(
                at=box.scanned_at,
                stage="Короб принят кладовщиком",
                place=str(batch.dispatch_location or "Зона отгрузки"),
                container=box.qr_code,
                actor=_fbs_actor_label(box.scanned_by, fallback="Кладовщик не указан"),
                status=box.get_status_display(),
            )
        if batch.dispatched_at:
            add(
                at=batch.dispatched_at,
                stage="Передача водителю",
                place=str(batch.dispatch_location or "Зона отгрузки"),
                container=box.qr_code,
                actor=_fbs_actor_label(batch.dispatched_by, fallback="Кладовщик не указан"),
                status=batch.get_status_display(),
            )
        if box.accepted_at or batch.accepted_at:
            add(
                at=box.accepted_at or batch.accepted_at,
                stage="Принятие маркетплейсом",
                place=order.profile.get_marketplace_display(),
                container=box.qr_code,
                actor="Маркетплейс",
                status=(
                    "Короб принят"
                    if box.status == box.STATUS_ACCEPTED
                    else batch.get_status_display()
                ),
                state="done" if box.status == box.STATUS_ACCEPTED else "pending",
            )

    for request in restock_requests:
        request_lines = list(request.lines.all())
        first_line = request_lines[0] if request_lines else None
        add(
            at=request.created_at,
            stage="Создан возврат товара",
            place=(
                _allocation_source_label(first_line.allocation)
                if first_line is not None
                else "Исходное место отбора"
            ),
            container=_fbs_tote_label(request.source_tote),
            actor=_fbs_actor_label(request.created_by),
            status=request.get_status_display(),
            detail=request.reason,
            state="problem" if request.status == request.STATUS_FAILED else "pending",
        )
        if request.completed_at:
            add(
                at=request.completed_at,
                stage="Возврат товара завершён",
                place="Исходное место отбора",
                container=_fbs_tote_label(request.source_tote),
                actor=_fbs_actor_label(request.assigned_to),
                status=f"Возвращено {request.returned_qty} из {request.planned_qty} шт.",
            )

    marketplace_at = (
        latest_marketplace_event.processed_at or latest_marketplace_event.received_at
        if latest_marketplace_event is not None
        else order.updated_at
    )
    add(
        at=marketplace_at,
        stage="Текущий статус маркетплейса",
        place=order.profile.get_marketplace_display(),
        container=order.external_order_id,
        actor="Маркетплейс",
        status=order.marketplace_status_label,
        detail=order.marketplace_substatus_label,
        state=(
            "problem"
            if "cancel" in str(order.marketplace_status or "").casefold()
            or "cancel" in str(order.marketplace_substatus or "").casefold()
            else "done"
        ),
    )

    rows.sort(key=lambda row: (row.at is None, row.at or order.imported_at))
    return rows


def _decorate_tote_movement_endpoints(movements) -> None:
    employee_ids = set()
    workstation_codes = set()
    zone_codes = set()
    for movement in movements:
        for kind, code in (
            (movement.source_kind, movement.source_code),
            (movement.target_kind, movement.target_code),
        ):
            value = str(code or "").strip()
            if kind == "employee" and value.isdigit():
                employee_ids.add(int(value))
            elif kind == "workstation" and value:
                workstation_codes.add(value)
            elif kind == "zone" and value:
                zone_codes.add(value)

    employees = {
        user.id: _fbs_actor_label(user)
        for user in get_user_model().objects.filter(id__in=employee_ids)
    }
    workstations = {
        row.barcode: row.name
        for row in FbsWorkstation.objects.filter(barcode__in=workstation_codes)
    }
    zones = {
        row.barcode: row.name for row in FbsToteZone.objects.filter(barcode__in=zone_codes)
    }
    kind_labels = {
        "zone": "Зона",
        "employee": "Сотрудник",
        "workstation": "Рабочее место",
        "pick_tote": "Тара подбора",
        "check_tote": "Тара контроля",
        "shipment_flow": "Поток отгрузки",
        "handover_box": "Короб отгрузки",
        "problem_tote": "Проблемная тара",
    }

    def endpoint(kind, code):
        value = str(code or "").strip()
        if kind == "employee" and value.isdigit():
            value = employees.get(int(value), f"ID {value}")
        elif kind == "workstation":
            value = workstations.get(value, value)
        elif kind == "zone":
            value = zones.get(value, value)
        label = kind_labels.get(kind, str(kind or "Место").replace("_", " ").title())
        return f"{label}: {value or '-'}"

    for movement in movements:
        movement.source_display = endpoint(movement.source_kind, movement.source_code)
        movement.target_display = endpoint(movement.target_kind, movement.target_code)


AVAILABILITY_GROUPS = {
    "secured": ("available", "Доступно", "fbs_available"),
    "fbs_available": ("available", "Доступно", "fbs_available"),
    "in_movement": ("risk", "Может не хватить", "needs_replenishment"),
    "needs_replenishment": ("risk", "Может не хватить", "needs_replenishment"),
    "unavailable": ("no_fbs", "Нет на остатках", "unavailable"),
    "no_barcode": ("no_fbs", "Нет на остатках", "unavailable"),
}
AVAILABILITY_REASONS = {
    "secured": "Заказ уже обеспечен FBS-резервом.",
    "fbs_available": "Доступного FBS-остатка достаточно для сборки.",
    "in_movement": "FBS-остатка не хватает; недостающее количество уже перемещается.",
    "needs_replenishment": "FBS-остатка не хватает; товар есть на общем складе только для будущего подсорта.",
    "unavailable": "FBS-остатка, перемещения и готового товара для подсорта недостаточно.",
    "no_barcode": "Штрихкод не передан, проверить FBS-остаток невозможно.",
}


def _format_wave_duration(started_at, ended_at) -> str:
    if started_at is None:
        return "-"
    total_seconds = max(int((ended_at - started_at).total_seconds()), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} ч {minutes} мин."
    if minutes:
        return f"{minutes} мин."
    return f"{seconds} сек."


def _decorate_wave_timing(waves) -> None:
    now = timezone.now()
    for batch in waves:
        batch.duration_label = _format_wave_duration(
            batch.started_at,
            batch.completed_at or now,
        )


def operator_module_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not module_enabled():
            raise Http404
        return view_func(request, *args, **kwargs)

    return wrapped


def _base_context(request, *, section: str, **extra) -> dict:
    role = get_request_role(request)
    workspace_presentation = {
        "orders": ("Заказы", "", "Заказы"),
        "queue": ("Очередь на сборку", "Заказы, готовые уйти в волну", "Очередь"),
        "waves": ("Волны сборки", "Где стоит каждая волна", "Волны"),
        "reports": ("Отчёты по заказам", "", "Отчёты"),
        "problems": ("Разбор проблем", "Один экран на все источники", "Проблемы"),
        "movements": (
            "Перемещения на FBS",
            "Заявки «Общий склад → FBS» и планы ТСД",
            "Перемещения",
        ),
        "stock_history": (
            "История движения товара",
            "Когда пришёл на FBS и куда ушёл",
            "Отчёты / История товара",
        ),
        "readiness": ("Диагностика смены", "Предсменная проверка", "Диагностика"),
        "clients": ("Клиенты и интеграции", "", "Клиенты"),
        "totes": (
            "Тара FBS",
            "Активность, местоположение и история использования тары",
            "Тара",
        ),
    }
    workspace_title, workspace_subtitle, workspace_crumb = workspace_presentation.get(
        section,
        ("FBS", "", "FBS"),
    )
    context = {
        "employee": get_request_employee(request),
        "request_role": role,
        "workspace_shell": workspace_shell_enabled(role),
        "cabinet_url": resolve_cabinet_url(role),
        "warehouse_writes_enabled": feature_enabled("warehouse_writes"),
        "operator_section": section,
        "workspace_section": section,
        "workspace_title": workspace_title,
        "workspace_subtitle": workspace_subtitle,
        "workspace_crumb": workspace_crumb,
    }
    context.update(extra)
    return context


def _profiles_for_connection_status():
    order_cursors = FbsSyncCursor.objects.filter(
        stream=FbsSyncCursor.STREAM_ORDERS,
        cursor_key="default",
    ).order_by("id")
    return FbsIntegrationProfile.objects.select_related("agency").prefetch_related(
        Prefetch("sync_cursors", queryset=order_cursors, to_attr="order_sync_cursors")
    )


def _decorate_profile_connection(profile: FbsIntegrationProfile) -> None:
    credential_state = marketplace_credentials_status(profile)
    cursor = profile.order_sync_cursors[0] if profile.order_sync_cursors else None
    now = timezone.now()
    profile.credentials_configured = bool(credential_state["configured"])
    profile.credentials_source = str(credential_state["source"] or "")
    profile.credentials_source_label = {
        "client_cabinet": "ЛК клиента",
        "server_config": "Настройка FBS",
    }.get(profile.credentials_source, "Не настроены")
    profile.credentials_error = str(credential_state["error"] or "")
    profile.orders_last_polled_at = cursor.last_polled_at if cursor else None
    profile.orders_last_success_at = cursor.last_success_at if cursor else None
    profile.orders_last_error = str(cursor.last_error or "") if cursor else ""
    profile.orders_sync_running = bool(
        cursor
        and cursor.lease_token
        and cursor.lease_expires_at
        and cursor.lease_expires_at > now
    )
    profile.orders_sync_enabled = bool(
        feature_enabled("order_pull") and profile.is_active and profile.order_pull_enabled
    )
    profile.can_sync_orders = bool(
        profile.orders_sync_enabled
        and profile.credentials_configured
        and not profile.orders_sync_running
    )

    if profile.orders_sync_running:
        profile.api_state = "syncing"
        profile.api_state_label = "Обновляется"
    elif profile.orders_last_error:
        profile.api_state = "error"
        profile.api_state_label = "Ошибка API"
    elif profile.orders_last_success_at:
        profile.api_state = "connected"
        profile.api_state_label = "Подключен"
    elif profile.credentials_configured:
        profile.api_state = "pending"
        profile.api_state_label = "Не проверен"
    else:
        profile.api_state = "missing"
        profile.api_state_label = "Нет токена"


def _decorate_client_connection(agency: Agency, profiles, order_counts=None) -> None:
    profiles = list(profiles)
    for profile in profiles:
        _decorate_profile_connection(profile)
    agency.fbs_profiles_for_ui = profiles
    agency.profile_count = len(profiles)
    agency.credentials_count = sum(1 for profile in profiles if profile.credentials_configured)
    agency.order_sync_enabled_count = sum(1 for profile in profiles if profile.orders_sync_enabled)
    agency.can_sync_orders = any(profile.can_sync_orders for profile in profiles)
    agency.last_orders_success_at = max(
        (
            profile.orders_last_success_at
            for profile in profiles
            if profile.orders_last_success_at is not None
        ),
        default=None,
    )
    agency.last_orders_error = next(
        (profile.orders_last_error for profile in profiles if profile.orders_last_error),
        "",
    )
    states = {profile.api_state for profile in profiles}
    if "error" in states:
        agency.api_state = "error"
        agency.api_state_label = "Ошибка API"
    elif "syncing" in states:
        agency.api_state = "syncing"
        agency.api_state_label = "Обновляется"
    elif states == {"connected"}:
        agency.api_state = "connected"
        agency.api_state_label = "Подключен"
    elif "connected" in states:
        agency.api_state = "partial"
        agency.api_state_label = "Частично подключен"
    elif "pending" in states:
        agency.api_state = "pending"
        agency.api_state_label = "Не проверен"
    else:
        agency.api_state = "missing"
        agency.api_state_label = "Нет токена"
    counts = order_counts or {}
    agency.fbs_order_count = int(counts.get("total") or 0)
    agency.fbs_active_order_count = int(counts.get("active") or 0)


def _client_order_counts(agency_ids) -> dict[int, dict]:
    terminal_statuses = (
        FbsOrder.STATUS_DELIVERED,
        FbsOrder.STATUS_CANCELLED,
        FbsOrder.STATUS_RETURNED,
    )
    rows = (
        FbsOrder.objects.filter(profile__agency_id__in=agency_ids)
        .values("profile__agency_id")
        .annotate(
            total=Count("id"),
            active=Count("id", filter=~Q(internal_status__in=terminal_statuses)),
        )
    )
    return {int(row["profile__agency_id"]): row for row in rows}


def _sync_profile_orders(profiles) -> dict:
    summary = {
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "received": 0,
        "created": 0,
        "updated": 0,
        "duplicate": 0,
        "skipped": 0,
        "errors": [],
    }
    for profile in profiles:
        summary["attempted"] += 1
        try:
            result = pull_profile_orders(
                profile_id=profile.id,
                limit=MANUAL_ORDER_SYNC_LIMIT,
            )
        except FbsError as exc:
            summary["failed"] += 1
            summary["errors"].append(
                f"{profile.agency} / {profile.get_marketplace_display()}: {exc}"
            )
            continue
        except Exception:
            logger.exception("Unexpected manual FBS order sync failure for profile %s", profile.id)
            summary["failed"] += 1
            summary["errors"].append(
                f"{profile.agency} / {profile.get_marketplace_display()}: внутренняя ошибка"
            )
            continue
        summary["succeeded"] += 1
        for field in ("received", "created", "updated", "duplicate", "skipped"):
            summary[field] += int(getattr(result, field, 0) or 0)
    return summary


def _publish_sync_result(request, summary: dict) -> None:
    if not summary["attempted"]:
        messages.warning(request, "Нет включенных FBS-профилей для получения заказов.")
        return
    result_text = (
        f"Проверено профилей: {summary['attempted']}; получено: {summary['received']}; "
        f"новых: {summary['created']}; обновлено: {summary['updated']}."
    )
    if summary["failed"]:
        errors = "; ".join(summary["errors"][:3])
        messages.error(request, f"{result_text} Ошибок: {summary['failed']}. {errors}")
    else:
        messages.success(request, result_text)


def _order_annotations() -> dict:
    item_rows = FbsOrderItem.objects.filter(order_id=OuterRef("pk")).values("order_id")
    return {
        "line_count": Coalesce(
            Subquery(
                item_rows.annotate(value=Count("id")).values("value")[:1],
                output_field=IntegerField(),
            ),
            0,
        ),
        "unit_count": Coalesce(
            Subquery(
                item_rows.annotate(value=Sum("quantity")).values("value")[:1],
                output_field=IntegerField(),
            ),
            0,
        ),
        "has_active_pick_task": Exists(
            FbsPickTask.objects.filter(
                order_id=OuterRef("pk"),
                status__in=ACTIVE_TASK_STATUSES,
            )
        ),
    }


def _orders_queryset():
    return FbsOrder.objects.select_related("profile__agency").annotate(**_order_annotations())


def _problem_order_local_filter() -> Q:
    return (
        Q(
            internal_status__in=(
                FbsOrder.STATUS_VALIDATION_FAILED,
                FbsOrder.STATUS_AWAITING_STOCK,
                FbsOrder.STATUS_EXCEPTION,
            )
        )
        | ~Q(hold_reason="")
        | ~Q(problem_reason="")
    )


def _filter_problem_orders(queryset):
    local_problem_ids = (
        FbsOrder.objects.filter(_problem_order_local_filter())
        .order_by()
        .values_list("pk", flat=True)
    )
    open_pick_exception_ids = (
        FbsPickException.objects.filter(status=FbsPickException.STATUS_OPEN)
        .order_by()
        .values_list("task__order_id", flat=True)
    )
    label_error_ids = (
        FbsOrderLabel.objects.filter(status=FbsOrderLabel.STATUS_ERROR)
        .order_by()
        .values_list("order_id", flat=True)
    )
    transfer_problem_ids = (
        FbsMarketplaceMetadataTransfer.objects.filter(
            status__in=PROBLEM_TRANSFER_STATUSES
        )
        .order_by()
        .values_list("order_item__order_id", flat=True)
    )
    problem_ids = local_problem_ids.union(
        open_pick_exception_ids,
        label_error_ids,
        transfer_problem_ids,
    )
    return queryset.filter(pk__in=Subquery(problem_ids))


def _clean_page_size(raw_value) -> int:
    try:
        value = int(raw_value or 50)
    except (TypeError, ValueError):
        return 50
    return value if value in QUEUE_PAGE_SIZES else 50


def _clean_queue_limit(raw_value) -> int:
    try:
        value = int(raw_value or 25)
    except (TypeError, ValueError):
        return 25
    return value if value in QUEUE_LIMITS else 25


def _clean_quantity_filter(raw_value) -> str:
    value = str(raw_value or "").strip()
    if not value:
        return ""
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        return ""
    return str(quantity) if quantity >= 0 else ""


def _order_started_before_q(moment) -> Q:
    return Q(ordered_at__lte=moment) | Q(
        ordered_at__isnull=True,
        imported_at__lte=moment,
    )


def _filter_orders_by_attention(queryset, attention, *, now=None):
    attention = str(attention or "").strip()
    if attention not in ORDER_ATTENTION_VALUES:
        return queryset, ""
    now = now or timezone.now()
    if attention == ORDER_ATTENTION_STALE_UNSTARTED:
        queryset = queryset.filter(
            internal_status__in=ORDER_ATTENTION_NOT_STARTED_STATUSES,
            has_active_pick_task=False,
        ).filter(
            _order_started_before_q(
                now - timedelta(hours=ORDER_ATTENTION_STALE_HOURS)
            )
        )
    else:
        local_midnight = timezone.localtime(now).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        queryset = queryset.filter(
            internal_status__in=ORDER_ATTENTION_NOT_PICKED_STATUSES,
        ).filter(_order_started_before_q(local_midnight))
    return queryset, attention


def _format_order_attention_age(now, started_at) -> str:
    if started_at is None:
        return ""
    total_minutes = max(int((now - started_at).total_seconds() // 60), 0)
    days, remaining_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remaining_minutes, 60)
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


def _apply_order_filters(queryset, params):
    query = "".join(
        character
        for character in str(params.get("q") or "")
        if character.isprintable()
    ).strip()[:512]
    agency_id = str(params.get("agency") or "").strip()
    marketplace = str(params.get("marketplace") or "").strip()
    integration = str(params.get("integration") or "").strip()
    status = str(params.get("status") or "").strip()
    marketplace_status = str(params.get("marketplace_status") or "").strip()
    unit_kind = str(params.get("unit_kind") or "").strip()
    date_field = str(params.get("date_field") or "created").strip()
    date_from = str(params.get("date_from") or "").strip()
    date_to = str(params.get("date_to") or "").strip()
    deadline = str(params.get("deadline") or "").strip()
    problem = str(params.get("problem") or "").strip()
    attention = str(params.get("attention") or "").strip()
    availability = str(params.get("availability") or "").strip()
    group_by = str(params.get("group_by") or "").strip()
    unit_min = _clean_quantity_filter(params.get("unit_min"))
    unit_max = _clean_quantity_filter(params.get("unit_max"))

    if query:
        queryset = queryset.filter(
            Q(external_order_id__icontains=query)
            | Q(profile__agency__agn_name__icontains=query)
            | Q(items__external_sku__icontains=query)
            | Q(items__barcode__icontains=query)
            | Q(items__product_name__icontains=query)
            | Q(marketplace_labels__external_label_id__icontains=query)
            | Q(marketplace_labels__barcode__icontains=query)
        )
    if agency_id.isdigit():
        queryset = queryset.filter(profile__agency_id=int(agency_id))
    else:
        agency_id = ""
    if marketplace in dict(FbsIntegrationProfile.MARKETPLACE_CHOICES):
        queryset = queryset.filter(profile__marketplace=marketplace)
    else:
        marketplace = ""
    if integration.isdigit() and FbsIntegrationProfile.objects.filter(
        pk=int(integration)
    ).exists():
        queryset = queryset.filter(profile_id=int(integration))
    else:
        integration = ""
    if status in dict(FbsOrder.STATUS_CHOICES):
        queryset = queryset.filter(internal_status=status)
    else:
        status = ""
    if marketplace_status:
        queryset = queryset.filter(marketplace_status=marketplace_status)
    if unit_min:
        queryset = queryset.filter(unit_count__gte=int(unit_min))
    if unit_max:
        queryset = queryset.filter(unit_count__lte=int(unit_max))
    if unit_kind == "one":
        queryset = queryset.filter(unit_count=1)
    elif unit_kind == "multiple":
        queryset = queryset.filter(unit_count__gt=1)
    else:
        unit_kind = ""

    if date_field not in {"created", "cutoff"}:
        date_field = "created"
    parsed_from = parse_date(date_from) if date_from else None
    parsed_to = parse_date(date_to) if date_to else None
    if date_from and parsed_from is None:
        date_from = ""
    if date_to and parsed_to is None:
        date_to = ""
    date_lookup = "cutoff_at__date" if date_field == "cutoff" else "imported_at__date"
    if parsed_from:
        queryset = queryset.filter(**{f"{date_lookup}__gte": parsed_from})
    if parsed_to:
        queryset = queryset.filter(**{f"{date_lookup}__lte": parsed_to})

    now = timezone.now()
    if deadline == "overdue":
        queryset = queryset.filter(cutoff_at__lt=now)
    elif deadline == "urgent":
        queryset = queryset.filter(cutoff_at__gte=now, cutoff_at__lte=now + timedelta(hours=2))
    elif deadline == "today":
        queryset = queryset.filter(cutoff_at__date=timezone.localdate())
    elif deadline == "none":
        queryset = queryset.filter(cutoff_at__isnull=True)
    else:
        deadline = ""
    if problem == "open":
        queryset = _filter_problem_orders(queryset)
    else:
        problem = ""
    queryset, attention = _filter_orders_by_attention(
        queryset,
        attention,
        now=now,
    )
    if availability not in AVAILABILITY_FILTER_STATES:
        availability = ""
    if group_by != "sku":
        group_by = ""

    filters = {
        "q": query,
        "agency": agency_id,
        "marketplace": marketplace,
        "integration": integration,
        "status": status,
        "marketplace_status": marketplace_status,
        "unit_kind": unit_kind,
        "date_field": date_field,
        "date_from": date_from,
        "date_to": date_to,
        "deadline": deadline,
        "problem": problem,
        "attention": attention,
        "availability": availability,
        "group_by": group_by,
        "unit_min": unit_min,
        "unit_max": unit_max,
    }
    return queryset.distinct(), filters


def _stock_quantity_map(queryset, *, quantity_fields=("qty",)) -> dict[tuple[int, str], dict]:
    result = defaultdict(lambda: defaultdict(int))
    annotations = {field: Sum(field) for field in quantity_fields}
    for row in queryset.values("agency_id", "barcode").annotate(**annotations):
        key = (int(row["agency_id"]), normalize_barcode(row["barcode"]))
        for field in quantity_fields:
            result[key][field] += int(row[field] or 0)
    return result


def _decorate_order_availability(orders) -> None:
    order_ids = [order.id for order in orders]
    if not order_ids:
        return
    items = list(
        FbsOrderItem.objects.filter(order_id__in=order_ids)
        .select_related("order__profile")
        .order_by("order_id", "id")
    )
    agency_ids = {item.order.profile.agency_id for item in items}
    barcode_aliases = sku_barcode_alias_map(
        (item.sku_id, item.barcode) for item in items
    )
    aliases_by_item_id = {
        item.id: barcode_aliases.get(
            (int(item.sku_id), normalize_barcode(item.barcode)),
            (str(item.barcode or "").strip(),),
        )
        if item.sku_id
        else (str(item.barcode or "").strip(),)
        for item in items
    }
    barcodes = {
        barcode
        for aliases in aliases_by_item_id.values()
        for barcode in aliases
        if barcode
    }

    fbs_quantities = _stock_quantity_map(
        filter_normalized_barcodes(
            FbsStockBalance.objects.filter(agency_id__in=agency_ids),
            barcodes,
        ),
        quantity_fields=("qty", "reserved_qty"),
    )
    reservable_fbs_quantities = _stock_quantity_map(
        filter_normalized_barcodes(
            FbsStockBalance.objects.filter(
                agency_id__in=agency_ids,
                available_qty__gt=0,
            ),
            barcodes,
        ).filter(
            fbs_box_reservable_q(),
            Q(expiry_date__isnull=True) | Q(expiry_date__gte=timezone.localdate()),
        ),
        quantity_fields=("available_qty",),
    )
    secured_by_item = {
        int(row["order_item_id"]): int(row["total"] or 0)
        for row in FbsOrderStockAllocation.objects.filter(
            order_item__order_id__in=order_ids,
            status__in=SECURED_ALLOCATION_STATUSES,
        )
        .values("order_item_id")
        .annotate(total=Sum("qty_reserved"))
    }

    movement_quantities = defaultdict(int)
    pending_movement_quantities = defaultdict(int)
    replenishment_lines = filter_normalized_barcodes(
        FbsReplenishmentLine.objects.filter(
            plan__agency_id__in=agency_ids,
            status__in=ACTIVE_REPLENISHMENT_LINE_STATUSES,
        ),
        barcodes,
    )
    for row in replenishment_lines.values("plan__agency_id", "barcode").annotate(
        planned_total=Sum("qty_planned"),
        moved_total=Sum("qty_moved"),
    ):
        key = (int(row["plan__agency_id"]), normalize_barcode(row["barcode"]))
        movement_quantities[key] += max(
            int(row["planned_total"] or 0) - int(row["moved_total"] or 0),
            0,
        )
    client_movement_lines = filter_normalized_barcodes(
        FbsClientMovementRequestLine.objects.filter(
            request__agency_id__in=agency_ids,
            request__status__in=PENDING_CLIENT_MOVEMENT_STATUSES,
        ),
        barcodes,
    )
    for row in client_movement_lines.values(
        "request__agency_id", "barcode"
    ).annotate(total=Sum("requested_qty")):
        key = (int(row["request__agency_id"]), normalize_barcode(row["barcode"]))
        pending_qty = int(row["total"] or 0)
        pending_movement_quantities[key] += pending_qty
        movement_quantities[key] += pending_qty

    fbs_zone = str(getattr(settings, "FBS_ZONE_CODE", "FBS") or "FBS").strip()
    general_stock = filter_normalized_barcodes(
        WarehouseStockSnapshot.objects.filter(
            agency_id__in=agency_ids,
            is_archived=False,
            is_in_vehicle=False,
            qty__gt=0,
        ),
        barcodes,
    ).exclude(zone_code__iexact=fbs_zone).filter(fbs_ready_goods_type_q())
    if any(field.name == "expiry_date" for field in WarehouseStockSnapshot._meta.concrete_fields):
        general_stock = general_stock.filter(
            Q(expiry_date__isnull=True) | Q(expiry_date__gte=timezone.localdate())
        )
    general_quantities = _stock_quantity_map(
        general_stock,
        quantity_fields=("available_qty",),
    )

    item_groups = {}
    for item in items:
        barcode = str(item.barcode or "").strip()
        aliases = tuple(
            dict.fromkeys(
                alias
                for alias in aliases_by_item_id[item.id]
                if normalize_barcode(alias)
            )
        )
        alias_signature = tuple(sorted(normalize_barcode(alias) for alias in aliases))
        group_key = (item.order_id, item.sku_id, alias_signature)
        group = item_groups.setdefault(
            group_key,
            {
                "order_id": item.order_id,
                "agency_id": item.order.profile.agency_id,
                "barcode": barcode,
                "aliases": aliases,
                "names": [],
                "required_qty": 0,
                "secured_qty": 0,
            },
        )
        name = item.product_name or item.external_sku or "Товар"
        if name not in group["names"]:
            group["names"].append(name)
        group["required_qty"] += int(item.quantity or 0)
        group["secured_qty"] += int(secured_by_item.get(item.id, 0))

    rows_by_order = defaultdict(list)
    for group in item_groups.values():
        barcode = group["barcode"]
        alias_keys = [
            (group["agency_id"], normalize_barcode(alias))
            for alias in group["aliases"]
        ]
        fbs = defaultdict(int)
        for key in alias_keys:
            for field in ("qty", "reserved_qty"):
                fbs[field] += int(fbs_quantities[key][field] or 0)
        required_qty = int(group["required_qty"] or 0)
        secured_qty = min(int(group["secured_qty"] or 0), required_qty)
        fbs_available_qty = sum(
            int(reservable_fbs_quantities[key]["available_qty"] or 0)
            for key in alias_keys
        )
        in_movement_qty = sum(
            int(movement_quantities[key] or 0) for key in alias_keys
        )
        general_available_qty = sum(
            max(
                int(general_quantities[key]["available_qty"] or 0)
                - int(pending_movement_quantities[key] or 0),
                0,
            )
            for key in alias_keys
        )
        covered_qty = secured_qty
        if not barcode:
            state = "no_barcode"
        elif covered_qty >= required_qty:
            state = "secured"
        elif covered_qty + fbs_available_qty >= required_qty:
            state = "fbs_available"
        elif covered_qty + fbs_available_qty + in_movement_qty >= required_qty:
            state = "in_movement"
        elif (
            covered_qty + fbs_available_qty + in_movement_qty + general_available_qty
            >= required_qty
        ):
            state = "needs_replenishment"
        else:
            state = "unavailable"
        rows_by_order[group["order_id"]].append(
            {
                "name": ", ".join(group["names"]),
                "barcode": barcode,
                "required_qty": required_qty,
                "fbs_fact_qty": int(fbs["qty"] or 0),
                "fbs_available_qty": fbs_available_qty,
                "fbs_reserved_qty": int(fbs["reserved_qty"] or 0),
                "secured_qty": secured_qty,
                "in_movement_qty": in_movement_qty,
                "general_available_qty": general_available_qty,
                "state": state,
                "state_label": AVAILABILITY_LABELS[state],
                "group": AVAILABILITY_GROUPS[state][0],
                "group_label": AVAILABILITY_GROUPS[state][1],
                "group_css": AVAILABILITY_GROUPS[state][2],
                "reason": AVAILABILITY_REASONS[state],
            }
        )

    for order in orders:
        order.availability_rows = rows_by_order[order.id]
        order.availability_state = max(
            (row["state"] for row in order.availability_rows),
            key=lambda state: AVAILABILITY_PRIORITY[state],
            default="unavailable",
        )
        order.availability_label = AVAILABILITY_LABELS[order.availability_state]
        order.availability_group = AVAILABILITY_GROUPS[order.availability_state][0]
        order.availability_group_label = AVAILABILITY_GROUPS[
            order.availability_state
        ][1]
        order.availability_group_css = AVAILABILITY_GROUPS[
            order.availability_state
        ][2]


def _filter_orders_by_availability(queryset, availability: str):
    states = AVAILABILITY_FILTER_STATES.get(availability)
    if not states:
        return queryset
    orders = list(order_by_pick_priority(queryset))
    _decorate_order_availability(orders)
    matching_ids = [order.id for order in orders if order.availability_state in states]
    return queryset.filter(pk__in=matching_ids)


def _prioritized_queue_order_ids(queryset, *, limit: int) -> list[int]:
    return list(
        order_by_pick_priority(queryset).values_list("id", flat=True)[:limit]
    )


def _oldest_launchable_orders(queryset, *, limit: int) -> list[FbsOrder]:
    """Return oldest orders that can actually enter a wave right now."""
    orders = list(order_by_pick_priority(queryset)[:500])
    _decorate_orders(orders)
    return [
        order
        for order in orders
        if order.can_queue
        and order.availability_state in {"secured", "fbs_available"}
    ][:limit]


def _filtered_wave_agency_ids(queryset) -> list[int]:
    return list(
        queryset.order_by()
        .values_list("profile__agency_id", flat=True)
        .distinct()[:2]
    )


def _russian_marketplace_status(value, *, empty_label="Не получен") -> str:
    raw_value = str(value or "").strip()
    if not raw_value:
        return empty_label
    normalized = raw_value.lower().replace("-", "_").replace(" ", "_")
    if normalized in MARKETPLACE_STATUS_LABELS:
        return MARKETPLACE_STATUS_LABELS[normalized]
    if any(("а" <= char.lower() <= "я") or char.lower() == "ё" for char in raw_value):
        return raw_value
    return "Другой статус площадки"


def _decorate_order_status_labels(orders) -> None:
    for order in orders:
        order.internal_status_label = OPERATOR_INTERNAL_STATUS_LABELS.get(
            order.internal_status,
            "Другой внутренний статус",
        )
        order.marketplace_status_label = _russian_marketplace_status(
            order.marketplace_status
        )
        order.marketplace_substatus_label = _russian_marketplace_status(
            order.marketplace_substatus,
            empty_label="",
        )


def _decorate_orders(orders) -> None:
    now = timezone.now()
    terminal_ids = set(
        FbsOrder.objects.filter(pk__in=[order.pk for order in orders])
        .filter(_presentation_completed_marketplace_order_q()).values_list("pk", flat=True)
    )
    _decorate_order_status_labels(orders)
    _decorate_order_availability(orders)
    recovery_states = wb_confirm_order_recovery_states(orders)
    for order in orders:
        attention_started_at = order.ordered_at or order.imported_at
        order.attention_age_label = _format_order_attention_age(
            now,
            attention_started_at,
        )
        order.is_stale_unstarted = bool(
            order.internal_status in ORDER_ATTENTION_NOT_STARTED_STATUSES
            and not order.has_active_pick_task
            and attention_started_at
            and attention_started_at
            <= now - timedelta(hours=ORDER_ATTENTION_STALE_HOURS)
        )
        order.is_previous_day_unpicked = bool(
            order.internal_status in ORDER_ATTENTION_NOT_PICKED_STATUSES
            and attention_started_at
            and timezone.localtime(attention_started_at).date()
            < timezone.localtime(now).date()
        )
        queue_error = marketplace_order_queue_error(
            order,
            recovery_state=recovery_states.get(order.id),
        )
        is_wb_confirm_recovery = (
            order.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
            and str(order.marketplace_status or "").strip().lower() == "confirm"
            and not queue_error
        )
        order.operator_hold_reason = order.hold_reason
        order.operator_problem_reason = order.problem_reason
        if (
            order.internal_status == FbsOrder.STATUS_AWAITING_STOCK
            and order.availability_state in {"secured", "fbs_available"}
            and order.hold_reason in {"", "stock_shortage"}
        ):
            order.internal_status_label = (
                "Готов к восстановлению"
                if is_wb_confirm_recovery
                else "Готов к резервированию"
            )
            order.operator_hold_reason = ""
        if is_wb_confirm_recovery:
            order.operator_problem_reason = ""
        order.track_number = _first_payload_text(
            order.raw_payload,
            ("tracking_number", "track_number", "trackingNumber", "trackNumber"),
        )
        order.can_queue = (
            order.internal_status in QUEUEABLE_ORDER_STATUSES
            and not order.has_active_pick_task
            and not queue_error
        )
        order.is_overdue = bool(
            order.pk not in terminal_ids and order.cutoff_at and order.cutoff_at < now
        )
        order.is_urgent = bool(
            order.pk not in terminal_ids and order.cutoff_at
            and not order.is_overdue
            and order.cutoff_at <= now + timedelta(hours=2)
        )


def _first_payload_text(payload, keys) -> str:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
        for value in payload.values():
            nested = _first_payload_text(value, keys)
            if nested:
                return nested
    elif isinstance(payload, list):
        for value in payload:
            nested = _first_payload_text(value, keys)
            if nested:
                return nested
    return ""


def _orders_context(request, *, params=None, error="", queue_only=False) -> dict:
    params = params or request.GET
    archive_only = bool(
        not queue_only and str(params.get("scope") or "").strip() == "archive"
    )
    terminal_marketplace_orders = marketplace_terminal_order_q()
    current_orders = _orders_queryset().exclude(terminal_marketplace_orders)
    active_orders = (
        current_orders
        .exclude(internal_status__in=OPERATOR_HIDDEN_ORDER_STATUSES)
    )
    archive_orders = _orders_queryset().filter(
        Q(internal_status__in=OPERATOR_ARCHIVE_ORDER_STATUSES)
        | terminal_marketplace_orders
    )
    requested_attention = str(params.get("attention") or "").strip()
    if requested_attention not in ORDER_ATTENTION_VALUES:
        requested_attention = ""
    attention_orders = current_orders.filter(
        internal_status__in=ORDER_ATTENTION_NOT_PICKED_STATUSES,
    )
    all_orders = (
        archive_orders
        if archive_only
        else (attention_orders if requested_attention else active_orders)
    )
    search_all_orders = bool(
        not queue_only
        and not archive_only
        and not requested_attention
        and str(params.get("q") or "").strip()
    )
    if search_all_orders:
        all_orders = _orders_queryset()
    problem_orders = (
        _orders_queryset()
        .exclude(
            internal_status__in=(
                FbsOrder.STATUS_CANCELLED,
                FbsOrder.STATUS_DELIVERED,
                FbsOrder.STATUS_HANDED_OVER,
                FbsOrder.STATUS_RETURNED,
            )
        )
        .exclude(terminal_marketplace_orders)
    )
    summary = {
        "total": active_orders.count(),
        "archive": archive_orders.count(),
        "queueable": active_orders.filter(
            internal_status__in=QUEUEABLE_ORDER_STATUSES,
            has_active_pick_task=False,
        ).count(),
        "in_waves": active_orders.filter(
            internal_status__in=(
                FbsOrder.STATUS_QUEUED_FOR_PICK,
                FbsOrder.STATUS_PICKING,
            )
        ).count(),
        "problems": _filter_problem_orders(problem_orders).count(),
    }
    now = timezone.now()
    stale_attention_orders, _ = _filter_orders_by_attention(
        attention_orders,
        ORDER_ATTENTION_STALE_UNSTARTED,
        now=now,
    )
    previous_day_attention_orders, _ = _filter_orders_by_attention(
        attention_orders,
        ORDER_ATTENTION_PREVIOUS_DAY,
        now=now,
    )
    attention_summary = {
        "stale_unstarted": stale_attention_orders.count(),
        "previous_day_unpicked": previous_day_attention_orders.count(),
    }
    sla_summary = active_orders.aggregate(
        active=Count("id"),
        overdue=Count("id", filter=Q(cutoff_at__lt=now)),
        urgent=Count(
            "id",
            filter=Q(cutoff_at__gte=now, cutoff_at__lte=now + timedelta(hours=2)),
        ),
        without_cutoff=Count("id", filter=Q(cutoff_at__isnull=True)),
    )
    visible_orders = all_orders
    if queue_only:
        visible_orders = visible_orders.filter(
            internal_status__in=QUEUEABLE_ORDER_STATUSES,
            has_active_pick_task=False,
        )
    filtered_orders, filters = _apply_order_filters(visible_orders, params)
    filtered_orders = _filter_orders_by_availability(
        filtered_orders,
        filters["availability"],
    )
    order_by = (
        F("cutoff_at").asc(nulls_last=True),
        "imported_at",
        "id",
    )
    if queue_only and filters["group_by"] == "sku":
        primary_item = FbsOrderItem.objects.filter(order_id=OuterRef("pk")).order_by(
            "external_sku", "barcode", "id"
        )
        sku_count = (
            FbsOrderItem.objects.filter(order_id=OuterRef("pk"))
            .values("order_id")
            .annotate(value=Count("external_sku", distinct=True))
            .values("value")[:1]
        )
        filtered_orders = filtered_orders.annotate(
            grouping_sku=Subquery(primary_item.values("external_sku")[:1]),
            grouping_barcode=Subquery(primary_item.values("barcode")[:1]),
            grouping_name=Subquery(primary_item.values("product_name")[:1]),
            grouping_sku_count=Coalesce(
                Subquery(sku_count, output_field=IntegerField()),
                0,
            ),
        )
        order_by = (
            F("grouping_sku").asc(nulls_last=True),
            F("cutoff_at").asc(nulls_last=True),
            "imported_at",
            "id",
        )
    page_size = _clean_page_size(params.get("page_size"))
    page = Paginator(
        filtered_orders.order_by(*order_by),
        page_size,
    ).get_page(params.get("page"))
    _decorate_orders(page.object_list)
    article_groups = []
    if queue_only and filters["group_by"] == "sku":
        groups_by_key = {}
        for order in page.object_list:
            if int(order.grouping_sku_count or 0) > 1:
                key = ("__mixed__", "")
                sku = "Смешанные заказы"
                barcode = ""
                name = "В заказе несколько артикулов"
            else:
                sku = str(order.grouping_sku or "").strip() or "Без артикула"
                barcode = str(order.grouping_barcode or "").strip()
                name = str(order.grouping_name or "").strip() or "Без названия"
                key = (sku, barcode)
            group = groups_by_key.get(key)
            if group is None:
                group = SimpleNamespace(
                    index=len(article_groups) + 1,
                    sku=sku,
                    barcode=barcode,
                    name=name,
                    orders=[],
                    order_count=0,
                    unit_count=0,
                    selectable_count=0,
                )
                groups_by_key[key] = group
                article_groups.append(group)
            group.orders.append(order)
            group.order_count += 1
            group.unit_count += int(order.unit_count or 0)
            if order.can_queue:
                group.selectable_count += 1
    agencies = Agency.objects.filter(fbs_integration_profiles__isnull=False).distinct()
    profiles = FbsIntegrationProfile.objects.select_related("agency").filter(
        agency__in=agencies
    )
    marketplace_status_values = list(
        all_orders.exclude(marketplace_status="")
        .order_by("marketplace_status")
        .values_list("marketplace_status", flat=True)
        .distinct()
    )
    marketplace_statuses = [
        {"value": value, "label": _russian_marketplace_status(value)}
        for value in marketplace_status_values
    ]
    selected_agency = (
        agencies.filter(pk=int(filters["agency"])).first()
        if filters["agency"]
        else None
    )
    selection_enabled = selected_agency is not None
    filter_values = {**filters, "page_size": page_size}
    if archive_only:
        filter_values["scope"] = "archive"
    filter_query = urlencode(filter_values)
    advanced_filters_active = any(
        filters[key]
        for key in (
            "integration",
            "marketplace_status",
            "unit_kind",
            "date_from",
            "date_to",
            "deadline",
            "problem",
            "attention",
            "availability",
            "group_by",
            "unit_min",
            "unit_max",
        )
    )
    return _base_context(
        request,
        section="queue" if queue_only else "orders",
        page_title="FBS · Очередь" if queue_only else "FBS · Заказы",
        back_url=reverse("fbs:tsd_storekeeper"),
        page=page,
        page_size=page_size,
        page_sizes=QUEUE_PAGE_SIZES,
        filters=filters,
        filter_query=filter_query,
        summary=summary,
        sla_summary=sla_summary,
        attention_summary=attention_summary,
        agencies=agencies.order_by("agn_name", "id"),
        profiles=profiles.order_by("agency__agn_name", "marketplace", "name", "id"),
        selected_agency=selected_agency,
        selection_enabled=selection_enabled,
        status_choices=(
            OPERATOR_ARCHIVE_STATUS_CHOICES
            if archive_only
            else OPERATOR_STATUS_CHOICES
        ),
        marketplace_choices=FbsIntegrationProfile.MARKETPLACE_CHOICES,
        marketplace_statuses=marketplace_statuses,
        availability_filter_choices=AVAILABILITY_FILTER_CHOICES,
        article_groups=article_groups,
        queue_limits=QUEUE_LIMITS,
        advanced_filters_active=advanced_filters_active,
        orders_mode="queue" if queue_only else "orders",
        orders_scope="archive" if archive_only else "active",
        search_all_orders=search_all_orders,
        workflow_section="queue" if queue_only else "orders",
        reset_url=(
            reverse("fbs:operator_queue")
            if queue_only
            else (
                f"{reverse('fbs:operator_orders')}?scope=archive"
                if archive_only
                else reverse("fbs:operator_orders")
            )
        ),
        latest_order_at=all_orders.order_by("-imported_at").values_list(
            "imported_at", flat=True
        ).first(),
        error=error,
    )


def _order_timeline(
    order,
    *,
    tasks,
    allocations,
    labels,
    transfers,
    commands,
    handover,
    exceptions,
    marketplace_events,
    tote_orders,
    tote_movements,
    restock_requests,
):
    rows = order_audit_timeline(order)
    for row in rows:
        row.setdefault("actor", "Система")

    def add(at, title, detail="", status="", actor="Система"):
        if at:
            rows.append(
                {
                    "at": at,
                    "title": title,
                    "detail": detail,
                    "status": status,
                    "actor": actor,
                }
            )

    add(
        order.ordered_at,
        "Заказ создан на площадке",
        order.external_order_id,
        actor="Маркетплейс",
    )
    add(order.imported_at, "Заказ импортирован", order.profile.get_marketplace_display())
    for event in marketplace_events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        external_status = str(
            payload.get("status") or payload.get("marketplace_status") or ""
        ).strip()
        external_substatus = str(
            payload.get("substatus") or payload.get("marketplace_substatus") or ""
        ).strip()
        external_detail = " · ".join(
            value for value in (event.event_type, external_status, external_substatus) if value
        )
        add(
            event.received_at,
            "Получено событие площадки",
            external_detail,
            event.get_status_display(),
            "Маркетплейс",
        )
        add(
            event.processed_at,
            "Событие площадки обработано",
            external_detail,
            event.get_status_display(),
        )
    for allocation in allocations:
        barcode = str(allocation.balance.barcode or "").strip() or "ШК не указан"
        source = _allocation_source_label(allocation)
        add(
            allocation.reserved_at,
            "Создан FBS-резерв",
            f"{source}; ШК {barcode}; {allocation.qty_reserved} шт.",
            allocation.get_status_display(),
            _fbs_actor_label(allocation.reserved_by),
        )
        add(
            allocation.picked_at,
            "Товар отобран",
            f"{source}; ШК {barcode}; {allocation.qty_picked} шт.",
            allocation.get_status_display(),
            _fbs_actor_label(allocation.picked_by, fallback="Сборщик не указан"),
        )
        add(
            allocation.released_at,
            "Резерв освобожден",
            f"{source}; ШК {barcode}; {allocation.qty_reserved} шт.",
            allocation.get_status_display(),
            _fbs_actor_label(allocation.released_by),
        )
    seen_batches = set()
    for task in tasks:
        add(
            task.created_at,
            f"Заказ добавлен в волну #{task.batch_id}",
            f"{task.planned_qty} шт.",
            task.get_status_display(),
            _fbs_actor_label(task.batch.created_by),
        )
        add(
            task.claimed_at,
            f"Начат подбор в волне #{task.batch_id}",
            task.assigned_to.get_username() if task.assigned_to else "Сборщик не указан",
            task.get_status_display(),
            _fbs_actor_label(task.assigned_to, fallback="Сборщик не указан"),
        )
        add(
            task.completed_at,
            f"Подбор завершен в волне #{task.batch_id}",
            f"{task.picked_qty} из {task.planned_qty} шт.",
            task.get_status_display(),
            _fbs_actor_label(task.assigned_to, fallback="Сборщик не указан"),
        )
        if task.batch_id in seen_batches:
            continue
        seen_batches.add(task.batch_id)
        batch = task.batch
        workstation = batch.workstation.name if batch.workstation else "без рабочего места"
        cart = batch.cart.name if batch.cart else "без тары"
        add(
            batch.started_at,
            f"Волна #{batch.id} запущена",
            f"{workstation}, {cart}",
            batch.get_status_display(),
            _fbs_actor_label(batch.assigned_to, fallback="Сборщик не указан"),
        )
        add(
            batch.completed_at,
            f"Волна #{batch.id} завершена",
            f"{batch.picked_qty} из {batch.planned_qty} шт.",
            batch.get_status_display(),
            _fbs_actor_label(
                batch.verification_assigned_to or batch.assigned_to,
                fallback="Сотрудник не указан",
            ),
        )
    for exception in exceptions:
        add(
            exception.created_at,
            "Зафиксирована проблема сборки",
            exception.reason or exception.get_exception_type_display(),
            exception.get_status_display(),
            _fbs_actor_label(exception.created_by),
        )
        add(
            exception.resolved_at,
            "Проблема сборки решена",
            exception.get_exception_type_display(),
            exception.get_status_display(),
            _fbs_actor_label(exception.resolved_by),
        )
    for label in labels:
        add(
            label.requested_at,
            "Запрошена этикетка заказа",
            label.external_label_id or order.profile.get_marketplace_display(),
            label.get_status_display(),
            _fbs_actor_label(label.requested_by),
        )
        add(
            label.ready_at,
            "Этикетка заказа получена",
            label.external_label_id,
            label.get_status_display(),
            "Маркетплейс",
        )
        add(
            label.applied_at,
            "Этикетка заказа подтверждена",
            label.barcode,
            label.get_status_display(),
            _fbs_actor_label(label.applied_by, fallback="Контролёр не указан"),
        )
    for transfer in transfers:
        metadata = transfer.get_metadata_type_display()
        add(
            transfer.prepared_at,
            f"{metadata}: подготовлено",
            transfer.order_item.external_sku,
            transfer.get_status_display(),
        )
        add(
            transfer.sent_at,
            f"{metadata}: отправлено площадке",
            transfer.order_item.external_sku,
            transfer.get_status_display(),
        )
        add(
            transfer.confirmed_at,
            f"{metadata}: подтверждено площадкой",
            transfer.external_status,
            transfer.get_status_display(),
        )
    for command in commands:
        add(
            command.created_at,
            "Создана команда площадке",
            command.command_type,
            command.get_status_display(),
            _fbs_actor_label(command.requested_by),
        )
        add(
            command.confirmed_at,
            "Команда площадки подтверждена",
            command.command_type,
            command.get_status_display(),
            "Маркетплейс",
        )
    for tote_order in tote_orders:
        add(
            tote_order.label_confirmed_at,
            "Контролёр подтвердил этикетку заказа",
            (
                f"{_fbs_tote_label(tote_order.pick_tote.tote)} → "
                f"{tote_order.control_destination or _fbs_tote_label(tote_order.check_tote.tote)}"
            ),
            tote_order.get_status_display(),
            _fbs_actor_label(tote_order.label_confirmed_by),
        )
        add(
            tote_order.composition_checked_at,
            "Контролёр подтвердил состав заказа",
            f"{tote_order.units} шт.",
            tote_order.get_status_display(),
            _fbs_actor_label(tote_order.composition_checked_by),
        )
    for movement in tote_movements:
        add(
            movement.created_at,
            f"Тара: {movement.get_action_display()}",
            f"{movement.tote.barcode}; {movement.source_display} → {movement.target_display}",
            f"{movement.quantity} шт." if movement.quantity else "",
            _fbs_actor_label(movement.performed_by),
        )
    for request in restock_requests:
        add(
            request.created_at,
            "Создано задание возврата",
            request.reason,
            request.get_status_display(),
            _fbs_actor_label(request.created_by),
        )
        add(
            request.completed_at,
            "Возврат товара завершён",
            f"{request.returned_qty} из {request.planned_qty} шт.",
            request.get_status_display(),
            _fbs_actor_label(request.assigned_to),
        )
        for scan in request.scans.all():
            add(
                scan.created_at,
                f"Возврат: {scan.get_stage_display()}",
                f"Скан {scan.scan_value or '-'}; ожидалось {scan.expected_value or '-'}",
                scan.get_result_display(),
                _fbs_actor_label(scan.created_by),
            )
    if handover:
        box = handover.box
        batch = box.batch
        add(
            handover.added_at,
            "Заказ добавлен в короб передачи",
            box.qr_code,
            box.get_status_display(),
            _fbs_actor_label(handover.added_by, fallback="Контролёр не указан"),
        )
        add(
            handover.verified_at,
            "Заказ проверен в коробе передачи",
            box.qr_code,
            handover.get_status_display(),
            _fbs_actor_label(handover.verified_by, fallback="Контролёр не указан"),
        )
        add(
            box.scanned_at,
            "Короб просканирован кладовщиком",
            box.qr_code,
            box.get_status_display(),
            _fbs_actor_label(box.scanned_by, fallback="Кладовщик не указан"),
        )
        add(
            batch.dispatched_at,
            "Поставка передана водителю",
            batch.external_supply_id,
            batch.get_status_display(),
            _fbs_actor_label(batch.dispatched_by, fallback="Кладовщик не указан"),
        )
        add(
            box.accepted_at or batch.accepted_at,
            "Заказ принят маркетплейсом",
            box.qr_code,
            box.get_status_display(),
            "Маркетплейс",
        )
    add(
        order.updated_at,
        "Текущее состояние заказа",
        order.marketplace_status_label,
        order.internal_status_label,
    )
    rows.sort(key=lambda row: row["at"], reverse=True)
    return rows


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_GET
def operator_orders(request):
    return render(request, "fbs/operator_orders.html", _orders_context(request))


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_GET
def operator_queue(request):
    return render(
        request,
        "fbs/operator_orders.html",
        _orders_context(request, queue_only=True),
    )


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_GET
def operator_order_reports(request):
    report_type = str(request.GET.get("report") or "status").strip()
    report_types = {
        "status": "Статус сборки",
        "count": "Количество заказов",
        "shipped": "Отгруженные заказы",
        "goods": "Отгруженные товары FBS",
    }
    if report_type not in report_types:
        report_type = "status"

    agency_filter = str(request.GET.get("agency") or "").strip()
    marketplace_filter = str(request.GET.get("marketplace") or "").strip()
    date_from = str(request.GET.get("date_from") or "").strip()
    date_to = str(request.GET.get("date_to") or "").strip()
    parsed_from = parse_date(date_from) if date_from else None
    parsed_to = parse_date(date_to) if date_to else None
    if date_from and parsed_from is None:
        date_from = ""
    if date_to and parsed_to is None:
        date_to = ""

    orders = FbsOrder.objects.select_related("profile__agency")
    if agency_filter.isdigit():
        orders = orders.filter(profile__agency_id=int(agency_filter))
    else:
        agency_filter = ""
    if marketplace_filter in dict(FbsIntegrationProfile.MARKETPLACE_CHOICES):
        orders = orders.filter(profile__marketplace=marketplace_filter)
    else:
        marketplace_filter = ""
    if parsed_from:
        orders = orders.filter(imported_at__date__gte=parsed_from)
    if parsed_to:
        orders = orders.filter(imported_at__date__lte=parsed_to)

    shipped_statuses = (
        FbsOrder.STATUS_HANDED_OVER,
        FbsOrder.STATUS_DELIVERED,
    )
    total_summary = orders.aggregate(
        orders=Count("id", distinct=True),
        units=Coalesce(Sum("items__quantity"), 0),
    )
    shipped_summary = orders.filter(internal_status__in=shipped_statuses).aggregate(
        orders=Count("id", distinct=True),
        units=Coalesce(Sum("items__quantity"), 0),
    )

    rows = []
    report_page = None
    if report_type == "status":
        status_rows = list(
            orders.values("internal_status")
            .annotate(
                order_count=Count("id", distinct=True),
                unit_count=Coalesce(Sum("items__quantity"), 0),
            )
            .order_by("internal_status")
        )
        for row in status_rows:
            row["label"] = OPERATOR_INTERNAL_STATUS_LABELS.get(
                row["internal_status"], "Другой статус"
            )
        rows = status_rows
    elif report_type == "count":
        rows = list(
            orders.annotate(report_date=TruncDate("imported_at"))
            .values("report_date", "profile__marketplace")
            .annotate(
                order_count=Count("id", distinct=True),
                unit_count=Coalesce(Sum("items__quantity"), 0),
            )
            .order_by("-report_date", "profile__marketplace")
        )
        marketplace_labels = dict(FbsIntegrationProfile.MARKETPLACE_CHOICES)
        for row in rows:
            row["marketplace_label"] = marketplace_labels.get(
                row["profile__marketplace"], row["profile__marketplace"]
            )
    elif report_type == "shipped":
        report_page = Paginator(
            orders.filter(internal_status__in=shipped_statuses)
            .prefetch_related("items")
            .order_by("-updated_at", "-id"), 100
        ).get_page(request.GET.get("page"))
        rows = list(report_page.object_list)
        _decorate_order_status_labels(rows)
        for order in rows:
            order.report_unit_count = sum(int(item.quantity or 0) for item in order.items.all())
    else:
        report_page = Paginator(
            FbsOrderItem.objects.filter(
                order__in=orders.filter(internal_status__in=shipped_statuses)
            )
            .values(
                "order__profile__agency__agn_name",
                "order__profile__marketplace",
                "external_sku",
                "barcode",
                "product_name",
            )
            .annotate(
                unit_count=Coalesce(Sum("quantity"), 0),
                order_count=Count("order_id", distinct=True),
            )
            .order_by(
                "order__profile__agency__agn_name", "external_sku", "barcode",
                "order__profile__marketplace", "product_name",
            ), 100
        ).get_page(request.GET.get("page"))
        rows = list(report_page.object_list)
        marketplace_labels = dict(FbsIntegrationProfile.MARKETPLACE_CHOICES)
        for row in rows:
            row["marketplace_label"] = marketplace_labels.get(
                row["order__profile__marketplace"],
                row["order__profile__marketplace"],
            )

    return render(
        request,
        "fbs/operator_order_reports.html",
        _base_context(
            request,
            section="reports",
            page_title=f"FBS · {report_types[report_type]}",
            back_url=reverse("fbs:operator_orders"),
            workspace_section="reports",
            report_type=report_type,
            report_title=report_types[report_type],
            report_types=report_types,
            report_page=report_page,
            rows=rows,
            agencies=Agency.objects.filter(fbs_integration_profiles__isnull=False)
            .distinct()
            .order_by("agn_name", "id"),
            marketplace_choices=FbsIntegrationProfile.MARKETPLACE_CHOICES,
            agency_filter=agency_filter,
            marketplace_filter=marketplace_filter,
            date_from=date_from,
            date_to=date_to,
            total_summary=total_summary,
            shipped_summary=shipped_summary,
        ),
    )


def _stock_history_date(value: str):
    cleaned = str(value or "").strip()
    return (cleaned, parse_date(cleaned)) if cleaned else ("", None)


def _stock_history_sources(
    *,
    agency_filter: str,
    query: str,
    parsed_from,
    parsed_to,
):
    arrivals = FbsStockMovement.objects.all()
    outgoing = FbsOrderStockAllocation.objects.filter(qty_picked__gt=0).annotate(
        history_at=Coalesce("picked_at", "updated_at")
    )
    if agency_filter:
        agency_id = int(agency_filter)
        arrivals = arrivals.filter(target_balance__agency_id=agency_id)
        outgoing = outgoing.filter(balance__agency_id=agency_id)
    if query:
        arrivals = arrivals.filter(
            Q(target_balance__sku_code__icontains=query)
            | Q(target_balance__barcode__icontains=query)
            | Q(target_balance__name__icontains=query)
            | Q(source_snapshot__sku_code__icontains=query)
            | Q(source_snapshot__barcode__icontains=query)
        )
        outgoing = outgoing.filter(
            Q(balance__sku_code__icontains=query)
            | Q(balance__barcode__icontains=query)
            | Q(balance__name__icontains=query)
            | Q(order_item__external_sku__icontains=query)
            | Q(order_item__barcode__icontains=query)
            | Q(order_item__product_name__icontains=query)
        )
    if parsed_from:
        arrivals = arrivals.filter(occurred_at__date__gte=parsed_from)
        outgoing = outgoing.filter(history_at__date__gte=parsed_from)
    if parsed_to:
        arrivals = arrivals.filter(occurred_at__date__lte=parsed_to)
        outgoing = outgoing.filter(history_at__date__lte=parsed_to)
    return arrivals, outgoing


def _stock_history_arrival_rows(queryset):
    return (
        queryset.order_by()
        .annotate(
            event_at=F("occurred_at"),
            event_id=F("id"),
            direction=Value("arrival", output_field=CharField()),
            agency_id=F("target_balance__agency_id"),
            agency_name=F("target_balance__agency__agn_name"),
            sku_code=F("target_balance__sku_code"),
            external_sku=Value("", output_field=CharField()),
            product_name=F("target_balance__name"),
            barcode=F("target_balance__barcode"),
            source_container=F("source_snapshot__container_code"),
            source_zone=F("source_snapshot__zone_code"),
            fbs_box=F("target_balance__box__box_code"),
            fbs_pallet=F("target_balance__box__pallet__pallet_code"),
            fbs_cell=F("target_balance__box__pallet__cell__cell_code"),
            plan_id=F("allocation__line__plan_id"),
            request_id=F("allocation__line__plan__client_movement_request_id"),
            order_id=Value(None, output_field=BigIntegerField()),
            external_order_id=Value("", output_field=CharField()),
            marketplace=Value("", output_field=CharField()),
            order_status=Value("", output_field=CharField()),
            pick_batch_id=Value(None, output_field=BigIntegerField()),
        )
        .values(*STOCK_HISTORY_VALUE_FIELDS)
    )


def _stock_history_outgoing_rows(queryset):
    return (
        queryset.order_by()
        .annotate(
            event_at=F("history_at"),
            event_id=F("id"),
            direction=Value("outgoing", output_field=CharField()),
            agency_id=F("balance__agency_id"),
            agency_name=F("balance__agency__agn_name"),
            sku_code=F("balance__sku_code"),
            external_sku=F("order_item__external_sku"),
            product_name=F("balance__name"),
            barcode=F("balance__barcode"),
            qty=F("qty_picked"),
            source_container=Value("", output_field=CharField()),
            source_zone=Value("", output_field=CharField()),
            fbs_box=F("balance__box__box_code"),
            fbs_pallet=F("balance__box__pallet__pallet_code"),
            fbs_cell=F("balance__box__pallet__cell__cell_code"),
            plan_id=Value(None, output_field=BigIntegerField()),
            request_id=Value(None, output_field=BigIntegerField()),
            order_id=F("order_item__order_id"),
            external_order_id=F("order_item__order__external_order_id"),
            marketplace=F("order_item__order__profile__marketplace"),
            order_status=F("order_item__order__internal_status"),
            pick_batch_id=F("pick_task__batch_id"),
        )
        .values(*STOCK_HISTORY_VALUE_FIELDS)
    )


def _stock_history_location(*parts) -> str:
    return " · ".join(
        str(part).strip() for part in parts if str(part or "").strip()
    ) or "—"


def _decorate_stock_history_rows(rows) -> None:
    marketplace_labels = dict(FbsIntegrationProfile.MARKETPLACE_CHOICES)
    for row in rows:
        row["article"] = row["sku_code"] or row["external_sku"] or "—"
        if row["direction"] == "arrival":
            row["direction_label"] = "Приход"
            row["direction_status"] = "approved"
            row["source_label"] = _stock_history_location(
                row["source_container"] or "Общий склад", row["source_zone"]
            )
            row["destination_label"] = _stock_history_location(
                "FBS", row["fbs_box"], row["fbs_pallet"], row["fbs_cell"]
            )
            if row["request_id"]:
                row["reference_label"] = f"FBS-MOV-{int(row['request_id']):06d}"
            else:
                row["reference_label"] = f"План FBS #{row['plan_id']}"
            row["status_label"] = "Перемещено в FBS"
        else:
            marketplace_label = marketplace_labels.get(
                row["marketplace"], str(row["marketplace"] or "").upper()
            )
            row["direction_label"] = "Расход"
            row["direction_status"] = "picked"
            row["source_label"] = _stock_history_location(
                "FBS", row["fbs_box"], row["fbs_pallet"], row["fbs_cell"]
            )
            row["destination_label"] = _stock_history_location(
                marketplace_label, f"заказ {row['external_order_id']}"
            )
            row["reference_label"] = f"{marketplace_label} · {row['external_order_id']}"
            row["status_label"] = OPERATOR_INTERNAL_STATUS_LABELS.get(
                row["order_status"], row["order_status"] or "Отобрано"
            )


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_GET
def operator_stock_history(request):
    query = str(request.GET.get("q") or "").strip()
    agency_filter = str(request.GET.get("agency") or "").strip()
    if not agency_filter.isdigit():
        agency_filter = ""
    direction = str(request.GET.get("direction") or "").strip()
    if direction not in dict(STOCK_HISTORY_DIRECTION_CHOICES):
        direction = ""

    today = timezone.localdate()
    default_from = today - timedelta(days=30)
    date_from_value = (
        request.GET.get("date_from")
        if "date_from" in request.GET
        else default_from.isoformat()
    )
    date_to_value = (
        request.GET.get("date_to")
        if "date_to" in request.GET
        else today.isoformat()
    )
    date_from, parsed_from = _stock_history_date(date_from_value)
    date_to, parsed_to = _stock_history_date(date_to_value)
    if date_from and parsed_from is None:
        date_from = ""
    if date_to and parsed_to is None:
        date_to = ""

    arrivals, outgoing = _stock_history_sources(
        agency_filter=agency_filter,
        query=query,
        parsed_from=parsed_from,
        parsed_to=parsed_to,
    )
    zero_stats = {"events": 0, "qty": 0}
    arrival_stats = (
        arrivals.aggregate(events=Count("id"), qty=Coalesce(Sum("qty"), 0))
        if direction != "outgoing"
        else zero_stats
    )
    outgoing_stats = (
        outgoing.aggregate(
            events=Count("id"), qty=Coalesce(Sum("qty_picked"), 0)
        )
        if direction != "arrival"
        else zero_stats
    )

    if direction == "arrival":
        history = _stock_history_arrival_rows(arrivals)
    elif direction == "outgoing":
        history = _stock_history_outgoing_rows(outgoing)
    else:
        history = _stock_history_arrival_rows(arrivals).union(
            _stock_history_outgoing_rows(outgoing), all=True
        )
    history = history.order_by("-event_at", "-event_id")
    page_size = _clean_page_size(request.GET.get("page_size"))
    page = Paginator(history, page_size).get_page(request.GET.get("page"))
    page.object_list = list(page.object_list)
    _decorate_stock_history_rows(page.object_list)

    balances = FbsStockBalance.objects.all()
    if agency_filter:
        balances = balances.filter(agency_id=int(agency_filter))
    if query:
        balances = balances.filter(
            Q(sku_code__icontains=query)
            | Q(barcode__icontains=query)
            | Q(name__icontains=query)
        )
    current_stock = balances.aggregate(
        qty=Coalesce(Sum("qty"), 0),
        available=Coalesce(Sum("available_qty"), 0),
    )

    return render(
        request,
        "fbs/operator_stock_history.html",
        _base_context(
            request,
            section="stock_history",
            page_title="FBS · История товара",
            back_url=reverse("fbs:operator_order_reports"),
            page=page,
            page_size=page_size,
            page_sizes=QUEUE_PAGE_SIZES,
            agencies=Agency.objects.filter(
                Q(fbs_integration_profiles__isnull=False)
                | Q(fbs_stock_balances__isnull=False)
            )
            .distinct()
            .order_by("agn_name", "id"),
            query=query,
            agency_filter=agency_filter,
            direction=direction,
            direction_choices=STOCK_HISTORY_DIRECTION_CHOICES,
            date_from=date_from,
            date_to=date_to,
            summary={
                "arrival_qty": int(arrival_stats["qty"] or 0),
                "outgoing_qty": int(outgoing_stats["qty"] or 0),
                "current_qty": int(current_stock["qty"] or 0),
                "current_available": int(current_stock["available"] or 0),
                "operations": int(arrival_stats["events"] or 0)
                + int(outgoing_stats["events"] or 0),
            },
        ),
    )


STOCK_ITEM_WINDOW_DAYS = 30
STOCK_ITEM_SOURCE_LIMIT = 200
STOCK_ITEM_BLOCKER_LIMIT = 50
STOCK_ITEM_MARKING_LIMIT = 200
STOCK_ITEM_ORDER_LIMIT = 200
STOCK_ITEM_EXPORT_LIMIT = 20000
STOCK_ITEM_STALE_RESERVE_HOURS = 24
STOCK_ITEM_KIND_CHOICES = (
    ("arrival", "Приход на FBS"),
    ("internal", "Перемещение внутри FBS"),
    ("inventory", "Инвентаризация"),
    ("reserve", "Резерв"),
    ("pick", "Отбор"),
    ("scan", "Сканирование"),
    ("problem", "Проблемы"),
    ("restock", "Возврат на место"),
    ("marking", "КИЗ и сроки"),
    ("control", "Контроль"),
    ("handover", "Отгрузка"),
)
STOCK_ITEM_ORDER_KINDS = ("control", "handover")


def _stock_item_identity(request):
    """Товар определяется клиентом и артикулом; ШК — запасной ключ, КИЗ — сужение."""
    agency_raw = str(request.GET.get("agency") or "").strip()
    if not agency_raw.isdigit():
        raise Http404("Клиент не указан")
    sku_code = str(request.GET.get("sku") or "").strip()
    barcode = str(request.GET.get("barcode") or "").strip()
    marking = str(request.GET.get("marking") or "").strip()
    if not sku_code and not barcode:
        raise Http404("Товар не указан")
    return {
        "agency_id": int(agency_raw),
        "sku_code": sku_code,
        "barcode": barcode,
        "marking": marking,
    }


def _stock_item_scope_q(prefix, scope) -> Q:
    """Фильтр по клиенту и артикулу через join — списки id не материализуем."""
    filters = {f"{prefix}agency_id": scope["agency_id"]}
    if scope["sku_code"]:
        filters[f"{prefix}sku_code"] = scope["sku_code"]
    else:
        filters[f"{prefix}barcode"] = scope["barcode"]
    if scope["marking"]:
        filters[f"{prefix}marking_code"] = scope["marking"]
    return Q(**filters)


def _stock_item_period(request) -> dict:
    """По умолчанию окно 30 дней: у ходовой позиции событий тысячи."""
    if str(request.GET.get("period") or "").strip() == "all":
        return {"mode": "all", "date_from": "", "date_to": "", "from": None, "to": None}
    today = timezone.localdate()
    raw_from = (
        request.GET.get("date_from")
        if "date_from" in request.GET
        else (today - timedelta(days=STOCK_ITEM_WINDOW_DAYS)).isoformat()
    )
    raw_to = request.GET.get("date_to") if "date_to" in request.GET else today.isoformat()
    date_from, parsed_from = _stock_history_date(raw_from)
    date_to, parsed_to = _stock_history_date(raw_to)
    if date_from and parsed_from is None:
        date_from = ""
    if date_to and parsed_to is None:
        date_to = ""
    return {
        "mode": "window",
        "date_from": date_from,
        "date_to": date_to,
        "from": parsed_from,
        "to": parsed_to,
    }


def _stock_item_window_q(period, *fields) -> Q:
    if not period["from"] and not period["to"]:
        return Q()
    combined = Q()
    for field in fields:
        clause = Q(**{f"{field}__isnull": False})
        if period["from"]:
            clause &= Q(**{f"{field}__date__gte": period["from"]})
        if period["to"]:
            clause &= Q(**{f"{field}__date__lte": period["to"]})
        combined |= clause
    return combined


def _stock_item_window_bounds(period):
    """Границы окна считаем один раз: событий тысячи, localtime на каждом — дорого."""
    current_zone = timezone.get_current_timezone()
    start = (
        timezone.make_aware(datetime.combine(period["from"], day_time.min), current_zone)
        if period["from"]
        else None
    )
    end = (
        timezone.make_aware(datetime.combine(period["to"], day_time.max), current_zone)
        if period["to"]
        else None
    )
    return start, end


def _stock_item_in_window(moment, bounds) -> bool:
    if moment is None:
        return False
    start, end = bounds
    if start and moment < start:
        return False
    if end and moment > end:
        return False
    return True


_STOCK_ITEM_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]")


def _stock_item_text(value) -> str:
    """КИЗ и коды сканирования содержат служебные разделители: Excel их не принимает."""
    if value is None:
        return ""
    return _STOCK_ITEM_CONTROL_CHARS_RE.sub("·", str(value))


def _stock_item_box_labels(scope) -> dict:
    """Метки мест строим один раз по коробам: их единицы, а событий тысячи."""
    box_ids = list(
        FbsStockBalance.objects.filter(_stock_item_scope_q("", scope))
        .exclude(box_id=None)
        .order_by()  # Meta.ordering иначе попадёт в DISTINCT и вернёт строки, а не короба
        .values_list("box_id", flat=True)
        .distinct()[:200]
    )
    labels = {}
    if not box_ids:
        return labels
    for box in FbsBox.objects.filter(id__in=box_ids).select_related("pallet__cell__location"):
        cell = box.pallet.cell
        location = str(cell.warehouse_location_label or "").strip() or str(cell.cell_code or "")
        labels[box.pk] = _stock_history_location(
            f"Ячейка {location}" if location else "",
            f"паллета {box.pallet.pallet_code}",
            f"короб {box.box_code}",
        )
    return labels


def _stock_item_place_label(balance, labels=None) -> str:
    if balance is None or balance.box_id is None:
        return "Место не указано"
    if labels is not None and balance.box_id in labels:
        return labels[balance.box_id]
    box = balance.box
    location = str(box.pallet.cell.warehouse_location_label or "").strip() or str(
        box.pallet.cell.cell_code or ""
    )
    label = _stock_history_location(
        f"Ячейка {location}" if location else "",
        f"паллета {box.pallet.pallet_code}",
        f"короб {box.box_code}",
    )
    if labels is not None:
        # промах карты стоит запросов — запоминаем, чтобы он был один на короб
        labels[balance.box_id] = label
    return label


def _stock_item_order_doc(order, cache=None):
    """reverse() на каждое событие заметен на тысячах строк — кешируем по заказу."""
    if order is None:
        return "", ""
    if cache is not None and order.pk in cache:
        return cache[order.pk]
    doc = (
        f"{order.profile.get_marketplace_display()} · {order.external_order_id}",
        reverse("fbs:operator_order_detail", kwargs={"order_id": order.pk}),
    )
    if cache is not None:
        cache[order.pk] = doc
    return doc


def _stock_item_profile(scope):
    """Каноничные данные позиции: имя, ШК, суммы, партии, места."""
    balances = FbsStockBalance.objects.filter(_stock_item_scope_q("", scope))
    totals = balances.aggregate(
        qty=Coalesce(Sum("qty"), 0),
        available=Coalesce(Sum("available_qty"), 0),
        reserved=Coalesce(Sum("reserved_qty"), 0),
        rows=Count("id"),
        marked=Count("id", filter=~Q(marking_code="")),
        updated=Max("updated_at"),
    )
    sample = (
        balances.select_related("agency", "sku_ref")
        .order_by("-qty", "-updated_at", "id")
        .first()
    )
    barcodes = [
        value
        for value in balances.exclude(barcode="")
        .values_list("barcode", flat=True)
        .distinct()[:20]
    ]
    return balances, totals, sample, barcodes


def _stock_item_placements(balances, *, show_empty: bool):
    """Где сейчас: агрегат по коробу и партии, а не список строк баланса."""
    groups = list(
        balances.values(
            "box_id",
            "box__box_code",
            "box__pallet__pallet_code",
            "box__pallet__cell_id",
            "box__pallet__cell__cell_code",
            "lot_code",
            "expiry_date",
        )
        .annotate(
            qty=Coalesce(Sum("qty"), 0),
            available=Coalesce(Sum("available_qty"), 0),
            reserved=Coalesce(Sum("reserved_qty"), 0),
            rows=Count("id"),
            marked=Count("id", filter=~Q(marking_code="")),
            updated=Max("updated_at"),
        )
        .order_by("-qty", "box__box_code")[:500]
    )
    if not show_empty:
        groups = [group for group in groups if int(group["qty"] or 0) > 0]
    cell_ids = {group["box__pallet__cell_id"] for group in groups if group["box__pallet__cell_id"]}
    cell_labels = {}
    if cell_ids:
        for cell in FbsStorageCell.objects.filter(id__in=cell_ids).select_related("location"):
            cell_labels[cell.pk] = str(cell.warehouse_location_label or "").strip() or str(
                cell.cell_code or ""
            )
    for group in groups:
        group["cell_label"] = cell_labels.get(
            group["box__pallet__cell_id"], group["box__pallet__cell__cell_code"] or "—"
        )
    return groups


def _stock_item_blockers(scope, *, barcodes, order_ids, box_labels):
    """Только активные блокировки — то, из-за чего товар стоит прямо сейчас."""
    rows = []

    def _stock_item_place_label_mapped(balance):
        return _stock_item_place_label(balance, box_labels)

    def add(at, *, title, detail="", actor="", place="", url="", severity="problem"):
        rows.append(
            {
                "at": at,
                "title": title,
                "detail": detail,
                "actor": actor,
                "place": place,
                "url": url,
                "severity": severity,
            }
        )

    exceptions = (
        FbsPickException.objects.filter(
            _stock_item_scope_q("allocation__balance__", scope),
            status=FbsPickException.STATUS_OPEN,
        )
        .select_related("created_by", "task__order__profile", "allocation__balance")
        .order_by("-created_at")[:STOCK_ITEM_BLOCKER_LIMIT]
    )
    for exception in exceptions:
        doc_label, doc_url = _stock_item_order_doc(
            exception.task.order if exception.task_id else None
        )
        add(
            exception.created_at,
            title=f"Проблема сборки: {exception.get_exception_type_display()}",
            detail=exception.reason or doc_label,
            actor=_fbs_actor_label(exception.created_by),
            place=_stock_item_place_label_mapped(
                exception.allocation.balance if exception.allocation_id else None
            ),
            url=doc_url,
        )

    problem_filter = Q(order_item__order__profile__agency_id=scope["agency_id"])
    if barcodes:
        problem_filter &= Q(order_item__barcode__in=barcodes)
    elif scope["sku_code"]:
        problem_filter &= Q(order_item__external_sku=scope["sku_code"])
    else:
        problem_filter &= Q(order_item__barcode=scope["barcode"])
    problems = (
        FbsProblemToteItem.objects.filter(
            problem_filter, status=FbsProblemToteItem.STATUS_IN_TOTE
        )
        .select_related("problem_tote", "reported_by", "order__profile")
        .order_by("-reported_at")[:STOCK_ITEM_BLOCKER_LIMIT]
    )
    for problem in problems:
        doc_label, doc_url = _stock_item_order_doc(problem.order)
        add(
            problem.reported_at,
            title="Лежит в проблемной таре",
            detail=" · ".join(
                value
                for value in (
                    problem.reason,
                    problem.get_severity_display(),
                    f"{problem.quantity} шт.",
                    doc_label,
                )
                if value
            ),
            actor=_fbs_actor_label(problem.reported_by),
            place=_fbs_tote_label(problem.problem_tote),
            url=doc_url,
            severity="critical"
            if problem.severity == FbsProblemToteItem.SEVERITY_CRITICAL
            else "problem",
        )

    if barcodes:
        unknown_items = (
            FbsUnknownToteItem.objects.filter(
                scanned_value__in=barcodes, status=FbsUnknownToteItem.STATUS_WAITING
            )
            .select_related("unknown_tote", "reported_by")
            .order_by("-reported_at")[:STOCK_ITEM_BLOCKER_LIMIT]
        )
        for unknown in unknown_items:
            add(
                unknown.reported_at,
                title="Лежит в таре неопознанного",
                detail=" · ".join(
                    value
                    for value in (
                        unknown.comment,
                        "совпадение по штрихкоду, принадлежность не подтверждена",
                    )
                    if value
                ),
                actor=_fbs_actor_label(unknown.reported_by),
                place=_fbs_tote_label(unknown.unknown_tote),
            )

    restock_lines = (
        FbsPickRestockLine.objects.filter(
            _stock_item_scope_q("source_balance__", scope),
            status__in=(
                FbsPickRestockLine.STATUS_PENDING,
                FbsPickRestockLine.STATUS_IN_PROGRESS,
            ),
            request__status__in=ACTIVE_PICK_RESTOCK_STATUSES,
        )
        .select_related(
            "request__order__profile",
            "request__assigned_to",
            "request__source_tote",
            "source_balance",
        )
        .order_by("-created_at")[:STOCK_ITEM_BLOCKER_LIMIT]
    )
    for line in restock_lines:
        restock_request = line.request
        doc_label, doc_url = _stock_item_order_doc(restock_request.order)
        add(
            line.created_at,
            title="Ждёт возврата на место",
            detail=" · ".join(
                value
                for value in (
                    restock_request.reason or restock_request.get_reason_code_display(),
                    f"{line.returned_qty} из {line.planned_qty} шт.",
                    restock_request.get_status_display(),
                    doc_label,
                )
                if value
            ),
            actor=_fbs_actor_label(restock_request.assigned_to, fallback="Исполнитель не назначен"),
            place=_fbs_tote_label(restock_request.source_tote)
            if restock_request.source_tote_id
            else _stock_item_place_label_mapped(line.source_balance),
            url=doc_url,
        )

    stale_edge = timezone.now() - timedelta(hours=STOCK_ITEM_STALE_RESERVE_HOURS)
    stale_reserves = (
        FbsOrderStockAllocation.objects.filter(
            _stock_item_scope_q("balance__", scope),
            status=FbsOrderStockAllocation.STATUS_RESERVED,
            picked_at__isnull=True,
            reserved_at__lt=stale_edge,
        )
        .select_related(
            "order_item__order__profile",
            "balance",
            "pick_task__batch",
        )
        .order_by("reserved_at")[:STOCK_ITEM_BLOCKER_LIMIT]
    )
    for allocation in stale_reserves:
        order = allocation.order_item.order if allocation.order_item_id else None
        doc_label, doc_url = _stock_item_order_doc(order)
        wave = (
            f"волна #{allocation.pick_task.batch_id}"
            if allocation.pick_task_id
            else "волна не назначена"
        )
        add(
            allocation.reserved_at,
            title="Резерв висит больше суток",
            detail=" · ".join(
                value for value in (f"{allocation.qty_reserved} шт.", wave, doc_label) if value
            ),
            actor=_fbs_actor_label(allocation.reserved_by),
            place=_stock_item_place_label_mapped(allocation.balance),
            url=doc_url,
            severity="warning",
        )

    if order_ids:
        troubled_orders = (
            FbsOrder.objects.filter(id__in=order_ids)
            .filter(
                Q(internal_status__in=(FbsOrder.STATUS_EXCEPTION, FbsOrder.STATUS_VALIDATION_FAILED))
                | ~Q(problem_reason="")
                | ~Q(hold_reason="")
            )
            .select_related("profile")
            .order_by("-updated_at")[:STOCK_ITEM_BLOCKER_LIMIT]
        )
        for order in troubled_orders:
            doc_label, doc_url = _stock_item_order_doc(order)
            add(
                order.updated_at,
                title=f"Заказ в состоянии «{OPERATOR_INTERNAL_STATUS_LABELS.get(order.internal_status, order.internal_status)}»",
                detail=order.problem_reason or order.hold_reason or doc_label,
                actor="Маркетплейс" if order.hold_reason else "Система",
                url=doc_url,
            )

    rows.sort(key=lambda row: row["at"] or timezone.now(), reverse=True)
    return rows


def _stock_item_timeline(scope, *, period, barcodes, include_orders, box_labels):
    """Хронология позиции. level='item' — событие штуки, level='order' — контекст заказа."""
    rows = []
    truncated = False
    kind_labels = dict(STOCK_ITEM_KIND_CHOICES)
    bounds = _stock_item_window_bounds(period)
    order_doc_cache = {}

    def _stock_item_order_doc_cached(order):
        return _stock_item_order_doc(order, order_doc_cache)

    def _stock_item_place_label_cached(balance):
        return _stock_item_place_label(balance, box_labels)

    def add(
        at,
        *,
        kind,
        title,
        actor="Система",
        place="",
        qty=None,
        detail="",
        result="",
        doc_label="",
        doc_url="",
        level="item",
    ):
        if not _stock_item_in_window(at, bounds):
            return
        rows.append(
            {
                "at": at,
                "kind": kind,
                "kind_label": kind_labels.get(kind, kind),
                "level": level,
                "title": title,
                "actor": actor,
                "place": place or "—",
                "qty": qty,
                "detail": detail,
                "result": result,
                "doc_label": doc_label,
                "doc_url": doc_url,
            }
        )

    def collect(queryset):
        nonlocal truncated
        items = list(queryset[:STOCK_ITEM_SOURCE_LIMIT])
        if len(items) >= STOCK_ITEM_SOURCE_LIMIT:
            truncated = True
        return items

    movements = collect(
        FbsStockMovement.objects.filter(
            _stock_item_scope_q("target_balance__", scope),
            _stock_item_window_q(period, "occurred_at"),
        )
        .select_related(
            "performed_by",
            "source_snapshot",
            "target_balance",
            "allocation__line__plan",
        )
        .order_by("-occurred_at", "-id")
    )
    for movement in movements:
        plan = movement.allocation.line.plan if movement.allocation_id else None
        if plan is not None and plan.client_movement_request_id:
            doc_label = f"FBS-MOV-{int(plan.client_movement_request_id):06d}"
            doc_url = reverse(
                "fbs:operator_movement_detail",
                kwargs={"request_id": plan.client_movement_request_id},
            )
        elif plan is not None:
            doc_label = f"План FBS #{plan.pk}"
            doc_url = reverse("fbs:tsd_storekeeper_plan", kwargs={"plan_id": plan.pk})
        else:
            doc_label, doc_url = "", ""
        add(
            movement.occurred_at,
            kind="arrival",
            title="Принят на FBS",
            actor=_fbs_actor_label(movement.performed_by),
            place=_stock_item_place_label_cached(movement.target_balance),
            qty=int(movement.qty or 0),
            detail="Со склада: "
            + _stock_history_location(
                movement.source_snapshot.container_code, movement.source_snapshot.zone_code
            ),
            doc_label=doc_label,
            doc_url=doc_url,
        )

    prepared_items = collect(
        FbsReplenishmentPreparedBoxItem.objects.filter(
            _stock_item_scope_q("target_balance__", scope),
            _stock_item_window_q(period, "created_at"),
        )
        .select_related("prepared_box__placed_by")
        .order_by("-created_at", "-id")
    )
    for prepared in prepared_items:
        box = prepared.prepared_box
        add(
            prepared.created_at,
            kind="arrival",
            title="Сложен в короб подсорта",
            actor=_fbs_actor_label(box.placed_by),
            place=f"короб подсорта {box.box_code}",
            qty=int(prepared.qty_placed or prepared.qty_scanned or 0),
            detail=f"Отсканировано {prepared.qty_scanned} шт., размещено {prepared.qty_placed} шт.",
        )

    internal_movements = collect(
        FbsInternalMovement.objects.filter(
            _stock_item_scope_q("source_balance__", scope),
            _stock_item_window_q(period, "started_at", "completed_at"),
        )
        .select_related(
            "assigned_to",
            "requested_by",
            "source_box",
            "target_box",
            "target_pallet",
            "source_balance",
        )
        .order_by("-created_at", "-id")
    )
    for movement in internal_movements:
        target = _stock_history_location(
            f"короб {movement.target_box.box_code}" if movement.target_box_id else "",
            f"паллета {movement.target_pallet.pallet_code}" if movement.target_pallet_id else "",
        )
        source = (
            f"короб {movement.source_box.box_code}"
            if movement.source_box_id
            else _stock_item_place_label_cached(movement.source_balance)
        )
        add(
            movement.started_at,
            kind="internal",
            title="Перемещение внутри FBS начато",
            actor=_fbs_actor_label(movement.assigned_to, fallback="Исполнитель не указан"),
            place=source,
            qty=int(movement.planned_qty or 0),
            detail=f"{source} → {target}",
            result=movement.get_status_display(),
        )
        add(
            movement.completed_at,
            kind="internal",
            title="Перемещение внутри FBS завершено",
            actor=_fbs_actor_label(movement.assigned_to, fallback="Исполнитель не указан"),
            place=target,
            qty=int(movement.moved_qty or 0),
            detail=f"{source} → {target}",
            result=movement.get_status_display(),
        )

    inventory_scans = collect(
        FbsInventoryScan.objects.filter(
            _stock_item_scope_q("line__balance__", scope),
            _stock_item_window_q(period, "created_at"),
        )
        .select_related(
            "counted_by",
            "line__session",
            "line__balance",
        )
        .order_by("-created_at", "-id")
    )
    for scan in inventory_scans:
        line = scan.line
        add(
            scan.created_at,
            kind="inventory",
            title=f"Инвентаризация: пересчёт №{scan.count_round}",
            actor=_fbs_actor_label(scan.counted_by, fallback="Счётчик не указан"),
            place=_stock_item_place_label_cached(line.balance),
            qty=int(scan.qty or 0),
            detail=f"Ожидалось {line.expected_qty} шт.; скан {_stock_item_text(scan.scan_code) or '—'}",
            doc_label=f"Инвентаризация #{line.session_id}",
            doc_url=reverse("fbs:tsd_inventory_detail", kwargs={"session_id": line.session_id}),
        )

    allocations = collect(
        FbsOrderStockAllocation.objects.filter(
            _stock_item_scope_q("balance__", scope),
            _stock_item_window_q(period, "reserved_at", "picked_at", "released_at"),
        )
        .select_related(
            "balance",
            "order_item__order__profile",
            "pick_task__batch__assigned_to",
            "reserved_by",
            "picked_by",
            "released_by",
        )
        .defer(
            "order_item__raw_payload",
            "order_item__requirements",
            "order_item__order__raw_payload",
        )
        .order_by("-reserved_at", "-id")
    )
    order_ids = []
    seen_batches = set()
    for allocation in allocations:
        order = allocation.order_item.order if allocation.order_item_id else None
        if order is not None and order.pk not in order_ids:
            order_ids.append(order.pk)
        doc_label, doc_url = _stock_item_order_doc_cached(order)
        place = _stock_item_place_label_cached(allocation.balance)
        wave = (
            f"волна #{allocation.pick_task.batch_id}"
            if allocation.pick_task_id
            else "волна не назначена"
        )
        add(
            allocation.reserved_at,
            kind="reserve",
            title="Зарезервирован под заказ",
            actor=_fbs_actor_label(allocation.reserved_by),
            place=place,
            qty=int(allocation.qty_reserved or 0),
            detail=wave,
            result=allocation.get_status_display(),
            doc_label=doc_label,
            doc_url=doc_url,
        )
        add(
            allocation.picked_at,
            kind="pick",
            title="Отобран со своего места",
            actor=_fbs_actor_label(allocation.picked_by, fallback="Сборщик не указан"),
            place=place,
            qty=int(allocation.qty_picked or 0),
            detail=wave,
            result=allocation.get_status_display(),
            doc_label=doc_label,
            doc_url=doc_url,
        )
        add(
            allocation.released_at,
            kind="reserve",
            title="Резерв освобождён",
            actor=_fbs_actor_label(allocation.released_by),
            place=place,
            qty=int(allocation.qty_reserved or 0),
            detail=wave,
            result=allocation.get_status_display(),
            doc_label=doc_label,
            doc_url=doc_url,
        )
        if include_orders and allocation.pick_task_id:
            task = allocation.pick_task
            if task.batch_id not in seen_batches:
                seen_batches.add(task.batch_id)
                batch = task.batch
                add(
                    batch.started_at,
                    kind="pick",
                    title=f"Волна #{batch.pk} запущена",
                    actor=_fbs_actor_label(batch.assigned_to, fallback="Сборщик не указан"),
                    detail=f"{batch.picked_qty} из {batch.planned_qty} шт. по волне",
                    result=batch.get_status_display(),
                    level="order",
                    doc_label=f"Волна #{batch.pk}",
                    doc_url=reverse("fbs:operator_wave_detail", kwargs={"batch_id": batch.pk}),
                )
                add(
                    batch.completed_at,
                    kind="pick",
                    title=f"Волна #{batch.pk} завершена",
                    actor=_fbs_actor_label(batch.assigned_to, fallback="Сборщик не указан"),
                    detail=f"{batch.picked_qty} из {batch.planned_qty} шт. по волне",
                    result=batch.get_status_display(),
                    level="order",
                    doc_label=f"Волна #{batch.pk}",
                    doc_url=reverse("fbs:operator_wave_detail", kwargs={"batch_id": batch.pk}),
                )

    scan_events = collect(
        FbsPickScanEvent.objects.filter(
            _stock_item_scope_q("allocation__balance__", scope),
            _stock_item_window_q(period, "created_at"),
        )
        .select_related(
            "created_by",
            "task__order__profile",
            "allocation__balance",
        )
        .defer("task__order__raw_payload")
        .order_by("-created_at", "-id")
    )
    for event in scan_events:
        order = event.task.order if event.task_id else None
        doc_label, doc_url = _stock_item_order_doc_cached(order)
        detail_parts = [f"Скан {_stock_item_text(event.scan_value) or 'пустой'}"]
        if event.expected_value:
            detail_parts.append(f"ожидалось {_stock_item_text(event.expected_value)}")
        if event.quantity_after is not None:
            detail_parts.append(f"после скана {event.quantity_after} шт.")
        if event.message:
            detail_parts.append(event.message)
        add(
            event.created_at,
            kind="scan",
            title=f"Скан: {event.get_stage_display()}",
            actor=_fbs_actor_label(event.created_by),
            place=_stock_item_place_label_cached(
                event.allocation.balance if event.allocation_id else None
            ),
            detail="; ".join(detail_parts),
            result=event.get_result_display(),
            doc_label=doc_label or f"Волна #{event.batch_id}",
            doc_url=doc_url,
        )

    exceptions = collect(
        FbsPickException.objects.filter(
            _stock_item_scope_q("allocation__balance__", scope),
            _stock_item_window_q(period, "created_at", "resolved_at"),
        )
        .select_related("created_by", "resolved_by", "task__order__profile")
        .defer("task__order__raw_payload")
        .order_by("-created_at", "-id")
    )
    for exception in exceptions:
        order = exception.task.order if exception.task_id else None
        doc_label, doc_url = _stock_item_order_doc_cached(order)
        add(
            exception.created_at,
            kind="problem",
            title=f"Проблема сборки: {exception.get_exception_type_display()}",
            actor=_fbs_actor_label(exception.created_by),
            detail=exception.reason,
            result=exception.get_status_display(),
            doc_label=doc_label,
            doc_url=doc_url,
        )
        add(
            exception.resolved_at,
            kind="problem",
            title="Проблема сборки закрыта",
            actor=_fbs_actor_label(exception.resolved_by),
            detail=exception.get_exception_type_display(),
            result=exception.get_status_display(),
            doc_label=doc_label,
            doc_url=doc_url,
        )

    restock_lines = collect(
        FbsPickRestockLine.objects.filter(
            _stock_item_scope_q("source_balance__", scope),
            _stock_item_window_q(period, "created_at", "completed_at"),
        )
        .select_related(
            "request__order__profile",
            "request__created_by",
            "request__assigned_to",
            "source_balance",
        )
        .defer("request__order__raw_payload")
        .order_by("-created_at", "-id")
    )
    for line in restock_lines:
        restock_request = line.request
        doc_label, doc_url = _stock_item_order_doc_cached(restock_request.order)
        add(
            line.created_at,
            kind="restock",
            title="Заявка на возврат товара на место",
            actor=_fbs_actor_label(restock_request.created_by),
            place=_stock_item_place_label_cached(line.source_balance),
            qty=int(line.planned_qty or 0),
            detail=restock_request.reason or restock_request.get_reason_code_display(),
            result=restock_request.get_status_display(),
            doc_label=doc_label,
            doc_url=doc_url,
        )
        add(
            line.completed_at,
            kind="restock",
            title="Возвращён на место",
            actor=_fbs_actor_label(restock_request.assigned_to, fallback="Исполнитель не указан"),
            place=_stock_item_place_label_cached(line.source_balance),
            qty=int(line.returned_qty or 0),
            detail=f"{line.returned_qty} из {line.planned_qty} шт.",
            result=restock_request.get_status_display(),
            doc_label=doc_label,
            doc_url=doc_url,
        )

    restock_scans = collect(
        FbsPickRestockScan.objects.filter(
            _stock_item_scope_q("line__source_balance__", scope),
            _stock_item_window_q(period, "created_at"),
        )
        .select_related("created_by", "request__order__profile")
        .defer("request__order__raw_payload")
        .order_by("-created_at", "-id")
    )
    for scan in restock_scans:
        doc_label, doc_url = _stock_item_order_doc_cached(scan.request.order)
        add(
            scan.created_at,
            kind="restock",
            title=f"Возврат, скан: {scan.get_stage_display()}",
            actor=_fbs_actor_label(scan.created_by),
            detail=(
                f"Скан {_stock_item_text(scan.scan_value) or 'пустой'}; "
                f"ожидалось {_stock_item_text(scan.expected_value) or '—'}"
            ),
            result=scan.get_result_display(),
            doc_label=doc_label,
            doc_url=doc_url,
        )

    traces = collect(
        FbsOrderTraceability.objects.filter(
            _stock_item_scope_q("allocation__balance__", scope),
            _stock_item_window_q(period, "created_at"),
        )
        .select_related("allocation__order_item__order__profile")
        .defer(
            "allocation__order_item__raw_payload",
            "allocation__order_item__requirements",
            "allocation__order_item__order__raw_payload",
        )
        .order_by("-created_at", "-id")
    )
    for trace in traces:
        order_item = trace.allocation.order_item if trace.allocation_id else None
        order = order_item.order if order_item is not None else None
        doc_label, doc_url = _stock_item_order_doc_cached(order)
        detail_parts = []
        if trace.marking_code:
            detail_parts.append(f"КИЗ {_stock_item_text(trace.marking_code)}")
        if trace.lot_code:
            detail_parts.append(f"партия {trace.lot_code}")
        if trace.expiry_date:
            detail_parts.append(f"годен до {trace.expiry_date.strftime('%d.%m.%Y')}")
        add(
            trace.created_at,
            kind="marking",
            title="Зафиксированы КИЗ и срок",
            qty=int(trace.qty or 0),
            detail="; ".join(detail_parts) or "Без кода",
            result=trace.get_status_display(),
            doc_label=doc_label,
            doc_url=doc_url,
        )

    problem_filter = Q(order_item__order__profile__agency_id=scope["agency_id"])
    if barcodes:
        problem_filter &= Q(order_item__barcode__in=barcodes)
    elif scope["sku_code"]:
        problem_filter &= Q(order_item__external_sku=scope["sku_code"])
    else:
        problem_filter &= Q(order_item__barcode=scope["barcode"])
    problem_items = collect(
        FbsProblemToteItem.objects.filter(
            problem_filter,
            _stock_item_window_q(period, "reported_at", "resolved_at"),
        )
        .select_related("problem_tote", "reported_by", "resolved_by", "order__profile")
        .defer("order__raw_payload", "order_item__raw_payload", "order_item__requirements")
        .order_by("-reported_at", "-id")
    )
    for problem in problem_items:
        doc_label, doc_url = _stock_item_order_doc_cached(problem.order)
        add(
            problem.reported_at,
            kind="problem",
            title="Отложен в проблемную тару",
            actor=_fbs_actor_label(problem.reported_by),
            place=_fbs_tote_label(problem.problem_tote),
            qty=int(problem.quantity or 0),
            detail=" · ".join(
                value for value in (problem.reason, problem.get_severity_display()) if value
            ),
            result=problem.get_status_display(),
            doc_label=doc_label,
            doc_url=doc_url,
        )
        add(
            problem.resolved_at,
            kind="problem",
            title="Забран из проблемной тары",
            actor=_fbs_actor_label(problem.resolved_by),
            place=_fbs_tote_label(problem.problem_tote),
            qty=int(problem.quantity or 0),
            result=problem.get_status_display(),
            doc_label=doc_label,
            doc_url=doc_url,
        )

    if barcodes:
        unknown_items = collect(
            FbsUnknownToteItem.objects.filter(
                Q(scanned_value__in=barcodes),
                _stock_item_window_q(period, "reported_at", "placed_at"),
            )
            .select_related("unknown_tote", "reported_by", "placed_by")
            .order_by("-reported_at", "-id")
        )
        for unknown in unknown_items:
            add(
                unknown.reported_at,
                kind="problem",
                title="Отложен в тару неопознанного",
                actor=_fbs_actor_label(unknown.reported_by),
                place=_fbs_tote_label(unknown.unknown_tote),
                detail="Совпадение по штрихкоду, принадлежность не подтверждена",
                result=unknown.get_status_display(),
            )
            add(
                unknown.placed_at,
                kind="problem",
                title="Разобран из тары неопознанного",
                actor=_fbs_actor_label(unknown.placed_by),
                place=_fbs_tote_label(unknown.unknown_tote),
                result=unknown.get_status_display(),
            )

    order_ids = order_ids[:STOCK_ITEM_ORDER_LIMIT]
    if include_orders and order_ids:
        tote_orders = collect(
            FbsControllerToteOrder.objects.filter(
                order_id__in=order_ids,
            )
            .select_related(
                "order__profile",
                "pick_tote__tote",
                "check_tote__tote",
                "label_confirmed_by",
                "composition_checked_by",
                "transport_box",
            )
            .defer("order__raw_payload")
            .order_by("-updated_at", "-id")
        )
        for tote_order in tote_orders:
            doc_label, doc_url = _stock_item_order_doc_cached(tote_order.order)
            check_tote = tote_order.check_tote.tote if tote_order.check_tote_id else None
            add(
                tote_order.label_confirmed_at,
                kind="control",
                title="Контролёр подтвердил этикетку заказа",
                actor=_fbs_actor_label(tote_order.label_confirmed_by),
                place=_fbs_tote_label(check_tote),
                result=tote_order.get_status_display(),
                level="order",
                doc_label=doc_label,
                doc_url=doc_url,
            )
            add(
                tote_order.composition_checked_at,
                kind="control",
                title="Контролёр подтвердил состав заказа",
                actor=_fbs_actor_label(tote_order.composition_checked_by),
                place=_fbs_tote_label(check_tote),
                qty=int(tote_order.units or 0),
                result=tote_order.get_status_display(),
                level="order",
                doc_label=doc_label,
                doc_url=doc_url,
            )

        handovers = collect(
            FbsHandoverOrder.objects.filter(order_id__in=order_ids)
            .select_related(
                "order__profile",
                "box__batch",
                "added_by",
                "verified_by",
                "box__scanned_by",
                "box__batch__dispatched_by",
            )
            .defer("order__raw_payload", "box__batch__marketplace_payload")
            .order_by("-added_at", "-id")
        )
        for handover in handovers:
            box = handover.box
            batch = box.batch
            doc_label, doc_url = _stock_item_order_doc_cached(handover.order)
            handover_url = reverse("fbs:tsd_handover_detail", kwargs={"batch_id": batch.pk})
            add(
                handover.added_at,
                kind="handover",
                title="Уложен в короб отгрузки",
                actor=_fbs_actor_label(handover.added_by, fallback="Контролёр не указан"),
                place=f"короб {box.qr_code}",
                result=handover.get_status_display(),
                level="order",
                doc_label=doc_label,
                doc_url=doc_url,
            )
            add(
                handover.verified_at,
                kind="handover",
                title="Проверен в коробе отгрузки",
                actor=_fbs_actor_label(handover.verified_by, fallback="Контролёр не указан"),
                place=f"короб {box.qr_code}",
                result=handover.get_status_display(),
                level="order",
                doc_label=doc_label,
                doc_url=doc_url,
            )
            add(
                box.scanned_at,
                kind="handover",
                title="Короб просканирован кладовщиком",
                actor=_fbs_actor_label(box.scanned_by, fallback="Кладовщик не указан"),
                place=f"короб {box.qr_code}",
                result=box.get_status_display(),
                level="order",
                doc_label=f"Отгрузка #{batch.pk}",
                doc_url=handover_url,
            )
            add(
                batch.dispatched_at,
                kind="handover",
                title="Поставка передана перевозчику",
                actor=_fbs_actor_label(batch.dispatched_by, fallback="Кладовщик не указан"),
                place=batch.external_supply_id or f"поставка #{batch.pk}",
                result=batch.get_status_display(),
                level="order",
                doc_label=f"Отгрузка #{batch.pk}",
                doc_url=handover_url,
            )
            add(
                box.accepted_at or batch.accepted_at,
                kind="handover",
                title="Принят маркетплейсом",
                actor="Маркетплейс",
                place=f"короб {box.qr_code}",
                result=box.get_status_display(),
                level="order",
                doc_label=f"Отгрузка #{batch.pk}",
                doc_url=handover_url,
            )

    rows.sort(key=lambda row: row["at"], reverse=True)
    return rows, truncated, order_ids


def _stock_item_filter_rows(rows, *, kinds, item_only, errors_only, order_query, actor_query):
    order_query = order_query.lower()
    actor_query = actor_query.lower()
    filtered = []
    for row in rows:
        if kinds and row["kind"] not in kinds:
            continue
        if item_only and row["level"] != "item":
            continue
        if errors_only and row["result"] != "Ошибка":
            continue
        if order_query and order_query not in row["doc_label"].lower():
            continue
        if actor_query and actor_query not in row["actor"].lower():
            continue
        filtered.append(row)
    return filtered


def _stock_item_export(rows, *, scope, profile_name):
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(title="История товара")
    sheet.append(
        [
            "Дата и время",
            "Уровень",
            "Событие",
            "Количество",
            "Место",
            "Сотрудник",
            "Результат",
            "Документ",
            "Детали",
        ]
    )
    for row in rows[:STOCK_ITEM_EXPORT_LIMIT]:
        sheet.append(
            [
                timezone.localtime(row["at"]).strftime("%d.%m.%Y %H:%M:%S") if row["at"] else "",
                "Товар" if row["level"] == "item" else "Заказ",
                _stock_item_text(row["title"]),
                row["qty"] if row["qty"] is not None else "",
                _stock_item_text(row["place"]),
                _stock_item_text(row["actor"]),
                _stock_item_text(row["result"]),
                _stock_item_text(row["doc_label"]),
                _stock_item_text(row["detail"]),
            ]
        )
    output = BytesIO()
    workbook.save(output)
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    stamp = timezone.localdate().isoformat()
    article = scope["sku_code"] or scope["barcode"] or profile_name
    safe_article = "".join(
        symbol if symbol.isalnum() or symbol in "-_" else "_" for symbol in str(article)
    )[:40]
    response["Content-Disposition"] = (
        f'attachment; filename="fbs_item_history_{safe_article}_{stamp}.xlsx"'
    )
    return response


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_GET
def operator_stock_item_history(request):
    scope = _stock_item_identity(request)
    period = _stock_item_period(request)
    balances, totals, sample, barcodes = _stock_item_profile(scope)

    agency = sample.agency if sample is not None else None
    if agency is None:
        agency = Agency.objects.filter(pk=scope["agency_id"]).first()
        if agency is None:
            raise Http404("Клиент не найден")

    include_orders = str(request.GET.get("item_only") or "") != "1"
    show_empty = str(request.GET.get("show_empty") or "") == "1"
    errors_only = str(request.GET.get("errors") or "") == "1"
    order_query = str(request.GET.get("order") or "").strip()
    actor_query = str(request.GET.get("actor") or "").strip()
    kinds = [
        value
        for value in request.GET.getlist("kind")
        if value in dict(STOCK_ITEM_KIND_CHOICES)
    ]

    box_labels = _stock_item_box_labels(scope)
    rows, truncated, order_ids = _stock_item_timeline(
        scope,
        period=period,
        barcodes=barcodes,
        include_orders=include_orders,
        box_labels=box_labels,
    )
    filtered_rows = _stock_item_filter_rows(
        rows,
        kinds=kinds,
        item_only=not include_orders,
        errors_only=errors_only,
        order_query=order_query,
        actor_query=actor_query,
    )

    if str(request.GET.get("export") or "") == "xlsx":
        return _stock_item_export(
            filtered_rows,
            scope=scope,
            profile_name=sample.name if sample is not None else "",
        )

    placements = _stock_item_placements(balances, show_empty=show_empty)
    blockers = _stock_item_blockers(
        scope, barcodes=barcodes, order_ids=order_ids, box_labels=box_labels
    )

    marking_rows = []
    if not scope["marking"] and int(totals["marked"] or 0):
        marking_rows = list(
            balances.exclude(marking_code="")
            .select_related("box")
            .order_by("-qty", "marking_code")
            .values("marking_code", "qty", "box__box_code", "updated_at")[
                :STOCK_ITEM_MARKING_LIMIT
            ]
        )

    first_arrival = FbsStockMovement.objects.filter(
        _stock_item_scope_q("target_balance__", scope)
    ).aggregate(first=Min("occurred_at"))["first"]

    page_size = _clean_page_size(request.GET.get("page_size"))
    page = Paginator(filtered_rows, page_size).get_page(request.GET.get("page"))

    query_params = request.GET.copy()
    query_params.pop("page", None)
    export_params = query_params.copy()
    export_params["export"] = "xlsx"

    article = scope["sku_code"] or scope["barcode"]
    return render(
        request,
        "fbs/operator_stock_item.html",
        _base_context(
            request,
            section="stock_history",
            page_title=f"FBS · История товара {article}",
            workspace_crumb_article=article,
            back_url=reverse("fbs:operator_stock_history"),
            scope=scope,
            agency=agency,
            balance_sample=sample,
            barcodes=barcodes,
            totals=totals,
            first_arrival=first_arrival,
            placements=placements,
            blockers=blockers,
            marking_rows=marking_rows,
            marking_limit=STOCK_ITEM_MARKING_LIMIT,
            page=page,
            page_size=page_size,
            page_sizes=QUEUE_PAGE_SIZES,
            page_query=query_params.urlencode(),
            export_url=f"{reverse('fbs:operator_stock_item_history')}?{export_params.urlencode()}",
            event_total=len(filtered_rows),
            truncated=truncated,
            period=period,
            kind_choices=STOCK_ITEM_KIND_CHOICES,
            selected_kinds=kinds,
            item_only=not include_orders,
            show_empty=show_empty,
            errors_only=errors_only,
            order_query=order_query,
            actor_query=actor_query,
            window_days=STOCK_ITEM_WINDOW_DAYS,
            stale_reserve_hours=STOCK_ITEM_STALE_RESERVE_HOURS,
            reset_url=(
                f"{reverse('fbs:operator_stock_item_history')}?agency={scope['agency_id']}"
                f"&sku={scope['sku_code']}&barcode={scope['barcode']}"
            ),
        ),
    )


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_GET
def operator_readiness(request):
    from .controller_views import _controller_equipment_context

    equipment_rows = []
    workstations = (
        FbsWorkstation.objects.filter(is_active=True)
        .select_related("device_agent", "shift_controller")
        .order_by("name", "id")
    )
    for workstation in workstations:
        equipment = _controller_equipment_context(workstation)
        agent = equipment.get("agent") or {}
        printer = equipment.get("printer") or {}
        scanner = equipment.get("scanner") or {}
        planned = workstation.shift_status != FbsWorkstation.SHIFT_CLOSED
        equipment_rows.append(
            {
                "workstation": workstation,
                "agent": agent,
                "printer": printer,
                "scanner": scanner,
                "planned": planned,
                "agent_last_seen": (
                    workstation.device_agent.last_seen
                    if workstation.device_agent_id
                    else None
                ),
                "ready": bool(
                    planned
                    and agent.get("online")
                    and printer.get("severity") == "ok"
                    and scanner.get("ready") is True
                ),
            }
        )

    report = add_equipment_readiness(
        build_fbs_readiness_report(),
        equipment_rows,
    )
    return render(
        request,
        "fbs/operator_readiness.html",
        _base_context(
            request,
            section="readiness",
            page_title="FBS · Диагностика",
            back_url=reverse("fbs:tsd_storekeeper"),
            report=report,
        ),
    )


def _operator_tote_rows():
    carts = list(
        FbsPickingCart.objects.select_related(
            "owner_agency",
            "created_by",
            "updated_by",
            "binding__zone",
            "binding__workstation",
            "binding__employee",
            "binding__pick_batch__agency",
            "binding__pick_batch__assigned_to",
            "binding__pick_batch__assigned_to__employee_profile",
        ).order_by("name", "id")
    )
    latest_batches = {}
    if carts:
        for batch in (
            FbsPickBatch.objects.filter(cart_id__in=[cart.id for cart in carts])
            .select_related("agency", "assigned_to", "assigned_to__employee_profile")
            .order_by("cart_id", "-id")
        ):
            latest_batches.setdefault(batch.cart_id, batch)

    rows = []
    for cart in carts:
        try:
            binding = cart.binding
        except FbsToteBinding.DoesNotExist:
            binding = None
        state = binding.state if binding is not None else FbsToteBinding.STATE_UNBOUND
        current_batch = binding.pick_batch if binding is not None else None
        latest_batch = latest_batches.get(cart.id)
        display_batch = current_batch or latest_batch
        active_batch_obj = current_batch
        if (
            active_batch_obj is None
            and latest_batch is not None
            and latest_batch.status
            in (FbsPickBatch.STATUS_IN_PROGRESS, FbsPickBatch.STATUS_VERIFICATION)
            and latest_batch.cart_released_at is None
        ):
            active_batch_obj = latest_batch
        active_batch = bool(
            active_batch_obj is not None
            and active_batch_obj.status
            in (FbsPickBatch.STATUS_IN_PROGRESS, FbsPickBatch.STATUS_VERIFICATION)
            and active_batch_obj.cart_released_at is None
        )
        if binding is None:
            location = "Не привязана"
        elif binding.zone_id:
            location = binding.zone.name
        elif binding.workstation_id:
            location = binding.workstation.name
        elif binding.employee_id:
            location = _fbs_actor_label(binding.employee, fallback="Сотрудник")
        else:
            location = "Не привязана"
        is_free = state in {FbsToteBinding.STATE_UNBOUND, FbsToteBinding.STATE_FREE}
        if not cart.is_active:
            status_code = "disabled"
            status_label = "Отключена"
        elif active_batch:
            status_code = "work"
            status_label = "В сборке"
        elif not is_free:
            status_code = "occupied"
            status_label = dict(FbsToteBinding.STATE_CHOICES).get(state, state)
        else:
            status_code = "free"
            status_label = "Свободна"
        rows.append(
            SimpleNamespace(
                cart=cart,
                state=state,
                status_code=status_code,
                status_label=status_label,
                location=location,
                batch=display_batch,
                picker=(
                    _fbs_tote_picker_label(display_batch.assigned_to, fallback="—")
                    if display_batch is not None
                    else "—"
                ),
                creator=_fbs_actor_label(cart.created_by, fallback="Система"),
                can_disable=bool(cart.is_active and is_free and not active_batch),
            )
        )
    return rows


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_GET
def operator_totes(request):
    query = str(request.GET.get("q") or "").strip()
    selected_status = str(request.GET.get("status") or "").strip()
    if selected_status not in {"", "active", "free", "work", "disabled"}:
        selected_status = ""
    all_rows = _operator_tote_rows()
    rows = []
    for row in all_rows:
        if selected_status == "active" and not row.cart.is_active:
            continue
        if selected_status == "free" and row.status_code != "free":
            continue
        if selected_status == "work" and row.status_code not in {"work", "occupied"}:
            continue
        if selected_status == "disabled" and row.status_code != "disabled":
            continue
        if query:
            batch_id = row.batch.id if row.batch is not None else ""
            agency = row.batch.agency if row.batch is not None else row.cart.owner_agency
            haystack = " ".join(
                str(value or "")
                for value in (
                    row.cart.name,
                    row.cart.barcode,
                    row.cart.notes,
                    row.location,
                    row.picker,
                    row.creator,
                    batch_id,
                    agency,
                )
            ).lower()
            if query.lower() not in haystack:
                continue
        rows.append(row)

    page_size = _clean_page_size(request.GET.get("page_size"))
    page = Paginator(rows, page_size).get_page(request.GET.get("page"))
    return render(
        request,
        "fbs/operator_totes.html",
        _base_context(
            request,
            section="totes",
            page_title="FBS · Тара",
            back_url=reverse("fbs:tsd_storekeeper"),
            page=page,
            page_size=page_size,
            page_sizes=QUEUE_PAGE_SIZES,
            query=query,
            selected_status=selected_status,
            summary={
                "total": len(all_rows),
                "active": sum(1 for row in all_rows if row.cart.is_active),
                "free": sum(1 for row in all_rows if row.status_code == "free"),
                "work": sum(
                    1 for row in all_rows if row.status_code in {"work", "occupied"}
                ),
            },
        ),
    )


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_POST
def operator_tote_create(request):
    try:
        carts = create_fbs_picking_carts(
            actor=request.user,
            count=request.POST.get("count"),
            name_prefix=str(request.POST.get("name_prefix") or ""),
            notes=str(request.POST.get("notes") or ""),
        )
    except (FbsError, ValidationError, TypeError, ValueError) as exc:
        messages.error(request, str(exc))
        return redirect("fbs:operator_totes")
    messages.success(request, f"Создано тар FBS: {len(carts)}.")
    if request.POST.get("print_labels") == "1":
        query = urlencode({"ids": ",".join(str(cart.id) for cart in carts)})
        return redirect(f"{reverse('fbs:operator_tote_labels')}?{query}")
    return redirect("fbs:operator_totes")


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_POST
def operator_tote_update(request, cart_id: int):
    cart = get_object_or_404(FbsPickingCart, pk=cart_id)
    try:
        cart = update_fbs_picking_cart(
            actor=request.user,
            cart=cart,
            name=str(request.POST.get("name") or ""),
            notes=str(request.POST.get("notes") or ""),
            is_active=request.POST.get("is_active") == "1",
        )
    except (FbsError, ValidationError, TypeError, ValueError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Настройки тары «{cart.name}» сохранены.")
    return redirect("fbs:operator_totes")


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_GET
def operator_tote_labels(request):
    raw_ids = str(request.GET.get("ids") or "")
    try:
        ids = tuple(
            dict.fromkeys(int(value) for value in raw_ids.split(",") if value.strip())
        )
    except ValueError as exc:
        raise Http404("Некорректный список тары.") from exc
    if not ids or len(ids) > 100:
        raise Http404("Выберите от 1 до 100 этикеток тары.")
    labels = list(FbsPickingCart.objects.filter(pk__in=ids).order_by("id"))
    if len(labels) != len(ids):
        raise Http404("Часть выбранных тар не найдена.")
    return render(
        request,
        "fbs/operator_tote_labels.html",
        {"labels": labels},
    )


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_GET
def operator_clients(request):
    query = str(request.GET.get("q") or "").strip()
    agencies = Agency.objects.filter(fbs_integration_profiles__isnull=False)
    if query:
        agencies = agencies.filter(
            Q(agn_name__icontains=query)
            | Q(short_name__icontains=query)
            | Q(inn__icontains=query)
            | Q(fbs_integration_profiles__external_account_id__icontains=query)
            | Q(fbs_integration_profiles__external_warehouse_id__icontains=query)
        )
    agencies = list(
        agencies.distinct()
        .prefetch_related(
            Prefetch(
                "fbs_integration_profiles",
                queryset=_profiles_for_connection_status().order_by("marketplace", "name", "id"),
                to_attr="connection_profiles",
            )
        )
        .order_by("agn_name", "id")
    )
    order_counts = _client_order_counts([agency.id for agency in agencies])
    for agency in agencies:
        _decorate_client_connection(
            agency,
            agency.connection_profiles,
            order_counts.get(agency.id),
        )
    page_size = _clean_page_size(request.GET.get("page_size"))
    page = Paginator(agencies, page_size).get_page(request.GET.get("page"))
    return render(
        request,
        "fbs/operator_clients.html",
        _base_context(
            request,
            section="clients",
            page_title="FBS · Клиенты",
            back_url=reverse("fbs:tsd_storekeeper"),
            page=page,
            page_size=page_size,
            page_sizes=QUEUE_PAGE_SIZES,
            query=query,
            summary={
                "clients": len(agencies),
                "credentials": sum(1 for agency in agencies if agency.credentials_count),
                "connected": sum(
                    1
                    for agency in agencies
                    if agency.api_state in {"connected", "partial"}
                ),
                "errors": sum(1 for agency in agencies if agency.api_state == "error"),
            },
            global_order_pull_enabled=feature_enabled("order_pull"),
        ),
    )


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_POST
def operator_sync_all_orders(request):
    profiles = list(
        _profiles_for_connection_status()
        .filter(is_active=True, order_pull_enabled=True)
        .order_by("agency__agn_name", "marketplace", "id")
    )
    _publish_sync_result(request, _sync_profile_orders(profiles))
    return_to = str(request.POST.get("return_to") or "").strip()
    if return_to == "clients":
        return redirect("fbs:operator_clients")
    if return_to == "queue":
        return redirect("fbs:operator_queue")
    if return_to == "orders":
        return redirect("fbs:operator_orders")
    return redirect("fbs:tsd_storekeeper")


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_POST
def operator_sync_client_orders(request, agency_id: int):
    agency = get_object_or_404(
        Agency.objects.filter(fbs_integration_profiles__isnull=False).distinct(),
        pk=agency_id,
    )
    profiles = list(
        _profiles_for_connection_status()
        .filter(agency=agency, is_active=True, order_pull_enabled=True)
        .order_by("marketplace", "id")
    )
    _publish_sync_result(request, _sync_profile_orders(profiles))
    return redirect("fbs:operator_client_detail", agency_id=agency.id)


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_POST
def operator_sync_profile_orders(request, profile_id: int):
    profile = get_object_or_404(_profiles_for_connection_status(), pk=profile_id)
    _publish_sync_result(request, _sync_profile_orders([profile]))
    return redirect("fbs:operator_client_detail", agency_id=profile.agency_id)


@operator_module_required
@role_required(*ORDER_DETAIL_ROLES)
@require_GET
def operator_order_detail(request, order_id: int):
    order = get_object_or_404(
        FbsOrder.objects.select_related("profile__agency"),
        pk=order_id,
    )
    _decorate_order_status_labels((order,))
    order.track_number = _first_payload_text(
        order.raw_payload,
        ("tracking_number", "track_number", "trackingNumber", "trackNumber"),
    )
    items = list(
        FbsOrderItem.objects.select_related("sku")
        .prefetch_related("sku__photos")
        .filter(order=order)
        .order_by("id")
    )
    _decorate_order_item_photos(items, order_payload=order.raw_payload)
    allocations = list(
        FbsOrderStockAllocation.objects.filter(order_item__order=order)
        .select_related(
            "order_item__sku",
            "balance__box__pallet__cell",
            "pick_task__batch",
            "pick_task__batch__cart",
            "picked_by",
            "reserved_by",
            "released_by",
        )
        .order_by("order_item_id", "id")
    )
    tasks = list(
        FbsPickTask.objects.filter(order=order)
        .select_related(
            "batch",
            "assigned_to",
            "batch__assigned_to",
            "batch__created_by",
            "batch__verification_assigned_to",
            "batch__workstation",
            "batch__cart",
        )
        .order_by("-created_at", "-id")
    )
    labels = list(
        FbsOrderLabel.objects.filter(order=order)
        .select_related("requested_by", "applied_by")
        .order_by("-requested_at", "-id")
    )
    transfers = list(
        FbsMarketplaceMetadataTransfer.objects.filter(order_item__order=order)
        .select_related("order_item")
        .order_by("order_item_id", "metadata_type", "id")
    )
    commands = list(
        FbsMarketplaceCommand.objects.filter(order=order)
        .select_related("requested_by")
        .order_by("-created_at", "-id")
    )
    handover_assignment = (
        FbsHandoverOrderAssignment.objects.filter(order=order)
        .select_related(
            "assigned_by",
            "batch__created_by",
            "batch__dispatched_by",
            "batch__dispatch_location",
        )
        .first()
    )
    handover = (
        FbsHandoverOrder.objects.filter(order=order)
        .select_related(
            "added_by",
            "excluded_by",
            "verified_by",
            "verified_label",
            "box__scanned_by",
            "box__batch__created_by",
            "box__batch__dispatched_by",
            "box__batch__dispatch_location",
        )
        .prefetch_related("box__batch__boxes")
        .first()
    )
    exceptions = list(
        FbsPickException.objects.filter(task__order=order)
        .select_related("task__batch", "allocation", "created_by", "resolved_by")
        .order_by("-created_at", "-id")
    )
    scan_events = list(
        FbsPickScanEvent.objects.filter(
            Q(task__order=order) | Q(allocation__order_item__order=order)
        )
        .select_related("batch", "task", "allocation", "created_by")
        .distinct()
        .order_by("-created_at", "-id")
    )
    marketplace_events = list(
        FbsMarketplaceEvent.objects.filter(
            profile=order.profile,
            external_id=order.external_order_id,
        ).order_by("-received_at", "-id")
    )
    tote_orders = list(
        FbsControllerToteOrder.objects.filter(order=order)
        .select_related(
            "label",
            "label_confirmed_by",
            "composition_checked_by",
            "pick_tote__tote",
            "pick_tote__session__workstation",
            "check_tote__tote",
            "check_tote__session__workstation",
            "check_tote__opened_by",
            "check_tote__closed_by",
            "transport_box__batch",
        )
        .order_by("label_confirmed_at", "id")
    )
    pick_batch_ids = {task.batch_id for task in tasks}
    tote_ids = {
        task.batch.cart_id for task in tasks if task.batch.cart_id is not None
    }
    for tote_order in tote_orders:
        tote_ids.add(tote_order.pick_tote.tote_id)
        if tote_order.check_tote.tote_id:
            tote_ids.add(tote_order.check_tote.tote_id)
    tote_movements = list(
        FbsToteMovement.objects.filter(tote_id__in=tote_ids)
        .filter(
            Q(details__order_id=order.id)
            | Q(details__order_id=str(order.id))
            | Q(
                pick_batch_id__in=pick_batch_ids,
                action__in=(
                    FbsToteMovement.ACTION_ASSIGN,
                    FbsToteMovement.ACTION_TAKE,
                    FbsToteMovement.ACTION_HANDOVER,
                    FbsToteMovement.ACTION_RELEASE,
                    FbsToteMovement.ACTION_CLOSE,
                ),
            )
        )
        .select_related(
            "tote",
            "performed_by",
            "pick_batch",
            "handover_batch",
        )
        .distinct()
        .order_by("created_at", "id")
    )
    _decorate_tote_movement_endpoints(tote_movements)
    control_destination_by_order_id = {}
    for movement in tote_movements:
        details = movement.details if isinstance(movement.details, dict) else {}
        if str(details.get("order_id") or "") == str(order.id):
            control_destination_by_order_id[order.id] = movement.target_display
    for tote_order in tote_orders:
        tote_order.control_destination = (
            _fbs_tote_label(tote_order.check_tote.tote)
            if tote_order.check_tote.tote_id
            else control_destination_by_order_id.get(order.id, "")
        )
    restock_requests = list(
        FbsPickRestockRequest.objects.filter(order=order)
        .select_related("source_tote", "created_by", "assigned_to")
        .prefetch_related(
            "lines__allocation__balance__box__pallet__cell",
            "scans__created_by",
        )
        .order_by("created_at", "id")
    )
    latest_marketplace_event = marketplace_events[0] if marketplace_events else None
    route_rows = _order_route_rows(
        order,
        allocations=allocations,
        tasks=tasks,
        tote_orders=tote_orders,
        handover_assignment=handover_assignment,
        handover=handover,
        restock_requests=restock_requests,
        latest_marketplace_event=latest_marketplace_event,
    )
    request_role = get_request_role(request)
    if request_role == "fbs_controller":
        related_batch_id = (
            handover.box.batch_id
            if handover is not None
            else handover_assignment.batch_id if handover_assignment is not None else None
        )
        back_url = (
            reverse("fbs:tsd_handover_detail", kwargs={"batch_id": related_batch_id})
            if related_batch_id
            else reverse("fbs:controller_home")
        )
        section = "handover" if related_batch_id else "controller"
    else:
        back_url = reverse("fbs:operator_orders")
        section = "orders"
    return render(
        request,
        "fbs/operator_order_detail.html",
        _base_context(
            request,
            section=section,
            page_title=f"FBS · Заказ {order.external_order_id}",
            back_url=back_url,
            order=order,
            workflow_section="orders",
            items=items,
            allocations=allocations,
            tasks=tasks,
            labels=labels,
            transfers=transfers,
            commands=commands,
            handover=handover,
            handover_assignment=handover_assignment,
            handover_check=_order_handover_check(
                handover_assignment, handover,
                marketplace_terminal=FbsOrder.objects.filter(pk=order.pk)
                .filter(_presentation_completed_marketplace_order_q()).exists(),
            ),
            handover_summary=_order_handover_summary(handover),
            exceptions=exceptions,
            scan_events=scan_events,
            tote_orders=tote_orders,
            tote_movements=tote_movements,
            restock_requests=restock_requests,
            route_rows=route_rows,
            latest_marketplace_event=latest_marketplace_event,
            timeline=_order_timeline(
                order,
                tasks=tasks,
                allocations=allocations,
                labels=labels,
                transfers=transfers,
                commands=commands,
                handover=handover,
                exceptions=exceptions,
                marketplace_events=marketplace_events,
                tote_orders=tote_orders,
                tote_movements=tote_movements,
                restock_requests=restock_requests,
            ),
            unit_count=sum(int(item.quantity or 0) for item in items),
        ),
    )


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_GET
def operator_client_detail(request, agency_id: int):
    agency = get_object_or_404(
        Agency.objects.filter(fbs_integration_profiles__isnull=False).distinct(),
        pk=agency_id,
    )
    page_size = _clean_page_size(request.GET.get("page_size"))
    page = Paginator(
        _orders_queryset()
        .filter(profile__agency=agency)
        .exclude(internal_status=FbsOrder.STATUS_CANCELLED)
        .order_by(F("cutoff_at").asc(nulls_last=True), "imported_at", "id"),
        page_size,
    ).get_page(request.GET.get("page"))
    _decorate_orders(page.object_list)
    status_rows = (
        FbsOrder.objects.filter(profile__agency=agency)
        .exclude(internal_status=FbsOrder.STATUS_CANCELLED)
        .values("internal_status")
        .annotate(count=Count("id"))
        .order_by("internal_status")
    )
    status_labels = OPERATOR_INTERNAL_STATUS_LABELS
    stock = FbsStockBalance.objects.filter(agency=agency).aggregate(
        qty=Coalesce(Sum("qty"), 0),
        available=Coalesce(Sum("available_qty"), 0),
        reserved=Coalesce(Sum("reserved_qty"), 0),
        sku_count=Count("sku_ref", distinct=True),
        box_count=Count("box", distinct=True),
    )
    stock_overview = client_stock_overview(agency=agency)
    profiles = list(
        _profiles_for_connection_status()
        .filter(agency=agency)
        .order_by("marketplace", "name", "id")
    )
    order_counts = _client_order_counts([agency.id]).get(agency.id)
    _decorate_client_connection(agency, profiles, order_counts)
    return render(
        request,
        "fbs/operator_client_detail.html",
        _base_context(
            request,
            section="clients",
            page_title=f"FBS · {agency}",
            back_url=reverse("fbs:operator_clients"),
            agency=agency,
            profiles=profiles,
            page=page,
            page_size=page_size,
            page_sizes=QUEUE_PAGE_SIZES,
            status_rows=[
                {"label": status_labels.get(row["internal_status"], row["internal_status"]), **row}
                for row in status_rows
            ],
            stock=stock,
            stock_overview=stock_overview,
            active_waves=FbsPickBatch.objects.filter(
                agency=agency, status__in=ACTIVE_WAVE_STATUSES
            )
            .select_related("assigned_to")
            .annotate(order_count=Count("tasks"))
            .order_by("created_at", "id"),
            open_plans=FbsReplenishmentPlan.objects.filter(
                agency=agency,
                status__in=(
                    FbsReplenishmentPlan.STATUS_PROPOSED,
                    FbsReplenishmentPlan.STATUS_CONFIRMED,
                    FbsReplenishmentPlan.STATUS_IN_PROGRESS,
                ),
            ).order_by("created_at", "id"),
            movement_requests=FbsClientMovementRequest.objects.filter(agency=agency)
            .order_by("-created_at", "-id")[:10],
        ),
    )


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_POST
def operator_update_client_stock_safety(request, agency_id: int):
    agency = get_object_or_404(
        Agency.objects.filter(fbs_integration_profiles__isnull=False).distinct(),
        pk=agency_id,
    )
    try:
        quantity = update_client_safety_stock_qty(
            agency=agency,
            quantity=request.POST.get("safety_stock_qty", "0"),
        )
    except FbsError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"Страховой остаток сохранён: {quantity} шт. на каждый SKU.",
        )
    return redirect("fbs:operator_client_detail", agency_id=agency.id)


def _movement_detail_context(request, request_row, *, error="", ok_message="") -> dict:
    from .services.movement_acceptance_jobs import job_context
    acceptance_job = job_context(request_row.pk)
    lines = list(request_row.lines.select_related("sku").order_by("id"))
    can_accept_request = request_row.status in {
        FbsClientMovementRequest.STATUS_SUBMITTED,
        FbsClientMovementRequest.STATUS_APPROVED,
    }
    summaries = (
        {
            summary.line_id: summary
            for summary in movement_box_candidate_summaries(request_row)
        }
        if can_accept_request and not (acceptance_job and acceptance_job['pending'])
        else {}
    )
    item_fallback_lines = []
    for line in lines:
        line.box_candidate_summary = summaries.get(line.id)
        if line.box_candidate_summary:
            line.item_fallback_box_count = max(
                0,
                int(line.box_candidate_summary.required_count or 0)
                - int(line.box_candidate_summary.available_count or 0),
            )
            line.item_fallback_qty = (
                line.item_fallback_box_count * int(line.units_per_box or 0)
            )
            if line.item_fallback_box_count:
                item_fallback_lines.append(line)
    boxes = FbsBox.objects.filter(
        agency=request_row.agency,
        status__in=(FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE),
        pallet__status__in=(FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE),
        pallet__cell__is_active=True,
    ).select_related("pallet__cell").order_by("pallet__cell__cell_code", "box_code", "id")
    pallets = list(
        FbsPallet.objects.filter(
            agency=request_row.agency,
            status__in=(FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE),
            cell__is_active=True,
        )
        .select_related("cell")
        .annotate(
            active_box_count=Count(
                "boxes",
                filter=Q(boxes__status__in=(FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE)),
            )
        )
        .order_by("cell__cell_code", "pallet_code", "id")
    )
    for pallet in pallets:
        pallet.free_box_slots = max(
            0, int(pallet.max_boxes or 0) - int(pallet.active_box_count or 0)
        )
    plans = list(
        request_row.replenishment_plans.select_related(
            "target_cell__location",
            "target_pallet",
            "target_box__source_container__current_location",
            "staging_location",
            "assigned_to",
        ).order_by("id")
    )
    staged_by_plan = {
        row["line__plan_id"]: int(row["total"] or 0)
        for row in FbsReplenishmentAllocation.objects.filter(
            line__plan__client_movement_request=request_row
        )
        .values("line__plan_id")
        .annotate(total=Sum("qty_staged"))
    }
    physical_labels_by_plan = defaultdict(list)
    movement_allocations = (
        FbsReplenishmentAllocation.objects.filter(
            line__plan__client_movement_request=request_row,
            target_box__isnull=False,
        )
        .select_related(
            "line",
            "target_box__source_container__current_location",
            "target_box__pallet__cell__location",
        )
        .order_by("line__plan_id", "id")
    )
    for movement_allocation in movement_allocations:
        label = fbs_box_physical_location_label(movement_allocation.target_box)
        labels = physical_labels_by_plan[movement_allocation.line.plan_id]
        if label not in labels:
            labels.append(label)
    for plan in plans:
        plan.staged_qty = staged_by_plan.get(plan.id, 0)
        physical_labels = physical_labels_by_plan.get(plan.id, [])
        if physical_labels:
            plan.physical_location_label = ", ".join(physical_labels)
        elif plan.target_box_id:
            plan.physical_location_label = fbs_box_physical_location_label(
                plan.target_box
            )
        else:
            plan.physical_location_label = "Место не указано"
    prepared_boxes = list(
        FbsReplenishmentPreparedBox.objects.filter(
            plan__client_movement_request=request_row
        )
        .select_related("plan", "fbs_box__pallet__cell__location")
        .annotate(
            distinct_barcode_count=Count(
                "items__allocation__line__barcode",
                distinct=True,
            )
        )
        .order_by("plan_id", "sequence_no", "id")
    )
    prepared_by_plan = {}
    for prepared_box in prepared_boxes:
        prepared_by_plan.setdefault(prepared_box.plan_id, []).append(prepared_box)
    for plan in plans:
        plan.prepared_box_rows = prepared_by_plan.get(plan.id, [])
    moved_qty = sum(int(plan.moved_qty or 0) for plan in plans)
    staged_qty = sum(int(plan.staged_qty or 0) for plan in plans)
    prepared_scanned_qty = sum(
        int(box.scanned_qty or 0) for box in prepared_boxes
    )
    staging_location = next(
        (plan.staging_location for plan in plans if plan.staging_location_id),
        None,
    )
    return _base_context(
        request,
        section="movements",
        page_title=f"FBS · {request_row.number}",
        back_url=reverse("fbs:operator_movements"),
        movement_request=request_row,
        acceptance_job=acceptance_job,
        lines=lines,
        boxes=boxes,
        pallets=pallets,
        plans=plans,
        staging_container_code=(
            movement_staging_container_code(request_row)
            if request_row.mode == FbsClientMovementRequest.MODE_ITEM
            and plans
            and not prepared_boxes
            else ""
        ),
        staging_location=staging_location,
        moved_qty=moved_qty,
        staged_qty=sum(
            int(plan.staged_qty or 0)
            for plan in plans
            if plan.status == FbsReplenishmentPlan.STATUS_AWAITING_PACK
        ),
        prepared_boxes=prepared_boxes,
        prepared_scanned_qty=prepared_scanned_qty,
        prepared_placed_qty=sum(int(box.placed_qty or 0) for box in prepared_boxes),
        prepared_closed_count=sum(
            box.status
            in {
                FbsReplenishmentPreparedBox.STATUS_CLOSED,
                FbsReplenishmentPreparedBox.STATUS_PLACED,
            }
            for box in prepared_boxes
        ),
        prepared_placed_count=sum(
            box.status == FbsReplenishmentPreparedBox.STATUS_PLACED
            for box in prepared_boxes
        ),
        prepared_unused_count=sum(
            box.status == FbsReplenishmentPreparedBox.STATUS_UNUSED
            for box in prepared_boxes
        ),
        prepared_mixed_count=sum(
            box.status != FbsReplenishmentPreparedBox.STATUS_UNUSED
            and int(box.distinct_barcode_count or 0) > 1
            for box in prepared_boxes
        ),
        item_fallback_lines=item_fallback_lines,
        can_accept_with_item_fallback=bool(item_fallback_lines)
        and not request_row.uses_hard_reserve
        and request_row.status
        in {
            FbsClientMovementRequest.STATUS_SUBMITTED,
            FbsClientMovementRequest.STATUS_APPROVED,
        },
        can_accept=(
            request_row.status == FbsClientMovementRequest.STATUS_APPROVED
            if request_row.uses_hard_reserve
            else request_row.status
            in {
                FbsClientMovementRequest.STATUS_SUBMITTED,
                FbsClientMovementRequest.STATUS_APPROVED,
            }
        ),
        uses_hard_reserve=request_row.uses_hard_reserve,
        can_confirm=request_row.status
        in {
            FbsClientMovementRequest.STATUS_MOVED,
            FbsClientMovementRequest.STATUS_NEEDS_CLARIFICATION,
            FbsClientMovementRequest.STATUS_AWAITING_MANAGER_CONFIRMATION,
        },
        can_pack=not prepared_boxes and any(
            plan.status == FbsReplenishmentPlan.STATUS_AWAITING_PACK for plan in plans
        ),
        can_cancel=(
            request_row.status
            in {
                FbsClientMovementRequest.STATUS_SUBMITTED,
                FbsClientMovementRequest.STATUS_APPROVED,
                FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED,
                FbsClientMovementRequest.STATUS_IN_PROGRESS,
            }
            and moved_qty == 0
            and staged_qty == 0
            and prepared_scanned_qty == 0
            and not any(
                plan.status == FbsReplenishmentPlan.STATUS_DONE for plan in plans
            )
        ),
        error=error,
        ok_message=ok_message,
    )


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_GET
def operator_movements(request):
    page_size = _clean_page_size(request.GET.get("page_size"))
    query = str(request.GET.get("q") or "").strip()
    status = str(request.GET.get("status") or "").strip()
    mode = str(request.GET.get("mode") or "").strip()
    waiting_warehouse_statuses = (
        FbsClientMovementRequest.STATUS_SUBMITTED,
        FbsClientMovementRequest.STATUS_APPROVED,
    )
    active_statuses = (
        FbsClientMovementRequest.STATUS_SUBMITTED,
        FbsClientMovementRequest.STATUS_APPROVED,
        FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED,
        FbsClientMovementRequest.STATUS_IN_PROGRESS,
        FbsClientMovementRequest.STATUS_MOVED,
        FbsClientMovementRequest.STATUS_NEEDS_CLARIFICATION,
    )
    status_groups = {
        "waiting_warehouse": waiting_warehouse_statuses,
        "active": active_statuses,
    }
    rows = FbsClientMovementRequest.objects.select_related(
        "agency", "requested_by", "reviewed_by"
    ).annotate(
        line_count=Count("lines", distinct=True),
        plan_count=Count("replenishment_plans", distinct=True),
        moved_qty=Coalesce(Sum("replenishment_plans__moved_qty"), 0),
    )
    summary_rows = FbsClientMovementRequest.objects.all()
    if query:
        query_filter = (
            Q(agency__agn_name__icontains=query)
            | Q(agency__short_name__icontains=query)
            | Q(agency__inn__icontains=query)
        )
        rows = rows.filter(query_filter)
        summary_rows = summary_rows.filter(query_filter)
    valid_statuses = {value for value, _ in FbsClientMovementRequest.STATUS_CHOICES}
    valid_modes = {value for value, _ in FbsClientMovementRequest.MODE_CHOICES}
    if status in status_groups:
        rows = rows.filter(status__in=status_groups[status])
    elif status in valid_statuses:
        rows = rows.filter(status=status)
    else:
        status = ""
    if mode in valid_modes:
        rows = rows.filter(mode=mode)
        summary_rows = summary_rows.filter(mode=mode)
    else:
        mode = ""
    page = Paginator(rows.order_by("-created_at", "-id"), page_size).get_page(
        request.GET.get("page")
    )
    summary = summary_rows.aggregate(
        total=Count("id"),
        waiting_warehouse=Count(
            "id",
            filter=Q(status__in=waiting_warehouse_statuses),
        ),
        active=Count(
            "id",
            filter=Q(status__in=active_statuses),
        ),
        completed=Count(
            "id", filter=Q(status=FbsClientMovementRequest.STATUS_COMPLETED)
        ),
    )
    return render(
        request,
        "fbs/operator_movements.html",
        _base_context(
            request,
            section="movements",
            page_title="FBS · Заявки на перемещение",
            back_url=reverse("fbs:tsd_storekeeper"),
            page=page,
            page_size=page_size,
            page_sizes=QUEUE_PAGE_SIZES,
            query=query,
            selected_status=status,
            selected_mode=mode,
            status_choices=(
                ("waiting_warehouse", "Ждут склада"),
                ("active", "В перемещении"),
            )
            + tuple(FbsClientMovementRequest.STATUS_CHOICES),
            mode_choices=FbsClientMovementRequest.MODE_CHOICES,
            summary=summary,
        ),
    )


def _receiving_movement_context(
    request,
    *,
    selected_agency_id=0,
    selected_order_id="",
    error="",
    created_request=None,
):
    options = receiving_pallet_movement_options()
    agencies_by_id = {}
    orders_by_key = {}
    for option in options:
        agencies_by_id.setdefault(option.agency_id, option.agency_name)
        if not selected_agency_id or option.agency_id == selected_agency_id:
            orders_by_key.setdefault(
                option.order_id,
                {
                    "order_id": option.order_id,
                    "agency_id": option.agency_id,
                    "pallet_count": 0,
                    "box_count": 0,
                    "qty": 0,
                },
            )
            orders_by_key[option.order_id]["pallet_count"] += 1
            orders_by_key[option.order_id]["box_count"] += option.box_count
            orders_by_key[option.order_id]["qty"] += option.qty
    agencies = [
        {"id": agency_id, "name": name}
        for agency_id, name in sorted(
            agencies_by_id.items(),
            key=lambda item: (item[1].casefold(), item[0]),
        )
    ]
    orders = sorted(
        orders_by_key.values(),
        key=lambda row: row["order_id"].casefold(),
        reverse=True,
    )
    pallets = [
        option
        for option in options
        if selected_agency_id
        and option.agency_id == selected_agency_id
        and selected_order_id
        and option.order_id == selected_order_id
    ]
    return _base_context(
        request,
        section="movements",
        page_title="FBS · Перемещение из приемки",
        back_url=reverse("fbs:operator_movements"),
        agencies=agencies,
        receiving_orders=orders,
        receiving_pallets=pallets,
        selected_agency_id=selected_agency_id,
        selected_order_id=selected_order_id,
        error=error,
        created_request=created_request,
    )


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_GET
def operator_movement_from_receiving(request):
    try:
        agency_id = int(request.GET.get("agency_id") or 0)
    except (TypeError, ValueError):
        agency_id = 0
    order_id = str(request.GET.get("order_id") or "").strip()
    return render(
        request,
        "fbs/operator_movement_from_receiving.html",
        _receiving_movement_context(
            request,
            selected_agency_id=agency_id,
            selected_order_id=order_id,
        ),
    )


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_POST
def operator_create_movement_from_receiving(request):
    try:
        agency_id = int(request.POST.get("agency_id") or 0)
    except (TypeError, ValueError):
        agency_id = 0
    order_id = str(request.POST.get("order_id") or "").strip()
    pallet_codes = request.POST.getlist("pallet_code")
    created_request = None
    try:
        created_request = create_receiving_fbs_movement_request(
            agency_id=agency_id,
            order_id=order_id,
            pallet_codes=pallet_codes,
            created_by=request.user,
        )
        if (
            created_request.mode == FbsClientMovementRequest.MODE_BOX
            and (
                created_request.requested_box_count >= 20
                or created_request.requested_qty >= 500
            )
        ):
            from .services.movement_acceptance_jobs import enqueue_acceptance

            enqueue_acceptance(request_row=created_request, actor=request.user)
            url = reverse(
                "fbs:operator_movement_detail",
                kwargs={"request_id": created_request.id},
            )
            return redirect(f"{url}?acceptance_job=1&from_receiving=1")
        accept_client_movement_request(
            request_id=created_request.id,
            accepted_by=request.user,
        )
    except (FbsError, Agency.DoesNotExist, TypeError, ValueError) as exc:
        return render(
            request,
            "fbs/operator_movement_from_receiving.html",
            _receiving_movement_context(
                request,
                selected_agency_id=agency_id,
                selected_order_id=order_id,
                error=str(exc),
                created_request=created_request,
            ),
            status=409,
        )
    url = reverse(
        "fbs:operator_movement_detail",
        kwargs={"request_id": created_request.id},
    )
    return redirect(f"{url}?accepted=1&from_receiving=1")


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_GET
def operator_movement_detail(request, request_id: int):
    request_row = get_object_or_404(
        FbsClientMovementRequest.objects.select_related(
            "agency", "requested_by", "reviewed_by"
        ),
        pk=request_id,
    )
    return render(
        request,
        "fbs/operator_movement_detail.html",
        _movement_detail_context(
            request,
            request_row,
            ok_message=(
                "Паллета принята в FBS на текущем месте. Для перестановки используйте свободное перемещение FBS."
                if request.GET.get("accepted") == "1"
                and request_row.status == FbsClientMovementRequest.STATUS_COMPLETED
                and str(request_row.source_file_name or "").strip().casefold().startswith(
                    RECEIVING_SOURCE_FILE_PREFIX
                )
                else "Заявка принята складом и передана ричтраку."
                if request.GET.get("accepted") == "1"
                else "Заявка отменена. Резерв снят, товар снова доступен клиенту."
                if request.GET.get("canceled") == "1"
                else "Заявка выполнена. Биллинг и статус клиента обновлены."
                if request.GET.get("confirmed") == "1"
                else "QR FBS-короба создан. Распечатайте его и передайте короб водителю для размещения."
                if request.GET.get("packed") == "1"
                else ""
            ),
        ),
    )


def _movement_label_agency_form(agency) -> str:
    agency_name = str(agency or "").strip()
    normalized_name = agency_name.casefold()
    if re.search(r"(^|\W)ип($|\W)", agency_name, flags=re.IGNORECASE) or (
        "индивидуальн" in normalized_name and "предпринимател" in normalized_name
    ):
        return "ИП"
    if re.search(r"(^|\W)ооо($|\W)", agency_name, flags=re.IGNORECASE) or (
        "общество с ограниченной ответственностью" in normalized_name
    ):
        return "ООО"
    return agency_name


def _movement_final_box_label(request_row, *, box_code: str, sequence_no: int) -> dict:
    return {
        "kind": "Короб FBS",
        "title": f"Короб №{sequence_no}",
        "subtitle": (
            f"{_movement_label_agency_form(request_row.agency)} · "
            f"{request_row.number}"
        ),
        "code": box_code,
    }


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_GET
def operator_movement_labels(request, request_id: int):
    request_row = get_object_or_404(
        FbsClientMovementRequest.objects.select_related("agency"),
        pk=request_id,
    )
    plan = (
        request_row.replenishment_plans.select_related("staging_location", "target_box")
        .filter(staging_location__isnull=False)
        .order_by("id")
        .first()
    )
    if plan is None:
        raise Http404
    kind = str(request.GET.get("kind") or "container").strip().lower()
    if kind == "container":
        labels = [
            {
                "kind": "Временный короб",
                "title": request_row.number,
                "subtitle": str(request_row.agency),
                "code": movement_staging_container_code(request_row),
            }
        ]
    elif kind == "zone":
        labels = [
            {
                "kind": "Зона подготовки FBS",
                "title": plan.staging_location.display_name or "Зона подготовки FBS",
                "subtitle": "Постоянный QR места передачи кладовщику",
                "code": (
                    plan.staging_location.location_code
                    or plan.staging_location.zone_code
                ),
            }
        ]
    elif kind == "final_box":
        prepared_boxes = list(
            FbsReplenishmentPreparedBox.objects.filter(
                plan__client_movement_request=request_row
            )
            .exclude(status=FbsReplenishmentPreparedBox.STATUS_UNUSED)
            .order_by("plan_id", "sequence_no", "id")
        )
        if prepared_boxes:
            labels = [
                _movement_final_box_label(
                    request_row,
                    box_code=prepared_box.box_code,
                    sequence_no=prepared_box.sequence_no,
                )
                for prepared_box in prepared_boxes
            ]
        elif plan.target_box_id:
            labels = [
                _movement_final_box_label(
                    request_row,
                    box_code=plan.target_box.box_code,
                    sequence_no=1,
                )
            ]
        else:
            raise Http404
    else:
        raise Http404
    return render(
        request,
        "fbs/movement_labels.html",
        {
            "movement_request": request_row,
            "labels": labels,
            "back_url": reverse(
                "fbs:operator_movement_detail",
                kwargs={"request_id": request_row.id},
            ),
        },
    )


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_POST
def operator_approve_movement(request, request_id: int):
    request_row = get_object_or_404(
        FbsClientMovementRequest.objects.select_related("agency"), pk=request_id
    )
    try:
        # Few large boxes can contain hundreds of stock rows too; accepting
        # them must not depend on the web worker's request timeout.
        if request_row.mode == FbsClientMovementRequest.MODE_BOX and (
            request_row.requested_box_count >= 20 or request_row.requested_qty >= 500
        ):
            from .services.movement_acceptance_jobs import enqueue_acceptance
            enqueue_acceptance(request_row=request_row, actor=request.user,
                allow_item_fallback=request.POST.get("allow_item_fallback", "") == "1")
            url = reverse("fbs:operator_movement_detail", kwargs={"request_id": request_row.id})
            return redirect(f"{url}?acceptance_job=1")
        accept_client_movement_request(
            request_id=request_row.id,
            accepted_by=request.user,
            prepared_box_count=(
                request.POST.get("prepared_box_count", "")
                if request_row.mode == FbsClientMovementRequest.MODE_ITEM
                else None
            ),
            allow_item_fallback=(
                request_row.mode == FbsClientMovementRequest.MODE_BOX
                and request.POST.get("allow_item_fallback", "") == "1"
            ),
        )
    except (FbsError, TypeError, ValueError) as exc:
        request_row.refresh_from_db()
        return render(
            request,
            "fbs/operator_movement_detail.html",
            _movement_detail_context(request, request_row, error=str(exc)),
            status=409,
        )
    url = reverse("fbs:operator_movement_detail", kwargs={"request_id": request_row.id})
    return redirect(f"{url}?accepted=1")


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_POST
def operator_cancel_movement(request, request_id: int):
    request_row = get_object_or_404(
        FbsClientMovementRequest.objects.select_related("agency"), pk=request_id
    )
    try:
        cancel_client_movement_by_warehouse(
            request_id=request_row.id,
            canceled_by=request.user,
            reason=request.POST.get("reason", ""),
        )
    except FbsError as exc:
        request_row.refresh_from_db()
        return render(
            request,
            "fbs/operator_movement_detail.html",
            _movement_detail_context(request, request_row, error=str(exc)),
            status=409,
        )
    url = reverse("fbs:operator_movement_detail", kwargs={"request_id": request_row.id})
    return redirect(f"{url}?canceled=1")


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_POST
def operator_confirm_movement(request, request_id: int):
    request_row = get_object_or_404(
        FbsClientMovementRequest.objects.select_related("agency"),
        pk=request_id,
    )
    try:
        confirm_client_movement_by_warehouse(
            request_id=request_id,
            confirmed_by=request.user,
            comment=request.POST.get("comment", ""),
        )
    except FbsError as exc:
        request_row.refresh_from_db()
        return render(
            request,
            "fbs/operator_movement_detail.html",
            _movement_detail_context(request, request_row, error=str(exc)),
            status=409,
        )
    url = reverse("fbs:operator_movement_detail", kwargs={"request_id": request_id})
    return redirect(f"{url}?confirmed=1")


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_POST
def operator_pack_movement(request, request_id: int):
    request_row = get_object_or_404(
        FbsClientMovementRequest.objects.select_related("agency"), pk=request_id
    )
    try:
        plan_id = int(request.POST.get("plan_id") or 0)
        if not request_row.replenishment_plans.filter(pk=plan_id).exists():
            raise ValueError("План не относится к этой заявке.")
        pack_staged_item_plan(
            plan_id=plan_id,
            packed_by=request.user,
            staging_container_scan=request.POST.get(
                "staging_container_scan", ""
            ),
        )
    except (FbsError, TypeError, ValueError) as exc:
        request_row.refresh_from_db()
        return render(
            request,
            "fbs/operator_movement_detail.html",
            _movement_detail_context(request, request_row, error=str(exc)),
            status=409,
        )
    url = reverse("fbs:operator_movement_detail", kwargs={"request_id": request_id})
    return redirect(f"{url}?packed=1")


def _wave_builder_context(params) -> dict:
    builder_agency_filter = str(
        params.get("builder_agency") or params.get("agency") or ""
    ).strip()
    builder_limit = _clean_queue_limit(
        params.get("builder_limit") or params.get("queue_limit")
    )
    builder_query = str(params.get("builder_q") or "").strip()
    queueable_orders = _orders_queryset().filter(
        internal_status__in=QUEUEABLE_ORDER_STATUSES,
        has_active_pick_task=False,
    )
    client_counts = {
        int(row["profile__agency_id"]): int(row["total"] or 0)
        for row in queueable_orders.values("profile__agency_id").annotate(
            total=Count("id")
        )
    }
    builder_agencies = list(
        Agency.objects.filter(fbs_integration_profiles__isnull=False)
        .distinct()
        .order_by("agn_name", "id")
    )
    for agency in builder_agencies:
        agency.queueable_order_count = client_counts.get(agency.id, 0)

    selected_agency = None
    builder_orders = []
    builder_candidate_count = 0
    if builder_agency_filter.isdigit():
        selected_agency = next(
            (
                agency
                for agency in builder_agencies
                if agency.id == int(builder_agency_filter)
            ),
            None,
        )
    if selected_agency is None:
        builder_agency_filter = ""
    else:
        candidates = queueable_orders.filter(
            profile__agency_id=selected_agency.id
        )
        if builder_query:
            candidates = candidates.filter(
                Q(external_order_id__icontains=builder_query)
                | Q(items__external_sku__icontains=builder_query)
                | Q(items__barcode__icontains=builder_query)
                | Q(items__product_name__icontains=builder_query)
            ).distinct()
        builder_candidate_count = candidates.count()
        builder_orders = list(order_by_pick_priority(candidates)[:500])
        _decorate_orders(builder_orders)
        builder_orders = [order for order in builder_orders if order.can_queue]
        builder_orders.sort(
            key=lambda order: (
                *pick_order_priority_key(order),
                AVAILABILITY_PRIORITY[order.availability_state],
            )
        )
        builder_orders = builder_orders[:builder_limit]

    return {
        "wave_builder_open": str(params.get("builder") or "") == "1"
        or bool(selected_agency),
        "builder_agencies": builder_agencies,
        "builder_selected_agency": selected_agency,
        "builder_agency_filter": builder_agency_filter,
        "builder_limit": builder_limit,
        "builder_query": builder_query,
        "builder_orders": builder_orders,
        "builder_candidate_count": builder_candidate_count,
        "builder_visible_count": len(builder_orders),
        "queue_limits": QUEUE_LIMITS,
    }


def _waiting_controller_totes(queryset=None):
    queryset = queryset if queryset is not None else FbsPickBatch.objects.all()
    return queryset.filter(
        status=FbsPickBatch.STATUS_VERIFICATION,
        picking_completed_at__isnull=False,
        cart_released_at__isnull=True,
        verification_assigned_to__isnull=True,
    ).exclude(pick_restock_request__status__in=ACTIVE_PICK_RESTOCK_STATUSES)


def _waves_context(request, *, error="", ok_message="", params=None) -> dict:
    params = params or request.GET
    status_filter = str(params.get("status") or "active").strip()
    agency_filter = str(params.get("agency") or "").strip()
    user_filter = str(params.get("user") or "").strip()
    wave_date_field = str(params.get("date_field") or "created").strip()
    wave_date_from = str(params.get("date_from") or "").strip()
    wave_date_to = str(params.get("date_to") or "").strip()
    waves = FbsPickBatch.objects.select_related(
        "agency",
        "assigned_to",
        "assigned_to__employee_profile",
        "workstation",
        "cart",
        "created_by",
        "created_by__employee_profile",
    ).annotate(
        order_count=Count("tasks", distinct=True),
        wb_order_count=Count(
            "tasks",
            filter=Q(
                tasks__order__profile__marketplace=FbsIntegrationProfile.MARKETPLACE_WB
            ),
            distinct=True,
        ),
        ozon_order_count=Count(
            "tasks",
            filter=Q(
                tasks__order__profile__marketplace=FbsIntegrationProfile.MARKETPLACE_OZON
            ),
            distinct=True,
        ),
        problem_count=Count(
            "tasks__exceptions",
            filter=Q(tasks__exceptions__status=FbsPickException.STATUS_OPEN),
            distinct=True,
        ),
    )
    if status_filter == "waiting_controller":
        waves = _waiting_controller_totes(waves)
    elif status_filter == "active":
        waves = waves.filter(status__in=ACTIVE_WAVE_STATUSES)
    elif status_filter in dict(FbsPickBatch.STATUS_CHOICES):
        waves = waves.filter(status=status_filter)
    else:
        status_filter = "all"
    if agency_filter.isdigit():
        waves = waves.filter(agency_id=int(agency_filter))
    else:
        agency_filter = ""
    if user_filter.isdigit():
        waves = waves.filter(created_by_id=int(user_filter))
    else:
        user_filter = ""
    wave_date_fields = {
        "created": "created_at__date",
        "started": "started_at__date",
        "completed": "completed_at__date",
    }
    if wave_date_field not in wave_date_fields:
        wave_date_field = "created"
    parsed_wave_from = parse_date(wave_date_from) if wave_date_from else None
    parsed_wave_to = parse_date(wave_date_to) if wave_date_to else None
    if wave_date_from and parsed_wave_from is None:
        wave_date_from = ""
    if wave_date_to and parsed_wave_to is None:
        wave_date_to = ""
    wave_date_lookup = wave_date_fields[wave_date_field]
    if parsed_wave_from:
        waves = waves.filter(**{f"{wave_date_lookup}__gte": parsed_wave_from})
    if parsed_wave_to:
        waves = waves.filter(**{f"{wave_date_lookup}__lte": parsed_wave_to})
    page_size = _clean_page_size(params.get("page_size"))
    if status_filter == FbsPickBatch.STATUS_QUEUED:
        waves = order_wave_queue_queryset(waves)
    else:
        waves = waves.order_by("-created_at", "-id")
    page = Paginator(waves, page_size).get_page(
        params.get("page")
    )
    if status_filter == FbsPickBatch.STATUS_QUEUED:
        decorate_wave_queue_rows(page.object_list)
    _decorate_wave_timing(page.object_list)
    builder_context = _wave_builder_context(params)
    return _base_context(
        request,
        section="waves",
        page_title="FBS · Волны",
        back_url=reverse("fbs:tsd_storekeeper"),
        page=page,
        page_size=page_size,
        page_sizes=QUEUE_PAGE_SIZES,
        status_filter=status_filter,
        agency_filter=agency_filter,
        user_filter=user_filter,
        wave_date_field=wave_date_field,
        wave_date_from=wave_date_from,
        wave_date_to=wave_date_to,
        filter_query=urlencode(
            {
                "status": status_filter,
                "agency": agency_filter,
                "user": user_filter,
                "date_field": wave_date_field,
                "date_from": wave_date_from,
                "date_to": wave_date_to,
                "page_size": page_size,
            }
        ),
        wave_status_choices=FbsPickBatch.STATUS_CHOICES,
        agencies=Agency.objects.filter(fbs_pick_batches__isnull=False)
        .distinct()
        .order_by("agn_name", "id"),
        wave_users=get_user_model().objects.filter(
            created_fbs_pick_batches__isnull=False
        )
        .select_related("employee_profile")
        .distinct()
        .order_by("first_name", "last_name", "username", "id"),
        workflow_section="waves",
        summary={
            "queued": FbsPickBatch.objects.filter(status=FbsPickBatch.STATUS_QUEUED).count(),
            "in_progress": FbsPickBatch.objects.filter(
                status=FbsPickBatch.STATUS_IN_PROGRESS
            ).count(),
            "verification": FbsPickBatch.objects.filter(
                status=FbsPickBatch.STATUS_VERIFICATION
            ).count(),
            "waiting_controller": _waiting_controller_totes().count(),
            "problems": FbsPickException.objects.filter(
                status=FbsPickException.STATUS_OPEN
            ).count(),
        },
        error=error,
        ok_message=ok_message,
        **builder_context,
    )


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_GET
def operator_waves(request):
    ok_message = ""
    if request.GET.get("prepared") == "1":
        ok_message = (
            f"Подготовлено волн: {request.GET.get('batches', '0')}; "
            f"добавлено заданий: {request.GET.get('tasks', '0')}."
        )
    return render(
        request,
        "fbs/operator_waves.html",
        _waves_context(request, ok_message=ok_message),
    )


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_POST
def operator_reorder_wave(request, batch_id: int):
    try:
        reorder_queued_wave(
            batch_id=batch_id,
            action=request.POST.get("direction", ""),
            actor=request.user,
        )
    except FbsError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Положение волны #{batch_id} сохранено на сегодня.")
    return redirect(f"{reverse('fbs:operator_waves')}?status=queued")


def _wave_report_user_label(user) -> str:
    if user is None:
        return "—"
    return str(user.get_full_name() or user.get_username()).strip() or "—"


def _wave_report_date(value: str):
    cleaned = str(value or "").strip()
    return (cleaned, parse_date(cleaned)) if cleaned else ("", None)


def _decorate_wave_report_rows(rows) -> None:
    for allocation in rows:
        task = allocation.pick_task
        batch = task.batch
        order = allocation.order_item.order
        picker = allocation.picked_by or task.assigned_to or batch.assigned_to
        allocation.report_picker = _wave_report_user_label(picker)
        allocation.report_picked_at = (
            allocation.picked_at
            or task.completed_at
            or batch.picking_completed_at
            or allocation.updated_at
        )
        allocation.report_source = _stock_history_location(
            fbs_box_physical_location_label(allocation.balance.box),
            allocation.balance.box.pallet.pallet_code,
            allocation.balance.box.box_code,
        )
        allocation.report_article = (
            allocation.order_item.external_sku
            or allocation.balance.sku_code
            or "—"
        )
        allocation.report_product_name = (
            allocation.order_item.product_name
            or allocation.balance.name
            or "—"
        )
        allocation.report_barcode = (
            allocation.order_item.barcode
            or allocation.balance.barcode
            or "—"
        )

        controller_link = getattr(batch, "controller_pick_tote", None)
        if controller_link is not None:
            controller = controller_link.session.controller
            check_tote = controller_link.check_tote.tote
            allocation.report_transfer_destination = (
                f"Контролёр · {_wave_report_user_label(controller)}"
            )
            allocation.report_transfer_detail = _stock_history_location(
                "Тара проверки",
                check_tote.name if check_tote is not None else "без номера",
                f"из {batch.cart.name}" if batch.cart is not None else "",
            )
            allocation.report_transfer_at = controller_link.attached_at
            allocation.report_transfer_by = _wave_report_user_label(controller)
        elif batch.picking_completed_at:
            allocation.report_transfer_destination = "Передано на проверку"
            allocation.report_transfer_detail = _stock_history_location(
                batch.cart.name if batch.cart is not None else "Тара не указана"
            )
            allocation.report_transfer_at = batch.picking_completed_at
            allocation.report_transfer_by = _wave_report_user_label(
                batch.verification_assigned_to
            )
        else:
            allocation.report_transfer_destination = "На сборке"
            allocation.report_transfer_detail = _stock_history_location(
                batch.cart.name if batch.cart is not None else "Тара не указана"
            )
            allocation.report_transfer_at = None
            allocation.report_transfer_by = "—"

        handover = getattr(order, "handover_order", None)
        if handover is not None:
            handover_batch = handover.box.batch
            marketplace = handover_batch.profile.get_marketplace_display()
            supply = (
                handover_batch.external_supply_id
                or handover_batch.external_name
                or f"#{handover_batch.id}"
            )
            allocation.report_final_destination = (
                f"{marketplace} · поставка {supply}"
            )
            allocation.report_final_detail = f"Короб {handover.box.qr_code}"
            if handover_batch.dispatched_at:
                allocation.report_final_status = "Передано водителю"
                allocation.report_final_at = handover_batch.dispatched_at
                allocation.report_final_by = _wave_report_user_label(
                    handover_batch.dispatched_by
                )
            elif handover.verified_at:
                allocation.report_final_status = "Проверено в отгрузке"
                allocation.report_final_at = handover.verified_at
                allocation.report_final_by = _wave_report_user_label(
                    handover.verified_by
                )
            else:
                allocation.report_final_status = handover.get_status_display()
                allocation.report_final_at = handover.added_at
                allocation.report_final_by = _wave_report_user_label(
                    handover.added_by
                )
        else:
            allocation.report_final_destination = (
                f"{order.profile.get_marketplace_display()} · заказ "
                f"{order.external_order_id}"
            )
            allocation.report_final_detail = "Отгрузка ещё не сформирована"
            allocation.report_final_status = order.get_internal_status_display()
            allocation.report_final_at = None
            allocation.report_final_by = "—"


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_GET
def operator_wave_report(request):
    query = str(request.GET.get("q") or "").strip()
    agency_filter = str(request.GET.get("agency") or "").strip()
    picker_filter = str(request.GET.get("picker") or "").strip()
    status_filter = str(request.GET.get("status") or "").strip()
    if not agency_filter.isdigit():
        agency_filter = ""
    if not picker_filter.isdigit():
        picker_filter = ""
    if status_filter not in dict(FbsPickBatch.STATUS_CHOICES):
        status_filter = ""

    today = timezone.localdate()
    default_from = today - timedelta(days=30)
    date_from_value = (
        request.GET.get("date_from")
        if "date_from" in request.GET
        else default_from.isoformat()
    )
    date_to_value = (
        request.GET.get("date_to")
        if "date_to" in request.GET
        else today.isoformat()
    )
    date_from, parsed_from = _wave_report_date(date_from_value)
    date_to, parsed_to = _wave_report_date(date_to_value)
    if date_from and parsed_from is None:
        date_from = ""
    if date_to and parsed_to is None:
        date_to = ""

    allocations = (
        FbsOrderStockAllocation.objects.filter(
            qty_picked__gt=0,
            pick_task__isnull=False,
        )
        .annotate(
            report_at=Coalesce(
                "picked_at",
                "pick_task__completed_at",
                "pick_task__batch__picking_completed_at",
                "updated_at",
            )
        )
        .select_related(
            "picked_by",
            "pick_task__assigned_to",
            "pick_task__batch__agency",
            "pick_task__batch__assigned_to",
            "pick_task__batch__verification_assigned_to",
            "pick_task__batch__cart",
            "pick_task__batch__controller_pick_tote__session__controller",
            "pick_task__batch__controller_pick_tote__check_tote__tote",
            "order_item__order__profile__agency",
            "order_item__order__handover_order__added_by",
            "order_item__order__handover_order__verified_by",
            "order_item__order__handover_order__box__batch__profile",
            "order_item__order__handover_order__box__batch__dispatched_by",
            "balance",
            "balance__box__source_container__current_location",
        )
    )
    if agency_filter:
        allocations = allocations.filter(
            pick_task__batch__agency_id=int(agency_filter)
        )
    if picker_filter:
        picker_id = int(picker_filter)
        allocations = allocations.filter(
            Q(picked_by_id=picker_id)
            | Q(picked_by__isnull=True, pick_task__assigned_to_id=picker_id)
            | Q(
                picked_by__isnull=True,
                pick_task__assigned_to__isnull=True,
                pick_task__batch__assigned_to_id=picker_id,
            )
        )
    if status_filter:
        allocations = allocations.filter(pick_task__batch__status=status_filter)
    if query:
        query_filter = (
            Q(order_item__external_sku__icontains=query)
            | Q(order_item__barcode__icontains=query)
            | Q(order_item__product_name__icontains=query)
            | Q(balance__sku_code__icontains=query)
            | Q(balance__barcode__icontains=query)
            | Q(balance__name__icontains=query)
            | Q(order_item__order__external_order_id__icontains=query)
            | Q(
                order_item__order__handover_order__box__batch__external_supply_id__icontains=query
            )
            | Q(order_item__order__handover_order__box__qr_code__icontains=query)
        )
        wave_number = query.lstrip("#")
        if wave_number.isdigit():
            query_filter |= Q(pick_task__batch_id=int(wave_number))
        allocations = allocations.filter(query_filter)
    if parsed_from:
        allocations = allocations.filter(report_at__date__gte=parsed_from)
    if parsed_to:
        allocations = allocations.filter(report_at__date__lte=parsed_to)

    summary = allocations.aggregate(
        qty=Coalesce(Sum("qty_picked"), 0),
        waves=Count("pick_task__batch_id", distinct=True),
        orders=Count("order_item__order_id", distinct=True),
    )
    summary["handed_qty"] = int(
        allocations.filter(
            order_item__order__handover_order__status__in=(
                FbsHandoverOrder.STATUS_ACTIVE,
                FbsHandoverOrder.STATUS_RETURN_PENDING,
            ),
            order_item__order__handover_order__box__batch__dispatched_at__isnull=False,
        ).aggregate(qty=Coalesce(Sum("qty_picked"), 0))["qty"]
        or 0
    )
    picker_ids = set(
        allocations.exclude(picked_by_id__isnull=True).values_list(
            "picked_by_id", flat=True
        )
    )
    picker_ids.update(
        allocations.filter(
            picked_by_id__isnull=True,
            pick_task__assigned_to_id__isnull=False,
        ).values_list("pick_task__assigned_to_id", flat=True)
    )
    picker_ids.update(
        allocations.filter(
            picked_by_id__isnull=True,
            pick_task__assigned_to_id__isnull=True,
            pick_task__batch__assigned_to_id__isnull=False,
        ).values_list("pick_task__batch__assigned_to_id", flat=True)
    )
    summary["pickers"] = len(picker_ids)

    page_size = _clean_page_size(request.GET.get("page_size"))
    page = Paginator(
        allocations.order_by("-report_at", "-id"), page_size
    ).get_page(request.GET.get("page"))
    page.object_list = list(page.object_list)
    _decorate_wave_report_rows(page.object_list)

    picker_users = (
        get_user_model()
        .objects.filter(
            Q(picked_fbs_order_allocations__qty_picked__gt=0)
            | Q(assigned_fbs_pick_tasks__allocations__qty_picked__gt=0)
            | Q(assigned_fbs_pick_batches__tasks__allocations__qty_picked__gt=0)
        )
        .distinct()
        .order_by("first_name", "last_name", "username", "id")
    )
    return render(
        request,
        "fbs/operator_wave_report.html",
        _base_context(
            request,
            section="waves",
            page_title="FBS · Отчёт по волнам",
            back_url=reverse("fbs:operator_waves"),
            workflow_section="waves",
            page=page,
            page_size=page_size,
            page_sizes=QUEUE_PAGE_SIZES,
            agencies=Agency.objects.filter(
                fbs_pick_batches__tasks__allocations__qty_picked__gt=0
            )
            .distinct()
            .order_by("agn_name", "id"),
            picker_users=picker_users,
            wave_status_choices=FbsPickBatch.STATUS_CHOICES,
            query=query,
            agency_filter=agency_filter,
            picker_filter=picker_filter,
            status_filter=status_filter,
            date_from=date_from,
            date_to=date_to,
            filter_query=urlencode(
                {
                    "q": query,
                    "agency": agency_filter,
                    "picker": picker_filter,
                    "status": status_filter,
                    "date_from": date_from,
                    "date_to": date_to,
                    "page_size": page_size,
                }
            ),
            summary={
                "qty": int(summary["qty"] or 0),
                "waves": int(summary["waves"] or 0),
                "orders": int(summary["orders"] or 0),
                "pickers": int(summary["pickers"] or 0),
                "handed_qty": int(summary["handed_qty"] or 0),
            },
        ),
    )


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_GET
def operator_wave_detail(request, batch_id: int):
    batch = get_object_or_404(
        FbsPickBatch.objects.select_related(
            "agency", "assigned_to", "workstation", "cart", "created_by", "verification_assigned_to"
        ),
        pk=batch_id,
    )
    tasks = list(
        FbsPickTask.objects.filter(batch=batch)
        .select_related("order__profile__agency", "assigned_to")
        .prefetch_related(
            "exceptions",
            "allocations__order_item__sku",
            "allocations__balance__box__pallet__cell",
            "allocations__verification_progress",
        )
        .order_by("sort_order", "id")
    )
    for task in tasks:
        task.verified_qty = sum(
            getattr(getattr(allocation, "verification_progress", None), "qty_verified", 0)
            for allocation in task.allocations.all()
        )
    batch.verified_qty = sum(task.verified_qty for task in tasks)
    scan_events = list(
        FbsPickScanEvent.objects.filter(batch=batch)
        .select_related("task__order", "created_by")
        .order_by("-created_at", "-id")
    )
    _decorate_wave_timing((batch,))
    return render(
        request,
        "fbs/operator_wave_detail.html",
        _base_context(
            request,
            section="waves",
            page_title=f"FBS · Волна #{batch.id}",
            back_url=reverse("fbs:operator_waves"),
            batch=batch,
            tasks=tasks,
            scan_events=scan_events,
            workflow_section="waves",
            open_exception_count=FbsPickException.objects.filter(
                task__batch=batch,
                status=FbsPickException.STATUS_OPEN,
            ).count(),
        ),
    )


def _render_prepare_wave_error(request, message: str):
    if str(request.POST.get("return_to") or "").strip() == "waves":
        return render(
            request,
            "fbs/operator_waves.html",
            _waves_context(request, params=request.POST, error=message),
            status=409,
        )
    if str(request.POST.get("return_to") or "").strip() == "queue":
        return render(
            request,
            "fbs/operator_orders.html",
            _orders_context(
                request,
                params=request.POST,
                error=message,
                queue_only=True,
            ),
            status=409,
        )
    return render(
        request,
        "fbs/operator_orders.html",
        _orders_context(request, params=request.POST, error=message),
        status=409,
    )


def _render_wave_stock_confirmation(
    request,
    *,
    shortages,
    selected_order_ids: list[int],
    agency_id: int,
    queue_limit: int,
):
    context = _orders_context(
        request,
        params=request.POST,
        queue_only=True,
    )
    shortage_order_ids = {shortage.order_id for shortage in shortages}
    filter_query = context.get("filter_query", "")
    cancel_url = reverse("fbs:operator_queue")
    if filter_query:
        cancel_url = f"{cancel_url}?{filter_query}"
    context["wave_stock_confirmation"] = {
        "shortages": shortages,
        "preview": shortages[:8],
        "remaining_count": max(len(shortages) - 8, 0),
        "missing_qty": sum(shortage.missing_qty for shortage in shortages),
        "selected_order_ids": selected_order_ids,
        "available_order_count": sum(
            order_id not in shortage_order_ids for order_id in selected_order_ids
        ),
        "agency_id": agency_id,
        "queue_limit": queue_limit,
        "cancel_url": cancel_url,
        "wave_action": request.POST.get("wave_action", ""),
        "target_batch_id": request.POST.get("target_batch_id", ""),
    }
    return render(
        request,
        "fbs/operator_orders.html",
        context,
        status=409,
    )


def _render_wave_launch_choice(
    request, *, selected_order_ids, agency_id, queue_limit, error="",
):
    context = _orders_context(request, params=request.POST, queue_only=True)
    cancel_url = reverse("fbs:operator_queue")
    if context.get("filter_query"):
        cancel_url += "?" + context["filter_query"]
    profile_count = FbsOrder.objects.filter(pk__in=selected_order_ids).values(
        "profile_id"
    ).distinct().count()
    context["wave_launch_choice"] = {
        "selected_order_ids": selected_order_ids,
        "order_count": len(selected_order_ids),
        "unit_count": FbsOrderItem.objects.filter(order_id__in=selected_order_ids).aggregate(
            total=Sum("quantity")
        )["total"] or 0,
        "destinations": pick_wave_destination_choices(order_ids=selected_order_ids),
        "multiple_profiles": profile_count > 1,
        "agency_id": agency_id, "queue_limit": queue_limit,
        "cancel_url": cancel_url, "error": error,
    }
    return render(request, "fbs/operator_orders.html", context, status=409 if error else 200)


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_POST
def operator_prepare_wave(request):
    wave_action = str(request.POST.get("wave_action") or "").strip()
    if wave_action not in {"", "new", "existing"}:
        return _render_prepare_wave_error(request, "Неизвестный способ запуска волны.")
    queue_limit = _clean_queue_limit(request.POST.get("queue_limit"))
    selected_values = request.POST.getlist("order_ids")
    submission_mode = str(request.POST.get("submit_mode") or "").strip()
    if not submission_mode:
        submission_mode = "selected" if selected_values else "filtered"
    if submission_mode not in {"selected", "filtered"}:
        return _render_prepare_wave_error(request, "Неизвестный режим подачи волны.")

    selected_order_ids = []
    if submission_mode == "selected":
        for value in selected_values:
            try:
                selected_order_ids.append(int(value))
            except (TypeError, ValueError):
                return _render_prepare_wave_error(
                    request,
                    "В выборе заказов обнаружен некорректный идентификатор.",
                )
        selected_order_ids = list(dict.fromkeys(selected_order_ids))
        if not selected_order_ids:
            return _render_prepare_wave_error(
                request,
                "Выберите хотя бы один заказ для создания волны.",
            )
        if len(selected_order_ids) > queue_limit:
            return _render_prepare_wave_error(
                request,
                f"Выбрано больше {queue_limit} заказов. Увеличьте количество в списке.",
            )

    agency_value = str(request.POST.get("agency") or "").strip()
    agency_id = int(agency_value) if agency_value.isdigit() else None
    if agency_id is not None and not Agency.objects.filter(
        pk=agency_id,
        fbs_integration_profiles__isnull=False,
    ).exists():
        return _render_prepare_wave_error(request, "Выбранный клиент FBS не найден.")
    if submission_mode == "filtered" and agency_id is None:
        return _render_prepare_wave_error(
            request,
            "Сначала выберите партнера в фильтре очереди.",
        )

    if selected_order_ids:
        selected_orders = list(
            FbsOrder.objects.filter(pk__in=selected_order_ids).values_list(
                "id", "profile__agency_id"
            )
        )
        if len(selected_orders) != len(selected_order_ids):
            return _render_prepare_wave_error(
                request, "Один из выбранных заказов не найден."
            )
        selected_agency_ids = {row_agency_id for _, row_agency_id in selected_orders}
        if len(selected_agency_ids) != 1:
            error = (
                "В одну подачу разрешены заказы только выбранного клиента."
                if agency_id is not None
                else "В одну подачу разрешены заказы только одного клиента."
            )
            return _render_prepare_wave_error(request, error)
        selected_agency_id = selected_agency_ids.pop()
        if agency_id is not None and agency_id != selected_agency_id:
            return _render_prepare_wave_error(
                request,
                "В одну подачу разрешены заказы только выбранного клиента.",
            )
        agency_id = selected_agency_id

    if submission_mode == "selected" and selected_order_ids:
        oldest_launchable = _oldest_launchable_orders(
            _orders_queryset().filter(
                profile__agency_id=agency_id,
                internal_status__in=QUEUEABLE_ORDER_STATUSES,
                has_active_pick_task=False,
            ),
            limit=len(selected_order_ids),
        )
        selected_order_id_set = set(selected_order_ids)
        skipped_oldest = [
            order
            for order in oldest_launchable
            if order.id not in selected_order_id_set
        ]
        if skipped_oldest:
            shown = ", ".join(
                order.external_order_id for order in skipped_oldest[:5]
            )
            suffix = (
                f" и ещё {len(skipped_oldest) - 5}"
                if len(skipped_oldest) > 5
                else ""
            )
            return _render_prepare_wave_error(
                request,
                "Сначала запустите более старые доступные заказы: "
                f"{shown}{suffix}. Новые заказы не могут обходить старые.",
            )

    if submission_mode == "filtered":
        filtered_orders, _ = _apply_order_filters(_orders_queryset(), request.POST)
        filtered_orders = _filter_orders_by_availability(
            filtered_orders,
            str(request.POST.get("availability") or "").strip(),
        )
        filtered_orders = filtered_orders.filter(
            internal_status__in=QUEUEABLE_ORDER_STATUSES,
            has_active_pick_task=False,
        )
        filtered_agency_ids = _filtered_wave_agency_ids(filtered_orders)
        if not filtered_agency_ids:
            return _render_prepare_wave_error(
                request, "Нет подходящих заказов для волны."
            )
        if agency_id is None:
            if len(filtered_agency_ids) > 1:
                return _render_prepare_wave_error(
                    request,
                    "По выбранному фильтру найдено несколько партнеров. "
                    "Выберите одного партнера для запуска волны.",
                )
            agency_id = filtered_agency_ids[0]
        selected_order_ids = _prioritized_queue_order_ids(
            filtered_orders,
            limit=queue_limit,
        )
    if not selected_order_ids:
        return _render_prepare_wave_error(request, "Нет подходящих заказов для волны.")
    choice_options = dict(
        selected_order_ids=selected_order_ids, agency_id=agency_id, queue_limit=queue_limit,
    )
    if not wave_action:
        return _render_wave_launch_choice(request, **choice_options)
    target_batch_id = None
    if wave_action == "existing":
        target_value = str(request.POST.get("target_batch_id") or "").strip()
        if not target_value.isascii() or not target_value.isdigit() or len(target_value) > 18 or int(target_value) <= 0:
            return _render_wave_launch_choice(
                request, **choice_options, error="Выберите волну для добавления заказов.",
            )
        target_batch_id = int(target_value)
    stock_shortage_policy = (
        STOCK_SHORTAGE_POLICY_SKIP
        if str(request.POST.get("confirm_without_stock") or "").strip() == "1"
        else STOCK_SHORTAGE_POLICY_REQUIRE_CONFIRMATION
    )
    try:
        result = prepare_pick_queue(
            limit=queue_limit,
            order_ids=selected_order_ids,
            single_agency_only=True,
            max_orders_per_batch=100,
            max_units_per_batch=100,
            performed_by=request.user,
            stock_shortage_policy=stock_shortage_policy,
            reuse_queued_batches=False,
            target_batch_id=target_batch_id,
        )
    except FbsQueueStockConfirmationRequired as exc:
        return _render_wave_stock_confirmation(
            request,
            shortages=exc.shortages,
            selected_order_ids=selected_order_ids,
            agency_id=agency_id,
            queue_limit=queue_limit,
        )
    except FbsError as exc:
        return _render_wave_launch_choice(request, **choice_options, error=str(exc))
    rejection_message = format_queue_rejections(getattr(result, "rejected_orders", ()))
    if rejection_message and not result.tasks_created:
        return _render_prepare_wave_error(
            request, f"Волна не создана. {rejection_message}"
        )
    if rejection_message:
        messages.warning(request, rejection_message)
    if result.awaiting_stock_orders:
        messages.warning(
            request,
            "Не включено в волну без товара на остатках: "
            f"{result.awaiting_stock_orders} заказов.",
        )
    if result.validation_failed_orders:
        messages.warning(
            request,
            "Не включено из-за ошибок данных: "
            f"{result.validation_failed_orders} заказов.",
        )
    if not result.tasks_created and (
        result.awaiting_stock_orders
        or result.validation_failed_orders
        or rejection_message
    ):
        return _render_prepare_wave_error(
            request,
            "Волна не создана: после проверки не осталось доступных заказов.",
        )
    if not result.tasks_created:
        return _render_prepare_wave_error(
            request, "Новые задания не созданы. Возможно, заказы уже переданы в сборку. Обновите очередь.",
        )
    query = urlencode(
        {
            "prepared": 1,
            "batches": len(result.batches),
            "tasks": result.tasks_created,
        }
    )
    return redirect(f"{reverse('fbs:operator_waves')}?{query}")


@operator_module_required
@role_required(*OPERATOR_ROLES)
@require_GET
def operator_problems(request):
    page_size = _clean_page_size(request.GET.get("page_size"))
    page = Paginator(
        _filter_problem_orders(_orders_queryset()).order_by(
            F("cutoff_at").asc(nulls_last=True), "imported_at", "id"
        ),
        page_size,
    ).get_page(request.GET.get("page"))
    _decorate_orders(page.object_list)
    return render(
        request,
        "fbs/operator_problems.html",
        _base_context(
            request,
            section="problems",
            page_title="FBS · Проблемы",
            back_url=reverse("fbs:tsd_storekeeper"),
            page=page,
            page_size=page_size,
            page_sizes=QUEUE_PAGE_SIZES,
            pick_exceptions=FbsPickException.objects.filter(status=FbsPickException.STATUS_OPEN)
            .select_related("task__order__profile__agency", "task__batch")
            .order_by("-created_at", "-id")[:100],
            label_errors=FbsOrderLabel.objects.filter(status=FbsOrderLabel.STATUS_ERROR)
            .select_related("order__profile__agency")
            .order_by("-updated_at", "-id")[:100],
            transfer_conflicts=FbsMarketplaceMetadataTransfer.objects.filter(
                status__in=PROBLEM_TRANSFER_STATUSES
            )
            .select_related("order_item__order__profile__agency")
            .order_by("-updated_at", "-id")[:100],
        ),
    )
