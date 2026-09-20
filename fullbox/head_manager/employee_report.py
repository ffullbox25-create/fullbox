from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from django.contrib.auth import get_user_model
from django.db.models import Count, Max, Min, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone


MetricPoint = tuple[date, object, str, int | float]
ActivityPoint = tuple[date, int, str, datetime, datetime]

PLACEMENT_COMPLETED = "placement_completed"
MOVEMENT_COMPLETED = "movement_completed"
OTG_ARRIVED = "otg_arrived"
PALLETIZATION_COMPLETED = "palletization_completed"
FBS_REPLENISHMENT_COMPLETED = "fbs_replenishment_completed"
SHIPPED = "shipped"

_WAREHOUSE_EVENT_CONTOURS = {
    FBS_REPLENISHMENT_COMPLETED: "fbs_replenishment",
    PLACEMENT_COMPLETED: "placement",
    MOVEMENT_COMPLETED: "movement",
    OTG_ARRIVED: "otg",
    PALLETIZATION_COMPLETED: "palletization",
    SHIPPED: "shipping",
}


def _report_helpers():
    # Lazy import keeps this module safe to import from head_manager.web_ui later.
    from .web_ui import (
        _head_manager_fbs_employee_report_date,
        _head_manager_fbs_employee_report_period,
        _head_manager_fbs_employee_report_rows,
        _head_manager_fbs_employee_user_filter,
    )

    return (
        _head_manager_fbs_employee_report_date,
        _head_manager_fbs_employee_report_period,
        _head_manager_fbs_employee_report_rows,
        _head_manager_fbs_employee_user_filter,
    )


def _period(filters: dict) -> tuple[date, date, object, object]:
    parse_date, report_period, _, _ = _report_helpers()
    date_from = parse_date(filters.get("date_from"))
    date_to = parse_date(filters.get("date_to"))
    today = timezone.localdate()
    if date_from is None:
        date_from = today
    if date_to is None:
        date_to = today
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    period_start, period_end = report_period(date_from, date_to)
    return date_from, date_to, period_start, period_end


def _employee_filter(field: str, filters: dict) -> Q:
    _, _, _, user_filter = _report_helpers()
    return user_filter(field, str(filters.get("employee") or "").strip())


def _metric_points(
    grouped_rows: Iterable[dict],
    *,
    user_id_key: str,
    metrics: tuple[tuple[str, str], ...],
) -> list[MetricPoint]:
    rows = list(grouped_rows)
    user_ids = {int(row.get(user_id_key) or 0) for row in rows}
    user_ids.discard(0)
    users = (
        get_user_model()
        .objects.select_related("employee_profile")
        .in_bulk(user_ids)
    )
    points: list[MetricPoint] = []
    for row in rows:
        user = users.get(int(row.get(user_id_key) or 0))
        day = row.get("day")
        if user is None or not isinstance(day, date):
            continue
        for annotation, metric in metrics:
            value = row.get(annotation)
            points.append((day, user, metric, value if value is not None else 0))
    return points


def fbs_metric_points(filters: dict) -> list[MetricPoint]:
    _, _, report_rows, _ = _report_helpers()
    date_from, date_to, _period_start, _period_end = _period(filters)
    normalized_filters = {
        **filters,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
    }
    rows, _summary = report_rows(normalized_filters)
    user_ids = {int(row.get("user_id") or 0) for row in rows}
    user_ids.discard(0)
    users = (
        get_user_model()
        .objects.select_related("employee_profile")
        .in_bulk(user_ids)
    )
    metric_names = (
        "pick_waves",
        "pick_orders",
        "pick_units",
        "avg_pick_minutes",
        "verified_waves",
        "verified_orders",
        "verified_units",
        "shipments",
        "shipment_boxes",
        "shipment_orders",
        "shipment_units",
    )
    points: list[MetricPoint] = []
    for row in rows:
        user = users.get(int(row.get("user_id") or 0))
        day = row.get("date_value")
        if user is None or not isinstance(day, date):
            continue
        for metric in metric_names:
            points.append((day, user, metric, row.get(metric, 0)))
    return points


