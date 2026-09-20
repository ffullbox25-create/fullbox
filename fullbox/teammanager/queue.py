"""Single read model for the manager queue screen."""

from __future__ import annotations

import math
from collections import Counter
from datetime import timedelta
from urllib.parse import urlencode

from django.db.models import Q, Sum
from django.utils import timezone

from billing.permissions import filter_agencies_for_user
from client_cabinet.lk_status_map import resolve_lk_entry_status, resolve_lk_request_status
from client_cabinet.models import ChatThread
from employees.access import employee_has_role, get_request_employee
from fbs.models import FbsExternalIssue
from fbs.services.external_issues import client_portal_issue_queryset
from sku.models import Agency
from todo.deadlines import WAREHOUSE_EXECUTOR_ROLES, canonical_warehouse_request_route
from todo.models import Task, WarehouseRequestDeadlineChange

from .handoff import REASSIGN_POWER_ROLES, TRANSFER_REASONS, cabinet_colleagues, covered_principal_ids
from .queue_states import (
    QUEUE_STATE_LABELS,
    QUEUE_STATES,
    STUCK_AFTER_HOURS,
    is_trip_confirmation,
    is_waiting_manager_act,
    queue_state,
)
from .views import (
    _attach_shipping_task_orders,
    _entry_key,
    _latest_request_entries,
    _preload_audit_payloads,
    _preload_task_row_caches,
    _quick_entry_status,
    _request_url,
    _task_order_ref,
    _task_row,
)


MANAGER_QUEUE_HARD_LIMIT = 1000
MANAGER_QUEUE_PAGE_SIZE = 25
QUEUE_DUE_OPTIONS = (
    ("all", "Любой срок"),
    ("today", "Сегодня"),
    ("tomorrow", "Завтра"),
    ("week", "7 дней"),
    ("overdue", "Просрочено"),
)
QUEUE_SCOPE_OPTIONS = (
    ("all", "Все задачи"),
    ("mine", "Мои и замещаемые"),
    ("unassigned", "Без исполнителя"),
)


def _clean_filters(request) -> dict:
    state = str(request.GET.get("state") or "").strip()
    if state not in QUEUE_STATE_LABELS:
        state = ""
    due = str(request.GET.get("due") or "all").strip()
    if due not in dict(QUEUE_DUE_OPTIONS):
        due = "all"
    scope = str(request.GET.get("scope") or "all").strip()
    if scope not in {key for key, _label in QUEUE_SCOPE_OPTIONS}:
        scope = "all"
    return {
        "state": state,
        "client": str(request.GET.get("client") or "all").strip(),
        "warehouse": str(request.GET.get("warehouse") or "all").strip(),
        "due": due,
        "scope": scope,
        "q": " ".join(str(request.GET.get("q") or "").split())[:160],
    }


def _query_url(filters: dict, **changes) -> str:
    values = {**filters, **changes}
    params = []
    for key in ("state", "client", "warehouse", "due", "scope", "q", "page"):
        value = values.get(key)
        if value in (None, "", "all"):
            continue
        if key == "page" and str(value) == "1":
            continue
        params.append((key, value))
    query = urlencode(params)
    return f"/team-manager/queue/?{query}" if query else "/team-manager/queue/"


def _apply_due_filter(queryset, due: str, now):
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    if due == "today":
        return queryset.filter(due_date__gte=today, due_date__lt=tomorrow)
    if due == "tomorrow":
        return queryset.filter(due_date__gte=tomorrow, due_date__lt=tomorrow + timedelta(days=1))
    if due == "week":
        return queryset.filter(due_date__gte=today, due_date__lt=today + timedelta(days=7))
    if due == "overdue":
        return queryset.filter(due_date__lt=now)
    return queryset


def _request_status(entry):
    if entry is None:
        return resolve_lk_request_status(
            bucket="manager",
            status_label="Задача менеджера",
            source="manager-queue:task",
        )
    order_type = str(getattr(entry, "order_type", "") or "").strip().lower()
    if order_type == "shipping":
        quick = _quick_entry_status(entry, payload=getattr(entry, "payload", None))
        return resolve_lk_request_status(
            bucket=quick.get("bucket") or "manager",
            status_label=quick.get("label") or "Заявка на отгрузку",
            payload=getattr(entry, "payload", None),
            source="manager-queue:shipping-batch",
        )
    return resolve_lk_entry_status(entry, audience="default", use_live_warehouse=False)


