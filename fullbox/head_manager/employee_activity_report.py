from __future__ import annotations

from datetime import date
from typing import Iterable

from django.db.models import Count, Max, Min, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from .employee_report import (
    FBS_REPLENISHMENT_COMPLETED,
    MOVEMENT_COMPLETED,
    OTG_ARRIVED,
    PALLETIZATION_COMPLETED,
    PLACEMENT_COMPLETED,
    SHIPPED,
    _period,
)


EMPLOYEE_ACTIVITY_CONTOUR_LABELS = {
    "fbs_handover": "FBS-передача",
    "fbs_handover_scan": "FBS-скан короба",
    "fbs_inventory_scan": "FBS-инвентаризация",
    "fbs_picking": "FBS-отбор",
    "fbs_restock_scan": "FBS-возврат отбора",
    "fbs_scan": "FBS-сканирование",
    "fbs_verification": "FBS-проверка",
    "inventory": "Инвентаризация",
    "processing": "Обработка",
    "reachtruck_inventory": "Инвентаризация ричтраком",
    "reachtruck_move": "Ричтрак",
    "receiving_cz": "Приёмка ЧЗ",
    "warehouse": "Склад",
}

_WAREHOUSE_EVENT_LABELS = {
    PLACEMENT_COMPLETED: "Размещение товара завершено",
    MOVEMENT_COMPLETED: "Перемещение товара завершено",
    OTG_ARRIVED: "Товар доставлен в OTG",
    PALLETIZATION_COMPLETED: "Паллетизация завершена",
    FBS_REPLENISHMENT_COMPLETED: "Подсорт FBS завершён",
    SHIPPED: "Складская отгрузка завершена",
}


def _blank_rollup(employee) -> dict:
    return {
        "employee_id": int(employee.pk),
        "actions": 0,
        "scans": 0,
        "scan_success": 0,
        "scan_errors": 0,
        "volume": 0,
        "first_action": None,
        "last_action": None,
        "contours": set(),
        "daily": {},
    }


def _group_rows(
    queryset,
    *,
    timestamp_field: str,
    actor_field: str,
    volume_field: str | None = None,
    result_field: str | None = None,
):
    annotations = {
        "actions": Count("id"),
        "first_action": Min(timestamp_field),
        "last_action": Max(timestamp_field),
    }
    if volume_field:
        annotations["volume"] = Sum(volume_field)
    if result_field:
        annotations["scan_success"] = Count(
            "id",
            filter=Q(**{result_field: "success"}),
        )
        annotations["scan_errors"] = Count(
            "id",
            filter=Q(**{result_field: "error"}),
        )
    return (
        queryset.annotate(
            day=TruncDate(timestamp_field, tzinfo=timezone.get_current_timezone())
        )
        .values("day", actor_field)
        .annotate(**annotations)
        .order_by()
    )


def _merge_rows(
    rollups: dict[int, dict],
    rows: Iterable[dict],
    *,
    actor_key: str,
    contour: str,
    user_to_employee: dict[int, int] | None = None,
    scans: bool = False,
) -> None:
    for row in rows:
        raw_actor_id = int(row.get(actor_key) or 0)
        employee_id = (
            user_to_employee.get(raw_actor_id)
            if user_to_employee is not None
            else raw_actor_id
        )
        if not employee_id or employee_id not in rollups:
            continue
        day = row.get("day")
        first_action = row.get("first_action")
        last_action = row.get("last_action")
        if not isinstance(day, date) or first_action is None or last_action is None:
            continue
        action_count = int(row.get("actions") or 0)
        success_count = int(row.get("scan_success") or 0) if scans else 0
        error_count = int(row.get("scan_errors") or 0) if scans else 0
        volume = int(row.get("volume") or 0)
        rollup = rollups[employee_id]
        daily = rollup["daily"].setdefault(
            day,
            {
                "date_value": day,
                "actions": 0,
                "scans": 0,
                "scan_success": 0,
                "scan_errors": 0,
                "volume": 0,
                "first_action": None,
                "last_action": None,
                "contours": set(),
            },
        )
        for target in (rollup, daily):
            target["actions"] += action_count
            target["volume"] += volume
            target["contours"].add(contour)
            if scans:
                target["scans"] += action_count
                target["scan_success"] += success_count
                target["scan_errors"] += error_count
            target["first_action"] = (
                first_action
                if target["first_action"] is None
                else min(target["first_action"], first_action)
            )
            target["last_action"] = (
                last_action
                if target["last_action"] is None
                else max(target["last_action"], last_action)
            )