def fbs_quality_metric_points(filters: dict) -> list[MetricPoint]:
    from fbs.models import FbsPickScanEvent

    _date_from, _date_to, period_start, period_end = _period(filters)
    queryset = (
        FbsPickScanEvent.objects.filter(
            created_by__isnull=False,
            created_at__gte=period_start,
            created_at__lt=period_end,
            stage__in=(
                FbsPickScanEvent.STAGE_VERIFY_ITEM,
                FbsPickScanEvent.STAGE_VERIFY_MARKING,
            ),
        )
        .filter(_employee_filter("created_by", filters))
        .annotate(day=TruncDate("created_at", tzinfo=timezone.get_current_timezone()))
        .values("day", "created_by_id")
        .annotate(
            kiz_scans_success=Count(
                "id",
                filter=Q(
                    stage=FbsPickScanEvent.STAGE_VERIFY_MARKING,
                    result=FbsPickScanEvent.RESULT_SUCCESS,
                ),
            ),
            kiz_scan_errors=Count(
                "id",
                filter=Q(
                    stage=FbsPickScanEvent.STAGE_VERIFY_MARKING,
                    result=FbsPickScanEvent.RESULT_ERROR,
                ),
            ),
            barcode_scan_errors=Count(
                "id",
                filter=Q(
                    stage=FbsPickScanEvent.STAGE_VERIFY_ITEM,
                    result=FbsPickScanEvent.RESULT_ERROR,
                ),
            ),
        )
        .order_by()
    )
    return _metric_points(
        queryset,
        user_id_key="created_by_id",
        metrics=(
            ("kiz_scans_success", "kiz_scans_success"),
            ("kiz_scan_errors", "kiz_scan_errors"),
            ("barcode_scan_errors", "barcode_scan_errors"),
        ),
    )


def receiving_cz_metric_points(filters: dict) -> list[MetricPoint]:
    from receiving_cz.models import ReceivingCzUnit

    _date_from, _date_to, period_start, period_end = _period(filters)
    queryset = (
        ReceivingCzUnit.objects.filter(
            accepted_by__isnull=False,
            accepted_at__gte=period_start,
            accepted_at__lt=period_end,
        )
        .filter(_employee_filter("accepted_by", filters))
        .annotate(day=TruncDate("accepted_at", tzinfo=timezone.get_current_timezone()))
        .values("day", "accepted_by_id")
        .annotate(receiving_cz_units=Count("id"))
        .order_by()
    )
    return _metric_points(
        queryset,
        user_id_key="accepted_by_id",
        metrics=(("receiving_cz_units", "receiving_cz_units"),),
    )


def _processing_employee_filter(filters: dict) -> Q:
    value = str(filters.get("employee") or "").strip()
    if not value:
        return Q()
    return (
        Q(employee__full_name__icontains=value)
        | Q(employee__user__username__icontains=value)
        | Q(employee__user__first_name__icontains=value)
        | Q(employee__user__last_name__icontains=value)
    )


def processing_metric_points(filters: dict) -> list[MetricPoint]:
    from processing_app.models import ProcessingWorkEvent
    from employees.models import Employee

    _date_from, _date_to, period_start, period_end = _period(filters)
    rows = list(
        ProcessingWorkEvent.objects.filter(
            employee__isnull=False,
            occurred_at__gte=period_start,
            occurred_at__lt=period_end,
        )
        .filter(_processing_employee_filter(filters))
        .annotate(day=TruncDate("occurred_at", tzinfo=timezone.get_current_timezone()))
        .values("day", "employee_id")
        .annotate(
            processing_operations=Count(
                "id",
                filter=Q(
                    operation_type=ProcessingWorkEvent.TYPE_OPERATION_COMPLETED,
                ),
            ),
            processing_units=Sum(
                "units",
                filter=Q(
                    operation_type=ProcessingWorkEvent.TYPE_OPERATION_COMPLETED,
                ),
            ),
            processing_boxes=Sum(
                "boxes",
                filter=Q(operation_type=ProcessingWorkEvent.TYPE_BOX_FORMED),
            ),
            processing_boxed_units=Sum(
                "units",
                filter=Q(operation_type=ProcessingWorkEvent.TYPE_BOX_FORMED),
            ),
        )
        .order_by()
    )
    employees = Employee.objects.select_related("user").in_bulk(
        {int(row.get("employee_id") or 0) for row in rows if row.get("employee_id")}
    )
    points: list[MetricPoint] = []
    metrics = (
        "processing_operations",
        "processing_units",
        "processing_boxes",
        "processing_boxed_units",
    )
    for row in rows:
        employee = employees.get(int(row.get("employee_id") or 0))
        day = row.get("day")
        if employee is None or not isinstance(day, date):
            continue
        for metric in metrics:
            points.append((day, employee, metric, row.get(metric) or 0))
    return points