def _expected_caches(tasks, latest_by_key):
    routing_ids = set()
    billing_refs = set()
    for task in tasks:
        shipping_order = getattr(task, "_shipping_order_cache", None)
        if shipping_order is not None:
            if getattr(shipping_order, "pk", None):
                routing_ids.add(int(shipping_order.pk))
            number = str(getattr(shipping_order, "number", "") or "").strip()
            if number:
                billing_refs.add(("shipping", number))
            continue
        ref = _task_order_ref(task)
        if ref and ref[0] in {"receiving", "processing"}:
            billing_refs.add((str(ref[0]), str(ref[1])))
    return routing_ids, billing_refs


def _warehouse_deadline_context(tasks):
    routes = {
        canonical_warehouse_request_route(getattr(task, "route", ""))
        for task in tasks
    }
    routes.discard("")
    if not routes:
        return {}, {}, {}

    route_filter = Q(pk__in=[])
    for route in sorted(routes):
        route_filter |= Q(route__startswith=route)
    anchors = (
        Task.objects.filter(route_filter, assigned_to__role__in=WAREHOUSE_EXECUTOR_ROLES)
        .exclude(status="done")
        .select_related("assigned_to")
        .order_by("due_date", "id")
    )
    anchor_by_route = {}
    due_by_route = {}
    for anchor in anchors:
        route = canonical_warehouse_request_route(anchor.route)
        if route and route not in anchor_by_route:
            anchor_by_route[route] = anchor.id
            due_by_route[route] = anchor.due_date

    count_by_route = Counter()
    latest_due_by_route = {}
    changes = WarehouseRequestDeadlineChange.objects.filter(request_route__in=routes).order_by(
        "request_route", "-created_at", "-id"
    )
    for change in changes:
        count_by_route[change.request_route] += 1
        latest_due_by_route.setdefault(change.request_route, change.due_date)
    for route, due_date in latest_due_by_route.items():
        due_by_route[route] = due_date
    return anchor_by_route, dict(count_by_route), due_by_route


def _chat_urls(rows):
    client_ids = {int(row["client_id"]) for row in rows if row.get("client_id")}
    if not client_ids:
        return {}
    threads = ChatThread.objects.filter(
        agency_id__in=client_ids,
        kind=ChatThread.KIND_CLIENT_GENERAL,
        is_archived=False,
    ).order_by("agency_id", "-updated_at", "-id")
    result = {}
    for thread in threads:
        result.setdefault(
            thread.agency_id,
            f"/team-manager/chats/?thread={thread.id}&kind=clients",
        )
    return result


def _row_matches(row: dict, filters: dict) -> bool:
    client = filters["client"]
    if client != "all" and str(row.get("client_id") or "") != client:
        return False
    warehouse = filters["warehouse"]
    if warehouse != "all" and str(row.get("warehouse") or "") != warehouse:
        return False
    query = filters["q"].casefold()
    if query:
        haystack = " ".join(
            str(row.get(key) or "")
            for key in ("number", "title", "client_name", "warehouse", "executor", "status", "next_step")
        ).casefold()
        if query not in haystack:
            return False
    return True


def _state_explanation(state: str, row: dict, request_status) -> str:
    if state == "to_confirm":
        if row.get("trip_confirmation"):
            return "Открыть рейс и отметить результат по каждой заявке"
        return str(getattr(request_status, "status_label", "") or "Акт ожидает подписи менеджера")
    if state == "to_accept":
        return str(getattr(request_status, "status_label", "") or "Нужно принять задачу в работу")
    if state == "waiting_client":
        return str(getattr(request_status, "status_label", "") or "Следующий шаг — у клиента")
    if state == "waiting_warehouse":
        return str(getattr(request_status, "status_label", "") or "Следующий шаг — у склада")
    if state == "waiting_logistics":
        return row.get("logistics_status") or "Заявка находится в маршрутизации"
    if state == "overdue":
        return "Срок задачи истёк — требуется решение менеджера"
    if state == "stuck":
        return f"Нет обновлений более {STUCK_AFTER_HOURS} часов"
    return str(getattr(request_status, "status_label", "") or "Работа завершена")