def _mark_all_scans_success(rows: Iterable[dict]) -> list[dict]:
    result = list(rows)
    for row in result:
        row["scan_success"] = row.get("actions") or 0
        row["scan_errors"] = 0
    return result


def warehouse_employee_activity_rollups(
    employees: Iterable[object],
    filters: dict,
) -> dict[int, dict]:
    """Aggregate already-recorded warehouse actions without changing any workflow."""
    from fbs.models import (
        FbsHandoverBatch,
        FbsHandoverBox,
        FbsInventoryScan,
        FbsPickBatch,
        FbsPickRestockScan,
        FbsPickScanEvent,
    )
    from inventory.models import InventoryLine
    from processing_app.models import ProcessingWorkEvent
    from reachtruck.models import MoveTask
    from reachtruck_inventory.models import InventoryTask
    from receiving_cz.models import ReceivingCzUnit
    from sklad.models import WarehouseEvent

    employee_rows = list(employees)
    rollups = {int(employee.pk): _blank_rollup(employee) for employee in employee_rows}
    if not rollups:
        return rollups
    user_to_employee = {
        int(employee.user_id): int(employee.pk)
        for employee in employee_rows
        if getattr(employee, "user_id", None)
    }
    user_ids = tuple(user_to_employee)
    employee_ids = tuple(rollups)
    _date_from, _date_to, period_start, period_end = _period(filters)

    if user_ids:
        scan_sources = (
            (
                FbsPickScanEvent.objects.filter(
                    created_by_id__in=user_ids,
                    created_at__gte=period_start,
                    created_at__lt=period_end,
                ),
                "created_at",
                "created_by_id",
                "fbs_scan",
            ),
            (
                FbsPickRestockScan.objects.filter(
                    created_by_id__in=user_ids,
                    created_at__gte=period_start,
                    created_at__lt=period_end,
                ),
                "created_at",
                "created_by_id",
                "fbs_restock_scan",
            ),
        )
        for queryset, timestamp_field, actor_field, contour in scan_sources:
            _merge_rows(
                rollups,
                _group_rows(
                    queryset,
                    timestamp_field=timestamp_field,
                    actor_field=actor_field,
                    result_field="result",
                ),
                actor_key=actor_field,
                contour=contour,
                user_to_employee=user_to_employee,
                scans=True,
            )

        inventory_scan_rows = _mark_all_scans_success(
            _group_rows(
                FbsInventoryScan.objects.filter(
                    counted_by_id__in=user_ids,
                    created_at__gte=period_start,
                    created_at__lt=period_end,
                ),
                timestamp_field="created_at",
                actor_field="counted_by_id",
                volume_field="qty",
            )
        )
        _merge_rows(
            rollups,
            inventory_scan_rows,
            actor_key="counted_by_id",
            contour="fbs_inventory_scan",
            user_to_employee=user_to_employee,
            scans=True,
        )

        handover_scan_rows = _mark_all_scans_success(
            _group_rows(
                FbsHandoverBox.objects.filter(
                    scanned_by_id__in=user_ids,
                    scanned_at__gte=period_start,
                    scanned_at__lt=period_end,
                ),
                timestamp_field="scanned_at",
                actor_field="scanned_by_id",
            )
        )
        _merge_rows(
            rollups,
            handover_scan_rows,
            actor_key="scanned_by_id",
            contour="fbs_handover_scan",
            user_to_employee=user_to_employee,
            scans=True,
        )

        receiving_rows = _mark_all_scans_success(
            _group_rows(
                ReceivingCzUnit.objects.filter(
                    accepted_by_id__in=user_ids,
                    accepted_at__gte=period_start,
                    accepted_at__lt=period_end,
                ),
                timestamp_field="accepted_at",
                actor_field="accepted_by_id",
            )
        )
        for row in receiving_rows:
            row["volume"] = row.get("actions") or 0
        _merge_rows(
            rollups,
            receiving_rows,
            actor_key="accepted_by_id",
            contour="receiving_cz",
            user_to_employee=user_to_employee,
            scans=True,
        )

        user_sources = (
            (
                WarehouseEvent.objects.filter(
                    performed_by_id__in=user_ids,
                    occurred_at__gte=period_start,
                    occurred_at__lt=period_end,
                ),
                "occurred_at",
                "performed_by_id",
                "qty",
                "warehouse",
            ),
            (
                InventoryLine.objects.filter(
                    counted_by_id__in=user_ids,
                    counted_at__gte=period_start,
                    counted_at__lt=period_end,
                ),
                "counted_at",
                "counted_by_id",
                "actual_qty",
                "inventory",
            ),
            (
                MoveTask.objects.filter(
                    status=MoveTask.STATUS_DONE,
                    assigned_to_id__in=user_ids,
                    completed_at__gte=period_start,
                    completed_at__lt=period_end,
                ),
                "completed_at",
                "assigned_to_id",
                "qty_done",
                "reachtruck_move",
            ),
            (
                InventoryTask.objects.filter(
                    status=InventoryTask.STATUS_COMPLETED,
                    assigned_to_id__in=user_ids,
                    completed_at__gte=period_start,
                    completed_at__lt=period_end,
                ),
                "completed_at",
                "assigned_to_id",
                None,
                "reachtruck_inventory",
            ),
            (
                FbsPickBatch.objects.filter(
                    assigned_to_id__in=user_ids,
                    picking_completed_at__gte=period_start,
                    picking_completed_at__lt=period_end,
                ),
                "picking_completed_at",
                "assigned_to_id",
                "picked_qty",
                "fbs_picking",
            ),
            (
                FbsPickBatch.objects.filter(
                    verification_assigned_to_id__in=user_ids,
                    completed_at__gte=period_start,
                    completed_at__lt=period_end,
                ),
                "completed_at",
                "verification_assigned_to_id",
                "picked_qty",
                "fbs_verification",
            ),
            (
                FbsHandoverBatch.objects.filter(
                    dispatched_by_id__in=user_ids,
                    dispatched_at__gte=period_start,
                    dispatched_at__lt=period_end,
                ),
                "dispatched_at",
                "dispatched_by_id",
                None,
                "fbs_handover",
            ),
        )
        for queryset, timestamp_field, actor_field, volume_field, contour in user_sources:
            _merge_rows(
                rollups,
                _group_rows(
                    queryset,
                    timestamp_field=timestamp_field,
                    actor_field=actor_field,
                    volume_field=volume_field,
                ),
                actor_key=actor_field,
                contour=contour,
                user_to_employee=user_to_employee,
            )

    _merge_rows(
        rollups,
        _group_rows(
            ProcessingWorkEvent.objects.filter(
                employee_id__in=employee_ids,
                occurred_at__gte=period_start,
                occurred_at__lt=period_end,
            ),
            timestamp_field="occurred_at",
            actor_field="employee_id",
            volume_field="units",
        ),
        actor_key="employee_id",
        contour="processing",
    )

    for rollup in rollups.values():
        rollup["active_days"] = len(rollup["daily"])
        rollup["contours"] = tuple(sorted(rollup["contours"]))
        daily_rows = []
        for daily in rollup["daily"].values():
            daily["contours"] = tuple(sorted(daily["contours"]))
            daily_rows.append(daily)
        daily_rows.sort(key=lambda item: item["date_value"], reverse=True)
        rollup["daily"] = daily_rows
    return rollups


