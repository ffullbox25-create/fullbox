from __future__ import annotations

import re
from collections import defaultdict

from django import template
from django.core.paginator import Paginator
from django.db.models import Q
from django.utils import timezone

from fbs.models import FbsClientMovementRequest
from fullbox.order_numbers import format_order_number
from logistics.models import LogisticsTripOrder
from shipping.models import ShippingOrder, ShippingOrderItem
from shipping.selectors import shipping_ui_status_label
from sklad.models import WarehouseContainer, WarehouseStockSnapshot
from todo.templatetags.todo_panel import task_panel
from todo.deadlines import attach_warehouse_deadline_context


register = template.Library()

_RECEIVING_ROUTE_RE = re.compile(r"/orders/receiving/([^/]+)/")
_OTHER_ROUTE_RE = re.compile(r"/orders/other/([^/]+)/")
_SHIPPING_ROUTE_RE = re.compile(r"/shipping/(\d+)/")
_TRIP_ROUTE_RE = re.compile(r"/logistics/trips/(\d+)/")
_DAILY_PROBLEM_CHECK_QUERY = "daily_problem_check="
_NUMBER_RE = re.compile(r"№\s*([^\s,.;]+)")
_PAGE_SIZES = (10, 25, 50)
_TYPE_LABELS = {
    "receiving": "Приемка",
    "shipping": "Отгрузка",
    "fbs_movement": "FBS перемещение",
    "other": "Прочая",
    "logistics": "Рейс",
    "inspection": "Проверка",
}
_SCHEDULE_LABELS = {
    "backlog": "Просрочено",
    "in_progress": "Сегодня",
    "blocked": "Скоро",
    "done": "Выполнено",
}
_STATUS_TONES = {"neutral", "pending", "moving", "active", "done", "danger"}
_SCHEDULE_STATUSES = {"backlog", "in_progress", "blocked", "done"}
_IDENTIFIER_SEARCH_MIN_LENGTH = 3
_IDENTIFIER_SEARCH_LIMIT = 20
_FBS_WAREHOUSE_VISIBLE_STATUSES = (
    FbsClientMovementRequest.STATUS_APPROVED,
    FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED,
    FbsClientMovementRequest.STATUS_IN_PROGRESS,
    FbsClientMovementRequest.STATUS_MOVED,
    FbsClientMovementRequest.STATUS_AWAITING_MANAGER_CONFIRMATION,
    FbsClientMovementRequest.STATUS_NEEDS_CLARIFICATION,
    FbsClientMovementRequest.STATUS_COMPLETED,
    FbsClientMovementRequest.STATUS_CANCELED,
)
_FBS_WAREHOUSE_DONE_STATUSES = {
    FbsClientMovementRequest.STATUS_AWAITING_MANAGER_CONFIRMATION,
    FbsClientMovementRequest.STATUS_COMPLETED,
    FbsClientMovementRequest.STATUS_CANCELED,
}
_FBS_REACHTRUCK_STATUSES = {
    FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED,
    FbsClientMovementRequest.STATUS_IN_PROGRESS,
    FbsClientMovementRequest.STATUS_MOVED,
    FbsClientMovementRequest.STATUS_NEEDS_CLARIFICATION,
}


def _positive_int(value, default: int) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _normalized_search_value(value) -> str:
    return " ".join(str(value or "").strip().split())


def _agency_label(agency) -> str:
    if agency is None:
        return "-"
    for attr in ("short_name", "agn_name", "fio_agn"):
        value = _normalized_search_value(getattr(agency, attr, ""))
        if value:
            return value
    return _normalized_search_value(agency) or "-"


def _user_label(user) -> str:
    if user is None:
        return "Не назначен"
    full_name = _normalized_search_value(user.get_full_name())
    return full_name or _normalized_search_value(user.username) or "Не назначен"


def _datetime_label(value) -> str:
    if value is None:
        return "-"
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return timezone.localtime(value).strftime("%d.%m.%Y %H:%M")


def _datetime_iso(value) -> str:
    if value is None:
        return ""
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return timezone.localtime(value).isoformat()


