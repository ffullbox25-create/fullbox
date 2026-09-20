import re
from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView

from audit.models import OrderAuditEntry
from employees.access import RoleRequiredMixin, get_request_employee
from employees.models import Employee
from marking.models import MarkingCode
from sklad.models import WarehouseStockSnapshot
from sku.models import SKUBarcode
from todo.models import Task, TaskPanelSnapshot


DASHBOARD_STATUSES = (
    ("new", "Новые"),
    ("planning", "Требуют планирования"),
    ("in_work", "В работе"),
    ("review", "На проверке"),
    ("overdue", "Просроченные"),
    ("blocked", "Заблокированные"),
    ("ready", "Готово"),
    ("all", "Все заявки"),
)

DOCUMENT_FILTERS = (
    ("all", "Все документы"),
    ("processing", "Заявки на обработку"),
    ("receiving", "Заявки на приемку"),
    ("other", "Другие заявки"),
)

PERIOD_FILTERS = (
    ("today", "Период: Сегодня"),
    ("week", "Период: 7 дней"),
    ("all", "Период: Все"),
)

PROCESSING_ORDER_Q = (
    Q(route__contains="/orders/processing/")
    | Q(panel_snapshots__role_key="processing_head", panel_snapshots__task_route__contains="/orders/processing/")
)
REQUESTS_SECTION_ORDER_Q = (
    PROCESSING_ORDER_Q
    | Q(route__contains="/orders/receiving/")
    | Q(panel_snapshots__task_route__contains="/orders/receiving/")
    | Q(route__contains="/team-manager/other-requests/")
    | Q(panel_snapshots__task_route__contains="/team-manager/other-requests/")
    | Q(route__contains="/other-requests/")
    | Q(panel_snapshots__task_route__contains="/other-requests/")
)
PROCESSING_WORKFLOW_ROLE_Q = (
    Q(panel_snapshots__role_key="processing_head", panel_snapshots__is_hidden=False)
    | Q(assigned_to__role__in=("processing_head", "processing_worker"))
    | Q(observer__role__in=("processing_head", "processing_worker"))
)
PROCESSING_DETAIL_ROUTE_RE = re.compile(r"^/orders/processing/([^/?#]+)/?$")
PROCESSING_ROUTE_RE = re.compile(r"/orders/processing/([^/?#]+)")
RECEIVING_ROUTE_RE = re.compile(r"/orders/receiving/([^/?#]+)")
OTHER_REQUEST_ROUTE_RE = re.compile(r"/(?:team-manager/)?other-requests/([^/?#]+)")


def _format_datetime(value):
    if not value:
        return "-"
    return timezone.localtime(value).strftime("%d.%m.%Y %H:%M")


def _short_name(full_name):
    parts = [part for part in str(full_name or "").split() if part]
    if not parts:
        return "-"
    surname = parts[0]
    initials = "".join(f"{part[:1].upper()}." for part in parts[1:3])
    return f"{surname} {initials}".strip()



def _repair_mojibake_text(value):
    text = str(value or "")
    if not text or not any(marker in text for marker in ("Р—", "Р°", "Рџ", "Рќ", "РЎ", "Р ", "Р\x98", "в„")):
        return text
    for source_encoding, target_encoding in (
        ("cp1251", "utf-8"),
        ("latin-1", "utf-8"),
        ("latin-1", "cp1251"),
        ("cp1252", "utf-8"),
    ):
        try:
            repaired = text.encode(source_encoding).decode(target_encoding)
        except UnicodeError:
            continue
        if repaired and repaired != text and sum(repaired.count(marker) for marker in ("Р", "СЃ", "в„")) < sum(text.count(marker) for marker in ("Р", "СЃ", "в„")):
            return repaired
    return text

def _initials(full_name):
    parts = [part for part in str(full_name or "").split() if part]
    if not parts:
        return "Р"
    return "".join(part[:1].upper() for part in parts[:2])


def _document_type(route):
    route_value = str(route or "")
    if "/orders/receiving/" in route_value:
        return "receiving"
    if "/orders/processing/" in route_value:
        return "processing"
    return "other"


def _document_identity(route, task_id):
    route_value = str(route or "")
    processing_match = PROCESSING_ROUTE_RE.search(route_value)
    if processing_match:
        return "processing", processing_match.group(1), f"processing:{processing_match.group(1)}"
    receiving_match = RECEIVING_ROUTE_RE.search(route_value)
    if receiving_match:
        return "receiving", receiving_match.group(1), f"receiving:{receiving_match.group(1)}"
    other_match = OTHER_REQUEST_ROUTE_RE.search(route_value)
    if other_match:
        return "other", other_match.group(1), f"other:{other_match.group(1)}"
    return _document_type(route_value), "", f"task:{task_id}"