def _short_text(value, limit: int = 180) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1].rstrip()}…"


def warehouse_employee_activity_timeline(
    employee,
    filters: dict,
    *,
    limit: int = 500,
) -> tuple[list[dict], bool]:
    """Return newest recorded actions for one employee; raw scan values stay hidden."""
    from fbs.models import (
        FbsHandoverBatch,
        FbsHandoverBox,
        FbsInventoryScan,
        FbsPickBatch,
        FbsPickRestockScan,
        FbsPickScanEvent,
    )
    from inventory.models import InventoryLine
    from processing_app.models import ProcessingWorkEvent
    from reachtruck.models import MoveTask
    from reachtruck_inventory.models import InventoryTask
    from receiving_cz.models import ReceivingCzUnit
    from sklad.models import WarehouseEvent

    _date_from, _date_to, period_start, period_end = _period(filters)
    per_source_limit = max(1, int(limit)) + 1
    entries: list[dict] = []

    def add(
        *,
        occurred_at,
        contour: str,
        action: str,
        result: str = "Выполнено",
        result_tone: str = "success",
        amount: str = "—",
        object_label: str = "—",
        details: str = "",
    ) -> None:
        if occurred_at is None:
            return
        entries.append(
            {
                "occurred_at": occurred_at,
                "contour": contour,
                "contour_label": EMPLOYEE_ACTIVITY_CONTOUR_LABELS.get(contour, contour),
                "action": _short_text(action),
                "result": result,
                "result_tone": result_tone,
                "amount": amount or "—",
                "object": _short_text(object_label) or "—",
                "details": _short_text(details),
            }
        )

    user_id = int(getattr(employee, "user_id", 0) or 0)
    if user_id:
        scan_models = (
            (FbsPickScanEvent, "batch_id", "fbs_scan"),
            (FbsPickRestockScan, "request_id", "fbs_restock_scan"),
        )
        for model, object_field, contour in scan_models:
            stage_labels = dict(model.STAGE_CHOICES)
            result_labels = dict(model.RESULT_CHOICES)
            rows = model.objects.filter(
                created_by_id=user_id,
                created_at__gte=period_start,
                created_at__lt=period_end,
            ).values(
                "id",
                "created_at",
                object_field,
                "stage",
                "result",
                "quantity_after",
                "message",
            ).order_by("-created_at", "-id")[:per_source_limit]
            for row in rows:
                result_value = str(row.get("result") or "")
                quantity_after = row.get("quantity_after")
                add(
                    occurred_at=row["created_at"],
                    contour=contour,
                    action=stage_labels.get(row.get("stage"), row.get("stage") or "Сканирование"),
                    result=result_labels.get(result_value, result_value or "—"),
                    result_tone="error" if result_value == "error" else "success",
                    amount=(
                        f"После скана: {quantity_after}"
                        if quantity_after is not None
                        else "1 скан"
                    ),
                    object_label=f"#{row.get(object_field) or '—'}",
                    details=row.get("message") or "",
                )

        rows = FbsInventoryScan.objects.filter(
            counted_by_id=user_id,
            created_at__gte=period_start,
            created_at__lt=period_end,
        ).values("id", "created_at", "line_id", "count_round", "qty").order_by(
            "-created_at", "-id"
        )[:per_source_limit]
        round_labels = dict(FbsInventoryScan.ROUND_CHOICES)
        for row in rows:
            add(
                occurred_at=row["created_at"],
                contour="fbs_inventory_scan",
                action=f"Пересчёт, {str(round_labels.get(row['count_round'], row['count_round'])).lower()}",
                amount=f"{int(row.get('qty') or 0)} шт.",
                object_label=f"Строка FBS-инвентаризации #{row['line_id']}",
            )

        rows = FbsHandoverBox.objects.filter(
            scanned_by_id=user_id,
            scanned_at__gte=period_start,
            scanned_at__lt=period_end,
        ).values("id", "batch_id", "scanned_at").order_by("-scanned_at", "-id")[:per_source_limit]
        for row in rows:
            add(
                occurred_at=row["scanned_at"],
                contour="fbs_handover_scan",
                action="Короб поставки просканирован",
                amount="1 скан",
                object_label=f"Поставка #{row['batch_id']}, короб #{row['id']}",
            )

        rows = ReceivingCzUnit.objects.filter(
            accepted_by_id=user_id,
            accepted_at__gte=period_start,
            accepted_at__lt=period_end,
        ).values(
            "id", "accepted_at", "order_id", "sku_code", "name", "box_code"
        ).order_by("-accepted_at", "-id")[:per_source_limit]
        for row in rows:
            add(
                occurred_at=row["accepted_at"],
                contour="receiving_cz",
                action="Код маркировки принят",
                amount="1 скан",
                object_label=f"{row.get('sku_code') or 'SKU не указан'} · заказ {row.get('order_id') or '—'}",
                details=f"{row.get('name') or ''} · короб {row.get('box_code') or '—'}",
            )

        rows = WarehouseEvent.objects.filter(
            performed_by_id=user_id,
            occurred_at__gte=period_start,
            occurred_at__lt=period_end,
        ).values(
            "id", "occurred_at", "event_type", "qty", "stock_context_type",
            "stock_context_id", "container_id", "from_zone_code", "to_zone_code",
        ).order_by("-occurred_at", "-id")[:per_source_limit]
        for row in rows:
            context = " ".join(
                value
                for value in (
                    str(row.get("stock_context_type") or "").strip(),
                    str(row.get("stock_context_id") or "").strip(),
                )
                if value
            )
            direction = " → ".join(
                value
                for value in (
                    str(row.get("from_zone_code") or "").strip(),
                    str(row.get("to_zone_code") or "").strip(),
                )
                if value
            )
            add(
                occurred_at=row["occurred_at"],
                contour="warehouse",
                action=_WAREHOUSE_EVENT_LABELS.get(
                    row.get("event_type"),
                    str(row.get("event_type") or "Складская операция").replace("_", " "),
                ),
                amount=f"{int(row.get('qty') or 0)} шт.",
                object_label=context or f"Складское событие #{row['id']}",
                details=direction or (f"Контейнер #{row['container_id']}" if row.get("container_id") else ""),
            )

        rows = InventoryLine.objects.filter(
            counted_by_id=user_id,
            counted_at__gte=period_start,
            counted_at__lt=period_end,
        ).values(
            "id", "counted_at", "inventory_id", "sku_code", "name", "actual_qty", "location_id"
        ).order_by("-counted_at", "-id")[:per_source_limit]
        for row in rows:
            add(
                occurred_at=row["counted_at"],
                contour="inventory",
                action="Строка инвентаризации пересчитана",
                amount=f"{int(row.get('actual_qty') or 0)} шт.",
                object_label=f"{row.get('sku_code') or 'SKU не указан'} · инвентаризация #{row['inventory_id']}",
                details=f"{row.get('name') or ''} · место #{row.get('location_id') or '—'}",
            )

        rows = MoveTask.objects.filter(
            status=MoveTask.STATUS_DONE,
            assigned_to_id=user_id,
            completed_at__gte=period_start,
            completed_at__lt=period_end,
        ).values(
            "id", "completed_at", "pallet_code", "qty_done", "from_zone", "to_zone"
        ).order_by("-completed_at", "-id")[:per_source_limit]
        for row in rows:
            add(
                occurred_at=row["completed_at"],
                contour="reachtruck_move",
                action="Задание перемещения завершено",
                amount=f"{int(row.get('qty_done') or 0)} шт.",
                object_label=f"Паллета {row.get('pallet_code') or '—'}",
                details=f"{row.get('from_zone') or '—'} → {row.get('to_zone') or '—'}",
            )

        rows = InventoryTask.objects.filter(
            status=InventoryTask.STATUS_COMPLETED,
            assigned_to_id=user_id,
            completed_at__gte=period_start,
            completed_at__lt=period_end,
        ).values(
            "id", "completed_at", "inventory_id", "location_id", "actual_box_count"
        ).order_by("-completed_at", "-id")[:per_source_limit]
        for row in rows:
            box_count = row.get("actual_box_count")
            add(
                occurred_at=row["completed_at"],
                contour="reachtruck_inventory",
                action="Задание инвентаризации завершено",
                amount=f"{int(box_count)} коробов" if box_count is not None else "—",
                object_label=f"Инвентаризация #{row['inventory_id']}, место #{row['location_id']}",
            )

        pick_rows = FbsPickBatch.objects.filter(
            assigned_to_id=user_id,
            picking_completed_at__gte=period_start,
            picking_completed_at__lt=period_end,
        ).values("id", "picking_completed_at", "picked_qty").order_by(
            "-picking_completed_at", "-id"
        )[:per_source_limit]
        for row in pick_rows:
            add(
                occurred_at=row["picking_completed_at"],
                contour="fbs_picking",
                action="Отбор волны завершён",
                amount=f"{int(row.get('picked_qty') or 0)} шт.",
                object_label=f"Волна #{row['id']}",
            )

        verification_rows = FbsPickBatch.objects.filter(
            verification_assigned_to_id=user_id,
            completed_at__gte=period_start,
            completed_at__lt=period_end,
        ).values("id", "completed_at", "picked_qty").order_by(
            "-completed_at", "-id"
        )[:per_source_limit]
        for row in verification_rows:
            add(
                occurred_at=row["completed_at"],
                contour="fbs_verification",
                action="Проверка волны завершена",
                amount=f"{int(row.get('picked_qty') or 0)} шт.",
                object_label=f"Волна #{row['id']}",
            )

        handover_rows = FbsHandoverBatch.objects.filter(
            dispatched_by_id=user_id,
            dispatched_at__gte=period_start,
            dispatched_at__lt=period_end,
        ).values("id", "dispatched_at", "external_name", "external_supply_id").order_by(
            "-dispatched_at", "-id"
        )[:per_source_limit]
        for row in handover_rows:
            add(
                occurred_at=row["dispatched_at"],
                contour="fbs_handover",
                action="Поставка передана водителю",
                object_label=row.get("external_name") or f"Поставка #{row['id']}",
                details=(f"ID маркетплейса: {row['external_supply_id']}" if row.get("external_supply_id") else ""),
            )

    processing_rows = ProcessingWorkEvent.objects.filter(
        employee_id=employee.pk,
        occurred_at__gte=period_start,
        occurred_at__lt=period_end,
    ).values(
        "id", "occurred_at", "operation_type", "operation_label", "order_id",
        "container_code", "units", "boxes",
    ).order_by("-occurred_at", "-id")[:per_source_limit]
    processing_labels = dict(ProcessingWorkEvent.TYPE_CHOICES)
    for row in processing_rows:
        amounts = []
        if row.get("units"):
            amounts.append(f"{int(row['units'])} шт.")
        if row.get("boxes"):
            amounts.append(f"{int(row['boxes'])} коробов")
        add(
            occurred_at=row["occurred_at"],
            contour="processing",
            action=row.get("operation_label") or processing_labels.get(
                row.get("operation_type"), row.get("operation_type") or "Операция обработки"
            ),
            amount=", ".join(amounts) or "—",
            object_label=f"Заказ {row.get('order_id') or '—'}",
            details=f"Контейнер {row.get('container_code')}" if row.get("container_code") else "",
        )

    entries.sort(key=lambda item: item["occurred_at"], reverse=True)
    was_truncated = len(entries) > limit
    return entries[:limit], was_truncated