def _fbs_schedule_status(request_row: FbsClientMovementRequest) -> str:
    return "done" if request_row.status in _FBS_WAREHOUSE_DONE_STATUSES else "in_progress"


def _fbs_status_tone(request_row: FbsClientMovementRequest) -> str:
    if request_row.status == FbsClientMovementRequest.STATUS_CANCELED:
        return "danger"
    if request_row.status in {
        FbsClientMovementRequest.STATUS_COMPLETED,
        FbsClientMovementRequest.STATUS_AWAITING_MANAGER_CONFIRMATION,
    }:
        return "done"
    if request_row.status == FbsClientMovementRequest.STATUS_NEEDS_CLARIFICATION:
        return "danger"
    if request_row.status in {
        FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED,
        FbsClientMovementRequest.STATUS_IN_PROGRESS,
    }:
        return "active"
    if request_row.status == FbsClientMovementRequest.STATUS_MOVED:
        return "moving"
    return "pending"


def _fbs_request_id_from_query(query: str) -> int | None:
    compact = _normalized_search_value(query).upper().replace(" ", "")
    match = re.fullmatch(r"(?:FBS-?MOV-?)?0*(\d+)", compact)
    return int(match.group(1)) if match else None


def _filter_fbs_requests(rows, *, client_id: str, query: str, document_status: str):
    if client_id:
        rows = rows.filter(agency_id=client_id)
    normalized_query = _normalized_search_value(query)
    if normalized_query:
        conditions = (
            Q(agency__agn_name__icontains=normalized_query)
            | Q(agency__short_name__icontains=normalized_query)
            | Q(agency__fio_agn__icontains=normalized_query)
            | Q(agency__inn__icontains=normalized_query)
            | Q(comment__icontains=normalized_query)
            | Q(lines__barcode__icontains=normalized_query)
            | Q(lines__sku_code__icontains=normalized_query)
        )
        request_id = _fbs_request_id_from_query(normalized_query)
        if request_id is not None:
            conditions |= Q(pk=request_id)
        rows = rows.filter(conditions).distinct()
    if document_status == "reachtruck":
        rows = rows.filter(status__in=_FBS_REACHTRUCK_STATUSES)
    elif document_status == "accepted":
        rows = rows.filter(
            status__in={
                FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED,
                FbsClientMovementRequest.STATUS_IN_PROGRESS,
            }
        )
    elif document_status:
        rows = rows.none()
    return rows


def _fbs_table_row(request_row: FbsClientMovementRequest) -> dict:
    responsible = request_row.warehouse_confirmed_by or request_row.warehouse_accepted_by
    schedule_status = _fbs_schedule_status(request_row)
    return {
        "task_id": f"fbs-movement-{request_row.id}",
        "number": request_row.number,
        "type_label": _TYPE_LABELS["fbs_movement"],
        "client_label": _agency_label(request_row.agency),
        "date_label": _datetime_label(request_row.updated_at),
        "due_date_label": "-",
        "due_date_iso": "",
        "deadline_state": "done" if schedule_status == "done" else "none",
        "deadline_change_count": 0,
        "warehouse_label": "Основной → FBS",
        "responsible_label": _user_label(responsible),
        "status_label": request_row.get_status_display(),
        "status_tone": _fbs_status_tone(request_row),
        "detail_url": f"/fbs/operator/movements/{request_row.id}/",
        "title": (
            f"{request_row.get_mode_display()} · {int(request_row.requested_qty or 0)} шт."
        ),
        "_sort_value": request_row.updated_at.timestamp() if request_row.updated_at else 0,
        "_schedule_status": schedule_status,
    }


def _storekeeper_status_tone(order: ShippingOrder) -> str:
    if order.status == ShippingOrder.STATUS_CANCELED:
        return "danger"
    if order.status in {
        ShippingOrder.STATUS_SHIPPED,
        ShippingOrder.STATUS_PARTIAL,
    }:
        return "done"
    if order.status in {
        ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
        ShippingOrder.STATUS_PICKING,
        ShippingOrder.STATUS_PACKED,
    }:
        return "active"
    return "pending"