def _processing_work_url(url):
    url_value = str(url or "").strip()
    match = PROCESSING_DETAIL_ROUTE_RE.match(url_value)
    if match:
        return f"/orders/processing/{match.group(1)}/work/"
    return url_value


def _bucket_for_task(task, now):
    if task.status == "done":
        return "ready"
    if task.status == "blocked":
        return "review"
    if task.due_date and task.due_date < now:
        return "overdue"
    if task.status == "backlog":
        return "new"
    return "in_work"


def _queue_bucket_for_row(row, *, task, now):
    status_text = f"{row.get('status_label') or ''} {row.get('title') or ''}".casefold()
    if row["bucket"] == "ready":
        return "ready"
    if task.status == "blocked" or any(token in status_text for token in ("заблок", "проблем", "ошибк")):
        return "blocked"
    if task.due_date and task.due_date < now:
        return "overdue"
    if row["bucket"] == "new":
        return "new"
    if any(token in status_text for token in ("провер", "готова к закрыт", "готово к закрыт")):
        return "review"
    if row["executor"] == "Не назначен":
        return "new"
    if any(token in status_text for token in ("утверждено менеджером", "подтвержден", "создано перемещение", "ждет план")):
        return "planning"
    return "in_work"


def _row_tone(row):
    queue_bucket = row.get("queue_bucket") or row["bucket"]
    status_text = str(row.get("status_label") or "").casefold()
    if "отмен" in status_text:
        return "danger"
    if queue_bucket == "overdue":
        return "danger"
    if queue_bucket in {"review", "planning", "blocked", "new"}:
        return "warning"
    if queue_bucket == "ready":
        return "success"
    return "info"


def _snapshot_map(tasks):
    task_ids = [task.id for task in tasks]
    if not task_ids:
        return {}
    snapshots = TaskPanelSnapshot.objects.filter(
        task_id__in=task_ids,
        role_key="processing_head",
        is_hidden=False,
    )
    return {snapshot.task_id: snapshot for snapshot in snapshots}


def _processing_client_labels(tasks, snapshots):
    order_ids_by_type = {"processing": set(), "receiving": set()}
    for task in tasks:
        snapshot = snapshots.get(task.id)
        route = (snapshot.task_route if snapshot else "") or task.route or ""
        processing_match = PROCESSING_ROUTE_RE.search(str(route))
        if processing_match:
            order_ids_by_type["processing"].add(processing_match.group(1))
            continue
        receiving_match = RECEIVING_ROUTE_RE.search(str(route))
        if receiving_match:
            order_ids_by_type["receiving"].add(receiving_match.group(1))
    if not any(order_ids_by_type.values()):
        return {}
    entries = (
        OrderAuditEntry.objects.filter(
            order_type__in=[key for key, values in order_ids_by_type.items() if values],
            agency__isnull=False,
        )
        .filter(
            Q(order_type="processing", order_id__in=order_ids_by_type["processing"])
            | Q(order_type="receiving", order_id__in=order_ids_by_type["receiving"])
        )
        .select_related("agency")
        .order_by("order_type", "order_id", "-created_at", "-id")
    )
    labels = {}
    for entry in entries:
        key = (entry.order_type, entry.order_id)
        if key in labels:
            continue
        labels[key] = entry.agency.short_name or entry.agency.agn_name or f"Клиент {entry.agency_id}"
    return labels