def reachtruck_move_metric_points(filters: dict) -> list[MetricPoint]:
    from reachtruck.models import MoveTask

    _date_from, _date_to, period_start, period_end = _period(filters)
    queryset = (
        MoveTask.objects.filter(
            status=MoveTask.STATUS_DONE,
            assigned_to__isnull=False,
            completed_at__gte=period_start,
            completed_at__lt=period_end,
        )
        .filter(_employee_filter("assigned_to", filters))
        .annotate(day=TruncDate("completed_at", tzinfo=timezone.get_current_timezone()))
        .values("day", "assigned_to_id")
        .annotate(
            reachtruck_tasks=Count("id"),
            reachtruck_units=Sum("qty_done"),
        )
        .order_by()
    )
    return _metric_points(
        queryset,
        user_id_key="assigned_to_id",
        metrics=(
            ("reachtruck_tasks", "reachtruck_tasks"),
            ("reachtruck_units", "reachtruck_units"),
        ),
    )


_WAREHOUSE_EVENT_GROUPS = {
    "fbs_replenishment": {
        "event_type": FBS_REPLENISHMENT_COMPLETED,
        "annotations": {
            "fbs_replenishment_tasks": Count(
                "operation_task_id",
                distinct=True,
                filter=Q(event_type=FBS_REPLENISHMENT_COMPLETED),
            ),
            "fbs_replenishment_units": Sum(
                "qty",
                filter=Q(event_type=FBS_REPLENISHMENT_COMPLETED),
            ),
        },
    },
    "placement": {
        "event_type": PLACEMENT_COMPLETED,
        "annotations": {
            "placement_positions": Count("id", filter=Q(event_type=PLACEMENT_COMPLETED)),
            "placement_units": Sum("qty", filter=Q(event_type=PLACEMENT_COMPLETED)),
        },
    },
    "movement": {
        "event_type": MOVEMENT_COMPLETED,
        "annotations": {
            "movement_positions": Count("id", filter=Q(event_type=MOVEMENT_COMPLETED)),
            "movement_units": Sum("qty", filter=Q(event_type=MOVEMENT_COMPLETED)),
        },
    },
    "otg": {
        "event_type": OTG_ARRIVED,
        "annotations": {
            "otg_positions": Count("id", filter=Q(event_type=OTG_ARRIVED)),
            "otg_units": Sum("qty", filter=Q(event_type=OTG_ARRIVED)),
        },
    },
    "palletization": {
        "event_type": PALLETIZATION_COMPLETED,
        "annotations": {
            "palletization_positions": Count(
                "id",
                filter=Q(event_type=PALLETIZATION_COMPLETED),
            ),
            "palletization_units": Sum(
                "qty",
                filter=Q(event_type=PALLETIZATION_COMPLETED),
            ),
        },
    },
    "shipping": {
        "event_type": SHIPPED,
        "annotations": {
            "shipping_orders": Count(
                "stock_context_id",
                distinct=True,
                filter=Q(event_type=SHIPPED) & ~Q(stock_context_id=""),
            ),
            "shipping_positions": Count("id", filter=Q(event_type=SHIPPED)),
            "shipping_units": Sum("qty", filter=Q(event_type=SHIPPED)),
            "shipping_containers": Count(
                "container_id",
                distinct=True,
                filter=Q(event_type=SHIPPED),
            ),
        },
    },
}


def _warehouse_event_metric_points(
    filters: dict,
    groups: tuple[str, ...],
) -> list[MetricPoint]:
    from sklad.models import WarehouseEvent

    _date_from, _date_to, period_start, period_end = _period(filters)
    event_types: list[str] = []
    annotations = {}
    metrics: list[tuple[str, str]] = []
    for group in groups:
        config = _WAREHOUSE_EVENT_GROUPS[group]
        event_types.append(config["event_type"])
        annotations.update(config["annotations"])
        metrics.extend((name, name) for name in config["annotations"])
    queryset = (
        WarehouseEvent.objects.filter(
            performed_by__isnull=False,
            occurred_at__gte=period_start,
            occurred_at__lt=period_end,
            event_type__in=event_types,
        )
        .filter(_employee_filter("performed_by", filters))
        .annotate(day=TruncDate("occurred_at", tzinfo=timezone.get_current_timezone()))
        .values("day", "performed_by_id")
        .annotate(**annotations)
        .order_by()
    )
    return _metric_points(
        queryset,
        user_id_key="performed_by_id",
        metrics=tuple(metrics),
    )