def _item_comment_match_label(comment: str, query: str) -> str:
    query_key = _normalized_search_value(query).casefold()
    for segment in re.split(r"[;\n]+", str(comment or "")):
        segment_key = _normalized_search_value(segment).casefold()
        if query_key not in segment_key:
            continue
        if "шк гм" in segment_key:
            return "ШК ГМ / состав заявки"
        if "короб" in segment_key:
            return "Короб / состав заявки"
    return "Данные позиции заявки"


def _shipping_identifier_results(query: str, *, limit: int = _IDENTIFIER_SEARCH_LIMIT) -> list[dict]:
    normalized_query = _normalized_search_value(query)
    if len(normalized_query) < _IDENTIFIER_SEARCH_MIN_LENGTH:
        return []
    query_key = normalized_query.casefold()
    matches_by_order_id: dict[int, list[dict]] = defaultdict(list)
    matches_by_order_number: dict[str, list[dict]] = defaultdict(list)

    def append_match(target: dict, key, label: str, value) -> None:
        clean_value = _normalized_search_value(value)
        if not clean_value or query_key not in clean_value.casefold():
            return
        rows = target[key]
        row_key = (label.casefold(), clean_value.casefold())
        if any((row["label"].casefold(), row["value"].casefold()) == row_key for row in rows):
            return
        rows.append(
            {
                "label": label,
                "value": clean_value,
                "exact": clean_value.casefold() == query_key,
            }
        )

    direct_rows = ShippingOrder.objects.filter(
        Q(number__icontains=normalized_query)
        | Q(supply_number__icontains=normalized_query)
        | Q(wb_supply_barcode__icontains=normalized_query)
        | Q(shipping_barcode__icontains=normalized_query)
        | Q(comment__icontains=normalized_query)
    ).values(
        "id",
        "number",
        "supply_number",
        "wb_supply_barcode",
        "shipping_barcode",
        "comment",
    )[:100]
    direct_fields = (
        ("number", "Номер заявки"),
        ("supply_number", "Номер поставки Ozon"),
        ("wb_supply_barcode", "Поставка WB"),
        ("shipping_barcode", "ШК поставки"),
        ("comment", "ШК ГМ / данные поставки"),
    )
    for row in direct_rows:
        order_id = int(row["id"])
        for field, label in direct_fields:
            value = normalized_query if field == "comment" else row.get(field)
            append_match(matches_by_order_id, order_id, label, value)

    for row in ShippingOrderItem.objects.filter(
        comment__icontains=normalized_query
    ).values("order_id", "comment")[:100]:
        comment = str(row.get("comment") or "")
        append_match(
            matches_by_order_id,
            int(row["order_id"]),
            _item_comment_match_label(comment, normalized_query),
            normalized_query,
        )

    container_rows = WarehouseContainer.objects.filter(
        container_code__icontains=normalized_query
    ).filter(
        Q(source_context_type="shipping")
        | Q(parent_container__source_context_type="shipping")
    ).values(
        "container_code",
        "source_context_type",
        "source_context_id",
        "parent_container__source_context_type",
        "parent_container__source_context_id",
    )[:100]
    for row in container_rows:
        order_number = (
            row.get("source_context_id")
            if row.get("source_context_type") == "shipping"
            else row.get("parent_container__source_context_id")
        )
        order_number = _normalized_search_value(order_number)
        if order_number:
            append_match(
                matches_by_order_number,
                order_number,
                "Короб Fullbox",
                row.get("container_code"),
            )

    snapshot_rows = WarehouseStockSnapshot.objects.filter(
        Q(container_code__icontains=normalized_query)
        | Q(container__container_code__icontains=normalized_query)
    ).filter(
        Q(source_context_type="shipping")
        | Q(last_event__stock_context_type="shipping")
    ).values(
        "container_code",
        "container__container_code",
        "source_context_type",
        "source_context_id",
        "last_event__stock_context_type",
        "last_event__stock_context_id",
    )[:100]
    for row in snapshot_rows:
        order_number = (
            row.get("source_context_id")
            if row.get("source_context_type") == "shipping"
            else row.get("last_event__stock_context_id")
        )
        order_number = _normalized_search_value(order_number)
        if not order_number:
            continue
        box_code = row.get("container__container_code") or row.get("container_code")
        append_match(
            matches_by_order_number,
            order_number,
            "Короб Fullbox",
            box_code,
        )

    context_numbers = list(matches_by_order_number)
    if context_numbers:
        for row in ShippingOrder.objects.filter(number__in=context_numbers).values("id", "number"):
            order_id = int(row["id"])
            for match in matches_by_order_number.get(str(row["number"]), []):
                append_match(
                    matches_by_order_id,
                    order_id,
                    match["label"],
                    match["value"],
                )

    if not matches_by_order_id:
        return []

    orders = list(
        ShippingOrder.objects.filter(pk__in=matches_by_order_id)
        .select_related("agency", "marketplace")
        .order_by("-updated_at", "-id")
    )

    def result_score(order: ShippingOrder) -> tuple[int, object, int]:
        match_rows = matches_by_order_id.get(order.id, [])
        exact_score = sum(100 for row in match_rows if row.get("exact"))
        return exact_score + len(match_rows), order.updated_at, order.id

    orders.sort(key=result_score, reverse=True)
    results = []
    for order in orders[: max(int(limit or 0), 1)]:
        client_label = "-"
        if order.agency:
            client_label = _normalized_search_value(
                getattr(order.agency, "short_name", "")
                or getattr(order.agency, "agn_name", "")
                or getattr(order.agency, "fio_agn", "")
            ) or "-"
        marketplace_label = _normalized_search_value(
            getattr(order.marketplace, "name", "") if order.marketplace else ""
        ) or "-"
        destination_label = _normalized_search_value(
            order.destination_warehouse
            or order.destination_address
            or order.transit_address
        ) or "-"
        results.append(
            {
                "order_id": order.id,
                "number": format_order_number("shipping", order.number),
                "client_label": client_label,
                "marketplace_label": marketplace_label,
                "destination_label": destination_label,
                "status_label": shipping_ui_status_label(order),
                "status_tone": _storekeeper_status_tone(order),
                "detail_url": f"/shipping/{order.pk}/",
                "matches": matches_by_order_id.get(order.id, [])[:4],
            }
        )
    return results