def _build_task_rows(tasks, *, now):
    snapshots = _snapshot_map(tasks)
    client_labels = _processing_client_labels(tasks, snapshots)
    rows = []
    for task in tasks:
        snapshot = snapshots.get(task.id)
        bucket = _bucket_for_task(task, now)
        route = (snapshot.task_route if snapshot else "") or task.route or ""
        document_type = _document_type(route)
        document_label = dict(DOCUMENT_FILTERS).get(document_type, "Задача")
        executor_label = ""
        if snapshot and snapshot.executor_label:
            executor_label = snapshot.executor_label
        elif task.assigned_to:
            executor_label = _short_name(task.assigned_to.full_name)
        else:
            executor_label = "Не назначен"
        title = _repair_mojibake_text((snapshot.panel_title if snapshot else "") or task.display_title())
        route_match = PROCESSING_ROUTE_RE.search(str(route))
        receiving_match = RECEIVING_ROUTE_RE.search(str(route))
        document_identity_type, order_id, document_key = _document_identity(route, task.id)
        client_key = ("processing", order_id) if route_match else ("receiving", order_id) if receiving_match else ("other", order_id)
        client_label = (snapshot.order_client_label if snapshot else "") or client_labels.get(client_key) or "-"
        status_label = _repair_mojibake_text(
            (snapshot.order_status_label if snapshot else "")
            or dict(DASHBOARD_STATUSES).get(bucket, task.get_status_display())
        )
        raw_url = (snapshot.panel_url if snapshot else "") or route or reverse("todo:detail", args=[task.id])
        row_url = _processing_work_url(raw_url)
        if document_identity_type == "processing" and order_id:
            row_url = f"/orders/processing/{order_id}/work/"
        row = {
            "id": task.id,
            "title": title,
            "client": client_label,
            "executor": executor_label,
            "updated_at": (snapshot.panel_updated_at_label if snapshot else "") or _format_datetime(task.updated_at),
            "updated_at_value": task.updated_at,
            "due_label": _format_datetime(task.due_date),
            "status_label": status_label,
            "bucket": bucket,
            "document_type": document_identity_type or document_type,
            "document_label": document_label,
            "document_key": document_key,
            "order_id": order_id,
            "url": row_url,
            "priority": task.priority,
            "priority_label": task.get_priority_display(),
            "is_unassigned": executor_label == "Не назначен",
            "sla_label": "Просрочено" if task.due_date and task.due_date < now else "В срок",
        }
        row["queue_bucket"] = _queue_bucket_for_row(row, task=task, now=now)
        row["tone"] = _row_tone(row)
        rows.append(row)
    return rows


def _dedupe_request_rows(rows):
    priority_order = {"overdue": 0, "blocked": 1, "review": 2, "new": 3, "planning": 4, "in_work": 5, "ready": 9}

    def score(row):
        updated_at = row.get("updated_at_value")
        return (
            priority_order.get(row.get("queue_bucket"), 9),
            0 if row.get("priority") == "urgent" else 1,
            -(updated_at.timestamp() if updated_at else 0),
        )

    grouped = {}
    for row in rows:
        key = row.get("document_key") or f"row:{row.get('id')}"
        current = grouped.get(key)
        if current is None or score(row) < score(current):
            grouped[key] = row
    return list(grouped.values())


def _metric_cards(rows, *, request=None):
    counts = {status_key: 0 for status_key, _label in DASHBOARD_STATUSES}
    active_rows = [row for row in rows if row["queue_bucket"] != "ready"]
    counts["all"] = len(active_rows)
    today = timezone.localdate()
    counts["ready_today"] = len(
        [
            row
            for row in rows
            if row["queue_bucket"] == "ready"
            and row.get("updated_at_value")
            and timezone.localtime(row["updated_at_value"]).date() == today
        ]
    )
    for row in rows:
        counts[row["queue_bucket"]] = counts.get(row["queue_bucket"], 0) + 1
    cards = [
        {
            "key": "all",
            "label": "Активные заявки",
            "value": counts["all"],
            "hint": "без закрытого архива",
            "tone": "accent",
        },
        {
            "key": "new",
            "label": "Новые",
            "value": counts["new"],
            "hint": "ожидают руководителя",
            "tone": "warning",
        },
        {
            "key": "overdue",
            "label": "Просрочены",
            "value": counts["overdue"],
            "hint": "требуют внимания",
            "tone": "danger",
        },
        {
            "key": "in_work",
            "label": "В работе",
            "value": counts["in_work"],
            "hint": "активные заявки",
            "tone": "warning",
        },
        {
            "key": "review",
            "label": "На проверке",
            "value": counts["review"],
            "hint": "нужна приемка работы",
            "tone": "warning",
        },
        {
            "key": "ready",
            "label": "Готово сегодня",
            "value": counts["ready_today"],
            "hint": "закрытые за день",
            "tone": "success",
        },
    ]
    if request is not None:
        for card in cards:
            card["url"] = f"/processing-head/requests/?tab={card['key']}&period=all"
    return cards


def _period_start(period_key, now):
    today = timezone.localdate()
    if period_key == "week":
        return timezone.make_aware(
            timezone.datetime.combine(today - timedelta(days=6), timezone.datetime.min.time())
        )
    if period_key == "today":
        return timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
    return None


