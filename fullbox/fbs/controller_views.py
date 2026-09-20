from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
import json
import re
from types import SimpleNamespace
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Exists, IntegerField, OuterRef, Prefetch, Q, Sum
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from agent.models import AgentEvent
from employees.access import get_request_employee, get_request_role, role_required
from labels.services import (
    build_equipment_status_payload,
    scanner_settings_apply_response,
)

from .controller_session import (
    CONTROLLER_WORKSTATION_SESSION_KEY,
    get_controller_workstation,
)
from .exceptions import FbsError
from .flags import feature_enabled
from .models import (
    FbsControllerCheckTote,
    FbsControllerPickTote,
    FbsControllerSession,
    FbsControllerToteOrder,
    FbsHandoverBatch,
    FbsHandoverBox,
    FbsHandoverOrder,
    FbsHandoverOrderAssignment,
    FbsIntegrationProfile,
    FbsMarketplaceCommand,
    FbsMarketplaceMetadataTransfer,
    FbsOrderStockAllocation,
    FbsPickBatch,
    FbsPickScanEvent,
    FbsPickingCart,
    FbsPickRestockLine,
    FbsPickRestockRequest,
    FbsToteBinding,
    FbsWorkstation,
)
from .services import (
    ACTIVE_PICK_RESTOCK_STATUSES,
    create_pick_restock_request,
)
from .services.picking import CONTROLLER_ACTIVE_WAVE_LIMIT
from .services.pick_restock import (
    BLOCKING_PICK_RESTOCK_STATUSES,
    confirm_ozon_canceled_order_return,
)
from .services.totes import (
    ACTIVE_CHECK_TOTE_STATUSES,
    ACTIVE_PICK_TOTE_STATUSES,
    COMPOSITION_ALREADY_PACKED_MESSAGE,
    CompositionProblemToteRoutingRequired,
    SEPARATED_PICK_RESTOCK_STATUSES,
    active_check_tote_orders,
    add_controller_check_tote,
    attach_pick_tote_to_available_check_tote,
    attach_pick_tote_to_check_tote,
    bind_controller_service_totes,
    check_tote_readiness,
    check_totes_readiness,
    close_controller_check_tote,
    close_controller_session,
    confirm_check_tote_composition_item,
    confirm_composition_item_in_problem_tote,
    confirm_pick_tote_empty,
    start_controller_session,
)
from .services.controller_shift import (
    controller_shift_is_live,
    heartbeat_controller_shift,
    start_controller_shift,
)
from .tsd_views import fbs_module_required


CONTROLLER_CABINET_ROLES = ("fbs_controller", "head_manager", "director", "admin")

CONTROLLER_AGENT_RETRY_MIN_SECONDS = 6.5
CONTROLLER_AGENT_RETRY_MAX_SECONDS = 9.5
CONTROLLER_METADATA_POLL_INTERVAL_MS = 5000
CONTROLLER_REPORT_STAGES = (
    FbsPickScanEvent.STAGE_VERIFY_ITEM,
    FbsPickScanEvent.STAGE_VERIFY_MARKING,
    FbsPickScanEvent.STAGE_VERIFY_EXPIRY,
    FbsPickScanEvent.STAGE_ORDER_LABEL,
)