def fbs_replenishment_metric_points(filters: dict) -> list[MetricPoint]:
    return _warehouse_event_metric_points(filters, ("fbs_replenishment",))


def warehouse_placement_metric_points(filters: dict) -> list[MetricPoint]:
    return _warehouse_event_metric_points(filters, ("placement",))


def warehouse_movement_metric_points(filters: dict) -> list[MetricPoint]:
    return _warehouse_event_metric_points(filters, ("movement",))


def warehouse_otg_metric_points(filters: dict) -> list[MetricPoint]:
    return _warehouse_event_metric_points(filters, ("otg",))


def warehouse_palletization_metric_points(filters: dict) -> list[MetricPoint]:
    return _warehouse_event_metric_points(filters, ("palletization",))


def non_fbs_shipping_metric_points(filters: dict) -> list[MetricPoint]:
    return _warehouse_event_metric_points(filters, ("shipping",))


def inventory_metric_points(filters: dict) -> list[MetricPoint]:
    from inventory.models import InventoryLine

    _date_from, _date_to, period_start, period_end = _period(filters)
    queryset = (
        InventoryLine.objects.filter(
            counted_by__isnull=False,
            counted_at__gte=period_start,
            counted_at__lt=period_end,
        )
        .filter(_employee_filter("counted_by", filters))
        .annotate(day=TruncDate("counted_at", tzinfo=timezone.get_current_timezone()))
        .values("day", "counted_by_id")
        .annotate(
            inventory_lines=Count("id"),
            inventory_units=Sum("actual_qty"),
        )
        .order_by()
    )
    return _metric_points(
        queryset,
        user_id_key="counted_by_id",
        metrics=(
            ("inventory_lines", "inventory_lines"),
            ("inventory_units", "inventory_units"),
        ),
    )


def reachtruck_inventory_metric_points(filters: dict) -> list[MetricPoint]:
    from reachtruck_inventory.models import InventoryTask

    _date_from, _date_to, period_start, period_end = _period(filters)
    queryset = (
        InventoryTask.objects.filter(
            status=InventoryTask.STATUS_COMPLETED,
            assigned_to__isnull=False,
            completed_at__gte=period_start,
            completed_at__lt=period_end,
        )
        .filter(_employee_filter("assigned_to", filters))
        .annotate(day=TruncDate("completed_at", tzinfo=timezone.get_current_timezone()))
        .values("day", "assigned_to_id")
        .annotate(reachtruck_inventory_tasks=Count("id"))
        .order_by()
    )
    return _metric_points(
        queryset,
        user_id_key="assigned_to_id",
        metrics=(("reachtruck_inventory_tasks", "reachtruck_inventory_tasks"),),
    )


def warehouse_employee_metric_points(filters: dict) -> list[MetricPoint]:
    points: list[MetricPoint] = []
    for collector in (
        fbs_metric_points,
        fbs_quality_metric_points,
        receiving_cz_metric_points,
        processing_metric_points,
        reachtruck_move_metric_points,
    ):
        points.extend(collector(filters))
    points.extend(_warehouse_event_metric_points(filters, tuple(_WAREHOUSE_EVENT_GROUPS)))
    points.extend(inventory_metric_points(filters))
    points.extend(reachtruck_inventory_metric_points(filters))
    return points


def _activity_rows(
    queryset,
    *,
    timestamp_field: str,
    user_id_field: str,
    contour: str,
) -> list[ActivityPoint]:
    rows = (
        queryset.annotate(
            day=TruncDate(timestamp_field, tzinfo=timezone.get_current_timezone())
        )
        .values("day", user_id_field)
        .annotate(
            first_action=Min(timestamp_field),
            last_action=Max(timestamp_field),
        )
        .order_by()
    )
    return [
        (
            row["day"],
            int(row[user_id_field]),
            contour,
            row["first_action"],
            row["last_action"],
        )
        for row in rows
        if row.get("day")
        and row.get(user_id_field)
        and row.get("first_action")
        and row.get("last_action")
    ]