def _apply_filters(rows, request, *, now, default_period="today"):
    active_tab = request.GET.get("tab") or "all"
    if active_tab not in {key for key, _label in DASHBOARD_STATUSES}:
        active_tab = "all"
    document_filter = request.GET.get("document") or "all"
    if document_filter not in {key for key, _label in DOCUMENT_FILTERS}:
        document_filter = "all"
    period_filter = request.GET.get("period") or default_period
    if period_filter not in {key for key, _label in PERIOD_FILTERS}:
        period_filter = "today"
    client_filter = request.GET.get("client") or ""
    executor_filter = request.GET.get("executor") or ""
    priority_filter = request.GET.get("priority") or ""
    raw_query = " ".join(str(request.GET.get("q") or "").split())
    query = raw_query.casefold()

    filtered_rows = list(rows)
    if active_tab != "all":
        filtered_rows = [row for row in filtered_rows if row["queue_bucket"] == active_tab]
    if document_filter != "all":
        filtered_rows = [row for row in filtered_rows if row["document_type"] == document_filter]
    if client_filter:
        filtered_rows = [row for row in filtered_rows if row["client"] == client_filter]
    if executor_filter:
        if executor_filter == "__none__":
            filtered_rows = [row for row in filtered_rows if row["is_unassigned"]]
        else:
            filtered_rows = [row for row in filtered_rows if row["executor"] == executor_filter]
    if priority_filter:
        filtered_rows = [row for row in filtered_rows if row["priority"] == priority_filter]
    period_start = _period_start(period_filter, now)
    if period_start:
        filtered_rows = [
            row
            for row in filtered_rows
            if row.get("updated_at_value") and row["updated_at_value"] >= period_start
        ]
    if query:
        filtered_rows = [
            row
            for row in filtered_rows
            if query in " ".join(
                [
                    row["title"],
                    row["client"],
                    row["executor"],
                    row["status_label"],
                    row["document_label"],
                    row["priority_label"],
                ]
            ).casefold()
        ]
    return filtered_rows, active_tab, document_filter, period_filter, client_filter, executor_filter, priority_filter, raw_query


def _tab_counts(rows):
    counts = {status_key: 0 for status_key, _label in DASHBOARD_STATUSES}
    counts["all"] = len([row for row in rows if row["queue_bucket"] != "ready"])
    for row in rows:
        counts[row["queue_bucket"]] = counts.get(row["queue_bucket"], 0) + 1
    return [
        {"key": status_key, "label": label, "count": counts.get(status_key, 0)}
        for status_key, label in DASHBOARD_STATUSES
    ]


def _client_options(rows, selected_client):
    clients = sorted({row["client"] for row in rows if row["client"] and row["client"] != "-"})
    return [
        {
            "value": client,
            "label": client,
            "selected": client == selected_client,
        }
        for client in clients
    ]


def _executor_options(rows, selected_executor):
    executors = sorted({row["executor"] for row in rows if row["executor"] and row["executor"] != "-"})
    return [
        {
            "value": executor,
            "label": executor,
            "selected": executor == selected_executor,
        }
        for executor in executors
    ]


def _priority_options(selected_priority):
    return [
        {
            "value": key,
            "label": label,
            "selected": key == selected_priority,
        }
        for key, label in Task.PRIORITY_CHOICES
    ]


def _priority_rows(rows, *, limit=8):
    priority_order = {"overdue": 0, "blocked": 1, "review": 2, "new": 3, "planning": 4, "in_work": 5, "ready": 9}
    return sorted(
        rows,
        key=lambda row: (
            priority_order.get(row["queue_bucket"], 9),
            0 if row["priority"] == "urgent" else 1,
            -(row["updated_at_value"].timestamp() if row.get("updated_at_value") else 0),
        ),
    )[:limit]


def _sorted_queue_rows(rows):
    priority_order = {"overdue": 0, "blocked": 1, "review": 2, "new": 3, "planning": 4, "in_work": 5, "ready": 9}
    return sorted(
        rows,
        key=lambda row: (
            priority_order.get(row["queue_bucket"], 9),
            0 if row["priority"] == "urgent" else 1,
            -(row["updated_at_value"].timestamp() if row.get("updated_at_value") else 0),
        ),
    )


def _priority_scope_rows(rows):
    return [
        row
        for row in rows
        if row["document_type"] == "processing"
        and row["queue_bucket"] in {"overdue", "new", "planning", "in_work"}
    ]