def _page_links(page_obj, radius: int = 2) -> list[dict]:
    current = int(page_obj.number)
    total = int(page_obj.paginator.num_pages)
    numbers = {1, total}
    numbers.update(range(max(1, current - radius), min(total, current + radius) + 1))
    links = []
    previous = 0
    for number in sorted(numbers):
        if previous and number - previous > 1:
            links.append({"ellipsis": True})
        links.append({"number": number, "current": number == current})
        previous = number
    return links


def _display_number(task, *, shipping_order: ShippingOrder | None = None) -> str:
    route = str(getattr(task, "route", "") or "")
    if "/sklad/journal/" in route and _DAILY_PROBLEM_CHECK_QUERY in route:
        return f"ПРВ-{int(task.id):06d}"
    match = _RECEIVING_ROUTE_RE.search(route)
    if match:
        return format_order_number("receiving", match.group(1))
    match = _OTHER_ROUTE_RE.search(route)
    if match:
        return format_order_number("other", match.group(1))
    match = _SHIPPING_ROUTE_RE.search(route)
    if match:
        raw_number = str(getattr(shipping_order, "number", "") or match.group(1)).strip()
        return format_order_number("shipping", raw_number)
    title = str(getattr(task, "panel_title", "") or getattr(task, "title", "") or "")
    match = _NUMBER_RE.search(title)
    return match.group(1) if match else "-"


def _shipping_order_pk(task) -> int | None:
    match = _SHIPPING_ROUTE_RE.search(str(getattr(task, "route", "") or ""))
    return int(match.group(1)) if match else None


def _trip_pk(task) -> int | None:
    match = _TRIP_ROUTE_RE.search(str(getattr(task, "route", "") or ""))
    return int(match.group(1)) if match else None