def _pending_external_issues(request):
    agencies = filter_agencies_for_user(Agency.objects.all(), request)
    return client_portal_issue_queryset(
        FbsExternalIssue.objects.filter(
            agency__in=agencies,
            status__in=("reserved", "picking"),
        )
    ).filter(manager_approved=False)


def _external_issue_rows(queryset):
    issues = list(
        queryset.select_related("agency")
        .annotate(requested_qty_total=Sum("lines__requested_qty"))
        .order_by("-created_at", "-id")[:MANAGER_QUEUE_HARD_LIMIT]
    )
    rows = []
    for issue in issues:
        quantity = int(issue.requested_qty_total or 0)
        next_step = f"Подтвердить или отклонить выдачу {quantity} шт. со склада FBS"
        rows.append(
            {
                "id": f"fbs-out-{issue.pk}",
                "number": issue.number,
                "title": " ".join(
                    value
                    for value in (
                        str(issue.reference or "").strip(),
                        str(issue.recipient or "").strip(),
                        str(issue.basis or "").strip(),
                    )
                    if value
                ),
                "type_label": "Вывоз товара из FBS",
                "executor": "Менеджер",
                "client_id": issue.agency_id,
                "client_name": (
                    issue.agency.agn_name
                    or issue.agency.short_name
                    or f"Клиент #{issue.agency_id}"
                ),
                "warehouse": "FBS",
                "status": "Ожидает подтверждения менеджером",
                "next_step": next_step,
                "state": "to_confirm",
                "state_label": QUEUE_STATE_LABELS["to_confirm"],
                "state_explanation": next_step,
                "open_url": f"/team-manager/fbs/movements/?{urlencode({'q': issue.number})}",
                "can_claim": False,
                "can_transfer": False,
                "deadline": None,
                "updated_at": issue.created_at,
                "warehouse_deadline_task_id": None,
                "warehouse_deadline_change_count": 0,
                "warehouse_deadline_value": "",
                "act_sign_url": "",
                "trip_action_url": "",
                "trip_confirmation": False,
                "waiting_manager_act": False,
                "process_label": "",
            }
        )
    return rows