def _notifications(rows):
    messages = []
    for row in sorted(
        rows,
        key=lambda item: item["updated_at_value"].timestamp() if item.get("updated_at_value") else 0,
        reverse=True,
    )[:5]:
        if row["queue_bucket"] == "overdue":
            title = "Просрочена задача"
        elif row["queue_bucket"] == "blocked":
            title = "Критическое отклонение"
        elif row["queue_bucket"] == "review":
            title = "Задача готова к проверке"
        elif row["queue_bucket"] == "ready":
            title = "Обновлен план работ"
        else:
            title = "Новая заявка в работе"
        messages.append(
            {
                "title": title,
                "body": row["title"],
                "time": row["updated_at"],
                "tone": row["tone"],
                "url": row["url"],
            }
        )
    return messages


def _team_load():
    today = timezone.localdate()
    employees = Employee.objects.filter(
        role__in=("processing_head", "processing_worker"),
        is_active=True,
    ).annotate(
        active_tasks_count=Count(
            "tasks",
            filter=~Q(tasks__status="done") & (
                Q(tasks__route__contains="/orders/processing/")
                | Q(tasks__route__contains="/orders/receiving/")
                | Q(tasks__assigned_to__role__in=("processing_head", "processing_worker"))
            ),
            distinct=True,
        ),
        ready_tasks_count=Count(
            "tasks",
            filter=Q(tasks__status="done", tasks__updated_at__date=today),
            distinct=True,
        ),
    ).order_by("full_name")[:6]
    rows = []
    for employee in employees:
        active_count = employee.active_tasks_count
        ready_count = employee.ready_tasks_count
        load_value = min(96, 24 + active_count * 16 + ready_count * 6)
        if not active_count and not ready_count:
            load_value = 32
        tone = "danger" if load_value >= 85 else "warning" if load_value >= 70 else "success"
        rows.append(
            {
                "name": _short_name(employee.full_name),
                "value": load_value,
                "tone": tone,
            }
        )
    average = round(sum(row["value"] for row in rows) / len(rows)) if rows else 0
    return rows, average


def _processing_staff_rows():
    employees = (
        Employee.objects.filter(
            role__in=("processing_head", "processing_worker"),
            is_active=True,
        )
        .annotate(
            active_tasks_count=Count(
                "tasks",
                filter=~Q(tasks__status="done") & Q(tasks__route__contains="/orders/processing/"),
                distinct=True,
            ),
            review_tasks_count=Count(
                "tasks",
                filter=(
                    Q(tasks__route__contains="/orders/processing/")
                    & ~Q(tasks__status="done")
                    & (
                        Q(tasks__title__icontains="провер")
                        | Q(tasks__description__icontains="провер")
                        | Q(tasks__status="blocked")
                    )
                ),
                distinct=True,
            ),
            ready_tasks_count=Count(
                "tasks",
                filter=Q(tasks__status="done", tasks__route__contains="/orders/processing/"),
                distinct=True,
            ),
        )
        .order_by("role", "full_name")
    )
    role_labels = dict(Employee.ROLE_CHOICES)
    return [
        {
            "name": employee.full_name,
            "short_name": _short_name(employee.full_name),
            "role": role_labels.get(employee.role, employee.role),
            "active_tasks": employee.active_tasks_count,
            "review_tasks": employee.review_tasks_count,
            "ready_tasks": employee.ready_tasks_count,
            "tone": "warning" if employee.review_tasks_count else "info" if employee.active_tasks_count else "success",
        }
        for employee in employees
    ]