def _table_row(
    task,
    schedule_status: str,
    *,
    shipping_order=None,
    trip_otg_orders=None,
) -> dict:
    filter_type = str(getattr(task, "filter_type", "") or "other")
    assigned_to = getattr(task, "assigned_to", None)
    status_label = str(getattr(task, "order_status_label", "") or "").strip()
    status_tone = str(getattr(task, "order_status_tone", "") or "neutral")
    if not status_label:
        if schedule_status == "done":
            status_label = "Выполнена"
            status_tone = "done"
        elif assigned_to is not None:
            status_label = "В работе"
            status_tone = "active"
        else:
            status_label = "Ожидает назначения"
            status_tone = "pending"
    if status_tone not in _STATUS_TONES:
        status_tone = "neutral"

    type_label = _TYPE_LABELS.get(filter_type, "Прочая")
    warehouse_label = "-"
    if filter_type == "shipping":
        warehouse_label = str(getattr(task, "executor_label", "") or "-").strip() or "-"
        if shipping_order is not None:
            if shipping_order.delivery_type == ShippingOrder.DELIVERY_TRANSFER:
                type_label = "Перемещение"
            warehouse_label = str(
                shipping_order.destination_warehouse
                or shipping_order.destination_address
                or shipping_order.transit_address
                or warehouse_label
                or "-"
            ).strip() or "-"

    responsible_label = str(getattr(assigned_to, "full_name", "") or "").strip() or "Не назначен"
    due_date = getattr(task, "effective_due_date", None) or getattr(task, "due_date", None)
    deadline_change = getattr(task, "warehouse_deadline_change", None)
    return {
        "task_id": task.id,
        "number": _display_number(task, shipping_order=shipping_order),
        "type_label": type_label,
        "client_label": str(getattr(task, "order_client_label", "") or "-").strip() or "-",
        "date_label": _datetime_label(getattr(task, "created_at", None)),
        "due_date_label": _datetime_label(due_date),
        "due_date_iso": _datetime_iso(due_date),
        "deadline_state": (
            "done"
            if schedule_status == "done"
            else ("active" if due_date else "none")
        ),
        "deadline_change_count": getattr(task, "warehouse_deadline_change_count", 0),
        "deadline_reason": str(getattr(deadline_change, "reason", "") or ""),
        "deadline_author": str(
            getattr(getattr(deadline_change, "changed_by", None), "full_name", "") or ""
        ),
        "warehouse_label": warehouse_label,
        "responsible_label": responsible_label,
        "status_label": status_label,
        "status_tone": status_tone,
        "detail_url": str(getattr(task, "panel_url", "") or getattr(task, "route", "") or "").strip(),
        "title": str(getattr(task, "panel_title", "") or getattr(task, "title", "") or "").strip(),
        "is_trip": filter_type == "logistics",
        "trip_otg_orders": list(trip_otg_orders or []),
        "_sort_value": task.updated_at.timestamp() if getattr(task, "updated_at", None) else 0,
    }