def _fbs_activity_points(filters: dict) -> list[ActivityPoint]:
    from fbs.models import FbsHandoverBatch, FbsPickBatch, FbsPickScanEvent

    _date_from, _date_to, period_start, period_end = _period(filters)
    points: list[ActivityPoint] = []
    picking = FbsPickBatch.objects.filter(
        assigned_to__isnull=False,
        picking_completed_at__gte=period_start,
        picking_completed_at__lt=period_end,
    ).filter(_employee_filter("assigned_to", filters))
    points.extend(
        _activity_rows(
            picking,
            timestamp_field="picking_completed_at",
            user_id_field="assigned_to_id",
            contour="fbs_picking",
        )
    )
    verification = FbsPickBatch.objects.filter(
        verification_assigned_to__isnull=False,
        completed_at__gte=period_start,
        completed_at__lt=period_end,
    ).filter(_employee_filter("verification_assigned_to", filters))
    points.extend(
        _activity_rows(
            verification,
            timestamp_field="completed_at",
            user_id_field="verification_assigned_to_id",
            contour="fbs_verification",
        )
    )
    handover = FbsHandoverBatch.objects.filter(
        dispatched_by__isnull=False,
        dispatched_at__gte=period_start,
        dispatched_at__lt=period_end,
    ).filter(_employee_filter("dispatched_by", filters))
    points.extend(
        _activity_rows(
            handover,
            timestamp_field="dispatched_at",
            user_id_field="dispatched_by_id",
            contour="fbs_handover",
        )
    )
    quality = FbsPickScanEvent.objects.filter(
        created_by__isnull=False,
        created_at__gte=period_start,
        created_at__lt=period_end,
        stage__in=(
            FbsPickScanEvent.STAGE_VERIFY_ITEM,
            FbsPickScanEvent.STAGE_VERIFY_MARKING,
        ),
    ).filter(_employee_filter("created_by", filters))
    points.extend(
        _activity_rows(
            quality,
            timestamp_field="created_at",
            user_id_field="created_by_id",
            contour="fbs_quality",
        )
    )
    return points


def _receiving_activity_points(filters: dict) -> list[ActivityPoint]:
    from receiving_cz.models import ReceivingCzUnit

    _date_from, _date_to, period_start, period_end = _period(filters)
    queryset = ReceivingCzUnit.objects.filter(
        accepted_by__isnull=False,
        accepted_at__gte=period_start,
        accepted_at__lt=period_end,
    ).filter(_employee_filter("accepted_by", filters))
    return _activity_rows(
        queryset,
        timestamp_field="accepted_at",
        user_id_field="accepted_by_id",
        contour="receiving_cz",
    )


def _reachtruck_activity_points(filters: dict) -> list[ActivityPoint]:
    from reachtruck.models import MoveTask

    _date_from, _date_to, period_start, period_end = _period(filters)
    queryset = MoveTask.objects.filter(
        status=MoveTask.STATUS_DONE,
        assigned_to__isnull=False,
        completed_at__gte=period_start,
        completed_at__lt=period_end,
    ).filter(_employee_filter("assigned_to", filters))
    return _activity_rows(
        queryset,
        timestamp_field="completed_at",
        user_id_field="assigned_to_id",
        contour="reachtruck_move",
    )


def _warehouse_event_activity_points(filters: dict) -> list[ActivityPoint]:
    from sklad.models import WarehouseEvent

    _date_from, _date_to, period_start, period_end = _period(filters)
    rows = (
        WarehouseEvent.objects.filter(
            performed_by__isnull=False,
            occurred_at__gte=period_start,
            occurred_at__lt=period_end,
            event_type__in=tuple(_WAREHOUSE_EVENT_CONTOURS),
        )
        .filter(_employee_filter("performed_by", filters))
        .annotate(day=TruncDate("occurred_at", tzinfo=timezone.get_current_timezone()))
        .values("day", "performed_by_id", "event_type")
        .annotate(
            first_action=Min("occurred_at"),
            last_action=Max("occurred_at"),
        )
        .order_by()
    )
    return [
        (
            row["day"],
            int(row["performed_by_id"]),
            _WAREHOUSE_EVENT_CONTOURS[row["event_type"]],
            row["first_action"],
            row["last_action"],
        )
        for row in rows
        if row.get("day")
        and row.get("performed_by_id")
        and row.get("first_action")
        and row.get("last_action")
    ]