def _processing_stock_context():
    stock_queryset = (
        WarehouseStockSnapshot.objects.filter(is_archived=False, qty__gt=0)
        .filter(
            Q(zone_code__iexact="OBR")
            | Q(location__zone_code__iexact="OBR")
            | Q(warehouse_state_code__icontains="processing")
            | Q(processing_reserved_qty__gt=0)
        )
        .select_related("agency", "location", "container", "parent_container")
        .order_by("agency__short_name", "agency__agn_name", "sku_code", "size", "id")
    )
    totals = stock_queryset.aggregate(
        rows_count=Count("id"),
        qty_total=Sum("qty"),
        available_total=Sum("available_qty"),
        reserved_total=Sum("processing_reserved_qty"),
        boxes_count=Count("container_code", filter=~Q(container_code=""), distinct=True),
        pallets_count=Count("parent_container__container_code", distinct=True),
    )
    snapshots = list(stock_queryset[:80])
    missing_barcode_keys = {
        (
            int(snapshot.agency_id or 0),
            str(snapshot.barcode or "").strip(),
        )
        for snapshot in snapshots
        if not snapshot.sku_ref_id and str(snapshot.barcode or "").strip()
    }
    sku_id_by_client_barcode = (
        {
            (int(agency_id), str(value or "").strip()): int(sku_id)
            for agency_id, value, sku_id in SKUBarcode.objects.filter(
                agency_id__in={key[0] for key in missing_barcode_keys},
                value__in={key[1] for key in missing_barcode_keys},
                sku__deleted=False,
            ).values_list("agency_id", "value", "sku_id")
            if (int(agency_id), str(value or "").strip()) in missing_barcode_keys
        }
        if missing_barcode_keys
        else {}
    )
    stock_rows = []
    for snapshot in snapshots:
        location = ""
        if snapshot.location_id:
            location = (
                getattr(snapshot.location, "display_name", "")
                or getattr(snapshot.location, "location_code", "")
                or str(snapshot.location)
            )
        stock_rows.append(
            {
                "client": snapshot.agency.short_name or snapshot.agency.agn_name or f"Клиент {snapshot.agency_id}",
                "sku_ref_id": int(
                    snapshot.sku_ref_id
                    or sku_id_by_client_barcode.get(
                        (
                            int(snapshot.agency_id or 0),
                            str(snapshot.barcode or "").strip(),
                        )
                    )
                    or 0
                ),
                "article": snapshot.sku_code or "-",
                "name": snapshot.name or "-",
                "size": snapshot.size or "-",
                "barcode": snapshot.barcode or "-",
                "qty": snapshot.qty,
                "available_qty": snapshot.available_qty,
                "reserved_qty": snapshot.processing_reserved_qty,
                "box": snapshot.container_code or getattr(snapshot.container, "container_code", "") or "-",
                "pallet": getattr(snapshot.parent_container, "container_code", "") or "-",
                "zone": snapshot.zone_code or getattr(snapshot.location, "zone_code", "") or "-",
                "location": location or "-",
                "state": snapshot.warehouse_state_code or "-",
            }
        )
    return {
        "rows": stock_rows,
        "rows_count": totals.get("rows_count") or 0,
        "qty_total": totals.get("qty_total") or 0,
        "available_total": totals.get("available_total") or 0,
        "reserved_total": totals.get("reserved_total") or 0,
        "boxes_count": totals.get("boxes_count") or 0,
        "pallets_count": totals.get("pallets_count") or 0,
    }


def _processing_reports_context(task_rows):
    stock = _processing_stock_context()
    marking_queryset = MarkingCode.objects.filter(order_type="processing").select_related("agency")
    marking_totals = marking_queryset.aggregate(
        total=Count("id"),
        printed=Count("id", filter=Q(printed_at__isnull=False)),
        used=Count("id", filter=Q(used_at__isnull=False)),
        available=Count("id", filter=Q(used_at__isnull=True)),
    )
    marking_rows = list(
        marking_queryset.values(
            "agency__short_name",
            "agency__agn_name",
            "order_id",
            "sku_code",
            "size",
        )
        .annotate(
            total=Count("id"),
            printed=Count("id", filter=Q(printed_at__isnull=False)),
            used=Count("id", filter=Q(used_at__isnull=False)),
            available=Count("id", filter=Q(used_at__isnull=True)),
        )
        .order_by("agency__short_name", "agency__agn_name", "order_id", "sku_code", "size")[:80]
    )
    for row in marking_rows:
        row["client"] = row["agency__short_name"] or row["agency__agn_name"] or "—"
        row["order_number"] = row["order_id"] or "—"
        row["article"] = row["sku_code"] or "—"
        row["size_label"] = row["size"] or "—"

    active_rows = _priority_scope_rows(task_rows)
    return {
        "processing": {
            "total": len(task_rows),
            "active": len(active_rows),
            "ready": sum(1 for row in task_rows if row["queue_bucket"] == "ready"),
            "overdue": sum(1 for row in task_rows if row["queue_bucket"] == "overdue"),
            "rows": _sorted_queue_rows(task_rows)[:80],
        },
        "goods": stock,
        "marking": {
            "total": marking_totals.get("total") or 0,
            "printed": marking_totals.get("printed") or 0,
            "used": marking_totals.get("used") or 0,
            "available": marking_totals.get("available") or 0,
            "rows": marking_rows,
        },
    }


