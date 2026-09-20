"""Read-only data adapters for the parallel head-manager WMS workspace."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Count, Exists, F, Max, OuterRef, Q, Sum
from django.utils.dateparse import parse_date, parse_datetime
from django.utils import timezone

from audit.models import AuditEntry, OrderAuditEntry
from billing.models import BillingAct, BillingStorageDay, ClientInvoice
from employees.models import Employee
from fbs.models import (
    FbsIntegrationProfile,
    FbsOrder,
    FbsPickException,
    FbsPickingCart,
    FbsBox,
    FbsPallet,
    FbsStorageCell,
    FbsWorkstation,
)
from logistics.models import LogisticsTrip, ProblemTrip
from shipping.models import ShippingOrder, ShippingTransportNote
from sklad.models import (
    WarehouseContainer,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseStockSnapshot,
)
from sku.models import Agency, MarketCredential, SKU
from todo.models import Task
from head_manager.models import OwnCompany
from wms_new.models import (
    WmsNewAcceptance,
    WmsNewAssemblyLine,
    WmsNewAssemblySession,
    WmsNewBox,
    WmsNewBoxItem,
    WmsNewDocument,
    WmsNewBundleOperation,
    WmsNewBundleStock,
    WmsNewExtraFieldDefinition,
    WmsNewExtraFieldValue,
    WmsNewInventoryLine,
    WmsNewInventorySession,
    WmsNewMarkingCode,
    WmsNewMarketplaceStock,
    WmsNewBillingItem,
    WmsNewInvoice,
    WmsNewPartnerProfile,
    WmsNewPrimaryDocument,
    WmsNewRecord,
    WmsNewLogisticsManifest,
    WmsNewLogisticsOrder,
    WmsNewLogisticsPackage,
    WmsNewLogisticsRouteRule,
    WmsNewMovement,
    WmsNewEvent,
    WmsNewOrder,
    WmsNewOrderItem,
    WmsNewProduct,
    WmsNewReturn,
    WmsNewReturnLine,
    WmsNewShipment,
    WmsNewShipmentBox,
    WmsNewShipmentOrder,
    WmsNewTask,
    WmsNewWave,
    WmsNewWaveAllocation,
    WmsNewWaveOrder,
    WmsNewWavePickLine,
)
from wms_new.services.reports import REPORT_TITLES, build_report
from wms_new.services.partner_billing import ensure_partner_profiles, ensure_storage_items
from wms_new.services.settings import DIRECTORY_TYPES, system_section_settings, system_settings
from wms_new.services.tasks import TASK_BOARD_STAGES, board_stage_for


WMS_NEW_AVAILABILITY_FILTERS = {
    "ready": ("ready",),
    "risk": ("risk",),
    "unavailable": ("unavailable",),
}


READ_ONLY_NOTE = (
    "Экран читает фактические production-данные Fullbox. "
    "Изменяющие операции пока открываются в существующем рабочем контуре."
)


def _dt(value) -> str:
    if not value:
        return "-"
    if hasattr(value, "hour"):
        try:
            value = timezone.localtime(value)
        except (TypeError, ValueError):
            pass
        return value.strftime("%d.%m.%Y %H:%M")
    return value.strftime("%d.%m.%Y")


def _number(value) -> str:
    if value is None:
        return "0"
    if isinstance(value, Decimal):
        return f"{value:,.2f}".replace(",", " ")
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def _user(value) -> str:
    if not value:
        return "-"
    full_name = value.get_full_name() if hasattr(value, "get_full_name") else ""
    return str(full_name or getattr(value, "username", "") or value)


def _agency(value) -> str:
    if not value:
        return "-"
    return str(getattr(value, "short_name", "") or getattr(value, "agn_name", "") or value)


def _display(instance, field: str) -> str:
    getter = getattr(instance, f"get_{field}_display", None)
    value = getter() if callable(getter) else getattr(instance, field, "")
    return str(value or "-")


def _summary(label: str, value, hint: str = "") -> dict:
    return {"label": label, "value": _number(value), "hint": hint}


def _action(label: str, url: str, *, primary: bool = False) -> dict:
    return {"label": label, "url": url, "primary": primary}


def _table(
    *,
    title: str,
    columns: tuple[str, ...],
    rows: list[dict],
    summary: tuple[dict, ...] = (),
    actions: tuple[dict, ...] = (),
    empty: str = "Нет данных для отображения.",
    note: str = READ_ONLY_NOTE,
) -> dict:
    return {
        "title": title,
        "columns": columns,
        "rows": rows,
        "summary": summary,
        "actions": actions,
        "empty": empty,
        "note": note,
    }


def _problem_data(request=None) -> dict:
    params = request.GET if request is not None else {}
    problem_type = str(params.get("type") or "").strip()
    problem_status = str(params.get("status") or "open").strip()
    task_statuses = ("backlog", "blocked")
    tasks = list(
        Task.objects.filter(status__in=task_statuses)
        .select_related("assigned_to")
        .order_by("-updated_at", "-id")[:12]
    )
    exceptions = list(
        FbsPickException.objects.filter(status=FbsPickException.STATUS_OPEN)
        .select_related("task__order", "created_by")
        .order_by("-created_at", "-id")[:10]
    )
    trip_problems = list(
        ProblemTrip.objects.exclude(status__in=(ProblemTrip.STATUS_RESOLVED, ProblemTrip.STATUS_CLOSED))
        .select_related("trip", "assigned_to")
        .order_by("-updated_at", "-id")[:8]
    )
    raw_rows = [
        {
            "source_key": f"task:{item.id}",
            "number": f"T-{item.id}",
            "type": "Задача",
            "type_key": "task",
            "created_at": item.created_at,
            "resolved_at": None,
            "user": _user(item.assigned_to),
            "description": item.title,
            "source_status": "open",
        }
        for item in tasks
    ]
    raw_rows.extend(
        {
            "source_key": f"fbs:{item.id}",
            "number": f"F-{item.id}",
            "type": "FBS",
            "type_key": "fbs",
            "created_at": item.created_at,
            "resolved_at": None,
            "user": _user(item.created_by),
            "description": f"Заказ {item.task.order.external_order_id}: {_display(item, 'exception_type')}",
            "source_status": "open",
        }
        for item in exceptions
    )
    raw_rows.extend(
        {
            "source_key": f"logistics:{item.id}",
            "number": f"L-{item.id}",
            "type": "Логистика",
            "type_key": "logistics",
            "created_at": item.created_at,
            "resolved_at": None,
            "user": _user(item.assigned_to),
            "description": f"Рейс {item.trip.number}: {_display(item, 'reason_code')}",
            "source_status": "open",
        }
        for item in trip_problems
    )
    overlays = {
        item.source_key: item
        for item in WmsNewRecord.objects.filter(
            module="problems", entity_type="problem_state"
        )
    }
    rows = []
    for row in raw_rows:
        overlay = overlays.get(row["source_key"])
        row["status"] = overlay.status if overlay and overlay.status else row["source_status"]
        row["status_label"] = "Решена" if row["status"] == "resolved" else "Открыта"
        row["resolved_at"] = (overlay.payload or {}).get("resolved_at") if overlay else None
        if problem_type and row["type_key"] != problem_type:
            continue
        if problem_status in {"open", "resolved"} and row["status"] != problem_status:
            continue
        rows.append(row)
    rows.sort(key=lambda row: row["created_at"] or timezone.now(), reverse=True)
    return {
        "kind": "problems_list",
        "title": "Проблемы",
        "rows": rows,
        "filters": {"type": problem_type, "status": problem_status},
        "types": (("task", "Задачи"), ("fbs", "FBS"), ("logistics", "Логистика")),
        "statuses": (("open", "Открытые"), ("resolved", "Решенные"), ("all", "Все статусы")),
        "return_query": urlencode({key: value for key, value in {
            "type": problem_type, "status": problem_status,
        }.items() if value}),
        "empty": "Проблемы не найдены.",
    }


def _task_data() -> dict:
    open_tasks = Task.objects.exclude(status="done")
    rows = [
        {
            "cells": (
                f"#{item.id}",
                item.title,
                _display(item, "status"),
                _display(item, "priority"),
                _user(item.assigned_to),
                _dt(item.due_date),
                _dt(item.updated_at),
            ),
            "url": f"/todo/{item.id}/",
        }
        for item in open_tasks.select_related("assigned_to").order_by("-updated_at", "-id")[:25]
    ]
    return _table(
        title="Открытые задачи",
        columns=("№", "Задача", "Статус", "Приоритет", "Исполнитель", "Срок", "Обновлено"),
        rows=rows,
        summary=(
            _summary("Всего открыто", open_tasks.count()),
            _summary("В очереди", open_tasks.filter(status="backlog").count()),
            _summary("В работе", open_tasks.filter(status="in_progress").count()),
            _summary("Заблокировано", open_tasks.filter(status="blocked").count()),
            _summary(
                "Просрочено",
                open_tasks.filter(due_date__lt=timezone.now()).count(),
            ),
        ),
        actions=(
            _action("Все задачи", "/todo/", primary=True),
            _action("Создать задачу", "/todo/new/"),
        ),
    )


def _task_list_data(
    request=None,
    *,
    board_type: str = "",
    title: str = "Список задач",
) -> dict:
    params = request.GET if request is not None else {}
    filters = {
        "number": str(params.get("number") or "").strip(),
        "q": str(params.get("q") or "").strip(),
        "partner": str(params.get("partner") or "").strip(),
        "task_type": str(params.get("task_type") or board_type or "").strip(),
        "status": str(params.get("status") or "open").strip(),
    }
    queryset = WmsNewTask.objects.select_related("agency", "assigned_to").prefetch_related(
        "items", "services"
    )
    if filters["number"]:
        if filters["number"].isdigit():
            queryset = queryset.filter(pk=int(filters["number"]))
        else:
            queryset = queryset.none()
    if filters["q"]:
        queryset = queryset.filter(
            Q(title__icontains=filters["q"])
            | Q(description__icontains=filters["q"])
            | Q(source_snapshot__icontains=filters["q"])
        )
    if filters["partner"].isdigit():
        queryset = queryset.filter(agency_id=int(filters["partner"]))
    else:
        filters["partner"] = ""
    task_types = dict(WmsNewTask.TYPE_CHOICES)
    if filters["task_type"] in task_types:
        queryset = queryset.filter(workflow_type=filters["task_type"])
    else:
        filters["task_type"] = ""
    if filters["status"] == "open":
        queryset = queryset.exclude(status=WmsNewTask.STATUS_DONE)
    elif filters["status"] in dict(WmsNewTask.STATUS_CHOICES):
        queryset = queryset.filter(status=filters["status"])
    elif filters["status"] == "all":
        pass
    else:
        filters["status"] = "open"
        queryset = queryset.exclude(status=WmsNewTask.STATUS_DONE)

    try:
        page_size = int(params.get("page_size") or 25)
    except (TypeError, ValueError):
        page_size = 25
    if page_size not in (25, 50, 100, 200, 500):
        page_size = 25
    page = Paginator(queryset.order_by("due_date", "id"), page_size).get_page(
        params.get("page")
    )
    rows = []
    for task in page.object_list:
        task_items = list(task.items.all())
        service_total = sum((service.total for service in task.services.all()), Decimal("0"))
        rows.append(
            {
                "id": task.id,
                "partner": _agency(task.agency),
                "title": task.title,
                "task_type": task_types.get(task.workflow_type, "Прочие задачи"),
                "status": task.get_status_display(),
                "due_date": task.due_date,
                "overdue": task.is_overdue,
                "address": task.delivery_address or "-",
                "sku_count": len({item.product_id for item in task_items}),
                "unit_count": sum(item.planned_qty for item in task_items),
                "billing": (
                    f"Протарифицирована: {service_total:.2f} руб"
                    if task.tariff_finalized
                    else f"Не протарифицирована: {service_total:.2f} руб"
                ),
                "confirmed": task.client_confirmed,
                "url": f"/head-manager/fbs-new/tasks/{task.id}/",
            }
        )
    query_values = {**filters, "page_size": page_size}
    query_values = {key: value for key, value in query_values.items() if value not in ("", None)}
    query_prefix = urlencode(query_values)
    selected_task = None
    selected_task_id = str(params.get("task_id") or "").strip()
    if selected_task_id.isdigit():
        selected = (
            WmsNewTask.objects.select_related("agency", "assigned_to", "created_by")
            .filter(pk=int(selected_task_id))
            .first()
        )
        if selected:
            selected_task = {
                "id": selected.id,
                "partner": _agency(selected.agency),
                "title": selected.title,
                "description": selected.description,
                "task_type": task_types.get(selected.workflow_type, "Прочие задачи"),
                "status": selected.status,
                "status_label": selected.get_status_display(),
                "priority": selected.priority,
                "priority_label": selected.get_priority_display(),
                "assigned_to": _user(selected.assigned_to),
                "assigned_to_id": selected.assigned_to_id,
                "due_date": selected.due_date,
                "created_at": selected.created_at,
                "updated_at": selected.updated_at,
            }
    return {
        "kind": "tasks_list",
        "title": title,
        "rows": rows,
        "filters": filters,
        "partners": Agency.objects.filter(archived=False).order_by("agn_name", "id"),
        "task_types": WmsNewTask.TYPE_CHOICES,
        "statuses": WmsNewTask.STATUS_CHOICES,
        "priorities": WmsNewTask.PRIORITY_CHOICES,
        "page": page,
        "page_numbers": page.paginator.get_elided_page_range(
            page.number, on_each_side=2, on_ends=1
        ),
        "page_size": page_size,
        "page_sizes": (25, 50, 100, 200, 500),
        "query_prefix": f"{query_prefix}&" if query_prefix else "",
        "selected_task": selected_task,
        "employees": Employee.objects.filter(is_active=True, user__isnull=False).order_by(
            "full_name", "id"
        ),
        "empty": "Задачи не найдены.",
    }


def build_task_detail_data(task_id: int, *, request=None) -> dict | None:
    task = (
        WmsNewTask.objects.select_related("agency", "assigned_to", "created_by")
        .prefetch_related(
            "items__product",
            "items__box",
            "boxes",
            "services__item__product",
            "attachments__uploaded_by",
        )
        .filter(pk=task_id)
        .first()
    )
    if task is None:
        return None
    items = list(task.items.all())
    services = list(task.services.all())
    task_tab = str((request.GET if request is not None else {}).get("tab") or "goods").strip()
    if task_tab not in {"goods", "services", "history"}:
        task_tab = "goods"
    return {
        "kind": "task_detail",
        "title": f"Задача #{task.id}",
        "task": task,
        "stage": board_stage_for(task),
        "stage_label": dict(
            TASK_BOARD_STAGES.get(task.workflow_type, TASK_BOARD_STAGES[WmsNewTask.TYPE_OTHER])
        ).get(board_stage_for(task), task.get_status_display()),
        "items": items,
        "boxes": list(task.boxes.all()),
        "services": services,
        "services_total": sum((service.total for service in services), Decimal("0")),
        "task_tab": task_tab,
        "attachments": list(task.attachments.all()),
        "products": WmsNewProduct.objects.filter(
            **({"agency_id": task.agency_id} if task.agency_id else {}),
            is_archived=False,
        ).order_by("name", "id")[:1000],
        "employees": Employee.objects.filter(is_active=True, user__isnull=False).order_by(
            "full_name", "id"
        ),
        "history": WmsNewEvent.objects.filter(entity_type="task", entity_id=task.id)
        .select_related("actor")
        .order_by("-created_at", "-id")[:100],
        "can_edit": task.status != WmsNewTask.STATUS_DONE,
        "back_url": "/head-manager/fbs-new/tasks-list/",
    }


def _task_board_data(request, *, workflow_type: str, title: str) -> dict:
    params = request.GET if request is not None else {}
    query = str(params.get("q") or "").strip()
    partner = str(params.get("partner") or "").strip()
    queryset = WmsNewTask.objects.filter(workflow_type=workflow_type).select_related("agency")
    if query:
        queryset = queryset.filter(
            Q(title__icontains=query)
            | Q(description__icontains=query)
            | Q(source_snapshot__icontains=query)
        )
    if partner.isdigit():
        queryset = queryset.filter(agency_id=int(partner))
    else:
        partner = ""
    stages = TASK_BOARD_STAGES[workflow_type]
    columns = [{"key": key, "label": label, "cards": []} for key, label in stages]
    by_key = {column["key"]: column for column in columns}
    for task in queryset.order_by("due_date", "id"):
        snapshot = task.source_snapshot or {}
        if task.status == WmsNewTask.STATUS_DONE or snapshot.get("board_cancelled"):
            continue
        stage = board_stage_for(task)
        column = by_key.get(stage, columns[0])
        column["cards"].append(
            {
                "id": task.id,
                "partner": _agency(task.agency),
                "date": task.due_date or task.created_at,
                "title": task.title,
                "description": task.description,
                "sku_count": int(snapshot.get("sku_count") or snapshot.get("items_count") or 0),
                "unit_count": int(snapshot.get("unit_count") or snapshot.get("quantity") or 0),
                "stage": stage,
                "is_first": stage == stages[0][0],
                "is_last": stage == stages[-1][0],
            }
        )
    return {
        "kind": "tasks_board",
        "title": title,
        "workflow_type": workflow_type,
        "columns": columns,
        "partners": Agency.objects.filter(archived=False).order_by("agn_name", "id"),
        "task_types": WmsNewTask.TYPE_CHOICES,
        "priorities": WmsNewTask.PRIORITY_CHOICES,
        "filters": {"q": query, "partner": partner},
        "return_query": urlencode({key: value for key, value in {
            "q": query, "partner": partner,
        }.items() if value}),
    }


def _multiacceptance_data(request=None) -> dict:
    params = request.GET if request is not None else {}
    query = str(params.get("q") or "").strip()
    partner = str(params.get("partner") or "").strip()
    status = str(params.get("status") or "all").strip()
    queryset = WmsNewRecord.objects.filter(
        module="tasks", entity_type="multiacceptance"
    ).select_related("agency")
    if query:
        lookup = Q(title__icontains=query) | Q(source_key__icontains=query) | Q(payload__icontains=query)
        if query.isdigit():
            lookup |= Q(pk=int(query))
        queryset = queryset.filter(lookup)
    if partner.isdigit():
        queryset = queryset.filter(agency_id=int(partner))
    else:
        partner = ""
    status_labels = {
        "created": "Создана",
        "in_progress": "В работе",
        "done": "Завершена",
        "cancelled": "Отменена",
    }
    if status in status_labels:
        queryset = queryset.filter(status=status)
    else:
        status = "all"
    try:
        page_size = int(params.get("page_size") or 100)
    except (TypeError, ValueError):
        page_size = 100
    if page_size not in (25, 50, 100, 200, 500):
        page_size = 100
    page = Paginator(queryset.order_by("-created_at", "-id"), page_size).get_page(params.get("page"))
    rows = []
    for record in page.object_list:
        payload = record.payload or {}
        partner_names = payload.get("partner_names") or ([] if not record.agency else [_agency(record.agency)])
        rows.append({
            "id": record.id,
            "title": record.title or f"Мультиприемка №{record.id}",
            "status": status_labels.get(record.status, record.status or "Создана"),
            "status_key": record.status or "created",
            "created_at": record.created_at,
            "partner_count": int(payload.get("partner_count") or len(partner_names)),
            "sku_count": int(payload.get("sku_count") or 0),
            "unit_count": int(payload.get("unit_count") or 0),
        })
    return {
        "kind": "tasks_multiacceptance",
        "title": "Список мультиприемок",
        "rows": rows,
        "page": page,
        "page_size": page_size,
        "page_sizes": (25, 50, 100, 200, 500),
        "partners": Agency.objects.filter(archived=False).order_by("agn_name", "id"),
        "statuses": tuple(status_labels.items()),
        "filters": {"q": query, "partner": partner, "status": status},
        "return_query": urlencode({key: value for key, value in {
            "q": query, "partner": partner, "status": status,
        }.items() if value and value != "all"}),
    }


def _order_rows(queryset, limit: int = 25) -> list[dict]:
    orders = queryset.select_related("agency").annotate(
        line_count=Count("items", distinct=True),
        unit_count=Sum("items__quantity"),
    )[:limit]
    return [
        {
            "cells": (
                item.external_order_id,
                item.integration_name or item.marketplace or "-",
                _agency(item.agency),
                f"{item.line_count} SKU / {_number(item.unit_count)} шт.",
                item.get_status_display(),
                _dt(item.cutoff_at),
                _dt(item.updated_at),
            ),
            "url": f"/head-manager/fbs-new/fbs-orders/{item.id}/",
        }
        for item in orders
    ]


def _fbs_order_data(request=None, title: str = "Заказы FBS", queryset=None) -> dict:
    params = request.GET if request is not None else {}
    queryset = queryset if queryset is not None else WmsNewOrder.objects.all()
    queryset = queryset.select_related("agency").annotate(
        unit_count=Sum("items__quantity")
    )
    filters = {
        "q": str(params.get("q") or "").strip(),
        "partner": str(params.get("partner") or "").strip(),
        "delivery": str(params.get("delivery") or "").strip(),
        "integration": str(params.get("integration") or "").strip(),
        "status": str(params.get("status") or "").strip(),
        "quantity": str(params.get("quantity") or "").strip(),
        "date_field": str(params.get("date_field") or "created").strip(),
        "date_from": str(params.get("date_from") or "").strip(),
        "date_to": str(params.get("date_to") or "").strip(),
    }
    if filters["q"]:
        queryset = queryset.filter(
            Q(external_order_id__icontains=filters["q"])
            | Q(source_order_id__icontains=filters["q"])
            | Q(tracking_number__icontains=filters["q"])
        )
    if filters["partner"].isdigit():
        queryset = queryset.filter(agency_id=int(filters["partner"]))
    else:
        filters["partner"] = ""
    delivery_choices = (
        ("wb", "Wildberries"),
        ("ozon", "Ozon"),
        ("courier", "Курьер"),
    )
    if filters["delivery"] in dict(delivery_choices):
        queryset = queryset.filter(marketplace=filters["delivery"])
    else:
        filters["delivery"] = ""
    if filters["integration"]:
        queryset = queryset.filter(integration_name=filters["integration"])
    if filters["status"] in dict(WmsNewOrder.STATUS_CHOICES):
        queryset = queryset.filter(status=filters["status"])
    else:
        filters["status"] = ""
    if filters["quantity"] == "one":
        queryset = queryset.filter(unit_count=1)
    elif filters["quantity"] == "many":
        queryset = queryset.filter(unit_count__gt=1)
    else:
        filters["quantity"] = ""
    date_field = "cutoff_at" if filters["date_field"] == "shipping" else "source_created_at"
    if filters["date_field"] not in {"created", "shipping"}:
        filters["date_field"] = "created"
        date_field = "source_created_at"
    date_from = parse_date(filters["date_from"])
    date_to = parse_date(filters["date_to"])
    if date_from:
        queryset = queryset.filter(**{f"{date_field}__date__gte": date_from})
    else:
        filters["date_from"] = ""
    if date_to:
        queryset = queryset.filter(**{f"{date_field}__date__lte": date_to})
    else:
        filters["date_to"] = ""

    page_sizes = (25, 50, 100, 200, 500)
    try:
        page_size = int(params.get("page_size") or 25)
    except (TypeError, ValueError):
        page_size = 25
    if page_size not in page_sizes:
        page_size = 25
    ordered_queryset = queryset.distinct().order_by(
        F("source_created_at").desc(nulls_last=True), "-id"
    )
    page = Paginator(ordered_queryset, page_size).get_page(params.get("page"))
    rows = [
        {
            "id": order.id,
            "display_id": order.source_order_id or order.id,
            "number": order.external_order_id,
            "partner": _agency(order.agency),
            "integration": order.integration_name or dict(delivery_choices).get(
                order.marketplace, order.marketplace
            ),
            "created_at": order.ordered_at or order.source_created_at,
            "delivery": order.delivery_type or "-",
            "tracking": order.tracking_number or "-",
            "unit_count": int(order.unit_count or 0),
            "status": order.get_status_display(),
            "url": f"/head-manager/fbs-new/fbs-orders/{order.id}/",
        }
        for order in page.object_list
    ]
    partners = Agency.objects.filter(wms_new_orders__isnull=False).distinct().order_by(
        "agn_name", "id"
    )
    integrations = list(
        WmsNewOrder.objects.exclude(integration_name="")
        .order_by("integration_name")
        .values_list("integration_name", flat=True)
        .distinct()
    )
    latest_order = WmsNewOrder.objects.order_by("-last_synced_at").only("last_synced_at").first()
    query_values = {**filters, "page_size": page_size}
    query_values = {key: value for key, value in query_values.items() if value not in ("", None)}
    query_prefix = urlencode(query_values)
    return {
        "kind": "fbs_orders_list",
        "title": title,
        "rows": rows,
        "page": page,
        "page_size": page_size,
        "page_sizes": page_sizes,
        "query_prefix": f"{query_prefix}&" if query_prefix else "",
        "filters": filters,
        "partners": partners,
        "manual_partners": Agency.objects.filter(archived=False).order_by("agn_name", "id"),
        "deliveries": delivery_choices,
        "integrations": integrations,
        "statuses": WmsNewOrder.STATUS_CHOICES,
        "updated_label": _queue_updated_label(latest_order.last_synced_at if latest_order else None),
        "empty": "Заказы не найдены.",
    }


def _queue_payload_value(payload, keys: tuple[str, ...]) -> str:
    """Return the first scalar value for a known delivery field."""

    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if value not in (None, "", [], {}):
                if isinstance(value, (str, int, float)):
                    return str(value)
        for value in payload.values():
            found = _queue_payload_value(value, keys)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload[:20]:
            found = _queue_payload_value(value, keys)
            if found:
                return found
    return ""


def _queue_availability(order) -> tuple[str, str, list[dict]]:
    state = str(getattr(order, "availability_state", "unavailable") or "unavailable")
    if state in WMS_NEW_AVAILABILITY_FILTERS["ready"]:
        label, css, icon = "Доступно", "success", "✓"
    elif state in WMS_NEW_AVAILABILITY_FILTERS["risk"]:
        label, css, icon = "Может не хватить", "warning", "●"
    else:
        label, css, icon = "Не в наличии", "danger", "×"

    detail_rows = []
    for item in getattr(order, "availability_snapshot", ()):
        storage_qty = int(item.get("storage") or 0)
        transit_qty = int(item.get("transit") or 0)
        total_qty = int(item.get("total") or storage_qty + transit_qty)
        reserve_qty = int(item.get("reserve") or 0)
        detail_rows.append(
            {
                "name": item.get("name") or "Товар",
                "icon": icon,
                "required": int(item.get("required") or item.get("required_qty") or 0),
                "storage": storage_qty,
                "total": total_qty,
                "transit": transit_qty,
                "reserve": reserve_qty,
                "css": css,
            }
        )
    if not detail_rows:
        detail_rows.append(
            {
                "name": "Товар не сопоставлен с остатками",
                "icon": icon,
                "required": int(getattr(order, "unit_count", 0) or 0),
                "storage": 0,
                "total": 0,
                "transit": 0,
                "reserve": 0,
                "css": css,
            }
        )
    return label, css, detail_rows


def _queue_updated_label(value) -> str:
    if not value:
        return "данных пока нет"
    try:
        delta = max(int((timezone.now() - value).total_seconds()), 0)
    except (TypeError, ValueError):
        return "только что"
    if delta < 60:
        return f"{delta} сек. назад"
    if delta < 3600:
        return f"{delta // 60} мин. назад"
    return _dt(value)


def _fbs_queue_data(request=None) -> dict:
    queue_statuses = (
        WmsNewOrder.STATUS_NEW,
        WmsNewOrder.STATUS_AWAITING_STOCK,
        WmsNewOrder.STATUS_RESERVED,
        WmsNewOrder.STATUS_QUEUED,
    )
    params = request.GET if request is not None else {}
    queryset = WmsNewOrder.objects.filter(status__in=queue_statuses).select_related(
        "agency"
    ).annotate(
        line_count=Count("items", distinct=True),
        unit_count=Sum("items__quantity"),
    )

    filters = {
        "q": str(params.get("q") or "").strip(),
        "partner": str(params.get("partner") or "").strip(),
        "delivery": str(params.get("delivery") or "").strip(),
        "integration": str(params.get("integration") or "").strip(),
        "status": str(params.get("status") or "").strip(),
        "zone": str(params.get("zone") or "").strip(),
        "quantity": str(params.get("quantity") or "").strip(),
        "availability": str(params.get("availability") or "").strip(),
        "date_field": str(params.get("date_field") or "created").strip(),
        "date_from": str(params.get("date_from") or "").strip(),
        "date_to": str(params.get("date_to") or "").strip(),
    }

    if filters["q"]:
        queryset = queryset.filter(
            Q(external_order_id__icontains=filters["q"])
            | Q(agency__agn_name__icontains=filters["q"])
            | Q(items__external_sku__icontains=filters["q"])
            | Q(items__barcode__icontains=filters["q"])
            | Q(items__product_name__icontains=filters["q"])
        )
    if filters["partner"].isdigit():
        queryset = queryset.filter(agency_id=int(filters["partner"]))
    else:
        filters["partner"] = ""
    delivery_choices = (
        ("wb", "Wildberries"),
        ("ozon", "Ozon"),
        ("courier", "Курьер"),
    )
    if filters["delivery"] in dict(delivery_choices):
        queryset = queryset.filter(marketplace=filters["delivery"])
    else:
        filters["delivery"] = ""
    if filters["integration"]:
        queryset = queryset.filter(integration_name=filters["integration"])
    if filters["status"] in dict(WmsNewOrder.STATUS_CHOICES):
        queryset = queryset.filter(status=filters["status"])
    else:
        filters["status"] = ""
    if filters["quantity"] == "one":
        queryset = queryset.filter(unit_count=1)
    elif filters["quantity"] == "many":
        queryset = queryset.filter(unit_count__gt=1)
    else:
        filters["quantity"] = ""

    if filters["zone"]:
        zone_stock = WarehouseStockSnapshot.objects.filter(
            agency_id=OuterRef("order__agency_id"),
            barcode=OuterRef("barcode"),
            zone_code__iexact=filters["zone"],
            is_archived=False,
            qty__gt=0,
        )
        zone_items = WmsNewOrderItem.objects.filter(order_id=OuterRef("pk")).annotate(
            exists_in_zone=Exists(zone_stock)
        ).filter(exists_in_zone=True)
        queryset = queryset.annotate(has_zone_stock=Exists(zone_items)).filter(
            has_zone_stock=True
        )

    date_field = "cutoff_at" if filters["date_field"] == "shipping" else "source_created_at"
    if filters["date_field"] not in {"created", "shipping"}:
        filters["date_field"] = "created"
        date_field = "source_created_at"
    date_from = parse_date(filters["date_from"])
    date_to = parse_date(filters["date_to"])
    if date_from:
        queryset = queryset.filter(**{f"{date_field}__date__gte": date_from})
    else:
        filters["date_from"] = ""
    if date_to:
        queryset = queryset.filter(**{f"{date_field}__date__lte": date_to})
    else:
        filters["date_to"] = ""

    if filters["availability"] in WMS_NEW_AVAILABILITY_FILTERS:
        queryset = queryset.filter(
            availability_state__in=WMS_NEW_AVAILABILITY_FILTERS[filters["availability"]]
        )
    else:
        filters["availability"] = ""

    page_sizes = (25, 50, 100, 200, 500)
    try:
        page_size = int(params.get("page_size") or 25)
    except (TypeError, ValueError):
        page_size = 25
    if page_size not in page_sizes:
        page_size = 25
    ordered_queryset = queryset.distinct().order_by(
        F("source_created_at").desc(nulls_last=True), "-id"
    )
    page = Paginator(ordered_queryset, page_size).get_page(params.get("page"))
    orders = list(page.object_list)
    rows = []
    for order in orders:
        availability_label, availability_css, availability_rows = _queue_availability(order)
        created_at = order.ordered_at or order.source_created_at
        rows.append(
            {
                "id": order.id,
                "display_id": order.source_order_id or order.id,
                "number": order.external_order_id,
                "partner": _agency(order.agency),
                "integration": order.integration_name or dict(delivery_choices).get(order.marketplace, order.marketplace),
                "created_at": created_at,
                "delivery": order.delivery_type or "-",
                "tracking": order.tracking_number or "-",
                "unit_count": int(order.unit_count or 0),
                "status": order.get_status_display(),
                "availability": availability_label,
                "availability_css": availability_css,
                "availability_rows": availability_rows,
                "url": f"/head-manager/fbs-new/fbs-orders/{order.id}/",
            }
        )

    partners = Agency.objects.filter(
        wms_new_orders__status__in=queue_statuses
    ).distinct().order_by("agn_name", "id")
    integrations = list(
        WmsNewOrder.objects.filter(status__in=queue_statuses)
        .exclude(integration_name="")
        .order_by("integration_name")
        .values_list("integration_name", flat=True)
        .distinct()
    )
    zones = list(
        WarehouseStockSnapshot.objects.filter(is_archived=False, qty__gt=0)
        .exclude(zone_code="")
        .order_by("zone_code")
        .values_list("zone_code", flat=True)
        .distinct()[:100]
    )
    latest_order = WmsNewOrder.objects.filter(status__in=queue_statuses).order_by(
        "-last_synced_at"
    ).only("last_synced_at").first()
    query_values = {**filters, "page_size": page_size}
    query_values = {key: value for key, value in query_values.items() if value not in ("", None)}
    query_prefix = urlencode(query_values)
    return {
        "kind": "fbs_queue",
        "title": "Очередь",
        "rows": rows,
        "page": page,
        "page_size": page_size,
        "page_sizes": page_sizes,
        "query_prefix": f"{query_prefix}&" if query_prefix else "",
        "filters": filters,
        "partners": partners,
        "manual_partners": Agency.objects.filter(archived=False).order_by("agn_name", "id"),
        "deliveries": delivery_choices,
        "integrations": integrations,
        "statuses": tuple(
            (value, label)
            for value, label in WmsNewOrder.STATUS_CHOICES
            if value in queue_statuses
        ),
        "zones": zones,
        "updated_label": _queue_updated_label(
            latest_order.last_synced_at if latest_order else None
        ),
        "empty": "Нет заказов, соответствующих фильтрам.",
    }


def build_fbs_return_detail_data(return_id: int) -> dict | None:
    item = (
        WmsNewReturn.objects.select_related(
            "order__agency",
            "assigned_to",
            "received_by",
            "destination_location",
            "return_box",
        )
        .prefetch_related("lines__product", "lines__order_item")
        .filter(pk=return_id)
        .first()
    )
    if item is None:
        return None
    locations = WarehouseLocation.objects.filter(is_active=True).order_by(
        "warehouse_code", "zone_code", "location_code", "id"
    )
    history = list(
        WmsNewEvent.objects.filter(entity_type="return", entity_id=item.id)
        .select_related("actor")
        .order_by("-created_at", "-id")[:50]
    )
    return {
        "kind": "fbs_return_detail",
        "title": f"Возврат #{item.source_request_id or item.id}",
        "item": item,
        "id": item.id,
        "display_id": item.source_request_id or item.id,
        "order": item.order,
        "partner": _agency(item.order.agency),
        "status": item.get_status_display(),
        "lines": list(item.lines.all()),
        "locations": locations,
        "history": history,
        "can_receive": item.is_manual and item.status in {
            WmsNewReturn.STATUS_QUEUED,
            WmsNewReturn.STATUS_FAILED,
            WmsNewReturn.STATUS_IN_PROGRESS,
        },
        "can_inspect": item.is_manual and item.status == WmsNewReturn.STATUS_IN_PROGRESS,
        "back_url": "/head-manager/fbs-new/fbs-returns/",
    }


def _order_source_money(order: WmsNewOrder, payload: dict) -> Decimal:
    for key in (
        "convertedFinalPrice",
        "convertedPrice",
        "salePrice",
        "finalPrice",
        "price",
    ):
        raw_value = payload.get(key)
        if raw_value in (None, ""):
            continue
        try:
            value = Decimal(str(raw_value))
        except (ArithmeticError, ValueError):
            continue
        if str(order.marketplace or "").lower() in {"wb", "wildberries"}:
            value /= Decimal("100")
        return value.quantize(Decimal("0.01"))
    return Decimal("0.00")


def build_fbs_order_detail_data(order_id: int) -> dict | None:
    order = (
        WmsNewOrder.objects.select_related("agency", "created_by")
        .prefetch_related("items__sku", "wave_orders__wave")
        .filter(pk=order_id)
        .first()
    )
    if order is None:
        return None

    raw_payload = order.source_snapshot.get("raw_payload") or {}
    item_rows = []
    total_quantity = 0
    total_price = Decimal("0.00")
    for item in order.items.all():
        price = _order_source_money(order, item.source_snapshot or {})
        line_total = price * item.quantity
        total_quantity += item.quantity
        total_price += line_total
        sku = item.sku
        item_rows.append(
            {
                "image_url": sku.img if sku else "",
                "article": item.external_sku or (sku.sku_code if sku else "-"),
                "barcode": item.barcode or "-",
                "name": item.product_name or (sku.name if sku else "Товар"),
                "quantity": item.quantity,
                "price": _number(price),
                "total": _number(line_total),
            }
        )

    wave_order = order.wave_orders.select_related("wave").order_by("-id").first()
    event_labels = {
        "create_manual": "Заказ создан в FBS-NEW",
        "update": "Данные заказа изменены",
        "bulk_update": "Выполнено групповое действие",
        "launch_wave": "Заказ включен в волну",
    }
    history = [
        {
            "created_at": event.created_at,
            "action": event_labels.get(event.action, event.action.replace("_", " ").capitalize()),
            "actor": _user(event.actor),
        }
        for event in WmsNewEvent.objects.select_related("actor").filter(
            entity_type="order", entity_id=order.id
        )[:50]
    ]

    source_id = order.source_order_id or order.id
    creator = _user(order.created_by) if order.created_by_id else "Система"
    load_note = "Создан вручную в FBS-NEW" if order.is_manual else "Загружен автоматически"
    internal_comment = _queue_payload_value(
        raw_payload,
        ("internalComment", "warehouseComment", "managerComment", "comment"),
    )
    customer_comment = _queue_payload_value(
        raw_payload,
        ("customerComment", "deliveryComment", "buyerComment"),
    )
    recipient = _queue_payload_value(
        raw_payload,
        ("recipientName", "recipient", "customerName", "buyerName", "fio"),
    )
    cod_amount = _queue_payload_value(
        raw_payload,
        ("codAmount", "paymentAmount", "cashOnDelivery", "paymentSum"),
    )
    act_number = _queue_payload_value(raw_payload, ("actNumber", "actId", "act"))
    place_number = _queue_payload_value(raw_payload, ("placeNumber", "placeId", "place"))

    return {
        "kind": "fbs_order_detail",
        "title": f"Заказ #{source_id}",
        "id": order.id,
        "source_id": source_id,
        "external_order_id": order.external_order_id,
        "delivery_type": order.delivery_type or order.marketplace or "-",
        "status": order.status,
        "status_label": order.get_status_display(),
        "statuses": WmsNewOrder.STATUS_CHOICES,
        "partner": _agency(order.agency),
        "creator": creator,
        "load_note": load_note,
        "integration": order.integration_name or order.marketplace or "-",
        "created_at": order.ordered_at or order.source_created_at or order.created_at,
        "cutoff_at": order.cutoff_at,
        "total_quantity": total_quantity,
        "total_price": _number(total_price),
        "cod_amount": cod_amount or "-",
        "tracking_number": order.tracking_number or "-",
        "act_number": act_number or "-",
        "place_number": place_number or "-",
        "wave": wave_order.wave.number if wave_order else "-",
        "files_count": 0,
        "warehouse_code": order.warehouse_code or "-",
        "recipient": recipient or "Не указан",
        "internal_comment": internal_comment,
        "customer_comment": customer_comment,
        "items": item_rows,
        "history": history,
        "last_synced_at": order.last_synced_at,
        "back_url": "/head-manager/fbs-new/fbs-queue/",
    }


def _fbs_wave_data(request=None) -> dict:
    params = request.GET if request is not None else {}
    queryset = WmsNewWave.objects.select_related("created_by", "assigned_to")
    filters = {
        "status": str(params.get("status") or "").strip(),
        "user": str(params.get("user") or "").strip(),
        "date_field": str(params.get("date_field") or "created").strip(),
        "date_from": str(params.get("date_from") or "").strip(),
        "date_to": str(params.get("date_to") or "").strip(),
    }
    status_filters = {
        "new": Q(status=WmsNewWave.STATUS_QUEUED),
        "picking": Q(
            status__in=(
                WmsNewWave.STATUS_IN_PROGRESS,
                WmsNewWave.STATUS_VERIFICATION,
            )
        ),
        "done_success": Q(status=WmsNewWave.STATUS_DONE),
        "done_problems": Q(
            status=WmsNewWave.STATUS_DONE,
            source_snapshot__has_problems=True,
        ),
    }
    if filters["status"] in status_filters:
        queryset = queryset.filter(status_filters[filters["status"]])
        if filters["status"] == "done_success":
            queryset = queryset.exclude(source_snapshot__has_problems=True)
    else:
        filters["status"] = ""
    if filters["user"].isdigit():
        queryset = queryset.filter(created_by_id=int(filters["user"]))
    else:
        filters["user"] = ""
    date_fields = {
        "created": "source_created_at",
        "started": "started_at",
        "completed": "completed_at",
    }
    if filters["date_field"] not in date_fields:
        filters["date_field"] = "created"
    date_field = date_fields[filters["date_field"]]
    date_from = parse_date(filters["date_from"])
    date_to = parse_date(filters["date_to"])
    if date_from:
        queryset = queryset.filter(**{f"{date_field}__date__gte": date_from})
    else:
        filters["date_from"] = ""
    if date_to:
        queryset = queryset.filter(**{f"{date_field}__date__lte": date_to})
    else:
        filters["date_to"] = ""

    page_sizes = (25, 50, 100, 200, 500)
    try:
        page_size = int(params.get("page_size") or 25)
    except (TypeError, ValueError):
        page_size = 25
    if page_size not in page_sizes:
        page_size = 25
    page = Paginator(
        queryset.order_by(F("source_created_at").desc(nulls_last=True), "-id"),
        page_size,
    ).get_page(params.get("page"))
    rows = [
        {
            "id": wave.id,
            "display_id": wave.source_batch_id or wave.id,
            "status": _wave_status_label(wave),
            "created_at": wave.source_created_at or wave.created_at,
            "started_at": wave.started_at,
            "completed_at": wave.completed_at,
            "creator": _user(wave.created_by),
            "place": wave.cart_name or wave.workstation_name or "-",
            "orders": wave.planned_orders,
            "units": wave.planned_units,
            "url": f"/head-manager/fbs-new/fbs-waves/{wave.id}/",
        }
        for wave in page.object_list
    ]
    query_values = {**filters, "page_size": page_size}
    query_values = {key: value for key, value in query_values.items() if value not in ("", None)}
    query_prefix = urlencode(query_values)
    latest = WmsNewWave.objects.order_by("-last_synced_at").only("last_synced_at").first()
    return {
        "kind": "fbs_waves_list",
        "title": "Волны",
        "rows": rows,
        "filters": filters,
        "statuses": (
            ("new", "Новая"),
            ("picking", "Подбор товаров"),
            ("done_success", "Завершена успешно"),
            ("done_problems", "Завершена с проблемами"),
        ),
        "users": Employee.objects.filter(is_active=True, user__isnull=False)
        .select_related("user")
        .order_by("full_name", "id"),
        "page": page,
        "page_size": page_size,
        "page_sizes": page_sizes,
        "query_prefix": f"{query_prefix}&" if query_prefix else "",
        "updated_label": _queue_updated_label(latest.last_synced_at if latest else None),
        "empty": "Волны не найдены.",
    }


def _wave_status_label_value(status: str, has_problems: bool = False) -> str:
    if status == WmsNewWave.STATUS_QUEUED:
        return "Новая"
    if status in {WmsNewWave.STATUS_IN_PROGRESS, WmsNewWave.STATUS_VERIFICATION}:
        return "Подбор товаров"
    if status == WmsNewWave.STATUS_DONE:
        return "Завершена с проблемами" if has_problems else "Завершена успешно"
    if status == WmsNewWave.STATUS_CANCELLED:
        return "Отменена"
    return status


def _wave_status_label(wave: WmsNewWave) -> str:
    return _wave_status_label_value(
        wave.status,
        bool((wave.source_snapshot or {}).get("has_problems")),
    )


def build_fbs_wave_detail_data(wave_id: int, *, collection_mode: bool = False) -> dict | None:
    wave = (
        WmsNewWave.objects.select_related("agency", "created_by", "assigned_to")
        .prefetch_related(
            "wave_orders__order__items",
            "pick_lines__order",
            "pick_lines__order_item",
            "pick_lines__product",
            "pick_lines__allocations__box_item",
        )
        .filter(pk=wave_id)
        .first()
    )
    if wave is None:
        return None
    item_rows = []
    orders = []
    pick_lines = list(wave.pick_lines.all())
    if pick_lines:
        seen_orders = set()
        for line in pick_lines:
            order = line.order
            if order.id not in seen_orders:
                seen_orders.add(order.id)
                orders.append(order)
            allocations = list(line.allocations.all())
            route = ", ".join(
                dict.fromkeys(
                    allocation.source_place_name or allocation.source_place_code
                    for allocation in allocations
                    if allocation.source_place_name or allocation.source_place_code
                )
            ) or "Нет доступного места"
            item_rows.append(
                {
                    "item_id": line.order_item.source_item_id or line.order_item.id,
                    "order_id": order.source_order_id or order.id,
                    "internal_order_id": order.id,
                    "order_number": order.external_order_id,
                    "order_url": f"/head-manager/fbs-new/fbs-orders/{order.id}/",
                    "product": line.order_item.product_name or "Товар",
                    "status": line.get_status_display(),
                    "barcode": line.order_item.barcode or "-",
                    "article": line.order_item.external_sku or "-",
                    "quantity": line.planned_quantity,
                    "picked_quantity": line.picked_quantity,
                    "route": route,
                    "can_remove": wave.status == WmsNewWave.STATUS_QUEUED,
                }
            )
    else:
        for link in wave.wave_orders.all():
            order = link.order
            orders.append(order)
            for item in order.items.all():
                item_rows.append(
                    {
                        "item_id": item.source_item_id or item.id,
                        "order_id": order.source_order_id or order.id,
                        "internal_order_id": order.id,
                        "order_number": order.external_order_id,
                        "order_url": f"/head-manager/fbs-new/fbs-orders/{order.id}/",
                        "product": item.product_name or "Товар",
                        "status": "Ожидает подбор" if wave.status == WmsNewWave.STATUS_QUEUED else order.get_status_display(),
                        "barcode": item.barcode or "-",
                        "article": item.external_sku or "-",
                        "quantity": item.quantity,
                        "picked_quantity": 0,
                        "route": "-",
                        "can_remove": wave.status == WmsNewWave.STATUS_QUEUED,
                    }
                )
    history = [
        {
            "created_at": event.created_at,
            "action": event.action.replace("_", " ").capitalize(),
            "actor": _user(event.actor),
        }
        for event in WmsNewEvent.objects.select_related("actor").filter(
            entity_type="wave", entity_id=wave.id
        )[:50]
    ]
    place_names = set(
        FbsWorkstation.objects.filter(is_active=True)
        .exclude(name="")
        .values_list("name", flat=True)
    )
    place_names.update(
        FbsPickingCart.objects.filter(is_active=True)
        .exclude(name="")
        .values_list("name", flat=True)
    )
    if wave.workstation_name:
        place_names.add(wave.workstation_name)
    if wave.cart_name:
        place_names.add(wave.cart_name)
    allowed_actions = {
        WmsNewWave.STATUS_QUEUED: (("collect", "К подбору"), ("cancel", "Отменить волну")),
        WmsNewWave.STATUS_IN_PROGRESS: (
            ("collect", "Продолжить подбор"),
            ("finish", "Завершить подбор по волне"),
        ),
    }
    active_allocation = (
        WmsNewWaveAllocation.objects.select_related(
            "pick_line__order", "pick_line__order_item", "pick_line__product", "box_item"
        )
        .filter(
            pick_line__wave=wave,
            status__in=(
                WmsNewWaveAllocation.STATUS_RESERVED,
                WmsNewWaveAllocation.STATUS_PICKING,
            ),
        )
        .order_by("sequence", "id")
        .first()
    )
    can_manage = bool(wave.is_manual and pick_lines)
    collection_state = "complete"
    collection_prompt = "Подбор по волне завершен. Передайте место в сборку заказов."
    if not can_manage:
        collection_state = "legacy"
        collection_prompt = (
            "Волна импортирована из старого рабочего контура. "
            "В FBS-NEW она доступна только для просмотра."
        )
    elif wave.status == WmsNewWave.STATUS_QUEUED:
        collection_state = "collection_place"
        collection_prompt = "Для начала подбора волны отсканируйте место подбора."
    elif wave.status == WmsNewWave.STATUS_IN_PROGRESS and active_allocation:
        if active_allocation.status == WmsNewWaveAllocation.STATUS_PICKING:
            collection_state = "product"
            collection_prompt = "Отсканируйте товар."
        else:
            collection_state = "source_place"
            collection_prompt = "Отсканируйте указанное место хранения."
    creation_result = (wave.source_snapshot or {}).get("creation_result") or {}
    return {
        "kind": "fbs_wave_collect" if collection_mode else "fbs_wave_detail",
        "title": f"Волны № {wave.source_batch_id or wave.id}",
        "id": wave.id,
        "display_id": wave.source_batch_id or wave.id,
        "creation_method": "Вручную",
        "status": wave.status,
        "status_label": _wave_status_label(wave),
        "statuses": WmsNewWave.STATUS_CHOICES,
        "started_at": wave.started_at,
        "completed_at": wave.completed_at,
        "place": wave.cart_name or wave.workstation_name or "Не назначено",
        "planned_orders": wave.planned_orders,
        "planned_units": wave.planned_units,
        "picked_units": wave.picked_units,
        "creator": _user(wave.created_by),
        "assigned_to": _user(wave.assigned_to),
        "place_options": tuple(sorted(place_names, key=str.casefold)),
        "wave_actions": allowed_actions.get(wave.status, ()) if can_manage else (),
        "can_manage": can_manage,
        "creation_result": creation_result,
        "has_problems": bool((wave.source_snapshot or {}).get("has_problems")),
        "collection_state": collection_state,
        "collection_prompt": collection_prompt,
        "active_allocation": active_allocation,
        "active_remaining": (
            active_allocation.reserved_quantity - active_allocation.picked_quantity
            if active_allocation else 0
        ),
        "collection_place_code": (wave.source_snapshot or {}).get("collection_place_code", ""),
        "items": item_rows,
        "orders": orders,
        "history": history,
        "back_url": "/head-manager/fbs-new/fbs-waves/",
    }


def _fbs_assembly_data(request=None) -> dict:
    params = request.GET if request is not None else {}
    try:
        session_id = int(params.get("session") or 0)
    except (TypeError, ValueError):
        session_id = 0
    places = []
    seen_place_names = set()
    for place in FbsWorkstation.objects.filter(is_active=True).order_by("name", "id"):
        if place.name.casefold() in seen_place_names:
            continue
        seen_place_names.add(place.name.casefold())
        places.append({"code": place.barcode, "name": place.name})
    for place in FbsPickingCart.objects.filter(is_active=True).order_by("name", "id"):
        if place.name.casefold() in seen_place_names:
            continue
        seen_place_names.add(place.name.casefold())
        places.append({"code": place.barcode, "name": place.name})

    active_sessions = list(
        WmsNewAssemblySession.objects.select_related("started_by")
        .filter(status=WmsNewAssemblySession.STATUS_ACTIVE)
        .order_by("-started_at", "-id")[:20]
    )
    session = None
    if session_id:
        session = (
            WmsNewAssemblySession.objects.select_related("started_by", "current_order")
            .prefetch_related("lines__wave", "lines__order", "lines__order_item")
            .filter(pk=session_id)
            .first()
        )
    if session is None:
        ready_waves = WmsNewWave.objects.filter(status=WmsNewWave.STATUS_VERIFICATION)
        return {
            "kind": "fbs_assembly_start",
            "title": "Сборка заказов",
            "places": places,
            "ready_waves": ready_waves.count(),
            "ready_items": ready_waves.aggregate(total=Sum("picked_units"))["total"] or 0,
            "active_sessions": active_sessions,
        }

    line_rows = []
    for line in session.lines.all():
        line_rows.append(
            {
                "id": line.id,
                "wave": line.wave.source_batch_id or line.wave.id,
                "order": line.order.external_order_id,
                "order_url": f"/head-manager/fbs-new/fbs-orders/{line.order_id}/",
                "product": line.order_item.product_name or "Товар",
                "barcode": line.order_item.barcode or "-",
                "article": line.order_item.external_sku or "-",
                "status": line.get_status_display(),
                "status_code": line.status,
                "assembled": line.assembled_quantity,
                "planned": line.planned_quantity,
                "tracking": line.order.tracking_number or "-",
                "problem_place": line.problem_place,
            }
        )
    awaiting_line = next(
        (
            row
            for row in line_rows
            if row["status_code"] == WmsNewAssemblyLine.STATUS_AWAITING_ORDER
        ),
        None,
    )
    return {
        "kind": "fbs_assembly_work",
        "title": "Сборка заказов",
        "session": session,
        "lines": line_rows,
        "awaiting_line": awaiting_line,
        "pending_count": sum(
            1
            for row in line_rows
            if row["status_code"]
            in (WmsNewAssemblyLine.STATUS_PENDING, WmsNewAssemblyLine.STATUS_AWAITING_ORDER)
        ),
        "assembled_count": sum(
            1 for row in line_rows if row["status_code"] == WmsNewAssemblyLine.STATUS_ASSEMBLED
        ),
        "problem_count": sum(
            1 for row in line_rows if row["status_code"] == WmsNewAssemblyLine.STATUS_PROBLEM
        ),
        "active_sessions": active_sessions,
    }


def _fbs_shipment_data(*, request=None) -> dict:
    request_get = request.GET if request is not None else {}
    order_number = str(request_get.get("order") or "").strip()
    date_field = str(request_get.get("date_field") or "created").strip()
    date_from = parse_date(str(request_get.get("date_from") or ""))
    date_to = parse_date(str(request_get.get("date_to") or ""))
    partner = str(request_get.get("partner") or "").strip()
    delivery = str(request_get.get("delivery") or "").strip()
    status = str(request_get.get("status") or "except_accepted").strip()
    try:
        page_size = int(request_get.get("page_size") or 25)
    except (TypeError, ValueError):
        page_size = 25
    if page_size not in (25, 50, 100):
        page_size = 25

    queryset = WmsNewShipment.objects.select_related("agency", "created_by").all()
    if order_number:
        queryset = queryset.filter(
            Q(shipment_orders__order__external_order_id__icontains=order_number)
            | Q(shipment_orders__order__tracking_number__icontains=order_number)
        )
    if partner.isdigit():
        queryset = queryset.filter(agency_id=int(partner))
    if delivery:
        queryset = queryset.filter(delivery_type=delivery)
    if status == "except_accepted":
        queryset = queryset.exclude(status=WmsNewShipment.STATUS_ACCEPTED)
    elif status in dict(WmsNewShipment.STATUS_CHOICES):
        queryset = queryset.filter(status=status)
    date_map = {
        "created": "source_created_at__date",
        "checked": "checked_at__date",
        "sent": "dispatched_at__date",
    }
    date_lookup = date_map.get(date_field, date_map["created"])
    if date_from:
        queryset = queryset.filter(**{f"{date_lookup}__gte": date_from})
    if date_to:
        queryset = queryset.filter(**{f"{date_lookup}__lte": date_to})
    queryset = queryset.distinct().order_by("-source_created_at", "-id")
    paginator = Paginator(queryset, page_size)
    page = paginator.get_page(request_get.get("page") or 1)
    query_values = request_get.copy() if hasattr(request_get, "copy") else {}
    if hasattr(query_values, "pop"):
        query_values.pop("page", None)
    query_prefix = urlencode(query_values, doseq=True)
    if query_prefix:
        query_prefix += "&"
    rows = [
        {
            "id": shipment.id,
            "display_id": shipment.source_batch_id or shipment.id,
            "partner": _agency(shipment.agency),
            "created_at": shipment.source_created_at or shipment.created_at,
            "delivery": shipment.delivery_type or "-",
            "integration": shipment.integration_name or "-",
            "status": shipment.get_status_display(),
            "status_code": shipment.status,
            "orders": shipment.order_count,
            "items": shipment.item_count,
            "url": f"/head-manager/fbs-new/fbs-shipments/{shipment.id}/",
        }
        for shipment in page.object_list
    ]
    return {
        "kind": "fbs_shipments_list",
        "title": "Отгрузки",
        "rows": rows,
        "page": page,
        "page_size": page_size,
        "page_sizes": (25, 50, 100),
        "query_prefix": query_prefix,
        "updated_label": "только что",
        "filters": {
            "order": order_number,
            "date_field": date_field,
            "date_from": date_from.isoformat() if date_from else "",
            "date_to": date_to.isoformat() if date_to else "",
            "partner": partner,
            "delivery": delivery,
            "status": status,
        },
        "partners": Agency.objects.filter(archived=False).order_by("agn_name", "id"),
        "deliveries": list(
            WmsNewShipment.objects.exclude(delivery_type="")
            .order_by("delivery_type")
            .values_list("delivery_type", flat=True)
            .distinct()
        ),
        "statuses": (("except_accepted", "Кроме принятых"),) + WmsNewShipment.STATUS_CHOICES,
        "overdue_count": WmsNewShipment.objects.filter(
            status__in=(WmsNewShipment.STATUS_NEW, WmsNewShipment.STATUS_CHECKING),
            source_created_at__lt=timezone.now() - timedelta(days=1),
        ).count(),
        "empty": "Отгрузки не найдены.",
    }


def build_fbs_shipment_detail_data(shipment_id: int) -> dict | None:
    shipment = (
        WmsNewShipment.objects.select_related(
            "agency", "created_by", "checked_by", "dispatched_by"
        )
        .prefetch_related(
            "boxes",
            "shipment_orders__order__items",
            "shipment_orders__box",
            "shipment_orders__verified_by",
            "services__created_by",
        )
        .filter(pk=shipment_id)
        .first()
    )
    if shipment is None:
        return None
    links = list(shipment.shipment_orders.all())
    order_rows = []
    for index, link in enumerate(links, start=1):
        order = link.order
        order_rows.append(
            {
                "index": index,
                "link_id": link.id,
                "order_id": order.id,
                "external_order_id": order.external_order_id,
                "sticker": link.sticker_number or order.tracking_number or "-",
                "created_at": order.source_created_at or order.created_at,
                "items": sum(max(1, int(item.quantity or 1)) for item in order.items.all()),
                "weight": link.weight_kg,
                "volume": link.volume_l,
                "total": link.order_total,
                "status": order.get_status_display(),
                "verification": link.get_verification_status_display(),
                "verification_code": link.verification_status,
                "box_id": link.box_id,
                "in_supply": link.in_supply,
                "marketplace_state": link.marketplace_state,
                "url": f"/head-manager/fbs-new/fbs-orders/{order.id}/",
            }
        )
    boxes = []
    for box in shipment.boxes.all():
        boxes.append(
            {
                "id": box.id,
                "display_id": box.external_box_id or box.source_box_id or box.id,
                "qr_code": box.qr_code,
                "status": box.status,
                "orders": [row for row in order_rows if row["box_id"] == box.id],
            }
        )
    unboxed = [row for row in order_rows if row["box_id"] is None]
    active_conflicts = WmsNewShipmentOrder.objects.filter(
        shipment__status__in=(
            WmsNewShipment.STATUS_NEW,
            WmsNewShipment.STATUS_CHECKING,
            WmsNewShipment.STATUS_CHECKED,
            WmsNewShipment.STATUS_IN_TRANSIT,
        )
    ).exclude(shipment=shipment).values("order_id")
    eligible_orders = list(
        WmsNewOrder.objects.filter(
            agency_id=shipment.agency_id,
            status=WmsNewOrder.STATUS_READY,
        )
        .exclude(id__in=[link.order_id for link in links])
        .exclude(id__in=active_conflicts)
        .order_by("source_created_at", "id")[:200]
    )
    allowed_actions = {
        WmsNewShipment.STATUS_NEW: (("start_check", "Проверить отгрузку"),),
        WmsNewShipment.STATUS_CHECKING: (("finish_check", "Завершить проверку"),),
        WmsNewShipment.STATUS_CHECKED: (("send", "Отправить отгрузку"),),
        WmsNewShipment.STATUS_IN_TRANSIT: (
            ("accept", "Принята маркетплейсом"),
            ("partial", "Принята частично"),
            ("reject", "Отклонена маркетплейсом"),
        ),
    }
    history = list(
        WmsNewEvent.objects.filter(entity_type="shipment", entity_id=shipment.id)
        .select_related("actor")
        .order_by("-created_at", "-id")[:50]
    )
    return {
        "kind": "fbs_shipment_detail",
        "title": f"Отгрузка #{shipment.source_batch_id or shipment.id}",
        "id": shipment.id,
        "display_id": shipment.source_batch_id or shipment.id,
        "shipment": shipment,
        "status": shipment.get_status_display(),
        "partner": _agency(shipment.agency),
        "creator": _user(shipment.created_by),
        "checker": _user(shipment.checked_by),
        "dispatcher": _user(shipment.dispatched_by),
        "boxes": boxes,
        "unboxed": unboxed,
        "orders": order_rows,
        "eligible_orders": eligible_orders,
        "shipment_actions": allowed_actions.get(shipment.status, ()),
        "services": [
            {
                "id": service.id,
                "name": service.name,
                "unit_price": service.unit_price,
                "quantity": service.quantity,
                "total": service.total,
            }
            for service in shipment.services.all()
        ],
        "services_total": sum((service.total for service in shipment.services.all()), Decimal("0")),
        "history": history,
        "can_edit": shipment.status in (WmsNewShipment.STATUS_NEW, WmsNewShipment.STATUS_CHECKING),
        "back_url": "/head-manager/fbs-new/fbs-shipments/",
    }


def _fbs_return_data(*, request=None) -> dict:
    request_get = request.GET if request is not None else {}
    partner = str(request_get.get("partner") or "").strip()
    delivery = str(request_get.get("delivery") or "").strip()
    date_field = str(request_get.get("date_field") or "created").strip()
    date_from = parse_date(str(request_get.get("date_from") or ""))
    date_to = parse_date(str(request_get.get("date_to") or ""))
    try:
        page_size = int(request_get.get("page_size") or 25)
    except (TypeError, ValueError):
        page_size = 25
    if page_size not in (25, 50, 100, 200, 500):
        page_size = 25
    queryset = WmsNewReturn.objects.select_related("order__agency").all()
    if partner.isdigit():
        queryset = queryset.filter(order__agency_id=int(partner))
    if delivery:
        queryset = queryset.filter(order__delivery_type=delivery)
    date_map = {
        "created": "source_created_at__date",
        "shipping": "shipped_at__date",
        "returned": "returned_at__date",
    }
    lookup = date_map.get(date_field, date_map["created"])
    if date_from:
        queryset = queryset.filter(**{f"{lookup}__gte": date_from})
    if date_to:
        queryset = queryset.filter(**{f"{lookup}__lte": date_to})
    queryset = queryset.order_by("-source_created_at", "-id")
    paginator = Paginator(queryset, page_size)
    page = paginator.get_page(request_get.get("page") or 1)
    query_values = request_get.copy() if hasattr(request_get, "copy") else {}
    if hasattr(query_values, "pop"):
        query_values.pop("page", None)
    query_prefix = urlencode(query_values, doseq=True)
    if query_prefix:
        query_prefix += "&"
    rows = []
    for item in page.object_list:
        order = item.order
        rows.append(
            {
                "id": item.id,
                "display_id": item.source_request_id or item.id,
                "order_id": order.source_order_id or order.id,
                "number": order.external_order_id,
                "partner": _agency(order.agency),
                "created_at": item.source_created_at or item.created_at,
                "shipped_at": item.shipped_at,
                "returned_at": item.returned_at,
                "delivery": order.delivery_type or "-",
                "tracking": order.tracking_number or "-",
                "task_number": item.source_request_id or item.id,
                "task_status": item.get_status_display(),
                "status_code": item.status,
                "url": f"/head-manager/fbs-new/fbs-returns/{item.id}/",
            }
        )
    return {
        "kind": "fbs_returns_list",
        "title": "Возвраты",
        "rows": rows,
        "page": page,
        "page_size": page_size,
        "page_sizes": (25, 50, 100, 200, 500),
        "query_prefix": query_prefix,
        "updated_label": "только что",
        "partners": Agency.objects.filter(archived=False).order_by("agn_name", "id"),
        "deliveries": list(
            WmsNewOrder.objects.exclude(delivery_type="")
            .order_by("delivery_type")
            .values_list("delivery_type", flat=True)
            .distinct()
        ),
        "filters": {
            "partner": partner,
            "delivery": delivery,
            "date_field": date_field,
            "date_from": date_from.isoformat() if date_from else "",
            "date_to": date_to.isoformat() if date_to else "",
        },
        "empty": "Данных не найдено",
    }


def _warehouse_bundle_data(*, request=None) -> dict:
    request_get = request.GET if request is not None else {}
    mode = str(request_get.get("mode") or "assemble").strip()
    if mode not in {"assemble", "disassemble", "create"}:
        mode = "assemble"
    partner = str(request_get.get("partner") or "").strip()
    bundle_id = str(request_get.get("bundle") or "").strip()
    create_step = str(request_get.get("step") or "select").strip()
    products = WmsNewProduct.objects.none()
    if partner.isdigit():
        products = WmsNewProduct.objects.filter(
            agency_id=int(partner), is_archived=False
        ).select_related("agency")
    else:
        partner = ""
    bundles = list(
        products.filter(is_bundle=True)
        .prefetch_related("bundle_components__component")
        .order_by("agency__agn_name", "name", "id")
    )
    regular_products = list(
        products.filter(is_bundle=False).order_by("name", "id")[:1000]
    )
    selected_bundle = next(
        (item for item in bundles if str(item.id) == bundle_id),
        None,
    )
    selected_product = next(
        (item for item in regular_products if str(item.id) == bundle_id),
        None,
    )
    if mode == "create":
        selected_bundle = None
        if create_step != "components" or selected_product is None:
            create_step = "select"
    else:
        selected_product = None
        create_step = "select"
    if selected_bundle is None and mode != "create":
        bundle_id = ""
    if selected_product is None and mode == "create":
        bundle_id = ""
    stocks = []
    if mode == "disassemble" and selected_bundle is not None:
        stocks = list(
            WmsNewBundleStock.objects.filter(
                bundle=selected_bundle,
                quantity__gt=0,
            )
            .select_related("bundle", "location", "bundle__agency")
            .order_by("location__location_code")
        )
    operations = list(
        WmsNewBundleOperation.objects.select_related("bundle", "location", "actor")
        .order_by("-created_at", "-id")[:25]
    )
    return {
        "kind": "warehouse_bundles",
        "title": "Наборы",
        "mode": mode,
        "partner": partner,
        "bundle_id": bundle_id,
        "selected_bundle": selected_bundle,
        "selected_product": selected_product,
        "create_step": create_step,
        "partners": Agency.objects.filter(archived=False).order_by("agn_name", "id"),
        "bundles": bundles,
        "products": regular_products,
        "locations": WarehouseLocation.objects.filter(is_active=True).order_by(
            "zone_code", "row_no", "section_no", "tier_no", "cell_no", "id"
        ),
        "stocks": stocks,
        "operations": operations,
    }


def _warehouse_movement_data(*, request=None) -> dict:
    params = request.GET if request is not None else {}
    mode = str(params.get("mode") or "product").strip()
    if mode not in {"product", "all"}:
        mode = "product"
    partner = str(params.get("partner") or "").strip()
    product_id = str(params.get("product") or "").strip()
    source_id = str(params.get("source") or "").strip()
    products = WmsNewProduct.objects.none()
    if partner.isdigit():
        products = WmsNewProduct.objects.filter(
            agency_id=int(partner), is_archived=False, stock_on_hand__gt=0
        ).select_related("agency")
    else:
        partner = ""
    source_locations = WarehouseLocation.objects.none()
    if mode == "all":
        source_locations = WarehouseLocation.objects.filter(
            is_active=True,
        ).order_by("zone_code", "location_code", "id")
    elif product_id.isdigit():
        source_locations = (
            WarehouseLocation.objects.filter(
                is_active=True,
                wms_new_boxes__status=WmsNewBox.STATUS_ACTIVE,
                wms_new_boxes__items__product_id=int(product_id),
                wms_new_boxes__items__qty__gt=0,
            )
            .distinct()
            .order_by("zone_code", "location_code", "id")
        )
    source_selected = bool(
        source_id.isdigit() and source_locations.filter(id=int(source_id)).exists()
    )
    if not source_selected:
        source_id = ""
    boxes = WmsNewBox.objects.none()
    if product_id.isdigit() and source_selected:
        boxes = (
            WmsNewBox.objects.filter(
                status=WmsNewBox.STATUS_ACTIVE,
                location_id=int(source_id),
                items__product_id=int(product_id),
                items__qty__gt=0,
            )
            .select_related("agency", "location")
            .distinct()
            .order_by("code", "id")
        )
    all_stock_groups = []
    if mode == "all" and source_selected:
        stock_rows = list(
            WmsNewBoxItem.objects.filter(
                box__location_id=int(source_id),
                box__status=WmsNewBox.STATUS_ACTIVE,
                qty__gt=0,
            )
            .values(
                "agency_id",
                "agency__agn_name",
                "product_id",
                "product__name",
                "product__article",
                "product_name",
                "size",
            )
            .annotate(qty=Sum("qty"))
            .order_by("agency__agn_name", "product__name", "product_name", "size")
        )
        grouped = {}
        for row in stock_rows:
            agency_id = row["agency_id"]
            group = grouped.setdefault(
                agency_id,
                {
                    "agency": row["agency__agn_name"] or "Партнер не указан",
                    "products": [],
                },
            )
            product_name = row["product__name"] or row["product_name"] or "Товар"
            size = str(row["size"] or "").strip()
            if size and size.lower() not in product_name.lower():
                product_name = f"{product_name} / {size}"
            group["products"].append(
                {
                    "name": product_name,
                    "article": row["product__article"] or "",
                    "qty": int(row["qty"] or 0),
                }
            )
        all_stock_groups = list(grouped.values())
    return {
        "kind": "warehouse_movement",
        "title": "Перемещение",
        "mode": mode,
        "filters": {
            "partner": partner,
            "product": product_id if product_id.isdigit() else "",
            "source": source_id,
        },
        "source_selected": source_selected,
        "partners": Agency.objects.filter(archived=False).order_by("agn_name", "id"),
        "products": products.order_by("name", "article", "id")[:2000],
        "source_locations": source_locations[:3000],
        "target_locations": WarehouseLocation.objects.filter(is_active=True).order_by(
            "zone_code", "location_code", "id"
        )[:3000],
        "boxes": boxes[:1000],
        "all_stock_groups": all_stock_groups,
    }


def _warehouse_history_data(*, request=None) -> dict:
    params = request.GET if request is not None else {}
    query = str(params.get("q") or "").strip()
    partner = str(params.get("partner") or "").strip()
    action = str(params.get("action") or "").strip()
    try:
        page_size = int(params.get("page_size") or 100)
    except (TypeError, ValueError):
        page_size = 100
    if page_size not in (25, 50, 100, 200, 500):
        page_size = 100
    queryset = WmsNewMovement.objects.select_related(
        "agency", "product", "source_location", "target_location", "actor"
    )
    if query:
        search = (
            Q(product_name__icontains=query)
            | Q(article__icontains=query)
            | Q(product__barcode__icontains=query)
        )
        if query.isdigit():
            search |= Q(id=int(query))
        queryset = queryset.filter(search)
    if partner.isdigit():
        queryset = queryset.filter(agency_id=int(partner))
    else:
        partner = ""
    if action in dict(WmsNewMovement.ACTION_CHOICES):
        queryset = queryset.filter(action=action)
    else:
        action = ""
    page = Paginator(queryset.order_by("-occurred_at", "-id"), page_size).get_page(
        params.get("page") or 1
    )
    query_values = {
        key: value
        for key, value in {
            "q": query,
            "partner": partner,
            "action": action,
            "page_size": page_size,
        }.items()
        if value not in ("", None)
    }
    query_prefix = urlencode(query_values)
    if query_prefix:
        query_prefix += "&"
    return {
        "kind": "warehouse_history",
        "title": "История движений",
        "rows": [
            {
                "name": item.product_name or (item.product.name if item.product else "-"),
                "partner": _agency(item.agency),
                "article": item.article or (item.product.article if item.product else "-"),
                "occurred_at": item.occurred_at,
                "source": item.source_location_name or "-",
                "target": item.target_location_name or "-",
                "quantity": item.quantity,
                "balance": item.balance_after,
                "information": item.information or item.get_action_display(),
                "actor": item.actor_name or _user(item.actor),
                "action": item.action,
            }
            for item in page.object_list
        ],
        "page": page,
        "page_size": page_size,
        "page_sizes": (25, 50, 100, 200, 500),
        "page_numbers": page.paginator.get_elided_page_range(
            page.number, on_each_side=2, on_ends=1
        ),
        "query_prefix": query_prefix,
        "partners": Agency.objects.filter(archived=False).order_by("agn_name", "id"),
        "action_choices": WmsNewMovement.ACTION_CHOICES,
        "history_columns": (
            ("name", "Название"),
            ("partner", "Партнер"),
            ("article", "Артикул"),
            ("occurred", "Дата операции"),
            ("source", "Место Источник"),
            ("target", "Место размещения"),
            ("quantity", "Кол-во"),
            ("balance", "Остаток"),
            ("information", "Информация"),
            ("actor", "Пользователь"),
        ),
        "filters": {"q": query, "partner": partner, "action": action},
    }


def _warehouse_data() -> dict:
    stock = WarehouseStockSnapshot.objects.filter(qty__gt=0, is_archived=False).aggregate(
        qty=Sum("qty"), available=Sum("available_qty")
    )
    active_statuses = (
        WarehouseOperation.STATUS_CREATED,
        WarehouseOperation.STATUS_PLANNED,
        WarehouseOperation.STATUS_IN_PROGRESS,
        WarehouseOperation.STATUS_PARTIAL,
        WarehouseOperation.STATUS_BLOCKED,
    )
    active = WarehouseOperation.objects.filter(status__in=active_statuses)
    rows = [
        {
            "cells": (
                f"#{item.id}",
                _display(item, "operation_type"),
                _agency(item.agency),
                getattr(item.source_location, "location_code", "") or item.source_zone_code or "-",
                getattr(item.destination_location, "location_code", "") or item.destination_zone_code or "-",
                _display(item, "status"),
                f"{item.done_qty}/{item.planned_qty}",
                _dt(item.updated_at),
            ),
            "url": "/sklad/journal/",
        }
        for item in active.select_related(
            "agency", "source_location", "destination_location"
        ).order_by("-updated_at", "-id")[:25]
    ]
    return _table(
        title="Активные складские операции",
        columns=("Операция", "Тип", "Клиент", "Откуда", "Куда", "Статус", "Выполнено", "Обновлено"),
        rows=rows,
        summary=(
            _summary("Физический остаток", stock.get("qty") or 0),
            _summary("Доступный остаток", stock.get("available") or 0),
            _summary("Активные операции", active.count()),
            _summary("Места хранения", WarehouseLocation.objects.filter(is_active=True).count()),
            _summary("Активные контейнеры", WarehouseContainer.objects.exclude(status="archived").count()),
        ),
        actions=(
            _action("Остатки", "/head-manager/stock-editor/", primary=True),
            _action("Карта склада", "/stockmap/"),
            _action("Журнал склада", "/sklad/journal/"),
            _action("Инвентаризации", "/inventory/"),
        ),
    )


def _warehouse_goods_data(request=None) -> dict:
    params = request.GET if request is not None else {}
    filters = {
        "q": str(params.get("q") or "").strip(),
        "partner": str(params.get("partner") or "").strip(),
        "category": str(params.get("category") or "").strip(),
        "bundle": str(params.get("bundle") or "all").strip(),
        "nonzero": str(params.get("nonzero") or "").strip(),
        "reserved": str(params.get("reserved") or "").strip(),
    }
    queryset = WmsNewProduct.objects.filter(is_archived=False).select_related("agency")
    if filters["q"]:
        query = (
            Q(name__icontains=filters["q"])
            | Q(article__icontains=filters["q"])
            | Q(barcode__icontains=filters["q"])
        )
        if filters["q"].isdigit():
            query |= Q(pk=int(filters["q"]))
        queryset = queryset.filter(query)
    if filters["partner"].isdigit():
        queryset = queryset.filter(agency_id=int(filters["partner"]))
    else:
        filters["partner"] = ""
    if filters["category"]:
        queryset = queryset.filter(category=filters["category"])
    if filters["bundle"] == "without":
        queryset = queryset.filter(is_bundle=False)
    elif filters["bundle"] == "only":
        queryset = queryset.filter(is_bundle=True)
    else:
        filters["bundle"] = "all"
    if filters["nonzero"]:
        filters["nonzero"] = "1"
        queryset = queryset.filter(stock_on_hand__gt=0)
    if filters["reserved"]:
        filters["reserved"] = "1"
        queryset = queryset.filter(
            Q(fbo_reserved__gt=0) | Q(fbs_reserved__gt=0) | Q(internal_reserved__gt=0)
        )

    try:
        page_size = int(params.get("page_size") or 100)
    except (TypeError, ValueError):
        page_size = 100
    if page_size not in (25, 50, 100, 200, 500):
        page_size = 100
    page = Paginator(queryset.order_by("id"), page_size).get_page(params.get("page"))
    rows = [
        {
            "id": product.id,
            "image_url": product.image_url,
            "name": product.name,
            "partner": _agency(product.agency),
            "article": product.article,
            "barcode": product.barcode or "-",
            "marking_count": product.marking_count,
            "weight": format(product.weight_grams.normalize(), "f"),
            "size": product.size or "",
            "dimensions": product.dimensions,
            "stock_on_hand": product.stock_on_hand,
            "stock_free": product.stock_free,
            "fbo_reserved": product.fbo_reserved,
            "fbs_reserved": product.fbs_reserved,
            "expected_qty": product.expected_qty,
            "category": product.category,
            "is_bundle": product.is_bundle,
            "notes": product.internal_notes or "",
            "url": f"/head-manager/fbs-new/warehouse-goods/?product_id={product.id}",
        }
        for product in page.object_list
    ]
    query_values = {**filters, "page_size": page_size}
    query_values = {
        key: value
        for key, value in query_values.items()
        if value not in ("", None, "all")
    }
    query_prefix = urlencode(query_values)
    if query_prefix:
        query_prefix += "&"

    selected_product = None
    selected_id = str(params.get("product_id") or "").strip()
    if selected_id.isdigit():
        product = (
            WmsNewProduct.objects.select_related("agency")
            .filter(pk=int(selected_id), is_archived=False)
            .first()
        )
        if product:
            product_tabs = (
                ("overview", "О товаре"),
                ("attributes", "Характеристики"),
                ("locations", "Места хранения"),
                ("barcodes", "ШК и артикулы"),
                ("services", "Услуги по умолчанию"),
                ("orders", "Заказы"),
                ("files", "Файлы"),
                ("marking", "Коды маркировки"),
                ("extra", "Доп. поля"),
                ("requirement", "Типовое ТЗ"),
            )
            product_tab = str(params.get("product_tab") or "overview").strip()
            if product_tab not in {item[0] for item in product_tabs}:
                product_tab = "overview"
            location_rows = [
                {
                    "box": item.box.code,
                    "location": str(item.box.location or item.box.location_code or "-"),
                    "zone": item.box.zone_code or "-",
                    "qty": item.qty,
                    "available": item.available_qty,
                    "reserved": item.reserved_qty,
                    "updated_at": item.source_updated_at or item.updated_at,
                }
                for item in WmsNewBoxItem.objects.filter(product=product, qty__gt=0)
                .select_related("box__location")
                .order_by("box__location_code", "box__code", "id")[:500]
            ]
            barcode_values = []
            if product.barcode:
                barcode_values.append({"value": product.barcode, "source": "Карточка товара"})
            known_barcodes = {product.barcode} if product.barcode else set()
            for value in (
                WmsNewBoxItem.objects.filter(product=product)
                .exclude(barcode="")
                .values_list("barcode", flat=True)
                .distinct()
                .order_by("barcode")
            ):
                if value not in known_barcodes:
                    known_barcodes.add(value)
                    barcode_values.append({"value": value, "source": "Складской снимок"})
            order_filter = Q()
            if product.source_sku_id:
                order_filter |= Q(sku_id=product.source_sku_id)
            if product.article:
                order_filter |= Q(external_sku=product.article)
            if product.barcode:
                order_filter |= Q(barcode=product.barcode)
            order_rows = []
            if order_filter:
                order_rows = [
                    {
                        "number": item.order.external_order_id,
                        "partner": _agency(item.order.agency),
                        "marketplace": item.order.marketplace or item.order.delivery_type or "-",
                        "status": item.order.get_status_display(),
                        "quantity": item.quantity,
                        "updated_at": item.order.source_updated_at or item.order.updated_at,
                        "url": f"/head-manager/fbs-new/fbs-orders/{item.order_id}/",
                    }
                    for item in WmsNewOrderItem.objects.filter(order_filter)
                    .select_related("order__agency")
                    .order_by("-order__source_updated_at", "-order_id", "-id")[:100]
                ]
            marking_rows = [
                {
                    "code": item.code,
                    "type": item.get_code_type_display(),
                    "source": item.get_source_display(),
                    "received_at": item.received_at,
                    "retired_at": item.retired_at,
                    "printed": item.print_count,
                }
                for item in WmsNewMarkingCode.objects.filter(product=product)
                .order_by("-created_at", "-id")[:100]
            ]
            extra_rows = [
                {
                    "name": item.definition.name,
                    "type": item.definition.get_field_type_display(),
                    "value": item.value,
                    "updated_at": item.source_updated_at or item.updated_at,
                }
                for item in WmsNewExtraFieldValue.objects.filter(product=product)
                .select_related("definition")
                .order_by("definition__sort_order", "definition__name", "id")
            ]
            movement_rows = [
                {
                    "date": item.occurred_at,
                    "action": item.get_action_display(),
                    "source": str(item.source_location or item.source_location_name or "-"),
                    "target": str(item.target_location or item.target_location_name or "-"),
                    "quantity": item.quantity,
                    "balance": item.balance_after,
                    "actor": _user(item.actor) if item.actor_id else (item.actor_name or "-"),
                }
                for item in WmsNewMovement.objects.filter(product=product)
                .select_related("source_location", "target_location", "actor")
                .order_by("-occurred_at", "-id")[:100]
            ]
            source_snapshot = product.source_snapshot if isinstance(product.source_snapshot, dict) else {}
            source_services = source_snapshot.get("default_services") or source_snapshot.get("services") or []
            if not isinstance(source_services, list):
                source_services = []
            source_files = source_snapshot.get("files") or source_snapshot.get("attachments") or []
            if not isinstance(source_files, list):
                source_files = []
            selected_product = {
                "id": product.id,
                "agency_id": product.agency_id,
                "partner": _agency(product.agency),
                "name": product.name,
                "article": product.article,
                "barcode": product.barcode,
                "color": product.color,
                "weight_grams": product.weight_grams,
                "size": product.size,
                "category": product.category,
                "width_cm": product.width_cm,
                "depth_cm": product.depth_cm,
                "height_cm": product.height_cm,
                "image_url": product.image_url,
                "internal_notes": product.internal_notes,
                "description": product.description,
                "is_bundle": product.is_bundle,
                "source_sku_id": product.source_sku_id,
                "pilot_revision": product.pilot_revision,
                "tab": product_tab,
                "tabs": product_tabs,
                "location_rows": location_rows,
                "barcode_rows": barcode_values,
                "order_rows": order_rows,
                "marking_rows": marking_rows,
                "extra_rows": extra_rows,
                "movement_rows": movement_rows,
                "services": source_services,
                "files": source_files,
                "technical_requirement": (
                    source_snapshot.get("technical_requirement")
                    or source_snapshot.get("typical_requirement")
                    or product.description
                    or ""
                ),
            }

    return {
        "kind": "warehouse_goods",
        "title": "Товары",
        "filters": filters,
        "rows": rows,
        "page": page,
        "page_numbers": page.paginator.get_elided_page_range(
            page.number, on_each_side=2, on_ends=1
        ),
        "page_size": page_size,
        "page_sizes": (25, 50, 100, 200, 500),
        "query_prefix": query_prefix,
        "partners": Agency.objects.filter(archived=False).order_by("agn_name"),
        "categories": list(
            WmsNewProduct.objects.filter(is_archived=False)
            .exclude(category="")
            .values_list("category", flat=True)
            .distinct()
            .order_by("category")
        ),
        "selected_product": selected_product,
        "updated_label": _dt(
            WmsNewProduct.objects.filter(is_archived=False)
            .order_by("-last_synced_at")
            .values_list("last_synced_at", flat=True)
            .first()
        ),
        "total": queryset.count(),
    }


def _warehouse_acceptance_data(request=None) -> dict:
    params = request.GET if request is not None else {}
    filters = {
        "date_field": str(params.get("date_field") or "created").strip(),
        "date_from": str(params.get("date_from") or "").strip(),
        "date_to": str(params.get("date_to") or "").strip(),
        "partner": str(params.get("partner") or "").strip(),
        "status": str(params.get("status") or "").strip(),
    }
    queryset = WmsNewAcceptance.objects.select_related("agency")
    if filters["date_field"] not in {"created", "completed"}:
        filters["date_field"] = "created"
    date_field = "completed_at" if filters["date_field"] == "completed" else "source_created_at"
    date_from = parse_date(filters["date_from"])
    date_to = parse_date(filters["date_to"])
    if date_from:
        queryset = queryset.filter(**{f"{date_field}__date__gte": date_from})
    if date_to:
        queryset = queryset.filter(**{f"{date_field}__date__lte": date_to})
    if filters["partner"].isdigit():
        queryset = queryset.filter(agency_id=int(filters["partner"]))
    else:
        filters["partner"] = ""
    if filters["status"] in dict(WmsNewAcceptance.STATUS_CHOICES):
        queryset = queryset.filter(status=filters["status"])
    else:
        filters["status"] = ""
    page = Paginator(queryset.order_by("-source_created_at", "-id"), 100).get_page(
        params.get("page")
    )
    rows = [
        {
            "id": item.id,
            "partner": _agency(item.agency),
            "task_number": item.task_number or item.source_order_key,
            "title": item.title,
            "received_qty": item.received_qty,
            "expected_qty": item.expected_qty,
            "progress": min(
                100,
                round(item.received_qty * 100 / (item.expected_qty or item.received_qty or 1)),
            ),
            "type": item.get_acceptance_type_display(),
            "created_at": item.source_created_at,
            "completed_at": item.completed_at,
            "status": item.get_status_display(),
            "url": f"/head-manager/fbs-new/warehouse-acceptances/?acceptance_id={item.id}",
        }
        for item in page.object_list
    ]
    selected = None
    selected_items = []
    selected_id = str(params.get("acceptance_id") or "").strip()
    if selected_id.isdigit():
        selected = (
            WmsNewAcceptance.objects.select_related("agency")
            .filter(pk=int(selected_id))
            .first()
        )
    if selected:
        payload = (selected.source_snapshot or {}).get("payload") or {}
        payload_rows = []
        for key in ("act_items", "items", "products", "goods"):
            value = payload.get(key)
            if isinstance(value, list):
                payload_rows = [item for item in value if isinstance(item, dict)]
                if payload_rows:
                    break
        for index, item in enumerate(payload_rows, start=1):
            expected = item.get("planned_qty", item.get("expected_qty", item.get("quantity", item.get("qty", 0))))
            received = item.get("actual_qty", item.get("received_qty", item.get("accepted_qty", item.get("qty", 0))))
            selected_items.append({
                "id": item.get("id") or index,
                "name": item.get("name") or item.get("product_name") or item.get("title") or "Товар",
                "article": item.get("article") or item.get("sku_code") or item.get("vendor_code") or "-",
                "barcode": item.get("barcode") or item.get("bar_code") or "-",
                "expected": expected or 0,
                "received": received or 0,
                "marking": item.get("marking_code") or item.get("kiz") or "-",
                "place": item.get("location") or item.get("place") or item.get("box") or "-",
            })
    selected_tab = str(params.get("tab") or "info").strip()
    if selected_tab not in {"info", "goods", "marking", "acceptance"}:
        selected_tab = "info"
    query_values = {key: value for key, value in filters.items() if value}
    query_prefix = urlencode(query_values)
    if query_prefix:
        query_prefix += "&"
    return {
        "kind": "warehouse_acceptances",
        "title": "Приемки",
        "filters": filters,
        "rows": rows,
        "page": page,
        "page_numbers": page.paginator.get_elided_page_range(
            page.number, on_each_side=3, on_ends=1
        ),
        "query_prefix": query_prefix,
        "partners": Agency.objects.filter(archived=False).order_by("agn_name"),
        "statuses": WmsNewAcceptance.STATUS_CHOICES,
        "selected_acceptance": selected,
        "selected_items": selected_items,
        "selected_tab": selected_tab,
    }


def _warehouse_marking_data(request=None) -> dict:
    params = request.GET if request is not None else {}
    filters = {
        "q": str(params.get("q") or "").strip(),
        "partner": str(params.get("partner") or "").strip(),
        "product": str(params.get("product") or "").strip(),
    }
    queryset = WmsNewMarkingCode.objects.select_related("agency", "product")
    if filters["q"]:
        queryset = queryset.filter(code__icontains=filters["q"])
    if filters["partner"].isdigit():
        queryset = queryset.filter(agency_id=int(filters["partner"]))
    else:
        filters["partner"] = ""
    if filters["product"].isdigit():
        queryset = queryset.filter(product_id=int(filters["product"]))
    else:
        filters["product"] = ""

    page_size = 100
    page = Paginator(queryset.order_by("-created_at", "-id"), page_size).get_page(
        params.get("page")
    )
    rows = [
        {
            "id": item.id,
            "partner": _agency(item.agency),
            "product": item.product_name or (item.product.name if item.product else "-"),
            "article": item.article,
            "barcode": item.barcode,
            "type": item.get_code_type_display(),
            "code": item.code,
            "received_at": item.received_at,
            "received_reference": item.received_reference,
            "retired_at": item.retired_at,
            "retired_reference": item.retired_reference,
            "printed_at": item.printed_at,
            "print_count": item.print_count,
            "file_name": item.file_name,
            "processed_at": item.processed_at,
            "is_returned": item.is_returned,
            "created_at": item.created_at,
        }
        for item in page.object_list
    ]
    query_values = {key: value for key, value in filters.items() if value}
    query_prefix = urlencode(query_values)
    if query_prefix:
        query_prefix += "&"
    products = WmsNewProduct.objects.filter(is_archived=False)
    if filters["partner"].isdigit():
        products = products.filter(agency_id=int(filters["partner"]))
    return {
        "kind": "warehouse_marking",
        "title": "Коды маркировки",
        "filters": filters,
        "rows": rows,
        "page": page,
        "page_numbers": page.paginator.get_elided_page_range(
            page.number, on_each_side=2, on_ends=1
        ),
        "query_prefix": query_prefix,
        "partners": Agency.objects.filter(archived=False).order_by("agn_name"),
        "products": products.select_related("agency").order_by("name", "id"),
        "code_types": WmsNewMarkingCode.TYPE_CHOICES,
        "updated_label": _dt(
            WmsNewMarkingCode.objects.order_by("-last_synced_at")
            .values_list("last_synced_at", flat=True)
            .first()
        ),
    }


def _warehouse_extra_fields_data(request=None) -> dict:
    params = request.GET if request is not None else {}
    filters = {
        "q": str(params.get("q") or "").strip(),
        "field": str(params.get("field") or "").strip(),
        "partner": str(params.get("partner") or "").strip(),
        "filling": str(params.get("filling") or "filled").strip(),
        "representation": str(params.get("representation") or "values").strip(),
    }
    if filters["filling"] not in {"filled", "all", "empty"}:
        filters["filling"] = "filled"
    if filters["representation"] not in {"values", "instances"}:
        filters["representation"] = "values"
    if not filters["field"].isdigit():
        filters["field"] = ""
    if not filters["partner"].isdigit():
        filters["partner"] = ""
    try:
        page_size = int(params.get("page_size") or 50)
    except (TypeError, ValueError):
        page_size = 50
    if page_size not in {25, 50, 100, 200}:
        page_size = 50

    definition_queryset = WmsNewExtraFieldDefinition.objects.annotate(
        values_count=Count("values"),
        products_count=Count("values__product", distinct=True),
    ).order_by("sort_order", "name", "id")
    definitions = list(definition_queryset)
    definition_by_id = {item.id: item for item in definitions}
    selected_filter_definition = definition_by_id.get(int(filters["field"])) if filters["field"] else None

    value_queryset = WmsNewExtraFieldValue.objects.select_related(
        "definition", "product", "product__agency"
    )
    product_queryset = WmsNewProduct.objects.filter(is_archived=False).select_related("agency")
    if selected_filter_definition:
        value_queryset = value_queryset.filter(definition=selected_filter_definition)
    if filters["partner"]:
        value_queryset = value_queryset.filter(product__agency_id=int(filters["partner"]))
        product_queryset = product_queryset.filter(agency_id=int(filters["partner"]))
    if filters["q"]:
        q = filters["q"]
        product_queryset = product_queryset.filter(
            Q(name__icontains=q) | Q(article__icontains=q) | Q(barcode__icontains=q)
        )

    actual_values = list(value_queryset.order_by("definition__sort_order", "product__name", "id")[:20000])
    if filters["q"]:
        needle = filters["q"].casefold()
        actual_values = [
            item
            for item in actual_values
            if needle
            in " ".join(
                (
                    item.product.name or "",
                    item.product.article or "",
                    item.product.barcode or "",
                    item.definition.name or "",
                    str(item.value or ""),
                )
            ).casefold()
        ]

    def value_is_filled(value) -> bool:
        return value not in (None, "", [], {})

    def value_row(*, definition, product, value_item=None) -> dict:
        value = value_item.value if value_item else None
        snapshot = value_item.source_snapshot if value_item and isinstance(value_item.source_snapshot, dict) else {}
        acceptance = (
            snapshot.get("acceptance_number")
            or snapshot.get("acceptance")
            or snapshot.get("received_reference")
            or "—"
        )
        if isinstance(value, bool):
            display_value = "Да" if value else "Нет"
        else:
            display_value = value if value_is_filled(value) else "—"
        return {
            "id": value_item.id if value_item else None,
            "partner": _agency(product.agency),
            "product": product.name,
            "article": product.article,
            "field": definition.name,
            "value": display_value,
            "acceptance": acceptance,
            "updated_at": (
                value_item.source_updated_at or value_item.updated_at if value_item else None
            ),
            "status": "Заполнено" if value_is_filled(value) else "Пусто",
            "is_filled": value_is_filled(value),
        }

    if filters["filling"] == "filled":
        rows = [
            value_row(definition=item.definition, product=item.product, value_item=item)
            for item in actual_values
            if value_is_filled(item.value)
        ]
    else:
        candidate_definitions = (
            [selected_filter_definition]
            if selected_filter_definition
            else [item for item in definitions if item.is_active]
        )
        candidate_products = list(product_queryset.order_by("agency__agn_name", "name", "id")[:5000])
        actual_map = {
            (item.definition_id, item.product_id): item
            for item in actual_values
        }
        rows = []
        for definition in candidate_definitions:
            for product in candidate_products:
                item = actual_map.get((definition.id, product.id))
                filled = bool(item and value_is_filled(item.value))
                if filters["filling"] == "empty" and filled:
                    continue
                rows.append(value_row(definition=definition, product=product, value_item=item))
                if len(rows) >= 5000:
                    break
            if len(rows) >= 5000:
                break

    export_rows = [
        (
            row["partner"],
            row["product"],
            row["article"],
            row["field"],
            row["value"],
            row["acceptance"],
            _dt(row["updated_at"]),
            row["status"],
        )
        for row in rows
    ]
    page = Paginator(rows, page_size).get_page(params.get("page"))
    selected_definition = None
    selected_values = []
    selected_id = str(params.get("field_id") or "").strip()
    if selected_id.isdigit():
        selected_definition = WmsNewExtraFieldDefinition.objects.filter(
            pk=int(selected_id)
        ).first()
    if selected_definition:
        selected_values = [
            {
                "id": item.id,
                "product": item.product.name,
                "article": item.product.article,
                "partner": _agency(item.product.agency),
                "value": item.value,
                "updated_at": item.updated_at,
            }
            for item in WmsNewExtraFieldValue.objects.filter(
                definition=selected_definition
            ).select_related("product", "product__agency").order_by("product__name", "id")[:100]
        ]
    definition_rows = []
    for item in definitions:
        options = item.options if isinstance(item.options, dict) else {}
        total = int(item.products_count or 0)
        filled = int(item.values_count or 0)
        definition_rows.append(
            {
                "id": item.id,
                "name": item.name,
                "type": item.get_field_type_display(),
                "level": options.get("level") or "Товар",
                "required": "Да" if options.get("required") else "Нет",
                "unique": options.get("unique") or "Нет",
                "marking": "Да" if options.get("marking") else "Нет",
                "generation": options.get("generation") or "Ручная",
                "filled": filled,
                "products": total,
                "percent": f"{round(filled * 100 / total)}%" if total else "—",
                "status": "Вкл" if item.is_active else "Выкл",
                "url": f"/head-manager/fbs-new/warehouse-extra-fields/?field_id={item.id}&fields=1",
            }
        )
    query_values = {key: value for key, value in filters.items() if value}
    query_values["page_size"] = page_size
    query_prefix = urlencode(query_values)
    if query_prefix:
        query_prefix += "&"
    return {
        "kind": "warehouse_extra_fields",
        "title": "Дополнительные поля",
        "filters": filters,
        "rows": page.object_list,
        "page": page,
        "page_numbers": page.paginator.get_elided_page_range(
            page.number, on_each_side=2, on_ends=1
        ),
        "query_prefix": query_prefix,
        "page_size": page_size,
        "page_sizes": (25, 50, 100, 200),
        "definitions": definitions,
        "definition_rows": definition_rows,
        "field_types": WmsNewExtraFieldDefinition.TYPE_CHOICES,
        "fields_open": str(params.get("fields") or "") == "1",
        "add_open": str(params.get("add") or "") == "1",
        "selected_definition": selected_definition,
        "selected_values": selected_values,
        "export_columns": (
            "Партнёр",
            "Товар",
            "Артикул",
            "Поле",
            "Значение",
            "Приёмка",
            "Дата",
            "Статус",
        ),
        "export_rows": export_rows,
        "representation_note": (
            "В FBS-NEW дополнительные значения привязаны к карточке товара; "
            "экземплярный режим показывает доступные фактические записи."
            if filters["representation"] == "instances"
            else ""
        ),
        "products": WmsNewProduct.objects.filter(is_archived=False)
        .select_related("agency")
        .order_by("name", "id"),
        "partners": Agency.objects.filter(archived=False).order_by("agn_name", "id"),
    }


def _inventory_scope_target(item: WmsNewInventorySession) -> str:
    if item.scope_type == WmsNewInventorySession.SCOPE_AGENCY:
        return _agency(item.agency)
    if item.scope_type == WmsNewInventorySession.SCOPE_CELL:
        return item.cell.warehouse_location_label if item.cell else "-"
    if item.scope_type == WmsNewInventorySession.SCOPE_PALLET:
        return item.pallet.pallet_code if item.pallet else "-"
    if item.scope_type == WmsNewInventorySession.SCOPE_BOX:
        return item.box.box_code if item.box else "-"
    if item.scope_type == WmsNewInventorySession.SCOPE_PRODUCT:
        return (
            f"{item.product.article} · {item.product.name}"
            if item.product
            else "-"
        )
    return "Весь склад"


def _inventory_type_label(item: WmsNewInventorySession) -> str:
    return {
        WmsNewInventorySession.SCOPE_ALL: "полная",
        WmsNewInventorySession.SCOPE_AGENCY: "по партнеру",
        WmsNewInventorySession.SCOPE_PRODUCT: "по товару",
        WmsNewInventorySession.SCOPE_CELL: "по местам",
        WmsNewInventorySession.SCOPE_PALLET: "по местам",
        WmsNewInventorySession.SCOPE_BOX: "по местам",
    }.get(item.scope_type, item.get_scope_type_display().lower())


def _inventory_created_at(item: WmsNewInventorySession):
    snapshot = item.source_snapshot if isinstance(item.source_snapshot, dict) else {}
    source_created_at = snapshot.get("source_created_at")
    if isinstance(source_created_at, str):
        return parse_datetime(source_created_at) or item.created_at
    return item.created_at


def _warehouse_inventory_data(request=None) -> dict:
    params = request.GET if request is not None else {}
    filters = {
        "q": str(params.get("q") or "").strip(),
        "partner": str(params.get("partner") or "").strip(),
        "status": str(params.get("status") or "").strip(),
        "scope_type": str(params.get("scope_type") or "").strip(),
    }
    queryset = WmsNewInventorySession.objects.select_related(
        "agency",
        "cell__location",
        "pallet",
        "box",
        "product",
        "first_counter",
        "second_counter",
        "approved_by",
        "created_by",
    ).annotate(
        lines_count=Count("lines", distinct=True),
        discrepancy_count=Count(
            "lines",
            filter=(
                Q(lines__first_count_qty__isnull=False)
                & ~Q(lines__first_count_qty=F("lines__expected_qty"))
            ),
            distinct=True,
        ),
    )
    if filters["q"]:
        query = Q(number__icontains=filters["q"])
        query |= Q(cell__cell_code__icontains=filters["q"])
        query |= Q(pallet__pallet_code__icontains=filters["q"])
        query |= Q(box__box_code__icontains=filters["q"])
        query |= Q(product__article__icontains=filters["q"])
        query |= Q(product__name__icontains=filters["q"])
        queryset = queryset.filter(query)
    if filters["partner"].isdigit():
        queryset = queryset.filter(agency_id=int(filters["partner"]))
    else:
        filters["partner"] = ""
    if filters["status"] in dict(WmsNewInventorySession.STATUS_CHOICES):
        queryset = queryset.filter(status=filters["status"])
    else:
        filters["status"] = ""
    if filters["scope_type"] in dict(WmsNewInventorySession.SCOPE_CHOICES):
        queryset = queryset.filter(scope_type=filters["scope_type"])
    else:
        filters["scope_type"] = ""

    page = Paginator(queryset.order_by("-created_at", "-id"), 100).get_page(
        params.get("page")
    )
    rows = [
        {
            "id": item.id,
            "number": item.number,
            "type": _inventory_type_label(item),
            "partner": _agency(item.agency),
            "scope": item.get_scope_type_display(),
            "target": _inventory_scope_target(item),
            "mode": item.get_mode_display(),
            "scan_mode": item.get_scan_mode_display(),
            "status": item.get_status_display(),
            "status_value": item.status,
            "lines_count": item.lines_count,
            "discrepancy_count": item.discrepancy_count,
            "counter": (
                str(item.second_counter or item.first_counter or "-")
            ),
            "created_by": _user(item.created_by),
            "performer": _user(item.second_counter or item.first_counter or item.approved_by),
            "created_at": _inventory_created_at(item),
            "started_at": item.started_at,
            "completed_at": item.completed_at,
            "updated_at": item.updated_at,
            "url": f"/head-manager/fbs-new/warehouse-inventories/?inventory_id={item.id}",
        }
        for item in page.object_list
    ]

    selected = None
    selected_lines = []
    selected_documents = []
    selected_id = str(params.get("inventory_id") or "").strip()
    if selected_id.isdigit():
        selected = WmsNewInventorySession.objects.select_related(
            "agency",
            "cell__location",
            "pallet",
            "box",
            "product",
            "created_by",
            "first_counter",
            "second_counter",
            "approved_by",
        ).filter(pk=int(selected_id)).first()
    if selected:
        selected.scope_target = _inventory_scope_target(selected)
        selected.type_label = _inventory_type_label(selected)
        selected.source_created_at_display = _inventory_created_at(selected)
        selected.performer = selected.second_counter or selected.first_counter or selected.approved_by
        selected_lines = [
            {
                "id": line.id,
                "location": line.location_code or line.cell_code or "-",
                "box": line.box_code or "-",
                "product": line.product_name or (line.product.name if line.product else "-"),
                "article": line.sku_code,
                "barcode": line.barcode,
                "marking_code": line.marking_code,
                "expected": line.expected_qty,
                "first": line.first_count_qty,
                "second": line.second_count_qty,
                "final": line.final_qty,
                "actual": (
                    line.final_qty
                    if line.final_qty is not None
                    else line.second_count_qty
                    if line.second_count_qty is not None
                    else line.first_count_qty
                ),
                "delta": line.approved_delta,
                "needs_final": (
                    line.second_count_qty is not None
                    and line.first_count_qty != line.second_count_qty
                ),
            }
            for line in WmsNewInventoryLine.objects.filter(session=selected)
            .select_related("product")
            .order_by("location_code", "box_code", "sku_code", "id")[:500]
        ]
        for line in selected_lines:
            if line["actual"] is not None:
                line["result_delta"] = int(line["actual"]) - int(line["expected"])
            else:
                line["result_delta"] = None
        related_documents = WmsNewDocument.objects.select_related("agency").filter(
            Q(basis__icontains=selected.number)
            | Q(source_key__icontains=selected.number)
            | Q(comment__icontains=selected.number)
        )
        if selected.source_session_id:
            related_documents = related_documents | WmsNewDocument.objects.select_related(
                "agency"
            ).filter(source_snapshot__inventory_id=selected.source_session_id)
        selected_documents = [
            {
                "id": item.id,
                "type": item.get_document_type_display(),
                "partner": _agency(item.agency),
                "status": item.get_status_display(),
                "url": f"/head-manager/fbs-new/documents/?document_id={item.id}",
            }
            for item in related_documents.distinct().order_by("document_type", "id")[:100]
        ]

    query_values = {key: value for key, value in filters.items() if value}
    query_prefix = urlencode(query_values)
    if query_prefix:
        query_prefix += "&"
    active_statuses = (
        WmsNewInventorySession.STATUS_DRAINING,
        WmsNewInventorySession.STATUS_COUNTING,
        WmsNewInventorySession.STATUS_RECOUNT,
        WmsNewInventorySession.STATUS_APPROVAL,
    )
    all_sessions = WmsNewInventorySession.objects.all()
    return {
        "kind": "warehouse_inventories",
        "title": f"Инвентаризация № {selected.number}" if selected else "Инвентаризации",
        "filters": filters,
        "rows": rows,
        "page": page,
        "page_numbers": page.paginator.get_elided_page_range(
            page.number, on_each_side=2, on_ends=1
        ),
        "query_prefix": query_prefix,
        "partners": Agency.objects.filter(archived=False).order_by("agn_name"),
        "cells": FbsStorageCell.objects.filter(is_active=True)
        .select_related("location")
        .order_by("cell_code"),
        "pallets": FbsPallet.objects.exclude(status=FbsPallet.STATUS_ARCHIVED)
        .select_related("agency", "cell")
        .order_by("pallet_code"),
        "boxes": FbsBox.objects.exclude(status=FbsBox.STATUS_ARCHIVED)
        .select_related("agency", "pallet")
        .order_by("box_code"),
        "products": WmsNewProduct.objects.filter(is_archived=False, stock_on_hand__gt=0)
        .select_related("agency")
        .order_by("name", "id"),
        "scope_types": WmsNewInventorySession.SCOPE_CHOICES,
        "modes": WmsNewInventorySession.MODE_CHOICES,
        "scan_modes": WmsNewInventorySession.SCAN_MODE_CHOICES,
        "statuses": WmsNewInventorySession.STATUS_CHOICES,
        "inventory_columns": (
            ("id", "ID"),
            ("type", "Тип"),
            ("status", "Статус"),
            ("creator", "Создал"),
            ("performer", "Провел"),
            ("created", "Дата создания"),
            ("started", "Дата начала"),
            ("completed", "Дата окончания"),
        ),
        "selected_inventory": selected,
        "selected_lines": selected_lines,
        "selected_documents": selected_documents,
        "export_columns": (
            "Место",
            "Товар",
            "Плановое кол-во",
            "Фактическое кол-во",
            "Разница",
        ),
        "export_rows": [
            (
                line["location"],
                line["product"],
                line["expected"],
                line["actual"] if line["actual"] is not None else "—",
                line["result_delta"] if line["result_delta"] is not None else "—",
            )
            for line in selected_lines
        ],
        "summary": {
            "total": all_sessions.count(),
            "active": all_sessions.filter(status__in=active_statuses).count(),
            "counting": all_sessions.filter(
                status__in=(
                    WmsNewInventorySession.STATUS_COUNTING,
                    WmsNewInventorySession.STATUS_RECOUNT,
                )
            ).count(),
            "approval": all_sessions.filter(
                status=WmsNewInventorySession.STATUS_APPROVAL
            ).count(),
            "done": all_sessions.filter(status=WmsNewInventorySession.STATUS_DONE).count(),
        },
    }


def _warehouse_boxes_data(request=None) -> dict:
    params = request.GET if request is not None else {}
    filters = {
        "q": str(params.get("q") or "").strip(),
        "container_type": str(params.get("container_type") or "").strip(),
        "partner": str(params.get("partner") or "").strip(),
        "barcode": str(params.get("barcode") or "").strip(),
    }
    queryset = WmsNewBox.objects.select_related("agency", "location").prefetch_related("items")
    if filters["q"]:
        queryset = queryset.filter(code__icontains=filters["q"])
    if filters["container_type"] == "pallet":
        queryset = queryset.filter(source_context_type="pallet")
    elif filters["container_type"] == "box":
        queryset = queryset.exclude(source_context_type="pallet")
    else:
        filters["container_type"] = ""
    if filters["partner"]:
        queryset = queryset.filter(agency__agn_name__icontains=filters["partner"])
    if filters["barcode"]:
        queryset = queryset.filter(
            Q(code__icontains=filters["barcode"])
            | Q(items__barcode__icontains=filters["barcode"])
        ).distinct()

    page = Paginator(queryset.order_by("code", "id"), 100).get_page(params.get("page"))
    rows = []
    for item in page.object_list:
        first_barcode = next((row.barcode for row in item.items.all() if row.barcode), "")
        container_type = "pallet" if item.source_context_type == "pallet" else "box"
        rows.append(
            {
                "id": item.id,
                "code": item.code,
                "name": item.code,
                "container_type": container_type,
                "type": "паллета" if container_type == "pallet" else "короб",
                "partner": _agency(item.agency),
                "barcode": first_barcode or item.code,
                "parent": item.parent_code or "-",
                "location": item.location_code or "-",
                "zone": item.zone_code or "-",
                "status": item.get_status_display(),
                "sku_count": item.sku_count,
                "qty": item.stock_on_hand,
                "free": item.stock_free,
                "reserved": item.reserved_qty,
                "markings": item.marking_count,
                "updated_at": item.updated_at,
                "url": f"/head-manager/fbs-new/warehouse-boxes/?box_id={item.id}",
            }
        )
    selected = None
    selected_items = []
    selected_id = str(params.get("box_id") or "").strip()
    if selected_id.isdigit():
        selected = WmsNewBox.objects.select_related("agency", "location").filter(
            pk=int(selected_id)
        ).first()
    if selected:
        selected.type_label = "паллета" if selected.source_context_type == "pallet" else "короб"
        selected_items = [
            {
                "product": item.product_name or (item.product.name if item.product else "-"),
                "article": item.sku_code,
                "size": item.size or "-",
                "barcode": item.barcode or "-",
                "marking": item.marking_code or "-",
                "qty": item.qty,
                "free": item.available_qty,
                "reserved": item.reserved_qty,
                "state": item.warehouse_state_code or "-",
            }
            for item in WmsNewBoxItem.objects.filter(box=selected)
            .select_related("product")
            .order_by("sku_code", "size", "id")[:500]
        ]
    query_values = {key: value for key, value in filters.items() if value}
    query_prefix = urlencode(query_values)
    if query_prefix:
        query_prefix += "&"
    all_boxes = WmsNewBox.objects.all()
    return {
        "kind": "warehouse_boxes",
        "title": "Коробы",
        "filters": filters,
        "rows": rows,
        "page": page,
        "page_numbers": page.paginator.get_elided_page_range(
            page.number, on_each_side=2, on_ends=1
        ),
        "query_prefix": query_prefix,
        "partners": Agency.objects.filter(archived=False).order_by("agn_name"),
        "container_types": (
            ("pallet", "паллета"),
            ("box", "короб"),
        ),
        "box_columns": (
            ("id", "ID"),
            ("name", "Название"),
            ("type", "Тип"),
            ("partner", "Партнер"),
            ("barcode", "ШК"),
            ("qty", "Кол-во"),
            ("actions", "Действия"),
        ),
        "statuses": WmsNewBox.STATUS_CHOICES,
        "locations": WarehouseLocation.objects.filter(is_active=True)
        .order_by("zone_code", "location_code")[:3000],
        "selected_box": selected,
        "selected_items": selected_items,
        "summary": {
            "total": all_boxes.count(),
            "active": all_boxes.filter(status=WmsNewBox.STATUS_ACTIVE).count(),
            "nonempty": all_boxes.filter(stock_on_hand__gt=0).count(),
            "qty": int(all_boxes.aggregate(n=Sum("stock_on_hand"))["n"] or 0),
            "reserved": int(all_boxes.aggregate(n=Sum("reserved_qty"))["n"] or 0),
        },
    }


def _logistics_orders_data(*, request=None) -> dict:
    params = request.GET if request is not None else {}
    filters = {
        "q": str(params.get("q") or "").strip(),
        "status": str(params.get("status") or "").strip(),
        "package": str(params.get("package") or "").strip(),
    }
    try:
        page_size = int(params.get("page_size") or 50)
    except (TypeError, ValueError):
        page_size = 50
    if page_size not in (25, 50, 100, 200):
        page_size = 50
    queryset = WmsNewLogisticsOrder.objects.select_related(
        "agency", "source_fbs_shipment"
    ).prefetch_related("package_link__package")
    if filters["q"]:
        queryset = queryset.filter(
            Q(number__icontains=filters["q"])
            | Q(tracking_number__icontains=filters["q"])
        )
    if filters["status"] in dict(WmsNewLogisticsOrder.STATUS_CHOICES):
        queryset = queryset.filter(status=filters["status"])
    else:
        filters["status"] = ""
    if filters["package"] == "none":
        queryset = queryset.filter(package_link__isnull=True)
    elif filters["package"] == "assigned":
        queryset = queryset.filter(package_link__isnull=False)
    else:
        filters["package"] = ""
    page = Paginator(queryset.order_by("-created_at", "-id"), page_size).get_page(
        params.get("page") or 1
    )
    query_values = {**filters, "page_size": page_size}
    query_prefix = urlencode({key: value for key, value in query_values.items() if value})
    if query_prefix:
        query_prefix += "&"
    selected = None
    selected_id = str(params.get("order_id") or "").strip()
    if selected_id.isdigit():
        selected = WmsNewLogisticsOrder.objects.select_related("agency").filter(
            pk=int(selected_id)
        ).first()
    import_shipment = None
    import_rows = []
    import_shipment_id = str(params.get("import_shipment") or "").strip()
    if import_shipment_id.isdigit():
        import_shipment = (
            WmsNewShipment.objects.select_related("agency")
            .prefetch_related("shipment_orders__order__items")
            .filter(pk=int(import_shipment_id))
            .first()
        )
        if import_shipment is not None:
            imported_order_ids = set(
                WmsNewLogisticsOrder.objects.filter(
                    source_fbs_order_id__in=[
                        link.order_id for link in import_shipment.shipment_orders.all()
                    ]
                ).values_list("source_fbs_order_id", flat=True)
            )
            for link in import_shipment.shipment_orders.all():
                order = link.order
                import_rows.append(
                    {
                        "id": order.id,
                        "number": order.external_order_id,
                        "tracking": order.tracking_number or "-",
                        "status": (
                            "уже загружено"
                            if order.id in imported_order_ids
                            else "готово к загрузке"
                        ),
                        "already_imported": order.id in imported_order_ids,
                    }
                )
    rows = []
    for item in page.object_list:
        package = None
        try:
            package = item.package_link.package
        except (AttributeError, ObjectDoesNotExist):
            pass
        rows.append(
            {
                "id": item.id,
                "number": item.number,
                "package": package.number if package else "-",
                "tracking": item.tracking_number or "-",
                "source": item.source_label or item.get_source_type_display(),
                "weight": item.weight_g or "-",
                "dimensions": (
                    f"{item.width_mm} × {item.height_mm} × {item.depth_mm}"
                    if all((item.width_mm, item.height_mm, item.depth_mm))
                    else "-"
                ),
                "status": item.get_status_display(),
                "delivery_status": item.delivery_status or "-",
                "error": item.error_text or "-",
                "created_at": item.created_at,
                "checked_at": item.last_checked_at,
                "url": f"/head-manager/fbs-new/logistics-orders/?order_id={item.id}",
            }
        )
    return {
        "kind": "logistics_orders",
        "title": "Отправления",
        "filters": filters,
        "rows": rows,
        "page": page,
        "page_size": page_size,
        "page_sizes": (25, 50, 100, 200),
        "page_numbers": page.paginator.get_elided_page_range(
            page.number, on_each_side=2, on_ends=1
        ),
        "query_prefix": query_prefix,
        "statuses": WmsNewLogisticsOrder.STATUS_CHOICES,
        "package_filters": (("none", "Без грузоместа"), ("assigned", "В грузоместе")),
        "selected_order": selected,
        "import_shipment": import_shipment,
        "import_rows": import_rows,
        "fbs_shipments": WmsNewShipment.objects.select_related("agency")
        .annotate(link_count=Count("shipment_orders"))
        .filter(link_count__gt=0)
        .order_by("-source_created_at", "-id")[:100],
    }


def _logistics_packages_data(*, request=None) -> dict:
    params = request.GET if request is not None else {}
    filters = {
        "q": str(params.get("q") or "").strip(),
        "carrier": str(params.get("carrier") or "").strip(),
        "status": str(params.get("status") or "").strip(),
        "source": str(params.get("source") or "").strip(),
    }
    try:
        page_size = int(params.get("page_size") or 50)
    except (TypeError, ValueError):
        page_size = 50
    if page_size not in (25, 50, 100, 200):
        page_size = 50
    queryset = WmsNewLogisticsPackage.objects.select_related("manifest").annotate(
        order_count=Count("package_orders")
    )
    if filters["q"]:
        queryset = queryset.filter(
            Q(number__icontains=filters["q"])
            | Q(tracking_number__icontains=filters["q"])
            | Q(package_orders__order__number__icontains=filters["q"])
        ).distinct()
    if filters["carrier"] in dict(WmsNewLogisticsPackage.CARRIER_CHOICES):
        queryset = queryset.filter(carrier_code=filters["carrier"])
    else:
        filters["carrier"] = ""
    if filters["status"] in dict(WmsNewLogisticsPackage.STATUS_CHOICES):
        queryset = queryset.filter(status=filters["status"])
    else:
        filters["status"] = ""
    if filters["source"]:
        queryset = queryset.filter(delivery_service=filters["source"])
    page = Paginator(queryset.order_by("-created_at", "-id"), page_size).get_page(
        params.get("page") or 1
    )
    query_prefix = urlencode(
        {key: value for key, value in {**filters, "page_size": page_size}.items() if value}
    )
    if query_prefix:
        query_prefix += "&"
    warehouse_codes = list(
        WarehouseLocation.objects.filter(is_active=True)
        .exclude(warehouse_code="")
        .values_list("warehouse_code", flat=True)
        .distinct()
        .order_by("warehouse_code")
    )
    if not warehouse_codes:
        warehouse_codes = ["Основной склад"]
    return {
        "kind": "logistics_packages",
        "title": "Грузоместа",
        "filters": filters,
        "rows": [
            {
                "id": item.id,
                "number": item.number,
                "tracking": item.tracking_number or item.number,
                "carrier": item.get_carrier_code_display(),
                "source": item.delivery_service,
                "orders": item.order_count,
                "weight": item.weight_g or "-",
                "manifest": item.manifest.name if item.manifest else "-",
                "manifest_name": item.manifest.name if item.manifest else "-",
                "status": item.get_status_display(),
                "created_at": item.created_at,
                "url": f"/head-manager/fbs-new/logistics-shipments/{item.id}/",
            }
            for item in page.object_list
        ],
        "page": page,
        "page_size": page_size,
        "page_sizes": (25, 50, 100, 200),
        "page_numbers": page.paginator.get_elided_page_range(
            page.number, on_each_side=2, on_ends=1
        ),
        "query_prefix": query_prefix,
        "carriers": WmsNewLogisticsPackage.CARRIER_CHOICES,
        "statuses": WmsNewLogisticsPackage.STATUS_CHOICES,
        "sources": list(
            WmsNewLogisticsOrder.objects.exclude(source_label="")
            .values_list("source_label", flat=True)
            .distinct()
            .order_by("source_label")
        ),
        "warehouses": warehouse_codes,
        "available_orders": WmsNewLogisticsOrder.objects.filter(package_link__isnull=True)
        .order_by("-created_at", "-id")[:500],
        "manifests": WmsNewLogisticsManifest.objects.exclude(
            status__in=(
                WmsNewLogisticsManifest.STATUS_COMPLETED,
                WmsNewLogisticsManifest.STATUS_CANCELLED,
            )
        ).order_by("-created_at", "-id")[:200],
    }


def build_logistics_package_detail_data(package_id: int) -> dict | None:
    item = (
        WmsNewLogisticsPackage.objects.select_related("manifest", "created_by")
        .prefetch_related("package_orders__order__agency")
        .filter(pk=package_id)
        .first()
    )
    if item is None:
        return None
    links = list(item.package_orders.all())
    return {
        "kind": "logistics_package_detail",
        "title": f"Грузоместо {item.number}",
        "item": item,
        "orders": links,
        "available_orders": WmsNewLogisticsOrder.objects.filter(
            package_link__isnull=True,
            source_label=item.delivery_service,
        ).order_by("-created_at", "-id")[:500],
        "manifests": WmsNewLogisticsManifest.objects.filter(
            status=WmsNewLogisticsManifest.STATUS_NEW
        ).order_by("-created_at", "-id")[:200],
        "history": WmsNewEvent.objects.filter(
            entity_type="logistics_package", entity_id=item.id
        ).select_related("actor").order_by("-created_at", "-id")[:50],
        "can_edit": item.status == WmsNewLogisticsPackage.STATUS_NEW,
        "back_url": "/head-manager/fbs-new/logistics-shipments/",
    }


def _logistics_manifests_data(*, request=None) -> dict:
    params = request.GET if request is not None else {}
    try:
        page_size = int(params.get("page_size") or 100)
    except (TypeError, ValueError):
        page_size = 100
    if page_size not in (25, 50, 100, 200, 500):
        page_size = 100
    page = Paginator(
        WmsNewLogisticsManifest.objects.order_by("-created_at", "-id"), page_size
    ).get_page(params.get("page") or 1)
    query_prefix = f"page_size={page_size}&"
    return {
        "kind": "logistics_manifests",
        "title": "Рейсы",
        "rows": [
            {
                "id": item.id,
                "name": item.name,
                "status": item.get_status_display(),
                "company": item.logistics_company or "-",
                "items": item.total_items,
                "weight": item.total_weight_g or "-",
                "departure": item.departure_airport or "-",
                "destination": item.destination_airport or "-",
                "arrival": item.expected_arrival_date,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
                "url": f"/head-manager/fbs-new/logistics-trips/{item.id}/",
            }
            for item in page.object_list
        ],
        "page": page,
        "page_size": page_size,
        "page_sizes": (25, 50, 100, 200, 500),
        "page_numbers": page.paginator.get_elided_page_range(
            page.number, on_each_side=2, on_ends=1
        ),
        "query_prefix": query_prefix,
    }


def build_logistics_manifest_detail_data(manifest_id: int) -> dict | None:
    item = (
        WmsNewLogisticsManifest.objects.prefetch_related(
            "packages__package_orders__order"
        ).filter(pk=manifest_id).first()
    )
    if item is None:
        return None
    actions = {
        WmsNewLogisticsManifest.STATUS_NEW: (("send", "Отправить рейс"), ("cancel", "Отменить рейс")),
        WmsNewLogisticsManifest.STATUS_SENT: (("complete", "Завершить рейс"),),
    }.get(item.status, ())
    return {
        "kind": "logistics_manifest_detail",
        "title": f"Рейс {item.name}",
        "item": item,
        "packages": list(item.packages.all()),
        "actions": actions,
        "history": WmsNewEvent.objects.filter(
            entity_type="logistics_manifest", entity_id=item.id
        ).select_related("actor").order_by("-created_at", "-id")[:50],
        "back_url": "/head-manager/fbs-new/logistics-trips/",
    }


def _logistics_settings_data(*, request=None) -> dict:
    return {
        "kind": "logistics_settings",
        "title": "Настройки",
        "rules": WmsNewLogisticsRouteRule.objects.order_by("delivery_service"),
        "international_carriers": (
            (WmsNewLogisticsPackage.CARRIER_GBS, "GBS"),
            (WmsNewLogisticsPackage.CARRIER_MANUAL, "Ручная"),
        ),
        "local_carriers": (
            (WmsNewLogisticsPackage.CARRIER_RUSSIAN_POST, "Почта России"),
            (WmsNewLogisticsPackage.CARRIER_CDEK, "СДЭК"),
        ),
    }


def _documents_data(*, request=None, document_type: str) -> dict:
    params = request.GET if request is not None else {}
    filters = {
        "date_from": str(params.get("date_from") or "").strip(),
        "date_to": str(params.get("date_to") or "").strip(),
        "partner": str(params.get("partner") or "").strip(),
        "status": str(params.get("status") or "").strip(),
    }
    queryset = WmsNewDocument.objects.filter(document_type=document_type).select_related(
        "agency", "created_by", "posted_by"
    )
    date_from = parse_date(filters["date_from"])
    date_to = parse_date(filters["date_to"])
    if date_from:
        queryset = queryset.filter(source_created_at__date__gte=date_from)
    else:
        filters["date_from"] = ""
    if date_to:
        queryset = queryset.filter(source_created_at__date__lte=date_to)
    else:
        filters["date_to"] = ""
    if filters["partner"].isdigit():
        queryset = queryset.filter(agency_id=int(filters["partner"]))
    else:
        filters["partner"] = ""
    if filters["status"] in dict(WmsNewDocument.STATUS_CHOICES):
        queryset = queryset.filter(status=filters["status"])
    else:
        filters["status"] = ""
    page = Paginator(
        queryset.order_by("-source_created_at", "-created_at", "-id"), 100
    ).get_page(params.get("page") or 1)
    query_prefix = urlencode({key: value for key, value in filters.items() if value})
    if query_prefix:
        query_prefix += "&"
    selected = None
    selected_items = []
    selected_history = []
    products = []
    selected_id = str(params.get("document_id") or "").strip()
    if selected_id.isdigit():
        selected = (
            WmsNewDocument.objects.select_related("agency", "created_by", "posted_by")
            .filter(pk=int(selected_id), document_type=document_type)
            .first()
        )
        if selected:
            selected_items = list(selected.items.select_related("product", "location").order_by("id"))
            selected_history = list(
                WmsNewEvent.objects.filter(entity_type="document", entity_id=selected.id)
                .select_related("actor")
                .order_by("-created_at", "-id")[:100]
            )
            products = WmsNewProduct.objects.filter(
                agency_id=selected.agency_id,
                is_archived=False,
            ).order_by("name", "article")[:5000]
    selected_tab = str(params.get("tab") or "info").strip()
    if selected_tab not in {"info", "items", "history"}:
        selected_tab = "info"
    rows = [
        {
            "id": item.id,
            "created_at": item.source_created_at or item.created_at,
            "partner": _agency(item.agency),
            "comment": item.comment,
            "internal_comment": item.internal_comment,
            "basis": item.basis,
            "status": item.get_status_display(),
            "sku_count": item.sku_count,
            "unit_count": item.unit_count,
            "url": (
                f"/head-manager/fbs-new/documents-{'receipt' if document_type == WmsNewDocument.TYPE_RECEIPT else 'writeoff'}/"
                f"?document_id={item.id}"
            ),
        }
        for item in page.object_list
    ]
    section_title = "Приход" if document_type == WmsNewDocument.TYPE_RECEIPT else "Расход"
    if selected is not None:
        section_title = f"Документ #{selected.id}"
    return {
        "kind": "documents_registry",
        "title": section_title,
        "document_type": document_type,
        "filters": filters,
        "rows": rows,
        "page": page,
        "page_numbers": page.paginator.get_elided_page_range(page.number, on_each_side=3, on_ends=1),
        "query_prefix": query_prefix,
        "partners": Agency.objects.filter(archived=False).order_by("agn_name", "id"),
        "statuses": WmsNewDocument.STATUS_CHOICES,
        "selected_document": selected,
        "selected_items": selected_items,
        "selected_history": selected_history,
        "selected_tab": selected_tab,
        "products": products,
        "locations": WarehouseLocation.objects.filter(is_active=True).order_by(
            "zone_code", "location_code"
        )[:3000],
    }


def _logistics_data() -> dict:
    active_statuses = (
        LogisticsTrip.STATUS_PLANNED,
        LogisticsTrip.STATUS_LOADING,
        LogisticsTrip.STATUS_DEPARTED,
    )
    rows = [
        {
            "cells": (
                item.number,
                _dt(item.trip_date),
                _display(item, "trip_kind"),
                _display(item, "status"),
                item.vehicle_number or item.vehicle_name or "-",
                _user(item.assigned_driver) if item.assigned_driver else (item.driver_name or "-"),
                item.gate_number or "-",
                _dt(item.updated_at),
            ),
            "url": f"/logistics/trips/{item.id}/",
        }
        for item in LogisticsTrip.objects.select_related("assigned_driver").order_by(
            "-trip_date", "-updated_at", "-id"
        )[:25]
    ]
    return _table(
        title="Рейсы",
        columns=("Рейс", "Дата", "Тип", "Статус", "Автомобиль", "Водитель", "Ворота", "Обновлено"),
        rows=rows,
        summary=(
            _summary("Активные рейсы", LogisticsTrip.objects.filter(status__in=active_statuses).count()),
            _summary("Запланировано", LogisticsTrip.objects.filter(status=LogisticsTrip.STATUS_PLANNED).count()),
            _summary("На погрузке", LogisticsTrip.objects.filter(status=LogisticsTrip.STATUS_LOADING).count()),
            _summary("В пути", LogisticsTrip.objects.filter(status=LogisticsTrip.STATUS_DEPARTED).count()),
            _summary(
                "Проблемы",
                ProblemTrip.objects.exclude(status__in=(ProblemTrip.STATUS_RESOLVED, ProblemTrip.STATUS_CLOSED)).count(),
            ),
        ),
        actions=(
            _action("Все рейсы", "/logistics/trips/", primary=True),
            _action("Планирование", "/logistics/trips/planning/"),
            _action("Проблемы", "/logistics/problems/"),
        ),
    )


def _document_data() -> dict:
    records: list[tuple] = []
    for item in BillingAct.objects.select_related("client").order_by("-updated_at", "-id")[:8]:
        records.append(
            (
                item.updated_at,
                {
                    "cells": (
                        "Акт",
                        item.number or f"#{item.id}",
                        _agency(item.client),
                        _display(item, "status"),
                        _number(item.total_amount),
                        _dt(item.act_date),
                    ),
                    "url": "/billing/acts/",
                },
            )
        )
    for item in ClientInvoice.objects.select_related("client").order_by("-updated_at", "-id")[:8]:
        records.append(
            (
                item.updated_at,
                {
                    "cells": (
                        "Счёт",
                        item.number or f"#{item.id}",
                        _agency(item.client),
                        _display(item, "status"),
                        _number(item.total_amount),
                        _dt(item.invoice_date),
                    ),
                    "url": f"/billing/invoices/{item.id}/",
                },
            )
        )
    for item in ShippingTransportNote.objects.select_related("order__agency").order_by("-updated_at", "-id")[:8]:
        records.append(
            (
                item.updated_at,
                {
                    "cells": (
                        "Транспортная накладная",
                        item.document_number or f"#{item.id}",
                        _agency(item.order.agency),
                        "Сформирована",
                        _number(item.service_cost),
                        _dt(item.document_date),
                    ),
                    "url": f"/shipping/{item.order_id}/documents/",
                },
            )
        )
    records.sort(key=lambda entry: entry[0] or timezone.now(), reverse=True)
    return _table(
        title="Последние документы",
        columns=("Документ", "Номер", "Клиент", "Статус", "Сумма", "Дата"),
        rows=[row for _, row in records[:25]],
        summary=(
            _summary("Акты", BillingAct.objects.count()),
            _summary("Счета", ClientInvoice.objects.count()),
            _summary("Транспортные накладные", ShippingTransportNote.objects.count()),
            _summary("События заявок", OrderAuditEntry.objects.count()),
        ),
        actions=(
            _action("Акты", "/billing/acts/", primary=True),
            _action("Счета", "/billing/invoices/"),
            _action("Документы отгрузки", "/shipping/"),
            _action("Аудит заявок", "/audit/orders/"),
        ),
    )


REPORTS = (
    ("Остатки товаров", "Текущие остатки по товарам, складам, зонам и статусам.", "/head-manager/stock-editor/"),
    ("Движение товаров", "История прихода, перемещений, отгрузок и текущего остатка SKU.", "/head-manager/reports/products/sku-movement/"),
    ("Состав коробов", "Товары, количество, маркировка и место каждого короба.", "/head-manager/reports/warehouse/boxes/"),
    ("Состав паллет", "Короба, SKU, количество и место каждой паллеты.", "/head-manager/reports/warehouse/pallets/"),
    ("FBS по сотрудникам", "Волны, заказы, единицы и отгрузки в разрезе сотрудников.", "/head-manager/reports/employees/fbs-productivity/"),
    ("Биллинг", "Начисления, акты, счета, оплаты и задолженность.", "/billing/reports/"),
)


def _report_data() -> dict:
    rows = [
        {"cells": (title, description, "Доступен"), "url": url}
        for title, description, url in REPORTS
    ]
    return _table(
        title="Рабочие отчеты",
        columns=("Отчет", "Назначение", "Состояние"),
        rows=rows,
        summary=(
            _summary("Каталог отчетов", len(REPORTS)),
            _summary("Активные SKU", SKU.objects.filter(deleted=False).count()),
            _summary("Действующие клиенты", Agency.objects.filter(archived=False).count()),
        ),
        actions=(
            _action("Все отчеты", "/head-manager/reports/", primary=True),
            _action("Отчеты биллинга", "/billing/reports/"),
        ),
    )


def _analytics_data() -> dict:
    rows: list[dict] = []
    groups = (
        ("FBS", FbsOrder, "internal_status"),
        ("Задачи", Task, "status"),
        ("Логистика", LogisticsTrip, "status"),
        ("Отгрузка", ShippingOrder, "status"),
    )
    for contour, model, field in groups:
        choices = dict(model._meta.get_field(field).choices)
        for entry in model.objects.values(field).annotate(total=Count("id")).order_by("-total"):
            status = entry[field]
            rows.append(
                {
                    "cells": (contour, str(choices.get(status, status)), _number(entry["total"])),
                    "url": "",
                }
            )
    stock = WarehouseStockSnapshot.objects.filter(qty__gt=0, is_archived=False).aggregate(
        qty=Sum("qty"), available=Sum("available_qty")
    )
    return _table(
        title="Операционные срезы",
        columns=("Контур", "Статус", "Количество"),
        rows=rows,
        summary=(
            _summary("Физический остаток", stock.get("qty") or 0),
            _summary("Доступный остаток", stock.get("available") or 0),
            _summary("Заказы FBS", FbsOrder.objects.count()),
            _summary("Задачи", Task.objects.count()),
            _summary("Рейсы", LogisticsTrip.objects.count()),
            _summary("Отгрузки", ShippingOrder.objects.count()),
        ),
        actions=(
            _action("Управленческие отчеты", "/head-manager/reports/", primary=True),
            _action("Движение SKU", "/head-manager/reports/products/sku-movement/"),
        ),
    )


def _marketplace_stocks_data(*, request=None) -> dict:
    params = request.GET if request is not None else {}
    filters = {
        "partner": str(params.get("partner") or "").strip(),
        "marketplace": str(params.get("marketplace") or "").strip(),
        "category": str(params.get("category") or "").strip(),
        "product": str(params.get("product") or "").strip(),
        "metric": str(params.get("metric") or "stock").strip(),
        "orient": str(params.get("orient") or "rows").strip(),
        "focus": str(params.get("focus") or "").strip(),
    }
    if filters["metric"] not in {"stock", "days"}:
        filters["metric"] = "stock"
    if filters["orient"] not in {"rows", "columns"}:
        filters["orient"] = "rows"
    if filters["marketplace"] not in {"", "wb", "ozon"}:
        filters["marketplace"] = ""
    if filters["focus"] not in {"", "critical", "zero", "slow"}:
        filters["focus"] = ""

    queryset = WmsNewMarketplaceStock.objects.select_related("agency", "product")
    if filters["partner"].isdigit():
        queryset = queryset.filter(agency_id=int(filters["partner"]))
    else:
        filters["partner"] = ""
    if filters["marketplace"]:
        queryset = queryset.filter(marketplace=filters["marketplace"])
    if filters["category"]:
        queryset = queryset.filter(category=filters["category"])
    if filters["product"]:
        queryset = queryset.filter(
            Q(product_name__icontains=filters["product"])
            | Q(sku_code__icontains=filters["product"])
            | Q(barcode__icontains=filters["product"])
        )
    summary_source = queryset
    product_key = ("agency_id", "product_id", "sku_code")
    critical_count = summary_source.filter(quantity__gt=0, days_cover__lt=10).values(
        *product_key
    ).distinct().count()
    zero_count = summary_source.filter(quantity=0, fullbox_qty__gt=0).values(
        *product_key
    ).distinct().count()
    slow_count = summary_source.filter(days_cover__gt=90).values(*product_key).distinct().count()
    total_stock = summary_source.aggregate(total=Sum("quantity"))["total"] or 0

    recommendation_columns = (
        "Партнер",
        "Маркетплейс",
        "Склад МП",
        "Товар",
        "Артикул",
        "Остаток МП",
        "Продажи 30 дн.",
        "Покрытие, дней",
        "Доступно Fullbox",
        "Рекомендовано к поставке",
        "Можно поставить сейчас",
    )
    recommendation_rows = []
    recommendation_items = []
    recommendation_source = summary_source.filter(sales_30d__gt=0).filter(
        Q(quantity=0) | Q(days_cover__lt=10)
    )
    for item in recommendation_source.order_by(
        "agency__agn_name", "marketplace", "warehouse_name", "product_name", "sku_code"
    )[:5000]:
        recommended = max(int(item.sales_30d or 0) - int(item.quantity or 0), 0)
        if recommended <= 0:
            continue
        available_now = min(recommended, int(item.fullbox_qty or 0))
        marketplace_label = item.get_marketplace_display()
        row = (
            _agency(item.agency),
            marketplace_label,
            item.warehouse_name,
            item.product_name or item.sku_code,
            item.sku_code,
            item.quantity,
            item.sales_30d,
            item.days_cover if item.days_cover is not None else 0,
            item.fullbox_qty,
            recommended,
            available_now,
        )
        recommendation_rows.append(row)
        recommendation_items.append(
            {
                "partner": row[0],
                "marketplace": marketplace_label,
                "warehouse": item.warehouse_name,
                "product": item.product_name or item.sku_code,
                "sku": item.sku_code,
                "quantity": item.quantity,
                "sales_30d": item.sales_30d,
                "days_cover": item.days_cover if item.days_cover is not None else 0,
                "fullbox_qty": item.fullbox_qty,
                "recommended": recommended,
                "available_now": available_now,
            }
        )
    recommendation_units = sum(item[9] for item in recommendation_rows)
    recommendation_available_units = sum(item[10] for item in recommendation_rows)

    if filters["focus"] == "critical":
        queryset = queryset.filter(quantity__gt=0, days_cover__lt=10)
    elif filters["focus"] == "zero":
        queryset = queryset.filter(quantity=0, fullbox_qty__gt=0)
    elif filters["focus"] == "slow":
        queryset = queryset.filter(days_cover__gt=90)

    warehouse_rows = list(
        queryset.values("marketplace", "warehouse_code", "warehouse_name")
        .distinct()
        .order_by("marketplace", "warehouse_name", "warehouse_code")[:40]
    )
    warehouses = [
        {
            **row,
            "key": f"{row['marketplace']}::{row['warehouse_code']}",
            "label": f"{'Wildberries' if row['marketplace'] == 'wb' else 'Ozon'} · {row['warehouse_name']}",
        }
        for row in warehouse_rows
    ]
    warehouse_keys = {item["key"] for item in warehouses}
    stock_rows = list(queryset.order_by("agency__agn_name", "product_name", "sku_code", "warehouse_name")[:20000])

    if filters["orient"] == "columns":
        products = {}
        for item in stock_rows:
            key = (item.agency_id, item.product_id, item.sku_code)
            products.setdefault(key, item.product_name or item.sku_code)
        product_items = [
            {"key": key, "label": label}
            for key, label in list(products.items())[:80]
        ]
        values = defaultdict(lambda: "-")
        for item in stock_rows:
            warehouse_key = f"{item.marketplace}::{item.warehouse_code}"
            product_id = (item.agency_id, item.product_id, item.sku_code)
            values[(warehouse_key, product_id)] = (
                item.quantity if filters["metric"] == "stock" else item.days_cover or "-"
            )
        columns = ("Маркетплейс / склад",) + tuple(item["label"] for item in product_items)
        rows = [
            (
                warehouse["label"],
                *(values[(warehouse["key"], product["key"])] for product in product_items),
            )
            for warehouse in warehouses
        ]
    else:
        grouped = {}
        for item in stock_rows:
            key = (item.agency_id, item.product_id, item.sku_code)
            row = grouped.setdefault(
                key,
                {
                    "base": (
                        _agency(item.agency),
                        item.product_name or item.sku_code,
                        item.sku_code,
                        item.fullbox_qty,
                        item.sales_30d,
                    ),
                    "values": {},
                },
            )
            warehouse_key = f"{item.marketplace}::{item.warehouse_code}"
            if warehouse_key in warehouse_keys:
                row["values"][warehouse_key] = (
                    item.quantity if filters["metric"] == "stock" else item.days_cover or "-"
                )
        columns = (
            "Партнер",
            "Товар",
            "Артикул",
            "Fullbox, шт",
            "Продажи 30 дн.",
            *(item["label"] for item in warehouses),
        )
        rows = [
            (*entry["base"], *(entry["values"].get(item["key"], "-") for item in warehouses))
            for entry in list(grouped.values())[:1000]
        ]

    configured = MarketCredential.objects.exclude(market_key__isnull=True).exclude(market_key="")
    integration_rows = list(
        configured.values("agency_id", "agency__agn_name", "market__name").order_by(
            "agency__agn_name", "market__name"
        )
    )
    latest = WmsNewMarketplaceStock.objects.aggregate(value=Max("fetched_at"))["value"]
    base_query = urlencode(
        {
            key: value
            for key, value in filters.items()
            if value not in ("", None) and key != "focus"
        }
    )
    return {
        "kind": "marketplace_analytics",
        "title": "Остатки по складам маркетплейсов",
        "filters": filters,
        "partners": Agency.objects.filter(archived=False).order_by("agn_name", "id"),
        "marketplaces": WmsNewMarketplaceStock.MARKETPLACE_CHOICES,
        "categories": WmsNewMarketplaceStock.objects.exclude(category="").values_list(
            "category", flat=True
        ).distinct().order_by("category"),
        "columns": columns,
        "rows": rows,
        "warehouses": warehouses,
        "critical_count": critical_count,
        "zero_count": zero_count,
        "slow_count": slow_count,
        "total_stock": total_stock,
        "recommendation_columns": recommendation_columns,
        "recommendation_rows": recommendation_rows,
        "recommendation_items": recommendation_items,
        "recommendation_count": len(recommendation_rows),
        "recommendation_units": recommendation_units,
        "recommendation_available_units": recommendation_available_units,
        "plan_open": str(params.get("plan") or "") == "1",
        "base_query": base_query,
        "integration_total": len(integration_rows),
        "integration_partner_total": len({item["agency_id"] for item in integration_rows}),
        "latest_fetched_at": latest,
        "has_snapshots": WmsNewMarketplaceStock.objects.exists(),
    }


def _rubles(value) -> str:
    return f"{Decimal(value or 0):,.2f}".replace(",", " ") + " руб."


def _partner_data(*, request=None) -> dict:
    ensure_partner_profiles()
    params = request.GET if request is not None else {}
    filters = {
        "q": str(params.get("q") or "").strip(),
        "status": str(params.get("status") or "active").strip(),
    }
    queryset = WmsNewPartnerProfile.objects.select_related("source_agency")
    if filters["q"]:
        queryset = queryset.filter(name__icontains=filters["q"])
    if filters["status"] == "active":
        queryset = queryset.filter(is_active=True)
    elif filters["status"] == "disabled":
        queryset = queryset.filter(is_active=False)
    elif filters["status"] != "all":
        filters["status"] = "active"
        queryset = queryset.filter(is_active=True)
    selected = None
    selected_id = str(params.get("partner_id") or "").strip()
    if selected_id.isdigit():
        profile = (
            WmsNewPartnerProfile.objects.select_related("source_agency")
            .filter(pk=int(selected_id))
            .first()
        )
        if profile:
            partner_tabs = (
                ("information", "Информация"),
                ("integrations", "Интеграции"),
                ("requisites", "Банковские реквизиты"),
                ("tariffs", "Тарифы"),
                ("catalog", "Шеринг каталога"),
                ("invoices", "Счета"),
                ("balance", "Баланс"),
            )
            partner_tab = str(params.get("partner_tab") or "information").strip()
            if partner_tab not in {item[0] for item in partner_tabs}:
                partner_tab = "information"
            agency = profile.source_agency
            agency_id = profile.source_agency_id
            integrations = []
            if agency_id:
                integrations = [
                    {
                        "marketplace": str(item.market),
                        "client_id": item.client_id or "-",
                        "configured": bool(item.market_key),
                    }
                    for item in MarketCredential.objects.filter(agency_id=agency_id)
                    .select_related("market")
                    .order_by("market__name", "id")
                ]
            invoice_items = list(
                WmsNewInvoice.objects.filter(agency_id=agency_id).order_by("-invoice_date", "-id")[:100]
            ) if agency_id else []
            billing_rows = [
                {
                    "kind": item.get_kind_display(),
                    "title": item.title,
                    "unit_price": item.unit_price,
                    "quantity": item.quantity,
                    "amount": item.amount,
                    "status": item.get_status_display(),
                }
                for item in WmsNewBillingItem.objects.filter(agency_id=agency_id)
                .order_by("kind", "title", "id")[:100]
            ] if agency_id else []
            product_count = WmsNewProduct.objects.filter(
                agency_id=agency_id, is_archived=False
            ).count() if agency_id else 0
            total_invoices = sum((item.total_amount for item in invoice_items), Decimal("0"))
            total_paid = sum((item.paid_amount for item in invoice_items), Decimal("0"))
            selected = {
                "id": profile.id,
                "name": profile.name,
                "balance": profile.balance,
                "is_active": profile.is_active,
                "wb_products_enabled": profile.wb_products_enabled,
                "wb_orders_enabled": profile.wb_orders_enabled,
                "ozon_products_enabled": profile.ozon_products_enabled,
                "ozon_orders_enabled": profile.ozon_orders_enabled,
                "requisites": profile.requisites,
                "tab": partner_tab,
                "tabs": partner_tabs,
                "agency": agency,
                "integrations": integrations,
                "billing_rows": billing_rows,
                "invoices": invoice_items,
                "product_count": product_count,
                "total_invoices": total_invoices,
                "total_paid": total_paid,
                "total_unpaid": max(total_invoices - total_paid, Decimal("0")),
            }
    rows = [
        {
            "id": index,
            "profile_id": item.pk,
            "name": item.name,
            "balance": _rubles(item.balance),
            "is_active": item.is_active,
            "wb_products": item.wb_products_enabled,
            "wb_orders": item.wb_orders_enabled,
            "ozon_products": item.ozon_products_enabled,
            "ozon_orders": item.ozon_orders_enabled,
            "url": f"/head-manager/fbs-new/partners-list/?partner_id={item.pk}",
        }
        for index, item in enumerate(queryset.order_by("name", "id")[:500], start=1)
    ]
    return {
        "kind": "partners_registry",
        "title": "Партнеры",
        "filters": filters,
        "rows": rows,
        "selected": selected,
        "create_open": str(params.get("create") or "") == "1",
    }


def _billing_tab(params, *, storage=False) -> str:
    allowed = {"priced", "archive"} if storage else {"unpriced", "priced", "archive"}
    value = str(params.get("tab") or ("priced" if storage else "unpriced")).strip()
    return value if value in allowed else ("priced" if storage else "unpriced")


def _partner_billing_data(active_slug: str, *, request=None) -> dict:
    params = request.GET if request is not None else {}
    partner = str(params.get("partner") or "").strip()
    partners = Agency.objects.filter(archived=False).order_by("agn_name", "id")
    kind = {
        "partners-billing-tasks": WmsNewBillingItem.KIND_TASK,
        "partners-billing-storage": WmsNewBillingItem.KIND_STORAGE,
        "partners-billing-fbs": WmsNewBillingItem.KIND_FBS,
    }[active_slug]
    tab = _billing_tab(params, storage=kind == WmsNewBillingItem.KIND_STORAGE)
    rows = []
    if kind == WmsNewBillingItem.KIND_TASK and tab == "unpriced":
        existing = dict(
            WmsNewBillingItem.objects.filter(kind=kind).values_list("source_key", "status")
        )
        queryset = WmsNewTask.objects.select_related("agency").exclude(agency__isnull=True)
        if partner.isdigit():
            queryset = queryset.filter(agency_id=int(partner))
        for item in queryset.order_by("-updated_at", "-id")[:500]:
            if existing.get(str(item.pk)) not in (None, WmsNewBillingItem.STATUS_UNPRICED):
                continue
            rows.append({
                "source_id": item.pk,
                "partner": _agency(item.agency),
                "number": item.source_task_id or item.pk,
                "type": item.get_workflow_type_display(),
                "status": item.get_status_display(),
            })
    elif kind == WmsNewBillingItem.KIND_FBS and tab == "unpriced":
        existing = dict(
            WmsNewBillingItem.objects.filter(kind=kind).values_list("source_key", "status")
        )
        queryset = WmsNewShipment.objects.select_related("agency")
        if partner.isdigit():
            queryset = queryset.filter(agency_id=int(partner))
        for item in queryset.order_by("-source_created_at", "-id")[:500]:
            if existing.get(str(item.pk)) not in (None, WmsNewBillingItem.STATUS_UNPRICED):
                continue
            rows.append({
                "source_id": item.pk,
                "partner": _agency(item.agency),
                "number": item.source_batch_id or item.pk,
                "date": item.dispatched_at or item.source_created_at or item.created_at,
                "marketplace": item.delivery_type or "-",
                "integration": item.integration_name or "-",
            })
    else:
        if kind == WmsNewBillingItem.KIND_STORAGE:
            ensure_storage_items()
        status = {
            "priced": WmsNewBillingItem.STATUS_PRICED,
            "archive": WmsNewBillingItem.STATUS_ARCHIVED,
        }.get(tab, WmsNewBillingItem.STATUS_UNPRICED)
        queryset = WmsNewBillingItem.objects.filter(kind=kind, status=status).select_related(
            "agency", "source_task", "source_shipment"
        )
        if partner.isdigit():
            queryset = queryset.filter(agency_id=int(partner))
        for item in queryset.order_by("-occurred_at", "-id")[:500]:
            if kind == WmsNewBillingItem.KIND_TASK:
                source = item.source_task
                rows.append({
                    "source_id": source.pk if source else item.pk,
                    "item_id": item.pk,
                    "partner": _agency(item.agency),
                    "number": (source.source_task_id or source.pk) if source else item.source_key,
                    "type": source.get_workflow_type_display() if source else item.title,
                    "status": source.get_status_display() if source else "-",
                    "amount": _rubles(item.amount),
                })
            elif kind == WmsNewBillingItem.KIND_FBS:
                source = item.source_shipment
                rows.append({
                    "source_id": source.pk if source else item.pk,
                    "item_id": item.pk,
                    "partner": _agency(item.agency),
                    "number": (source.source_batch_id or source.pk) if source else item.source_key,
                    "date": item.occurred_at,
                    "marketplace": item.marketplace or "-",
                    "integration": item.integration or "-",
                    "amount": _rubles(item.amount),
                })
            else:
                rows.append({
                    "source_id": item.pk,
                    "item_id": item.pk,
                    "partner": _agency(item.agency),
                    "type": item.title,
                    "period": f"{item.period_start:%Y-%m-%d} - {item.period_end:%Y-%m-%d}",
                    "amount": _rubles(item.amount),
                    "info": "Фактический снимок хранения Fullbox",
                })
    detail = None
    detail_id = str(params.get("detail") or "").strip()
    if kind == WmsNewBillingItem.KIND_STORAGE and detail_id.isdigit():
        item = WmsNewBillingItem.objects.select_related("agency").filter(
            pk=int(detail_id), kind=kind
        ).first()
        if item:
            source = BillingStorageDay.objects.filter(client=item.agency)
            if item.period_start:
                source = source.filter(day__gte=item.period_start)
            if item.period_end:
                source = source.filter(day__lte=item.period_end)
            totals = source.aggregate(
                days=Count("id"),
                pallets=Sum("pallet_count"),
                boxes=Sum("box_count"),
                units=Sum("sku_unit_count"),
                volume=Sum("billable_volume_m3"),
            )
            detail = {"item": item, **totals}
    return {
        "kind": "partner_billing",
        "billing_kind": kind,
        "tab": tab,
        "partner": partner,
        "partners": partners,
        "rows": rows,
        "detail": detail,
    }


def _partner_invoices_data(*, request=None) -> dict:
    params = request.GET if request is not None else {}
    filters = {
        "partner": str(params.get("partner") or "").strip(),
        "status": str(params.get("status") or "").strip(),
        "payment": str(params.get("payment") or "").strip(),
        "date_from": str(params.get("date_from") or "").strip(),
        "date_to": str(params.get("date_to") or "").strip(),
    }
    queryset = WmsNewInvoice.objects.select_related("agency")
    if filters["partner"].isdigit():
        queryset = queryset.filter(agency_id=int(filters["partner"]))
    if filters["status"] in dict(WmsNewInvoice.STATUS_CHOICES):
        queryset = queryset.filter(status=filters["status"])
    if parse_date(filters["date_from"]):
        queryset = queryset.filter(invoice_date__gte=parse_date(filters["date_from"]))
    if parse_date(filters["date_to"]):
        queryset = queryset.filter(invoice_date__lte=parse_date(filters["date_to"]))
    invoices = list(queryset.order_by("-invoice_date", "-id"))
    def payment_code(item):
        if item.paid_amount <= 0:
            return "unpaid"
        if item.paid_amount < item.total_amount:
            return "partial"
        if item.paid_amount == item.total_amount:
            return "paid"
        return "overpaid"
    if filters["payment"] in {"unpaid", "partial", "paid", "overpaid"}:
        invoices = [item for item in invoices if payment_code(item) == filters["payment"]]
    payment_labels = {
        "unpaid": "Не оплачен", "partial": "Оплачен частично",
        "paid": "Оплачен полностью", "overpaid": "Переплата по счету",
    }
    rows = [{
        "id": item.pk,
        "number": item.number,
        "partner": _agency(item.agency),
        "requisites": item.requisites or "Не указано",
        "invoice_date": item.invoice_date,
        "due_date": item.due_date,
        "payment_status": payment_labels[payment_code(item)],
        "status": item.get_status_display(),
        "amount": _rubles(item.total_amount),
        "paid": _rubles(item.paid_amount),
        "url": f"/head-manager/fbs-new/partners-invoices/?invoice_id={item.pk}",
    } for item in invoices]
    selected = None
    selected_id = str(params.get("invoice_id") or "").strip()
    if selected_id.isdigit():
        invoice = (
            WmsNewInvoice.objects.select_related("agency")
            .filter(pk=int(selected_id))
            .first()
        )
        if invoice:
            invoice_items = [
                {
                    "kind": item.get_kind_display(),
                    "title": item.title,
                    "quantity": item.quantity,
                    "unit_price": item.unit_price,
                    "amount": item.amount,
                    "date": item.occurred_at,
                    "period_start": item.period_start,
                    "period_end": item.period_end,
                }
                for item in invoice.items.order_by("kind", "occurred_at", "id")
            ]
            documents = [
                {
                    "id": item.id,
                    "type": item.get_document_type_display(),
                    "period": item.period,
                    "amount": item.amount,
                }
                for item in WmsNewPrimaryDocument.objects.filter(invoice=invoice)
                .order_by("-period", "id")
            ]
            remaining = max(invoice.total_amount - invoice.paid_amount, Decimal("0"))
            selected = {
                "id": invoice.id,
                "number": invoice.number,
                "agency": invoice.agency,
                "invoice_type": invoice.get_invoice_type_display(),
                "requisites": invoice.requisites or "Не указано",
                "invoice_date": invoice.invoice_date,
                "due_date": invoice.due_date,
                "status": invoice.get_status_display(),
                "status_code": invoice.status,
                "total_amount": invoice.total_amount,
                "paid_amount": invoice.paid_amount,
                "remaining_amount": remaining,
                "payment_status": payment_labels[payment_code(invoice)],
                "items": invoice_items,
                "documents": documents,
                "created_at": invoice.created_at,
                "updated_at": invoice.updated_at,
            }
    return {
        "kind": "partner_invoices",
        "filters": filters,
        "partners": Agency.objects.filter(archived=False).order_by("agn_name", "id"),
        "rows": rows,
        "selected": selected,
        "count": len(invoices),
        "total": _rubles(sum((item.total_amount for item in invoices), Decimal("0"))),
        "paid": _rubles(sum((item.paid_amount for item in invoices), Decimal("0"))),
        "unpaid": _rubles(sum((max(item.total_amount - item.paid_amount, Decimal("0")) for item in invoices), Decimal("0"))),
        "create_type": str(params.get("create") or "").strip(),
    }


def _partner_primary_docs_data(*, request=None) -> dict:
    params = request.GET if request is not None else {}
    filters = {
        "partner": str(params.get("partner") or "").strip(),
        "document_type": str(params.get("document_type") or "").strip(),
        "period_from": str(params.get("period_from") or "").strip(),
        "period_to": str(params.get("period_to") or "").strip(),
    }
    queryset = WmsNewPrimaryDocument.objects.select_related("agency", "invoice")
    if filters["partner"].isdigit():
        queryset = queryset.filter(agency_id=int(filters["partner"]))
    if filters["document_type"] in dict(WmsNewPrimaryDocument.TYPE_CHOICES):
        queryset = queryset.filter(document_type=filters["document_type"])
    if len(filters["period_from"]) == 7:
        start = parse_date(filters["period_from"] + "-01")
        if start:
            queryset = queryset.filter(period__gte=start)
    if len(filters["period_to"]) == 7:
        end = parse_date(filters["period_to"] + "-01")
        if end:
            queryset = queryset.filter(period__lte=end)
    docs = list(queryset.order_by("-period", "agency__agn_name", "id"))
    rows = [{
        "id": item.pk,
        "period": item.period,
        "partner": _agency(item.agency),
        "type": item.get_document_type_display(),
        "amount": _rubles(item.amount),
        "has_invoice": bool(item.invoice_id),
    } for item in docs]
    return {
        "kind": "partner_primary_docs",
        "filters": filters,
        "partners": Agency.objects.filter(archived=False).order_by("agn_name", "id"),
        "rows": rows,
        "count": len(rows),
        "total": _rubles(sum((item.amount for item in docs), Decimal("0"))),
        "incomplete_month": timezone.localdate().day < 28,
    }


def _crm_data() -> dict:
    events = AuditEntry.objects.filter(agency__isnull=False).select_related(
        "agency", "user", "journal"
    ).order_by("-created_at", "-id")[:30]
    rows = [
        {
            "cells": (
                _agency(item.agency),
                item.journal.name,
                _display(item, "action"),
                item.description or "-",
                _user(item.user),
                _dt(item.created_at),
            ),
            "url": "/audit/clients/",
        }
        for item in events
    ]
    active_clients = Agency.objects.filter(archived=False)
    return _table(
        title="Последние изменения по клиентам",
        columns=("Клиент", "Журнал", "Событие", "Описание", "Сотрудник", "Когда"),
        rows=rows,
        summary=(
            _summary("Активные клиенты", active_clients.count()),
            _summary("С email", active_clients.exclude(email="").count()),
            _summary("С телефоном", active_clients.exclude(phone="").count()),
            _summary("События клиентов", AuditEntry.objects.filter(agency__isnull=False).count()),
        ),
        actions=(
            _action("Карточки клиентов", "/head-manager/clients/", primary=True),
            _action("История клиентов", "/audit/clients/"),
        ),
        empty="Изменений по клиентам пока нет.",
    )


def _settings_data() -> dict:
    rows = (
        {"cells": ("FBS", "Интеграции, клиенты, складские политики и оборудование", _number(FbsIntegrationProfile.objects.filter(is_active=True).count())), "url": "/head-manager/fbs/settings/"},
        {"cells": ("Рабочие места", "Столы и контроллеры FBS", _number(FbsWorkstation.objects.filter(is_active=True).count())), "url": "/head-manager/fbs/settings/#equipment"},
        {"cells": ("Тележки", "Тележки комплектовщиков FBS", _number(FbsPickingCart.objects.filter(is_active=True).count())), "url": "/head-manager/fbs/settings/#equipment"},
        {"cells": ("Ячейки FBS", "Адреса хранения и назначения", _number(FbsStorageCell.objects.filter(is_active=True).count())), "url": "/head-manager/fbs/settings/"},
        {"cells": ("Сотрудники", "Роли, доступы и учетные записи", _number(Employee.objects.filter(is_active=True).count())), "url": "/employees/"},
        {"cells": ("Сканеры", "Подключение и проверка сканеров", "Настроить"), "url": "/scanner-settings/"},
        {"cells": ("Этикетки и принтеры", "Шаблоны, очереди и агенты печати", "Настроить"), "url": "/labels/settings/"},
    )
    return _table(
        title="Настройки Fullbox",
        columns=("Группа", "Назначение", "Состояние"),
        rows=list(rows),
        summary=(
            _summary("FBS-профили", FbsIntegrationProfile.objects.filter(is_active=True).count()),
            _summary("Рабочие места", FbsWorkstation.objects.filter(is_active=True).count()),
            _summary("Тележки", FbsPickingCart.objects.filter(is_active=True).count()),
            _summary("Сотрудники", Employee.objects.filter(is_active=True).count()),
        ),
    )


def _settings_billing_data(request=None) -> dict:
    profiles = list(WmsNewPartnerProfile.objects.filter(is_active=True))
    balance = sum((item.balance for item in profiles), Decimal("0"))
    requests = WmsNewRecord.objects.filter(
        module="settings", entity_type="billing_request"
    ).order_by("-created_at", "-id")[:30]
    return {
        "kind": "settings_billing",
        "tariff": "Fullbox · пилотный контур",
        "balance": _rubles(balance),
        "bonus": "0",
        "days_left": "—",
        "history": [
            {
                "date": item.created_at,
                "event": item.title,
                "amount": _rubles((item.payload or {}).get("amount") or 0),
                "status": item.status,
            }
            for item in requests
        ],
    }


def _settings_users_data(request=None) -> dict:
    params = request.GET if request is not None else {}
    tab = str(params.get("tab") or "employees")
    if tab not in {"employees", "partners", "roles"}:
        tab = "employees"
    overlays = {
        item.source_key: item
        for item in WmsNewRecord.objects.filter(module="settings", entity_type="user")
    }
    rows = []
    role_labels = dict(Employee.ROLE_CHOICES)
    for employee in Employee.objects.select_related("user").order_by("full_name", "id"):
        overlay = overlays.get(str(employee.pk))
        payload = dict(overlay.payload or {}) if overlay else {}
        rows.append({
            "id": employee.pk,
            "name": employee.full_name,
            "role": role_labels.get(employee.role, employee.role),
            "role_key": employee.role,
            "notifications": bool(payload.get("notifications", True)),
            "active": (overlay.status == "active") if overlay else employee.is_active,
            "source_active": employee.is_active,
            "registered": employee.created_at,
            "last_login": employee.user.last_login if employee.user_id else None,
        })
    invitations = list(WmsNewRecord.objects.filter(
        module="settings", entity_type="user_invitation"
    ).order_by("-created_at"))
    return {
        "kind": "settings_users",
        "tab": tab,
        "rows": rows,
        "invitations": invitations,
        "roles": Employee.ROLE_CHOICES,
        "role_summary": [
            {"key": key, "label": label, "count": sum(1 for row in rows if row["role_key"] == key)}
            for key, label in Employee.ROLE_CHOICES
        ],
    }


def _settings_places_data(request=None) -> dict:
    params = request.GET if request is not None else {}
    tab = str(params.get("tab") or "scheme")
    if tab not in {"scheme", "3d", "table"}:
        tab = "scheme"
    show_hidden = str(params.get("hidden") or "") == "1"
    overlays = {
        item.source_key: item
        for item in WmsNewRecord.objects.filter(module="settings", entity_type="place")
    }
    locations = WarehouseLocation.objects.order_by(
        "zone_code", "row_no", "section_no", "tier_no", "cell_no", "id"
    )
    rows = []
    for location in locations[:600]:
        overlay = overlays.get(str(location.pk))
        active = overlay.status != "hidden" if overlay else location.is_active
        if show_hidden != (not active):
            continue
        payload = dict(overlay.payload or {}) if overlay else {}
        rows.append({
            "source_key": str(location.pk),
            "name": overlay.title if overlay and overlay.title else str(location),
            "code": location.location_code or str(location),
            "warehouse": location.warehouse_code,
            "zone": location.zone_code,
            "kind": location.get_zone_kind_display(),
            "row": location.row_no,
            "section": location.section_no,
            "tier": location.tier_no,
            "cell": location.cell_no,
            "active": active,
            "x": payload.get("x", location.row_no * 42),
            "y": payload.get("y", location.section_no * 32),
        })
    manual = WmsNewRecord.objects.filter(
        module="settings", entity_type="place", source_key__startswith="manual-"
    ).order_by("title", "id")
    for item in manual:
        active = item.status != "hidden"
        if show_hidden != (not active):
            continue
        payload = dict(item.payload or {})
        rows.append({
            "source_key": item.source_key,
            "name": item.title,
            "code": payload.get("code") or item.title,
            "warehouse": payload.get("warehouse") or "MSK",
            "zone": payload.get("zone") or "NEW",
            "kind": payload.get("kind") or "Хранение",
            "row": payload.get("row") or 0,
            "section": payload.get("section") or 0,
            "tier": payload.get("tier") or 0,
            "cell": payload.get("cell") or 0,
            "active": active,
            "x": payload.get("x") or 0,
            "y": payload.get("y") or 0,
        })
    zones = list(WarehouseLocation.objects.values_list("zone_code", flat=True).distinct().order_by("zone_code"))
    return {
        "kind": "settings_places",
        "tab": tab,
        "show_hidden": show_hidden,
        "rows": rows,
        "zones": zones,
        "warehouse": "Основной склад",
        "dimensions": "60×40m",
    }


def _settings_printers_data(request=None) -> dict:
    jobs = WmsNewRecord.objects.filter(module="settings", entity_type="print_job").order_by("-created_at")
    token = WmsNewRecord.objects.filter(
        module="settings", entity_type="printer_token", source_key="main"
    ).first()
    workstations = WmsNewRecord.objects.filter(
        module="settings", entity_type="printer_workstation"
    ).exclude(status="archived").order_by("title")
    return {
        "kind": "settings_printers",
        "workstations": workstations,
        "token": (token.payload or {}).get("token", "") if token else "",
        "jobs": jobs[:60],
        "counts": {
            "waiting": jobs.filter(status="waiting").count(),
            "printing": jobs.filter(status="printing").count(),
            "printed": jobs.filter(status="printed").count(),
        },
    }


def _settings_directories_data(request=None) -> dict:
    params = request.GET if request is not None else {}
    active = str(params.get("directory") or "units")
    if active not in DIRECTORY_TYPES:
        active = "units"
    records = list(WmsNewRecord.objects.filter(
        module="settings", entity_type=f"directory_{active}"
    ).exclude(status="archived").order_by("title", "id"))
    defaults = {
        "units": (("pcs", "шт."), ("kg", "кг"), ("m", "м")),
        "services": (("receiving", "Приемка"), ("storage", "Хранение"), ("fbs", "FBS")),
    }.get(active, ())
    rows = [
        {"id": None, "code": code, "title": title, "status": "Системное значение"}
        for code, title in defaults
    ] + [
        {
            "id": item.id,
            "code": (item.payload or {}).get("code", ""),
            "title": item.title,
            "status": "FBS-NEW",
        }
        for item in records
    ]
    return {
        "kind": "settings_directories",
        "active": active,
        "title": DIRECTORY_TYPES[active],
        "directories": DIRECTORY_TYPES.items(),
        "rows": rows,
    }


def _settings_system_data(request=None) -> dict:
    params = request.GET if request is not None else {}
    tab = str(params.get("tab") or "general")
    if tab not in {"general", "requisites", "tasks", "fbs", "finance", "inventory"}:
        tab = "general"
    company = OwnCompany.objects.filter(is_active=True).order_by("-is_default", "id").first()
    requisites = {
        "inn": company.inn if company else "",
        "ogrn": company.ogrn if company else "",
        "company_name": company.name if company else "",
        "phone": company.phone if company else "",
        "email": company.email if company else "",
        "legal_address": company.address if company else "",
        "postal_address": company.postal_address if company else "",
        "director_position": "",
        "director_last_name": company.director_name if company else "",
        "director_first_name": "",
        "director_middle_name": "",
        "do_not_decline": "",
        "bank_bik": company.bank_bik if company else "",
        "bank_name": company.bank_name if company else "",
        "settlement_account": company.settlement_account if company else "",
        "correspondent_account": company.correspondent_account if company else "",
    }
    defaults = {
        "requisites": requisites,
        "tasks": {
            "auto_pick_places": "", "default_shipment_task_type": "main",
            "writeoff_status": "awaiting_shipment", "default_place": "",
            "box_label_size": "75x120", "picking_barcodes": "first",
            "picking_articles": "none", "box_code_type": "qr",
        },
        "fbs": {
            "wb_product_barcode_print": "no", "auto_add_to_shipment": "yes",
            "auto_assembled_place": "", "one_click_verification": "no",
            "scan_each_unit": "yes", "strict_collection": "no",
            "insufficient_stock_wave": "warn", "picking_barcodes": "all",
            "picking_articles": "first", "picking_font_size": "10",
            "picking_orientation": "portrait", "picking_extra_info": "none",
            "clone_marking_code": "",
        },
        "finance": {
            "balance_enabled": "1", "minimum_invoice_amount": "1000",
            "balance_service_name": "Пополнение баланса", "primary_documents_enabled": "1",
            "default_vat": "none", "auto_invoices": "1",
        },
        "inventory": {"count_input_mode": "quantity"},
    }
    values = system_settings() if tab == "general" else system_section_settings(tab, defaults.get(tab))
    return {"kind": "settings_system", "tab": tab, "values": values, "locations": WarehouseLocation.objects.filter(is_active=True).order_by("location_code", "id")[:300]}


def _settings_history_data(request=None) -> dict:
    params = request.GET if request is not None else {}
    page = max(1, int(params.get("page") or 1)) if str(params.get("page") or "1").isdigit() else 1
    queryset = WmsNewEvent.objects.select_related("actor").order_by("-created_at", "-id")
    page_obj = Paginator(queryset, 50).get_page(page)
    rows = []
    for item in page_obj.object_list:
        metadata = dict(item.metadata or {})
        product = metadata.get("product") or metadata.get("product_name") or "—"
        place = metadata.get("place") or metadata.get("location") or metadata.get("warehouse_code") or "—"
        rows.append({
            "id": item.pk,
            "date": item.created_at,
            "action": item.action,
            "user": _user(item.actor),
            "product": product,
            "count": metadata.get("quantity") or metadata.get("count") or "—",
            "place": place,
            "info": metadata.get("message") or metadata.get("reason") or item.entity_type,
        })
    return {"kind": "settings_history", "rows": rows, "page_obj": page_obj}


def _settings_labels_data(request=None) -> dict:
    params = request.GET if request is not None else {}
    templates = list(WmsNewRecord.objects.filter(
        module="settings", entity_type="label_template"
    ).exclude(status="archived").order_by("title", "id"))
    selected = None
    selected_id = str(params.get("template") or "")
    if selected_id.isdigit():
        selected = next((item for item in templates if item.pk == int(selected_id)), None)
    payload = dict(selected.payload or {}) if selected else {}
    return {
        "kind": "settings_labels",
        "templates": templates,
        "selected": selected,
        "editor": {
            "name": selected.title if selected else "Новый шаблон",
            "sheet_size": payload.get("sheet_size") or "58x40",
            "elements": payload.get("elements") or "Название товара\nАртикул\nШтрихкод",
        },
    }


def _settings_matching_data(request=None) -> dict:
    params = request.GET if request is not None else {}
    tab = str(params.get("tab") or "all")
    if tab not in {"all", "ozon", "wb"}:
        tab = "all"
    query = str(params.get("q") or "").strip()
    partner = str(params.get("partner") or "").strip()
    match = str(params.get("match") or "all")
    products = WmsNewProduct.objects.filter(is_archived=False).select_related("agency")
    if query:
        products = products.filter(Q(article__icontains=query) | Q(barcode__icontains=query) | Q(name__icontains=query))
    if partner.isdigit():
        products = products.filter(agency_id=int(partner))
    mappings = {
        item.source_key: item
        for item in WmsNewRecord.objects.filter(module="settings", entity_type="goods_mapping")
    }
    total_products = WmsNewProduct.objects.filter(is_archived=False).count()
    ozon_count = sum(1 for item in mappings.values() if (item.payload or {}).get("ozon"))
    wb_count = sum(1 for item in mappings.values() if (item.payload or {}).get("wb"))
    matched_count = sum(
        1 for item in mappings.values()
        if (item.payload or {}).get("ozon") or (item.payload or {}).get("wb")
    )
    source = []
    for product in products.order_by("id")[:1000]:
        record = mappings.get(str(product.pk))
        payload = dict(record.payload or {}) if record else {}
        matched = bool(payload.get("ozon") or payload.get("wb"))
        if match == "unmatched" and matched:
            continue
        if match == "matched" and not matched:
            continue
        if tab == "ozon" and not payload.get("ozon"):
            continue
        if tab == "wb" and not payload.get("wb"):
            continue
        source.append({
            "id": product.pk,
            "article": product.article,
            "barcode": product.barcode,
            "name": product.name,
            "size": product.size,
            "color": product.color,
            "partner": _agency(product.agency),
            "ozon": payload.get("ozon", ""),
            "wb": payload.get("wb", ""),
        })
    page_value = int(params.get("page") or 1) if str(params.get("page") or "1").isdigit() else 1
    page_obj = Paginator(source, 50).get_page(page_value)
    return {
        "kind": "settings_matching",
        "tab": tab,
        "filters": {"q": query, "partner": partner, "match": match},
        "count": products.count(),
        "total_products": total_products,
        "ozon_count": ozon_count,
        "wb_count": wb_count,
        "unmatched_total": max(0, total_products - matched_count),
        "unmatched": sum(1 for row in source if not row["ozon"] and not row["wb"]),
        "rows": page_obj.object_list,
        "page_obj": page_obj,
        "partners": Agency.objects.filter(archived=False).order_by("agn_name", "id"),
        "return_query": urlencode({key: value for key, value in {
            "tab": tab, "q": query, "partner": partner, "match": match,
            "page": page_obj.number,
        }.items() if value not in {"", "all", 1}}),
    }


def _help_new_data(active_slug: str) -> dict:
    if active_slug == "support":
        return {
            "kind": "help_support",
            "title": "Техническая поддержка",
            "text": "Чтобы разобраться в принципах работы Fullbox WMS NEW, изучите инструкции. Если вопрос останется — создайте задачу на доработку: она попадет в отдельный журнал нового контура.",
        }
    if active_slug == "instructions":
        return {
            "kind": "help_instructions",
            "title": "Инструкции",
            "sections": (
                {
                    "slug": "first-setup",
                    "title": "ПЕРВИЧНАЯ НАСТРОЙКА",
                    "intro": "Подготовьте независимый контур до выдачи доступа пилотным сотрудникам.",
                    "steps": (
                        "Проверьте справочник складов, зоны, места хранения и транзитные места: приемка, брак, тележки, обработка, сборка и отгрузка.",
                        "Создайте каждому сотруднику отдельную учетную запись и назначьте роль. Общие логины не используются: все действия должны иметь автора.",
                        "Настройте рабочие места и принтеры для этикеток заказов, коробов и документов.",
                        "Проверьте партнеров, интеграции, справочники единиц, услуг, категорий, типов маркировки и дополнительных полей.",
                        "Сопоставьте товары Fullbox с карточками маркетплейсов и проверьте штрихкоды, габариты и обязательные признаки маркировки.",
                    ),
                    "note": "Старый рабочий контур остается активным. Пилотный доступ к FBS-NEW выдается отдельно.",
                },
                {
                    "slug": "intro",
                    "title": "ОБЩИЙ ПРИНЦИП РАБОТЫ",
                    "intro": "Операция проходит от задания до подтвержденного результата и оставляет аудит.",
                    "steps": (
                        "Входящие данные читаются из Fullbox и материализуются в независимых сущностях FBS-NEW.",
                        "Сотрудник работает только под своей учетной записью и сканирует фактические места, товары, короба и этикетки.",
                        "Переход статуса выполняется после проверки обязательных условий, а не вручную в обход процесса.",
                        "Каждое изменение записывается в историю с пользователем, временем и контекстом операции.",
                        "Ошибки и недостачи не скрываются: заказ или задача переводятся в предусмотренное проблемное состояние.",
                    ),
                    "note": "FBS-NEW не записывает результаты в старый FBS, пока пилот не принят.",
                },
                {
                    "slug": "good",
                    "title": "КАРТОЧКА ТОВАРА",
                    "intro": "Карточка объединяет идентификацию товара и его фактическое складское состояние.",
                    "steps": (
                        "Откройте товар двойным кликом по строке либо по ссылке в названии.",
                        "Проверьте наименование, партнера, артикул, штрихкоды, изображение, вес, размеры и категорию.",
                        "Просмотрите места хранения, свободный остаток, резервы FBO/FBS, ожидаемое количество и историю движений.",
                        "Отдельно контролируйте КИЗ, IMEI, срок годности, дополнительные поля, услуги и связи с маркетплейсами.",
                        "Объединение, перенос партнеру, импорт и массовые действия выполняйте только после проверки выбранных строк.",
                    ),
                },
                {
                    "slug": "entrance",
                    "title": "ПОСТУПЛЕНИЕ ТОВАРА",
                    "intro": "Приемка подтверждает фактический состав поставки и размещает годный товар.",
                    "steps": (
                        "Создайте приемку по партнеру и основанию либо откройте приемку, созданную из задачи.",
                        "Сканируйте товар, упаковку и обязательные коды маркировки; фиксируйте принятое количество и расхождения.",
                        "Разделите годный товар, брак и неопознанные позиции, подготовьте этикетки коробов.",
                        "Укажите дату документа, место приемки и способ закрытия.",
                        "После контрольной сверки закройте приемку: система формирует приходный документ и движение в FBS-NEW.",
                    ),
                    "note": "Частичное закрытие оставляет продолжение приемки для недостающего количества.",
                },
                {
                    "slug": "movement",
                    "title": "ПЕРЕМЕЩЕНИЕ ТОВАРА",
                    "intro": "Перемещение всегда подтверждает источник, товар и назначение.",
                    "steps": (
                        "Выберите перемещение товара, короба, палеты или всего места.",
                        "Отсканируйте исходное место и убедитесь, что товар действительно числится в нем.",
                        "Отсканируйте товар или упаковку, укажите количество, затем отсканируйте место назначения.",
                        "Проверьте партнера, доступный остаток и ограничения зоны.",
                        "Завершение создает пару движений и обновляет только независимые остатки FBS-NEW.",
                    ),
                },
                {
                    "slug": "preparing",
                    "title": "ОБРАБОТКА ТОВАРА",
                    "intro": "Обработка выполняется задачей с понятным техническим заданием и фактическим результатом.",
                    "steps": (
                        "Откройте назначенную задачу и проверьте тип работы, партнера, товары, план и адрес обработки.",
                        "Переместите товар на транзитное место обработки и подтвердите исходные короба.",
                        "Выполните проверку, переупаковку, маркировку, комплектацию или другую указанную услугу.",
                        "Зафиксируйте обработанное количество, расходные материалы, новые короба, файлы и отклонения.",
                        "Верните результат на хранение либо передайте следующему этапу и завершите задачу после тарификации.",
                    ),
                },
                {
                    "slug": "order",
                    "title": "ПОСТУПЛЕНИЕ ЗАКАЗА",
                    "intro": "Заказы интеграций поступают односторонней синхронизацией, ручные создаются отдельно.",
                    "steps": (
                        "Проверьте номер, партнера, интеграцию, тип доставки, дедлайн и состав заказа.",
                        "Система пересчитывает доступность по местам хранения с учетом уже созданных резервов.",
                        "Очередь показывает достаточный остаток, возможную недостачу из-за более ранних заказов либо подтвержденную недостачу.",
                        "Интеграционный заказ не редактируется как ручной; спорные данные устраняются в источнике или отдельной операцией.",
                        "Готовый к обработке заказ запускается одиночно либо включается в выбранную волну.",
                    ),
                },
                {
                    "slug": "assembling-one",
                    "title": "СБОРКА ОДНОГО ЗАКАЗА",
                    "intro": "Одиночная сборка подтверждает каждую фактическую единицу товара.",
                    "steps": (
                        "Отсканируйте номер заказа и рабочее место или тележку.",
                        "Следуйте маршруту по приоритетам хранения: место, затем товар и обязательный индивидуальный код.",
                        "Подтвердите количество; недостачу или неверное место оформите проблемным действием.",
                        "На сборочном месте повторно проверьте товары, упакуйте заказ и напечатайте транспортную этикетку.",
                        "Отсканируйте этикетку заказа и место готовых заказов — только после этого заказ готов к отгрузке.",
                    ),
                },
                {
                    "slug": "assembling-many",
                    "title": "СБОРКА НЕСКОЛЬКИХ ЗАКАЗОВ",
                    "intro": "Волна объединяет выбранные заказы в один маршрут подбора, но сохраняет проверку каждого заказа.",
                    "steps": (
                        "В очереди выберите заказы. Допускаются разные партнеры и маркетплейсы; размер волны ограничьте вместимостью одной тележки и возможностями одного подборщика.",
                        "Запустите волну и проверьте рекомендацию: без сортировки для однотоварных заказов; смешанную волну при необходимости разделите на одно- и многотоварную части.",
                        "Начните подбор сканированием тележки. Система строит маршрут по возрастанию приоритета мест и выбирает упаковки так, чтобы сократить число действий.",
                        "На каждом шаге сканируйте место, товар и обязательную маркировку, затем подтверждайте фактическое количество. Пропуск и досрочное завершение фиксируются отдельно.",
                        "После подбора перенесите тележку к сборке. Для режима без сортировки сканируйте товар, собирайте соответствующий заказ, печатайте и сканируйте его этикетку.",
                        "Многотоварный заказ закрывается только после проверки всех его строк. Проблемное действие переносит весь заказ в проблемное место.",
                        "Завершите волну после обработки всех доступных заказов; не начатые при досрочном окончании возвращаются в очередь.",
                    ),
                    "note": "Сортировка многотоварных заказов — отдельный режим. Не подменяйте ее сборкой без сортировки.",
                },
                {
                    "slug": "shipment",
                    "title": "ОТГРУЗКА СО СКЛАДА",
                    "intro": "Отгрузка объединяет готовые заказы по типу доставки и подтверждает передачу перевозчику.",
                    "steps": (
                        "Создайте отгрузку по службе доставки и временному месту либо откройте существующую.",
                        "Добавляйте готовые заказы сканированием; система проверяет статус и принадлежность поставке маркетплейса.",
                        "Разложите заказы по коробам, проверьте вес, объем, этикетки, услуги и документы.",
                        "Выполните финальную проверку сканированием всех заказов; несколько сотрудников могут работать последовательно.",
                        "Переместите короба в зону отгрузки и нажмите отправку только после фактического выезда со склада.",
                    ),
                },
                {
                    "slug": "returns",
                    "title": "ОБРАБОТКА ВОЗВРАТОВ",
                    "intro": "Возврат проходит как приемка с обязательной проверкой состояния каждой единицы.",
                    "steps": (
                        "Зарегистрируйте возврат и свяжите его с исходным заказом, если связь известна.",
                        "Создайте задачу на возврат, назначьте сотрудника и место разбора.",
                        "По каждой строке зафиксируйте принятое количество, годное, брак, состояние упаковки и КИЗ.",
                        "Годный товар разместите в коробе и месте хранения; брак перенесите в отдельное место.",
                        "Завершите возврат после сверки: в остаток FBS-NEW поступает только подтвержденное годное количество.",
                    ),
                },
                {
                    "slug": "mobile",
                    "title": "МОБИЛЬНЫЙ ИНТЕРФЕЙС",
                    "intro": "Сканерный интерфейс показывает только текущий шаг и сохраняет тот же контроль ролей.",
                    "steps": (
                        "Войдите под личной учетной записью и выберите склад.",
                        "Откройте назначенную операцию: приемку, перемещение, подбор, сборку, отгрузку или инвентаризацию.",
                        "Держите фокус в поле сканирования; после каждого кода сверяйте подтверждение на экране.",
                        "Не передавайте терминал без выхода из учетной записи и не используйте ручной ввод вместо доступного штрихкода.",
                        "При потере связи остановите операцию и возобновите ее после синхронизации, не повторяя уже подтвержденные шаги.",
                    ),
                },
                {
                    "slug": "inventory",
                    "title": "ИНВЕНТАРИЗАЦИЯ",
                    "intro": "Инвентаризация сравнивает ожидаемый и фактический остаток по выбранной области.",
                    "steps": (
                        "Создайте инвентаризацию по складу, зоне, месту, партнеру или выбранным товарам.",
                        "Назначьте счетчика и режим сканирования, затем зафиксируйте первый пересчет.",
                        "Строки с расхождениями передайте на повторный независимый пересчет.",
                        "Начальник склада проверяет причины, короба, маркировку и движения после предыдущей фиксации.",
                        "Утверждение формирует отдельные корректирующие движения FBS-NEW по каждой строке.",
                    ),
                },
                {
                    "slug": "employee-report",
                    "title": "ОТЧЕТЫ ПО СОТРУДНИКАМ",
                    "intro": "Отчеты строятся из фактических событий и завершенных рабочих этапов.",
                    "steps": (
                        "Выберите период, склад, сотрудника, тип задачи или операции.",
                        "Сравнивайте количество завершенных задач, обработанные SKU и штуки, длительность и отклонения.",
                        "Учитывайте совместную работу: автор скана, исполнитель задачи и закрывший операцию могут различаться.",
                        "Проверяйте аномалии через карточку задачи и историю событий, а не только по итоговой строке отчета.",
                    ),
                },
                {
                    "slug": "warehouse-report",
                    "title": "ОТЧЕТЫ ПО СКЛАДУ",
                    "intro": "Складские отчеты дают срез остатков, мест, движений, заказов и документов.",
                    "steps": (
                        "Сначала задайте склад, партнера и период, затем выберите предмет отчета.",
                        "Сверяйте общий остаток со свободным, FBO/FBS-резервом, транзитом и ожидаемым количеством.",
                        "Для расхождения переходите от агрегата к товару, месту, коробу и конкретному движению.",
                        "Экспортируйте результат только после проверки фильтров и времени последнего обновления.",
                    ),
                },
                {
                    "slug": "consumables",
                    "title": "УЧЕТ РАСХОДНЫХ МАТЕРИАЛОВ",
                    "intro": "Расходники связываются с задачей, заказом, отгрузкой или другой операцией.",
                    "steps": (
                        "Заведите типы коробов, палет, пакетов, пленки, этикеток и прочих материалов.",
                        "При выполнении работы фиксируйте фактический тип и количество, а не плановое значение.",
                        "Проверьте привязку к партнеру, услуге и тарифу до завершения тарификации.",
                        "Списание и отчетность выполняются по журналу операций нового контура.",
                    ),
                },
                {
                    "slug": "purchase-planning",
                    "title": "ПЛАНИРОВАНИЕ ЗАКУПОК",
                    "intro": "Потребность рассчитывается по доступному остатку и подтвержденному спросу.",
                    "steps": (
                        "Выберите склад, партнера, категорию и горизонт планирования.",
                        "Отделите физический остаток от свободного: резервы, транзит и брак нельзя считать доступными.",
                        "Учитывайте открытые FBS-заказы, ожидаемые приемки, скорость отгрузки и страховой запас.",
                        "Проверьте товары без движения и аномальные пики перед передачей рекомендации в закупку.",
                    ),
                    "note": "План является рекомендацией; заказ поставщику создается отдельным согласованным действием.",
                },
            ),
        }
    records = WmsNewRecord.objects.filter(
        module="help", entity_type="development_task"
    ).order_by("-created_at", "-id")
    return {"kind": "help_development", "title": "Задачи на доработку", "rows": records}


def _help_data(active_slug: str) -> dict:
    if active_slug == "development":
        rows = [
            {
                "cells": (
                    f"#{item.id}",
                    item.title,
                    _display(item, "status"),
                    _display(item, "priority"),
                    _user(item.assigned_to),
                    _dt(item.updated_at),
                ),
                "url": f"/todo/{item.id}/",
            }
            for item in Task.objects.exclude(status="done").select_related("assigned_to").order_by(
                "-updated_at", "-id"
            )[:25]
        ]
        return _table(
            title="Задачи на доработку",
            columns=("№", "Задача", "Статус", "Приоритет", "Исполнитель", "Обновлено"),
            rows=rows,
            actions=(
                _action("Создать задачу", "/todo/new/", primary=True),
                _action("Журнал разработки", "/development-journal/"),
            ),
        )
    resources = (
        ("База знаний склада", "Рабочие инструкции и регламенты", "/sklad/knowledge/"),
        ("Помощь логистики", "Инструкции по рейсам, водителям и проблемам", "/logistics/help/"),
        ("Описание проекта", "Архитектура и назначение модулей Fullbox", "/project-description/"),
        ("Структура проекта", "Состав приложений и связей", "/project-structure/"),
        ("Журнал разработки", "Фактические изменения и история работ", "/development-journal/"),
    )
    if active_slug == "support":
        resources = resources[:2] + (("Создать задачу", "Обращение или проблема для команды", "/todo/new/"),)
    elif active_slug == "instructions":
        resources = resources[:4]
    rows = [
        {"cells": (title, description, "Доступно"), "url": url}
        for title, description, url in resources
    ]
    return _table(
        title="Помощь и инструкции",
        columns=("Ресурс", "Назначение", "Состояние"),
        rows=rows,
        actions=(
            _action("База знаний", "/sklad/knowledge/", primary=True),
            _action("Создать задачу", "/todo/new/"),
        ),
        note="Все ссылки ведут в действующие справочные и рабочие контуры Fullbox.",
    )


def build_section_data(active_slug: str, *, request=None) -> dict:
    """Return a bounded, read-only dataset for one WMS section."""

    builders = {
        "problems": _problem_data,
        "tasks": _task_data,
        "fbs": _fbs_order_data,
        "fbs-orders": _fbs_order_data,
        "fbs-queue": _fbs_queue_data,
        "fbs-waves": _fbs_wave_data,
        "fbs-assembly": _fbs_assembly_data,
        "fbs-shipments": _fbs_shipment_data,
        "fbs-returns": _fbs_return_data,
        "warehouse": _warehouse_data,
        "logistics": _logistics_data,
        "documents": _document_data,
        "reports": _report_data,
        "analytics": _analytics_data,
        "partners": _partner_data,
        "crm": _crm_data,
        "settings": _settings_data,
        "help": lambda: _help_data("help"),
        "support": lambda: _help_data("support"),
        "instructions": lambda: _help_data("instructions"),
        "development": lambda: _help_data("development"),
    }
    aliases = {
        "tasks-list": "tasks",
        "tasks-acceptance": "tasks",
        "tasks-processing": "tasks",
        "tasks-shipment": "tasks",
        "tasks-other": "tasks",
        "tasks-multiacceptance": "tasks",
        "warehouse-goods": "warehouse",
        "warehouse-acceptances": "warehouse",
        "warehouse-marking": "warehouse",
        "warehouse-extra-fields": "warehouse",
        "warehouse-inventories": "warehouse",
        "warehouse-boxes": "warehouse",
        "warehouse-bundles": "warehouse",
        "warehouse-movement": "warehouse",
        "warehouse-history": "warehouse",
        "logistics-orders": "logistics",
        "logistics-shipments": "logistics",
        "logistics-trips": "logistics",
        "logistics-settings": "logistics",
        "documents-receipt": "documents",
        "documents-writeoff": "documents",
        "reports-goods": "reports",
        "reports-goods-places": "reports",
        "reports-places": "reports",
        "reports-returns": "reports",
        "reports-shipments": "reports",
        "reports-tasks": "reports",
        "reports-fbs-status": "reports",
        "reports-fbs-count": "reports",
        "reports-fbs-orders": "reports",
        "reports-fbs-goods": "reports",
        "reports-invoices": "reports",
        "analytics-marketplace-stocks": "analytics",
        "partners-list": "partners",
        "partners-billing-tasks": "partners",
        "partners-billing-storage": "partners",
        "partners-billing-fbs": "partners",
        "partners-invoices": "partners",
        "partners-primary-docs": "partners",
        "crm-contacts": "crm",
        "crm-suppliers": "crm",
        "crm-leads": "crm",
        "settings-billing": "settings",
        "settings-users": "settings",
        "settings-places": "settings",
        "settings-printers": "settings",
        "settings-directories": "settings",
        "settings-system": "settings",
        "settings-history": "settings",
        "settings-labels": "settings",
        "settings-matching": "settings",
        "settings-pilot": "settings",
    }
    requested_slug = active_slug
    task_lists = {
        "tasks": ("", "Список задач"),
        "tasks-list": ("", "Список задач"),
    }
    task_boards = {
        "tasks-acceptance": (WmsNewTask.TYPE_ACCEPTANCE, "Приемка"),
        "tasks-processing": (WmsNewTask.TYPE_PROCESSING, "Обработка"),
        "tasks-shipment": (WmsNewTask.TYPE_SHIPMENT, "Отгрузка"),
        "tasks-other": (WmsNewTask.TYPE_OTHER, "Прочие задачи"),
    }
    if requested_slug in task_lists:
        board_type, title = task_lists[requested_slug]
        return _task_list_data(request=request, board_type=board_type, title=title)
    if requested_slug in task_boards:
        board_type, title = task_boards[requested_slug]
        return _task_board_data(request, workflow_type=board_type, title=title)
    if requested_slug == "tasks-multiacceptance":
        return _multiacceptance_data(request=request)
    if requested_slug == "problems":
        return _problem_data(request=request)

    if requested_slug == "warehouse-goods":
        return _warehouse_goods_data(request=request)
    if requested_slug == "warehouse-acceptances":
        return _warehouse_acceptance_data(request=request)
    if requested_slug == "warehouse-marking":
        return _warehouse_marking_data(request=request)
    if requested_slug == "warehouse-extra-fields":
        return _warehouse_extra_fields_data(request=request)
    if requested_slug == "warehouse-inventories":
        return _warehouse_inventory_data(request=request)
    if requested_slug == "warehouse-boxes":
        return _warehouse_boxes_data(request=request)
    if requested_slug == "warehouse-bundles":
        return _warehouse_bundle_data(request=request)
    if requested_slug == "warehouse-movement":
        return _warehouse_movement_data(request=request)
    if requested_slug == "warehouse-history":
        return _warehouse_history_data(request=request)
    if requested_slug == "logistics-orders":
        return _logistics_orders_data(request=request)
    if requested_slug == "logistics-shipments":
        return _logistics_packages_data(request=request)
    if requested_slug == "logistics-trips":
        return _logistics_manifests_data(request=request)
    if requested_slug == "logistics-settings":
        return _logistics_settings_data(request=request)
    if requested_slug == "documents-receipt":
        return _documents_data(request=request, document_type=WmsNewDocument.TYPE_RECEIPT)
    if requested_slug == "documents-writeoff":
        return _documents_data(request=request, document_type=WmsNewDocument.TYPE_WRITEOFF)
    if requested_slug in REPORT_TITLES:
        params = request.GET if request is not None else {}
        return {
            "kind": "wms_report",
            **build_report(requested_slug, params),
        }
    if requested_slug == "analytics-marketplace-stocks":
        return _marketplace_stocks_data(request=request)
    if requested_slug in {
        "partners-billing-tasks",
        "partners-billing-storage",
        "partners-billing-fbs",
    }:
        return _partner_billing_data(requested_slug, request=request)
    if requested_slug in {"partners", "partners-list"}:
        return _partner_data(request=request)
    if requested_slug == "partners-invoices":
        return _partner_invoices_data(request=request)
    if requested_slug == "partners-primary-docs":
        return _partner_primary_docs_data(request=request)
    settings_builders = {
        "settings-billing": _settings_billing_data,
        "settings-users": _settings_users_data,
        "settings-places": _settings_places_data,
        "settings-printers": _settings_printers_data,
        "settings-directories": _settings_directories_data,
        "settings-system": _settings_system_data,
        "settings-history": _settings_history_data,
        "settings-labels": _settings_labels_data,
        "settings-matching": _settings_matching_data,
    }
    if requested_slug in settings_builders:
        return settings_builders[requested_slug](request=request)
    if requested_slug in {"support", "instructions", "development"}:
        return _help_new_data(requested_slug)

    active_slug = aliases.get(active_slug, active_slug)
    builder = builders.get(active_slug)
    if not builder:
        return {}
    if active_slug in {"fbs", "fbs-orders", "fbs-queue", "fbs-waves", "fbs-assembly", "fbs-shipments", "fbs-returns"}:
        return builder(request=request)
    if active_slug == "partners":
        return builder(request=request)
    return builder()