@register.inclusion_tag("sklad/_request_journal.html", takes_context=True)
def storekeeper_request_journal(context):
    request = context.get("request")
    panel_context = context.flatten() if hasattr(context, "flatten") else dict(context)
    panel_context["_task_panel_open_only"] = True
    panel = task_panel(
        panel_context,
        role="storekeeper",
        limit=100000,
        show_meta=False,
        include_created_by=False,
    )
    requested_type = str(
        request.GET.get("todo_filter_type") if request else ""
    ).strip()
    selected_type = (
        "fbs_movement"
        if requested_type == "fbs_movement"
        else str(panel.get("task_panel_active_filter_type") or "all")
    )
    tabs = panel.get("task_panel_filter_tabs") or []
    if not any(tab.get("value") == "fbs_movement" for tab in tabs):
        insert_at = next(
            (
                index + 1
                for index, tab in enumerate(tabs)
                if tab.get("value") == "shipping"
            ),
            len(tabs),
        )
        tabs.insert(
            insert_at,
            {
                "value": "fbs_movement",
                "label": "FBS перемещения",
                "count": 0,
                "active": False,
            },
        )
    for tab in tabs:
        tab["active"] = tab.get("value") == selected_type
    panel["task_panel_filter_tabs"] = tabs
    panel["task_panel_active_filter_type"] = selected_type
    identifier_query = str(panel.get("task_panel_search_query") or "").strip()
    selected_client = str(panel.get("task_panel_selected_client") or "")
    selected_document_status = str(
        panel.get("task_panel_selected_document_status") or ""
    )

    fbs_base = FbsClientMovementRequest.objects.filter(
        status__in=_FBS_WAREHOUSE_VISIBLE_STATUSES
    ).select_related(
        "agency",
        "warehouse_accepted_by",
        "warehouse_confirmed_by",
    )
    fbs_requests = list(
        _filter_fbs_requests(
            fbs_base,
            client_id=selected_client,
            query=identifier_query,
            document_status=selected_document_status,
        ).order_by("-updated_at", "-id")
    )
    fbs_open_count = sum(
        _fbs_schedule_status(item) != "done" for item in fbs_requests
    )
    for tab in panel.get("task_panel_filter_tabs") or []:
        if tab.get("value") == "fbs_movement":
            tab["count"] = fbs_open_count
        elif tab.get("value") == "all":
            tab["count"] = int(tab.get("count") or 0) + fbs_open_count

    client_options = {
        int(option["id"]): option
        for option in panel.get("task_panel_client_options") or []
    }
    for item in fbs_base.order_by("agency_id", "id"):
        client_options.setdefault(
            int(item.agency_id),
            {
                "id": int(item.agency_id),
                "label": _agency_label(item.agency),
                "selected": str(item.agency_id) == selected_client,
            },
        )
    panel["task_panel_client_options"] = sorted(
        client_options.values(), key=lambda option: str(option["label"]).casefold()
    )

    fbs_rows_for_type = (
        [_fbs_table_row(item) for item in fbs_requests]
        if selected_type in {"all", "fbs_movement"}
        else []
    )
    fbs_schedule_counts = {
        status: sum(row["_schedule_status"] == status for row in fbs_rows_for_type)
        for status in _SCHEDULE_STATUSES
    }
    for stat in panel.get("task_panel_stats") or []:
        status = str(stat.get("status") or "")
        stat["count"] = int(stat.get("count") or 0) + int(
            fbs_schedule_counts.get(status, 0)
        )

    identifier_results = (
        []
        if selected_type == "fbs_movement"
        else _shipping_identifier_results(identifier_query)
    )

    selected_schedule_status = str(
        request.GET.get("todo_filter_schedule_status") if request else ""
    ).strip()
    if selected_schedule_status not in _SCHEDULE_STATUSES:
        selected_schedule_status = ""

    selected_tasks = []
    if selected_type != "fbs_movement":
        for column in panel.get("task_panel_columns") or []:
            schedule_status = str(column.get("status") or "")
            if selected_schedule_status and schedule_status != selected_schedule_status:
                continue
            if not selected_schedule_status and schedule_status == "done":
                continue
            selected_tasks.extend(
                (task, schedule_status)
                for task in column.get("tasks") or []
            )

    shipping_order_pks = {
        order_pk
        for task, _schedule_status in selected_tasks
        if (order_pk := _shipping_order_pk(task)) is not None
    }
    shipping_orders = {
        order.pk: order
        for order in ShippingOrder.objects.filter(pk__in=shipping_order_pks).only(
            "id",
            "delivery_type",
            "destination_warehouse",
            "destination_address",
            "transit_address",
        )
    }
    trip_pks = {
        trip_pk
        for task, _schedule_status in selected_tasks
        if (trip_pk := _trip_pk(task)) is not None
    }
    trip_otg_orders = defaultdict(list)
    for link in (
        LogisticsTripOrder.objects.filter(trip_id__in=trip_pks)
        .select_related("shipping_order", "shipping_order__agency")
        .order_by("trip_id", "loading_sequence", "delivery_sequence", "id")
    ):
        shipping_order = link.shipping_order
        trip_otg_orders[link.trip_id].append(
            {
                "number": format_order_number("shipping", shipping_order.number),
                "client_label": _agency_label(shipping_order.agency),
                "status_label": shipping_order.get_status_display(),
            }
        )
    attach_warehouse_deadline_context(task for task, _schedule_status in selected_tasks)
    rows = [
        _table_row(
            task,
            schedule_status,
            shipping_order=shipping_orders.get(_shipping_order_pk(task)),
            trip_otg_orders=trip_otg_orders.get(_trip_pk(task)),
        )
        for task, schedule_status in selected_tasks
    ]
    for row in fbs_rows_for_type:
        schedule_status = row["_schedule_status"]
        if selected_schedule_status and schedule_status != selected_schedule_status:
            continue
        if not selected_schedule_status and schedule_status == "done":
            continue
        rows.append(row)
    rows.sort(key=lambda row: row.get("_sort_value", 0), reverse=True)

    stat_links = []
    for stat in panel.get("task_panel_stats") or []:
        status = str(stat.get("status") or "")
        query = request.GET.copy() if request else None
        if query is not None:
            query["todo_filters_applied"] = "1"
            query["todo_filter_type"] = str(panel.get("task_panel_active_filter_type") or "all")
            selected_client = str(panel.get("task_panel_selected_client") or "")
            selected_query = str(panel.get("task_panel_search_query") or "")
            selected_document_status = str(
                panel.get("task_panel_selected_document_status") or ""
            )
            for name, value in (
                ("todo_filter_client", selected_client),
                ("todo_filter_query", selected_query),
                ("todo_filter_document_status", selected_document_status),
            ):
                if value:
                    query[name] = value
                else:
                    query.pop(name, None)
            query["todo_page_size"] = str(
                _positive_int(request.GET.get("todo_page_size"), 10)
            )
            query.pop("todo_page", None)
            if selected_schedule_status == status:
                query.pop("todo_filter_schedule_status", None)
            else:
                query["todo_filter_schedule_status"] = status
            filter_url = f"?{query.urlencode()}"
        else:
            filter_url = f"?todo_filters_applied=1&todo_filter_schedule_status={status}"
        stat_links.append(
            {
                **stat,
                "active": selected_schedule_status == status,
                "filter_url": filter_url,
            }
        )

    requested_page_size = _positive_int(request.GET.get("todo_page_size") if request else None, 10)
    page_size = requested_page_size if requested_page_size in _PAGE_SIZES else 10
    paginator = Paginator(rows, page_size)
    page_obj = paginator.get_page(request.GET.get("todo_page") if request else None)

    query_params = request.GET.copy() if request else None
    if query_params is not None:
        query_params.pop("todo_page", None)
        query_params["todo_page_size"] = str(page_size)
        page_query = query_params.urlencode()
        hidden_query = [
            {"name": name, "value": value}
            for name, values in request.GET.lists()
            if name not in {"todo_page", "todo_page_size"}
            for value in values
        ]
    else:
        page_query = ""
        hidden_query = []

    start_index = page_obj.start_index() if paginator.count else 0
    end_index = page_obj.end_index() if paginator.count else 0
    return {
        **panel,
        "task_panel_stats": stat_links,
        "request_selected_schedule_status": selected_schedule_status,
        "identifier_search_query": identifier_query,
        "identifier_search_performed": (
            selected_type != "fbs_movement"
            and len(identifier_query) >= _IDENTIFIER_SEARCH_MIN_LENGTH
        ),
        "identifier_search_results": identifier_results,
        "identifier_search_result_count": len(identifier_results),
        "request_rows": list(page_obj.object_list),
        "request_page_obj": page_obj,
        "request_page_links": _page_links(page_obj),
        "request_page_query_prefix": f"{page_query}&" if page_query else "",
        "request_page_sizes": _PAGE_SIZES,
        "request_page_size": page_size,
        "request_hidden_query": hidden_query,
        "request_start_index": start_index,
        "request_end_index": end_index,
        "request_total": paginator.count,
    }