def _dashboard_kpis(task_rows, stock):
    active_rows = [row for row in task_rows if row["queue_bucket"] != "ready"]
    overdue_rows = [row for row in task_rows if row["queue_bucket"] == "overdue"]
    review_rows = [row for row in task_rows if row["queue_bucket"] == "review"]
    unassigned_rows = [row for row in task_rows if row["is_unassigned"]]
    critical_rows = [row for row in task_rows if row["queue_bucket"] in {"overdue", "blocked"}]
    marking = MarkingCode.objects.filter(order_type="processing")
    marking_total = marking.count()
    marking_used = marking.filter(used_at__isnull=False).count()
    marking_available = marking.filter(used_at__isnull=True).count()
    return [
        {"label": "Активные заявки", "value": len(active_rows), "hint": f"Просрочено: {len(overdue_rows)} · завершено: {sum(row['queue_bucket'] == 'ready' for row in task_rows)}", "tone": "accent", "icon": "☑", "url": "/processing-head/requests/?tab=all&period=all"},
        {"label": "Товары в обработке", "value": stock["qty_total"], "hint": f"В резерве: {stock['reserved_total']} · доступно: {stock['available_total']}", "tone": "info", "icon": "□", "url": "/processing-head/?section=stock&period=all"},
        {"label": "КИЗы / ЧЗ", "value": marking_total, "hint": f"Использовано: {marking_used} · доступно: {marking_available}", "tone": "success", "icon": "⌁", "url": "/processing-head/reports/kiz"},
        {"label": "Без исполнителя", "value": len(unassigned_rows), "hint": "Требуют назначения", "tone": "warning" if unassigned_rows else "success", "icon": "◌", "url": "/processing-head/requests/?executor=__none__&period=all"},
        {"label": "На проверке", "value": len(review_rows), "hint": "Ожидают решения руководителя", "tone": "warning" if review_rows else "success", "icon": "✓", "url": "/processing-head/requests/?tab=review&period=all"},
        {"label": "Критические отклонения", "value": len(critical_rows), "hint": "Просрочки и блокировки", "tone": "danger" if critical_rows else "success", "icon": "!", "url": "/processing-head/requests/?tab=overdue&period=all"},
    ]


def _attention_rows(task_rows):
    rows = _priority_rows(
        [row for row in task_rows if row["queue_bucket"] in {"overdue", "blocked", "review"} or row["is_unassigned"]],
        limit=5,
    )
    for row in rows:
        if row["queue_bucket"] == "overdue":
            row["attention_reason"] = "Просрочен срок выполнения"
        elif row["queue_bucket"] == "blocked":
            row["attention_reason"] = "Критическое отклонение"
        elif row["queue_bucket"] == "review":
            row["attention_reason"] = "Ожидает проверки"
        else:
            row["attention_reason"] = "Не назначен исполнитель"
    return rows


def _printer_settings_cards():
    return [
        {
            "title": "Настройки печати этикеток",
            "description": "Размеры, поля, шрифты и состав этикеток товара, коробов и палет.",
            "url": "/labels/settings/?return=/processing-head/%3Fsection%3Dsettings",
            "action": "Открыть настройки",
            "tone": "accent",
        },
        {
            "title": "Агент печати",
            "description": "Пакет синхронизации принтеров для рабочего места обработки.",
            "url": "/orders/processing/print-agent/guide/",
            "action": "Скачать и открыть инструкцию",
            "tone": "info",
        },
    ]