def manager_queue(request) -> dict:
    """Return the one source used for queue rows, counters and pagination."""

    now = timezone.localtime()
    filters = _clean_filters(request)
    employee = get_request_employee(request)
    covered_ids = set(covered_principal_ids(employee)) if employee else set()
    can_manage_all = bool(
        employee
        and any(employee_has_role(employee, role) for role in REASSIGN_POWER_ROLES)
    )
    degraded = set()

    base_tasks = (
        Task.objects.select_related("assigned_to")
        .exclude(status="done")
        .filter(Q(assigned_to__role__in=("manager", "logistician")) | Q(assigned_to__isnull=True))
    )
    pending_external_issues = _pending_external_issues(request)
    active_total = base_tasks.count() + pending_external_issues.count()
    scoped_tasks = base_tasks
    if filters["scope"] == "mine":
        visible_ids = set(covered_ids)
        if employee:
            visible_ids.add(employee.id)
        scoped_tasks = scoped_tasks.filter(assigned_to_id__in=visible_ids)
    elif filters["scope"] == "unassigned":
        scoped_tasks = scoped_tasks.filter(assigned_to__isnull=True)
    scoped_tasks = _apply_due_filter(scoped_tasks, filters["due"], now)
    tasks = list(scoped_tasks.order_by("due_date", "-updated_at", "id")[:MANAGER_QUEUE_HARD_LIMIT])

    entries = _latest_request_entries(limit=MANAGER_QUEUE_HARD_LIMIT, include_latest_payload=True)
    latest_by_key = {_entry_key(entry): entry for entry in entries}
    try:
        _attach_shipping_task_orders(tasks, latest_by_key)
    except Exception:
        degraded.add("logistics")

    linked_entries = []
    for task in tasks:
        ref = _task_order_ref(task)
        entry = latest_by_key.get(ref) if ref else None
        if entry is not None:
            linked_entries.append(entry)
    _preload_audit_payloads(linked_entries)

    routing_ids, billing_refs = _expected_caches(tasks, latest_by_key)
    try:
        routing_cache, billing_cache = _preload_task_row_caches(tasks, latest_by_key)
    except Exception:
        routing_cache, billing_cache = {}, {}
        degraded.update({"logistics", "billing"})
    if routing_ids.difference(set(routing_cache)):
        degraded.add("logistics")
    if billing_refs.difference(set(billing_cache)):
        degraded.add("billing")

    anchor_by_route, deadline_counts, deadline_due_dates = _warehouse_deadline_context(tasks)
    rows = []
    for task in tasks:
        ref = _task_order_ref(task)
        entry = latest_by_key.get(ref) if ref else None
        payload = getattr(entry, "payload", None) if entry is not None else {}
        payload = payload if isinstance(payload, dict) else {}
        try:
            request_status = _request_status(entry)
        except Exception:
            degraded.add("statuses")
            request_status = resolve_lk_request_status(
                bucket="manager",
                status_label="Статус заявки временно недоступен",
                source="manager-queue:fallback",
            )
        row = _task_row(task, latest_by_key, now, live_status=False)
        shipping_order = getattr(task, "_shipping_order_cache", None)
        routing_snapshot = routing_cache.get(getattr(shipping_order, "pk", None), {}) if shipping_order else {}
        routing_status = str(routing_snapshot.get("status") or "")
        state = queue_state(
            task,
            request_status=request_status,
            payload=payload,
            routing_status=routing_status,
            now=now,
        )
        row["state"] = state
        row["state_label"] = QUEUE_STATE_LABELS[state]
        row["trip_confirmation"] = is_trip_confirmation(task)
        row["waiting_manager_act"] = is_waiting_manager_act(task, payload)
        row["state_explanation"] = _state_explanation(state, row, request_status)
        row["open_url"] = (
            _request_url(entry, shipping_order=shipping_order)
            if entry is not None
            else (task.route or "/team-manager/?section=tasks")
        )
        row["can_claim"] = bool(
            employee and (not task.assigned_to_id or task.assigned_to_id == employee.id)
        )
        row["can_transfer"] = bool(
            employee
            and (
                can_manage_all
                or not task.assigned_to_id
                or task.assigned_to_id == employee.id
                or task.assigned_to_id in covered_ids
            )
        )
        route = canonical_warehouse_request_route(task.route)
        row["warehouse_deadline_task_id"] = anchor_by_route.get(route)
        row["warehouse_deadline_change_count"] = int(deadline_counts.get(route) or 0)
        effective_due = deadline_due_dates.get(route)
        row["warehouse_deadline_value"] = (
            timezone.localtime(effective_due).strftime("%Y-%m-%dT%H:%M") if effective_due else ""
        )
        row["act_sign_url"] = ""
        if state == "to_confirm" and row["waiting_manager_act"] and ref and ref[0] == "receiving":
            row["act_sign_url"] = f"/orders/receiving/{ref[1]}/act/sign/manager/"
        row["trip_action_url"] = task.route if row["trip_confirmation"] else ""
        rows.append(row)

    external_rows = []
    if filters["scope"] != "mine" and filters["due"] == "all":
        external_rows = _external_issue_rows(pending_external_issues)
        rows = external_rows + rows

    chat_urls = _chat_urls(rows)
    for row in rows:
        client_id = row.get("client_id")
        row["chat_url"] = chat_urls.get(client_id, "/team-manager/chats/?kind=clients") if client_id else ""

    client_options = []
    seen_clients = set()
    for row in sorted(rows, key=lambda item: str(item.get("client_name") or "").casefold()):
        client_id = row.get("client_id")
        if not client_id or client_id in seen_clients:
            continue
        seen_clients.add(client_id)
        client_options.append({"id": client_id, "name": row.get("client_name") or f"Клиент #{client_id}"})
    warehouse_options = sorted(
        {
            str(row.get("warehouse") or "").strip()
            for row in rows
            if str(row.get("warehouse") or "").strip() not in {"", "—"}
        }
    )

    universe = [row for row in rows if _row_matches(row, filters)]
    state_counter = Counter(row["state"] for row in universe)
    counts = {key: int(state_counter.get(key) or 0) for key, _label in QUEUE_STATES}
    total = len(universe)
    selected_state = filters["state"]
    state_rows = [row for row in universe if not selected_state or row["state"] == selected_state]
    filtered_total = len(state_rows)

    try:
        requested_page = max(int(request.GET.get("page") or 1), 1)
    except (TypeError, ValueError):
        requested_page = 1
    num_pages = max(math.ceil(filtered_total / MANAGER_QUEUE_PAGE_SIZE), 1)
    page = min(requested_page, num_pages)
    start = (page - 1) * MANAGER_QUEUE_PAGE_SIZE
    page_rows = state_rows[start : start + MANAGER_QUEUE_PAGE_SIZE]
    for row in page_rows:
        row.pop("_task_obj", None)

    state_cards = []
    for key in ("to_accept", "to_confirm", "waiting_client", "waiting_warehouse", "overdue", "stuck"):
        state_cards.append(
            {
                "key": key,
                "label": QUEUE_STATE_LABELS[key],
                "count": counts[key],
                "url": _query_url(filters, state=key, page=1),
                "active": selected_state == key,
            }
        )

    page_links = [
        {"number": number, "url": _query_url(filters, page=number), "active": number == page}
        for number in range(1, num_pages + 1)
    ]
    recent_events = sorted(
        universe,
        key=lambda row: row.get("updated_at") or now - timedelta(days=36500),
        reverse=True,
    )[:5]
    billing_attention_count = sum(
        1 for row in universe if str(row.get("process_label") or "").strip() == "Биллинг"
    )

    degraded_labels = {
        "billing": "биллинг",
        "logistics": "логистика",
        "statuses": "статусы заявок",
    }
    ordered_degraded = [key for key in ("billing", "logistics", "statuses") if key in degraded]
    return {
        "rows": page_rows,
        "counts": counts,
        "filters": filters,
        "total": total,
        "active_total": active_total,
        "filtered_total": filtered_total,
        "range_start": start + 1 if filtered_total else 0,
        "range_end": min(start + len(page_rows), filtered_total),
        "page": page,
        "num_pages": num_pages,
        "page_links": page_links,
        "prev_url": _query_url(filters, page=page - 1) if page > 1 else "",
        "next_url": _query_url(filters, page=page + 1) if page < num_pages else "",
        "state_cards": state_cards,
        "state_labels": QUEUE_STATE_LABELS,
        "selected_state_label": QUEUE_STATE_LABELS.get(selected_state, "Все состояния"),
        "clear_state_url": _query_url(filters, state="", page=1),
        "clear_client_url": _query_url(filters, client="all", page=1),
        "client_options": client_options,
        "warehouse_options": warehouse_options,
        "due_options": QUEUE_DUE_OPTIONS,
        "scope_options": QUEUE_SCOPE_OPTIONS,
        "degraded": ordered_degraded,
        "degraded_text": ", ".join(degraded_labels[key] for key in ordered_degraded),
        "source_truncated": (
            len(tasks) >= MANAGER_QUEUE_HARD_LIMIT
            or len(external_rows) >= MANAGER_QUEUE_HARD_LIMIT
        ),
        "source_limit": MANAGER_QUEUE_HARD_LIMIT,
        "recent_events": recent_events,
        "billing_attention_count": billing_attention_count,
        "cabinet_colleagues": [
            {"id": item.id, "name": item.full_name or f"#{item.id}", "role": item.role}
            for item in cabinet_colleagues(exclude_id=getattr(employee, "id", None))
        ],
        "transfer_reasons": [{"key": key, "label": label} for key, label in TRANSFER_REASONS],
        "deadline_min": (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"),
    }