def _controller_report_duration(seconds: int) -> str:
    seconds = max(int(seconds or 0), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def _controller_daily_report(*, controller, report_date=None) -> dict:
    report_date = report_date or timezone.localdate()
    current_timezone = timezone.get_current_timezone()
    day_start = timezone.make_aware(
        datetime.combine(report_date, datetime.min.time()),
        current_timezone,
    )
    day_end = day_start + timedelta(days=1)

    events = list(
        FbsPickScanEvent.objects.filter(
            created_by=controller,
            created_at__gte=day_start,
            created_at__lt=day_end,
            stage__in=CONTROLLER_REPORT_STAGES,
        )
        .select_related("batch__agency", "task")
        .order_by("created_at", "id")
    )
    successful_events = [
        event
        for event in events
        if event.stage == FbsPickScanEvent.STAGE_VERIFY_ITEM
        and event.result == FbsPickScanEvent.RESULT_SUCCESS
    ]
    error_events = [
        event for event in events if event.result == FbsPickScanEvent.RESULT_ERROR
    ]
    completed_waves = list(
        FbsPickBatch.objects.filter(
            verification_assigned_to=controller,
            completed_at__gte=day_start,
            completed_at__lt=day_end,
            verification_started_at__isnull=False,
        )
        .select_related("agency")
        .order_by("completed_at", "id")
    )
    closed_shipments = list(
        FbsControllerCheckTote.objects.filter(
            closed_by=controller,
            closed_at__gte=day_start,
            closed_at__lt=day_end,
        )
        .select_related("agency")
        .order_by("closed_at", "id")
    )

    def new_client_row(agency):
        if agency is None:
            return {
                "name": "Клиент не указан",
                "units": 0,
                "orders": set(),
                "waves": 0,
                "shipments": 0,
                "wave_seconds": 0,
                "longest_wave_seconds": 0,
                "errors": 0,
            }
        return {
            "name": agency.short_name or agency.agn_name or f"Клиент {agency.id}",
            "units": 0,
            "orders": set(),
            "waves": 0,
            "shipments": 0,
            "wave_seconds": 0,
            "longest_wave_seconds": 0,
            "errors": 0,
        }

    clients = {}

    def client_row(agency):
        key = int(agency.id) if agency is not None else 0
        if key not in clients:
            clients[key] = new_client_row(agency)
        return clients[key]

    for event in successful_events:
        row = client_row(event.batch.agency)
        row["units"] += 1
        if event.task_id:
            row["orders"].add(event.task.order_id)

    total_wave_seconds = 0
    longest_wave_seconds = 0
    for batch in completed_waves:
        interval_start = max(batch.verification_started_at, day_start)
        interval_end = min(batch.completed_at, day_end)
        wave_seconds = max(int((interval_end - interval_start).total_seconds()), 0)
        total_wave_seconds += wave_seconds
        longest_wave_seconds = max(longest_wave_seconds, wave_seconds)
        row = client_row(batch.agency)
        row["waves"] += 1
        row["wave_seconds"] += wave_seconds
        row["longest_wave_seconds"] = max(
            row["longest_wave_seconds"], wave_seconds
        )

    for shipment in closed_shipments:
        client_row(shipment.agency)["shipments"] += 1

    error_reasons = Counter()
    for event in error_events:
        client_row(event.batch.agency)["errors"] += 1
        reason = str(event.message or "Ошибка без описания").strip()
        error_reasons[reason[:160] or "Ошибка без описания"] += 1

    client_rows = []
    for row in clients.values():
        wave_count = int(row["waves"])
        wave_seconds = int(row["wave_seconds"])
        client_rows.append(
            {
                "name": row["name"],
                "units": row["units"],
                "orders": len(row["orders"]),
                "waves": wave_count,
                "shipments": row["shipments"],
                "wave_time": _controller_report_duration(wave_seconds),
                "average_wave_time": _controller_report_duration(
                    wave_seconds // wave_count if wave_count else 0
                ),
                "longest_wave_time": _controller_report_duration(
                    row["longest_wave_seconds"]
                ),
                "errors": row["errors"],
            }
        )
    client_rows.sort(key=lambda row: (-row["units"], row["name"].casefold()))

    order_ids = {
        event.task.order_id for event in successful_events if event.task_id
    }
    first_event = events[0].created_at if events else None
    last_event = events[-1].created_at if events else None
    activity_seconds = (
        max(int((last_event - first_event).total_seconds()), 0)
        if first_event and last_event
        else 0
    )
    wave_count = len(completed_waves)
    return {
        "date": report_date,
        "units": len(successful_events),
        "orders": len(order_ids),
        "waves": wave_count,
        "shipments": len(closed_shipments),
        "wave_time": _controller_report_duration(total_wave_seconds),
        "average_wave_time": _controller_report_duration(
            total_wave_seconds // wave_count if wave_count else 0
        ),
        "longest_wave_time": _controller_report_duration(longest_wave_seconds),
        "errors": len(error_events),
        "first_activity": first_event,
        "last_activity": last_event,
        "activity_time": _controller_report_duration(activity_seconds),
        "clients": client_rows,
        "error_reasons": error_reasons.most_common(5),
    }


def _wb_delivery_status_payload(batch: FbsHandoverBatch) -> dict:
    if batch.marketplace_state == FbsHandoverBatch.MARKETPLACE_COMPLETE:
        return {
            "delivery_status": "complete",
            "message": "Статус отправки: передано в доставку WB.",
            "retry_after_ms": 0,
        }
    if batch.marketplace_state == FbsHandoverBatch.MARKETPLACE_ERROR:
        return {
            "delivery_status": "error",
            "message": (
                "Wildberries вернул ошибку отправки. Проверенные сканы сохранены."
            ),
            "retry_after_ms": 0,
        }
    return {
        "delivery_status": "uploading",
        "message": (
            "Статус отправки: загружается в доставку WB. "
            "Обновится автоматически."
        ),
        "retry_after_ms": 2000,
    }


def _dashboard_metadata_error_group(row):
    technical = " ".join(
        [str(row.get("external_status") or ""), str(row.get("last_error") or "")]
    ).casefold()
    if "sgtinapplied" in technical:
        return (
            "КИЗ уже использован",
            "WB сообщил, что код уже применён. Проверьте товар и отсканируйте другой действующий Data Matrix.",
        )
    if "sgtininvalidformat" in technical or "неверный формат" in technical:
        return (
            "Неверный формат КИЗ",
            "Повторно отсканируйте Data Matrix с товара. Если ошибка повторится, передайте товар ответственному за маркировку.",
        )
    if row.get("metadata_type") == FbsMarketplaceMetadataTransfer.TYPE_EXPIRATION:
        return (
            "Срок годности не подтверждён",
            "Проверьте срок годности товара и повторите передачу данных площадке.",
        )
    return (
        "КИЗ не подтверждён маркетплейсом",
        "Откройте отгрузку, проверьте ответ площадки и повторно отсканируйте Data Matrix.",
    )


def _decorate_check_tote_dashboard(check_totes) -> None:
    check_tote_ids = [check_tote.id for check_tote in check_totes]
    if not check_tote_ids:
        return
    separated_restock = FbsPickRestockRequest.objects.filter(
        order_id=OuterRef("order_id"),
        status__in=SEPARATED_PICK_RESTOCK_STATUSES,
    )
    active_rows = (
        FbsControllerToteOrder.objects.filter(check_tote_id__in=check_tote_ids)
        .exclude(status=FbsControllerToteOrder.STATUS_REMOVED)
        .exclude(
            order__handover_assignment__status=(
                FbsHandoverOrderAssignment.STATUS_CANCELED
            )
        )
        .annotate(has_separated_restock=Exists(separated_restock))
        .filter(has_separated_restock=False)
        .values("check_tote_id")
        .annotate(
            order_count=Count("id"),
            active_unit_count=Coalesce(Sum("units"), 0),
        )
    )
    totals_by_tote = {int(row["check_tote_id"]): row for row in active_rows}
    batch_ids = [
        check_tote.handover_batch_id
        for check_tote in check_totes
        if check_tote.handover_batch_id
    ]
    status_labels = dict(FbsControllerCheckTote.STATUS_CHOICES)
    if not batch_ids:
        for check_tote in check_totes:
            totals = totals_by_tote.get(check_tote.id, {})
            check_tote.order_count = int(totals.get("order_count") or 0)
            check_tote.active_unit_count = int(
                totals.get("active_unit_count") or 0
            )
            display_label = status_labels.get(
                check_tote.status, check_tote.status
            )
            if check_tote.status == FbsControllerCheckTote.STATUS_WAITING_KIZ:
                display_label = "Ожидает КИЗ/marketplace"
            check_tote.readiness = SimpleNamespace(
                ready=check_tote.status == FbsControllerCheckTote.STATUS_READY,
                display_label=display_label,
                blocking_hint="",
            )
            check_tote.error_details = []
            check_tote.error_count = 0
            check_tote.error_unit_count = 0
        return
    returns_by_batch = defaultdict(list)
    if batch_ids:
        return_rows = FbsPickRestockRequest.objects.filter(
            handover_assignment__batch_id__in=batch_ids,
            status__in=BLOCKING_PICK_RESTOCK_STATUSES,
        ).values(
            "handover_assignment__batch_id",
            "order_id",
            "order__external_order_id",
            "status",
            "source_tote_id",
            "planned_qty",
            "reason",
        )
        for row in return_rows:
            returns_by_batch[row["handover_assignment__batch_id"]].append(row)
    try:
        dashboard_readiness = check_totes_readiness(check_totes)
    except Exception:  # noqa: BLE001 - individual fallback keeps the screen usable
        dashboard_readiness = {}
    readiness_by_tote = {}
    metadata_order_ids = set()
    affected_order_ids = set()
    for check_tote in check_totes:
        totals = totals_by_tote.get(check_tote.id, {})
        check_tote.order_count = int(totals.get("order_count") or 0)
        check_tote.active_unit_count = int(totals.get("active_unit_count") or 0)
        # Read every flow from the fixed-query dashboard snapshot.  If a single
        # deployment/runtime edge case prevents bulk calculation, preserve the
        # previous per-flow path so the controller screen still opens.
        try:
            readiness = dashboard_readiness.get(check_tote.id)
            if readiness is None:
                readiness = check_tote_readiness(check_tote)
        except Exception:  # noqa: BLE001 - the dashboard must open regardless
            readiness = None
        if readiness is None:
            display_label = status_labels.get(check_tote.status, check_tote.status)
            if check_tote.status == FbsControllerCheckTote.STATUS_WAITING_KIZ:
                display_label = "Ожидает КИЗ/marketplace"
            check_tote.readiness = SimpleNamespace(
                ready=check_tote.status == FbsControllerCheckTote.STATUS_READY,
                display_label=display_label,
                blocking_hint="",
            )
            check_tote.error_details = []
            check_tote.error_count = 0
            check_tote.error_unit_count = 0
            continue
        readiness_by_tote[check_tote.id] = readiness
        metadata_order_ids.update(readiness.metadata_blocked_order_ids)
        affected_order_ids.update(readiness.blocked_order_ids)
        hints = []
        returns = returns_by_batch.get(check_tote.handover_batch_id, [])
        affected_order_ids.update(
            row["order_id"] for row in returns if row["order_id"] is not None
        )
        physical_count = sum(row["source_tote_id"] is None for row in returns)
        failed_count = sum(row["status"] == FbsPickRestockRequest.STATUS_FAILED for row in returns)
        if physical_count:
            hints.append(f"переложите отменённые заказы в тару своего стола: {physical_count}")
        if failed_count:
            hints.append(f"ошибка подтверждения возврата МП: {failed_count}; откройте отгрузку и проверьте причину")
        elif len(returns) > physical_count:
            hints.append(f"ожидается подтверждение возврата МП: {len(returns) - physical_count}; можно проверять другие заказы")
        if readiness.metadata_blocked_order_ids:
            hints.append(f"ждут КИЗ или срок: {len(readiness.metadata_blocked_order_ids)}")
        if readiness.assignment_blocked_order_ids:
            hints.append(
                f"маркетплейс подтверждает: {len(readiness.assignment_blocked_order_ids)}"
            )
        if readiness.label_blocked_order_ids:
            hints.append(f"ждут этикетку: {len(readiness.label_blocked_order_ids)}")
        check_tote.readiness = SimpleNamespace(
            ready=readiness.ready and not returns,
            display_label="Ожидает завершения возврата" if returns else readiness.display_label,
            blocking_hint="; ".join(hints),
        )

    order_rows_by_key = {}
    if affected_order_ids:
        for row in FbsControllerToteOrder.objects.filter(
            check_tote_id__in=check_tote_ids,
            order_id__in=affected_order_ids,
        ).exclude(status=FbsControllerToteOrder.STATUS_REMOVED).values(
            "check_tote_id",
            "order_id",
            "order__external_order_id",
            "units",
            "order__handover_assignment__error",
            "label__error",
        ):
            order_rows_by_key[(row["check_tote_id"], row["order_id"])] = row

    metadata_rows_by_order = defaultdict(list)
    if metadata_order_ids:
        for row in FbsMarketplaceMetadataTransfer.objects.filter(
            order_item__order_id__in=metadata_order_ids
        ).exclude(status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED).values(
            "order_item__order_id",
            "metadata_type",
            "status",
            "external_status",
            "last_error",
        ):
            metadata_rows_by_order[row["order_item__order_id"]].append(row)

    for check_tote in check_totes:
        readiness = readiness_by_tote.get(check_tote.id)
        if readiness is None:
            continue
        details = []
        affected_quantities = {}

        def order_description(order_id):
            row = order_rows_by_key.get((check_tote.id, order_id), {})
            return (
                str(row.get("order__external_order_id") or order_id),
                int(row.get("units") or 0),
            )

        def append_issue(title, instruction, rows, *, quantity_key="units"):
            if not rows:
                return
            order_numbers = []
            quantity = 0
            seen = set()
            for row in rows:
                order_id = row["order_id"]
                if order_id in seen:
                    continue
                seen.add(order_id)
                order_number, tote_quantity = order_description(order_id)
                order_number = str(
                    row.get("order__external_order_id") or order_number
                )
                order_numbers.append(order_number)
                row_quantity = int(row.get(quantity_key) or tote_quantity or 0)
                quantity += row_quantity
                affected_quantities[order_id] = max(
                    affected_quantities.get(order_id, 0), row_quantity
                )
            details.append({
                "title": title,
                "instruction": instruction,
                "order_count": len(seen),
                "quantity": quantity,
                "order_numbers": order_numbers[:6],
                "remaining_count": max(len(order_numbers) - 6, 0),
            })

        returns = returns_by_batch.get(check_tote.handover_batch_id, [])
        append_issue(
            "Ошибка возврата товара",
            "Откройте отгрузку и проверьте причину ошибки возврата.",
            [row for row in returns if row["status"] == FbsPickRestockRequest.STATUS_FAILED],
            quantity_key="planned_qty",
        )
        append_issue(
            "Маркетплейс проверяет отмену",
            "Дождитесь подтверждения отмены. Другие заказы можно продолжать проверять.",
            [row for row in returns if row["status"] == FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE],
            quantity_key="planned_qty",
        )
        movable_returns = [
            row for row in returns
            if row["status"] in {
                FbsPickRestockRequest.STATUS_QUEUED,
                FbsPickRestockRequest.STATUS_IN_PROGRESS,
            }
        ]
        append_issue(
            "Отменённые товары не убраны",
            "Переложите товары этих заказов в служебную тару отменённых заказов.",
            [row for row in movable_returns if row["source_tote_id"] is None],
            quantity_key="planned_qty",
        )
        append_issue(
            "Возврат товара не завершён",
            "Товар уже в служебной таре и ожидает возврата на складской остаток.",
            [row for row in movable_returns if row["source_tote_id"] is not None],
            quantity_key="planned_qty",
        )

        metadata_groups = defaultdict(list)
        for order_id in readiness.metadata_blocked_order_ids:
            transfer_rows = metadata_rows_by_order.get(order_id, [])
            if not transfer_rows:
                group = (
                    "Нет подтверждения КИЗ или срока",
                    "Проверьте обязательные данные товара и повторно отсканируйте КИЗ или срок годности.",
                )
                metadata_groups[group].append({"order_id": order_id})
                continue
            for transfer_row in transfer_rows:
                metadata_groups[_dashboard_metadata_error_group(transfer_row)].append(
                    {"order_id": order_id}
                )
        for (title, instruction), rows in metadata_groups.items():
            append_issue(title, instruction, rows)

        append_issue(
            "Маркетплейс не подтвердил заказ",
            "Откройте отгрузку и проверьте ответ площадки по добавлению заказа.",
            [{"order_id": order_id} for order_id in readiness.assignment_blocked_order_ids],
        )
        append_issue(
            "Официальная этикетка не готова",
            "Дождитесь этикетки маркетплейса или откройте отгрузку для просмотра ошибки.",
            [{"order_id": order_id} for order_id in readiness.label_blocked_order_ids],
        )

        check_tote.error_details = details
        check_tote.error_count = sum(detail["order_count"] for detail in details)
        check_tote.error_unit_count = sum(affected_quantities.values())
        if details:
            check_tote.readiness = SimpleNamespace(
                ready=False,
                display_label="Есть ошибки",
                blocking_hint=(
                    f"ошибок: {check_tote.error_count}; "
                    f"товаров: {check_tote.error_unit_count}"
                ),
            )


def _composition_operation_payload(check_tote, tote_order):
    scanned, total = _composition_scan_progress(check_tote)
    if tote_order.status == FbsControllerToteOrder.STATUS_REMOVED:
        return {"ok": False, "error": "Скан сохранён, но заказ уже исключён из этой проверки.", "operation_resolved": True}
    return {
        "ok": True, "scan_state": "accepted", "title": "СТИКЕР ПРИНЯТ",
        "operation_resolved": True,
        "sticker_number": str(tote_order.label.external_label_id or tote_order.label.barcode or ""),
        "order_number": tote_order.order.external_order_id,
        "box_code": tote_order.transport_box.qr_code if tote_order.transport_box_id else "текущий транспортный короб",
        "tote_order_id": tote_order.id, "scanned_count": scanned, "total_count": total,
        "all_scanned": bool(total and scanned == total),
    }


def _composition_scan_error_payload(message: str) -> dict[str, object]:
    normalized = str(message or "").casefold()
    if "уже прошёл проверку" in normalized:
        state = "duplicate"
        title = "ПОВТОР ТОВАРА"
        instruction = ""
    elif "тару проверки" in normalized or "отгрузке стола" in normalized:
        state = "foreign_order"
        title = "ЧУЖОЙ ЗАКАЗ"
        instruction = "Уберите этот товар: он относится к другой проверке."
    else:
        state = "rejected"
        title = "СКАН ОТКЛОНЕН"
        instruction = "Уберите товар и проверьте этикетку заказа."
    return {
        "ok": False,
        "scan_state": state,
        "title": title,
        "error": str(message or "Сканирование отклонено."),
        "instruction": instruction,
        "blocking": state != "duplicate",
    }


def _composition_error_sticker_context(
    check_tote: FbsControllerCheckTote,
    message: str,
) -> dict[str, str]:
    """Make the physical sticker the primary identifier in controller errors."""
    error_text = str(message or "")
    tote_orders = active_check_tote_orders(check_tote).select_related(
        "order",
        "label",
    )
    for tote_order in tote_orders:
        order_number = str(tote_order.order.external_order_id or "").strip()
        if not order_number or order_number not in error_text:
            continue
        label = tote_order.label
        sticker_number = str(
            (label.external_label_id if label is not None else "")
            or (label.barcode if label is not None else "")
            or ""
        ).strip()
        if not sticker_number:
            return {
                "error": error_text,
                "order_number": order_number,
            }
        error_text = error_text.replace(
            f"Заказ {order_number}",
            f"Стикер {sticker_number} (заказ {order_number})",
            1,
        )
        return {
            "error": error_text,
            "sticker_number": sticker_number,
            "order_number": order_number,
        }
    return {"error": error_text}


def _current_check_tote_required_transfers(tote_order):
    current_pick_batch_id = tote_order.pick_tote.pick_batch_id
    transfers = []
    for item in tote_order.order.items.all():
        for transfer in item.metadata_transfers.all():
            if not transfer.is_required:
                continue
            if transfer.traceability_id is None:
                transfers.append(transfer)
                continue
            allocation = getattr(transfer.traceability, "allocation", None)
            pick_task = getattr(allocation, "pick_task", None)
            if pick_task is not None and pick_task.batch_id == current_pick_batch_id:
                transfers.append(transfer)
    return transfers


def _check_tote_metadata_status(
    required_transfers, *, marketplace: str, metadata_blocked: bool | None = None
) -> dict:
    transfers = list(required_transfers)
    waiting = sum(
        transfer.status != FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED
        for transfer in transfers
    )
    # The shipment gate also detects missing transfers and local resolutions.
    # An empty list alone is not evidence that the order needs no metadata.
    if metadata_blocked is True and waiting == 0:
        return {
            "state": "problem",
            "label": "Нет подтверждения обязательного КИЗ/срока. Проверьте заказ.",
            "css_class": "status-problem",
            "ok": False,
            "waiting": 1,
        }
    if metadata_blocked is False and waiting:
        return {
            "state": "resolved",
            "label": "Проверено по правилам отгрузки",
            "css_class": "status-done",
            "ok": True,
            "waiting": 0,
        }
    if not transfers:
        return {
            "state": "not_required",
            "label": "Не требуется",
            "css_class": "status-done",
            "ok": True,
            "waiting": 0,
        }

    confirmed = waiting == 0
    if marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        return {
            "state": "confirmed" if confirmed else "waiting",
            "label": "ОК" if confirmed else f"Ожидает {waiting}",
            "css_class": "status-done" if confirmed else "status-warning",
            "ok": confirmed,
            "waiting": waiting,
        }
    if confirmed:
        return {
            "state": "confirmed",
            "label": "Подтверждено WB",
            "css_class": "status-done",
            "ok": True,
            "waiting": 0,
        }

    statuses = {transfer.status for transfer in transfers}
    if statuses & METADATA_PROBLEM_STATUSES:
        state = "problem"
        problem_error = next(
            (
                str(getattr(transfer, "last_error", "") or "").strip()
                for transfer in transfers
                if transfer.status in METADATA_PROBLEM_STATUSES
                and str(getattr(transfer, "last_error", "") or "").strip()
            ),
            "",
        )
        label = problem_error or "WB не подтвердил КИЗ/срок"
        css_class = "status-problem"
    elif statuses & {
        FbsMarketplaceMetadataTransfer.STATUS_SENT,
        FbsMarketplaceMetadataTransfer.STATUS_RETRY,
    }:
        state = "checking"
        label = "WB проверяет КИЗ/срок"
        css_class = "status-warning"
    elif FbsMarketplaceMetadataTransfer.STATUS_QUEUED in statuses:
        state = "queued"
        label = "Отправляется в WB"
        css_class = "status-warning"
    elif FbsMarketplaceMetadataTransfer.STATUS_PREPARED in statuses:
        state = "prepared"
        label = "Готовится к отправке"
        css_class = "status-warning"
    else:
        state = "waiting"
        label = "Ожидает подтверждения WB"
        css_class = "status-warning"
    return {
        "state": state,
        "label": label,
        "css_class": css_class,
        "ok": False,
        "waiting": waiting,
    }


def _decorate_check_tote_metadata(tote_order, *, marketplace: str, readiness=None) -> None:
    metadata = _check_tote_metadata_status(
        _current_check_tote_required_transfers(tote_order),
        marketplace=marketplace,
        metadata_blocked=(
            tote_order.order_id in readiness.metadata_blocked_order_ids
            if readiness is not None else None
        ),
    )
    tote_order.metadata_state = metadata["state"]
    tote_order.metadata_label = metadata["label"]
    tote_order.metadata_css_class = metadata["css_class"]
    tote_order.metadata_ok = metadata["ok"]
    tote_order.metadata_waiting = metadata["waiting"]


def _check_tote_metadata_poll_orders(check_tote, *, readiness=None):
    """Return narrow poll rows using the same decision as the shipment gate."""
    if readiness is None:
        readiness = check_tote_readiness(check_tote)
    tote_rows = list(
        active_check_tote_orders(check_tote)
        .order_by("label_confirmed_at", "id")
        .values("id", "order_id", "pick_tote__pick_batch_id")
    )
    if not tote_rows:
        return [], None

    tote_row_by_order_id = {row["order_id"]: row for row in tote_rows}
    transfers_by_order_id = {row["order_id"]: [] for row in tote_rows}
    oldest_waiting_at = None
    transfer_rows = FbsMarketplaceMetadataTransfer.objects.filter(
        order_item__order_id__in=transfers_by_order_id,
        is_required=True,
    ).values(
        "order_item__order_id",
        "traceability_id",
        "traceability__allocation__pick_task__batch_id",
        "status",
        "last_error",
        "prepared_at",
    )
    for transfer_row in transfer_rows:
        order_id = transfer_row["order_item__order_id"]
        tote_row = tote_row_by_order_id[order_id]
        if (
            transfer_row["traceability_id"] is not None
            and transfer_row["traceability__allocation__pick_task__batch_id"]
            != tote_row["pick_tote__pick_batch_id"]
        ):
            continue
        transfer = SimpleNamespace(
            status=transfer_row["status"],
            last_error=transfer_row["last_error"],
        )
        transfers_by_order_id[order_id].append(transfer)
        if transfer.status != FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED:
            prepared_at = transfer_row["prepared_at"]
            if oldest_waiting_at is None or prepared_at < oldest_waiting_at:
                oldest_waiting_at = prepared_at

    payload_rows = []
    for tote_row in tote_rows:
        metadata = _check_tote_metadata_status(
            transfers_by_order_id[tote_row["order_id"]],
            marketplace=check_tote.profile.marketplace,
            metadata_blocked=(tote_row["order_id"] in readiness.metadata_blocked_order_ids),
        )
        payload_rows.append(
            {
                "id": tote_row["id"],
                "state": metadata["state"],
                "label": metadata["label"],
                "css_class": metadata["css_class"],
                "ok": metadata["ok"],
            }
        )
    return payload_rows, oldest_waiting_at


def _composition_scan_progress(check_tote) -> tuple[int, int]:
    active_orders = active_check_tote_orders(check_tote)
    total_count = active_orders.count()
    scanned_count = active_orders.filter(
        status=FbsControllerToteOrder.STATUS_PACKED
    ).count()
    return scanned_count, total_count


def _controller_agent_replay_event_ids(events) -> set[int]:
    """Identify the COM agent's delayed retry without blocking a later real scan."""
    accepted_at_by_scan: dict[tuple[str, str], datetime] = {}
    replay_event_ids: set[int] = set()
    for event in events:
        event_payload = event.payload if isinstance(event.payload, dict) else {}
        value = str(event_payload.get("value") or "").strip()
        source = str(event_payload.get("source") or "").strip().casefold()
        if not value or source != "com":
            continue
        signature = (source, value)
        accepted_at = accepted_at_by_scan.get(signature)
        if accepted_at is not None:
            elapsed = (event.created_at - accepted_at).total_seconds()
            if CONTROLLER_AGENT_RETRY_MIN_SECONDS <= elapsed <= CONTROLLER_AGENT_RETRY_MAX_SECONDS:
                replay_event_ids.add(event.id)
                continue
        accepted_at_by_scan[signature] = event.created_at
    return replay_event_ids


CONTROLLER_PENDING_PICK_TOTE_SESSION_KEY = "fbs_controller_pending_pick_tote"
METADATA_WAITING_STATUSES = {
    FbsMarketplaceMetadataTransfer.STATUS_PREPARED,
    FbsMarketplaceMetadataTransfer.STATUS_QUEUED,
    FbsMarketplaceMetadataTransfer.STATUS_SENT,
    FbsMarketplaceMetadataTransfer.STATUS_RETRY,
}
METADATA_PROBLEM_STATUSES = {
    FbsMarketplaceMetadataTransfer.STATUS_FAILED,
    FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
    FbsMarketplaceMetadataTransfer.STATUS_UNSUPPORTED,
    FbsMarketplaceMetadataTransfer.STATUS_CANCELED,
}


def _get_or_select_controller_workstation(request) -> FbsWorkstation | None:
    workstation = get_controller_workstation(request)
    if workstation is None:
        return None
    workstation = (
        FbsWorkstation.objects.select_related(
            "device_agent",
            "active_handover_box__batch__profile__agency",
        )
        .filter(pk=workstation.pk, is_active=True)
        .first()
    )
    # Fullbox Desktop keeps the physical workstation binding while employees
    # change.  The authenticated controller entering that same Desktop window
    # takes over the desk session and its unfinished checks atomically.
    if (
        workstation is not None
        and workstation.shift_controller_id is not None
        and workstation.shift_controller_id != request.user.id
    ):
        user_agent = str(getattr(request, "META", {}).get("HTTP_USER_AGENT") or "")
        if re.search(r"(?:^|\s)FullboxDesktop/\d+\.\d+\.\d+(?:\s|$)", user_agent):
            try:
                return start_controller_shift(
                    workstation_id=workstation.id,
                    controller=request.user,
                )
            except FbsError:
                pass
        request.session.pop(CONTROLLER_WORKSTATION_SESSION_KEY, None)
        return None
    return workstation


def _previous_controller_tote_session(
    workstation: FbsWorkstation | None,
) -> FbsControllerSession | None:
    """Return the latest closed session while its shared service tote is free."""
    if workstation is None:
        return None
    previous = (
        FbsControllerSession.objects.select_related(
            "unknown_tote",
            "free_zone",
        )
        .filter(
            workstation=workstation,
            status=FbsControllerSession.STATUS_CLOSED,
            closed_at__isnull=False,
            unknown_tote__is_active=True,
        )
        .order_by("-closed_at", "-id")
        .first()
    )
    if previous is None:
        return None
    tote_id = previous.unknown_tote_id
    is_free = FbsToteBinding.objects.filter(
        tote_id=tote_id,
        state=FbsToteBinding.STATE_FREE,
        zone_id=previous.free_zone_id,
    ).exists()
    if not is_free:
        return None
    if FbsControllerSession.objects.filter(
        Q(unknown_tote_id=tote_id)
        | Q(problem_tote_id=tote_id)
        | Q(canceled_tote_id=tote_id),
        status=FbsControllerSession.STATUS_ACTIVE,
    ).exists():
        return None
    return previous


def _controller_last_scan_event_id(workstation: FbsWorkstation | None) -> int:
    if workstation is None or workstation.device_agent_id is None:
        return 0
    event_id = (
        AgentEvent.objects.filter(
            agent_id=workstation.device_agent.agent_id,
            event_type=AgentEvent.EVENT_SCAN,
        )
        .order_by("-id")
        .values_list("id", flat=True)
        .first()
    )
    return int(event_id or 0)


def _batch_queryset():
    return (
        FbsPickBatch.objects.select_related(
            "assigned_to",
            "verification_assigned_to",
            "workstation",
            "cart",
        )
        .annotate(
            order_count=Count("tasks", distinct=True),
            verified_qty=Coalesce(
                Sum("tasks__allocations__verification_progress__qty_verified"),
                0,
                output_field=IntegerField(),
            ),
        )
    )


def _controller_metadata_state(*, required, scanned, transfer):
    if not required:
        return {"state": "pending", "label": "Не требуется"}
    if transfer is not None and transfer.status in METADATA_PROBLEM_STATUSES:
        if transfer.status in {
            FbsMarketplaceMetadataTransfer.STATUS_FAILED,
            FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
        }:
            label = "Невалиден"
        elif transfer.status == FbsMarketplaceMetadataTransfer.STATUS_UNSUPPORTED:
            label = "Проверка недоступна"
        else:
            label = transfer.get_status_display()
        return {"state": "problem", "label": label}
    if (
        transfer is not None
        and transfer.status == FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED
    ):
        return {"state": "done", "label": "Подтверждено МП"}
    if scanned:
        return {"state": "in_progress", "label": "Отсканировано"}
    if transfer is not None and transfer.status in METADATA_WAITING_STATUSES:
        return {"state": "in_progress", "label": transfer.get_status_display()}
    return {"state": "pending", "label": "Ожидает скан"}


def _controller_equipment_context(workstation: FbsWorkstation | None) -> dict:
    if workstation is None:
        return {
            "connected": False,
            "agent": {"online": False, "text": "Сначала подключите рабочее место"},
            "printer": {"severity": "neutral", "text": "Стол не подключен"},
            "scanner": {"severity": "neutral", "text": "Стол не подключен"},
            "printers": [],
            "scanner_ports": [],
            "scanner_config": {"port": "", "baud": 9600, "eol": "Cr", "idle_ms": 200},
        }

    agent = workstation.device_agent
    if agent is None:
        return {
            "connected": True,
            "agent": {"online": False, "text": "Агент рабочего места не назначен"},
            "printer": {"severity": "danger", "text": "Сначала назначьте агент"},
            "scanner": {"severity": "danger", "text": "Сначала назначьте агент"},
            "printers": [],
            "scanner_ports": [],
            "scanner_config": {"port": "", "baud": 9600, "eol": "Cr", "idle_ms": 200},
        }

    payload = build_equipment_status_payload(
        agent_name=agent.agent_id,
        printer_name=workstation.printer_name,
    )
    meta = agent.meta if isinstance(agent.meta, dict) else {}
    com = meta.get("com") if isinstance(meta.get("com"), dict) else {}
    raw_ports = meta.get("com_ports") or meta.get("ports") or []
    if isinstance(raw_ports, str):
        raw_ports = [raw_ports]
    scanner_ports = sorted(
        {
            str(port).strip().upper()
            for port in raw_ports
            if re.fullmatch(r"COM\d+", str(port).strip(), flags=re.IGNORECASE)
        }
    )
    printer = payload.get("printer") if isinstance(payload.get("printer"), dict) else {}
    scanner = payload.get("scanner") if isinstance(payload.get("scanner"), dict) else {}
    detected_printers = [
        str(name).strip()
        for name in printer.get("detected", [])
        if str(name).strip()
    ]
    configured_printer = str(workstation.printer_name or "").strip()
    configured_detected = bool(
        configured_printer
        and any(
            name.casefold() == configured_printer.casefold()
            for name in detected_printers
        )
    )
    if not configured_printer:
        printer = {**printer, "severity": "danger", "text": "Принтер не выбран"}
    elif not configured_detected:
        printer = {
            **printer,
            "severity": "danger",
            "text": f"{configured_printer}: не найден на компьютере",
        }

    scanner_reason = str(scanner.get("reason") or "").strip()
    scanner_port = str(com.get("port") or scanner.get("port") or "").strip().upper()
    if scanner_reason == "port_not_found":
        scanner_hint = (
            f"Windows на {agent.name or agent.host or agent.agent_id} не видит "
            f"порт {scanner_port or 'сканера'}. Проверьте USB-кабель, драйвер и режим USB-COM."
        )
    elif scanner_reason == "port_missing":
        scanner_hint = "Выберите COM-порт сканера и примените настройки."
    elif scanner.get("ready") is True:
        scanner_hint = "Сканер подключен и передает данные Fullbox Agent."
    else:
        scanner_hint = str(scanner.get("text") or "Проверьте подключение сканера.")

    return {
        "connected": True,
        "agent": payload.get("agent") or {
            "online": False,
            "text": "Нет данных от Fullbox Agent",
        },
        "printer": printer,
        "scanner": scanner,
        "printers": detected_printers,
        "scanner_ports": scanner_ports,
        "scanner_hint": scanner_hint,
        "scanner_config": {
            "port": scanner_port,
            "baud": int(com.get("baud") or 9600),
            "eol": str(com.get("eol") or "Cr"),
            "idle_ms": int(com.get("idle_ms") or 200),
        },
    }


def _save_controller_printer(*, request, workstation: FbsWorkstation) -> tuple[bool, str]:
    equipment = _controller_equipment_context(workstation)
    selected = str(request.POST.get("printer_name") or "").strip()
    detected = equipment.get("printers") or []
    canonical = next(
        (name for name in detected if name.casefold() == selected.casefold()),
        "",
    )
    if not canonical:
        return False, "Выбранный принтер не найден на компьютере этого рабочего места."

    try:
        with transaction.atomic():
            locked = FbsWorkstation.objects.select_for_update().get(
                pk=workstation.pk,
                is_active=True,
            )
            locked.printer_name = canonical
            locked.updated_by = request.user
            locked.full_clean()
            locked.save(
                update_fields=["printer_name", "updated_by", "updated_at"],
            )
    except (FbsWorkstation.DoesNotExist, ValidationError):
        return False, "Не удалось сохранить принтер. Обновите экран и повторите."
    return True, f"Принтер {canonical} назначен для {workstation.name}."


def _apply_controller_scanner(
    *,
    request,
    workstation: FbsWorkstation,
    reconnect_only: bool = False,
) -> tuple[bool, str]:
    agent = workstation.device_agent
    if agent is None:
        return False, "У рабочего места не назначен Fullbox Agent."

    port = str(request.POST.get("scanner_port") or "").strip().upper()
    if not reconnect_only and not re.fullmatch(r"COM\d+", port, flags=re.IGNORECASE):
        return False, "Укажите COM-порт в формате COM3, COM4 и т. п."
    try:
        baud = int(request.POST.get("scanner_baud") or 9600)
        idle_ms = int(request.POST.get("scanner_idle_ms") or 200)
    except (TypeError, ValueError):
        return False, "Скорость и задержка сканера должны быть числами."
    if baud <= 0 or idle_ms < 0:
        return False, "Проверьте скорость и задержку сканера."
    eol = str(request.POST.get("scanner_eol") or "Cr").strip()
    if eol not in {"CrLf", "Cr", "Lf", "Tab", "None"}:
        return False, "Выберите допустимый символ завершения сканирования."

    body = json.dumps(
        {
            "agent_id": agent.agent_id,
            "reconnect_only": reconnect_only,
            "reconnect": not reconnect_only,
            "settings": {
                "enabled": True,
                "port": port,
                "baud": baud,
                "eol": eol,
                "idle_ms": idle_ms,
            },
        }
    ).encode("utf-8")
    response = scanner_settings_apply_response(body=body)
    try:
        result = json.loads(response.content.decode("utf-8"))
    except (TypeError, ValueError, json.JSONDecodeError):
        result = {}
    if response.status_code >= 400 or not result.get("ok"):
        if result.get("error") == "port_not_available":
            skipped = result.get("skipped_agents") or []
            available = []
            if skipped and isinstance(skipped[0], dict):
                available = skipped[0].get("available_ports") or []
            available_text = ", ".join(available) if available else "ни одного"
            return False, (
                f"Настройки не отправлены: Windows не видит {port}. "
                f"Доступные COM-порты: {available_text}. Проверьте USB и драйвер сканера."
            )
        return False, "Агент не принял настройки сканера. Обновите экран и повторите."
    if reconnect_only:
        return True, "Команда переподключения сканера отправлена агенту."
    return True, f"Настройки {port} отправлены агенту. Обновите статус через несколько секунд."


def _controller_context(
    request,
    *,
    error="",
    ok_message="",
    released_tote=None,
    suppress_previous_totes=False,
):
    workstation = _get_or_select_controller_workstation(request)
    equipment = _controller_equipment_context(workstation)
    waiting_waves = []
    if workstation is not None:
        waiting_waves = list(
            _batch_queryset()
            .filter(
                status=FbsPickBatch.STATUS_VERIFICATION,
                picking_completed_at__isnull=False,
                cart_released_at__isnull=True,
                verification_assigned_to__isnull=True,
                workstation=workstation,
            )
            .exclude(pick_restock_request__status__in=ACTIVE_PICK_RESTOCK_STATUSES)
            .order_by("picking_completed_at", "id")[:50]
        )
    workstation_wave_count = 0
    workstation_capacity = 0
    if workstation is not None:
        workstation_capacity = max(int(workstation.max_parallel_waves or 1), 1)
        workstation_wave_count = (
            FbsPickBatch.objects.filter(
                status=FbsPickBatch.STATUS_VERIFICATION,
                picking_completed_at__isnull=False,
                cart_released_at__isnull=True,
                completed_at__isnull=True,
                workstation=workstation,
            )
            .exclude(pick_restock_request__status__in=ACTIVE_PICK_RESTOCK_STATUSES)
            .count()
        )
    equipment_message = f"{error} {ok_message}".casefold()
    tote_session = None
    check_totes = []
    pick_totes = []
    pending_deliveries = []
    tote_kpis = {
        "check_totes": 0,
        "items_on_check": 0,
        "processed_today": 0,
        "shipments_today": 0,
    }
    if workstation is not None:
        tote_session = (
            FbsControllerSession.objects.select_related(
                "unknown_tote",
                "problem_tote",
                "canceled_tote",
                "free_zone",
                "workstation",
            )
            .filter(
                controller=request.user,
                workstation=workstation,
                status=FbsControllerSession.STATUS_ACTIVE,
            )
            .first()
        )
    if workstation is not None:
        pending_deliveries = list(FbsControllerCheckTote.objects.select_related("handover_batch", "profile__agency").filter(
            session__workstation=workstation,
            status=FbsControllerCheckTote.STATUS_CLOSED,
            profile__marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            handover_batch__isnull=False,
        ).exclude(handover_batch__marketplace_state=FbsHandoverBatch.MARKETPLACE_COMPLETE).order_by("closed_at", "id")[:50])
        for closed in pending_deliveries:
            closed.delivery = _wb_delivery_status_payload(closed.handover_batch)
    if tote_session is not None:
        check_totes = list(
            tote_session.check_totes.select_related(
                "tote", "profile__agency", "handover_batch"
            )
            .filter(status__in=ACTIVE_CHECK_TOTE_STATUSES)
            .order_by("opened_at", "id")
        )
        _decorate_check_tote_dashboard(check_totes)
        pick_totes = list(
            tote_session.pick_totes.select_related(
                "tote", "check_tote__tote", "pick_batch"
            )
            .filter(status__in=ACTIVE_PICK_TOTE_STATUSES)
            .order_by("attached_at", "id")
        )
        today = timezone.localdate()
        tote_kpis = {
            "check_totes": len(check_totes),
            "items_on_check": sum(
                check_tote.active_unit_count for check_tote in check_totes
            ),
            "processed_today": FbsControllerToteOrder.objects.filter(
                label_confirmed_by=tote_session.controller,
                label_confirmed_at__date=today,
            ).count(),
            "shipments_today": FbsControllerCheckTote.objects.filter(
                closed_by=tote_session.controller,
                closed_at__date=today,
            ).count(),
        }
    return {
        "employee": get_request_employee(request),
        "request_role": get_request_role(request),
        "warehouse_writes_enabled": feature_enabled("warehouse_writes"),
        "operator_section": "controller",
        "page_title": "FBS · Сборка заказов",
        "workstation": workstation,
        "controller_equipment": equipment,
        "controller_scan_event_id": _controller_last_scan_event_id(workstation),
        "equipment_panel_open": bool(
            request.GET.get("equipment") == "1"
            or any(
                marker in equipment_message
                for marker in ("принтер", "сканер", "агент", "com-порт")
            )
        ),
        "waiting_waves": waiting_waves,
        "workstation_wave_count": workstation_wave_count,
        "workstation_capacity": workstation_capacity,
        "tote_session": tote_session,
        "previous_tote_session": (
            _previous_controller_tote_session(workstation)
            if (
                tote_session is None
                and not suppress_previous_totes
                and request.GET.get("change_totes") != "1"
            )
            else None
        ),
        "tote_session_ready": bool(
            tote_session
            and tote_session.problem_tote_id
            and tote_session.canceled_tote_id
        ),
        "check_totes": check_totes,
        "pending_deliveries": pending_deliveries,
        "pick_totes": pick_totes,
        "tote_kpis": tote_kpis,
        "pick_restock_open_count": FbsPickRestockRequest.objects.filter(
            status__in=ACTIVE_PICK_RESTOCK_STATUSES
        ).count(),
        "error": error,
        "ok_message": ok_message,
        "released_tote": released_tote,
    }


@fbs_module_required
@role_required(*CONTROLLER_CABINET_ROLES)
@require_GET
def controller_scan_events(request):
    workstation = _get_or_select_controller_workstation(request)
    if workstation is None or workstation.device_agent_id is None:
        return JsonResponse(
            {
                "ok": True,
                "events": [],
                "last_event_id": 0,
                "reason": "workstation_not_connected",
            }
        )
    try:
        since_id = max(int(request.GET.get("since") or 0), 0)
    except (TypeError, ValueError):
        since_id = 0
    events = list(
        AgentEvent.objects.filter(
            id__gt=since_id,
            agent_id=workstation.device_agent.agent_id,
            event_type=AgentEvent.EVENT_SCAN,
        )
        .order_by("id")[:10]
    )
    replay_event_ids: set[int] = set()
    if events:
        history_started_at = events[0].created_at - timedelta(
            seconds=CONTROLLER_AGENT_RETRY_MAX_SECONDS
        )
        replay_event_ids = _controller_agent_replay_event_ids(
            AgentEvent.objects.filter(
                agent_id=workstation.device_agent.agent_id,
                event_type=AgentEvent.EVENT_SCAN,
                created_at__gte=history_started_at,
                id__lte=events[-1].id,
            )
            .only("id", "payload", "created_at")
            .order_by("id")
        )
    payload = []
    for event in events:
        event_payload = event.payload if isinstance(event.payload, dict) else {}
        value = str(event_payload.get("value") or "").strip()
        if not value:
            continue
        payload.append(
            {
                "id": event.id,
                "value": value,
                "source": str(event_payload.get("source") or ""),
                "is_replay": event.id in replay_event_ids,
            }
        )
    last_event_id = events[-1].id if events else since_id
    return JsonResponse(
        {
            "ok": True,
            "events": payload,
            "last_event_id": last_event_id,
        }
    )


@fbs_module_required
@role_required(*CONTROLLER_CABINET_ROLES)
@require_http_methods(["POST"])
def controller_shift_heartbeat(request):
    workstation = _get_or_select_controller_workstation(request)
    if workstation is None:
        return JsonResponse(
            {
                "ok": False,
                "error": "workstation_not_connected",
                "reason": "workstation_not_connected",
            },
            status=409,
        )
    try:
        workstation = heartbeat_controller_shift(
            workstation_id=workstation.id,
            controller=request.user,
        )
    except FbsError as exc:
        error = str(exc)
        if error == "Смена этого рабочего места открыта другим контролером.":
            reason = "shift_taken_over"
        elif error == "Смена рабочего места закрыта.":
            reason = "shift_closed"
        else:
            reason = "shift_conflict"
        return JsonResponse(
            {"ok": False, "error": error, "reason": reason},
            status=409,
        )
    return JsonResponse(
        {
            "ok": True,
            "status": workstation.shift_status,
            "heartbeat_at": workstation.shift_heartbeat_at.isoformat(),
        }
    )


@fbs_module_required
@role_required(*CONTROLLER_CABINET_ROLES)
@require_GET
def controller_daily_report(request):
    workstation = _get_or_select_controller_workstation(request)
    tote_session = None
    if workstation is not None:
        tote_session = (
            FbsControllerSession.objects.filter(
                workstation=workstation,
                controller=request.user,
                status=FbsControllerSession.STATUS_ACTIVE,
            )
            .select_related("workstation")
            .first()
        )
    return render(
        request,
        "fbs/controller_daily_report.html",
        {
            "employee": get_request_employee(request),
            "request_role": get_request_role(request),
            "warehouse_writes_enabled": feature_enabled("warehouse_writes"),
            "operator_section": "controller",
            "page_title": "FBS · Мой день",
            "back_url": reverse("fbs:controller_home"),
            "workstation": workstation,
            "tote_session": tote_session,
            "report": _controller_daily_report(controller=request.user),
            "show_close_action": request.GET.get("close") == "1",
            "pick_restock_open_count": FbsPickRestockRequest.objects.filter(
                status__in=ACTIVE_PICK_RESTOCK_STATUSES
            ).count(),
        },
    )


@fbs_module_required
@role_required(*CONTROLLER_CABINET_ROLES)
@require_http_methods(["GET", "POST"])
def controller_home(request):
    if request.method == "GET" and request.GET.get("delivery_status") == "1":
        workstation = _get_or_select_controller_workstation(request)
        if workstation is None:
            return JsonResponse({"ok": False, "error": "Рабочее место не выбрано."}, status=409)
        ids = [int(value) for value in request.GET.get("batch_ids", "").split(",") if value.isdigit()][:50]
        rows = FbsControllerCheckTote.objects.select_related("handover_batch").filter(
            session__workstation=workstation, status=FbsControllerCheckTote.STATUS_CLOSED,
            profile__marketplace=FbsIntegrationProfile.MARKETPLACE_WB, handover_batch_id__in=ids,
        )
        return JsonResponse({"ok": True, "deliveries": [
            {"batch_id": row.handover_batch_id, **_wb_delivery_status_payload(row.handover_batch)} for row in rows
        ]})
    error = ""
    ok_message = ""
    suppress_previous_totes = False
    if request.method == "POST":
        action = str(request.POST.get("action") or "bind_workstation").strip()
        if action in {"save_printer", "apply_scanner", "reconnect_scanner"}:
            workstation = _get_or_select_controller_workstation(request)
            if workstation is None:
                error = "Рабочее место не настроено. Обратитесь к начальнику склада."
            elif action == "save_printer":
                success, message = _save_controller_printer(
                    request=request,
                    workstation=workstation,
                )
                ok_message, error = (message, "") if success else ("", message)
            else:
                success, message = _apply_controller_scanner(
                    request=request,
                    workstation=workstation,
                    reconnect_only=action == "reconnect_scanner",
                )
                ok_message, error = (message, "") if success else ("", message)
        elif action in {"start_tote_session", "reuse_previous_totes"}:
            workstation = _get_or_select_controller_workstation(request)
            if workstation is None:
                error = "Рабочее место не настроено. Обратитесь к начальнику склада."
            else:
                try:
                    previous_session = None
                    if action == "reuse_previous_totes":
                        previous_session = _previous_controller_tote_session(workstation)
                        requested_previous_id = int(
                            request.POST.get("previous_session_id") or 0
                        )
                        if (
                            previous_session is None
                            or previous_session.id != requested_previous_id
                        ):
                            raise ValueError(
                                "Прежняя служебная тара уже недоступна. "
                                "Отсканируйте другую свободную тару."
                            )
                    if not (
                        workstation.shift_controller_id == request.user.id
                        and workstation.shift_status == FbsWorkstation.SHIFT_AVAILABLE
                        and controller_shift_is_live(workstation)
                    ):
                        start_controller_shift(
                            workstation_id=workstation.id,
                            controller=request.user,
                        )
                    start_controller_session(
                        workstation_id=workstation.id,
                        controller=request.user,
                        unknown_tote_scan=(
                            previous_session.unknown_tote.barcode
                            if previous_session is not None
                            else request.POST.get("unknown_tote_scan", "")
                        ),
                    )
                except (TypeError, ValueError, FbsError) as exc:
                    error = str(exc)
                    suppress_previous_totes = action == "reuse_previous_totes"
                else:
                    return redirect("fbs:controller_home")
        elif action == "bind_service_totes":
            try:
                bind_controller_service_totes(
                    session_id=int(request.POST.get("session_id") or 0),
                    problem_tote_scan=request.POST.get("problem_tote_scan", ""),
                    canceled_tote_scan=request.POST.get("canceled_tote_scan", ""),
                    performed_by=request.user,
                )
            except (TypeError, ValueError, FbsError) as exc:
                error = str(exc) or "Не удалось привязать служебные тары."
            else:
                return redirect("fbs:controller_home")
        elif action == "scan_pick_tote":
            workstation = _get_or_select_controller_workstation(request)
            pick_scan = str(request.POST.get("pick_tote_scan") or "").strip().upper()
            if workstation is None:
                error = "Рабочее место не настроено."
            elif not FbsControllerSession.objects.filter(
                workstation=workstation,
                controller=request.user,
                status=FbsControllerSession.STATUS_ACTIVE,
                problem_tote__isnull=False,
                canceled_tote__isnull=False,
            ).exists():
                error = "Сначала откройте рабочую сессию и привяжите служебную тару."
            elif not pick_scan:
                error = "Отсканируйте QR тары подбора."
            else:
                tote_session = FbsControllerSession.objects.filter(
                    workstation=workstation,
                    controller=request.user,
                    status=FbsControllerSession.STATUS_ACTIVE,
                ).first()
                try:
                    pick_context = attach_pick_tote_to_available_check_tote(
                        session_id=tote_session.id,
                        pick_tote_scan=pick_scan,
                        performed_by=request.user,
                    )
                except (TypeError, ValueError, FbsError) as exc:
                    error = str(exc) or "Не удалось принять тару подбора."
                else:
                    request.session.pop(
                        CONTROLLER_PENDING_PICK_TOTE_SESSION_KEY,
                        None,
                    )
                    return redirect(
                        "fbs:tsd_pick_verification",
                        batch_id=pick_context.pick_batch_id,
                    )
        elif action == "confirm_pick_tote_empty":
            try:
                released = confirm_pick_tote_empty(
                    pick_batch_id=int(request.POST.get("pick_batch_id") or 0),
                    performed_by=request.user,
                )
            except (TypeError, ValueError, FbsError) as exc:
                error = str(exc) or "Не удалось освободить тару подбора."
            else:
                released_query = urlencode(
                    {"tote_released": "1", "tote": released.tote.barcode}
                )
                return redirect(
                    f"{reverse('fbs:controller_home')}?{released_query}"
                )
        elif action == "close_tote_session":
            try:
                close_controller_session(
                    session_id=int(request.POST.get("session_id") or 0),
                    performed_by=request.user,
                )
            except (TypeError, ValueError, FbsError) as exc:
                error = str(exc) or "Не удалось закрыть смену тары."
            else:
                return redirect("fbs:controller_home")
        elif action != "bind_workstation":
            error = "Неизвестная команда рабочего места."
        else:
            scan = str(request.POST.get("workstation_scan") or "").strip()
            workstation = FbsWorkstation.objects.filter(
                barcode__iexact=scan,
                is_active=True,
            ).first()
            if workstation is None:
                error = "QR рабочего места не найден или рабочее место выключено."
            else:
                try:
                    workstation = start_controller_shift(
                        workstation_id=workstation.id,
                        controller=request.user,
                    )
                except FbsError as exc:
                    error = str(exc)
                else:
                    request.session[CONTROLLER_WORKSTATION_SESSION_KEY] = workstation.id
                    ok_message = (
                        f"Подключено рабочее место: {workstation.name}. "
                        "Принтер, сканер и задания этого стола готовы к работе."
                    )
    if request.GET.get("done") == "1":
        ok_message = "Проверка волны завершена. Можно взять следующую."
    if request.GET.get("empty"):
        ok_message = "Все заказы обработаны. Подтвердите, что тара подбора физически пустая."
    released_tote = None
    if request.GET.get("tote_released") == "1":
        # Штрихкод приходит строкой запроса, поэтому на экран попадает только
        # найденная в базе тара — произвольный текст не выводим.
        released_tote = FbsPickingCart.objects.filter(
            barcode=str(request.GET.get("tote") or "").strip()
        ).first()
        if released_tote is not None:
            ok_message = (
                f"Тара подбора {released_tote.barcode} освобождена — "
                "отвезите её в зону свободной тары."
            )
        else:
            ok_message = "Тара подбора освобождена и возвращена в свободный фонд."
    if request.GET.get("shipment_ready") == "1":
        if request.GET.get("wb_delivery") in {"queued", "uploading"}:
            ok_message = (
                "Вся поставка проверена. Отправка в доставку WB "
                "запущена автоматически."
            )
        elif request.GET.get("wb_delivery") == "error":
            ok_message = "Проверка сохранена. WB вернул ошибку отправки — откройте отгрузку. Можно продолжать работу с другими тарами."
        elif request.GET.get("wb_delivery") == "complete":
            ok_message = "Проверка сохранена. WB подтвердил передачу в доставку."
        else:
            ok_message = "Проверка состава закрыта. Отгрузка готова."
    if error and request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": False, "error": error}, status=400)
    return render(
        request,
        "fbs/controller_dashboard.html",
        _controller_context(
            request,
            error=error,
            ok_message=ok_message,
            released_tote=released_tote,
            suppress_previous_totes=suppress_previous_totes,
        ),
        status=400 if error else 200,
    )


def _check_tote_work_history(check_tote):
    """Read saved control stages only; never infer an actor from the current session."""
    from employees.models import Employee
    from zoneinfo import ZoneInfo

    records = list(FbsControllerToteOrder.objects.filter(check_tote=check_tote)
        .select_related("order__handover_order__box", "label", "label_confirmed_by",
                        "composition_checked_by", "order__handover_order__verified_by")
        .order_by("label_confirmed_at", "id"))
    users = {}
    for row in records:
        for user in (row.label_confirmed_by, row.composition_checked_by):
            if user:
                users[user.pk] = user
        try:
            user = row.order.handover_order.verified_by
            if user:
                users[user.pk] = user
        except FbsHandoverOrder.DoesNotExist:
            pass
    names = dict(Employee.objects.filter(user_id__in=users).values_list("user_id", "full_name"))

    def actor(user):
        return (names.get(user.pk) or user.get_full_name() or user.get_username()) if user else "Не записан"

    def stamp(value):
        return timezone.localtime(value, ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M:%S") if value else "Время не записано"

    facts, events = {}, []
    for row in records:
        first = f"{actor(row.label_confirmed_by)} · {stamp(row.label_confirmed_at)}"
        checked_at, checked_by = row.composition_checked_at, row.composition_checked_by
        if check_tote.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
            try:
                link = row.order.handover_order
            except FbsHandoverOrder.DoesNotExist:
                link = None
            if link and link.status == FbsHandoverOrder.STATUS_ACTIVE and link.box.batch_id == check_tote.handover_batch_id:
                checked_at, checked_by = link.verified_at, link.verified_by
            else:
                checked_at, checked_by = None, None
        mode = "По первичному скану" if row.primary_order_label_scan_reused else "Проверка состава"
        second = f"{actor(checked_by)} · {stamp(checked_at)} · {mode}" if checked_at else "Не подтверждено"
        facts[str(row.pk)] = {"primary": first, "composition": second}
        suffix = " · Заказ исключён из потока" if row.status == row.STATUS_REMOVED else ""
        events.append({"at": row.label_confirmed_at, "when": stamp(row.label_confirmed_at),
                       "order": row.order.external_order_id, "actor": actor(row.label_confirmed_by),
                       "stage": "Первый контроль" + suffix})
        # Retain saved historical composition for excluded orders, but not as current readiness.
        history_at = checked_at or (row.composition_checked_at if row.status == row.STATUS_REMOVED else None)
        history_by = checked_by if checked_at else row.composition_checked_by
        if history_at:
            events.append({"at": history_at, "when": stamp(history_at), "order": row.order.external_order_id,
                           "actor": actor(history_by), "stage": "Состав подтверждён · " + mode + suffix})
    events.sort(key=lambda event: event["at"], reverse=True)
    for event in events:
        event.pop("at")
    return {"rows": facts, "events": events[:300], "total": len(events)}


@fbs_module_required
@role_required(*CONTROLLER_CABINET_ROLES)
@require_http_methods(["GET", "POST"])
def controller_check_tote(request, check_tote_id: int):
    check_tote = get_object_or_404(
        FbsControllerCheckTote.objects.select_related(
            "session__workstation",
            "session__controller",
            "session__canceled_tote",
            "tote",
            "profile__agency",
            "handover_batch",
        ),
        pk=check_tote_id,
    )
    if request.method == "GET" and request.GET.get("work_history") == "1":
        response = JsonResponse({"ok": True, **_check_tote_work_history(check_tote)})
        response["Cache-Control"] = "private, no-store"
        return response
    if request.method == "GET" and request.GET.get("operation_status"):
        from .services.controller_operations import read_composition_operation
        try:
            row = read_composition_operation(
                check_tote_id=check_tote.id,
                operation_id=request.GET["operation_status"], actor=request.user,
            )
            if row is None:
                return JsonResponse({"ok": True, "operation_found": False})
            payload = _composition_operation_payload(check_tote, row)
            payload["operation_found"] = True
            if row.composition_request_duplicate:
                payload.update(_composition_scan_error_payload(COMPOSITION_ALREADY_PACKED_MESSAGE))
            return JsonResponse(payload)
        except (FbsError, ValueError) as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=409)
    error = ""
    ok_message = ""
    problem_route_scan = ""
    problem_route_tote_barcode = ""
    if request.method == "POST":
        action = str(request.POST.get("action") or "scan_composition").strip()
        fast_composition_scan = (
            action == "scan_composition"
            and request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )
        try:
            if action == "scan_composition":
                if request.POST.get("operation_id"):
                    from .services.controller_operations import submit_composition_operation
                    tote_order = submit_composition_operation(
                        check_tote_id=check_tote.id,
                        label_scan=request.POST.get("label_scan", ""),
                        operation_id=request.POST["operation_id"], actor=request.user,
                    )
                else:
                    tote_order = confirm_check_tote_composition_item(
                        check_tote_id=check_tote.id,
                        label_scan=request.POST.get("label_scan", ""),
                        performed_by=request.user,
                    )
                box_code = (
                    tote_order.transport_box.qr_code
                    if tote_order.transport_box_id
                    else "текущий транспортный короб"
                )
                ok_message = getattr(
                    tote_order,
                    "composition_scan_message",
                    (
                        f"Заказ {tote_order.order.external_order_id} подтвержден. "
                        f"Положите товар в транспортный короб {box_code}. "
                        "Затем сканируйте следующий заказ."
                    ),
                )
                if fast_composition_scan:
                    scanned_count, total_count = _composition_scan_progress(
                        check_tote
                    )
                    sticker_number = str(
                        tote_order.label.external_label_id
                        or tote_order.label.barcode
                        or ""
                    ).strip()
                    payload = {
                        "ok": True,
                        "scan_state": "accepted",
                        "title": "СТИКЕР ПРИНЯТ",
                        "message": ok_message,
                        "sticker_number": sticker_number,
                        "order_number": tote_order.order.external_order_id,
                        "box_code": box_code,
                        "tote_order_id": tote_order.id,
                        "scanned_count": scanned_count,
                        "total_count": total_count,
                        "all_scanned": bool(
                            total_count and scanned_count == total_count
                        ),
                    }
                    if request.POST.get("operation_id"):
                        payload = _composition_operation_payload(check_tote, tote_order)
                    if getattr(tote_order, "composition_request_duplicate", False) or (
                        ok_message == COMPOSITION_ALREADY_PACKED_MESSAGE
                        and not getattr(tote_order, "composition_request_replayed", False)
                    ):
                        duplicate_payload = _composition_scan_error_payload(
                            COMPOSITION_ALREADY_PACKED_MESSAGE
                        )
                        duplicate_payload.update(
                            {
                                "sticker_number": sticker_number,
                                "order_number": (
                                    tote_order.order.external_order_id
                                ),
                                "box_code": box_code,
                                "scanned_count": scanned_count,
                                "total_count": total_count,
                            }
                        )
                        return JsonResponse(duplicate_payload, status=409)
                    return JsonResponse(payload)
            elif action == "confirm_problem_tote":
                movement = confirm_composition_item_in_problem_tote(
                    check_tote_id=check_tote.id,
                    label_scan=request.POST.get("label_scan", ""),
                    problem_tote_scan=request.POST.get("problem_tote_scan", ""),
                    performed_by=request.user,
                )
                ok_message = (
                    "Товар помещен в проблемную тару "
                    f"{movement.target_code}. Ручное решение зафиксировано."
                )
            elif action == "confirm_ozon_canceled_return":
                restock = confirm_ozon_canceled_order_return(
                    handover_batch_id=check_tote.handover_batch_id,
                    order_id=int(request.POST.get("order_id") or 0),
                    order_scan=request.POST.get("order_scan", ""),
                    canceled_tote_scan=request.POST.get(
                        "canceled_tote_scan", ""
                    ),
                    comment="Физически убран из транспортного короба контролером.",
                    performed_by=request.user,
                )
                ok_message = (
                    f"Отмененный заказ {restock.order.external_order_id} помещен "
                    "в тару отмененных заказов и передан в возврат отбора."
                )
            elif action == "close_check_tote":
                close_controller_check_tote(
                    check_tote_id=check_tote.id,
                    performed_by=request.user,
                )
                delivery_payload = {
                    "delivery_status": "complete",
                    "message": "Проверка состава закрыта.",
                    "retry_after_ms": 0,
                }
                if (
                    check_tote.profile.marketplace
                    == FbsIntegrationProfile.MARKETPLACE_WB
                ):
                    handover_batch = FbsHandoverBatch.objects.only(
                        "marketplace_state"
                    ).get(pk=check_tote.handover_batch_id)
                    delivery_payload = _wb_delivery_status_payload(
                        handover_batch
                    )
                redirect_url = (
                    f"{reverse('fbs:controller_home')}"
                    f"?shipment_ready=1&wb_delivery={delivery_payload['delivery_status']}"
                    if check_tote.profile.marketplace
                    == FbsIntegrationProfile.MARKETPLACE_WB
                    else f"{reverse('fbs:controller_home')}?shipment_ready=1"
                )
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse(
                        {
                            "ok": True,
                            "shipment_complete": True,
                            "controller_can_continue": True,
                            "title": "ВСЯ ПОСТАВКА ПРОВЕРЕНА",
                            "redirect_url": redirect_url,
                            **delivery_payload,
                        }
                    )
                return redirect(redirect_url)
            else:
                raise ValueError("Неизвестная команда тары проверки.")
        except CompositionProblemToteRoutingRequired as exc:
            if fast_composition_scan:
                return JsonResponse(
                    {
                        "ok": False,
                        "scan_state": "problem_route",
                        "title": "НЕИЗВЕСТНАЯ ЭТИКЕТКА",
                        "error": str(exc),
                        "instruction": (
                            "Следуйте действующему маршруту проблемной тары."
                        ),
                        "fallback_submit": True,
                    },
                    status=409,
                )
            error = str(exc)
            problem_route_scan = exc.label_scan
            problem_route_tote_barcode = exc.problem_tote_barcode
        except (FbsError, ValueError) as exc:
            if fast_composition_scan:
                error_payload = _composition_scan_error_payload(str(exc))
                error_context = _composition_error_sticker_context(
                    check_tote,
                    str(exc),
                )
                error_context.setdefault(
                    "sticker_number",
                    str(request.POST.get("label_scan") or "").strip(),
                )
                error_payload.update(error_context)
                return JsonResponse(
                    error_payload,
                    status=409,
                )
            if (
                action == "close_check_tote"
                and request.headers.get("X-Requested-With") == "XMLHttpRequest"
            ):
                error_context = _composition_error_sticker_context(
                    check_tote,
                    str(exc),
                )
                return JsonResponse(
                    {
                        "ok": False,
                        "scan_state": "blocking",
                        "title": "ОТПРАВКА НЕ ЗАВЕРШЕНА",
                        "instruction": (
                            "Система сохранит проверенные сканы. Обновите экран "
                            "после устранения причины — отправка повторится автоматически."
                        ),
                        **error_context,
                    },
                    status=409,
                )
            error = str(exc)
        check_tote.refresh_from_db()
    readiness = check_tote_readiness(check_tote)
    if (
        request.method == "GET"
        and request.GET.get("metadata_status") == "1"
        and request.headers.get("X-Requested-With") == "XMLHttpRequest"
    ):
        metadata_orders, waiting_since = _check_tote_metadata_poll_orders(
            check_tote, readiness=readiness
        )
        metadata_has_problem = any(
            row["state"] == "problem" for row in metadata_orders
        )
        waiting_minutes = None
        if waiting_since is not None:
            waiting_minutes = max(
                0,
                int((timezone.now() - waiting_since).total_seconds() // 60),
            )
        if readiness.ready:
            poll_after_ms = 0
        elif metadata_has_problem:
            poll_after_ms = 5000
        else:
            poll_after_ms = CONTROLLER_METADATA_POLL_INTERVAL_MS
        return JsonResponse(
            {
                "ok": True,
                "ready": readiness.ready,
                "status": readiness.status,
                "status_label": readiness.display_label,
                "status_css_class": (
                    "status-done"
                    if readiness.ready
                    else (
                        "status-warning"
                        if readiness.status
                        == FbsControllerCheckTote.STATUS_WAITING_KIZ
                        else "status-verification"
                    )
                ),
                "reasons": [str(reason) for reason in readiness.reasons],
                "orders": metadata_orders,
                "waiting_since": (
                    waiting_since.isoformat()
                    if waiting_since is not None
                    else None
                ),
                "waiting_minutes": waiting_minutes,
                "poll_after_ms": poll_after_ms,
            }
        )
    orders = list(
        active_check_tote_orders(check_tote)
        .select_related(
            "order",
            "order__handover_order__box",
            "label",
            "transport_box",
            "pick_tote__tote",
            "pick_tote__pick_batch",
        )
        .prefetch_related(
            Prefetch(
                "order__items__metadata_transfers",
                queryset=FbsMarketplaceMetadataTransfer.objects.select_related(
                    "traceability__allocation__pick_task"
                ),
            )
        )
        .order_by("label_confirmed_at", "id")
    )
    for row in orders:
        try:
            handover_link = row.order.handover_order
        except FbsHandoverOrder.DoesNotExist:
            handover_link = None
        row.label_scanned = (
            row.status == row.STATUS_PACKED
            if check_tote.profile.marketplace
            != FbsIntegrationProfile.MARKETPLACE_WB
            else bool(
                handover_link
                and handover_link.status == FbsHandoverOrder.STATUS_ACTIVE
                and handover_link.box.batch_id == check_tote.handover_batch_id
                and handover_link.verified_at is not None
            )
        )
        _decorate_check_tote_metadata(
            row,
            marketplace=check_tote.profile.marketplace,
            readiness=readiness,
        )
    orders.sort(key=lambda row: (row.label_scanned, row.label_confirmed_at, row.id))
    scanned_label_count = sum(row.label_scanned for row in orders)
    work_history = _check_tote_work_history(check_tote)
    for row in orders:
        row.work_history = work_history["rows"].get(str(row.pk), {})
    pending_ozon_canceled_returns = list(
        FbsPickRestockRequest.objects.filter(
            handover_assignment__batch_id=check_tote.handover_batch_id,
            order__profile__marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            order__controller_tote_orders__check_tote_id=check_tote.id,
            order__controller_tote_orders__status__in=(
                FbsControllerToteOrder.STATUS_LABELED,
                FbsControllerToteOrder.STATUS_COMPOSITION,
                FbsControllerToteOrder.STATUS_PACKED,
            ),
            status=FbsPickRestockRequest.STATUS_QUEUED,
            source_tote__isnull=True,
        )
        .select_related("order")
        .order_by("id")
        .distinct()
    )
    for restock in pending_ozon_canceled_returns:
        restock.order_label_barcode = str(
            FbsControllerToteOrder.objects.filter(
                check_tote_id=check_tote.id,
                order_id=restock.order_id,
            )
            .exclude(status=FbsControllerToteOrder.STATUS_REMOVED)
            .values_list("label__barcode", flat=True)
            .first()
            or ""
        ).strip()
    from .controller_shipment_ui import check_progress
    problem_context = check_progress(check_tote, orders)
    if get_request_role(request) == "fbs_controller" and check_tote.handover_batch_id:
        from .tsd_views import _handover_detail_queryset, _handover_detail_summary, _handover_agent_scan_context
        from .controller_problems import controller_problem_context
        problem_batch = _handover_detail_queryset().get(pk=check_tote.handover_batch_id)
        problem_context.update(controller_problem_context(
            problem_batch, _handover_detail_summary(problem_batch, compact_controller=True), request.user,
        ))
        problem_context.update(_handover_agent_scan_context(request))
    return render(
        request,
        "fbs/controller_check_tote.html",
        {
            "employee": get_request_employee(request),
            "request_role": get_request_role(request),
            "warehouse_writes_enabled": feature_enabled("warehouse_writes"),
            "operator_section": "controller",
            "page_title": (
                f"FBS · Тара проверки {check_tote.tote.name}"
                if check_tote.tote_id
                else f"FBS · Отгрузка №{check_tote.handover_batch_id}" if check_tote.handover_batch_id else "FBS · Проверка заказов"
            ),
            "back_url": reverse("fbs:controller_home"),
            "check_tote": check_tote,
            "readiness": readiness,
            "orders": orders,
            "active_unit_count": sum(int(row.units or 0) for row in orders),
            "scanned_label_count": scanned_label_count,
            "pending_ozon_canceled_returns": pending_ozon_canceled_returns,
            "canceled_tote": check_tote.session.canceled_tote,
            "controller_scan_event_id": _controller_last_scan_event_id(
                check_tote.session.workstation
            ),
            "problem_route_scan": problem_route_scan,
            "problem_route_tote_barcode": problem_route_tote_barcode,
            "pick_restock_open_count": FbsPickRestockRequest.objects.filter(
                status__in=ACTIVE_PICK_RESTOCK_STATUSES
            ).count(),
            "error": error,
            "ok_message": ok_message,
            **problem_context,
        },
        status=400 if error else 200,
    )


def _pick_restock_request_rows(queryset):
    rows = list(
        queryset.prefetch_related(
            Prefetch(
                "lines",
                queryset=FbsPickRestockLine.objects.select_related(
                    "allocation__pick_task__order",
                    "source_box__pallet__cell__location",
                ).order_by("source_box__box_code", "id"),
            )
        )
    )
    for row in rows:
        lines = list(row.lines.all())
        row.order_count = len(
            {line.allocation.pick_task.order_id for line in lines}
        )
        source_boxes = {}
        for line in lines:
            box = line.source_box
            key = box.id
            source = source_boxes.setdefault(
                key,
                {
                    "box_code": box.box_code,
                    "cell_label": box.pallet.cell.warehouse_location_label,
                    "planned_qty": 0,
                    "returned_qty": 0,
                },
            )
            source["planned_qty"] += int(line.planned_qty or 0)
            source["returned_qty"] += int(line.returned_qty or 0)
        row.source_boxes = list(source_boxes.values())
    return rows


def _pick_restock_candidate_rows():
    batches = list(
        _batch_queryset()
        .filter(
            status=FbsPickBatch.STATUS_VERIFICATION,
            picking_completed_at__isnull=False,
            picked_qty__gt=0,
            pick_restock_request__isnull=True,
        )
        .order_by("picking_completed_at", "id")[:50]
    )
    for batch in batches:
        allocations = list(
            FbsOrderStockAllocation.objects.select_related(
                "balance__box__pallet__cell__location"
            )
            .filter(
                pick_task__batch=batch,
                status=FbsOrderStockAllocation.STATUS_PICKED,
                qty_picked__gt=0,
            )
            .order_by("balance__box__box_code", "id")
        )
        source_boxes = {}
        for allocation in allocations:
            box = allocation.balance.box
            source = source_boxes.setdefault(
                box.id,
                {
                    "box_code": box.box_code,
                    "cell_label": box.pallet.cell.warehouse_location_label,
                    "qty": 0,
                },
            )
            source["qty"] += int(allocation.qty_picked or 0)
        batch.restock_source_boxes = list(source_boxes.values())
    return batches


@fbs_module_required
@role_required(*CONTROLLER_CABINET_ROLES)
@require_http_methods(["GET", "POST"])
def controller_pick_restocks(request):
    error = ""
    ok_message = ""
    if request.method == "POST":
        if request.POST.get("confirm") != "1":
            error = "Подтвердите, что товары физически находятся у контролера."
        else:
            try:
                restock = create_pick_restock_request(
                    batch_id=int(request.POST.get("batch_id") or 0),
                    reason=request.POST.get("reason", ""),
                    created_by=request.user,
                )
            except (TypeError, ValueError, FbsError) as exc:
                error = str(exc) or "Не удалось создать задание возврата."
            else:
                return redirect(
                    f"{reverse('fbs:controller_pick_restocks')}?created={restock.id}"
                )
    if request.GET.get("created"):
        ok_message = "Задание возврата создано. Подборщик увидит его на ТСД."
    open_requests = _pick_restock_request_rows(
        FbsPickRestockRequest.objects.select_related(
            "batch__cart",
            "batch__workstation",
            "assigned_to",
            "created_by",
            "source_tote",
            "quarantine_box__pallet__cell__location",
        ).filter(status__in=ACTIVE_PICK_RESTOCK_STATUSES).order_by("created_at", "id")
    )
    history = _pick_restock_request_rows(
        FbsPickRestockRequest.objects.select_related(
            "batch__cart",
            "batch__workstation",
            "assigned_to",
            "created_by",
            "source_tote",
            "quarantine_box__pallet__cell__location",
        ).exclude(status__in=ACTIVE_PICK_RESTOCK_STATUSES).order_by(
            "-completed_at", "-id"
        )[:20]
    )
    return render(
        request,
        "fbs/controller_pick_restocks.html",
        {
            "employee": get_request_employee(request),
            "request_role": get_request_role(request),
            "warehouse_writes_enabled": feature_enabled("warehouse_writes"),
            "operator_section": "pick_restocks",
            "page_title": "Возврат ошибочного отбора",
            "candidate_batches": _pick_restock_candidate_rows(),
            "open_requests": open_requests,
            "history": history,
            "error": error,
            "ok_message": ok_message,
        },
        status=409 if error else 200,
    )