class ProcessingHeadDashboard(RoleRequiredMixin, TemplateView):
    template_name = "processing_head/dashboard.html"
    allowed_roles = ("processing_head",)
    default_section = "dashboard"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        now = timezone.now()
        active_section = str(self.request.GET.get("section") or self.default_section).strip().lower()
        if active_section not in {"dashboard", "tasks", "requests", "planning", "work", "stock", "personnel", "reports", "kpi", "settings"}:
            active_section = "dashboard"
        task_limit = 500 if active_section == "requests" else 120
        task_queryset = Task.objects.select_related("assigned_to", "observer", "created_by").filter(
            REQUESTS_SECTION_ORDER_Q if active_section == "requests" else PROCESSING_ORDER_Q
        )
        if active_section != "requests":
            task_queryset = task_queryset.filter(PROCESSING_WORKFLOW_ROLE_Q)
        task_queryset = task_queryset.distinct().order_by("-updated_at", "-id")[:task_limit]
        task_rows = [
            row
            for row in _build_task_rows(list(task_queryset), now=now)
            if active_section == "requests" or row["document_type"] == "processing"
        ]
        if active_section == "requests":
            task_rows = _dedupe_request_rows(task_rows)
        filtered_rows, active_tab, document_filter, period_filter, selected_client, selected_executor, selected_priority, query = _apply_filters(
            task_rows,
            self.request,
            now=now,
            default_period="all" if active_section == "dashboard" else "today",
        )
        employee = get_request_employee(self.request)
        employee_name = ""
        if employee:
            employee_name = employee.full_name
        elif self.request.user.is_authenticated:
            employee_name = self.request.user.get_full_name() or self.request.user.username
        else:
            employee_name = "Руководитель обработки"
        team_load_rows, average_load = _team_load()
        context["role"] = "processing_head"
        context["title"] = "Руководитель обработки"
        context["active_section"] = active_section
        context["is_requests_page"] = active_section == "requests" and self.request.path.rstrip("/") == "/processing-head/requests"
        context["employee_name"] = employee_name
        context["employee_initials"] = _initials(employee_name)
        context["today"] = timezone.localdate()
        processing_stock = _processing_stock_context()
        context["metric_cards"] = _dashboard_kpis(task_rows, processing_stock)
        context["status_tabs"] = _tab_counts(task_rows)
        context["active_tab"] = active_tab
        context["document_filters"] = [
            {
                "key": key,
                "label": label,
                "selected": key == document_filter,
            }
            for key, label in DOCUMENT_FILTERS
        ]
        context["period_filters"] = [
            {
                "key": key,
                "label": label,
                "selected": key == period_filter,
            }
            for key, label in PERIOD_FILTERS
        ]
        context["client_options"] = _client_options(task_rows, selected_client)
        context["executor_options"] = _executor_options(task_rows, selected_executor)
        context["priority_options"] = _priority_options(selected_priority)
        context["selected_client"] = selected_client
        context["selected_executor"] = selected_executor
        context["selected_priority"] = selected_priority
        context["search_query"] = query
        if active_section == "requests":
            priority_source_rows = filtered_rows
            total_source_rows = task_rows
        else:
            priority_source_rows = _priority_scope_rows(filtered_rows)
            total_source_rows = _priority_scope_rows(task_rows)
        context["filtered_count"] = len(priority_source_rows)
        context["total_count"] = len(total_source_rows)
        context["filter_reset_url"] = (
            "/processing-head/requests/?tab=all&period=all"
            if active_section == "requests"
            else "/processing-head/"
        )
        if active_section in {"tasks", "requests"}:
            context["queue_title"] = "Все задачи обработки" if active_section == "tasks" else "Все заявки"
            context["queue_subtitle"] = f"Рабочий список заявок · найдено {len(priority_source_rows)} из {len(total_source_rows)}"
            context["queue_action_label"] = "Главная"
            context["queue_action_url"] = "/processing-head/"
            context["priority_tasks"] = _sorted_queue_rows(priority_source_rows)
        else:
            context["queue_title"] = "Приоритетные задачи"
            context["queue_subtitle"] = f"Рабочая очередь заявок обработки · найдено {len(priority_source_rows)} из {len(_priority_scope_rows(task_rows))}"
            context["queue_action_label"] = "Все заявки"
            context["queue_action_url"] = "/processing-head/requests/?tab=all&period=all"
            context["priority_tasks"] = _sorted_queue_rows(priority_source_rows)
        context["notifications"] = _notifications(task_rows)
        context["attention_tasks"] = _attention_rows(task_rows)
        context["unassigned_tasks"] = _priority_rows([row for row in task_rows if row["is_unassigned"]], limit=5)
        context["review_tasks"] = _priority_rows([row for row in task_rows if row["queue_bucket"] == "review"], limit=5)
        context["critical_tasks"] = _priority_rows(
            [row for row in task_rows if row["queue_bucket"] in {"overdue", "blocked"}]
        )[:5]
        context["team_load_rows"] = team_load_rows
        context["average_load"] = average_load
        context["processing_staff_rows"] = _processing_staff_rows()
        context["rightbar_counts"] = {
            "unassigned": len([row for row in task_rows if row["is_unassigned"]]),
            "review": len([row for row in task_rows if row["queue_bucket"] == "review"]),
            "critical": len([row for row in task_rows if row["queue_bucket"] in {"overdue", "blocked"}]),
        }
        context["processing_stock"] = processing_stock
        context["processing_reports"] = _processing_reports_context(task_rows)
        context["printer_settings_cards"] = _printer_settings_cards()
        return context


class ProcessingHeadRequestsView(ProcessingHeadDashboard):
    default_section = "requests"