def _inventory_activity_points(filters: dict) -> list[ActivityPoint]:
    from inventory.models import InventoryLine

    _date_from, _date_to, period_start, period_end = _period(filters)
    queryset = InventoryLine.objects.filter(
        counted_by__isnull=False,
        counted_at__gte=period_start,
        counted_at__lt=period_end,
    ).filter(_employee_filter("counted_by", filters))
    return _activity_rows(
        queryset,
        timestamp_field="counted_at",
        user_id_field="counted_by_id",
        contour="inventory",
    )


def _reachtruck_inventory_activity_points(filters: dict) -> list[ActivityPoint]:
    from reachtruck_inventory.models import InventoryTask

    _date_from, _date_to, period_start, period_end = _period(filters)
    queryset = InventoryTask.objects.filter(
        status=InventoryTask.STATUS_COMPLETED,
        assigned_to__isnull=False,
        completed_at__gte=period_start,
        completed_at__lt=period_end,
    ).filter(_employee_filter("assigned_to", filters))
    return _activity_rows(
        queryset,
        timestamp_field="completed_at",
        user_id_field="assigned_to_id",
        contour="reachtruck_inventory",
    )


def _processing_work_windows(filters: dict) -> list[dict]:
    from employees.models import Employee
    from processing_app.models import ProcessingWorkEvent

    _date_from, _date_to, period_start, period_end = _period(filters)
    rows = list(
        ProcessingWorkEvent.objects.filter(
            employee__isnull=False,
            occurred_at__gte=period_start,
            occurred_at__lt=period_end,
        )
        .filter(_processing_employee_filter(filters))
        .annotate(day=TruncDate("occurred_at", tzinfo=timezone.get_current_timezone()))
        .values("day", "employee_id")
        .annotate(
            first_action=Min("occurred_at"),
            last_action=Max("occurred_at"),
        )
        .order_by()
    )
    employees = Employee.objects.select_related("user").in_bulk(
        {int(row.get("employee_id") or 0) for row in rows if row.get("employee_id")}
    )
    windows = []
    for row in rows:
        employee = employees.get(int(row.get("employee_id") or 0))
        first_action = row.get("first_action")
        last_action = row.get("last_action")
        if employee is None or first_action is None or last_action is None:
            continue
        span_seconds = max(0, int((last_action - first_action).total_seconds()))
        windows.append(
            {
                "date_value": row["day"],
                "user_id": int(employee.user_id or 0),
                "employee_id": int(employee.pk),
                "user": employee,
                "first_action": first_action,
                "last_action": last_action,
                "activity_span_seconds": span_seconds,
                "activity_span_hours": round(span_seconds / 3600, 2),
                "contours": ("processing",),
            }
        )
    return windows


def _work_windows_from_activity_points(
    points: Iterable[ActivityPoint],
) -> list[dict]:
    grouped: dict[tuple[date, int], dict] = {}
    for day, user_id, contour, first_action, last_action in points:
        key = (day, int(user_id))
        row = grouped.setdefault(
            key,
            {
                "date_value": day,
                "user_id": int(user_id),
                "first_action": first_action,
                "last_action": last_action,
                "contours": set(),
            },
        )
        row["first_action"] = min(row["first_action"], first_action)
        row["last_action"] = max(row["last_action"], last_action)
        row["contours"].add(contour)

    users = (
        get_user_model()
        .objects.select_related("employee_profile")
        .in_bulk({user_id for _day, user_id in grouped})
    )
    windows: list[dict] = []
    for row in grouped.values():
        user = users.get(row["user_id"])
        if user is None:
            continue
        span_seconds = max(
            0,
            int((row["last_action"] - row["first_action"]).total_seconds()),
        )
        windows.append(
            {
                **row,
                "user": user,
                "activity_span_seconds": span_seconds,
                "activity_span_hours": round(span_seconds / 3600, 2),
                "contours": tuple(sorted(row["contours"])),
            }
        )
    windows.sort(
        key=lambda row: (
            -row["date_value"].toordinal(),
            str(row["user"].get_username()).lower(),
        )
    )
    return windows


def warehouse_employee_work_windows(filters: dict) -> list[dict]:
    points: list[ActivityPoint] = []
    for collector in (
        _fbs_activity_points,
        _receiving_activity_points,
        _reachtruck_activity_points,
        _warehouse_event_activity_points,
        _inventory_activity_points,
        _reachtruck_inventory_activity_points,
    ):
        points.extend(collector(filters))
    windows = _work_windows_from_activity_points(points)
    windows.extend(_processing_work_windows(filters))
    return windows
